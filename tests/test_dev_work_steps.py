"""The dev pipeline's work as an ordered list of steps (#226).

`run_dev_issue` is a frame (lock, claim, worktree, cleanup, claim
reconciliation) around a fixed list of work steps. These tests pin the
list's order, that the frame runs every step in that order, and that a
step which raises still reaches the frame's exception path with what the
earlier steps left behind. Behaviour of the steps themselves is covered
by the existing dev pipeline tests, which this change did not edit.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import ctrlrelay.pipelines.dev as dev
from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
from ctrlrelay.core.dispatcher import SessionResult
from ctrlrelay.core.state import StateDB
from tests._publish_fakes import fake_relay_publish


def _done_dispatcher(exit_code: int = 0) -> AsyncMock:
    dispatcher = AsyncMock()
    dispatcher.spawn_session.return_value = SessionResult(
        session_id="dev-123",
        exit_code=exit_code,
        stdout="",
        stderr="",
        state=CheckpointState(
            version="1",
            status=CheckpointStatus.DONE,
            session_id="dev-123",
            timestamp="2026-11-02T12:00:00Z",
            summary="done",
            outputs={},
        ),
    )
    return dispatcher


def _fakes(tmp_path: Path) -> tuple[AsyncMock, AsyncMock]:
    worktree = AsyncMock()
    worktree.push_run_branch.return_value = "c0ffee"
    worktree.create_worktree_with_new_branch.return_value = (
        tmp_path / "worktree",
        True,
    )
    worktree.branch_exists_locally.return_value = False
    worktree.branch_exists_on_remote.return_value = True
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()

    github = AsyncMock()
    fake_relay_publish(github)
    github.get_issue.return_value = {
        "number": 123,
        "title": "Fix bug",
        "body": "Bug description",
        "comments": [],
    }
    github.get_pr_checks.return_value = [
        {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
    ]
    github.get_pr_state.return_value = {
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }
    return worktree, github


async def _run(tmp_path: Path, worktree, github, state_db):
    return await dev.run_dev_issue(
        claims=None,
        repo="owner/repo",
        issue_number=123,
        branch_template="fix/issue-{n}",
        dispatcher=_done_dispatcher(),
        github=github,
        worktree=worktree,
        dashboard=None,
        state_db=state_db,
        transport=None,
        contexts_dir=tmp_path / "contexts",
    )


def test_the_order_is_the_documented_one() -> None:
    """Fixed order, decided on #226 (plan added first by #220, proof
    before review by #221): plan, code, publish, verify, proof, review.

    A later change may switch a step off per repository; reordering is
    not on the table, so a change to this tuple is a design change and
    should have to edit this test.
    """
    assert [s.name for s in dev.DEV_WORK_STEPS] == [
        "plan",
        "code",
        "publish",
        "verify",
        "proof",
        "review",
    ]
    assert [s.run for s in dev.DEV_WORK_STEPS] == [
        dev._step_plan,
        dev._step_code,
        dev._step_publish,
        dev._step_verify,
        dev._step_proof,
        dev._step_review,
    ]


@pytest.mark.asyncio
async def test_the_frame_runs_every_step_in_list_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frame runs what the list says, in the list's order.

    Each real step is wrapped to record its name and then do its real
    work, so the run still reaches DONE — which shows the recording is
    of a run that worked, not of steps that were never asked to.
    """
    ran: list[str] = []

    def _recording(step: dev.DevWorkStep) -> dev.DevWorkStep:
        async def _run_step(run: dev._DevRun) -> None:
            ran.append(step.name)
            await step.run(run)

        return dev.DevWorkStep(step.name, _run_step)

    monkeypatch.setattr(
        dev,
        "DEV_WORK_STEPS",
        tuple(_recording(s) for s in dev.DEV_WORK_STEPS),
    )
    worktree, github = _fakes(tmp_path)
    result = await _run(tmp_path, worktree, github, StateDB(tmp_path / "s.db"))

    assert ran == ["plan", "code", "publish", "verify", "proof", "review"]
    assert result.success, result
    assert result.outputs["pr_number"] == 42
    # The frame attached a review outcome because the run had a PR.
    assert result.review is not None


@pytest.mark.asyncio
async def test_a_step_that_raises_reaches_the_frames_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step raising is the frame's exception path, unchanged.

    The verify step is replaced by one that raises after code and
    publish ran for real. The frame must still: remove the worktree,
    release the repo lock, mark the session failed, and retract the
    claim naming the pull request the publish step opened (#253) and the
    exit code the code step recorded. The review step must not run.
    """
    ran: list[str] = []
    steps = {s.name: s for s in dev.DEV_WORK_STEPS}

    async def _code(run: dev._DevRun) -> None:
        await steps["code"].run(run)
        # The DONE checkpoint carries no exit code; give it one, so the
        # retraction below can only show 7 by reading it off the run.
        run.last_exit_code = 7

    async def _raises(run: dev._DevRun) -> None:
        ran.append("verify")
        raise RuntimeError("verify exploded")

    async def _review(run: dev._DevRun) -> None:
        ran.append("review")

    monkeypatch.setattr(
        dev,
        "DEV_WORK_STEPS",
        (
            dev.DevWorkStep("code", _code),
            steps["publish"],
            dev.DevWorkStep("verify", _raises),
            dev.DevWorkStep("review", _review),
        ),
    )
    worktree, github = _fakes(tmp_path)
    state_db = StateDB(tmp_path / "s.db")

    result = await _run(tmp_path, worktree, github, state_db)

    assert ran == ["verify"]
    assert not result.success
    assert result.error == "verify exploded"
    # Dispatched, so not offered as a pre-dispatch retry (#234).
    assert "failed_before_dispatch" not in result.outputs

    worktree.remove_worktree.assert_awaited()
    # The branch is on origin (the publish step pushed it), so it stays.
    worktree.delete_branch.assert_not_awaited()

    # The lock is free again: another session can take it.
    assert state_db.acquire_lock("owner/repo", "someone-else")

    row = state_db.execute(
        "SELECT status, summary FROM sessions"
    ).fetchone()
    assert tuple(row) == ("failed", "Error: verify exploded")

    bodies = [
        c.kwargs["body"]
        for c in github.comment_on_issue.await_args_list
        if dev.AGENT_OUTCOME_MARKER in c.kwargs.get("body", "")
    ]
    assert len(bodies) == 1, bodies
    assert "Pull request #42 was opened" in bodies[0]
    assert "`7`" in bodies[0]
    assert "verify exploded" in bodies[0]
