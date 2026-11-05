"""A question is saved before the run waits for its answer (#324).

When the agent ends BLOCKED, the dev pipeline posts the question and waits
for the answer inside the poller process. The ``pending_resumes`` row used to
be written only after that wait ended, so a poller stopped during the wait
lost the question and left the session row ``running``. These tests stop the
run in the middle of the wait, as a restart does, and look at what is left.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
from ctrlrelay.core.dispatcher import SessionResult
from ctrlrelay.core.state import StateDB
from ctrlrelay.pipelines.dev import resume_dev_from_pending, run_dev_issue
from tests._publish_fakes import fake_relay_publish

REPO = "owner/repo"
QUESTION = "pin or bump typescript?"
AGENT_UUID = "11111111-2222-3333-4444-555555555555"


def _blocked(session_id: str) -> SessionResult:
    return SessionResult(
        session_id=session_id, exit_code=0, stdout="", stderr="",
        agent_session_id=AGENT_UUID,
        state=CheckpointState(
            version="1", status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
            session_id=session_id, timestamp="2026-11-04T22:22:19Z",
            question=QUESTION,
        ),
    )


def _resumed(session_id: str) -> SessionResult:
    """What the resumed agent returns. FAILED rather than DONE, so the run
    ends there without publishing or waiting on CI; these tests are about
    how the session was resumed, not how it finished."""
    return SessionResult(
        session_id=session_id, exit_code=0, stdout="", stderr="",
        agent_session_id=AGENT_UUID,
        state=CheckpointState(
            version="1", status=CheckpointStatus.FAILED,
            session_id=session_id, timestamp="2026-11-04T23:00:00Z",
            error="stopped here by the test",
        ),
    )


def _worktree(path: Path) -> AsyncMock:
    wt = AsyncMock()
    wt.push_run_branch.return_value = "c0ffee"
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text("gitdir: elsewhere\n")
    wt.create_worktree_with_new_branch.return_value = (path, True)
    wt.symlink_context = MagicMock()
    wt.remove_context_symlink = MagicMock()
    return wt


def _github() -> AsyncMock:
    gh = AsyncMock()
    fake_relay_publish(gh)
    gh.get_issue.return_value = {
        "number": 13, "title": "x", "body": "y", "comments": [],
    }
    return gh


class _HangingTransport:
    """Posts the question and never gets an answer: the wait that a poller
    restart cuts short."""

    def __init__(self) -> None:
        self.asked = asyncio.Event()

    async def ask(self, question: str, **kw: Any) -> str:
        self.asked.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(self, *a: Any, **kw: Any) -> None:
        return None

    async def close(self) -> None:
        return None


async def _run_until_cut(tmp_path: Path, state_db: StateDB) -> str:
    """Start a dev run that blocks, wait until its question is posted,
    then cancel the run as a stopping poller does. Returns the session id."""
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = (
        lambda **kw: _blocked(kw["session_id"])
    )
    transport = _HangingTransport()
    task = asyncio.create_task(run_dev_issue(
        claims=None, repo=REPO, issue_number=13,
        branch_template="fix/issue-{n}", dispatcher=dispatcher,
        github=_github(), worktree=_worktree(tmp_path / "wt"),
        dashboard=None, state_db=state_db, transport=transport,
        contexts_dir=tmp_path / "contexts",
    ))
    await asyncio.wait_for(transport.asked.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = state_db.execute(
        "SELECT id FROM sessions WHERE issue_number = 13"
    ).fetchall()
    assert len(rows) == 1
    return rows[0]["id"]


@pytest.mark.asyncio
async def test_a_run_cut_off_while_waiting_leaves_its_question_saved(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    sid = await _run_until_cut(tmp_path, state_db)

    # "A new poller": a fresh connection to the same database.
    state_db.close()
    again = StateDB(tmp_path / "state.db")
    row = again.get_pending_resume(sid)
    assert row is not None
    assert row["question"] == QUESTION
    assert row["answered_at"] is None and row["resumed_at"] is None
    status = again.execute(
        "SELECT status FROM sessions WHERE id = ?", (sid,)
    ).fetchone()["status"]
    assert status == "blocked"
    assert [r["session_id"] for r in again.list_unanswered_pending_resumes()] \
        == [sid]
    again.close()


@pytest.mark.asyncio
async def test_the_answer_after_a_restart_resumes_the_same_agent_session(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    sid = await _run_until_cut(tmp_path, state_db)
    state_db.close()

    again = StateDB(tmp_path / "state.db")
    # The bridge routes the late reply to the saved row; the sweeper
    # finds it and resumes.
    assert again.answer_pending_resume(sid, "pin it")
    (todo,) = again.list_pending_resumes_to_execute()
    assert todo["session_id"] == sid

    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = lambda **kw: _resumed(kw["session_id"])
    await resume_dev_from_pending(
        session_id=sid, repo=REPO, answer=todo["answer"],
        branch_template="fix/issue-{n}", dispatcher=dispatcher,
        github=_github(), worktree=_worktree(tmp_path / "wt"),
        dashboard=None, state_db=again, transport=None,
        contexts_dir=tmp_path / "contexts",
    )

    call = dispatcher.spawn_session.await_args_list[0].kwargs
    assert call["resume_session_id"] == AGENT_UUID
    assert call["session_id"] == sid
    assert "pin it" in call["prompt"]
    again.close()


def test_a_dead_blocked_runs_lock_is_dropped_and_no_live_ones_are(
    tmp_path: Path,
) -> None:
    """A lock left by a run stopped mid-wait would wedge the repo. Only a
    blocked run whose process is gone loses its lock: not this process's
    own runs, not a live process's (a run started by hand in another
    terminal), not a row that names no process, not a running session."""
    from ctrlrelay.core.claims import PROCESS_RUN
    from ctrlrelay.core.orphans import release_dead_blocked_locks

    db = StateDB(tmp_path / "state.db")
    rows = [
        ("s-dead", "o/a", "blocked", 101, "otherrun"),
        ("s-mine", "o/b", "blocked", 102, PROCESS_RUN),
        ("s-alive", "o/c", "blocked", 103, "otherrun"),
        ("s-legacy", "o/d", "blocked", None, None),
        ("s-running", "o/e", "running", 101, "otherrun"),
    ]
    for sid, repo, status, pid, run in rows:
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at, "
            "owner_pid, owner_run) VALUES (?, 'dev', ?, ?, 1, ?, ?)",
            (sid, repo, status, pid, run),
        )
        db.commit()
        assert db.acquire_lock(repo, sid)

    released = release_dead_blocked_locks(
        db, is_alive=lambda pid: pid == 103,
    )

    assert released == ["s-dead"]
    left = {r["session_id"] for r in db.list_locks()}
    assert left == {"s-mine", "s-alive", "s-legacy", "s-running"}
    # Each step guards on the status on its own: the lister finds only
    # blocked rows, and the drop refuses a session that is not blocked
    # (one resumed between the two keeps its lock).
    assert {r["id"] for r in db.list_blocked_sessions_holding_locks()} \
        == {"s-mine", "s-alive", "s-legacy"}
    assert db.release_blocked_session_lock("s-running") is False
    assert "s-running" in {r["session_id"] for r in db.list_locks()}
    db.close()


@pytest.mark.asyncio
async def test_an_answer_in_process_closes_the_saved_row(
    tmp_path: Path,
) -> None:
    """The fast path still works: an answer during the wait resumes in
    process, and the saved row is closed so the sweeper never resumes the
    session a second time."""
    state_db = StateDB(tmp_path / "state.db")
    dispatcher = AsyncMock()
    calls: list[dict[str, Any]] = []

    async def spawn(**kw: Any) -> SessionResult:
        calls.append(kw)
        return _blocked(kw["session_id"]) if len(calls) == 1 \
            else _resumed(kw["session_id"])

    dispatcher.spawn_session.side_effect = spawn
    transport = AsyncMock()
    seen: list[dict[str, Any] | None] = []

    async def ask(question: str, *, session_id: str, **kw: Any) -> str:
        seen.append(state_db.get_pending_resume(session_id))
        return "pin it"

    transport.ask.side_effect = ask

    result = await run_dev_issue(
        claims=None, repo=REPO, issue_number=13,
        branch_template="fix/issue-{n}", dispatcher=dispatcher,
        github=_github(), worktree=_worktree(tmp_path / "wt"),
        dashboard=None, state_db=state_db, transport=transport,
        contexts_dir=tmp_path / "contexts",
    )

    assert not result.blocked
    assert seen[0] is not None and seen[0]["question"] == QUESTION
    row = state_db.get_pending_resume(result.session_id)
    assert row["answer"] == "pin it"
    assert row["resumed_at"] is not None
    assert state_db.list_pending_resumes_to_execute() == []
    assert state_db.list_unanswered_pending_resumes() == []
    assert calls[1]["resume_session_id"] == AGENT_UUID
    state_db.close()


@pytest.mark.asyncio
async def test_an_answer_attached_during_the_wait_is_not_wiped_when_it_ends(
    tmp_path: Path,
) -> None:
    """The bridge may attach a reply to the saved row while the wait is
    still open, and the wait then times out. Writing the row again at the
    end used to clear that answer."""
    state_db = StateDB(tmp_path / "state.db")
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = (
        lambda **kw: _blocked(kw["session_id"])
    )
    transport = AsyncMock()

    async def ask(question: str, *, session_id: str, **kw: Any) -> str:
        assert state_db.answer_pending_resume(session_id, "pin it")
        raise TimeoutError("no answer on the socket")

    transport.ask.side_effect = ask

    result = await run_dev_issue(
        claims=None, repo=REPO, issue_number=13,
        branch_template="fix/issue-{n}", dispatcher=dispatcher,
        github=_github(), worktree=_worktree(tmp_path / "wt"),
        dashboard=None, state_db=state_db, transport=transport,
        contexts_dir=tmp_path / "contexts",
    )

    assert result.blocked
    (todo,) = state_db.list_pending_resumes_to_execute()
    assert todo["session_id"] == result.session_id
    assert todo["answer"] == "pin it"
    state_db.close()


@pytest.mark.asyncio
async def test_a_question_answered_first_through_its_saved_row_is_not_resumed_twice(
    tmp_path: Path,
) -> None:
    """The bridge routed one reply to the saved question while the live wait
    was still open, and then the live wait got a different reply. The first
    answer stands: this run must not resume with the second, and the
    sweeper resumes once, with the first."""
    state_db = StateDB(tmp_path / "state.db")
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = (
        lambda **kw: _blocked(kw["session_id"])
    )
    transport = AsyncMock()

    async def ask(question: str, *, session_id: str, **kw: Any) -> str:
        assert state_db.answer_pending_resume(session_id, "pin it")
        return "bump it"

    transport.ask.side_effect = ask

    result = await run_dev_issue(
        claims=None, repo=REPO, issue_number=13,
        branch_template="fix/issue-{n}", dispatcher=dispatcher,
        github=_github(), worktree=_worktree(tmp_path / "wt"),
        dashboard=None, state_db=state_db, transport=transport,
        contexts_dir=tmp_path / "contexts",
    )

    assert result.blocked
    assert dispatcher.spawn_session.await_count == 1
    (todo,) = state_db.list_pending_resumes_to_execute()
    assert todo["answer"] == "pin it"
    assert state_db.list_locks() == []
    state_db.close()
