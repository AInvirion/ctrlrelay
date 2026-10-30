"""The relay-health alert does not hold the repo lock (#208).

The dispatch hook runs after the child has exited and while the
pipeline still holds the per-repository lock. It used to await the
alert's delivery there, so a bridge that accepted the connection and
never answered held the lock for the full send bound on every failing
dispatch. These tests drive the real dispatcher and the real hook, and
time the lock rather than the alert.

The "wedged transport" is a connected transport whose ``send`` never
returns. A real unix socket that accepts and never reads does not
reproduce it: a one-line message fits in the kernel buffer, so the
write completes. What stalls in production is the await, and the fake
holds exactly that.
"""

import asyncio
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ctrlrelay.cli as cli_module
from ctrlrelay.core.dispatcher import ClaudeDispatcher, DispatchOutcome
from ctrlrelay.core.state import StateDB


class Wedged:
    """Connected, accepts the send, never answers."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def send(self, text: str, **kwargs) -> None:
        self.entered.set()
        await asyncio.sleep(3600)

    async def close(self) -> None:
        return None


class Spy:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, **kwargs) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        return None


def _config(threshold: int = 1):
    from ctrlrelay.core.config import AlertsConfig

    cfg = MagicMock()
    cfg.alerts = AlertsConfig(
        enabled=True, consecutive_dispatch_failures=threshold
    )
    return cfg


def _outcome(session_id: str, exit_code: int = 1) -> DispatchOutcome:
    return DispatchOutcome(
        session_id=session_id,
        repo="o/r",
        issue_number=None,
        exit_code=exit_code,
        stdout="",
        stderr="boom",
    )


def _failing_proc():
    proc = AsyncMock()
    proc.communicate.return_value = (b"", b"Invalid API key")
    proc.returncode = 1
    return proc


async def _cancel_leftovers() -> None:
    # `getattr` so this cleanup also runs on the pre-fix code, where the
    # registry does not exist and the test must fail on its assertion
    # rather than on an import.
    tasks = list(getattr(cli_module, "_RELAY_ALERT_TASKS", {}))
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class TestAWedgedBridgeDoesNotHoldTheLock:
    @pytest.mark.asyncio
    async def test_the_lock_is_released_while_the_send_is_still_stuck(
        self, tmp_path: Path
    ) -> None:
        """Through the real dispatcher, at the default bounds.

        On the pre-fix code this holds the lock for the full 5 s send
        bound. The bounds are NOT shrunk here: shrinking them would make
        the pre-fix code fast too, and the test could not tell the two
        apart.
        """
        db = StateDB(tmp_path / "state.db")
        hook = cli_module.make_dispatch_outcome_hook(_config(threshold=1), db)
        dispatcher = ClaudeDispatcher(claude_binary="claude", outcome_hook=hook)
        wedged = Wedged()
        repo_lock = asyncio.Lock()
        try:
            with (
                patch(
                    "asyncio.create_subprocess_exec",
                    return_value=_failing_proc(),
                ),
                patch(
                    "ctrlrelay.cli.connect_notify_transport",
                    new=AsyncMock(return_value=wedged),
                ),
            ):
                started = time.monotonic()

                async def _under_lock() -> None:
                    async with repo_lock:
                        await dispatcher.spawn_session(
                            session_id="s-0",
                            prompt="p",
                            working_dir=tmp_path,
                            state_file=tmp_path / "missing.json",
                            repo="o/r",
                        )

                await asyncio.wait_for(_under_lock(), timeout=30)
                held = time.monotonic() - started

                # The alert was attempted, not skipped. Without this the
                # timing passes for a hook that never sends at all.
                await asyncio.wait_for(wedged.entered.wait(), timeout=5)
                assert not repo_lock.locked()
                assert held < 1.0, (
                    f"the repo lock was held {held:.2f}s by an alert send "
                    "that never returns"
                )
                # And it is still in flight, tracked, not lost.
                assert any(
                    not t.done() for t in cli_module._RELAY_ALERT_TASKS
                )
        finally:
            await _cancel_leftovers()
            db.close()


class TestAnUnobservedDeliveryDoesNotSuppressTheNextAlert:
    @pytest.mark.asyncio
    async def test_a_cancelled_background_send_leaves_the_next_alert_armed(
        self, tmp_path: Path
    ) -> None:
        """The issue's claim, asserted rather than assumed.

        A detached send whose outcome nobody observes must not latch.
        Shutdown cancels it, nothing is recorded, the lease expires, and
        the outage is announced exactly once on a healthy channel.
        """
        db = StateDB(tmp_path / "state.db")
        hook = cli_module.make_dispatch_outcome_hook(_config(threshold=1), db)
        wedged = Wedged()
        try:
            with patch(
                "ctrlrelay.cli.connect_notify_transport",
                new=AsyncMock(return_value=wedged),
            ):
                await hook(_outcome("s-0"))
                await asyncio.wait_for(wedged.entered.wait(), timeout=5)
                await cli_module.drain_relay_alerts(0)
            assert cli_module._RELAY_ALERT_TASKS == {}
            assert db.get_dispatch_health()["alert_delivered_at"] is None, (
                "a send nobody saw finish latched the alert"
            )

            spy = Spy()
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
                    await hook(_outcome(f"s-{i}"))
                    # Let each delivery run, as a later dispatch would.
                    await asyncio.sleep(0)
                await cli_module.drain_relay_alerts(5)
            assert len(spy.sent) == 1
            assert db.get_dispatch_health()["alert_delivered_at"] is not None
        finally:
            await _cancel_leftovers()
            db.close()


class TestTheBackgroundDeliveryIsBoundedAndVisible:
    @pytest.mark.asyncio
    async def test_a_stalled_connect_hits_the_overall_deadline(
        self, tmp_path: Path, caplog
    ) -> None:
        """The backstop for a step the inner bounds do not cover."""
        db = StateDB(tmp_path / "state.db")
        hook = cli_module.make_dispatch_outcome_hook(_config(threshold=1), db)

        async def never_connects(_config):
            await asyncio.sleep(3600)

        try:
            with (
                patch.object(cli_module, "_NOTIFY_CONNECT_TIMEOUT_SECONDS", 0),
                patch.object(cli_module, "_NOTIFY_SEND_TIMEOUT_SECONDS", 0.2),
                patch(
                    "ctrlrelay.cli.connect_notify_transport",
                    new=never_connects,
                ),
                caplog.at_level(logging.INFO, logger="ctrlrelay"),
            ):
                await hook(_outcome("s-0"))
                tasks = list(cli_module._RELAY_ALERT_TASKS)
                assert len(tasks) == 1
                done, _ = await asyncio.wait(tasks, timeout=10)
                assert done == set(tasks), "the delivery outlived its deadline"
                assert tasks[0].result() is False
            assert cli_module._RELAY_ALERT_TASKS == {}
            assert any(
                r.getMessage() == "dispatch_health.alert_timed_out"
                for r in caplog.records
            )
            assert db.get_dispatch_health()["alert_delivered_at"] is None
        finally:
            await _cancel_leftovers()
            db.close()

    def test_the_deadline_is_shorter_than_the_lease(self) -> None:
        """Otherwise a slow delivery could still be running when its
        claim expires, and a second one would start beside it."""
        from ctrlrelay.core.dispatch_health import DEFAULT_ALERT_LEASE_SECONDS

        assert (
            cli_module._relay_alert_deadline_seconds()
            < DEFAULT_ALERT_LEASE_SECONDS
        )


class TestShutdown:
    @pytest.mark.asyncio
    async def test_a_wedged_delivery_is_cancelled_and_reported(
        self, tmp_path: Path, caplog
    ) -> None:
        db = StateDB(tmp_path / "state.db")
        hook = cli_module.make_dispatch_outcome_hook(_config(threshold=1), db)
        wedged = Wedged()
        try:
            with (
                patch(
                    "ctrlrelay.cli.connect_notify_transport",
                    new=AsyncMock(return_value=wedged),
                ),
                caplog.at_level(logging.INFO, logger="ctrlrelay"),
            ):
                await hook(_outcome("s-0"))
                await asyncio.wait_for(wedged.entered.wait(), timeout=5)
                started = time.monotonic()
                await asyncio.wait_for(
                    cli_module.drain_relay_alerts(0.1), timeout=10
                )
                took = time.monotonic() - started
            assert took < 2.0, f"shutdown waited {took:.2f}s on a wedged send"
            assert cli_module._RELAY_ALERT_TASKS == {}
            assert any(
                r.getMessage() == "dispatch_health.alert_cancelled"
                for r in caplog.records
            )
        finally:
            await _cancel_leftovers()
            db.close()

    @pytest.mark.asyncio
    async def test_a_delivery_that_finishes_inside_the_grace_is_kept(
        self, tmp_path: Path
    ) -> None:
        """The negative control: a drain that cancels everything passes
        the test above and drops a message that was nearly out."""

        class Slowish(Spy):
            async def send(self, text: str, **kwargs) -> None:
                await asyncio.sleep(0.05)
                self.sent.append(text)

        db = StateDB(tmp_path / "state.db")
        hook = cli_module.make_dispatch_outcome_hook(_config(threshold=1), db)
        slowish = Slowish()
        try:
            with patch(
                "ctrlrelay.cli.connect_notify_transport",
                new=AsyncMock(return_value=slowish),
            ):
                await hook(_outcome("s-0"))
                await cli_module.drain_relay_alerts(5)
            assert len(slowish.sent) == 1
            assert db.get_dispatch_health()["alert_delivered_at"] is not None
        finally:
            await _cancel_leftovers()
            db.close()
