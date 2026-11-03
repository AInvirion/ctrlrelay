"""Per-repository switches for the dev pipeline's work steps (#226).

The order of the steps is fixed in code. A repository sets each work step
to ``off``, ``on`` or ``required`` under ``repos[].steps``, and caps the CI
fix loop with ``repos[].max_fix_attempts``:

- ``on``: a failure is reported and the run continues.
- ``required``: a failure stops the run, and no route skips the step. A
  step that cannot run (a reviewer that cannot start, CI that never
  answered) has not passed.
- ``off``: the step does not run.

Unset keys reproduce what the pipeline did before the switches existed.
``code`` and ``publish`` cannot be anything but required in a dev run, so
the config refuses ``off`` and ``on`` for them rather than accepting and
ignoring them. ``code_review.method: off`` keeps working; set together
with ``steps.review`` it must agree, or the config refuses to load.

The cases below are written from that contract. Each one drives the real
``run_dev_issue`` (or ``resume_dev_from_pending``) with the fakes the
existing dev tests use, and checks what the operator would see: the
result, the session row, the issue comment, the chat announcement.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import ctrlrelay.pipelines.dev as dev
from ctrlrelay.cli import announce_issue_outcome
from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
from ctrlrelay.core.code_review import CodeReviewOutcome, ReviewStatus
from ctrlrelay.core.config import (
    CodeReviewConfig,
    ConfigError,
    DevStepsConfig,
    RepoConfig,
    load_config,
)
from ctrlrelay.core.dispatcher import SessionResult
from ctrlrelay.core.pr_verifier import VerificationResult
from ctrlrelay.core.state import StateDB
from tests._publish_fakes import fake_relay_publish

PR = 42


# --- fakes ------------------------------------------------------------------


def _session(session_id: str, status: CheckpointStatus) -> SessionResult:
    return SessionResult(
        session_id=session_id,
        exit_code=0,
        stdout="",
        stderr="",
        state=CheckpointState(
            version="1",
            status=status,
            session_id=session_id,
            timestamp="2026-11-02T12:00:00Z",
            summary="done" if status is CheckpointStatus.DONE else "failed",
            error=None if status is CheckpointStatus.DONE else "agent broke",
            outputs={},
        ),
    )


def _dispatcher(status: CheckpointStatus = CheckpointStatus.DONE) -> AsyncMock:
    async def spawn(**kwargs):
        return _session(kwargs["session_id"], status)

    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = spawn
    return dispatcher


def _fakes(tmp_path: Path) -> tuple[AsyncMock, AsyncMock]:
    worktree = AsyncMock()
    worktree.push_run_branch.return_value = "c0ffee"
    worktree.create_worktree_with_new_branch.return_value = (
        tmp_path / "worktree",
        True,
    )
    worktree.get_default_branch.return_value = "main"
    worktree.make_review_copy.return_value = tmp_path / "review-copy"
    worktree.branch_exists_locally.return_value = False
    worktree.branch_exists_on_remote.return_value = True
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()

    github = AsyncMock()
    fake_relay_publish(github, pr_number=PR)
    github.get_issue.return_value = {
        "number": 123,
        "title": "Fix bug",
        "body": "Bug description",
        "comments": [],
    }
    return worktree, github


def _verifier(*results: VerificationResult) -> AsyncMock:
    verifier = AsyncMock()
    verifier.verify.side_effect = list(results)
    return verifier


GREEN = VerificationResult(ready=True)
RED = VerificationResult(
    ready=False,
    reason="1 failing check",
    failing_checks=[{"name": "ci", "state": "FAILURE"}],
)
SLOW = VerificationResult(ready=False, timed_out=True, reason="CI still pending")


def _reviewer(monkeypatch, outcome: CodeReviewOutcome | Exception) -> AsyncMock:
    fake = AsyncMock()
    if isinstance(outcome, Exception):
        fake.side_effect = outcome
    else:
        fake.return_value = outcome
    monkeypatch.setattr(dev, "run_code_review", fake)
    return fake


REVIEWED = CodeReviewOutcome(
    status=ReviewStatus.REVIEWED,
    output="Looks fine.",
    evidence=("git diff origin/main...HEAD",),
)
CANNOT_RUN = CodeReviewOutcome(
    status=ReviewStatus.FAILED, error="reviewer not found on PATH"
)
NO_EVIDENCE = CodeReviewOutcome(
    status=ReviewStatus.NO_EVIDENCE, output="No findings."
)


def _repo(**raw) -> RepoConfig:
    return RepoConfig(name="owner/repo", **raw)


async def _run(
    tmp_path: Path,
    *,
    repo: RepoConfig | None = None,
    steps=None,
    code_review=None,
    verifier: AsyncMock | None = None,
    worktree=None,
    github=None,
    dispatcher=None,
    state_db: StateDB | None = None,
    max_fix_attempts: int | None = None,
):
    if worktree is None or github is None:
        worktree, github = _fakes(tmp_path)
    kwargs = {}
    if repo is not None:
        kwargs["steps"] = repo.steps
        kwargs["code_review"] = repo.code_review
        kwargs["max_fix_attempts"] = repo.max_fix_attempts
    if steps is not None:
        kwargs["steps"] = steps
    if code_review is not None:
        kwargs["code_review"] = code_review
    if max_fix_attempts is not None:
        kwargs["max_fix_attempts"] = max_fix_attempts
    result = await dev.run_dev_issue(
        claims=None,
        repo="owner/repo",
        issue_number=123,
        branch_template="fix/issue-{n}",
        dispatcher=dispatcher or _dispatcher(),
        github=github,
        worktree=worktree,
        dashboard=None,
        state_db=state_db or StateDB(tmp_path / "s.db"),
        transport=None,
        contexts_dir=tmp_path / "contexts",
        pr_verifier=verifier if verifier is not None else _verifier(GREEN),
        **kwargs,
    )
    return result, worktree, github


async def _announced(result) -> list[str]:
    transport = AsyncMock()
    return await announce_issue_outcome(
        transport, repo="owner/repo", issue_number=123, is_task=False,
        result=result,
    )


def _outcome_comments(github) -> list[str]:
    return [
        c.kwargs["body"]
        for c in github.comment_on_issue.await_args_list
        if dev.AGENT_OUTCOME_MARKER in c.kwargs.get("body", "")
    ]


def _assert_stopped_after_pr(result, worktree, github, state_db) -> None:
    """What a required step that did not pass leaves, once the PR exists.

    Reuses the ordinary failure path: the session is failed, the
    worktree is removed and the pushed branch kept, the claim is
    retracted naming the PR, and the announcement is the failure line,
    never "PR ready".
    """
    assert not result.success
    assert not result.blocked
    assert result.outputs["pr_number"] == PR
    row = state_db.execute("SELECT status FROM sessions").fetchone()
    assert row[0] == "failed"
    worktree.remove_worktree.assert_awaited()
    worktree.delete_branch.assert_not_awaited()
    bodies = _outcome_comments(github)
    assert len(bodies) == 1, bodies
    assert f"Pull request #{PR} was opened" in bodies[0]


# --- config: shape, defaults, refusals --------------------------------------


def test_defaults_are_unset_and_the_fix_cap_is_todays() -> None:
    repo = _repo()
    assert repo.steps == DevStepsConfig()
    assert (repo.steps.code, repo.steps.publish) == (None, None)
    assert (repo.steps.verify, repo.steps.review) == (None, None)
    assert repo.max_fix_attempts == dev.DEFAULT_MAX_FIX_ATTEMPTS
    assert repo.code_review.method == "cli"


@pytest.mark.parametrize("step", ["code", "publish"])
@pytest.mark.parametrize("mode", ["off", "on"])
def test_code_and_publish_cannot_be_switched_off_or_on(step, mode) -> None:
    with pytest.raises(ValueError, match=rf"steps\.{step}"):
        _repo(steps={step: mode})


@pytest.mark.parametrize("step", ["code", "publish"])
def test_code_and_publish_accept_required(step) -> None:
    assert getattr(_repo(steps={step: "required"}).steps, step) == "required"


def test_verify_on_is_refused_not_ignored() -> None:
    with pytest.raises(ValueError, match=r"steps\.verify"):
        _repo(steps={"verify": "on"})


@pytest.mark.parametrize("mode", ["off", "required"])
def test_verify_accepts_off_and_required(mode) -> None:
    assert _repo(steps={"verify": mode}).steps.verify == mode


@pytest.mark.parametrize("mode", ["off", "on", "required"])
def test_review_accepts_every_mode(mode) -> None:
    assert _repo(steps={"review": mode}).steps.review == mode


@pytest.mark.parametrize(
    "steps",
    [
        {"verfy": "required"},  # a typo must not read as "unset"
        {"merge": "on"},  # not a dev work step (yet)
        {"plan": "required"},
    ],
)
def test_an_unknown_step_name_is_refused(steps) -> None:
    with pytest.raises(ValueError):
        _repo(steps=steps)


@pytest.mark.parametrize("value", ["maybe", "REQUIRED ", "", 3])
def test_an_unknown_mode_is_refused(value) -> None:
    with pytest.raises(ValueError):
        _repo(steps={"review": value})


@pytest.mark.parametrize("mode", ["on", "required"])
def test_review_method_off_and_review_on_or_required_disagree(mode) -> None:
    with pytest.raises(ValueError, match=r"code_review\.method"):
        _repo(code_review={"method": "off"}, steps={"review": mode})


def test_review_off_and_an_explicit_cli_method_disagree() -> None:
    with pytest.raises(ValueError, match=r"code_review\.method"):
        _repo(code_review={"method": "cli"}, steps={"review": "off"})


def test_review_off_switches_the_reviewer_off() -> None:
    """Off in one place is off everywhere that reads the reviewer, which
    includes the startup probe that looks for the reviewer binary."""
    repo = _repo(steps={"review": "off"})
    assert repo.code_review.method == "off"


def test_review_method_off_alone_still_loads() -> None:
    repo = _repo(code_review={"method": "off"})
    assert repo.code_review.method == "off"
    assert repo.steps.review is None


def test_both_off_agree() -> None:
    repo = _repo(code_review={"method": "off"}, steps={"review": "off"})
    assert repo.code_review.method == "off"


def test_required_review_with_comment_on_pr_off_is_refused() -> None:
    """Required means findings reach the pull request; with comments off
    they never do, so the two settings cannot both hold."""
    with pytest.raises(ValueError, match="comment_on_pr"):
        _repo(code_review={"comment_on_pr": False}, steps={"review": "required"})


@pytest.mark.parametrize("value", [-1, -5])
def test_a_negative_fix_cap_is_refused(value) -> None:
    with pytest.raises(ValueError):
        _repo(max_fix_attempts=value)


def test_a_zero_fix_cap_is_allowed() -> None:
    assert _repo(max_fix_attempts=0).max_fix_attempts == 0


def _write(tmp_path: Path, base: dict, repos_yaml: str) -> Path:
    base = dict(base)
    base.pop("repos", None)
    path = tmp_path / "orchestrator.yaml"
    path.write_text(yaml.dump(base) + textwrap.dedent(repos_yaml))
    return path


def test_unquoted_yaml_on_and_off_load_as_modes(
    tmp_path: Path, sample_config_dict: dict
) -> None:
    """YAML 1.1 reads bare ``on``/``off`` as booleans. Written the way a
    person writes it, the config must still mean what it says."""
    path = _write(
        tmp_path,
        sample_config_dict,
        """
        repos:
          - name: owner/repo
            local_path: /tmp/owner-repo
            max_fix_attempts: 1
            steps:
              verify: off
              review: on
        """,
    )
    repo = load_config(path).repos[0]
    assert repo.steps.verify == "off"
    assert repo.steps.review == "on"
    assert repo.max_fix_attempts == 1


def test_a_refusal_names_the_setting_at_load(
    tmp_path: Path, sample_config_dict: dict
) -> None:
    path = _write(
        tmp_path,
        sample_config_dict,
        """
        repos:
          - name: owner/repo
            local_path: /tmp/owner-repo
            steps:
              code: off
        """,
    )
    with pytest.raises(ConfigError, match=r"steps\.code"):
        load_config(path)


def test_the_pipeline_refuses_a_mode_the_config_would_refuse(
    tmp_path: Path,
) -> None:
    """A caller that bypasses the config still cannot get a switch
    accepted and ignored."""
    import asyncio

    for steps in (
        SimpleNamespace(code="off"),
        SimpleNamespace(publish="on"),
        SimpleNamespace(verify="on"),
        SimpleNamespace(review="sometimes"),
    ):
        with pytest.raises(ValueError):
            asyncio.run(_run(tmp_path, steps=steps))


# --- code -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "required"])
async def test_code_failure_stops_the_run(tmp_path: Path, mode) -> None:
    worktree, github = _fakes(tmp_path)
    verifier = _verifier(GREEN)
    result, *_ = await _run(
        tmp_path,
        repo=_repo(steps={"code": mode} if mode else {}),
        dispatcher=_dispatcher(CheckpointStatus.FAILED),
        worktree=worktree, github=github, verifier=verifier,
    )
    assert not result.success
    worktree.push_run_branch.assert_not_awaited()
    github.create_pr.assert_not_awaited()
    verifier.verify.assert_not_awaited()


# --- publish ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "required"])
async def test_publish_failure_stops_the_run(tmp_path: Path, mode) -> None:
    worktree, github = _fakes(tmp_path)
    worktree.push_run_branch.side_effect = RuntimeError("remote said no")
    verifier = _verifier(GREEN)
    result, *_ = await _run(
        tmp_path,
        repo=_repo(steps={"publish": mode} if mode else {}),
        worktree=worktree, github=github, verifier=verifier,
    )
    assert not result.success
    assert result.outputs.get("publish_failed") is True
    verifier.verify.assert_not_awaited()


# --- verify -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_unset_hands_over_a_pr_whose_ci_timed_out(
    tmp_path: Path, monkeypatch
) -> None:
    """Today's behaviour, kept as the default: slow CI is not a failure."""
    _reviewer(monkeypatch, REVIEWED)
    result, *_ = await _run(tmp_path, repo=_repo(), verifier=_verifier(SLOW))
    assert result.success
    assert result.review is not None and result.review.posted


@pytest.mark.asyncio
async def test_verify_unset_fails_a_pr_still_red_after_the_cap(
    tmp_path: Path, monkeypatch
) -> None:
    reviewer = _reviewer(monkeypatch, REVIEWED)
    result, *_ = await _run(
        tmp_path, repo=_repo(max_fix_attempts=1),
        verifier=_verifier(RED, RED),
    )
    assert not result.success
    assert "not ready after 1 fix attempt" in result.summary
    reviewer.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_off_waits_for_no_ci_and_still_reviews(
    tmp_path: Path, monkeypatch
) -> None:
    reviewer = _reviewer(monkeypatch, REVIEWED)
    verifier = _verifier(RED)
    state_db = StateDB(tmp_path / "s.db")
    result, *_ = await _run(
        tmp_path, repo=_repo(steps={"verify": "off"}), verifier=verifier,
        state_db=state_db,
    )
    assert result.success, result
    assert result.outputs["pr_number"] == PR
    verifier.verify.assert_not_awaited()
    reviewer.assert_awaited_once()
    assert result.review.posted
    kinds = [e["kind"] for e in state_db.list_events(limit=100)]
    assert "ci_started" not in kinds
    assert "✅ PR ready: https://github.com/owner/repo/pull/42" in (
        await _announced(result)
    )


@pytest.mark.asyncio
async def test_verify_required_green_hands_over(
    tmp_path: Path, monkeypatch
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    result, *_ = await _run(
        tmp_path, repo=_repo(steps={"verify": "required"}),
        verifier=_verifier(GREEN),
    )
    assert result.success, result


@pytest.mark.asyncio
async def test_verify_required_fails_when_ci_never_answered(
    tmp_path: Path, monkeypatch
) -> None:
    """CI that timed out has not passed. Under required it stops the run
    with the PR open, and the operator is told it failed."""
    reviewer = _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    state_db = StateDB(tmp_path / "s.db")
    result, *_ = await _run(
        tmp_path, repo=_repo(steps={"verify": "required"}),
        verifier=_verifier(SLOW), worktree=worktree, github=github,
        state_db=state_db,
    )
    _assert_stopped_after_pr(result, worktree, github, state_db)
    assert "CI still pending" in (result.error or "")
    reviewer.assert_not_awaited()
    sent = await _announced(result)
    assert len(sent) == 1 and sent[0].startswith("❌ Failed on #123")
    assert not any("PR ready" in s for s in sent)


@pytest.mark.asyncio
async def test_verify_required_fails_when_ci_times_out_after_a_fix(
    tmp_path: Path, monkeypatch
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    state_db = StateDB(tmp_path / "s.db")
    dispatcher = _dispatcher()
    result, *_ = await _run(
        tmp_path, repo=_repo(steps={"verify": "required"}),
        verifier=_verifier(RED, SLOW), worktree=worktree, github=github,
        state_db=state_db, dispatcher=dispatcher,
    )
    # The fix round ran (a second agent spawn), then CI never answered.
    assert dispatcher.spawn_session.await_count == 2
    _assert_stopped_after_pr(result, worktree, github, state_db)


@pytest.mark.asyncio
async def test_verify_required_fails_when_the_verifier_raises(
    tmp_path: Path, monkeypatch
) -> None:
    reviewer = _reviewer(monkeypatch, REVIEWED)
    verifier = AsyncMock()
    verifier.verify.side_effect = RuntimeError("every checks read failed")
    result, *_ = await _run(
        tmp_path, repo=_repo(steps={"verify": "required"}), verifier=verifier,
    )
    assert not result.success
    reviewer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, 2])
async def test_the_fix_cap_bounds_the_fix_rounds(
    tmp_path: Path, monkeypatch, cap
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    dispatcher = _dispatcher()
    result, *_ = await _run(
        tmp_path, repo=_repo(max_fix_attempts=cap),
        verifier=_verifier(*([RED] * (cap + 1))), dispatcher=dispatcher,
    )
    assert not result.success
    # One spawn for the code step, one per fix round.
    assert dispatcher.spawn_session.await_count == 1 + cap


# --- review -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "on"])
async def test_review_on_reports_a_failed_review_and_hands_over(
    tmp_path: Path, monkeypatch, mode
) -> None:
    """Today's behaviour, and what ``on`` means: the PR is handed over,
    with the unreviewed warning in front of the routine line."""
    _reviewer(monkeypatch, CANNOT_RUN)
    result, *_ = await _run(
        tmp_path, repo=_repo(steps={"review": mode} if mode else {}),
    )
    assert result.success, result
    assert result.review.is_failure
    sent = await _announced(result)
    assert len(sent) == 2
    assert "NO REVIEW ON THIS PR" in sent[0]
    assert sent[1] == "✅ PR ready: https://github.com/owner/repo/pull/42"


@pytest.mark.asyncio
async def test_review_off_runs_no_reviewer_and_warns_nobody(
    tmp_path: Path, monkeypatch
) -> None:
    reviewer = _reviewer(monkeypatch, CANNOT_RUN)
    worktree, github = _fakes(tmp_path)
    result, *_ = await _run(
        tmp_path, repo=_repo(steps={"review": "off"}),
        worktree=worktree, github=github,
    )
    assert result.success, result
    reviewer.assert_not_awaited()
    worktree.make_review_copy.assert_not_awaited()
    assert not result.review.is_failure
    assert await _announced(result) == [
        "✅ PR ready: https://github.com/owner/repo/pull/42"
    ]


@pytest.mark.parametrize(
    ("steps", "code_review"),
    [
        (SimpleNamespace(review="off"), CodeReviewConfig()),
        (SimpleNamespace(review="on"), CodeReviewConfig(method="off")),
        (SimpleNamespace(review="required"), CodeReviewConfig(method="off")),
    ],
)
def test_the_pipeline_refuses_review_settings_that_disagree(
    tmp_path: Path, steps, code_review
) -> None:
    """A caller that bypasses the config cannot have one switch say off
    and the other on; the run refuses before it takes the lock."""
    import asyncio

    with pytest.raises(ValueError, match="code_review.method"):
        asyncio.run(_run(tmp_path, steps=steps, code_review=code_review))


@pytest.mark.asyncio
async def test_review_required_and_posted_hands_over(
    tmp_path: Path, monkeypatch
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    result, *_ = await _run(
        tmp_path, repo=_repo(steps={"review": "required"}),
        worktree=worktree, github=github,
    )
    assert result.success, result
    assert result.review.posted
    github.comment_on_pr.assert_awaited_once()
    assert await _announced(result) == [
        "✅ PR ready: https://github.com/owner/repo/pull/42"
    ]


@pytest.mark.asyncio
async def test_review_required_with_a_reviewer_that_cannot_run_stops_the_run(
    tmp_path: Path, monkeypatch
) -> None:
    """A reviewer that cannot run is not reviewed, never a pass. The PR
    stays open, is not announced ready, and the claim is retracted."""
    _reviewer(monkeypatch, CANNOT_RUN)
    worktree, github = _fakes(tmp_path)
    state_db = StateDB(tmp_path / "s.db")
    result, *_ = await _run(
        tmp_path, repo=_repo(steps={"review": "required"}),
        worktree=worktree, github=github, state_db=state_db,
    )
    _assert_stopped_after_pr(result, worktree, github, state_db)
    assert result.review is not None and result.review.is_failure
    assert "reviewer not found on PATH" in dev.failure_text(result)
    github.close_pr.assert_not_awaited()
    sent = await _announced(result)
    assert len(sent) == 1
    assert sent[0].startswith("❌ Failed on #123")
    assert "review" in sent[0]
    assert not any("PR ready" in s for s in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["no_evidence", "comment_failed", "copy_failed", "base_unknown", "raises"],
)
async def test_review_required_fails_on_every_way_a_review_does_not_post(
    tmp_path: Path, monkeypatch, case
) -> None:
    worktree, github = _fakes(tmp_path)
    if case == "no_evidence":
        _reviewer(monkeypatch, NO_EVIDENCE)
    elif case == "raises":
        _reviewer(monkeypatch, RuntimeError("reviewer crashed"))
    else:
        _reviewer(monkeypatch, REVIEWED)
    if case == "comment_failed":
        github.comment_on_pr.side_effect = RuntimeError("gh 502")
    if case == "copy_failed":
        worktree.make_review_copy.side_effect = RuntimeError("disk full")
    if case == "base_unknown":
        worktree.get_default_branch.side_effect = RuntimeError("no remote")
    state_db = StateDB(tmp_path / "s.db")
    result, *_ = await _run(
        tmp_path, repo=_repo(steps={"review": "required"}),
        worktree=worktree, github=github, state_db=state_db,
    )
    _assert_stopped_after_pr(result, worktree, github, state_db)
    assert not result.review.posted


@pytest.mark.asyncio
async def test_review_required_with_findings_kept_in_the_log_is_not_a_pass(
    tmp_path: Path, monkeypatch
) -> None:
    """The config refuses this pair; a caller that bypasses it still does
    not get a pass for findings nobody can read on the PR."""
    _reviewer(monkeypatch, REVIEWED)
    result, *_ = await _run(
        tmp_path,
        steps=SimpleNamespace(review="required"),
        code_review=SimpleNamespace(method="cli", comment_on_pr=False),
    )
    assert not result.success


@pytest.mark.asyncio
async def test_review_required_after_a_red_ci_keeps_the_ci_reason(
    tmp_path: Path, monkeypatch
) -> None:
    reviewer = _reviewer(monkeypatch, CANNOT_RUN)
    result, *_ = await _run(
        tmp_path,
        repo=_repo(steps={"review": "required"}, max_fix_attempts=0),
        verifier=_verifier(RED),
    )
    assert not result.success
    assert "not ready after 0 fix attempt" in result.summary
    reviewer.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_steps_still_fail_when_there_is_no_pull_request(
    tmp_path: Path, monkeypatch
) -> None:
    """No route around a required step: a run that reached verify and
    review with a success but no pull request has not passed either."""
    reviewer = _reviewer(monkeypatch, REVIEWED)

    async def _publish_nothing(run: dev._DevRun) -> None:
        # A publisher exists (the later steps need one) but nothing was
        # pushed and no pull request number reached the result.
        run.publisher = dev._Publisher(
            github=run.github, worktree=run.worktree, state_db=run.state_db,
            repo=run.repo, branch=run.branch_name,
            issue_number=run.issue_number, issue_title="",
            session_id=run.session_id,
        )

    steps = {s.name: s for s in dev.DEV_WORK_STEPS}
    for required in ("verify", "review"):
        monkeypatch.setattr(
            dev,
            "DEV_WORK_STEPS",
            (
                steps["code"],
                dev.DevWorkStep("publish", _publish_nothing),
                steps["verify"],
                steps["review"],
            ),
        )
        result, *_ = await _run(
            tmp_path / required,
            repo=_repo(steps={required: "required"}),
        )
        assert not result.success, required
    reviewer.assert_not_awaited()


# --- resume -----------------------------------------------------------------


def _blocked_row(state_db: StateDB, session_id: str) -> None:
    state_db.execute(
        """INSERT INTO sessions
           (id, pipeline, repo, worktree_path, status, started_at,
            issue_number)
           VALUES (?, 'dev', 'owner/repo', '/nonexistent', 'blocked', 0, 123)""",
        (session_id,),
    )
    state_db.commit()


async def _resume(
    tmp_path: Path,
    *,
    repo: RepoConfig,
    verifier: AsyncMock,
    worktree=None,
    github=None,
):
    if worktree is None or github is None:
        worktree, github = _fakes(tmp_path)
    state_db = StateDB(tmp_path / "s.db")
    session_id = "dev-owner-repo-123-abcdef01"
    _blocked_row(state_db, session_id)
    result = await dev.resume_dev_from_pending(
        session_id=session_id,
        repo="owner/repo",
        answer="go ahead",
        branch_template="fix/issue-{n}",
        dispatcher=_dispatcher(),
        github=github,
        worktree=worktree,
        dashboard=None,
        state_db=state_db,
        transport=None,
        contexts_dir=tmp_path / "contexts",
        pr_verifier=verifier,
        max_fix_attempts=repo.max_fix_attempts,
        code_review=repo.code_review,
        steps=repo.steps,
    )
    return result, state_db


@pytest.mark.asyncio
async def test_resume_with_defaults_does_what_it_did(
    tmp_path: Path, monkeypatch
) -> None:
    """Resume never reviewed, and a slow CI was handed over."""
    reviewer = _reviewer(monkeypatch, CANNOT_RUN)
    result, _ = await _resume(tmp_path, repo=_repo(), verifier=_verifier(SLOW))
    assert result.success, result
    assert result.outputs["pr_number"] == PR
    assert result.review is None
    reviewer.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_cannot_skip_a_required_review(
    tmp_path: Path, monkeypatch
) -> None:
    reviewer = _reviewer(monkeypatch, CANNOT_RUN)
    result, state_db = await _resume(
        tmp_path, repo=_repo(steps={"review": "required"}),
        verifier=_verifier(GREEN),
    )
    reviewer.assert_awaited_once()
    assert not result.success
    assert result.review is not None and result.review.is_failure
    row = state_db.execute("SELECT status FROM sessions").fetchone()
    assert row[0] == "failed"


@pytest.mark.asyncio
async def test_resume_with_a_required_review_that_posts_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    result, _ = await _resume(
        tmp_path, repo=_repo(steps={"review": "required"}),
        verifier=_verifier(GREEN),
    )
    assert result.success, result
    assert result.review.posted


@pytest.mark.asyncio
async def test_resume_cannot_skip_a_required_verify(
    tmp_path: Path, monkeypatch
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    result, _ = await _resume(
        tmp_path, repo=_repo(steps={"verify": "required"}),
        verifier=_verifier(SLOW),
    )
    assert not result.success
    assert result.outputs["pr_number"] == PR


@pytest.mark.asyncio
async def test_resume_with_verify_off_waits_for_no_ci(
    tmp_path: Path, monkeypatch
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    verifier = _verifier(RED)
    result, _ = await _resume(
        tmp_path, repo=_repo(steps={"verify": "off"}), verifier=verifier,
    )
    assert result.success, result
    verifier.verify.assert_not_awaited()
