"""#209: the daemon writes status-page events at the points it already
reaches, and a failed event write never changes the work it describes.

Each test drives the real function (pipeline run, watcher, bridge, CLI
entry point) with the fakes the existing tests use, and reads the rows
back through ``StateDB.list_events``.
"""

from __future__ import annotations

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
    worktree.create_worktree_with_new_branch.return_value = (tmp_path / "wt", True)
    worktree.get_default_branch.return_value = "main"
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()

    github = AsyncMock()
    github.get_issue.return_value = {
        "number": 1, "title": "x", "body": "y", "comments": [],
    }

    verifier = AsyncMock()
    verifier.verify.return_value = VerificationResult(ready=True)

    return await run_dev_issue(
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
    assert _kinds(db) == [
        "dispatched", "pr_opened", "ci_started", "ci_finished",
        "review_started", "review_finished", "done",
    ]
    events = _in_order(db)
    assert {e["session_id"] for e in events} == {result.session_id}
    assert {e["repo"] for e in events} == {"o/r"}
    assert {e["issue_number"] for e in events} == {1}

    assert events[0]["detail"] == {"pipeline": "dev"}
    assert events[1]["detail"] == {
        "pr_number": 7, "pr_url": "https://github.com/o/r/pull/7",
    }
    assert events[2]["detail"] == {"pr_number": 7}
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
    worktree.create_worktree_with_new_branch.return_value = (tmp_path / "wt", True)
    worktree.get_default_branch.return_value = "main"
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()
    github = AsyncMock()
    github.get_issue.return_value = {"number": 1, "title": "x", "body": ""}

    result = await dev_mod.run_dev_issue(
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
    worktree.create_worktree_with_new_branch.return_value = (tmp_path / "wt", True)
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()
    github = AsyncMock()
    github.get_issue.return_value = {"number": 1, "title": "x", "body": ""}

    result = await run_dev_issue(
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

    monkeypatch.setattr(StateDB, "record_event_nowait", broken)
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
            ),
        )

    return spawn


def _task_fakes(tmp_path: Path, spawn):
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = spawn
    worktree = AsyncMock()
    worktree.create_worktree.return_value = tmp_path / "wt"
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()
    github = AsyncMock()
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
    sec_worktree.create_worktree.return_value = tmp_path / "swt"
    sec_worktree.symlink_context = MagicMock()
    sec_worktree.remove_context_symlink = MagicMock()
    repo = MagicMock()
    repo.name = "o/sec"
    repo.automation = None

    [sec] = await run_secops_all(
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
    """One row per merge found: none for a watch that times out, one for a
    merge, and none again for a watcher that resumes an interrupted
    cleanup of a merge already recorded."""
    from ctrlrelay.pipelines.post_merge import pr_watch_task

    db = StateDB(tmp_path / "state.db")

    async def factory():
        return AsyncMock()

    open_pr = AsyncMock()
    open_pr.get_pr_state.return_value = {"state": "OPEN"}
    timed_out = await pr_watch_task(
        repo="o/r", issue_number=76, pr_url="", pr_number=41,
        session_id="sess-0", github=open_pr, poll_interval=0, timeout=0,
        state_db=db,
    )
    assert timed_out["timed_out"] is True

    merged_pr = AsyncMock()
    merged_pr.get_pr_state.return_value = {"state": "MERGED"}
    outcome = await pr_watch_task(
        repo="o/r", issue_number=77,
        pr_url="https://github.com/o/r/pull/42", pr_number=42,
        session_id="sess-1", github=merged_pr, transport_factory=factory,
        poll_interval=0, timeout=5, state_db=db,
    )
    assert outcome["merged"] is True

    db.add_pr_watch(
        repo="o/r", pr_number=43, issue_number=78, session_id="sess-2",
        pr_url="https://github.com/o/r/pull/43",
    )
    db.set_pr_watch_cleanup_phase("o/r", 43, "commented")
    resumed = await pr_watch_task(
        repo="o/r", issue_number=78,
        pr_url="https://github.com/o/r/pull/43", pr_number=43,
        session_id="sess-2", github=merged_pr, transport_factory=factory,
        poll_interval=0, timeout=5, state_db=db,
    )
    assert resumed["merged"] is True

    [merged] = _by_kind(db, "merged")
    assert merged["detail"] == {
        "pr_number": 42, "pr_url": "https://github.com/o/r/pull/42",
    }
    assert merged["session_id"] == "sess-1"
    assert merged["issue_number"] == 77


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
