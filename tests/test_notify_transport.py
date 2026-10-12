"""When the poller decides it has a notification channel (#198).

The defect: `poller_start` built the transport ONCE, at startup, behind
a single `socket_path.exists()`. Lose that instant to the bridge and
`transport` stayed `None` for the life of the process, so every
`handle_issue` announcement — new issue, PR ready, blocked, failed —
was skipped until somebody restarted the daemon.

Measured on alkaturbo, 2026-10-11. The poller logged
`mattermost socket not found` at 20:47:59 and the bridge process
started at 20:48:02; the dev lane then said nothing for 24 hours, and
`bridge.log` carries no `SEND delivered` between 12:02:59 and
21:03:31. The 21:03:31 line is the merge notice, which delivered
normally because the watch path builds its transport lazily — that
contrast is why this is a fault in one eager check and not in the
transport, the credentials or Mattermost.

Unit ordering is not the alternative. `ctrlrelay-poller.service`
already declares `After=ctrlrelay-bridge.service` and it did not order
them; the bridge is `Type=simple`, so systemd calls it started when
the process forks, a second before the socket exists.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
from pathlib import Path

import pytest
import yaml


@pytest.fixture(autouse=True)
def _no_pending_closes_between_tests():
    """Keep the module-global close set out of the arithmetic.

    Eight cases here read it as `after - before` or assert a ceiling on
    it, and a real close task left pending when pytest-asyncio closes a
    per-test loop would otherwise sit in it for the rest of the
    session.
    """
    import ctrlrelay.cli as cli_module

    cli_module._NOTIFY_CLOSE_TASKS.clear()
    yield
    for task in list(cli_module._NOTIFY_CLOSE_TASKS):
        task.cancel()
    cli_module._NOTIFY_CLOSE_TASKS.clear()


def _config(tmp_path: Path, transport: dict) -> object:
    from ctrlrelay.core.config import load_config

    data = {
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
        "agent": {"binary": "agent-bin", "default_timeout_seconds": 1800},
        "transport": transport,
        "dashboard": {"enabled": False},
        "repos": [],
    }
    p = tmp_path / "orchestrator.yaml"
    p.write_text(yaml.dump(data))
    return load_config(p)


def _mattermost(socket_path: Path, *, ask_timeout: int = 321) -> dict:
    return {
        "type": "mattermost",
        "mattermost": {
            "socket_path": str(socket_path),
            "url": "https://chat.example.com",
            "token_env": "MM_TOKEN",
            "channel_id": "a" * 26,
            "ask_timeout_seconds": ask_timeout,
        },
    }


def _literal_text(node) -> str | None:
    """The text of a string literal, including implicit concatenation."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_text(node.left)
        right = _literal_text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _pending_closes() -> list:
    import ctrlrelay.cli as cli_module

    return [t for t in cli_module._NOTIFY_CLOSE_TASKS if not t.done()]


async def _settle(predicate, *, tries: int = 100) -> None:
    """Give the loop turns until ``predicate`` holds, or run out.

    The close is scheduled rather than awaited, so its effect lands on
    a later turn. This does not assert — the caller does, so a failure
    reads as the missing effect rather than as a timeout.
    """
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)


def _ctrlrelay_events(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("ctrlrelay")]


def _event_names(caplog) -> list[str]:
    return [r.getMessage() for r in _ctrlrelay_events(caplog)]


class TestTheBootRace:
    """The one claim this ticket is about."""

    @pytest.mark.asyncio
    async def test_a_socket_that_appears_later_is_picked_up(
        self, tmp_path: Path, caplog
    ) -> None:
        """A poller that boots before the bridge still announces.

        Both halves matter and neither is sufficient alone. "Returns
        None when the socket is absent" would pass for a function that
        can only ever return None, which is the defect. "Returns a
        transport when the socket is there" would pass for the eager
        version too, as long as the test never moved the socket. So the
        discriminating case is ONE config object asked TWICE with the
        world changing in between — no restart, no re-read.
        """
        from ctrlrelay.cli import connect_notify_transport

        sock = tmp_path / "mm.sock"
        config = _config(tmp_path, _mattermost(sock))

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            before = await connect_notify_transport(config)
        assert before is None, "a missing socket is not a channel"
        assert "notify.transport.socket_missing" in _event_names(caplog)

        # The stand-in bridge READS, because "a transport was returned"
        # is not the claim this test makes. The claim is that the
        # announcement reaches the far side, so the far side has to be
        # able to say it arrived.
        received: list[bytes] = []
        arrived = asyncio.Event()

        async def _accept(reader, writer) -> None:
            try:
                line = await reader.readline()
                if line:
                    received.append(line)
                    arrived.set()
            finally:
                writer.close()

        server = await asyncio.start_unix_server(_accept, path=str(sock))
        try:
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                after = await connect_notify_transport(config)
            assert after is not None, (
                "the bridge is up and this is the same config object — a "
                "transport decided at boot is #198"
            )
            try:
                assert after.connected
                # Not a default: the operator's own setting has to reach
                # the object, because the same object carries the dev
                # lane's BLOCKED questions, not only one-way sends.
                assert after.ask_timeout_seconds == 321
                assert "notify.transport.connected" in _event_names(caplog)

                await after.send("PR #842 opened", repo="o/r", issue_number=1)
                await asyncio.wait_for(arrived.wait(), timeout=5)
                assert b"PR #842 opened" in received[0], received
            finally:
                await after.close()
        finally:
            server.close()
            await server.wait_closed()


class TestEveryFailureSaysSo:
    """#198's second done-when: a lost notification is an event.

    The original fault's only symptom was an absent chat message, which
    is indistinguishable from an uneventful day. One console line at
    startup is not a record anybody reads 24 hours later.
    """

    @pytest.mark.asyncio
    async def test_no_chat_transport_configured_is_logged(
        self, tmp_path: Path, caplog
    ) -> None:
        from ctrlrelay.cli import connect_notify_transport

        config = _config(
            tmp_path,
            {
                "type": "file_mock",
                "file_mock": {
                    "inbox": str(tmp_path / "in"),
                    "outbox": str(tmp_path / "out"),
                },
            },
        )
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            assert await connect_notify_transport(config) is None
        assert "notify.transport.unconfigured" in _event_names(caplog)

    @pytest.mark.asyncio
    async def test_a_socket_that_refuses_the_connection_is_logged(
        self, tmp_path: Path, caplog
    ) -> None:
        """`exists()` is not `accepts()`.

        A stale socket file left by a dead bridge passes the existence
        check and fails to connect. That has to read as a lost
        notification, not as a healthy one, and it must not raise:
        `handle_issue` calls this before the pipeline runs.
        """
        from ctrlrelay.cli import connect_notify_transport

        sock = tmp_path / "mm.sock"
        # A regular file at the socket path: present, not connectable.
        sock.write_text("not a socket")
        config = _config(tmp_path, _mattermost(sock))

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            assert await connect_notify_transport(config) is None
            # Let the scheduled close run before the set is read.
            # Without this the assertion below is complete only by
            # timing: a close that logged something would be missed on
            # a fast machine and caught on a slow one.
            await _settle(lambda: not _pending_closes())
        # The whole set, not "connected is absent": an absence
        # assertion here would also pass for a function that logged
        # nothing at all, which is the defect.
        assert {
            n for n in _event_names(caplog) if n.startswith("notify.transport.")
        } == {"notify.transport.connect_failed"}, _event_names(caplog)

    @pytest.mark.asyncio
    async def test_every_lost_notification_names_what_was_lost(
        self, tmp_path: Path, caplog
    ) -> None:
        """Each `None` carries a sentence, not just an event name.

        A reader at 2am gets the event name and whatever fields came
        with it. "notify.transport.socket_missing" alone does not say
        that a message was dropped on the floor.

        One case per way of losing a notification, and the list is why:
        an earlier version of this test had only the stale-socket and
        unconfigured cases, so removing the sentence from
        socket_missing — the event the outage itself produced — left it
        green. A test named "every" has to visit every branch, or it is
        a test about the branches its author happened to think of.
        """
        from ctrlrelay.cli import connect_notify_transport

        stale = tmp_path / "stale.sock"
        stale.write_text("not a socket")
        cases = [
            # The outage's own branch: nothing at the path at all.
            _config(tmp_path, _mattermost(tmp_path / "never-created.sock")),
            # Present but not connectable.
            _config(tmp_path, _mattermost(stale)),
            # No chat transport configured at all.
            _config(
                tmp_path,
                {
                    "type": "file_mock",
                    "file_mock": {
                        "inbox": str(tmp_path / "in"),
                        "outbox": str(tmp_path / "out"),
                    },
                },
            ),
        ]
        seen: set[str] = set()
        for config in cases:
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                assert await connect_notify_transport(config) is None
                await _settle(lambda: not _pending_closes())
            lost = [
                r
                for r in _ctrlrelay_events(caplog)
                if r.getMessage().startswith("notify.transport.")
            ]
            assert lost, "a dropped notification with no event is #198"
            assert all(
                "not sent" in getattr(r, "detail", "") for r in lost
            ), [
                (r.getMessage(), getattr(r, "detail", None)) for r in lost
            ]
            seen.update(r.getMessage() for r in lost)

        # The cases above must actually have reached three DIFFERENT
        # branches. Without this the loop could visit one branch three
        # times and still read as exhaustive. It is not a claim that
        # three is all of them — `TestEveryLossPathSaysNotSent` pins
        # that against the source, because one branch needs a directory
        # this process cannot stat and root can stat anything.
        assert len(seen) == 3, sorted(seen)
        assert seen <= {
            "notify.transport.socket_missing",
            "notify.transport.connect_failed",
            "notify.transport.unconfigured",
            "notify.transport.unreadable",
        }, sorted(seen)


class TestTheIssueLaneResolvesAtUse:
    """Structural, because the behavioural tests above cannot see it.

    `handle_issue` is a closure inside `poller_start`, which opens a
    state database and polls 87 repositories; driving it end to end to
    assert where one object came from is not worth it. What is worth
    asserting is the property the defect violated: the issue lane must
    ask for its transport when it needs one, and `poller_start` must
    not build a transport in its own body.

    Derived from the module's syntax tree rather than grepped, so a
    comment mentioning `SocketTransport` cannot satisfy or break it.
    """

    @staticmethod
    def _function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
        import ctrlrelay.cli as cli_module

        tree = ast.parse(Path(cli_module.__file__).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
            ):
                return node
        raise AssertionError(
            f"{name} is gone from cli.py — this guard is now about nothing"
        )

    def test_handle_issue_asks_for_a_transport_itself(self) -> None:
        handle_issue = self._function("handle_issue")
        called = {
            n.func.id
            for n in ast.walk(handle_issue)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "connect_notify_transport" in called, (
            "handle_issue must resolve its own transport; reading one "
            "decided at daemon startup is #198"
        )

    def test_poller_start_builds_no_transport_in_its_own_body(self) -> None:
        """Nested functions are fine — they run at use. The body is not.

        This is the exact line that caused the outage:
        `transport = SocketTransport(socket_path, ...)` evaluated once,
        during startup, with its answer kept for the process.
        """
        poller_start = self._function("poller_start")

        nested: set[int] = set()
        for node in ast.walk(poller_start):
            if node is poller_start:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    nested.add(id(inner))

        # Both spellings. An earlier version matched only an
        # unqualified `SocketTransport(...)`, so
        # `transports.SocketTransport(...)` in the same place would have
        # recreated the outage with this assertion green. An alias or a
        # factory is still out of reach — stated as a limit rather than
        # implied away.
        def _names_the_transport(call: ast.Call) -> bool:
            func = call.func
            if isinstance(func, ast.Name):
                return func.id == "SocketTransport"
            if isinstance(func, ast.Attribute):
                return func.attr == "SocketTransport"
            return False

        eager = [
            node
            for node in ast.walk(poller_start)
            if id(node) not in nested
            and isinstance(node, ast.Call)
            and _names_the_transport(node)
        ]
        assert not eager, (
            "SocketTransport is constructed directly in poller_start's "
            f"body at line(s) {[n.lineno for n in eager]} — that is the "
            "once-at-startup decision #198 removed"
        )


class TestItNeverCostsTheRun:
    """Invariant 1: a notification must not fail or delay an issue.

    All four cases here are faults a review round found in the first
    version of this change. Before it, the socket check ran once at
    daemon boot, where a raise is loud and happens before any work; now
    it runs per notification, where a raise would kill the issue whose
    work had just finished — and the poller marks an issue seen BEFORE
    handling it, so that work is dropped permanently rather than
    retried.
    """

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        getattr(os, "geteuid", lambda: 1)() == 0,
        reason="root ignores the mode bits, so the unstattable path is readable",
    )
    async def test_an_unstattable_socket_path_does_not_raise(
        self, tmp_path: Path, caplog
    ) -> None:
        """`Path.exists()` propagates PermissionError.

        It swallows only the not-found family. Measured: a socket under
        a directory with mode 000 raises
        `PermissionError: [Errno 13]` out of `exists()`, so guarding
        the connect alone left the docstring's "never raises" false.
        """
        from ctrlrelay.cli import connect_notify_transport

        locked = tmp_path / "locked"
        locked.mkdir()
        config = _config(tmp_path, _mattermost(locked / "mm.sock"))
        os.chmod(locked, 0o000)
        try:
            with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                assert await connect_notify_transport(config) is None
            assert "notify.transport.unreadable" in _event_names(caplog)
        finally:
            os.chmod(locked, 0o755)

    @pytest.mark.asyncio
    async def test_a_connect_that_never_completes_is_bounded(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """Catching exceptions does not help with a coroutine that hangs.

        `asyncio.wait_for` is a real bound here, and only here:
        `SocketTransport.connect` does nothing with `CancelledError`, so
        the cancellation the timeout issues is not absorbed. The close
        side is the opposite and is never awaited at all.
        """
        import ctrlrelay.cli as cli_module
        import ctrlrelay.transports as transports
        from ctrlrelay.cli import connect_notify_transport

        sock = tmp_path / "mm.sock"
        sock.write_text("placeholder so the existence check passes")
        config = _config(tmp_path, _mattermost(sock))

        built: list[object] = []

        class _WedgedTransport:
            def __init__(self, *_a, **_kw) -> None:
                self.closed = False
                built.append(self)

            async def connect(self) -> None:
                await asyncio.Event().wait()  # never returns

            async def close(self) -> None:
                self.closed = True

        monkeypatch.setattr(transports, "SocketTransport", _WedgedTransport)
        monkeypatch.setattr(
            cli_module, "_NOTIFY_CONNECT_TIMEOUT_SECONDS", 0.05
        )

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            result = await asyncio.wait_for(
                connect_notify_transport(config), timeout=5
            )
            assert result is None, (
                "a connect nobody waited for is not a working channel"
            )
            await _settle(lambda: bool(built) and built[0].closed)

        events = _event_names(caplog)
        assert "notify.transport.connect_failed" in events
        assert "notify.transport.connected" not in events
        # Asserted on the object. A previous version asserted only that
        # "connected" was absent, which stays true with the close
        # deleted.
        assert built, "the resolver never got as far as building one"
        assert built[0].closed, "the abandoned transport was left open"

    @pytest.mark.asyncio
    async def test_a_close_that_raises_is_reported_and_does_not_escape(
        self, caplog
    ) -> None:
        """The close is the last thing an issue does.

        If it raised, a run whose work was already finished and
        committed would report as failed. The failure is reported from
        the task's done callback, so it is still visible even though
        nobody waits for it.
        """
        from ctrlrelay.cli import _close_notify_transport

        class _AngryTransport:
            async def close(self) -> None:
                raise OSError("bridge went away")

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            _close_notify_transport(_AngryTransport())
            await _settle(
                lambda: "notify.transport.close_failed" in _event_names(caplog)
            )
        assert "notify.transport.close_failed" in _event_names(caplog)

    @pytest.mark.asyncio
    async def test_a_close_that_hangs_does_not_hold_up_the_caller(
        self, caplog
    ) -> None:
        """The reason the close is not awaited at all.

        Waiting for it correctly was wrong three different ways —
        `wait_for` waits for a cancellation `SocketTransport.close`
        absorbs, and abandoning the task leaves an orphan if the caller
        is itself cancelled mid-wait. Nothing downstream needs the
        socket shut, so this returns before the close has finished.
        """
        from ctrlrelay.cli import _close_notify_transport

        entered = asyncio.Event()

        class _WedgedTransport:
            async def close(self) -> None:
                entered.set()
                await asyncio.Event().wait()

        import ctrlrelay.cli as cli_module

        # No await on the call itself: a `finally` with no await in it
        # cannot be interrupted by the cancellation unwinding the stack.
        before = set(cli_module._NOTIFY_CLOSE_TASKS)
        _close_notify_transport(_WedgedTransport())
        started = set(cli_module._NOTIFY_CLOSE_TASKS) - before
        assert len(started) == 1, (
            "the close was not scheduled, so either it ran inline — which "
            "is what holds up the caller — or it was dropped"
        )
        task = started.pop()
        # It is still running. That is the whole claim: the caller got
        # its turn back with the close unfinished.
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert not task.done(), "the close finished, so this proves nothing"
        task.cancel()

    @pytest.mark.asyncio
    async def test_a_logging_handler_that_throws_does_not_kill_the_issue(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Every loss path ends in a log line, including the handlers.

        So a logger that raises escapes from inside the handler written
        to contain failures — and kills the issue rather than the
        message. `log_event`'s own docstring warns that a field
        colliding with a LogRecord attribute raises `KeyError`, which
        is how this happens for real.

        Found by mutation: deleting the guard around the logging left
        all fourteen other cases in this file green.
        """
        import ctrlrelay.cli as cli_module
        from ctrlrelay.cli import connect_notify_transport

        config = _config(tmp_path, _mattermost(tmp_path / "absent.sock"))

        calls: list[str] = []

        class _AngryLogger:
            def info(self, *_a, **_kw) -> None:
                calls.append("info")
                raise KeyError("name")

        monkeypatch.setattr(cli_module, "_logger", _AngryLogger())
        assert await connect_notify_transport(config) is None
        # Observed, so this cannot pass for a resolver that returned
        # None without ever trying to log.
        assert calls, "the throwing logger was never reached"

    @pytest.mark.asyncio
    async def test_a_close_that_is_cancelled_is_reported(self, caplog) -> None:
        """A cancelled close left the socket half-shut and said nothing.

        `SocketTransport.close` absorbs a cancellation around its await
        of the receive task and carries on into `wait_closed()`, so a
        second one arriving there leaves the socket open. Discarding
        that case made `_close_notify_transport`'s own promise — that a
        close which did not finish stays visible — false.
        """
        import ctrlrelay.cli as cli_module
        from ctrlrelay.cli import _close_notify_transport

        entered = asyncio.Event()

        class _SlowTransport:
            async def close(self) -> None:
                entered.set()
                await asyncio.Event().wait()

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            before = set(cli_module._NOTIFY_CLOSE_TASKS)
            _close_notify_transport(_SlowTransport())
            task = (set(cli_module._NOTIFY_CLOSE_TASKS) - before).pop()
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            await _settle(
                lambda: "notify.transport.close_cancelled"
                in _event_names(caplog)
            )
        assert "notify.transport.close_cancelled" in _event_names(caplog)
        assert task not in cli_module._NOTIFY_CLOSE_TASKS, (
            "the finished task was not let go of"
        )

    @pytest.mark.asyncio
    async def test_an_exception_that_cannot_be_printed_is_still_reported(
        self, caplog
    ) -> None:
        """Formatting the exception is part of what can fail.

        Every loss path used to hand the logger ``error=str(e)[:200]``,
        evaluated at the call site — outside the guard. A class whose
        ``__str__`` raises therefore escaped the handler written to
        contain failures and dropped the issue.

        Found by mutation: removing the inner guard left all eighteen
        other cases in this file green.
        """
        from ctrlrelay.cli import connect_notify_transport

        class _UnprintableError(Exception):
            def __str__(self) -> str:
                raise RuntimeError("cannot be printed")

        class _BadConfig:
            @property
            def transport(self):
                raise _UnprintableError

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            assert await connect_notify_transport(_BadConfig()) is None

        events = [
            r
            for r in _ctrlrelay_events(caplog)
            if r.getMessage() == "notify.transport.unreadable"
        ]
        assert events, _event_names(caplog)
        assert getattr(events[0], "error", None) == "<unprintable>"
        assert getattr(events[0], "error_type", None) == "_UnprintableError"

    @pytest.mark.asyncio
    async def test_an_interrupt_before_the_connect_propagates_and_says_so(
        self, caplog
    ) -> None:
        """KeyboardInterrupt and SystemExit are not swallowed.

        A process told to stop should stop, and the operator still
        needs to know a notification went missing. `_log_quietly`
        catches `Exception`, not `BaseException`, so this path is also
        how a logging handler raising one of these behaves.

        Raised from reading the config rather than from inside the
        connect, because a `KeyboardInterrupt` raised inside a coroutine
        the event loop is driving is not reliably delivered to the
        awaiting frame and can take the test runner with it. This path
        has no loop machinery between the raise and the handler. (An
        earlier version of this docstring explained the choice by
        saying `wait_for` wraps the coroutine in a Task — true up to
        3.11, and this project requires 3.12.)
        """
        from ctrlrelay.cli import connect_notify_transport

        class _InterruptingConfig:
            @property
            def transport(self):
                raise KeyboardInterrupt

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with pytest.raises(KeyboardInterrupt):
                await connect_notify_transport(_InterruptingConfig())
        assert "notify.transport.interrupted" in _event_names(caplog)

    @pytest.mark.asyncio
    async def test_an_interrupt_after_the_connect_still_closes_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The transport is closed on the way out.

        At this point it is held only by a statement that never
        completed, so `handle_issue`'s try/finally has not begun and
        nothing else will close it. Driven through the success log,
        which is the realistic trigger the docstring names and is not
        inside a Task.
        """
        import ctrlrelay.cli as cli_module
        import ctrlrelay.transports as transports
        from ctrlrelay.cli import connect_notify_transport

        sock = tmp_path / "mm.sock"
        config = _config(tmp_path, _mattermost(sock))

        built: list[object] = []

        class _FakeTransport:
            def __init__(self, *_a, **_kw) -> None:
                self.closed = False
                built.append(self)

            async def connect(self) -> None:
                return None

            async def close(self) -> None:
                self.closed = True

        def _interrupting_log_event(_logger, event, **_fields):
            if event == "notify.transport.connected":
                raise KeyboardInterrupt
            return None

        # A real socket, so the existence check passes for the right
        # reason rather than because the check was skipped.
        server = await asyncio.start_unix_server(
            lambda r, w: w.close(), path=str(sock)
        )
        try:
            monkeypatch.setattr(transports, "SocketTransport", _FakeTransport)
            monkeypatch.setattr(
                cli_module, "log_event", _interrupting_log_event
            )
            with pytest.raises(KeyboardInterrupt):
                await connect_notify_transport(config)
            await _settle(lambda: bool(built) and built[0].closed)
        finally:
            server.close()
            await server.wait_closed()

        assert built, "the resolver never built a transport"
        assert built[0].closed, "an interrupted connect left the socket open"

    @pytest.mark.asyncio
    async def test_unfinished_closes_do_not_pile_up(
        self, monkeypatch, caplog
    ) -> None:
        """`SocketTransport.close` can stay suspended in wait_closed().

        One retained task per issue, each holding a socket, is a slow
        leak on a daemon that runs for days. Over the ceiling the
        oldest is cancelled, which the done callback reports.
        """
        import ctrlrelay.cli as cli_module
        from ctrlrelay.cli import _close_notify_transport

        monkeypatch.setattr(cli_module, "_NOTIFY_CLOSE_MAX_PENDING", 4)

        class _WedgedTransport:
            async def close(self) -> None:
                await asyncio.Event().wait()

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            for _ in range(20):
                _close_notify_transport(_WedgedTransport())
                await asyncio.sleep(0)
            # Exactly the ceiling, not merely under it: `<= 4` also
            # passes for a close that never schedules anything.
            assert len(cli_module._NOTIFY_CLOSE_TASKS) == 4, len(
                cli_module._NOTIFY_CLOSE_TASKS
            )
            await _settle(
                lambda: _event_names(caplog).count(
                    "notify.transport.close_evicted"
                )
                >= 16
            )
        # 20 scheduled, 4 retained, so 16 were evicted and each one
        # said so.
        assert (
            _event_names(caplog).count("notify.transport.close_evicted") == 16
        ), _event_names(caplog)

    def test_closing_nothing_is_a_no_op(self, caplog) -> None:
        """Closing `None` does nothing and says nothing.

        This is what lets `handle_issue`'s `finally` be unconditional,
        so it can also run on the path where no channel was ever
        opened. The old code's `if connected_transport is not None`
        around each close was correct whenever the name was set — what
        leaked was an exception in the statements that sat OUTSIDE the
        try at all. It does not exercise
        `handle_issue` — `TestTheTransportIsClosedOnEveryPath` is the
        guard that reads that function.
        """
        import ctrlrelay.cli as cli_module
        from ctrlrelay.cli import _close_notify_transport

        before = set(cli_module._NOTIFY_CLOSE_TASKS)
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            _close_notify_transport(None)
        # Both halves. The task-set assertion alone also passes with the
        # `None` guard DELETED — `None.close()` raises AttributeError
        # synchronously, the try around ensure_future swallows it, and
        # the set is unchanged — so the silence is what discriminates.
        assert set(cli_module._NOTIFY_CLOSE_TASKS) == before
        assert not _event_names(caplog), _event_names(caplog)


class TestCancellationIsPassedOnAndCleanedUp:
    """Shutdown must not swallow a cancellation or leak the half-open socket.

    `asyncio.CancelledError` is a `BaseException`, so the resolver's
    `except Exception` does not see it — and the candidate was created
    inside a statement that never completed, so `handle_issue`'s
    try/finally has not started and nothing else holds a reference.
    """

    @pytest.mark.asyncio
    async def test_a_cancelled_connect_closes_and_re_raises(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        import ctrlrelay.cli as cli_module
        import ctrlrelay.transports as transports
        from ctrlrelay.cli import connect_notify_transport

        sock = tmp_path / "mm.sock"
        sock.write_text("placeholder so the existence check passes")
        config = _config(tmp_path, _mattermost(sock))

        started = asyncio.Event()
        built: list[object] = []

        class _SlowTransport:
            def __init__(self, *_a, **_kw) -> None:
                self.closed = False
                built.append(self)

            async def connect(self) -> None:
                started.set()
                await asyncio.Event().wait()

            async def close(self) -> None:
                self.closed = True

        monkeypatch.setattr(transports, "SocketTransport", _SlowTransport)
        monkeypatch.setattr(cli_module, "_NOTIFY_CONNECT_TIMEOUT_SECONDS", 30)

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            task = asyncio.ensure_future(connect_notify_transport(config))
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # The close is scheduled, not awaited, so give the loop a
            # turn to run it.
            await _settle(lambda: bool(built) and built[0].closed)

        assert built, "the resolver never built a transport"
        assert built[0].closed, "a cancelled connect left the socket open"
        assert "notify.transport.cancelled" in _event_names(caplog)


class TestEveryLossPathSaysNotSent:
    """Read from the source, because a test cannot reach every branch.

    One loss path needs a directory this process cannot stat, and CI
    runs as root, where there is no such directory. Another needs the
    enclosing task to be cancelled mid-connect. So the claim "every way
    of losing a notification is logged, and says a notification was
    lost" is pinned structurally: a branch added later is covered
    whether or not anybody writes a case for it.

    Paired per path, not counted globally. An earlier version of this
    guard compared the number of loss events to the number of
    ``return None`` statements, which is satisfied by two logs on one
    path plus a silent path, and broken by a correct path that exits
    with ``raise`` instead of a return.

    Known limit, stated rather than implied: this reads literal
    ``log_event`` calls in this one function. A loss routed through a
    helper, or an event name built at runtime, is invisible to it — the
    guard fails loudly on a computed name rather than passing quietly,
    which is the direction that sends somebody to look.
    """

    @staticmethod
    def _resolver() -> ast.AsyncFunctionDef:
        import ctrlrelay.cli as cli_module

        tree = ast.parse(Path(cli_module.__file__).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "connect_notify_transport"
            ):
                return node
        raise AssertionError("connect_notify_transport is gone from cli.py")

    @staticmethod
    def _loss_event(stmt) -> tuple[str, ast.Call] | None:
        """``(event_name, call)`` if this statement logs a lost notification.

        Only a DIRECT statement of the block counts. Walking every
        descendant accepted a log buried inside an `if`, followed by an
        unconditional silent `return None` — so the log did not have to
        run on the path that exited.
        """
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            return None
        for node in (stmt.value,):
            if not (
                isinstance(node.func, ast.Name)
                and node.func.id == "_log_quietly"
            ):
                continue
            name = node.args[0] if node.args else None
            if not (
                isinstance(name, ast.Constant) and isinstance(name.value, str)
            ):
                raise AssertionError(
                    "an event name in connect_notify_transport is computed "
                    "rather than literal; this guard can no longer read it, "
                    "so go and check the loss paths by hand"
                )
            if name.value != "notify.transport.connected":
                return name.value, node
        return None

    # The one expression a `return` may carry without being a loss:
    # the transport that was successfully connected.
    _SUCCESS_RETURN = "candidate"

    @classmethod
    def _is_loss_exit(cls, stmt) -> bool:
        """A statement that leaves the function without a channel.

        ``return None`` is the ordinary case, a bare ``return`` is the
        same thing written differently, and a bare ``raise`` is the
        cancellation and interrupt paths — all three must say what went
        unsent. Only the one return that hands back the connected
        transport is exempt, and it is named rather than inferred: any
        other returned expression fails loudly, because a guard that
        cannot tell what is being returned must not quietly decide it
        was the good case.
        """
        if isinstance(stmt, ast.Raise):
            # A bare `raise` and `raise e` re-raise the same thing, so
            # rewriting a handler as `except X as e: raise e` with its
            # log deleted must not slip past this.
            return stmt.exc is None or isinstance(stmt.exc, ast.Name)
        if not isinstance(stmt, ast.Return):
            return False
        value = stmt.value
        if value is None:
            return True
        if isinstance(value, ast.Constant):
            if value.value is None:
                return True
            raise AssertionError(
                f"connect_notify_transport returns {value.value!r}; this "
                "guard only knows None and the connected transport"
            )
        if isinstance(value, ast.Name) and value.id == cls._SUCCESS_RETURN:
            return False
        raise AssertionError(
            "connect_notify_transport returns an expression this guard "
            "cannot classify, so go and check the loss paths by hand"
        )

    def test_each_loss_path_logs_before_it_exits(self) -> None:
        resolver = self._resolver()

        exits = 0
        unlogged = []
        silent_detail = []

        # Every statement list in the function: a body, an else, a
        # handler. A loss exit must be preceded IN ITS OWN LIST by the
        # event that explains it, so a log on a sibling branch cannot
        # vouch for this one.
        for node in ast.walk(resolver):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                self._check_block(block, exits_counter := [], unlogged,
                                  silent_detail)
                exits += len(exits_counter)
            for handler in getattr(node, "handlers", []):
                self._check_block(handler.body, exits_counter := [],
                                  unlogged, silent_detail)
                exits += len(exits_counter)

        assert exits, "the resolver has stopped being able to say 'no'"
        assert not unlogged, (
            "these exits leave without logging anything, so a reader "
            f"cannot tell them from an uneventful day: lines {unlogged}"
        )
        assert not silent_detail, (
            "these loss events do not say a notification was not sent: "
            f"{silent_detail}"
        )

    @classmethod
    def _check_block(cls, block, exits, unlogged, silent_detail) -> None:
        logged: list[tuple[str, ast.Call]] = []
        for stmt in block:
            found = cls._loss_event(stmt)
            if found is not None:
                logged.append(found)
            if not cls._is_loss_exit(stmt):
                continue
            exits.append(stmt.lineno)
            if not logged:
                unlogged.append(stmt.lineno)
                continue
            event_name, call = logged[-1]
            detail = next(
                (kw.value for kw in call.keywords if kw.arg == "detail"), None
            )
            text = _literal_text(detail)
            if text is None or "not sent" not in text:
                silent_detail.append((event_name, text))


class TestTheTransportIsClosedOnEveryPath:
    """`handle_issue` now creates a connection per issue, not one per daemon.

    So an exception or an early return between creating it and entering
    the guarded block leaks a socket on every poll — a defect the old
    singleton could not have. Structural, because `handle_issue` is a
    closure that opens a state database and polls 87 repositories.
    """

    def test_nothing_sits_between_the_connect_and_its_try(self) -> None:
        import ctrlrelay.cli as cli_module

        tree = ast.parse(Path(cli_module.__file__).read_text())
        handle_issue = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "handle_issue"
            ),
            None,
        )
        assert handle_issue is not None, "handle_issue is gone from cli.py"

        body = handle_issue.body
        # Calls, not statements. An earlier version indexed statements,
        # so two connects inside one statement read as "exactly one".
        connect_calls = [
            (i, n)
            for i, stmt in enumerate(body)
            for n in ast.walk(stmt)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "connect_notify_transport"
        ]
        assert len(connect_calls) == 1, (
            "exactly one transport per issue; found "
            f"{len(connect_calls)} connect calls at lines "
            f"{[n.lineno for _, n in connect_calls]}"
        )
        i = connect_calls[0][0]
        assert i + 1 < len(body), "nothing follows the connect at all"
        following = body[i + 1]
        assert isinstance(following, ast.Try), (
            "the statement after the connect is "
            f"{type(following).__name__}, not a try — anything here can "
            "leak the connection"
        )
        assert following.finalbody, "the try has no finally to close it"
        # A DIRECT statement of the finally, not nested in an `if`: a
        # conditional close is how the old code left one path open.
        # Not awaited, either — `_close_notify_transport` is
        # deliberately synchronous so an unwinding cancellation cannot
        # interrupt the `finally`.
        closed = [
            stmt
            for stmt in following.finalbody
            if isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == "_close_notify_transport"
        ]
        assert len(closed) == 1, (
            "the finally must close the transport unconditionally, as one "
            "of its own statements; _close_notify_transport handles None "
            "so no `if` is needed"
        )
        passed = closed[0].value.args
        assert len(passed) == 1 and isinstance(passed[0], ast.Name), (
            "the finally closes something other than a plain local name"
        )
        # The exact name the connect was assigned to, not merely a name
        # assigned somewhere earlier: otherwise
        # `t = None; leaked = await connect(...); finally: close(t)`
        # satisfies every assertion while leaking `leaked`.
        connect_stmt = body[i]
        assert isinstance(connect_stmt, ast.Assign), (
            "the connect's result is not assigned to anything, so nothing "
            "can close it"
        )
        targets = [
            t.id for t in connect_stmt.targets if isinstance(t, ast.Name)
        ]
        assert passed[0].id in targets, (
            f"the finally closes {passed[0].id!r} but the connect was "
            f"assigned to {targets} — the connected transport is leaked"
        )
        # And it still holds the same object when the finally runs.
        # `connected_transport = None` anywhere in the guarded block —
        # the deferred branch is the tempting place — makes the finally
        # close None and leak the socket every poll, with every
        # assertion above green.
        rebound = [
            n
            for stmt in following.body
            for n in ast.walk(stmt)
            if (
                isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == passed[0].id
                    for t in n.targets
                )
            )
            or (
                isinstance(n, (ast.AugAssign, ast.AnnAssign))
                and isinstance(n.target, ast.Name)
                and n.target.id == passed[0].id
            )
            or (
                isinstance(n, ast.Delete)
                and any(
                    isinstance(t, ast.Name) and t.id == passed[0].id
                    for t in n.targets
                )
            )
        ]
        assert not rebound, (
            f"{passed[0].id!r} is reassigned or deleted inside the guarded "
            f"block at line(s) {[n.lineno for n in rebound]}, so the "
            "finally may close something other than what was opened"
        )
        # And it is the ONLY close, so no path can close it twice or
        # close it early and go on using it.
        closes = [
            n
            for n in ast.walk(handle_issue)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_close_notify_transport"
        ]
        assert len(closes) == 1, (
            f"{len(closes)} close sites in handle_issue; the finally should "
            "be the only one"
        )
        # And nobody reaches past the helper. An earlier version counted
        # only calls named `_close_notify_transport`, so
        # `await connected_transport.close()` inside the guarded block
        # would have double-closed with this assertion green.
        direct = [
            n
            for n in ast.walk(handle_issue)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "close"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == passed[0].id
        ]
        assert not direct, (
            f"{passed[0].id}.close() is called directly at line(s) "
            f"{[n.lineno for n in direct]}; the finally closes it too"
        )
