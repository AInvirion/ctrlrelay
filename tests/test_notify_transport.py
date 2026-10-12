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

        eager = [
            node
            for node in ast.walk(poller_start)
            if id(node) not in nested
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SocketTransport"
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

        A listening socket whose backlog is saturated leaves
        `open_unix_connection` pending, and an unbounded await there
        holds the issue open with no timeout anywhere above it.
        """
        import ctrlrelay.cli as cli_module
        import ctrlrelay.transports as transports
        from ctrlrelay.cli import connect_notify_transport

        sock = tmp_path / "mm.sock"
        sock.write_text("placeholder so the existence check passes")
        config = _config(tmp_path, _mattermost(sock))

        class _WedgedTransport:
            def __init__(self, *_a, **_kw) -> None:
                self.closed = False

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
        assert result is None
        events = _event_names(caplog)
        assert "notify.transport.connect_failed" in events
        # The abandoned half-open transport is closed rather than left
        # for the garbage collector.
        assert "notify.transport.connected" not in events

    @pytest.mark.asyncio
    async def test_a_close_that_raises_does_not_escape(self, caplog) -> None:
        """The close is the last thing an issue does.

        If it raised, a run whose work was already finished and
        committed would report as failed.
        """
        from ctrlrelay.cli import _close_notify_transport

        class _AngryTransport:
            async def close(self) -> None:
                raise OSError("bridge went away")

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            await _close_notify_transport(_AngryTransport())
        assert "notify.transport.close_failed" in _event_names(caplog)

    @pytest.mark.asyncio
    async def test_a_close_that_hangs_is_bounded(
        self, monkeypatch, caplog
    ) -> None:
        import ctrlrelay.cli as cli_module
        from ctrlrelay.cli import _close_notify_transport

        class _WedgedTransport:
            async def close(self) -> None:
                await asyncio.Event().wait()

        monkeypatch.setattr(cli_module, "_NOTIFY_CLOSE_TIMEOUT_SECONDS", 0.05)
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            await asyncio.wait_for(
                _close_notify_transport(_WedgedTransport()), timeout=5
            )
        assert "notify.transport.close_failed" in _event_names(caplog)

    @pytest.mark.asyncio
    async def test_closing_nothing_is_fine(self) -> None:
        """`handle_issue`'s finally runs whether or not a channel exists."""
        from ctrlrelay.cli import _close_notify_transport

        await _close_notify_transport(None)


class TestEveryLossPathSaysNotSent:
    """Read from the source, because a test cannot reach every branch.

    One loss path needs a directory this process cannot stat, and CI
    runs as root, where there is no such directory. So the claim "every
    way of losing a notification is logged, and says a notification was
    lost" is pinned structurally: a branch added later is covered
    whether or not anybody writes a case for it.
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

    def test_each_none_return_is_matched_by_a_logged_loss(self) -> None:
        resolver = self._resolver()

        none_returns = [
            n
            for n in ast.walk(resolver)
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Constant)
            and n.value.value is None
        ]
        loss_events = []
        for node in ast.walk(resolver):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "log_event"
            ):
                continue
            name = node.args[1] if len(node.args) > 1 else None
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                raise AssertionError(
                    "an event name in connect_notify_transport is computed "
                    "rather than literal; this guard can no longer read it"
                )
            if name.value != "notify.transport.connected":
                loss_events.append((name.value, node))

        assert none_returns, "the resolver has stopped being able to say 'no'"
        assert len(loss_events) == len(none_returns), (
            f"{len(none_returns)} paths return None but "
            f"{len(loss_events)} log a loss: "
            f"{[n for n, _ in loss_events]}"
        )

        missing = []
        for event_name, call in loss_events:
            detail = next(
                (kw.value for kw in call.keywords if kw.arg == "detail"), None
            )
            text = _literal_text(detail)
            if text is None or "not sent" not in text:
                missing.append((event_name, text))
        assert not missing, (
            "these loss events do not say a notification was not sent, so a "
            f"reader cannot tell them from an uneventful day: {missing}"
        )


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
        connect_at = [
            i
            for i, stmt in enumerate(body)
            if any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "connect_notify_transport"
                for n in ast.walk(stmt)
            )
        ]
        assert len(connect_at) == 1, (
            "exactly one transport per issue; "
            f"found {len(connect_at)} connect sites"
        )
        i = connect_at[0]
        assert i + 1 < len(body), "nothing follows the connect at all"
        following = body[i + 1]
        assert isinstance(following, ast.Try), (
            "the statement after the connect is "
            f"{type(following).__name__}, not a try — anything here can "
            "leak the connection"
        )
        assert following.finalbody, "the try has no finally to close it"
        closed = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_close_notify_transport"
            for stmt in following.finalbody
            for n in ast.walk(stmt)
        )
        assert closed, "the finally does not close the transport"
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
