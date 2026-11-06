"""Saving each step of a dev run, and continuing from it (#231).

A dev run saves each step it finishes (plan, code, publish, verify). When
its process dies, the poller settles the run at its next start, and the
next run for the issue takes over the saved steps instead of starting
over. ``ctrlrelay run dev --from-step`` starts a new run from a past step.

The first test is the one the ticket asks for: a real process is killed
with SIGKILL in the middle of a run (while it waits for CI), the poller's
start-up reaper settles it, and the next run continues without running
the agent again or opening a second pull request. Git is real throughout
(the relay's bare clone, worktrees, branch reuse); only GitHub, the agent
and the push are fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import ctrlrelay.pipelines.dev as dev
from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
from ctrlrelay.core.config import CodeReviewConfig
from ctrlrelay.core.dispatcher import SessionResult
from ctrlrelay.core.pr_verifier import VerificationResult
from ctrlrelay.core.state import StateDB
from tests._publish_fakes import fake_relay_publish

PR = 42
REPO = "owner/repo"
ISSUE = 7
BRANCH = "fix/issue-7"
REVIEW_OFF = CodeReviewConfig(method="off")
GREEN = VerificationResult(ready=True)
ROOT = Path(__file__).resolve().parent.parent

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_ENV, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _make_origin(tmp: Path) -> None:
    """An origin with `main`, and the relay's bare clone of it."""
    seed = tmp / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    (seed / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=seed)
    _git("commit", "-q", "-m", "init", cwd=seed)
    _git("clone", "-q", "--bare", str(seed), str(tmp / "origin.git"))
    (tmp / "repos").mkdir()
    _git("clone", "-q", "--bare", str(tmp / "origin.git"),
         str(tmp / "repos" / "owner-repo.git"))


def _manager(tmp: Path):
    """The real WorktreeManager, with only the clone refresh and the push
    replaced: the push 'lands' the worktree's head without a network."""
    from ctrlrelay.core.worktree import WorktreeManager

    m = WorktreeManager(
        worktrees_dir=tmp / "worktrees", bare_repos_dir=tmp / "repos",
    )

    async def ensure_bare_repo(repo):
        return tmp / "repos" / "owner-repo.git"

    async def push_run_branch(repo, branch, **kw):
        return _git("rev-parse", f"refs/heads/{branch}",
                    cwd=tmp / "repos" / "owner-repo.git")

    m.ensure_bare_repo = ensure_bare_repo  # type: ignore[method-assign]
    m.push_run_branch = AsyncMock(side_effect=push_run_branch)  # type: ignore[method-assign]
    return m


def _github(open_pr: int | None = None) -> AsyncMock:
    github = AsyncMock()
    fake_relay_publish(github, pr_number=PR)
    github.get_issue.return_value = {
        "number": ISSUE, "title": "Fix bug", "body": "b", "comments": [],
    }
    if open_pr is not None:
        github.list_prs = AsyncMock(return_value=[{
            "number": open_pr,
            "headRepositoryOwner": {"login": "owner"},
            "headRepository": {"name": "repo"},
            "baseRefName": "main",
        }])
    return github


def _done(session_id: str, outputs: dict | None = None) -> SessionResult:
    return SessionResult(
        session_id=session_id, exit_code=0, stdout="", stderr="",
        state=CheckpointState(
            version="1", status=CheckpointStatus.DONE, session_id=session_id,
            timestamp="2026-11-05T12:00:00Z", summary="did it",
            outputs=outputs or {},
        ),
    )


class _Agent:
    """Commits one file per session and reports DONE. Records prompts."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.dispatcher = AsyncMock()
        self.dispatcher.spawn_session.side_effect = self._spawn

    async def _spawn(self, **kw) -> SessionResult:
        self.prompts.append(kw["prompt"])
        wd = Path(kw["working_dir"])
        n = len(self.prompts)
        (wd / f"change{n}.txt").write_text(f"{n}\n")
        _git("add", f"change{n}.txt", cwd=wd)
        _git("commit", "-q", "-m", f"change {n}", cwd=wd)
        return _done(kw["session_id"], {"pr_title": "Fix bug"})


async def _run_dev(tmp: Path, agent, github, verifier, **kw):
    return await dev.run_dev_issue(
        claims=None, repo=REPO, issue_number=ISSUE,
        branch_template="fix/issue-{n}", dispatcher=agent.dispatcher,
        github=github, worktree=kw.pop("worktree", None) or _manager(tmp),
        dashboard=None, state_db=kw.pop("state_db", None) or StateDB(tmp / "s.db"),
        transport=None, contexts_dir=tmp / "contexts", pr_verifier=verifier,
        code_review=REVIEW_OFF, **kw,
    )


# --- the child process that gets killed ---------------------------------------


RED = VerificationResult(
    ready=False, reason="1 failing check",
    failing_checks=[{"name": "ci", "state": "FAILURE"}],
)


def _child(tmp_s: str, red_first: bool = False) -> None:
    """Runs a dev run that stops in the verify step's CI wait, after
    writing `ready`, until it is killed. With ``red_first`` CI fails once
    first, so the agent pushes a fix commit before the wait it dies in."""
    tmp = Path(tmp_s)

    async def main() -> None:
        verifier = AsyncMock()
        calls = {"n": 0}

        async def wait_for_ci(*a, **k):
            calls["n"] += 1
            if red_first and calls["n"] == 1:
                return RED
            (tmp / "ready").write_text("1")
            await asyncio.sleep(3600)

        verifier.verify.side_effect = wait_for_ci
        with __import__("unittest.mock").mock.patch.object(
            dev, "_failed_check_logs", AsyncMock(return_value=("", [])),
        ):
            await _run_dev(tmp, _Agent(), _github(), verifier)

    asyncio.run(main())


def _kill_mid_verify(tmp: Path, red_first: bool = False) -> int:
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import tests.test_run_resume as t; "
         f"t._child({str(tmp)!r}, {red_first!r})"],
        cwd=ROOT, env=_ENV,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 60
    while not (tmp / "ready").exists():
        if proc.poll() is not None:
            raise AssertionError(
                f"child exited {proc.returncode} before CI wait: "
                + proc.stderr.read().decode()[-2000:]
            )
        if time.monotonic() > deadline:
            proc.kill()
            raise AssertionError("child never reached the CI wait")
        time.sleep(0.05)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    return proc.pid


@pytest.mark.asyncio
async def test_a_killed_run_continues_after_its_last_finished_step(tmp_path):
    from ctrlrelay.core.orphans import reap_orphaned_sessions

    _make_origin(tmp_path)
    pid = _kill_mid_verify(tmp_path)
    bare = tmp_path / "repos" / "owner-repo.git"
    head_at_kill = _git("rev-parse", f"refs/heads/{BRANCH}", cwd=bare)

    db = StateDB(tmp_path / "s.db")
    [dead] = db.list_running_sessions()
    assert dead["owner_pid"] == pid
    assert [r["step"] for r in db.run_steps_for(dead["id"])] == [
        "plan", "code", "publish",
    ]

    # The poller's start: the run is settled, and handed back even though
    # its pull request is open, because it is the PR the run saved.
    poller = MagicMock()
    github = _github(open_pr=PR)
    manager = _manager(tmp_path)
    reaped = await reap_orphaned_sessions(
        db, poller=poller, github=github, worktree=manager,
    )
    assert [r["session_id"] for r in reaped] == [dead["id"]]
    poller.unmark_seen.assert_called_once_with(REPO, ISSUE)

    agent = _Agent()
    result = await _run_dev(
        tmp_path, agent, github, AsyncMock(verify=AsyncMock(return_value=GREEN)),
        worktree=manager, state_db=db,
    )

    assert result.success, result
    assert result.outputs.get("pr_number") == PR
    # Not run again: no agent session, no push, no second pull request.
    assert agent.prompts == []
    manager.push_run_branch.assert_not_awaited()
    github.create_pr.assert_not_awaited()
    assert _git("rev-parse", f"refs/heads/{BRANCH}", cwd=bare) == head_at_kill
    comments = [c.kwargs["body"] for c in github.comment_on_issue.await_args_list]
    assert any(
        f"Run `{dead['id']}` stopped" in b and "after the `publish` step" in b
        for b in comments
    ), comments
    # The new run holds the whole chain, so it too could be continued.
    assert [r["step"] for r in db.run_steps_for(result.session_id)] == [
        "plan", "code", "publish", "verify",
    ]
    assert db.get_session_row(dead["id"])["resumed_by"] == result.session_id


@pytest.mark.asyncio
async def test_a_run_interrupted_after_code_publishes_without_the_agent(
    tmp_path,
) -> None:
    """Killed (here: settled as if killed) after the code step: the next
    run publishes the saved commit; the agent is not asked again."""
    _make_origin(tmp_path)
    db = StateDB(tmp_path / "s.db")
    github = _github()

    async def die(*a, **k):
        raise KeyboardInterrupt  # stands in for the process going away

    first = _Agent()
    manager = _manager(tmp_path)
    manager.push_run_branch.side_effect = die
    with pytest.raises(KeyboardInterrupt):
        await _run_dev(
            tmp_path, first, github, AsyncMock(), worktree=manager, state_db=db,
        )
    [row] = db.list_running_sessions()
    db.execute("UPDATE sessions SET owner_pid = 999999999 WHERE id = ?",
               (row["id"],))
    db.commit()
    db.fail_orphaned_session(row["id"], "Run stopped")
    manager.detach_orphaned_worktree(REPO, row["worktree_path"])

    second = _Agent()
    result = await _run_dev(
        tmp_path, second, github,
        AsyncMock(verify=AsyncMock(return_value=GREEN)), state_db=db,
    )
    assert result.success, result
    assert second.prompts == []
    assert len(first.prompts) == 1
    github.create_pr.assert_awaited_once()


# --- what is saved -------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_finished_steps_are_saved(tmp_path) -> None:
    """A failed code step is not a finished step: only the plan is saved,
    and nothing after the failure."""
    _make_origin(tmp_path)
    db = StateDB(tmp_path / "s.db")
    agent = AsyncMock()
    agent.spawn_session.return_value = SessionResult(
        session_id="x", exit_code=1, stdout="", stderr="boom",
        state=CheckpointState(
            version="1", status=CheckpointStatus.FAILED, session_id="x",
            timestamp="2026-11-05T12:00:00Z", error="could not",
        ),
    )
    holder = MagicMock(dispatcher=agent)
    result = await _run_dev(tmp_path, holder, _github(), AsyncMock(), state_db=db)
    assert not result.success
    sid = db.latest_dev_session(REPO, ISSUE)["id"]
    assert [r["step"] for r in db.run_steps_for(sid)] == ["plan"]


@pytest.mark.asyncio
async def test_a_step_that_cannot_be_saved_does_not_fail_the_run(
    tmp_path, monkeypatch,
) -> None:
    _make_origin(tmp_path)
    db = StateDB(tmp_path / "s.db")

    def broken(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(db, "record_run_step", broken)
    result = await _run_dev(
        tmp_path, _Agent(), _github(),
        AsyncMock(verify=AsyncMock(return_value=GREEN)), state_db=db,
    )
    assert result.success, result


# --- when a run is not continued ----------------------------------------------


def _saved(db: StateDB, sid: str, *, interrupted=True, steps=("plan", "code"),
           head="a" * 40, started=100) -> None:
    db.execute(
        "INSERT INTO sessions (id, pipeline, repo, status, started_at, "
        "issue_number, interrupted) VALUES (?, 'dev', ?, 'failed', ?, ?, ?)",
        (sid, REPO, started, ISSUE, 1 if interrupted else None),
    )
    db.commit()
    for seq, step in enumerate(steps):
        db.record_run_step(
            session_id=sid, repo=REPO, issue_number=ISSUE, seq=seq, step=step,
            head_sha=head, data=json.dumps({"plan_text": "p"}),
        )


def test_a_run_that_failed_on_its_own_is_not_continued(tmp_path) -> None:
    db = StateDB(tmp_path / "s.db")
    _saved(db, "dev-a", interrupted=False)
    assert dev.find_resume_point(db, REPO, ISSUE) is None


def test_an_interrupted_run_is_continued_once(tmp_path) -> None:
    db = StateDB(tmp_path / "s.db")
    _saved(db, "dev-a")
    point = dev.find_resume_point(db, REPO, ISSUE)
    assert point is not None and point.steps == ("plan", "code")
    assert db.mark_resumed("dev-a", "dev-b")
    assert not db.mark_resumed("dev-a", "dev-c")
    assert dev.find_resume_point(db, REPO, ISSUE) is None


def test_only_the_newest_run_of_the_issue_counts(tmp_path) -> None:
    db = StateDB(tmp_path / "s.db")
    _saved(db, "dev-old", started=100)
    _saved(db, "dev-new", interrupted=False, started=200)
    assert dev.find_resume_point(db, REPO, ISSUE) is None


def test_a_step_without_a_known_head_is_not_trusted(tmp_path) -> None:
    db = StateDB(tmp_path / "s.db")
    _saved(db, "dev-a", head=None)
    assert dev.find_resume_point(db, REPO, ISSUE) is None


@pytest.mark.asyncio
async def test_a_moved_branch_starts_over_when_there_is_no_pr(tmp_path) -> None:
    """The saved code step's head is not where the branch is: the run does
    not build on it, and the agent runs as for a new run."""
    _make_origin(tmp_path)
    db = StateDB(tmp_path / "s.db")
    _saved(db, "dev-a", head="d" * 40)
    agent = _Agent()
    result = await _run_dev(
        tmp_path, agent, _github(),
        AsyncMock(verify=AsyncMock(return_value=GREEN)), state_db=db,
    )
    assert result.success, result
    assert len(agent.prompts) == 1


@pytest.mark.asyncio
async def test_a_moved_branch_with_a_pr_stops(tmp_path) -> None:
    _make_origin(tmp_path)
    db = StateDB(tmp_path / "s.db")
    db.execute(
        "INSERT INTO sessions (id, pipeline, repo, status, started_at, "
        "issue_number, interrupted) VALUES ('dev-a', 'dev', ?, 'failed', 1, ?, 1)",
        (REPO, ISSUE),
    )
    db.record_run_step(
        session_id="dev-a", repo=REPO, issue_number=ISSUE, seq=2,
        step="publish", head_sha="d" * 40,
        data=json.dumps({"publisher": {"pr_number": PR}}),
    )
    bare = tmp_path / "repos" / "owner-repo.git"
    _git("branch", BRANCH, "main", cwd=bare)
    agent = _Agent()
    result = await _run_dev(
        tmp_path, agent, _github(open_pr=PR), AsyncMock(), state_db=db,
    )
    assert not result.success
    assert "were not taken over" in (result.error or "")
    assert agent.prompts == []


@pytest.mark.asyncio
async def test_another_open_pr_on_the_branch_still_refuses(tmp_path) -> None:
    _make_origin(tmp_path)
    bare = tmp_path / "repos" / "owner-repo.git"
    _git("branch", BRANCH, "main", cwd=bare)
    from ctrlrelay.core.worktree import WorktreeError

    with pytest.raises(WorktreeError, match="open PR #43"):
        await _manager(tmp_path).create_worktree_with_new_branch(
            REPO, "s1", BRANCH, github=_github(open_pr=43), allow_pr=PR,
        )


# --- --from-step ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_from_step_code_keeps_the_plan_and_rewinds_the_branch(
    tmp_path,
) -> None:
    """Retry from the code step: the saved plan is reused (no plan
    session), the branch goes back to before the first run's code, and
    that code is kept under a ref."""
    from ctrlrelay.core.config import DevStepsConfig

    _make_origin(tmp_path)
    db = StateDB(tmp_path / "s.db")
    bare = tmp_path / "repos" / "owner-repo.git"
    plan = "## Change\nx\n\n## Acceptance criteria\n1. it works\n"

    class Planner(_Agent):
        async def _spawn(self, **kw):
            if "You are planning issue" in kw["prompt"]:
                self.prompts.append(kw["prompt"])
                return _done(kw["session_id"], {"plan": plan})
            return await super()._spawn(**kw)

    first = Planner()
    r1 = await _run_dev(
        tmp_path, first, _github(),
        AsyncMock(verify=AsyncMock(return_value=GREEN)), state_db=db,
        steps=DevStepsConfig(plan="on"),
    )
    assert r1.success, r1
    first_code_head = _git("rev-parse", f"refs/heads/{BRANCH}", cwd=bare)
    main = _git("rev-parse", "main", cwd=bare)

    second = Planner()
    r2 = await _run_dev(
        tmp_path, second, _github(),
        AsyncMock(verify=AsyncMock(return_value=GREEN)), state_db=db,
        steps=DevStepsConfig(plan="on"), from_step="code",
    )
    assert r2.success, r2
    assert all("You are planning issue" not in p for p in second.prompts)
    assert len(second.prompts) == 1 and plan.strip() in second.prompts[0]
    # New code sits on main, not on top of the first run's code.
    assert _git("rev-parse", f"refs/heads/{BRANCH}~1", cwd=bare) == main
    assert _git(
        "rev-parse", f"refs/ctrlrelay/before-retry/{r2.session_id}", cwd=bare,
    ) == first_code_head


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["verify", "proof", "review", "nope"])
async def test_from_step_after_the_pr_is_refused(tmp_path, step) -> None:
    _make_origin(tmp_path)
    agent = _Agent()
    result = await _run_dev(
        tmp_path, agent, _github(), AsyncMock(), from_step=step,
    )
    assert not result.success
    assert "--from-step takes one of plan, code, publish" in result.error
    assert agent.prompts == []


@pytest.mark.asyncio
async def test_from_step_with_nothing_saved_is_refused(tmp_path) -> None:
    _make_origin(tmp_path)
    result = await _run_dev(
        tmp_path, _Agent(), _github(), AsyncMock(), from_step="code",
    )
    assert not result.success
    assert "saved any steps" in result.error


@pytest.mark.asyncio
async def test_from_step_does_not_rewind_a_branch_behind_an_open_pr(
    tmp_path,
) -> None:
    _make_origin(tmp_path)
    db = StateDB(tmp_path / "s.db")
    bare = tmp_path / "repos" / "owner-repo.git"
    main = _git("rev-parse", "main", cwd=bare)
    _saved(db, "dev-a", interrupted=False, head=main)
    work = tmp_path / "w"
    _git("worktree", "add", "-q", "-b", BRANCH, str(work), "main", cwd=bare)
    (work / "z.txt").write_text("z")
    _git("add", "z.txt", cwd=work)
    _git("commit", "-q", "-m", "z", cwd=work)
    _git("worktree", "remove", "--force", str(work), cwd=bare)
    before = _git("rev-parse", f"refs/heads/{BRANCH}", cwd=bare)

    result = await _run_dev(
        tmp_path, _Agent(), _github(open_pr=PR), AsyncMock(), state_db=db,
        from_step="code",
    )
    assert not result.success
    assert _git("rev-parse", f"refs/heads/{BRANCH}", cwd=bare) == before


@pytest.mark.asyncio
async def test_a_run_killed_after_a_ci_fix_continues_from_its_own_push(
    tmp_path,
) -> None:
    """The fix commit moved the branch past the saved publish step. It is
    the dead run's own last push, so the run is continued, and the next
    push would be leased against it."""
    from ctrlrelay.core.orphans import reap_orphaned_sessions

    _make_origin(tmp_path)
    _kill_mid_verify(tmp_path, red_first=True)
    bare = tmp_path / "repos" / "owner-repo.git"
    db = StateDB(tmp_path / "s.db")
    [dead] = db.list_running_sessions()
    fixed = _git("rev-parse", f"refs/heads/{BRANCH}", cwd=bare)
    saved_head = db.run_steps_for(dead["id"])[-1]["head_sha"]
    assert fixed != saved_head  # the fix really moved the branch
    assert db.last_relay_push(
        repo=REPO, branch=BRANCH, session_id=dead["id"],
    ) == fixed

    github = _github(open_pr=PR)
    manager = _manager(tmp_path)
    await reap_orphaned_sessions(
        db, poller=MagicMock(), github=github, worktree=manager,
    )
    agent = _Agent()
    seen: list = []
    real_restore = dev._restore_steps

    def spy(run, point):
        real_restore(run, point)
        seen.append(run.publisher.last_pushed_sha)

    with __import__("unittest.mock").mock.patch.object(
        dev, "_restore_steps", spy,
    ):
        result = await _run_dev(
            tmp_path, agent, github,
            AsyncMock(verify=AsyncMock(return_value=GREEN)),
            worktree=manager, state_db=db,
        )
    assert result.success, result
    assert agent.prompts == []
    github.create_pr.assert_not_awaited()
    assert seen == [fixed]


@pytest.mark.asyncio
async def test_a_resumed_run_that_fails_before_dispatch_leaves_the_resume(
    tmp_path,
) -> None:
    """A transient failure before the run starts must not use up the
    resume: the retry still continues the dead run, and the failure
    comment does not claim there is no pull request."""
    _make_origin(tmp_path)
    db = StateDB(tmp_path / "s.db")
    _saved(db, "dev-a", steps=("plan", "code", "publish"))
    db.execute(
        "UPDATE run_steps SET data = ? WHERE step = 'publish'",
        (json.dumps({"publisher": {"pr_number": PR}}),),
    )
    db.commit()
    github = _github(open_pr=PR)
    manager = _manager(tmp_path)

    async def clone_fails(repo):
        raise RuntimeError("git fetch: timeout")

    manager.ensure_bare_repo = clone_fails  # after the claim, before the run
    result = await _run_dev(
        tmp_path, _Agent(), github, AsyncMock(), state_db=db, worktree=manager,
    )
    assert not result.success, result
    assert result.outputs.get("failed_before_dispatch"), result
    assert db.get_session_row("dev-a")["resumed_by"] is None
    assert dev.find_resume_point(db, REPO, ISSUE) is not None
    bodies = [c.kwargs["body"] for c in github.comment_on_issue.await_args_list]
    assert bodies and all("no pull request was opened" not in b for b in bodies)
    assert any(f"#{PR}" in b for b in bodies), bodies


@pytest.mark.asyncio
async def test_a_fix_with_no_session_to_resume_carries_the_contract(
    tmp_path,
) -> None:
    from ctrlrelay.pipelines.base import PipelineContext

    db = StateDB(tmp_path / "s.db")
    db.execute(
        "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
        "VALUES ('dev-b', 'dev', ?, 'running', 1)", (REPO,),
    )
    db.commit()
    agent = _Agent()
    pipeline = dev.DevPipeline(
        dispatcher=agent.dispatcher, github=AsyncMock(), worktree=AsyncMock(),
        dashboard=None, state_db=db, transport=None,
    )
    work = tmp_path / "w"
    work.mkdir()
    _git("init", "-q", cwd=work)
    ctx = PipelineContext(
        session_id="dev-b", repo=REPO, worktree_path=work,
        context_path=tmp_path / "c", state_file=work / "state.json",
        issue_number=ISSUE, extra={"issue_title": "Fix bug"},
    )
    await pipeline.request_fix(ctx, "CI failed: fix it")
    assert "Fix bug" in agent.prompts[0]
    assert "CI failed: fix it" in agent.prompts[0]
    assert "dev-b" in agent.prompts[0] and "state.json" in agent.prompts[0]

    db.set_agent_session_id("dev-b", "uuid-1")
    await pipeline.request_fix(ctx, "CI failed again")
    assert agent.prompts[1] == "CI failed again"
