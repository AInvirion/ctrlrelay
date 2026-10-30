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
    tasks += list(getattr(cli_module, "_RELAY_ALERT_SENDS", {}))
    for t in tasks:
        t.cancel()
    if tasks:
        # Bounded: a send that ignores cancellation must not hang the
        # cleanup. The test that builds one releases it first.
        await asyncio.wait(tasks, timeout=5)


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
        Shutdown cancels it and nothing is recorded. Once the lease has
        expired, a later failure on a healthy channel is delivered and
        recorded. (The later failures all run at one frozen instant, so
        the single message there comes from the new claim's lease, not
        from the delivery latch; the latch is proven in
        `test_a_run_of_failures_sends_one_message`.)
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
    async def test_an_unbounded_step_is_caught_by_the_backstop_deadline(
        self, tmp_path: Path, caplog
    ) -> None:
        """The overall deadline, for a step with no bound of its own.

        `connect_notify_transport` is replaced by one that never returns
        and has no timeout, so the only thing that can end this is the
        deadline around the whole delivery.
        """
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
        """The deadline plus the cancel grace is the longest a delivery
        can take to give up. Longer than the lease, and a slow delivery
        could still be running when its claim expires."""
        from ctrlrelay.core.dispatch_health import DEFAULT_ALERT_LEASE_SECONDS

        assert (
            cli_module._relay_alert_deadline_seconds()
            + cli_module._RELAY_ALERT_CANCEL_GRACE_SECONDS
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


class IgnoresCancel:
    """A send that swallows its cancellation and keeps waiting.

    `asyncio.wait_for` waits for the coroutine it cancelled, so a bound
    built on it is no bound against this. ``release`` is the test's own
    way out, because nothing else can stop it.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def send(self, text: str, **kwargs) -> None:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
        finally:
            self.active -= 1

    async def close(self) -> None:
        return None


class TestASendThatIgnoresCancellation:
    @pytest.mark.asyncio
    async def test_it_is_bounded_never_overlapped_and_cannot_hang_shutdown(
        self, tmp_path: Path, caplog
    ) -> None:
        db = StateDB(tmp_path / "state.db")
        hook = cli_module.make_dispatch_outcome_hook(_config(threshold=1), db)
        stuck = IgnoresCancel()
        try:
            with (
                patch.object(cli_module, "_NOTIFY_CONNECT_TIMEOUT_SECONDS", 0),
                patch.object(cli_module, "_NOTIFY_SEND_TIMEOUT_SECONDS", 0.1),
                patch(
                    "ctrlrelay.cli.connect_notify_transport",
                    new=AsyncMock(return_value=stuck),
                ),
                caplog.at_level(logging.INFO, logger="ctrlrelay"),
            ):
                started = time.monotonic()
                await hook(_outcome("s-0"))
                assert time.monotonic() - started < 0.5
                await asyncio.wait_for(stuck.entered.wait(), timeout=5)
                outer = list(cli_module._RELAY_ALERT_TASKS)
                assert len(outer) == 1

                # The delivery gives up at its deadline even though the
                # send it cancelled is still running.
                done, _ = await asyncio.wait(outer, timeout=10)
                assert done == set(outer), (
                    "the delivery waited for a send that ignores cancellation"
                )
                assert outer[0].result() is False
                assert stuck.active == 1
                # Its own event, and not reported as a timeout: the send
                # has not ended, so nothing has said how it ended.
                ends = [
                    r.getMessage()
                    for r in caplog.records
                    if r.getMessage().startswith("dispatch_health.alert_")
                ]
                assert ends == ["dispatch_health.alert_send_stuck"], ends

                # The lease expires and the outage continues. No second
                # send may start beside the one that is still alive.
                with patch(
                    "ctrlrelay.core.state.time.time",
                    return_value=time.time() + 120,
                ):
                    for i in range(1, 5):
                        await hook(_outcome(f"s-{i}"))
                        await asyncio.sleep(0.05)
                assert stuck.calls == 1
                assert stuck.max_active == 1
                assert any(
                    r.getMessage() == "dispatch_health.alert_already_in_flight"
                    for r in caplog.records
                )

                # Shutdown returns within its bound and says what it left.
                started = time.monotonic()
                await asyncio.wait_for(
                    cli_module.drain_relay_alerts(0.1), timeout=10
                )
                took = time.monotonic() - started
            assert took < 3.0, f"shutdown waited {took:.2f}s"
            stuck_lines = [
                r
                for r in caplog.records
                if r.getMessage() == "dispatch_health.alert_stuck_at_shutdown"
            ]
            assert len(stuck_lines) == 1
            # One delivery, counted once. Pins current behaviour: the
            # outer task ends within a tick of its cancel, so only the
            # send is still alive when the count is taken.
            assert stuck_lines[0].stuck == 1
        finally:
            stuck.release.set()
            await asyncio.sleep(0)
            await _cancel_leftovers()
            sends = list(getattr(cli_module, "_RELAY_ALERT_SENDS", {}))
            if sends:
                await asyncio.wait(sends, timeout=5)
            db.close()


class TestAShutdownThatIsItselfCancelled:
    @pytest.mark.asyncio
    async def test_the_cancelled_deliveries_are_still_reported(
        self, tmp_path: Path, caplog
    ) -> None:
        """A second stop during the grace must not skip the log lines
        that say the alert was not delivered."""
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
                drain = asyncio.ensure_future(
                    cli_module.drain_relay_alerts(30)
                )
                await asyncio.sleep(0.05)
                drain.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(drain, timeout=10)
                # Read at once: no extra turn of the loop for the
                # cancelled deliveries to report themselves late.
                names = [r.getMessage() for r in caplog.records]
            assert "dispatch_health.alert_cancelled" in names
            assert cli_module._RELAY_ALERT_TASKS == {}
        finally:
            await _cancel_leftovers()
            db.close()


class TestEachEndIsLoggedOnceAndTruly:
    @pytest.mark.asyncio
    async def test_a_delivery_that_raises_is_logged_once(
        self, tmp_path: Path, caplog
    ) -> None:
        async def raises(config, state_db, alert):
            raise RuntimeError("escaped every guard")

        from ctrlrelay.core.dispatch_health import DispatchAlert

        alert = DispatchAlert(
            reason="consecutive_failures",
            consecutive=1,
            threshold=1,
            detail="d",
            claim_token="t",
        )
        with (
            patch.object(cli_module, "deliver_relay_alert", new=raises),
            caplog.at_level(logging.INFO, logger="ctrlrelay"),
        ):
            task = cli_module.start_relay_alert(MagicMock(), MagicMock(), alert)
            await asyncio.wait([task], timeout=5)
            await asyncio.sleep(0)
        ends = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("dispatch_health.alert_")
        ]
        assert ends == ["dispatch_health.alert_task_failed"], ends

    @pytest.mark.asyncio
    async def test_a_recorded_delivery_is_not_reported_as_cancelled(
        self, tmp_path: Path, caplog
    ) -> None:
        """The outer task cancelled in the same tick the send records:
        the message went out, so the log must not say otherwise."""
        from ctrlrelay.core.dispatch_health import DispatchAlert

        recorded: list[bool] = []
        go = asyncio.Event()

        async def delivers(config, state_db, alert):
            await go.wait()
            recorded.append(True)
            return True

        alert = DispatchAlert(
            reason="consecutive_failures",
            consecutive=1,
            threshold=1,
            detail="d",
            claim_token="t",
        )
        with (
            patch.object(cli_module, "deliver_relay_alert", new=delivers),
            caplog.at_level(logging.INFO, logger="ctrlrelay"),
        ):
            outer = cli_module.start_relay_alert(MagicMock(), MagicMock(), alert)
            await asyncio.sleep(0)
            go.set()
            for _ in range(100):
                if recorded:
                    break
                await asyncio.sleep(0)
            assert recorded
            # The send has recorded and returned; the outer task has not
            # yet woken up to read it. Cancel it there.
            assert not outer.done()
            outer.cancel()
            await asyncio.wait([outer], timeout=5)
            await asyncio.sleep(0)
        names = [r.getMessage() for r in caplog.records]
        assert "dispatch_health.alert_cancelled" not in names, names

    @pytest.mark.asyncio
    async def test_a_second_stop_during_the_cancel_wait_still_reports_stuck(
        self, tmp_path: Path, caplog
    ) -> None:
        db = StateDB(tmp_path / "state.db")
        hook = cli_module.make_dispatch_outcome_hook(_config(threshold=1), db)
        stuck = IgnoresCancel()
        try:
            with (
                patch(
                    "ctrlrelay.cli.connect_notify_transport",
                    new=AsyncMock(return_value=stuck),
                ),
                caplog.at_level(logging.INFO, logger="ctrlrelay"),
            ):
                await hook(_outcome("s-0"))
                await asyncio.wait_for(stuck.entered.wait(), timeout=5)
                # Grace 0, so the drain goes straight into the 1 s
                # cancel wait; the second stop lands inside it.
                drain = asyncio.ensure_future(cli_module.drain_relay_alerts(0))
                await asyncio.sleep(0.2)
                assert not drain.done()
                drain.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(drain, timeout=10)
                names = [r.getMessage() for r in caplog.records]
            assert "dispatch_health.alert_stuck_at_shutdown" in names
        finally:
            stuck.release.set()
            await asyncio.sleep(0)
            await _cancel_leftovers()
            db.close()


class TestATimedOutDeliveryIsLoggedAsATimeout:
    @pytest.mark.asyncio
    async def test_the_deadline_path_logs_one_ending(
        self, tmp_path: Path, caplog
    ) -> None:
        """A send cancelled at the overall deadline ended because of the
        deadline, so it is logged as a timeout and not also as a
        cancellation. ``alert_interrupted`` is the delivery's own line,
        written by `deliver_relay_alert` whenever it is cancelled; it is
        shared with the whole-sweep path and says nothing about why."""
        from ctrlrelay.core.dispatch_health import DispatchAlert

        async def never_finishes(config, state_db, alert):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cli_module._log_quietly("dispatch_health.alert_interrupted")
                raise

        alert = DispatchAlert(
            reason="consecutive_failures",
            consecutive=1,
            threshold=1,
            detail="d",
            claim_token="t",
        )
        with (
            patch.object(cli_module, "deliver_relay_alert", new=never_finishes),
            patch.object(cli_module, "_NOTIFY_CONNECT_TIMEOUT_SECONDS", 0),
            patch.object(cli_module, "_NOTIFY_SEND_TIMEOUT_SECONDS", 0),
            caplog.at_level(logging.INFO, logger="ctrlrelay"),
        ):
            task = cli_module.start_relay_alert(MagicMock(), MagicMock(), alert)
            done, _ = await asyncio.wait([task], timeout=10)
            assert done == {task}
            assert task.result() is False
            await asyncio.sleep(0)
        ends = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("dispatch_health.alert_")
        ]
        assert ends == [
            "dispatch_health.alert_interrupted",
            "dispatch_health.alert_timed_out",
        ], ends
