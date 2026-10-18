"""Tests for the per-spawn outcome hook and its wiring (#204).

Three things are proven here, and the second is the one the ticket
turns on:

1. **Every** spawn reports its outcome — normal exit, timeout, and a
   binary that cannot be exec'd. Reporting from the dispatcher rather
   than from each pipeline is what makes a lane added later covered by
   construction; a per-call-site design leaves the new lane silently
   exempt, which is how a two-day outage went unreported.
2. A run of failures produces **exactly one** message on a real fake
   transport, and a healthy run produces **none**. Not an `ast` check
   that a `send` call exists — `if False: await transport.send(...)`
   satisfies every structural check while restoring the original
   silence.
3. The exit code and a scrubbed output tail land in `sessions`, and a
   row that was never inserted is reported rather than passing for a
   successful write.
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ctrlrelay.core.dispatcher import (
    ClaudeDispatcher,
    DispatchOutcome,
    make_agent_dispatcher,
)
from ctrlrelay.core.state import StateDB


def _proc(*, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    proc = AsyncMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    return proc


class TestEverySpawnReportsItsOutcome:
    @pytest.mark.asyncio
    async def test_a_clean_exit_is_reported_as_ok(self, tmp_path: Path) -> None:
        seen: list[DispatchOutcome] = []

        async def hook(outcome):
            seen.append(outcome)

        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "version": "1",
            "status": "DONE",
            "session_id": "s",
            "timestamp": "2026-10-18T00:00:00Z",
            "summary": "done",
        }))
        with patch(
            "asyncio.create_subprocess_exec", return_value=_proc(returncode=0)
        ):
            await d.spawn_session(
                session_id="s",
                prompt="p",
                working_dir=tmp_path,
                state_file=state_file,
                repo="o/r",
                issue_number=853,
            )
        assert len(seen) == 1
        assert seen[0].ok is True
        assert seen[0].exit_code == 0
        assert seen[0].repo == "o/r"
        assert seen[0].issue_number == 853

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_is_reported_with_its_output(
        self, tmp_path: Path
    ) -> None:
        """The incident's shape: exit 1, no state file, output on stderr."""
        seen: list[DispatchOutcome] = []

        async def hook(outcome):
            seen.append(outcome)

        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_proc(
                returncode=1, stderr=b"Invalid API key \xc2\xb7 Please run /login"
            ),
        ):
            await d.spawn_session(
                session_id="s",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "missing.json",
            )
        assert len(seen) == 1
        assert seen[0].ok is False
        assert seen[0].exit_code == 1
        assert "Invalid API key" in seen[0].stderr

    @pytest.mark.asyncio
    async def test_an_exit_zero_with_no_checkpoint_is_still_ok(
        self, tmp_path: Path
    ) -> None:
        """`ok` is about the relay, not about the work.

        An agent that exits 0 having written no checkpoint is a
        pipeline failure (#174) and NOT evidence that dispatching is
        broken. Counting it would alert on an agent misbehaving, which
        is a different ticket with a different fix.
        """
        seen: list[DispatchOutcome] = []

        async def hook(outcome):
            seen.append(outcome)

        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        with patch(
            "asyncio.create_subprocess_exec", return_value=_proc(returncode=0)
        ):
            await d.spawn_session(
                session_id="s",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "missing.json",
            )
        assert seen[0].ok is True

    @pytest.mark.asyncio
    async def test_a_timeout_is_reported(self, tmp_path: Path) -> None:
        seen: list[DispatchOutcome] = []

        async def hook(outcome):
            seen.append(outcome)

        d = ClaudeDispatcher(
            claude_binary="claude", default_timeout=1, outcome_hook=hook
        )
        proc = AsyncMock()
        proc.communicate.side_effect = asyncio.TimeoutError()
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await d.spawn_session(
                session_id="s",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "missing.json",
            )
        assert len(seen) == 1
        assert seen[0].ok is False
        assert seen[0].exit_code == -1

    @pytest.mark.asyncio
    async def test_a_binary_that_cannot_be_execd_is_reported(
        self, tmp_path: Path
    ) -> None:
        """A missing binary fails every dispatch identically.

        The exit code is `None`, not 0 and not -1: the child never
        ran, so there is no exit code and inventing one would make
        `ok` read True for the worst case there is.
        """
        seen: list[DispatchOutcome] = []

        async def hook(outcome):
            seen.append(outcome)

        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("no such file"),
        ):
            with pytest.raises(FileNotFoundError):
                await d.spawn_session(
                    session_id="s",
                    prompt="p",
                    working_dir=tmp_path,
                    state_file=tmp_path / "missing.json",
                )
        assert len(seen) == 1
        assert seen[0].exit_code is None
        assert seen[0].ok is False
        assert "no such file" in seen[0].stderr


class TestTheHookCannotBreakADispatch:
    @pytest.mark.asyncio
    async def test_a_raising_hook_is_swallowed(self, tmp_path: Path) -> None:
        """A dispatch that already ran must not be turned into an
        exception by the code that reports on it."""

        async def hook(outcome):
            raise RuntimeError("the database is locked")

        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        with patch(
            "asyncio.create_subprocess_exec", return_value=_proc(returncode=0)
        ):
            result = await d.spawn_session(
                session_id="s",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "missing.json",
            )
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_a_cancelling_hook_still_propagates(
        self, tmp_path: Path
    ) -> None:
        async def hook(outcome):
            raise asyncio.CancelledError()

        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        with patch(
            "asyncio.create_subprocess_exec", return_value=_proc(returncode=0)
        ):
            with pytest.raises(asyncio.CancelledError):
                await d.spawn_session(
                    session_id="s",
                    prompt="p",
                    working_dir=tmp_path,
                    state_file=tmp_path / "missing.json",
                )

    @pytest.mark.asyncio
    async def test_no_hook_is_a_no_op(self, tmp_path: Path) -> None:
        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=None)
        with patch(
            "asyncio.create_subprocess_exec", return_value=_proc(returncode=0)
        ):
            result = await d.spawn_session(
                session_id="s",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "missing.json",
            )
        assert result.exit_code == 0


class TestTheFactoryRefusesToGuess:
    def test_outcome_hook_is_a_required_keyword(self) -> None:
        """An optional parameter whose default is the old behaviour is a
        silent opt-out of the fix, and reads as an ordinary call.

        Required means the interpreter and the suite enumerate the call
        sites instead of whoever remembers to.
        """
        from ctrlrelay.core.config import AgentConfig

        with pytest.raises(TypeError):
            make_agent_dispatcher(AgentConfig())

    def test_an_explicit_none_is_accepted(self) -> None:
        from ctrlrelay.core.config import AgentConfig

        d = make_agent_dispatcher(AgentConfig(), outcome_hook=None)
        assert d.outcome_hook is None

    def test_the_hook_is_carried_through_to_the_adapter(self) -> None:
        from ctrlrelay.core.config import AgentConfig

        async def hook(outcome):
            return None

        d = make_agent_dispatcher(AgentConfig(), outcome_hook=hook)
        assert d.outcome_hook is hook


class TestPersistDispatchOutcome:
    def test_the_exit_code_and_output_land_on_the_row(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.cli import persist_dispatch_outcome

        db = StateDB(tmp_path / "state.db")
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s", "dev", "o/r", "running", 0),
        )
        db.commit()
        assert persist_dispatch_outcome(
            db,
            DispatchOutcome(
                session_id="s",
                repo="o/r",
                issue_number=853,
                exit_code=1,
                stdout="",
                stderr="Invalid API key",
            ),
        )
        row = db.get_session_row("s")
        db.close()
        # The column that held `None` while the log line for the same
        # session recorded `exit_code 1`.
        assert row["claude_exit_code"] == 1
        assert "Invalid API key" in row["output_tail"]

    def test_secrets_in_the_output_are_not_stored(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.cli import persist_dispatch_outcome

        secret = "ghp_" + "z" * 36
        db = StateDB(tmp_path / "state.db")
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s", "dev", "o/r", "running", 0),
        )
        db.commit()
        persist_dispatch_outcome(
            db,
            DispatchOutcome(
                session_id="s",
                repo="o/r",
                issue_number=None,
                exit_code=1,
                stdout=f"token {secret}",
                stderr="",
            ),
        )
        tail = db.get_session_row("s")["output_tail"]
        db.close()
        assert secret not in tail
        assert "[redacted]" in tail

    def test_no_output_is_stored_as_null_not_empty_string(
        self, tmp_path: Path
    ) -> None:
        """"The agent wrote nothing before it died" is a real failure
        shape and should not read the same as retained output."""
        from ctrlrelay.cli import persist_dispatch_outcome

        db = StateDB(tmp_path / "state.db")
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s", "dev", "o/r", "running", 0),
        )
        db.commit()
        persist_dispatch_outcome(
            db,
            DispatchOutcome(
                session_id="s",
                repo="o/r",
                issue_number=None,
                exit_code=1,
                stdout="",
                stderr="",
            ),
        )
        row = db.get_session_row("s")
        db.close()
        assert row["output_tail"] is None
        assert row["claude_exit_code"] == 1

    def test_a_missing_row_is_reported_rather_than_passing(
        self, tmp_path: Path, caplog
    ) -> None:
        """An UPDATE matching nothing is a success as far as SQLite is
        concerned, which is the same silence the ticket is about."""
        import logging

        from ctrlrelay.cli import persist_dispatch_outcome

        db = StateDB(tmp_path / "state.db")
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            wrote = persist_dispatch_outcome(
                db,
                DispatchOutcome(
                    session_id="never-inserted",
                    repo="o/r",
                    issue_number=None,
                    exit_code=1,
                    stdout="",
                    stderr="x",
                ),
            )
        db.close()
        assert wrote is False
        assert any(
            r.getMessage() == "dispatch.outcome.row_missing"
            for r in caplog.records
        )

    def test_a_broken_state_db_does_not_raise(self, tmp_path: Path) -> None:
        """A failure to record diagnostics must not become the failure
        the operator sees instead of the real one."""
        from ctrlrelay.cli import persist_dispatch_outcome

        db = MagicMock()
        db.record_session_outcome.side_effect = RuntimeError("disk full")
        assert (
            persist_dispatch_outcome(
                db,
                DispatchOutcome(
                    session_id="s",
                    repo="o/r",
                    issue_number=None,
                    exit_code=1,
                    stdout="",
                    stderr="",
                ),
            )
            is False
        )
        # It TRIED to write. Asserting only the False passes for a
        # function that never touches the database.
        db.record_session_outcome.assert_called_once()


class SpyTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, **kwargs) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        return None


def _config(*, enabled: bool = True, threshold: int = 3):
    from ctrlrelay.core.config import AlertsConfig

    cfg = MagicMock()
    cfg.alerts = AlertsConfig(
        enabled=enabled, consecutive_dispatch_failures=threshold
    )
    return cfg


def _outcome(session_id: str, exit_code: int):
    return DispatchOutcome(
        session_id=session_id,
        repo="o/r",
        issue_number=None,
        exit_code=exit_code,
        stdout="",
        stderr="boom",
    )


class TestTheHookAlertsExactlyOnce:
    """The end-to-end proof, on a transport that records what it was
    given rather than a structural check that a `send` exists."""

    @pytest.mark.asyncio
    async def test_a_run_of_failures_sends_one_message(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.cli import make_dispatch_outcome_hook

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        hook = make_dispatch_outcome_hook(_config(threshold=3), db)
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=spy),
        ):
            for i in range(50):
                await hook(_outcome(f"s-{i}", 1))
        db.close()
        assert len(spy.sent) == 1
        assert "3 dispatches in a row" in spy.sent[0]

    @pytest.mark.asyncio
    async def test_a_healthy_relay_sends_nothing(self, tmp_path: Path) -> None:
        """The negative control. Without it, "always send" passes the
        test above."""
        from ctrlrelay.cli import make_dispatch_outcome_hook

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        hook = make_dispatch_outcome_hook(_config(threshold=3), db)
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=spy),
        ):
            for i in range(50):
                await hook(_outcome(f"s-{i}", 0))
        db.close()
        assert spy.sent == []

    @pytest.mark.asyncio
    async def test_recovery_then_a_second_outage_sends_a_second_message(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.cli import make_dispatch_outcome_hook

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        hook = make_dispatch_outcome_hook(_config(threshold=2), db)
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=spy),
        ):
            for i in range(3):
                await hook(_outcome(f"a-{i}", 1))
            await hook(_outcome("recovered", 0))
            for i in range(3):
                await hook(_outcome(f"b-{i}", 1))
        db.close()
        assert len(spy.sent) == 2

    @pytest.mark.asyncio
    async def test_disabled_alerting_sends_nothing_and_leaves_no_latch(
        self, tmp_path: Path
    ) -> None:
        """Not counted-but-suppressed.

        Counting while suppressing would let the one-alert latch close
        with nothing sent, so turning alerting back on would arm a
        guard that then stays quiet.
        """
        from ctrlrelay.cli import make_dispatch_outcome_hook

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        hook = make_dispatch_outcome_hook(_config(enabled=False), db)
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=spy),
        ):
            for i in range(10):
                await hook(_outcome(f"s-{i}", 1))
        row = db.get_dispatch_health()
        db.close()
        assert spy.sent == []
        assert row["alerted_at"] is None
        assert row["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_the_exit_code_is_recorded_even_with_alerting_off(
        self, tmp_path: Path
    ) -> None:
        """Recording is diagnostics, not a notification. It is the
        ticket's third defect and does not depend on the second."""
        from ctrlrelay.cli import make_dispatch_outcome_hook

        db = StateDB(tmp_path / "state.db")
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s-0", "dev", "o/r", "running", 0),
        )
        db.commit()
        hook = make_dispatch_outcome_hook(_config(enabled=False), db)
        await hook(_outcome("s-0", 1))
        row = db.get_session_row("s-0")
        db.close()
        assert row["claude_exit_code"] == 1
        assert "boom" in row["output_tail"]

    @pytest.mark.asyncio
    async def test_a_failed_delivery_does_not_silence_the_outage(
        self, tmp_path: Path
    ) -> None:
        """Nothing delivered means nothing latched.

        Measured before claim and delivery were separated: the channel
        absent for the first failure and healthy for the next fifty
        delivered **zero** messages, because the latch closed on the
        attempt. The claim is a lease now, so the attempt leaves no
        durable mark and a later failure retries once it expires.
        """
        from ctrlrelay.cli import make_dispatch_outcome_hook

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        hook = make_dispatch_outcome_hook(_config(threshold=1), db)
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=None),
        ):
            await hook(_outcome("s-0", 1))
        assert db.get_dispatch_health()["alert_delivered_at"] is None, (
            "an undelivered attempt latched the alert"
        )
        # The channel recovers. The lease has to expire first, which is
        # what bounds the retry rate — then exactly one message.
        with (
            patch(
                "ctrlrelay.cli.connect_notify_transport",
                new=AsyncMock(return_value=spy),
            ),
            patch(
                "ctrlrelay.core.state.time.time",
                return_value=time.time() + 120,
            ),
        ):
            for i in range(1, 51):
                await hook(_outcome(f"s-{i}", 1))
        db.close()
        assert len(spy.sent) == 1

    @pytest.mark.asyncio
    async def test_a_send_that_throws_leaves_no_delivery_recorded(
        self, tmp_path: Path
    ) -> None:
        """The other way delivery fails: a connected transport whose
        `send` raises — a bridge whose websocket is reconnecting."""
        from ctrlrelay.cli import make_dispatch_outcome_hook

        class Broken(SpyTransport):
            async def send(self, text: str, **kwargs) -> None:
                raise RuntimeError("websocket reconnecting")

        db = StateDB(tmp_path / "state.db")
        hook = make_dispatch_outcome_hook(_config(threshold=1), db)
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=Broken()),
        ):
            await hook(_outcome("s-0", 1))
        row = db.get_dispatch_health()
        assert row["alert_delivered_at"] is None
        # And the run is NOT reset — a cleared counter would restart
        # the countdown and delay the alert by another full threshold.
        assert db.dispatch_failure_count() == 1
        db.close()

    @pytest.mark.asyncio
    async def test_a_restart_during_delivery_does_not_silence_the_outage(
        self, tmp_path: Path
    ) -> None:
        """The cold review's BLOCKING finding, reproduced then fixed.

        The claim is committed before the send is awaited, and the send
        window is up to 15 s. During a wedged-bridge outage the poller
        is inside that window most of the time, so a `systemctl
        restart` — the first thing an operator does — almost certainly
        lands there.

        Measured on the pre-fix code: cancel mid-delivery, then 59
        further failures against a **healthy** channel delivered zero
        messages, for the whole remaining outage.
        """
        from ctrlrelay.cli import make_dispatch_outcome_hook

        class Stalls(SpyTransport):
            async def send(self, text: str, **kwargs) -> None:
                await asyncio.sleep(3600)

        path = tmp_path / "state.db"
        db = StateDB(path)
        hook = make_dispatch_outcome_hook(_config(threshold=1), db)
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=Stalls()),
        ):
            task = asyncio.ensure_future(hook(_outcome("s-0", 1)))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert db.get_dispatch_health()["alert_delivered_at"] is None
        db.close()

        # The process comes back, the bridge is healthy, the outage
        # continues. The claim from the killed attempt must not hold.
        db = StateDB(path)
        spy = SpyTransport()
        hook = make_dispatch_outcome_hook(_config(threshold=1), db)
        with (
            patch(
                "ctrlrelay.cli.connect_notify_transport",
                new=AsyncMock(return_value=spy),
            ),
            patch(
                "ctrlrelay.core.state.time.time",
                return_value=time.time() + 120,
            ),
        ):
            for i in range(1, 60):
                await hook(_outcome(f"s-{i}", 1))
        db.close()
        assert len(spy.sent) == 1, (
            "the outage stayed silent after a restart during delivery"
        )

    @pytest.mark.asyncio
    async def test_an_undeliverable_alert_is_logged(
        self, tmp_path: Path, caplog
    ) -> None:
        """No channel is not "nothing needed sending".

        The original fault's only symptom was an absent message, which
        is why it ran for a day unnoticed.
        """
        import logging

        from ctrlrelay.cli import make_dispatch_outcome_hook

        db = StateDB(tmp_path / "state.db")
        hook = make_dispatch_outcome_hook(_config(threshold=1), db)
        with (
            patch(
                "ctrlrelay.cli.connect_notify_transport",
                new=AsyncMock(return_value=None),
            ),
            caplog.at_level(logging.INFO, logger="ctrlrelay"),
        ):
            await hook(_outcome("s", 1))
        db.close()
        assert any(
            r.getMessage() == "dispatch_health.alert_undelivered"
            for r in caplog.records
        )


class TestSweepAllFailedAlert:
    @pytest.mark.asyncio
    async def test_a_fully_failed_sweep_sends_one_message(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.cli import alert_if_sweep_all_failed
        from ctrlrelay.pipelines.base import PipelineResult

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        results = [
            PipelineResult(success=False, session_id=f"s{i}", summary="failed")
            for i in range(88)
        ]
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=spy),
        ):
            sent = await alert_if_sweep_all_failed(_config(), db, results)
        db.close()
        assert sent is True
        assert len(spy.sent) == 1
        assert "every repo failed" in spy.sent[0]

    @pytest.mark.asyncio
    async def test_one_healthy_repo_is_enough_to_stay_quiet(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.cli import alert_if_sweep_all_failed
        from ctrlrelay.pipelines.base import PipelineResult

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        results = [
            PipelineResult(success=False, session_id=f"s{i}", summary="failed")
            for i in range(87)
        ] + [PipelineResult(success=True, session_id="ok", summary="done")]
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=spy),
        ):
            assert await alert_if_sweep_all_failed(_config(), db, results) is False
        db.close()
        assert spy.sent == []

    @pytest.mark.asyncio
    async def test_a_sweep_that_blocked_everywhere_is_not_an_outage(
        self, tmp_path: Path
    ) -> None:
        """The agent ran and asked a question, so the relay works.

        Counting BLOCKED as failure would alert on a healthy sweep that
        happened to need input on every repo.
        """
        from ctrlrelay.cli import alert_if_sweep_all_failed
        from ctrlrelay.pipelines.base import PipelineResult

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        results = [
            PipelineResult(
                success=False,
                blocked=True,
                session_id=f"s{i}",
                summary="Blocked on user input",
                question="?",
            )
            for i in range(5)
        ]
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=spy),
        ):
            assert await alert_if_sweep_all_failed(_config(), db, results) is False
        db.close()
        assert spy.sent == []

    @pytest.mark.asyncio
    async def test_an_empty_sweep_sends_nothing(self, tmp_path: Path) -> None:
        from ctrlrelay.cli import alert_if_sweep_all_failed

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=spy),
        ):
            assert await alert_if_sweep_all_failed(_config(), db, []) is False
        db.close()
        assert spy.sent == []


class TestCommunicateFailuresAreReported:
    @pytest.mark.asyncio
    async def test_an_oserror_reading_the_pipes_reports_an_outcome(
        self, tmp_path: Path
    ) -> None:
        """Only the spawn failure, the timeout and a normal exit used to
        report.

        An `OSError` out of `communicate()` propagated with no
        `DispatchOutcome`, so a run of those would have been this
        ticket's own defect in a narrower window: dispatches failing
        with nothing counting them and no exit code recorded.
        """
        seen: list[DispatchOutcome] = []

        async def hook(outcome):
            seen.append(outcome)

        proc = AsyncMock()
        proc.communicate.side_effect = OSError("read failed on the pipe")
        proc.returncode = None
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(OSError):
                await d.spawn_session(
                    session_id="s",
                    prompt="p",
                    working_dir=tmp_path,
                    state_file=tmp_path / "missing.json",
                )
        assert len(seen) == 1
        assert seen[0].ok is False
        assert "read failed on the pipe" in seen[0].stderr
        # The child is killed: we are no longer reading its output, and
        # leaving it running against the worktree is what the
        # cancellation arm exists to prevent.
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_cancellation_is_not_swallowed_by_that_arm(
        self, tmp_path: Path
    ) -> None:
        """The control: the broad `except Exception` must not take
        precedence over the cancellation handling above it."""
        proc = AsyncMock()
        proc.communicate.side_effect = asyncio.CancelledError()
        proc.returncode = None
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        seen: list[DispatchOutcome] = []

        async def hook(outcome):
            seen.append(outcome)

        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(asyncio.CancelledError):
                await d.spawn_session(
                    session_id="s",
                    prompt="p",
                    working_dir=tmp_path,
                    state_file=tmp_path / "missing.json",
                )
        # A shutdown is not a dispatch failure and must not count
        # toward the outage threshold.
        assert seen == []


class TestTheClaimIsALeaseAndOnlyDeliveryLatches:
    """Claiming the right to send and having sent are two facts.

    Latching on the claim meant a poller stopped mid-send — a
    `systemctl restart`, the first thing an operator does — came back
    with the latch held and nothing sent, so the rest of the outage was
    silent. Measured before the split: cancel during the send, then 59
    further failures against a healthy channel, zero messages.
    """

    def test_a_delivery_suppresses_the_next_claim(
        self, tmp_path: Path
    ) -> None:
        db = StateDB(tmp_path / "state.db")
        token = db.claim_dispatch_alert(lease_seconds=60)
        assert token is not None
        assert db.mark_dispatch_alert_delivered(token) is True
        assert db.claim_dispatch_alert(lease_seconds=60) is None
        db.close()

    def test_a_claim_with_no_delivery_expires(self, tmp_path: Path) -> None:
        """The whole point of a lease.

        A process killed between claiming and sending must not hold the
        latch forever — and the same mechanism bounds the retry rate
        after a delivery that merely failed.
        """
        db = StateDB(tmp_path / "state.db")
        first = db.claim_dispatch_alert(lease_seconds=60)
        assert first is not None
        # Still held inside the lease.
        assert db.claim_dispatch_alert(lease_seconds=60) is None
        # Expired once the lease is up.
        second = db.claim_dispatch_alert(lease_seconds=0 + 1) if False else None
        with patch(
            "ctrlrelay.core.state.time.time",
            return_value=__import__("time").time() + 120,
        ):
            second = db.claim_dispatch_alert(lease_seconds=60)
        assert second is not None
        assert second != first
        db.close()

    def test_an_expired_claim_cannot_record_a_delivery(
        self, tmp_path: Path
    ) -> None:
        """A process whose claim aged out must not mark a delivery
        against the newer owner's claim."""
        db = StateDB(tmp_path / "state.db")
        stale = db.claim_dispatch_alert(lease_seconds=60)
        with patch(
            "ctrlrelay.core.state.time.time",
            return_value=__import__("time").time() + 120,
        ):
            fresh = db.claim_dispatch_alert(lease_seconds=60)
        assert stale is not None and fresh is not None
        assert db.mark_dispatch_alert_delivered(stale) is False
        assert db.get_dispatch_health()["alert_delivered_at"] is None
        assert db.mark_dispatch_alert_delivered(fresh) is True
        db.close()

    def test_a_delivery_naming_no_claim_records_nothing_and_says_so(
        self, tmp_path: Path, caplog
    ) -> None:
        import logging

        db = StateDB(tmp_path / "state.db")
        db.claim_dispatch_alert(lease_seconds=60)
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            assert db.mark_dispatch_alert_delivered(None) is False
        assert db.get_dispatch_health()["alert_delivered_at"] is None
        assert any(
            r.getMessage() == "dispatch_health.delivery_without_claim"
            for r in caplog.records
        )
        db.close()

    def test_a_success_clears_the_delivery_record_too(
        self, tmp_path: Path
    ) -> None:
        """Otherwise the alert fires once per database: a delivered
        alert would suppress every later outage forever."""
        db = StateDB(tmp_path / "state.db")
        token = db.claim_dispatch_alert(lease_seconds=60)
        db.mark_dispatch_alert_delivered(token)
        db.reset_dispatch_failures()
        row = db.get_dispatch_health()
        assert row["alert_delivered_at"] is None
        assert row["alert_claim_token"] is None
        assert db.claim_dispatch_alert(lease_seconds=60) is not None
        db.close()

    def test_a_claim_with_no_timestamp_is_treated_as_expired(
        self, tmp_path: Path
    ) -> None:
        """"I cannot tell how old this is" must not read as "it is
        current" — that is the direction that goes silent."""
        db = StateDB(tmp_path / "state.db")
        db.claim_dispatch_alert(lease_seconds=60)
        db.execute("UPDATE dispatch_health SET alerted_at = NULL WHERE id = 1")
        db.commit()
        assert db.claim_dispatch_alert(lease_seconds=60) is not None
        db.close()

    def test_two_claims_in_the_same_second_are_distinguishable(
        self, tmp_path: Path
    ) -> None:
        """The token is a uuid, not the `alerted_at` second.

        **The clock is frozen.** Making two claims back to back and
        asserting they differ is a STREAK and not a mechanism: with a
        timestamp token it passes whenever the two calls straddle a
        second boundary, and a mutation sweep duly reported it
        surviving on one run and failing on another.
        """
        db = StateDB(tmp_path / "state.db")
        with patch(
            "ctrlrelay.core.state.time.time", return_value=1_792_364_533.0
        ):
            first = db.claim_dispatch_alert(lease_seconds=60)
            db.reset_dispatch_failures()
            second = db.claim_dispatch_alert(lease_seconds=60)
            row = db.get_dispatch_health()
        db.close()
        assert first is not None and second is not None
        assert first != second
        assert row["alerted_at"] == 1_792_364_533

    def test_the_per_failure_path_also_mints_a_unique_token(
        self, tmp_path: Path
    ) -> None:
        """`record_dispatch_failure` mints its own claim, so it needs
        the same property — and nothing else can observe it, because a
        timestamp token still records a delivery against itself. A
        mutation of that line survived until this test existed."""
        db = StateDB(tmp_path / "state.db")
        with patch(
            "ctrlrelay.core.state.time.time", return_value=1_792_364_533.0
        ):
            _, first = db.record_dispatch_failure(1, lease_seconds=60)
            db.reset_dispatch_failures()
            _, second = db.record_dispatch_failure(1, lease_seconds=60)
        db.close()
        assert first is not None and second is not None
        assert first != second

    def test_a_zero_lease_is_refused_loudly(self, tmp_path: Path) -> None:
        """Not silently coerced. A zero lease makes every claim
        instantly expired, so two processes could both attempt the same
        alert and a failed delivery would retry on every dispatch."""
        db = StateDB(tmp_path / "state.db")
        from ctrlrelay.core.dispatch_health import DispatchHealth

        with pytest.raises(ValueError):
            DispatchHealth(db, threshold=3, lease_seconds=0)
        db.close()


class TestAStalledSendCannotHangADispatch:
    @pytest.mark.asyncio
    async def test_the_send_is_bounded(self, tmp_path: Path) -> None:
        """The alert is awaited from inside the dispatcher, after the
        child has exited and while the pipeline holds the repo lock.

        An unbounded send on an accepted-then-stalled socket would
        stall the whole serial poll cycle to deliver a notification.
        """
        import ctrlrelay.cli as cli_module
        from ctrlrelay.cli import make_dispatch_outcome_hook

        entered: list[str] = []

        class Stalls(SpyTransport):
            async def send(self, text: str, **kwargs) -> None:
                entered.append(text)
                await asyncio.sleep(3600)

        db = StateDB(tmp_path / "state.db")
        hook = make_dispatch_outcome_hook(_config(threshold=1), db)
        with (
            patch.object(cli_module, "_NOTIFY_SEND_TIMEOUT_SECONDS", 0.05),
            patch(
                "ctrlrelay.cli.connect_notify_transport",
                new=AsyncMock(return_value=Stalls()),
            ),
        ):
            await asyncio.wait_for(hook(_outcome("s-0", 1)), timeout=10)
        # The send was ENTERED, not merely skipped. Without this the
        # test passes for an alert implementation that never tries at
        # all, because nothing is recorded either way.
        assert len(entered) == 1
        # Nothing delivered, so nothing latched: the claim expires and
        # a later failure retries.
        assert db.get_dispatch_health()["alert_delivered_at"] is None
        db.close()


class TestTheWriterIsTheChokepoint:
    def test_record_session_outcome_scrubs_what_it_is_given(
        self, tmp_path: Path
    ) -> None:
        """Defence in depth at the one place every writer goes through.

        `persist_dispatch_outcome` already scrubs, so this is the
        second pass — and it is the one that holds for a caller added
        later. Redaction is idempotent, so it costs nothing. The
        original docstring argued the opposite, that a sanitiser in
        the writer could be bypassed; it is the CALLERS that can be
        added without one.
        """
        secret = "ghp_" + "q" * 36
        db = StateDB(tmp_path / "state.db")
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s", "dev", "o/r", "running", 0),
        )
        db.commit()
        # Deliberately unscrubbed, as a future careless caller would.
        db.record_session_outcome(
            "s", exit_code=1, output_tail=f"raw {secret} straight in"
        )
        tail = db.get_session_row("s")["output_tail"]
        db.close()
        assert secret not in tail
        assert "[redacted]" in tail
        # And the surrounding text still reads.
        assert "straight in" in tail


class TestAFailedWriteDoesNotHoldTheLock:
    """A real `database is locked`, produced by a second connection
    holding the write lock — not a simulated one.

    `sqlite3.Connection.commit` cannot be monkeypatched, and that is
    the better outcome: this reproduces the condition the daemon
    actually meets, two processes on one file.
    """

    @staticmethod
    def _locked_pair(tmp_path: Path):
        path = tmp_path / "state.db"
        a = StateDB(path)
        a.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s", "dev", "o/r", "running", 0),
        )
        a.commit()
        # Fail fast instead of waiting out the default five seconds.
        a._conn.execute("PRAGMA busy_timeout = 50")
        blocker = StateDB(path)
        blocker._conn.execute("BEGIN IMMEDIATE")
        blocker._conn.execute(
            "UPDATE dispatch_health SET consecutive_failures = 99 WHERE id = 1"
        )
        return a, blocker

    def test_a_failing_update_rolls_back(self, tmp_path: Path) -> None:
        a, blocker = self._locked_pair(tmp_path)
        try:
            with pytest.raises(sqlite3.OperationalError) as excinfo:
                a.record_session_outcome("s", exit_code=1, output_tail="x")
            assert "locked" in str(excinfo.value)
            assert not a._conn.in_transaction, (
                "a failed write left the transaction open, holding a write "
                "lock on the shared database"
            )
        finally:
            blocker._conn.rollback()
            blocker.close()
            a.close()

    def test_the_swallowing_caller_still_leaves_no_transaction(
        self, tmp_path: Path
    ) -> None:
        """The path that actually runs in the daemon.

        `persist_dispatch_outcome` swallows the exception, so if the
        rollback were missing nothing else would ever close that
        transaction.
        """
        from ctrlrelay.cli import persist_dispatch_outcome

        a, blocker = self._locked_pair(tmp_path)
        try:
            assert (
                persist_dispatch_outcome(
                    a,
                    DispatchOutcome(
                        session_id="s",
                        repo="o/r",
                        issue_number=None,
                        exit_code=1,
                        stdout="",
                        stderr="x",
                    ),
                )
                is False
            )
            assert not a._conn.in_transaction
        finally:
            blocker._conn.rollback()
            blocker.close()
            a.close()

    def test_an_unblocked_write_still_succeeds(self, tmp_path: Path) -> None:
        """The control: the pair above must fail because of the lock,
        not because `record_session_outcome` is broken."""
        a, blocker = self._locked_pair(tmp_path)
        blocker._conn.rollback()
        blocker.close()
        try:
            assert a.record_session_outcome(
                "s", exit_code=1, output_tail="x"
            ) is True
            assert a.get_session_row("s")["claude_exit_code"] == 1
        finally:
            a.close()


class TestTheChildIsReapedWithoutLosingTheCause:
    @pytest.mark.asyncio
    async def test_a_child_that_exited_before_the_kill_is_tolerated(
        self, tmp_path: Path
    ) -> None:
        """The child can exit between the returncode check and the
        signal.

        A bare `ProcessLookupError` from `kill()` would REPLACE the
        original `communicate()` failure and skip the outcome report —
        losing both the cause and the count.
        """
        seen: list[DispatchOutcome] = []

        async def hook(outcome):
            seen.append(outcome)

        proc = AsyncMock()
        proc.communicate.side_effect = OSError("read failed on the pipe")
        proc.returncode = None
        proc.kill = MagicMock(side_effect=ProcessLookupError())
        proc.wait = AsyncMock()
        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(OSError) as excinfo:
                await d.spawn_session(
                    session_id="s",
                    prompt="p",
                    working_dir=tmp_path,
                    state_file=tmp_path / "missing.json",
                )
        # The ORIGINAL cause survives, not the reaping error.
        assert "read failed on the pipe" in str(excinfo.value)
        assert len(seen) == 1
        assert "read failed on the pipe" in seen[0].stderr


class TestInvalidUtf8FromAChildIsNotLost:
    @pytest.mark.asyncio
    async def test_undecodable_output_still_reports_an_outcome(
        self, tmp_path: Path
    ) -> None:
        """A `UnicodeDecodeError` landed after the guarded
        `communicate()` and before the outcome report, so that dispatch
        was neither persisted nor counted."""
        seen: list[DispatchOutcome] = []

        async def hook(outcome):
            seen.append(outcome)

        proc = AsyncMock()
        proc.communicate.return_value = (b"\xff\xfe not utf-8", b"\x80bad")
        proc.returncode = 1
        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await d.spawn_session(
                session_id="s",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "missing.json",
            )
        assert result.exit_code == 1
        assert len(seen) == 1
        assert seen[0].exit_code == 1
        # The readable part of the output survives.
        assert "not utf-8" in seen[0].stdout
        assert "bad" in seen[0].stderr


class TestTheSweepAndTheCounterAgreeOnWhatFailedMeans:
    """Two definitions of "failed" sharing one latch produced a message
    that was false of its own input and repeated every sweep.

    The sweep counted PIPELINE failures, which include an agent that
    exits 0 and writes a FAILED checkpoint (#174). The counter counts a
    non-zero exit. A sweep of exit-0 FAILED results would have sent
    "every repo failed... check the agent credential... this will not
    be repeated until a dispatch succeeds" — and every dispatch DID
    succeed by the counter's definition, so the next sweep would clear
    the latch and send it again.
    """

    @staticmethod
    def _result(*, success=False, blocked=False, exit_code=None, n=0):
        from ctrlrelay.pipelines.base import PipelineResult

        return PipelineResult(
            success=success,
            blocked=blocked,
            session_id=f"s{n}",
            summary="x",
            exit_code=exit_code,
        )

    @pytest.mark.asyncio
    async def test_a_sweep_of_exit_zero_failures_does_not_alert(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.cli import alert_if_sweep_all_failed

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        results = [self._result(exit_code=0, n=i) for i in range(88)]
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=spy),
        ):
            assert (
                await alert_if_sweep_all_failed(_config(), db, results) is False
            )
        db.close()
        assert spy.sent == []

    @pytest.mark.asyncio
    async def test_a_sweep_of_nonzero_exits_does_alert(
        self, tmp_path: Path
    ) -> None:
        """The control. Without it, "never alert on a sweep" passes the
        test above."""
        from ctrlrelay.cli import alert_if_sweep_all_failed

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        results = [self._result(exit_code=1, n=i) for i in range(88)]
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=spy),
        ):
            assert await alert_if_sweep_all_failed(_config(), db, results) is True
        db.close()
        assert len(spy.sent) == 1

    @pytest.mark.asyncio
    async def test_an_absent_exit_code_counts_as_a_dispatch_failure(
        self, tmp_path: Path
    ) -> None:
        """The child never reported one — the binary-missing and
        pipe-error case, not a healthy run."""
        from ctrlrelay.cli import alert_if_sweep_all_failed

        db = StateDB(tmp_path / "state.db")
        spy = SpyTransport()
        results = [self._result(exit_code=None, n=i) for i in range(5)]
        with patch(
            "ctrlrelay.cli.connect_notify_transport",
            new=AsyncMock(return_value=spy),
        ):
            assert await alert_if_sweep_all_failed(_config(), db, results) is True
        db.close()
        assert len(spy.sent) == 1

    @pytest.mark.asyncio
    async def test_the_sweep_alert_cannot_cost_the_per_repo_messages(
        self, tmp_path: Path
    ) -> None:
        """The sweep calls this before its per-repo fan-out, and that
        fan-out wraps its whole loop in one `except`. An exception
        escaping here would skip every per-repo notification."""
        from ctrlrelay.cli import alert_if_sweep_all_failed_guarded

        db = StateDB(tmp_path / "state.db")
        with patch(
            "ctrlrelay.cli.alert_if_sweep_all_failed",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            assert (
                await alert_if_sweep_all_failed_guarded(_config(), db, [])
                is False
            )
        db.close()

    @pytest.mark.asyncio
    async def test_a_cancellation_through_the_guard_still_propagates(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.cli import alert_if_sweep_all_failed_guarded

        db = StateDB(tmp_path / "state.db")
        with patch(
            "ctrlrelay.cli.alert_if_sweep_all_failed",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with pytest.raises(asyncio.CancelledError):
                await alert_if_sweep_all_failed_guarded(_config(), db, [])
        db.close()


class TestAnUnreadExitStatusIsNotReportedAsHealthy:
    @pytest.mark.asyncio
    async def test_a_none_returncode_is_reported_as_none(
        self, tmp_path: Path
    ) -> None:
        """`proc.returncode or 0` collapses "not obtained" into 0.

        `DispatchOutcome.ok` is `exit_code == 0`, so that collapse
        reports a dispatch whose status nobody read as a healthy one —
        resetting the failure run and clearing the alert. It is the
        "I could not see" becoming "there was nothing to see" mistake,
        in the one expression the whole alert rests on.
        """
        seen: list[DispatchOutcome] = []

        async def hook(outcome):
            seen.append(outcome)

        proc = AsyncMock()
        proc.communicate.return_value = (b"", b"")
        proc.returncode = None
        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await d.spawn_session(
                session_id="s",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "missing.json",
            )
        assert len(seen) == 1
        assert seen[0].exit_code is None
        assert seen[0].ok is False, (
            "a dispatch whose exit status was never obtained counted as "
            "healthy and would have cleared the alert"
        )

    @pytest.mark.asyncio
    async def test_a_real_zero_is_still_healthy(self, tmp_path: Path) -> None:
        """The control: distinguishing None from 0 must not make a
        genuine clean exit look like a failure."""
        seen: list[DispatchOutcome] = []

        async def hook(outcome):
            seen.append(outcome)

        proc = AsyncMock()
        proc.communicate.return_value = (b"", b"")
        proc.returncode = 0
        d = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await d.spawn_session(
                session_id="s",
                prompt="p",
                working_dir=tmp_path,
                state_file=tmp_path / "missing.json",
            )
        assert seen[0].exit_code == 0
        assert seen[0].ok is True


class TestTheOutcomeWriterJoinsACallersTransaction:
    def test_it_does_not_publish_a_callers_pending_write(
        self, tmp_path: Path
    ) -> None:
        """The outcome hook runs mid-pipeline on the pipeline's own
        connection, so a caller with an open transaction is real and
        not hypothetical.

        `_begin_immediate` was corrected for this and the writers
        beside it committed unconditionally, reintroducing it. A
        mutation of the flag survived until this test existed, because
        the only coverage drove `record_dispatch_failure` instead.
        """
        db = StateDB(tmp_path / "state.db")
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("target", "dev", "o/r", "running", 0),
        )
        db.commit()
        # A caller mid-write, as `StateDB.execute` leaves it.
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("callers-pending-row", "dev", "o/r", "running", 0),
        )
        assert db._conn.in_transaction

        db.record_session_outcome("target", exit_code=1, output_tail="x")

        # Still the caller's to commit: not published, not discarded.
        assert db._conn.in_transaction, (
            "the outcome writer committed a transaction it only joined"
        )
        db._conn.rollback()
        assert db.get_session_row("callers-pending-row") is None
        # And our own write went with it, which is the accepted cost:
        # losing an exit code to a caller's rollback is recoverable,
        # publishing their partial write is not.
        assert db.get_session_row("target")["claude_exit_code"] is None
        db.close()

    def test_it_commits_its_own_transaction(self, tmp_path: Path) -> None:
        """The control: with no caller transaction open, the write is
        ours and is published."""
        db = StateDB(tmp_path / "state.db")
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("target", "dev", "o/r", "running", 0),
        )
        db.commit()
        assert not db._conn.in_transaction
        assert db.record_session_outcome(
            "target", exit_code=1, output_tail="x"
        ) is True
        assert not db._conn.in_transaction
        db.close()
        reopened = StateDB(tmp_path / "state.db")
        assert reopened.get_session_row("target")["claude_exit_code"] == 1
        reopened.close()
