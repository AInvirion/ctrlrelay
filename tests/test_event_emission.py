"""#209: the daemon writes status-page events at the points it already
reaches, and a failed event write never changes the work it describes.

Each test drives the real function (pipeline run, watcher, bridge, CLI
entry point) with the fakes the existing tests use, and reads the rows
back through ``StateDB.list_events``.
"""

from __future__ import annotations

import ast
import asyncio
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from ctrlrelay.core.state import StateDB
from tests._publish_fakes import fake_relay_publish
from tests.conftest import FakeChatHandler

DAY = 86400


def _in_order(db: StateDB) -> list[dict]:
    """Events oldest first (``list_events`` is newest first)."""
    return list(reversed(db.list_events(limit=1000)))


def _kinds(db: StateDB) -> list[str]:
    return [e["kind"] for e in _in_order(db)]


def _by_kind(db: StateDB, kind: str) -> list[dict]:
    return [e for e in _in_order(db) if e["kind"] == kind]


# --- dev ----------------------------------------------------------------


def _done_state(pr_number: int = 7):
    """A spawn that ends DONE with PR ``pr_number``, under the session id
    it was spawned with, as the real dispatcher does."""
    async def spawn(**kwargs):
        return _done_result(kwargs["session_id"], pr_number)

    return spawn


def _done_result(session_id: str, pr_number: int):
    from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
    from ctrlrelay.core.dispatcher import SessionResult

    return SessionResult(
        session_id=session_id,
        exit_code=0,
        stdout="",
        stderr="",
        agent_session_id="b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10",
        state=CheckpointState(
            version="1",
            status=CheckpointStatus.DONE,
            session_id=session_id,
            timestamp="2026-10-30T12:00:00Z",
            summary=f"PR #{pr_number} opened",
            outputs={
                "pr_url": f"https://github.com/o/r/pull/{pr_number}",
                "pr_number": pr_number,
            },
        ),
    )


def _blocked_state(question: str):
    async def spawn(**kwargs):
        return _blocked_result(kwargs["session_id"], question)

    return spawn


def _blocked_result(session_id: str, question: str):
    from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
    from ctrlrelay.core.dispatcher import SessionResult

    return SessionResult(
        session_id=session_id,
        exit_code=0,
        stdout="",
        stderr="",
        agent_session_id="b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10",
        state=CheckpointState(
            version="1",
            status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
            session_id=session_id,
            timestamp="2026-10-30T12:00:00Z",
            question=question,
        ),
    )


def _reviewed(monkeypatch) -> None:
    """A reviewer that read the diff. The review internals have their own
    tests; here only the phase around it matters."""
    from ctrlrelay.core.code_review import CodeReviewOutcome, ReviewStatus
    from ctrlrelay.pipelines import dev as dev_mod

    async def fake_review(**_kw):
        return CodeReviewOutcome(
            status=ReviewStatus.REVIEWED,
            output="Looks fine.",
            evidence=("git diff origin/main...HEAD",),
        )

    monkeypatch.setattr(dev_mod, "run_code_review", fake_review)


async def _dev_run(tmp_path: Path, db: StateDB, *, spawn, transport=None):
    from ctrlrelay.core.pr_verifier import VerificationResult
    from ctrlrelay.pipelines.dev import run_dev_issue

    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = spawn

    worktree = AsyncMock()
    worktree.push_run_branch.return_value = "c0ffee"
    worktree.create_worktree_with_new_branch.return_value = (tmp_path / "wt", True)
    worktree.get_default_branch.return_value = "main"
    worktree.push_run_branch.return_value = "c0ffee"
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()

    github = AsyncMock()
    fake_relay_publish(github, pr_number=7, repo="o/r")
    github.get_issue.return_value = {
        "number": 1, "title": "x", "body": "y", "comments": [],
    }

    verifier = AsyncMock()
    verifier.verify.return_value = VerificationResult(ready=True)

    return await run_dev_issue(
        claims=None,
        repo="o/r",
        issue_number=1,
        branch_template="fix/issue-{n}",
        dispatcher=dispatcher,
        github=github,
        worktree=worktree,
        dashboard=None,
        state_db=db,
        transport=transport,
        contexts_dir=tmp_path / "contexts",
        pr_verifier=verifier,
        code_review=SimpleNamespace(method="cli", comment_on_pr=False),
    )


@pytest.mark.asyncio
async def test_dev_run_records_dispatched_pr_ci_review_done(
    tmp_path: Path, monkeypatch,
) -> None:
    _reviewed(monkeypatch)
    db = StateDB(tmp_path / "state.db")

    result = await _dev_run(tmp_path, db, spawn=_done_state(7))

    assert result.success, result
    # pr_opened lands inside the CI wait: the first verify that returns
    # is the first time GitHub has answered about the PR.
    assert _kinds(db) == [
        "dispatched", "ci_started", "pr_opened", "ci_finished",
        "review_started", "review_finished", "done",
    ]
    events = _in_order(db)
    assert {e["session_id"] for e in events} == {result.session_id}
    assert {e["repo"] for e in events} == {"o/r"}
    assert {e["issue_number"] for e in events} == {1}

    assert events[0]["detail"] == {"pipeline": "dev"}
    assert events[1]["detail"] == {"pr_number": 7}
    assert events[2]["detail"] == {
        "pr_number": 7, "pr_url": "https://github.com/o/r/pull/7",
    }
    assert events[3]["detail"] == {"pr_number": 7, "ok": True}
    assert events[4]["detail"] == {"pr_number": 7}
    assert events[5]["detail"] == {"pr_number": 7, "ok": True}
    # The dev pipeline carries an exit code only on a failure.
    assert events[6]["detail"] == {
        "pipeline": "dev", "exit_code": None, "question": None,
    }


@pytest.mark.asyncio
async def test_ci_and_review_that_did_not_pass_finish_with_ok_false(
    tmp_path: Path, monkeypatch,
) -> None:
    """``ok`` comes from the outcome, not from the phase having ended: a
    reviewer that never ran and CI that timed out both read ``False``."""
    from ctrlrelay.core.code_review import CodeReviewOutcome, ReviewStatus
    from ctrlrelay.core.pr_verifier import VerificationResult
    from ctrlrelay.pipelines import dev as dev_mod

    async def failed_review(**_kw):
        return CodeReviewOutcome(status=ReviewStatus.FAILED, error="gone")

    monkeypatch.setattr(dev_mod, "run_code_review", failed_review)

    # CI that timed out hands the PR off unchanged, so the review still runs.
    class _SlowCI:
        def __init__(self, **_kw):
            pass

        async def verify(self, *_a, **_kw):
            return VerificationResult(ready=False, timed_out=True, reason="slow")

    monkeypatch.setattr(dev_mod, "PRVerifier", _SlowCI)
    db = StateDB(tmp_path / "state.db")
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = _done_state(9)
    worktree = AsyncMock()
    worktree.push_run_branch.return_value = "c0ffee"
    worktree.create_worktree_with_new_branch.return_value = (tmp_path / "wt", True)
    worktree.get_default_branch.return_value = "main"
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()
    github = AsyncMock()
    fake_relay_publish(github, pr_number=9, repo="o/r")
    github.get_issue.return_value = {"number": 1, "title": "x", "body": ""}

    result = await dev_mod.run_dev_issue(
        claims=None,
        repo="o/r", issue_number=1, branch_template="fix/issue-{n}",
        dispatcher=dispatcher, github=github, worktree=worktree,
        dashboard=None, state_db=db, transport=None,
        contexts_dir=tmp_path / "contexts",
        code_review=SimpleNamespace(method="cli", comment_on_pr=False),
    )

    assert result.success
    assert _by_kind(db, "ci_finished")[0]["detail"] == {
        "pr_number": 9, "ok": False,
    }
    assert _by_kind(db, "review_finished")[0]["detail"] == {
        "pr_number": 9, "ok": False,
    }


@pytest.mark.asyncio
async def test_dev_blocked_records_blocked_with_question(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    long_q = "pin or bump? " + "x" * 400

    result = await _dev_run(tmp_path, db, spawn=_blocked_state(long_q))

    assert result.blocked
    assert _kinds(db) == ["dispatched", "blocked"]
    blocked = _by_kind(db, "blocked")[0]
    assert blocked["session_id"] == result.session_id
    assert blocked["detail"] == {
        "pipeline": "dev", "exit_code": None, "question": long_q[:300],
    }


@pytest.mark.asyncio
async def test_dev_run_that_raises_records_failed(tmp_path: Path) -> None:
    from ctrlrelay.pipelines.dev import run_dev_issue

    db = StateDB(tmp_path / "state.db")
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = RuntimeError("agent crashed")
    worktree = AsyncMock()
    worktree.push_run_branch.return_value = "c0ffee"
    worktree.create_worktree_with_new_branch.return_value = (tmp_path / "wt", True)
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()
    github = AsyncMock()
    fake_relay_publish(github, pr_number=7, repo="o/r")
    github.get_issue.return_value = {"number": 1, "title": "x", "body": ""}

    result = await run_dev_issue(
        claims=None,
        repo="o/r", issue_number=1, branch_template="fix/issue-{n}",
        dispatcher=dispatcher, github=github, worktree=worktree,
        dashboard=None, state_db=db, transport=None,
        contexts_dir=tmp_path / "contexts",
    )

    assert not result.success
    assert _kinds(db) == ["dispatched", "failed"]
    assert _by_kind(db, "failed")[0]["detail"] == {
        "pipeline": "dev", "exit_code": None, "question": None,
    }


# --- the most important one ----------------------------------------------


def _comparable(result, db: StateDB) -> tuple:
    """The run's result and its sessions row, minus what differs between
    two identical runs anyway (the random session id, clock times, the
    tmp directory)."""
    row = dict(db.execute(
        "SELECT * FROM sessions WHERE id = ?", (result.session_id,),
    ).fetchone())
    for key in ("id", "started_at", "ended_at", "worktree_path"):
        row.pop(key)
    return (
        result.success, result.blocked, result.summary, result.error,
        result.question, result.exit_code, result.outputs, result.review,
        row,
    )


@pytest.mark.asyncio
async def test_event_write_failure_does_not_change_the_run(
    tmp_path: Path, monkeypatch,
) -> None:
    _reviewed(monkeypatch)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    clean_db = StateDB(tmp_path / "a" / "state.db")
    clean = await _dev_run(tmp_path / "a", clean_db, spawn=_done_state())

    calls: list[str] = []

    def broken(self, kind, **_kw):
        calls.append(kind)
        raise sqlite3.OperationalError("database is locked")

    # Both event write paths: the plain one and the record-once one.
    monkeypatch.setattr(StateDB, "record_event_nowait", broken)
    monkeypatch.setattr(StateDB, "record_event_once_nowait", broken)
    broken_db = StateDB(tmp_path / "b" / "state.db")
    run = await _dev_run(tmp_path / "b", broken_db, spawn=_done_state())

    # The write path was actually exercised, once per event the clean run
    # recorded: without this, a run that never tried to write would pass.
    assert calls == _kinds(clean_db)
    assert len(calls) == 7
    assert broken_db.list_events() == []

    assert _comparable(run, broken_db) == _comparable(clean, clean_db)
    assert run.success and run.outputs["pr_number"] == 7


# --- task and secops -----------------------------------------------------


def _task_spawn(status: str, question: str | None = None):
    from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
    from ctrlrelay.core.dispatcher import SessionResult

    async def spawn(**kwargs):
        return SessionResult(
            session_id=kwargs["session_id"],
            exit_code=0, stdout="", stderr="",
            agent_session_id="b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10",
            state=CheckpointState(
                version="1",
                status=CheckpointStatus(status),
                session_id=kwargs["session_id"],
                timestamp="2026-10-30T12:00:00Z",
                summary="ran it" if status == "DONE" else None,
                question=question,
                # #219: the relay posts the report from the outputs.
                outputs={"report": "build is green"} if status == "DONE" else {},
            ),
        )

    return spawn


def _task_fakes(tmp_path: Path, spawn):
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = spawn
    worktree = AsyncMock()
    worktree.push_run_branch.return_value = "c0ffee"
    worktree.create_worktree.return_value = tmp_path / "wt"
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()
    github = AsyncMock()
    fake_relay_publish(github, pr_number=7, repo="o/r")
    github.get_issue.return_value = {"number": 55, "title": "t", "body": ""}
    return dispatcher, worktree, github


@pytest.mark.asyncio
async def test_task_and_secops_record_dispatched_and_outcome(
    tmp_path: Path,
) -> None:
    from ctrlrelay.core.checkpoint import CheckpointStatus
    from ctrlrelay.core.dispatcher import SessionResult
    from ctrlrelay.pipelines.secops import run_secops_all
    from ctrlrelay.pipelines.task import run_task_issue

    db = StateDB(tmp_path / "state.db")
    dispatcher, worktree, github = _task_fakes(tmp_path, _task_spawn("DONE"))
    task = await run_task_issue(
        claims=None,
        repo="o/r", issue_number=55, dispatcher=dispatcher, github=github,
        worktree=worktree, dashboard=None, state_db=db, transport=None,
        contexts_dir=tmp_path / "contexts",
    )
    assert task.success
    assert _kinds(db) == ["dispatched", "done"]
    t_dispatched, t_done = _in_order(db)
    assert t_dispatched["detail"] == {"pipeline": "task"}
    # The task pipeline's result carries no exit code, so none is claimed.
    assert t_done["detail"] == {
        "pipeline": "task", "exit_code": None, "question": None,
    }
    assert {t_dispatched["session_id"], t_done["session_id"]} == {
        task.session_id,
    }
    assert t_done["issue_number"] == 55

    sec_db = StateDB(tmp_path / "secops.db")
    state = MagicMock()
    state.status = CheckpointStatus.FAILED
    state.question = None
    state.summary = None
    state.outputs = {}
    state.error = "alerts API said no"
    sec_dispatcher = AsyncMock()
    sec_dispatcher.spawn_session.return_value = SessionResult(
        session_id="agent", exit_code=3, state=state,
    )
    sec_worktree = AsyncMock()
    sec_worktree.push_run_branch.return_value = "c0ffee"
    sec_worktree.create_worktree.return_value = tmp_path / "swt"
    sec_worktree.symlink_context = MagicMock()
    sec_worktree.remove_context_symlink = MagicMock()
    repo = MagicMock()
    repo.name = "o/sec"
    repo.automation = None

    [sec] = await run_secops_all(
        job_claims=None,
        repos=[repo], dispatcher=sec_dispatcher, github=MagicMock(),
        worktree=sec_worktree, dashboard=None, state_db=sec_db,
        transport=None, contexts_dir=tmp_path / "contexts",
    )

    assert not sec.success and not sec.blocked
    assert _kinds(sec_db) == ["dispatched", "failed"]
    s_dispatched, s_failed = _in_order(sec_db)
    assert s_dispatched["issue_number"] is None
    assert s_failed["issue_number"] is None
    assert s_dispatched["detail"] == {"pipeline": "secops"}
    assert s_failed["detail"] == {
        "pipeline": "secops", "exit_code": 3, "question": None,
    }
    # The fake agent reports its own id; the events carry the run's.
    [(row_id,)] = sec_db.execute(
        "SELECT id FROM sessions WHERE repo = 'o/sec'"
    ).fetchall()
    assert s_dispatched["session_id"] == s_failed["session_id"] == row_id
    assert s_failed["repo"] == "o/sec"


@pytest.mark.asyncio
async def test_a_resumed_task_records_how_it_ended(tmp_path: Path) -> None:
    """A blocked session that is answered later ends in the resume path.
    Without an event there, the feed would show it blocked forever."""
    from ctrlrelay.pipelines.task import resume_task_from_pending, run_task_issue

    db = StateDB(tmp_path / "state.db")
    dispatcher, worktree, github = _task_fakes(
        tmp_path, _task_spawn("BLOCKED_NEEDS_INPUT", "which build?"),
    )
    blocked = await run_task_issue(
        claims=None,
        repo="o/r", issue_number=55, dispatcher=dispatcher, github=github,
        worktree=worktree, dashboard=None, state_db=db, transport=None,
        contexts_dir=tmp_path / "contexts",
    )
    assert blocked.blocked
    assert _kinds(db) == ["dispatched", "blocked"]

    dispatcher.spawn_session.side_effect = _task_spawn("DONE")
    resumed = await resume_task_from_pending(
        session_id=blocked.session_id, repo="o/r", answer="npm run build",
        dispatcher=dispatcher, github=github, worktree=worktree,
        dashboard=None, state_db=db, transport=None,
        contexts_dir=tmp_path / "contexts",
    )

    assert resumed.success
    assert _kinds(db) == ["dispatched", "blocked", "done"]
    done = _by_kind(db, "done")[0]
    assert done["session_id"] == blocked.session_id
    assert done["detail"]["pipeline"] == "task"


@pytest.mark.asyncio
async def test_secops_hold_records_question_held(tmp_path: Path) -> None:
    from ctrlrelay.pipelines.secops import QUESTION_HELD_UNTIL
    from ctrlrelay.transports.base import TransportUnansweredError
    from tests.test_question_backoff import MINER, _sweep, _ttl_expired_chain

    db = StateDB(tmp_path / "state.db")
    now = int(time.time())
    _ttl_expired_chain(db, 2, last_posted=now - DAY)
    transport = AsyncMock()
    transport.ask.side_effect = TransportUnansweredError("nobody answered")

    held = await _sweep(tmp_path, db, transport)

    assert held.blocked
    [event] = _by_kind(db, "question_held")
    assert event["detail"] == {"held_until": held.outputs[QUESTION_HELD_UNTIL]}
    assert event["session_id"] == held.session_id
    assert event["repo"] == MINER
    assert event["issue_number"] is None
    # Held before the outcome was written.
    assert _kinds(db) == ["dispatched", "question_held", "blocked"]


# --- merge --------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_watch_merge_records_merged(tmp_path: Path) -> None:
    """One row per merge, per session and PR: none for a PR seen open
    until the watch times out, one for a merge, none again when a watcher
    resumes the cleanup of a merge already recorded, and one when it
    resumes the cleanup of a merge that was never recorded."""
    from ctrlrelay.pipelines.post_merge import pr_watch_task

    db = StateDB(tmp_path / "state.db")

    async def factory():
        return AsyncMock()

    open_pr = AsyncMock()
    open_pr.get_pr_state.return_value = {"state": "OPEN"}
    timed_out = await pr_watch_task(
        repo="o/r", issue_number=76, pr_url="", pr_number=41,
        session_id="sess-0", github=open_pr, poll_interval=1, timeout=1,
        state_db=db,
    )
    assert timed_out["timed_out"] is True
    # The watcher saw the PR open, rather than giving up before looking.
    assert open_pr.get_pr_state.await_count >= 1
    assert _by_kind(db, "merged") == []

    merged_pr = AsyncMock()
    merged_pr.get_pr_state.return_value = {"state": "MERGED"}

    async def watch(pr: int, issue: int, session: str):
        return await pr_watch_task(
            repo="o/r", issue_number=issue,
            pr_url=f"https://github.com/o/r/pull/{pr}", pr_number=pr,
            session_id=session, github=merged_pr, transport_factory=factory,
            poll_interval=0, timeout=5, state_db=db,
        )

    def resumable(pr: int, issue: int, session: str) -> None:
        """A watch row whose cleanup an earlier run started."""
        db.add_pr_watch(
            repo="o/r", pr_number=pr, issue_number=issue, session_id=session,
            pr_url=f"https://github.com/o/r/pull/{pr}",
        )
        db.set_pr_watch_cleanup_phase("o/r", pr, "commented")

    assert (await watch(42, 77, "sess-1"))["merged"] is True
    # Resumed after the merge was recorded: no second row.
    resumable(42, 77, "sess-1")
    assert (await watch(42, 77, "sess-1"))["merged"] is True
    # Resumed after a run that found the merge but never recorded it.
    resumable(43, 78, "sess-2")
    assert (await watch(43, 78, "sess-2"))["merged"] is True

    merged = _by_kind(db, "merged")
    assert [(m["session_id"], m["detail"]) for m in merged] == [
        ("sess-1", {
            "pr_number": 42, "pr_url": "https://github.com/o/r/pull/42",
        }),
        ("sess-2", {
            "pr_number": 43, "pr_url": "https://github.com/o/r/pull/43",
        }),
    ]
    assert [m["issue_number"] for m in merged] == [77, 78]


# --- bridge -------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_send_and_ask_record_events(tmp_path: Path) -> None:
    from ctrlrelay import __version__
    from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
    from ctrlrelay.bridge.server import BridgeServer

    sock_dir = tempfile.mkdtemp()  # AF_UNIX path length on macOS
    socket_path = Path(sock_dir) / "b.sock"
    db = StateDB(tmp_path / "state.db")
    server = BridgeServer(
        socket_path=socket_path, handler=FakeChatHandler(), state_db=db,
    )
    task = asyncio.create_task(server.start())
    await asyncio.sleep(0.1)
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        writer.write(serialize_message(BridgeMessage(
            op=BridgeOp.SEND, request_id="r-send", text="hello there",
            session_id="dev-o-r-5-abc", repo="o/r", issue_number=5,
        )).encode())
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=1)
        writer.write(serialize_message(BridgeMessage(
            op=BridgeOp.ASK, request_id="r-ask", question="pin or bump?",
            session_id="dev-o-r-5-abc", repo="o/r", issue_number=5,
        )).encode())
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=1)
    finally:
        writer.close()
        await writer.wait_closed()
        await server.stop()
        task.cancel()
        shutil.rmtree(sock_dir, ignore_errors=True)

    assert _kinds(db) == ["restart", "message_sent", "question_posted"]
    restart, sent, posted = _in_order(db)
    assert restart["detail"] == {"process": "bridge", "version": __version__}
    assert sent["detail"] == {"request_id": "r-send", "chars": len("hello there")}
    assert posted["detail"] == {"request_id": "r-ask"}
    for e in (sent, posted):
        assert (e["repo"], e["issue_number"], e["session_id"]) == (
            "o/r", 5, "dev-o-r-5-abc",
        )


# --- cli: relay-health alert, poller restart, hourly prune ---------------


@pytest.mark.asyncio
async def test_relay_alert_delivery_records_alert_sent(tmp_path: Path) -> None:
    """Recorded for a delivered alert, and only for one."""
    import ctrlrelay.cli as cli_module

    db = StateDB(tmp_path / "state.db")
    token = db.claim_dispatch_alert(lease_seconds=60)
    alert = SimpleNamespace(claim_token=token, consecutive=4, reason="x")

    with patch.object(
        cli_module, "send_relay_alert", new=AsyncMock(return_value=False),
    ):
        assert await cli_module.deliver_relay_alert(None, db, alert) is False
    with patch.object(
        cli_module, "send_relay_alert", new=AsyncMock(return_value=True),
    ):
        assert await cli_module.deliver_relay_alert(None, db, alert) is True

    assert _kinds(db) == ["alert_sent"]
    assert _in_order(db)[0]["detail"] == {"consecutive_failures": 4}


def test_hourly_sweep_prunes_events_past_history(tmp_path: Path) -> None:
    """Drives `poller start --foreground` far enough to start the
    scheduler, then runs the hourly sweeper it registered. The config has
    no chat transport, so the sweeper returns early on the missing TTL:
    the prune must already have happened by then."""
    from typer.testing import CliRunner

    from ctrlrelay import __version__
    from ctrlrelay.cli import app

    db_path = tmp_path / "state.db"
    config = {
        "version": "1",
        "node_id": "test-node",
        "timezone": "UTC",
        "paths": {
            "state_db": str(db_path),
            "worktrees": str(tmp_path / "worktrees"),
            "bare_repos": str(tmp_path / "repos"),
            "contexts": str(tmp_path / "contexts"),
            "skills": str(tmp_path / "skills"),
        },
        "claude": {"binary": "claude", "default_timeout_seconds": 1800},
        "transport": {
            "type": "file_mock",
            "file_mock": {
                "inbox": str(tmp_path / "inbox.txt"),
                "outbox": str(tmp_path / "outbox.txt"),
            },
        },
        "dashboard": {"enabled": False},
        "repos": [{"name": "o/r", "local_path": str(tmp_path / "o-r")}],
    }
    config_path = tmp_path / "orchestrator.yaml"
    config_path.write_text(yaml.dump(config))

    now = int(time.time())
    seed = StateDB(db_path)
    seed.record_event("done", repo="o/r", ts=now - 8 * DAY)
    seed.record_event("done", repo="o/r", ts=now - 1 * DAY)
    seed.close()

    jobs: dict[str, object] = {}
    ran: list[str] = []
    scheduler = MagicMock()
    scheduler.add_cron_job.side_effect = (
        lambda *, name, cron_expr, func, **_kw: jobs.__setitem__(name, func)
    )
    scheduler.shutdown = AsyncMock()

    async def fake_poll_loop(**_kw):
        await jobs["question_expiry_sweeper"]()
        ran.append("sweeper")

    gh_user = subprocess.CompletedProcess(
        args=["gh"], returncode=0, stdout="me\n", stderr="",
    )
    with (
        patch("ctrlrelay.core.github._find_gh", return_value="gh"),
        patch("subprocess.run", return_value=gh_user),
        patch("ctrlrelay.core.scheduler.make_scheduler", return_value=scheduler),
        patch("ctrlrelay.core.poller.run_poll_loop", new=fake_poll_loop),
        patch(
            "ctrlrelay.core.poller.IssuePoller.seed_current", new=AsyncMock(),
        ),
    ):
        result = CliRunner().invoke(
            app,
            ["poller", "start", "--foreground", "--config", str(config_path)],
        )

    assert ran == ["sweeper"], result.output
    scheduler.start.assert_called_once()
    db = StateDB(db_path)
    try:
        kinds_and_ts = [(e["kind"], e["ts"]) for e in _in_order(db)]
        restart = _by_kind(db, "restart")
    finally:
        db.close()
    assert ("done", now - 8 * DAY) not in kinds_and_ts
    assert ("done", now - 1 * DAY) in kinds_and_ts
    assert [r["detail"] for r in restart] == [
        {"process": "poller", "version": __version__},
    ]


# --- review round on 0f47ef6 ----------------------------------------------


def test_in_memory_event_write_never_commits_an_open_transaction(
    caplog,
) -> None:
    """In-memory, event writes and prunes fall back to the main connection.
    With somebody's transaction open there, they must skip rather than
    join it and commit it."""
    import logging

    from ctrlrelay.core.events import EventKind, emit_event

    db = StateDB(":memory:")
    try:
        db.record_event("dispatched", repo="o/r", ts=1)
        # Somebody's half-finished write, not yet committed.
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES ('half', 'dev', 'o/r', 'running', 1)"
        )
        assert db._conn.in_transaction

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            emit_event(db, EventKind.DONE, lambda: {"repo": "o/r", "ts": 2})
            assert db.prune_events_nowait(10) == 0

        assert db._conn.in_transaction, "the open transaction was committed"
        db._conn.rollback()
        assert db.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = 'half'"
        ).fetchone()[0] == 0
        # Neither the event nor the prune happened.
        assert [e["ts"] for e in db.list_events()] == [1]
        skipped = [
            r for r in caplog.records
            if r.getMessage() == "events.write_skipped"
        ]
        assert [r.kind for r in skipped] == ["done", "prune"]

        # With no transaction open, the fallback still writes and prunes.
        emit_event(db, EventKind.DONE, lambda: {"repo": "o/r", "ts": 20})
        assert db.prune_events_nowait(10) == 1
        assert [e["ts"] for e in db.list_events()] == [20]
    finally:
        db.close()


def _emit_calls() -> list[tuple[str, int, ast.Call]]:
    import ctrlrelay

    root = Path(ctrlrelay.__file__).parent
    calls = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("emit_event", "emit_event_once", "emit_outcome")
            ):
                calls.append((str(path.relative_to(root)), node.lineno, node))
    return calls


def test_no_emit_call_site_evaluates_anything_outside_the_guard() -> None:
    """Arguments are evaluated before the callee runs, so anything a call
    site computes can raise in the caller. Every argument must be a bare
    name, ``self.<name>``, an ``EventKind`` member, or the builder (a
    lambda or a named local function) whose body runs inside the guard."""

    def plain(node: ast.expr) -> bool:
        return isinstance(node, ast.Name) or (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in ("self", "EventKind")
        )

    calls = _emit_calls()
    # The sweep actually found the call sites (an empty sweep passes).
    assert len(calls) >= 25, len(calls)
    bad = []
    for where, line, call in calls:
        builder_ok = bool(call.args) and isinstance(
            call.args[-1], (ast.Lambda, ast.Name)
        )
        if call.keywords or not builder_ok or not all(
            plain(a) for a in call.args[:-1]
        ):
            bad.append(f"{where}:{line}")
    assert bad == []


@pytest.mark.asyncio
async def test_an_alert_whose_fields_raise_is_still_delivered(
    tmp_path: Path, caplog,
) -> None:
    import logging

    import ctrlrelay.cli as cli_module

    class _Alert:
        reason = "x"

        def __init__(self, token):
            self.claim_token = token

        @property
        def consecutive(self):
            raise RuntimeError("not countable")

    db = StateDB(tmp_path / "state.db")
    alert = _Alert(db.claim_dispatch_alert(lease_seconds=60))

    with (
        patch.object(
            cli_module, "send_relay_alert", new=AsyncMock(return_value=True),
        ),
        caplog.at_level(logging.INFO, logger="ctrlrelay"),
    ):
        assert await cli_module.deliver_relay_alert(None, db, alert) is True

    failed = [
        r for r in caplog.records if r.getMessage() == "events.write_failed"
    ]
    assert [(r.kind, r.reason) for r in failed] == [("alert_sent", "RuntimeError")]
    # Not mistaken for an unrecorded delivery: the claim was marked.
    unrecorded = [
        r for r in caplog.records
        if r.getMessage() == "dispatch_health.delivery_unrecorded"
    ]
    assert unrecorded == []
    assert db.list_events() == []


@pytest.mark.asyncio
async def test_a_run_that_fails_before_its_session_exists_records_nothing(
    tmp_path: Path,
) -> None:
    """Setup failures (issue fetch, worktree) end before the `sessions`
    row is written. A `failed` event there would name a session nothing
    else knows about."""
    from ctrlrelay.pipelines.dev import run_dev_issue
    from ctrlrelay.pipelines.secops import run_secops_all
    from ctrlrelay.pipelines.task import run_task_issue

    db = StateDB(tmp_path / "state.db")
    db.record_event("dispatched", repo="o/r", ts=1)  # not an empty table

    github = AsyncMock()
    fake_relay_publish(github, pr_number=7, repo="o/r")
    github.get_issue.side_effect = RuntimeError("gh down")
    worktree = AsyncMock()
    worktree.push_run_branch.return_value = "c0ffee"
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()

    dev = await run_dev_issue(
        claims=None,
        repo="o/r", issue_number=1, branch_template="fix/issue-{n}",
        dispatcher=AsyncMock(), github=github, worktree=worktree,
        dashboard=None, state_db=db, transport=None,
        contexts_dir=tmp_path / "contexts",
    )
    task = await run_task_issue(
        claims=None,
        repo="o/t", issue_number=2, dispatcher=AsyncMock(), github=github,
        worktree=worktree, dashboard=None, state_db=db, transport=None,
        contexts_dir=tmp_path / "contexts",
    )
    sec_worktree = AsyncMock()
    sec_worktree.push_run_branch.return_value = "c0ffee"
    sec_worktree.ensure_bare_repo.side_effect = RuntimeError("clone failed")
    sec_worktree.remove_context_symlink = MagicMock()
    repo = MagicMock()
    repo.name = "o/s"
    repo.automation = None
    [sec] = await run_secops_all(
        job_claims=None,
        repos=[repo], dispatcher=AsyncMock(), github=MagicMock(),
        worktree=sec_worktree, dashboard=None, state_db=db, transport=None,
        contexts_dir=tmp_path / "contexts",
    )

    assert not dev.success and not task.success and not sec.success
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert _kinds(db) == ["dispatched"]


@pytest.mark.asyncio
async def test_pr_opened_records_once_after_verify_returns(
    tmp_path: Path, monkeypatch,
) -> None:
    """pr_opened is written after ``verify`` returns, not on the agent's
    word: a verify that raises records none, and one that returns records
    the PR once, however many verify rounds follow. The verifier is a
    mock here; that a returning verify means GitHub answered about the PR
    is ``PRVerifier.verify``'s own contract (``wait_for_checks`` raises
    when no read succeeded), tested with the verifier."""
    from ctrlrelay.core.pr_verifier import VerificationResult
    from ctrlrelay.pipelines.dev import run_dev_issue

    _reviewed(monkeypatch)

    async def run(db: StateDB, verify_side_effect):
        verifier = AsyncMock()
        verifier.verify.side_effect = verify_side_effect
        dispatcher = AsyncMock()
        dispatcher.spawn_session.side_effect = _done_state(7)
        worktree = AsyncMock()
        worktree.push_run_branch.return_value = "c0ffee"
        worktree.create_worktree_with_new_branch.return_value = (
            tmp_path / "wt", True,
        )
        worktree.get_default_branch.return_value = "main"
        worktree.symlink_context = MagicMock()
        worktree.remove_context_symlink = MagicMock()
        github = AsyncMock()
        fake_relay_publish(github, pr_number=7, repo="o/r")
        github.get_issue.return_value = {"number": 1, "title": "x", "body": ""}
        return await run_dev_issue(
            claims=None,
            repo="o/r", issue_number=1, branch_template="fix/issue-{n}",
            dispatcher=dispatcher, github=github, worktree=worktree,
            dashboard=None, state_db=db, transport=None,
            contexts_dir=tmp_path / "contexts", pr_verifier=verifier,
            code_review=SimpleNamespace(method="cli", comment_on_pr=False),
        )

    unconfirmed = StateDB(tmp_path / "a.db")
    result = await run(unconfirmed, RuntimeError("gh: could not resolve PR"))
    assert not result.success
    assert "pr_opened" not in _kinds(unconfirmed)
    assert "ci_finished" in _kinds(unconfirmed)

    confirmed = StateDB(tmp_path / "b.db")
    result = await run(confirmed, [
        VerificationResult(ready=False, reason="1 check(s) failing: ci"),
        VerificationResult(ready=True),
    ])
    assert result.success, result
    assert _kinds(confirmed).count("ci_started") == 2
    [opened] = _by_kind(confirmed, "pr_opened")
    assert opened["detail"] == {
        "pr_number": 7, "pr_url": "https://github.com/o/r/pull/7",
    }


def test_record_once_is_one_bounded_statement(tmp_path: Path, caplog) -> None:
    """The "once" check and the write are one statement: under a lock held
    by another connection the whole emit gives up within one write
    timeout (not one per step), and once the lock is gone a second emit
    for the same session and PR writes no row."""
    import logging

    from ctrlrelay.core.events import EventKind, emit_event_once

    path = tmp_path / "state.db"
    db = StateDB(path)

    def fields(pr: int, session: str = "s-1"):
        return lambda: {
            "repo": "o/r", "session_id": session,
            "detail": {"pr_number": pr, "pr_url": None},
        }

    try:
        db.record_event("merged", repo="o/r", session_id="s-1", detail={
            "pr_number": 6, "pr_url": None,
        }, ts=1)
        holder = sqlite3.connect(str(path), isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("INSERT INTO events (ts, kind) VALUES (2, 'held')")
            with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                start = time.monotonic()
                emit_event_once(db, EventKind.MERGED, fields(7))
                elapsed = time.monotonic() - start
            assert elapsed < 0.6, elapsed
            assert [
                r.kind for r in caplog.records
                if r.getMessage() == "events.write_failed"
            ] == ["merged"]
            assert db._event_conn is not None
            assert not db._event_conn.in_transaction
        finally:
            holder.execute("ROLLBACK")
            holder.close()

        emit_event_once(db, EventKind.MERGED, fields(7))
        emit_event_once(db, EventKind.MERGED, fields(7))  # same session+PR
        emit_event_once(db, EventKind.MERGED, fields(6))  # already there
        emit_event_once(db, EventKind.MERGED, fields(7, "s-2"))  # other session
        emit_event_once(db, EventKind.PR_OPENED, fields(7))  # other kind
        rows = [
            (e["kind"], e["session_id"], e["detail"]["pr_number"])
            for e in _in_order(db)
        ]
        assert rows == [
            ("merged", "s-1", 6),
            ("merged", "s-1", 7),
            ("merged", "s-2", 7),
            ("pr_opened", "s-1", 7),
        ]
    finally:
        db.close()


def test_the_once_lookup_is_answered_from_the_session_index(
    tmp_path: Path,
) -> None:
    db = StateDB(tmp_path / "state.db")
    try:
        plan = db.execute(
            "EXPLAIN QUERY PLAN " + StateDB._EVENT_ONCE_SQL,
            (1, "merged", "o/r", None, "s-1", None, "s-1", "merged", "o/r", 7),
        ).fetchall()
        details = [row[-1] for row in plan]
        assert any("idx_events_session" in d for d in details), details
        assert not any(d.startswith("SCAN events") for d in details), details
    finally:
        db.close()
