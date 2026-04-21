"""Tests for bridge server."""

import asyncio
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest


class TestBridgeServer:
    @pytest.fixture
    def socket_path(self):
        # tmp_path can exceed AF_UNIX's 104-char limit on macOS; use a short dir.
        d = tempfile.mkdtemp()
        yield Path(d) / "b.sock"
        shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_creates_socket_file(self, socket_path) -> None:
        """Server should create socket file."""
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)

        assert socket_path.exists()

        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_socket_permissions(self, socket_path) -> None:
        """Socket should have 0600 permissions."""
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)

        mode = stat.S_IMODE(os.stat(socket_path).st_mode)
        assert mode == 0o600

        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_handles_ping_pong(self, socket_path) -> None:
        """Server should respond to ping with pong."""
        from ctrlrelay.bridge.protocol import (
            BridgeMessage,
            BridgeOp,
            parse_message,
            serialize_message,
        )
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        ping = serialize_message(BridgeMessage(op=BridgeOp.PING))
        writer.write(ping.encode())
        await writer.drain()

        response = await asyncio.wait_for(reader.readline(), timeout=1)
        msg = parse_message(response.decode())
        assert msg.op == BridgeOp.PONG

        writer.close()
        await writer.wait_closed()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_cleans_up_socket_on_stop(self, socket_path) -> None:
        """Server should remove socket file on stop."""
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        assert socket_path.exists()

        await server.stop()
        task.cancel()

        assert not socket_path.exists()

    @pytest.mark.asyncio
    async def test_ask_then_telegram_reply_delivers_answer(self, socket_path) -> None:
        """End-to-end: a client ASKs, bridge posts to Telegram, a simulated
        incoming Telegram reply routes back to the same client as ANSWER."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.protocol import (
            BridgeMessage,
            BridgeOp,
            parse_message,
            serialize_message,
        )
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)

        # Swap in a mock Telegram handler so ASK doesn't hit the real API.
        server._telegram.ask = AsyncMock(return_value=999)  # type: ignore[attr-defined]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            ask = serialize_message(BridgeMessage(
                op=BridgeOp.ASK,
                request_id="r-xyz",
                question="pin or bump?",
            ))
            writer.write(ask.encode())
            await writer.drain()

            # Bridge acknowledges with status=pending.
            ack_raw = await asyncio.wait_for(reader.readline(), timeout=1)
            ack = parse_message(ack_raw.decode())
            assert ack.op == BridgeOp.ACK
            assert ack.status == "pending"
            assert ack.request_id == "r-xyz"

            # Simulate the operator replying via Telegram.
            await server._on_telegram_reply("pin", reply_to_message_id=None)

            # Bridge pushes ANSWER back over the same socket.
            answer_raw = await asyncio.wait_for(reader.readline(), timeout=1)
            answer = parse_message(answer_raw.decode())
            assert answer.op == BridgeOp.ANSWER
            assert answer.request_id == "r-xyz"
            assert answer.answer == "pin"
            assert answer.answered_at is not None
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
            task.cancel()

    @pytest.mark.asyncio
    async def test_reply_to_specific_message_prefers_that_question(
        self, socket_path,
    ) -> None:
        """If the operator replies to a specific question, bridge matches by
        telegram_msg_id rather than falling back to FIFO order."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.protocol import (
            BridgeMessage,
            BridgeOp,
            parse_message,
            serialize_message,
        )
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)

        # Two ASKs -> two different Telegram msg_ids.
        ask_mock = AsyncMock(side_effect=[111, 222])
        server._telegram.ask = ask_mock  # type: ignore[attr-defined]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            for rid, q in [("r-1", "first"), ("r-2", "second")]:
                writer.write(serialize_message(BridgeMessage(
                    op=BridgeOp.ASK, request_id=rid, question=q,
                )).encode())
                await writer.drain()
                await asyncio.wait_for(reader.readline(), timeout=1)  # ACK

            # Reply specifically to the SECOND question (msg_id=222).
            await server._on_telegram_reply("answering second", reply_to_message_id=222)

            raw = await asyncio.wait_for(reader.readline(), timeout=1)
            answer = parse_message(raw.decode())
            assert answer.op == BridgeOp.ANSWER
            assert answer.request_id == "r-2"
            assert answer.answer == "answering second"
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
            task.cancel()

    @pytest.mark.asyncio
    async def test_client_disconnect_drops_pending_questions(
        self, socket_path,
    ) -> None:
        """If the client disconnects, its pending questions must be cleared
        so we don't try to deliver ANSWER to a dead socket."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.protocol import (
            BridgeMessage,
            BridgeOp,
            serialize_message,
        )
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.ask = AsyncMock(return_value=42)  # type: ignore[attr-defined]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(serialize_message(BridgeMessage(
            op=BridgeOp.ASK, request_id="r-dead", question="?",
        )).encode())
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=1)  # ACK

        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.1)  # let server observe disconnect

        # Reply arriving now must be dropped cleanly (no exception raised,
        # no stale question left behind) AND the operator must be told the
        # reply didn't land — otherwise the message disappears silently and
        # the user waits forever for a BLOCKED session to resume.
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]
        await server._on_telegram_reply("hello", reply_to_message_id=None)
        assert server._pending_questions == {}
        server._telegram.send.assert_awaited_once()  # type: ignore[attr-defined]
        sent_text = server._telegram.send.await_args.args[0]  # type: ignore[attr-defined]
        assert "wasn't routed" in sent_text
        assert "ctrlrelay run secops" in sent_text

        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_orphan_reply_routes_to_pending_resume(
        self, socket_path, tmp_path,
    ) -> None:
        """With exactly one unanswered BLOCKED session, an orphan reply
        routes to it unambiguously (no session_id substring needed)."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume(
            session_id="secops-owner-r-abc",
            pipeline="secops",
            repo="owner/r",
            question="merge major bumps?",
        )

        server = BridgeServer(
            socket_path=socket_path, bot_token="test", chat_id=123,
            state_db=db,
        )
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]

        await server._on_telegram_reply(
            "merge #286, close the others", reply_to_message_id=None
        )

        # Answer was persisted against the BLOCKED session...
        rows = db.list_pending_resumes_to_execute()
        assert len(rows) == 1
        assert rows[0]["session_id"] == "secops-owner-r-abc"
        assert rows[0]["answer"] == "merge #286, close the others"

        # ...and the operator got told, not silently dropped.
        server._telegram.send.assert_awaited_once()  # type: ignore[attr-defined]
        sent_text = server._telegram.send.await_args.args[0]  # type: ignore[attr-defined]
        assert "Answer queued" in sent_text
        assert "secops-owner-r-abc" in sent_text
        assert "owner/r" in sent_text

        db.close()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_orphan_reply_refuses_to_guess_when_multiple_blocked(
        self, socket_path, tmp_path,
    ) -> None:
        """When two repos are BLOCKED at once and the reply text doesn't
        name one, refuse to route — FIFO would silently drive the wrong
        session. This was codex's P1-A finding."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume(
            session_id="secops-owner-repoA-111",
            pipeline="secops", repo="owner/repoA",
            question="A: merge major bumps?",
        )
        db.add_pending_resume(
            session_id="secops-owner-repoB-222",
            pipeline="secops", repo="owner/repoB",
            question="B: defer or merge?",
        )

        server = BridgeServer(
            socket_path=socket_path, bot_token="test", chat_id=123,
            state_db=db,
        )
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]

        await server._on_telegram_reply(
            "merge it", reply_to_message_id=None
        )

        # Neither row was answered — reply is held back until the
        # operator disambiguates.
        assert db.list_pending_resumes_to_execute() == []
        unanswered = db.list_unanswered_pending_resumes()
        assert len(unanswered) == 2
        assert all(r["answer"] is None for r in unanswered)

        # And the operator is told which session_ids are pending.
        sent_text = server._telegram.send.await_args.args[0]  # type: ignore[attr-defined]
        assert "multiple BLOCKED sessions" in sent_text.lower() or \
               "multiple" in sent_text
        assert "secops-owner-repoA-111" in sent_text
        assert "secops-owner-repoB-222" in sent_text

        db.close()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_orphan_reply_disambiguates_via_session_id_in_text(
        self, socket_path, tmp_path,
    ) -> None:
        """Operator reply includes the session_id they mean — route to
        that row, not FIFO. This is how the ambiguous case gets resolved:
        copy the session_id from the BLOCKED notification and paste it
        into the reply."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume(
            session_id="secops-owner-repoA-111",
            pipeline="secops", repo="owner/repoA",
            question="?",
        )
        db.add_pending_resume(
            session_id="secops-owner-repoB-222",
            pipeline="secops", repo="owner/repoB",
            question="?",
        )

        server = BridgeServer(
            socket_path=socket_path, bot_token="test", chat_id=123,
            state_db=db,
        )
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]

        # Reply names session B explicitly.
        await server._on_telegram_reply(
            "For secops-owner-repoB-222: close the PR",
            reply_to_message_id=None,
        )

        # Only B was answered; A stays pending.
        pending = db.list_pending_resumes_to_execute()
        assert [r["session_id"] for r in pending] == [
            "secops-owner-repoB-222"
        ]
        still_unanswered = [
            r["session_id"]
            for r in db.list_unanswered_pending_resumes()
        ]
        assert still_unanswered == ["secops-owner-repoA-111"]

        db.close()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_client_disconnect_during_response_write_is_silent(
        self, socket_path, caplog,
    ) -> None:
        """Race: the client closes the socket right after sending a request
        but before the bridge finishes flushing the ACK. The bridge used to
        propagate ConnectionResetError from writer.drain() as an unhandled
        exception into bridge.error.log. After the fix it must be swallowed
        quietly (DEBUG-level at most)."""
        import logging
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.protocol import (
            BridgeMessage,
            BridgeOp,
            serialize_message,
        )
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)

        # Make the telegram call slow enough that we can close the client
        # before the bridge finishes the response write.
        async def slow_ask(*args, **kwargs):
            await asyncio.sleep(0.1)
            return 42
        server._telegram.ask = AsyncMock(side_effect=slow_ask)  # type: ignore[attr-defined]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(serialize_message(BridgeMessage(
            op=BridgeOp.ASK, request_id="r-race", question="?",
        )).encode())
        await writer.drain()

        # Close immediately — race the bridge's write-back path.
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

        # Give the server a moment to try to flush the response and observe
        # the disconnect, THEN shutdown.
        with caplog.at_level(logging.ERROR, logger="asyncio"), \
             caplog.at_level(logging.ERROR, logger="ctrlrelay.bridge.server"):
            await asyncio.sleep(0.3)
            await server.stop()
            task.cancel()

        # Must not have logged an unhandled ERROR-level traceback.
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, (
            f"bridge logged ERROR records during client-disconnect race: "
            f"{[r.getMessage() for r in errors]}"
        )
