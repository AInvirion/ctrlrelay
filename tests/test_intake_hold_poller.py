"""The poller obeys the intake hold (#306), driven through the real
``poller start --foreground`` wiring: the handler it passes to the poll
loop and the jobs it registers with the scheduler are the ones under test,
not copies of them.

With a hold in place, a new issue, the scheduled sweep and the
resumed-question sweeper must each stop before doing anything; with the
hold gone, each must reach its first real step. The "reached" side is
what makes the "stopped" side mean something.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from typer.testing import CliRunner

from ctrlrelay.core.intake_hold import IntakeHold


class ReachedError(Exception):
    """Raised by the first real step of a piece of work: it got past the
    hold."""


def _config(tmp_path: Path) -> Path:
    config = {
        "version": "1",
        "node_id": "test-node",
        "timezone": "UTC",
        "paths": {
            "state_db": str(tmp_path / "state.db"),
            "worktrees": str(tmp_path / "worktrees"),
            "bare_repos": str(tmp_path / "repos"),
            "contexts": str(tmp_path / "contexts"),
            "skills": str(tmp_path / "skills"),
        },
        "agent": {"binary": "claude", "default_timeout_seconds": 1800},
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
    path = tmp_path / "orchestrator.yaml"
    path.write_text(yaml.dump(config))
    return path


def _drive(tmp_path: Path, body) -> list:
    """Start the poller with ``body(handler, jobs, poller, hold)`` as its
    poll loop; return what ``body`` recorded."""
    jobs: dict[str, object] = {}
    scheduler = MagicMock()
    scheduler.add_cron_job.side_effect = (
        lambda *, name, cron_expr, func, **_kw: jobs.__setitem__(name, func)
    )
    scheduler.shutdown = AsyncMock()
    record: list = []
    hold = IntakeHold.beside(tmp_path / "state.db")

    async def fake_poll_loop(*, poller, handler, **_kw):
        await body(handler, jobs, poller, hold, record)

    gh_user = subprocess.CompletedProcess(args=["gh"], returncode=0,
                                          stdout="me\n", stderr="")
    with (
        patch("ctrlrelay.core.github._find_gh", return_value="gh"),
        patch("subprocess.run", return_value=gh_user),
        patch("ctrlrelay.core.scheduler.make_scheduler", return_value=scheduler),
        patch("ctrlrelay.core.poller.run_poll_loop", new=fake_poll_loop),
        patch("ctrlrelay.core.poller.IssuePoller.seed_current", new=AsyncMock()),
        # The blocker check (#310) runs before the claim; no blockers here.
        patch(
            "ctrlrelay.core.github.GitHubCLI.list_blocked_by",
            new=AsyncMock(return_value=[]),
        ),
        # The first real step of each piece of work.
        patch("ctrlrelay.cli.peek_claim_owner", new=AsyncMock(side_effect=ReachedError)),
        patch("ctrlrelay.cli._bridge_socket_settings", side_effect=ReachedError),
        patch("ctrlrelay.cli.watch_issue_answers", new=AsyncMock(side_effect=ReachedError)),
    ):
        result = CliRunner().invoke(
            app_(), ["poller", "start", "--foreground", "--config",
                     str(_config(tmp_path))],
        )
    assert record, result.output
    return record


def app_():
    from ctrlrelay.cli import app
    return app


async def _attempt(record: list, name: str, call) -> None:
    try:
        await call()
    except ReachedError:
        record.append((name, "reached"))
    else:
        record.append((name, "stopped"))


ISSUE = {"number": 7, "title": "t", "labels": [], "assignees": []}


def test_a_held_poller_starts_no_issue_sweep_or_resume(tmp_path: Path) -> None:
    async def body(handler, jobs, poller, hold, record) -> None:
        hold.place(token="t", reason="self-update to 9.9", seconds=600)
        await _attempt(record, "issue", lambda: handler("o/r", ISSUE))
        await _attempt(record, "secops", jobs["secops"])
        await _attempt(record, "resume", jobs["pending_resume_sweeper"])
        # The issue is offered again rather than dropped.
        record.append(("rechecked", 7 in poller.recheck_after.get("o/r", {})))
        hold.release("t")

    assert _drive(tmp_path, body) == [
        ("issue", "stopped"), ("secops", "stopped"), ("resume", "stopped"),
        ("rechecked", True),
    ]


def test_without_a_hold_each_reaches_its_first_step(tmp_path: Path) -> None:
    async def body(handler, jobs, poller, hold, record) -> None:
        await _attempt(record, "issue", lambda: handler("o/r", ISSUE))
        await _attempt(record, "secops", jobs["secops"])
        await _attempt(record, "resume", jobs["pending_resume_sweeper"])

    assert _drive(tmp_path, body) == [
        ("issue", "reached"), ("secops", "reached"), ("resume", "reached"),
    ]


def test_the_poller_acknowledges_and_counts_a_run_in_flight(tmp_path: Path) -> None:
    """While an issue's handler is running, the poller's acknowledgement
    says busy; once it returns, not busy."""

    async def body(handler, jobs, poller, hold, record) -> None:
        release = asyncio.Event()

        async def slow_first_step(*_a, **_kw):
            await release.wait()
            raise ReachedError

        with patch("ctrlrelay.cli.peek_claim_owner", new=slow_first_step):
            run = asyncio.create_task(handler("o/r", ISSUE))
            await asyncio.sleep(0)
            hold.place(token="t", reason="r", seconds=600)
            await asyncio.sleep(2.5)            # one acknowledgement tick
            ack = hold.read_ack("t")
            record.append(("busy", ack.busy if ack else None))
            release.set()
            try:
                await run
            except ReachedError:
                pass
            await asyncio.sleep(2.5)
            ack = hold.read_ack("t")
            record.append(("busy", ack.busy if ack else None))
        hold.release("t")

    assert _drive(tmp_path, body) == [("busy", True), ("busy", False)]
