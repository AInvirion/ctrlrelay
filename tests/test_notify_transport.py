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

        async def _accept(reader, writer) -> None:  # pragma: no cover
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
                # the object, which is the #181 family.
                assert after.ask_timeout_seconds == 321
                assert "notify.transport.connected" in _event_names(caplog)
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
        names = _event_names(caplog)
        assert "notify.transport.connect_failed" in names
        assert "notify.transport.connected" not in names

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

        # The cases above must actually have reached three different
        # branches. Without this the loop could visit one branch three
        # times and still read as exhaustive.
        assert seen == {
            "notify.transport.socket_missing",
            "notify.transport.connect_failed",
            "notify.transport.unconfigured",
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
