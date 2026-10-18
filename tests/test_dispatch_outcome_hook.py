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
