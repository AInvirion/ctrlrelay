"""A dev run that goes in circles is stopped as stuck (#224).

Driven through ``run_dev_issue``, the way the poller runs an issue, with
the agent, GitHub and CI faked at their boundaries.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
from ctrlrelay.core.dispatcher import SessionResult
from ctrlrelay.core.pr_verifier import VerificationResult
from ctrlrelay.core.state import StateDB
from ctrlrelay.pipelines import dev as dev_mod
from ctrlrelay.pipelines.dev import run_dev_issue
from tests._publish_fakes import fake_relay_publish

JOB = "https://github.com/owner/repo/actions/runs/777/job/888"
FAILING = [{"name": "test", "state": "FAILURE", "bucket": "fail", "link": JOB}]
CODE_LOG = "test\tpytest\tFAILED tests/test_a.py::test_one - assert 1 == 2\n"
INFRA_LOG = "##[error]The runner has received a shutdown signal.\n"
BUDGET_LOG = (
    "##[error]The job running on runner X has exceeded the maximum "
    "execution time of 360 minutes.\n"
)


def _done(sid: str = "dev-a") -> SessionResult:
    return SessionResult(
        session_id=sid, exit_code=0, stdout="", stderr="",
        state=CheckpointState(
            version="1", status=CheckpointStatus.DONE, session_id=sid,
            timestamp="2026-11-04T12:00:00Z", summary="PR #42",
            outputs={"pr_url": "https://github.com/o/r/pull/42", "pr_number": 42},
        ),
    )


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(dev_mod, "_RERUN_SETTLE_SECONDS", 0.0)


async def _run(tmp_path: Path, *, verifications: list, logs: list[str],
               spawns: list | None = None, setup=None):
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = spawns or (lambda *a, **k: _done())

    worktree = AsyncMock()
    worktree.push_run_branch.return_value = "c0ffee"
    worktree.create_worktree_with_new_branch.return_value = (
        tmp_path / "worktree", True,
    )
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()
    worktree.branch_exists_on_remote.return_value = True

    github = AsyncMock()
    fake_relay_publish(github)
    github.get_issue.return_value = {
        "number": 1, "title": "x", "body": "y", "comments": [],
    }
    log_iter = iter(logs)
    github.get_failed_job_log = AsyncMock(side_effect=lambda *a, **k: next(log_iter))
    github.rerun_failed_jobs = AsyncMock(return_value=None)
    if setup is not None:
        setup(github)

    verifier = AsyncMock()
    verifier.verify.side_effect = verifications

    state_db = StateDB(tmp_path / "state.db")
    result = await run_dev_issue(
        claims=None, repo="owner/repo", issue_number=1,
        branch_template="fix/issue-{n}", dispatcher=dispatcher, github=github,
        worktree=worktree, dashboard=None, state_db=state_db, transport=None,
        contexts_dir=tmp_path / "contexts", pr_verifier=verifier,
        max_fix_attempts=3,
    )
    row = dict(state_db.execute(
        "SELECT status, summary, stuck_reason FROM sessions"
    ).fetchone())
    failed = state_db.execute(
        "SELECT detail FROM events WHERE kind = 'failed'"
    ).fetchone()
    row["failed_event"] = json.loads(failed[0]) if failed else None
    state_db.close()
    return result, row, dispatcher, github


def _failed() -> VerificationResult:
    return VerificationResult(ready=False, reason="ci failed", failing_checks=FAILING)


@pytest.mark.asyncio
async def test_same_tests_failing_after_fix_rounds_stops_the_run_as_stuck(tmp_path):
    result, row, dispatcher, github = await _run(
        tmp_path,
        verifications=[_failed()] * 4 + [VerificationResult(ready=True)],
        logs=[CODE_LOG] * 4,
    )
    assert not result.success
    assert result.stuck_reason is not None
    assert "tests/test_a.py::test_one" in result.stuck_reason
    assert "after 2 fix round(s)" in result.stuck_reason
    # The first run and two fixes; the third fix is not spent.
    assert dispatcher.spawn_session.call_count == 3
    # A code failure never gets a blind re-run.
    github.rerun_failed_jobs.assert_not_awaited()
    assert row["status"] == "failed"
    assert row["stuck_reason"] == result.stuck_reason
    assert row["summary"].startswith("Stuck:")


@pytest.mark.asyncio
async def test_changing_test_failures_are_progress_not_stuck(tmp_path):
    logs = [CODE_LOG.replace("test_one", f"test_{i}") for i in range(4)]
    result, row, dispatcher, _ = await _run(
        tmp_path, verifications=[_failed()] * 4, logs=logs,
    )
    assert not result.success
    assert result.stuck_reason is None
    assert row["stuck_reason"] is None
    assert "stuck" not in row["failed_event"]
    # Every fix attempt was spent, as before #224.
    assert dispatcher.spawn_session.call_count == 4


@pytest.mark.asyncio
async def test_flaky_infra_gets_a_rerun_not_an_agent_round(tmp_path):
    result, row, dispatcher, github = await _run(
        tmp_path,
        verifications=[_failed(), VerificationResult(ready=True)],
        logs=[INFRA_LOG],
    )
    assert result.success
    github.rerun_failed_jobs.assert_awaited_once()
    assert github.rerun_failed_jobs.await_args.args[:2] == ("owner/repo", "777")
    assert dispatcher.spawn_session.call_count == 1


@pytest.mark.asyncio
async def test_infra_failing_again_after_the_rerun_stops_without_the_agent(tmp_path):
    result, row, dispatcher, github = await _run(
        tmp_path,
        verifications=[_failed(), _failed()],
        logs=[INFRA_LOG, INFRA_LOG],
    )
    assert not result.success
    assert "infrastructure" in result.summary
    assert result.stuck_reason is None
    github.rerun_failed_jobs.assert_awaited_once()
    assert dispatcher.spawn_session.call_count == 1
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_a_new_infra_flake_after_a_fix_round_gets_its_own_rerun(tmp_path):
    result, row, dispatcher, github = await _run(
        tmp_path,
        verifications=[_failed()] * 3 + [VerificationResult(ready=True)],
        # flake, re-run; real failure, fix round; new flake, re-run again
        logs=[INFRA_LOG, CODE_LOG, INFRA_LOG],
    )
    assert result.success, result.summary
    assert github.rerun_failed_jobs.await_count == 2
    assert dispatcher.spawn_session.call_count == 2


@pytest.mark.asyncio
async def test_out_of_budget_is_not_retried_at_all(tmp_path):
    result, row, dispatcher, github = await _run(
        tmp_path, verifications=[_failed()], logs=[BUDGET_LOG],
    )
    assert not result.success
    assert "out of budget" in result.summary
    github.rerun_failed_jobs.assert_not_awaited()
    assert dispatcher.spawn_session.call_count == 1


@pytest.mark.asyncio
async def test_an_agent_stopped_in_a_loop_is_recorded_as_stuck(tmp_path):
    stuck = SessionResult(
        session_id="dev-a", exit_code=-9, state=None,
        stderr="Session stopped: stuck: ran the same command 5 times in a row",
        stuck_reason="ran the same command 5 times in a row: `pytest`",
    )
    result, row, _, _ = await _run(
        tmp_path, verifications=[], logs=[], spawns=[stuck],
    )
    assert not result.success
    assert result.stuck_reason == (
        "the agent ran the same command 5 times in a row: `pytest`"
    )
    assert row["status"] == "failed"
    assert row["stuck_reason"] == result.stuck_reason
    assert row["summary"] == f"Stuck: {result.stuck_reason}"
    assert row["failed_event"]["stuck"] == result.stuck_reason


@pytest.mark.asyncio
async def test_the_chat_message_says_stuck():
    from ctrlrelay.cli import announce_issue_outcome
    from ctrlrelay.pipelines.base import PipelineResult

    transport = AsyncMock()
    sent = await announce_issue_outcome(
        transport, repo="owner/repo", issue_number=7, is_task=False,
        result=PipelineResult(
            success=False, session_id="s", summary="Stuck: the agent looped",
            stuck_reason="the agent looped",
        ),
    )
    assert sent == ["🔁 Stuck on #7, stopped: the agent looped"]


@pytest.mark.asyncio
async def test_an_outage_across_more_jobs_than_are_read_is_still_infra():
    from ctrlrelay.core.stuck import FailureKind, classify_failures

    checks = [
        {"name": f"matrix-{i}", "state": "FAILURE", "bucket": "fail",
         "link": JOB.replace("/job/888", f"/job/{700 + i}")}
        for i in range(5)
    ] + [{"name": "cla", "state": "FAILURE", "bucket": "fail",
          "link": "https://cla.example/check"}]
    github = AsyncMock()
    github.get_failed_job_log = AsyncMock(return_value=INFRA_LOG)
    _, job_logs = await dev_mod._failed_check_logs(github, "owner/repo", checks)
    # Three logs read; the jobs past the limit and the non-Actions check
    # were never looked at, so they do not vote.
    assert job_logs == [INFRA_LOG.rstrip("\n")] * 3
    assert classify_failures(job_logs)[0] is FailureKind.INFRA


@pytest.mark.asyncio
async def test_a_log_that_could_not_be_fetched_is_unknown_so_code():
    github = AsyncMock()
    github.get_failed_job_log = AsyncMock(side_effect=RuntimeError("503"))
    _, job_logs = await dev_mod._failed_check_logs(github, "owner/repo", FAILING)
    assert job_logs == [None]


# --- a run stopped after its PR exists keeps the PR, as a draft (#223) -------


def _over_budget(sid: str = "dev-a") -> SessionResult:
    from ctrlrelay.core.budget import budget_reason

    return SessionResult(
        session_id=sid, exit_code=-9, stdout="", stderr="", state=None,
        stuck_reason=budget_reason(1_100, 1_000),
    )


async def _budget_stop_in_fix_round(tmp_path, monkeypatch, *, draft_fails=False):
    reconcile = AsyncMock()
    monkeypatch.setattr(dev_mod, "reconcile_claim_on_failure", reconcile)
    spawns = iter([_done(), _over_budget()])

    def _set_draft(github):
        if draft_fails:
            github.set_pr_draft.side_effect = RuntimeError("drafts not allowed")

    result, row, _, github = await _run(
        tmp_path, verifications=[_failed()], logs=[CODE_LOG],
        spawns=lambda *a, **k: next(spawns), setup=_set_draft,
    )
    return result, row, github, reconcile


@pytest.mark.asyncio
async def test_budget_stop_in_a_fix_round_puts_the_pr_back_to_draft(
    tmp_path, monkeypatch
):
    result, row, github, reconcile = await _budget_stop_in_fix_round(
        tmp_path, monkeypatch,
    )
    assert result.stuck_reason.startswith("budget: ")
    assert result.parked_pr == 42
    github.set_pr_draft.assert_any_await("owner/repo", 42, True)
    assert result.parked_note == "PR #42 was kept as a draft with the work so far."
    assert result.parked_note in reconcile.await_args.kwargs["reason"]
    assert row["stuck_reason"] == result.stuck_reason


@pytest.mark.asyncio
async def test_a_pr_that_cannot_be_drafted_gets_a_do_not_merge_comment(
    tmp_path, monkeypatch
):
    result, _, github, _ = await _budget_stop_in_fix_round(
        tmp_path, monkeypatch, draft_fails=True,
    )
    assert result.parked_pr == 42
    body = github.comment_on_pr.await_args.args[2]
    assert "Do not merge" in body and "over budget" in body
    assert "do-not-merge comment" in result.parked_note


def test_a_parked_pr_gets_no_merge_watcher():
    from ctrlrelay.cli import pr_to_watch
    from ctrlrelay.pipelines.base import PipelineResult

    handed = PipelineResult(
        success=True, session_id="s", summary="ok", outputs={"pr_number": 42},
    )
    assert pr_to_watch(handed) == 42
    parked = PipelineResult(
        success=False, session_id="s", summary="Stopped", outputs={"pr_number": 42},
        stuck_reason="budget: x", parked_pr=42, parked_note="n",
    )
    assert pr_to_watch(parked) is None


@pytest.mark.asyncio
async def test_chat_names_the_parked_pr():
    from ctrlrelay.cli import announce_issue_outcome
    from ctrlrelay.pipelines.base import PipelineResult

    transport = AsyncMock()
    note = "PR #42 was kept as a draft with the work so far."
    sent = await announce_issue_outcome(
        transport, repo="o/r", issue_number=7, is_task=False,
        result=PipelineResult(
            success=False, session_id="s", summary="Stopped",
            stuck_reason="budget: used 1,100 tokens, hard limit 1,000",
            parked_pr=42, parked_note=note,
        ),
    )
    assert sent[0].endswith(note)
