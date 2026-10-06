"""Tests for bridge server."""

import asyncio
import os
import shutil
import sqlite3
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


class TestQuestionHeader:
    """A secops sweep posts one question per repo back-to-back and the agent
    text rarely names its own repo. Without a header the operator can't tell
    twelve near-identical "approve #387?" messages apart, and never sees the
    session_id the orphan router asks them to quote."""

    def test_header_carries_repo_and_session(self) -> None:
        from ctrlrelay.bridge.server import format_question

        out = format_question(
            "Approve merging #387?",
            repo="AInvirion/aiproxyguard-cloud",
            session_id="secops-AInvirion-aiproxyguard-cloud-8eb03a2b",
        )

        assert out.startswith("[AInvirion/aiproxyguard-cloud]")
        assert "secops-AInvirion-aiproxyguard-cloud-8eb03a2b" in out
        assert out.endswith("Approve merging #387?")

    def test_header_includes_issue_number_for_dev(self) -> None:
        from ctrlrelay.bridge.server import format_question

        out = format_question(
            "Which approach?", repo="owner/r", session_id="dev-x", issue_number=141
        )

        assert "issue #141" in out

    def test_header_omitted_when_no_correlation_metadata(self) -> None:
        """Callers that send neither repo nor session (bare ASK) must get
        their text through untouched — no empty bracket line."""
        from ctrlrelay.bridge.server import format_question

        assert format_question("bare question?") == "bare question?"

    def test_header_is_plain_text(self) -> None:
        """TelegramHandler sends without parse_mode, so Markdown in the
        header would reach the operator as literal punctuation."""
        from ctrlrelay.bridge.server import format_question

        out = format_question("q?", repo="owner/r", session_id="s-1")

        assert "`" not in out
        assert "*" not in out
        assert "_" not in out


class TestReplyRoutingIsStrict:
    """The old FIFO fallback answered the OLDEST live question whenever it
    couldn't match the reply — so replying to the third question while the
    first was still open silently answered the first."""

    @pytest.fixture
    def socket_path(self):
        d = tempfile.mkdtemp()
        yield Path(d) / "b.sock"
        shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_fresh_message_refuses_when_several_questions_live(
        self, socket_path,
    ) -> None:
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.ask = AsyncMock(side_effect=[111, 222])  # type: ignore[attr-defined]
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            for rid, q in [("r-1", "first"), ("r-2", "second")]:
                writer.write(serialize_message(BridgeMessage(
                    op=BridgeOp.ASK, request_id=rid, question=q,
                )).encode())
                await writer.drain()
                await asyncio.wait_for(reader.readline(), timeout=1)  # ACK

            await server._on_telegram_reply("yes", reply_to_message_id=None)

            # No ANSWER may be delivered to either question.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(reader.readline(), timeout=0.3)
            assert set(server._pending_questions) == {"r-1", "r-2"}

            server._telegram.send.assert_awaited_once()  # type: ignore[attr-defined]
            notice = server._telegram.send.await_args.args[0]  # type: ignore[attr-defined]
            assert "wasn't routed" in notice
            assert "reply" in notice.lower()
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
            task.cancel()

    @pytest.mark.asyncio
    async def test_reply_to_expired_question_does_not_hit_the_live_one(
        self, socket_path, tmp_path,
    ) -> None:
        """Operator replies to a question whose ASK already timed out while a
        different repo's question is live. FIFO used to hand that answer to
        the live one — the wrong repo."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume(
            session_id="secops-owner-expired-1111",
            pipeline="secops",
            repo="owner/expired",
            question="merge #60?",
        )

        server = BridgeServer(
            socket_path=socket_path, bot_token="test", chat_id=123, state_db=db,
        )
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.ask = AsyncMock(return_value=999)  # type: ignore[attr-defined]
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]

        # The expired question is only remembered in the msg_id -> session map.
        async with server._pending_lock:
            server._remember_asked_session(111, "secops-owner-expired-1111")

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            writer.write(serialize_message(BridgeMessage(
                op=BridgeOp.ASK, request_id="r-live", question="live one",
                repo="owner/live", session_id="secops-owner-live-9999",
            )).encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=1)  # ACK

            await server._on_telegram_reply("hold it", reply_to_message_id=111)

            # The live question must NOT have been answered.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(reader.readline(), timeout=0.3)
            assert "r-live" in server._pending_questions

            # The expired session got the answer instead.
            rows = db.list_pending_resumes_to_execute()
            assert [r["session_id"] for r in rows] == ["secops-owner-expired-1111"]
            assert rows[0]["answer"] == "hold it"
        finally:
            writer.close()
            await writer.wait_closed()
            db.close()
            await server.stop()
            task.cancel()

    @pytest.mark.asyncio
    async def test_reply_to_picks_the_right_row_among_many_blocked(
        self, socket_path, tmp_path,
    ) -> None:
        """Twelve BLOCKED sessions used to make every late reply 'ambiguous',
        and the session_id it told the operator to quote had never been sent
        to them. A reply-to now resolves the session exactly."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        for i in range(3):
            db.add_pending_resume(
                session_id=f"secops-owner-r{i}-abc{i}",
                pipeline="secops",
                repo=f"owner/r{i}",
                question=f"approve #{i}?",
            )

        server = BridgeServer(
            socket_path=socket_path, bot_token="test", chat_id=123, state_db=db,
        )
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]

        async with server._pending_lock:
            server._remember_asked_session(500, "secops-owner-r1-abc1")

        await server._on_telegram_reply("approve it", reply_to_message_id=500)

        rows = db.list_pending_resumes_to_execute()
        assert [r["session_id"] for r in rows] == ["secops-owner-r1-abc1"]
        assert rows[0]["answer"] == "approve it"

        notice = server._telegram.send.await_args.args[0]  # type: ignore[attr-defined]
        assert "Answer queued" in notice

        db.close()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_asked_sessions_map_is_bounded(self, socket_path) -> None:
        """The map is a routing convenience, not a log — it must not grow
        without limit in a bridge that runs for weeks."""
        from ctrlrelay.bridge.server import _ASKED_SESSIONS_MAX, BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)

        async with server._pending_lock:
            for i in range(_ASKED_SESSIONS_MAX + 25):
                server._remember_asked_session(i, f"s-{i}")

        assert len(server._asked_sessions) == _ASKED_SESSIONS_MAX
        # Oldest evicted, newest kept.
        assert 0 not in server._asked_sessions
        assert (_ASKED_SESSIONS_MAX + 24) in server._asked_sessions

    @pytest.mark.asyncio
    async def test_ask_posts_question_with_header(self, socket_path) -> None:
        """End-to-end: the text handed to Telegram carries the header, not
        just the raw agent question."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.ask = AsyncMock(return_value=777)  # type: ignore[attr-defined]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            writer.write(serialize_message(BridgeMessage(
                op=BridgeOp.ASK, request_id="r-1", question="approve #387?",
                repo="owner/r", session_id="secops-owner-r-abc",
            )).encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=1)

            posted = server._telegram.ask.await_args.args[0]  # type: ignore[attr-defined]
            assert "[owner/r]" in posted
            assert "secops-owner-r-abc" in posted
            assert "approve #387?" in posted

            # And the session is recoverable by the msg_id Telegram returned.
            assert server._asked_sessions[777] == "secops-owner-r-abc"
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
            task.cancel()

    @pytest.mark.asyncio
    async def test_unknown_reply_to_points_at_live_questions_not_dead_end(
        self, socket_path,
    ) -> None:
        """Reply-to names a message the bridge no longer tracks (restart, or
        evicted) and nothing is persisted — but questions ARE live. Telling
        the operator "no active session is waiting" would be flatly wrong and
        would stop them retrying."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.ask = AsyncMock(return_value=900)  # type: ignore[attr-defined]
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            writer.write(serialize_message(BridgeMessage(
                op=BridgeOp.ASK, request_id="r-live", question="live one",
                repo="owner/live", session_id="secops-owner-live-9999",
            )).encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=1)  # ACK

            # No state_db, so the orphan router returns "none".
            await server._on_telegram_reply("yes", reply_to_message_id=4242)

            notice = server._telegram.send.await_args.args[0]  # type: ignore[attr-defined]
            assert "no active session is waiting" not in notice
            assert "Still waiting" in notice
            # Named the way the operator sees it, not by internal ids.
            assert "owner/live" in notice
            assert "secops-owner-live-9999" in notice
            # Not by internal plumbing the operator can't see or act on.
            assert "telegram msg id" not in notice
            assert "900" not in notice  # the telegram_msg_id
            # And the live question was not answered by accident.
            assert "r-live" in server._pending_questions
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
            task.cancel()

    @pytest.mark.asyncio
    async def test_pending_lock_is_free_while_notices_are_sent(
        self, socket_path,
    ) -> None:
        """A stalled Telegram send must not serialize reply routing. The
        notice paths used to await the API with _pending_lock held, so one
        slow send blocked every other reply and every ASK registration."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.ask = AsyncMock(side_effect=[111, 222])  # type: ignore[attr-defined]

        lock_was_free = asyncio.Event()

        async def stalled_send(_text):
            # If the lock were still held, this would hang forever.
            async with server._pending_lock:
                lock_was_free.set()

        server._telegram.send = AsyncMock(side_effect=stalled_send)  # type: ignore[attr-defined]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            for rid, q in [("r-1", "first"), ("r-2", "second")]:
                writer.write(serialize_message(BridgeMessage(
                    op=BridgeOp.ASK, request_id=rid, question=q,
                )).encode())
                await writer.drain()
                await asyncio.wait_for(reader.readline(), timeout=1)

            # Two live questions + plain message -> ambiguous notice path.
            await asyncio.wait_for(
                server._on_telegram_reply("yes", reply_to_message_id=None),
                timeout=2,
            )
            assert lock_was_free.is_set()
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
            task.cancel()

    @pytest.mark.asyncio
    async def test_notice_failure_does_not_break_routing(
        self, socket_path, tmp_path,
    ) -> None:
        """A Telegram outage while acknowledging an orphan answer must not
        lose the answer — it is already committed to pending_resumes."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume(
            session_id="secops-owner-r-abc",
            pipeline="secops",
            repo="owner/r",
            question="merge?",
        )

        server = BridgeServer(
            socket_path=socket_path, bot_token="test", chat_id=123, state_db=db,
        )
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.send = AsyncMock(  # type: ignore[attr-defined]
            side_effect=RuntimeError("telegram down")
        )

        await server._on_telegram_reply("merge it", reply_to_message_id=None)

        rows = db.list_pending_resumes_to_execute()
        assert rows[0]["answer"] == "merge it"

        db.close()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_expired_question_falls_through_to_the_persisted_row(
        self, socket_path, tmp_path,
    ) -> None:
        """The pipeline gives up on ask() after `timeout` and writes a
        pending_resumes row, but it does NOT close the socket — the secops
        sweep reuses one transport across every repo. The bridge therefore
        still held the question, matched a later reply-to against it, and
        wrote the ANSWER into a socket whose client had already discarded
        the request_id. The answer vanished with no log and no notice: the
        operator replied exactly as instructed and nothing ever happened."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume(
            session_id="secops-owner-r-abc",
            pipeline="secops",
            repo="owner/r",
            question="merge #60?",
        )

        server = BridgeServer(
            socket_path=socket_path, bot_token="test", chat_id=123, state_db=db,
        )
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.ask = AsyncMock(return_value=555)  # type: ignore[attr-defined]
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            writer.write(serialize_message(BridgeMessage(
                op=BridgeOp.ASK, request_id="r-1", question="merge #60?",
                repo="owner/r", session_id="secops-owner-r-abc",
                timeout=1,
            )).encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=1)  # ACK

            # The pipeline's ask() has now given up; the socket stays open.
            await asyncio.sleep(1.2)

            await server._on_telegram_reply("merge it", reply_to_message_id=555)

            # Nothing may be written into the dead request.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(reader.readline(), timeout=0.3)

            # The answer landed on the persisted row instead.
            rows = db.list_pending_resumes_to_execute()
            assert [r["session_id"] for r in rows] == ["secops-owner-r-abc"]
            assert rows[0]["answer"] == "merge it"
        finally:
            writer.close()
            await writer.wait_closed()
            db.close()
            await server.stop()
            task.cancel()

    @pytest.mark.asyncio
    async def test_expired_questions_do_not_make_a_live_one_ambiguous(
        self, socket_path,
    ) -> None:
        """Stale entries used to inflate the >1 count, so a plain answer to
        the single genuinely-live question was refused and the operator was
        shown a listing of ghosts."""
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
        server._telegram.ask = AsyncMock(side_effect=[111, 222])  # type: ignore[attr-defined]
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            writer.write(serialize_message(BridgeMessage(
                op=BridgeOp.ASK, request_id="r-stale", question="old", timeout=1,
            )).encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=1)

            await asyncio.sleep(1.2)

            writer.write(serialize_message(BridgeMessage(
                op=BridgeOp.ASK, request_id="r-live", question="new", timeout=600,
            )).encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=1)

            # Plain message: only one question is really live, so it routes.
            await server._on_telegram_reply("yes", reply_to_message_id=None)

            raw = await asyncio.wait_for(reader.readline(), timeout=1)
            answer = parse_message(raw.decode())
            assert answer.op == BridgeOp.ANSWER
            assert answer.request_id == "r-live"
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
            task.cancel()

    @pytest.mark.asyncio
    async def test_send_with_session_is_answerable_by_reply(
        self, socket_path, tmp_path,
    ) -> None:
        """The sweep fans out a 'blocked on <repo>' SEND after the run, and
        that message is the most recent one the operator sees for the repo.
        Its Telegram id was never recorded, so replying to it landed in the
        ambiguous branch even though the session was unmistakable."""
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        for i in range(3):
            db.add_pending_resume(
                session_id=f"secops-owner-r{i}-abc{i}",
                pipeline="secops",
                repo=f"owner/r{i}",
                question=f"approve #{i}?",
            )

        server = BridgeServer(
            socket_path=socket_path, bot_token="test", chat_id=123, state_db=db,
        )
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.send = AsyncMock(return_value=321)  # type: ignore[attr-defined]

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            writer.write(serialize_message(BridgeMessage(
                op=BridgeOp.SEND, request_id="r-fanout",
                text="⏸️ Scheduled secops blocked on owner/r2",
                session_id="secops-owner-r2-abc2", repo="owner/r2",
            )).encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=1)  # ACK

            assert server._asked_sessions[321] == "secops-owner-r2-abc2"

            await server._on_telegram_reply("approve it", reply_to_message_id=321)

            rows = db.list_pending_resumes_to_execute()
            assert [r["session_id"] for r in rows] == ["secops-owner-r2-abc2"]
            assert rows[0]["answer"] == "approve it"
        finally:
            writer.close()
            await writer.wait_closed()
            db.close()
            await server.stop()
            task.cancel()


class TestQuestionDeadline:
    """The bridge's view of "still live" must never outlast the client's, or
    it writes the answer into a request nobody is waiting on."""

    def test_no_timeout_means_no_deadline(self) -> None:
        from ctrlrelay.bridge.server import _deadline

        assert _deadline(1000.0, None) is None

    def test_deadline_tracks_the_client_with_no_margin(self) -> None:
        """No safety margin either way. Expiring early is not free: a reply
        in the gap is routed to a pending_resumes row the still-running
        pipeline has not written yet, so it is refused and the operator has
        to send it again."""
        from ctrlrelay.bridge.server import _deadline

        received_at, timeout = 1000.0, 900

        assert _deadline(received_at, timeout) == received_at + timeout

    def test_a_reply_just_before_the_deadline_still_reaches_the_pipeline(
        self,
    ) -> None:
        from ctrlrelay.bridge.server import _deadline, _PendingQuestion

        received_at, timeout = 1000.0, 900
        q = _PendingQuestion(
            request_id="r", telegram_msg_id=1, writer=None,  # type: ignore[arg-type]
            expires_at=_deadline(received_at, timeout),
        )

        assert not q.is_expired(received_at + timeout - 2)
        assert q.is_expired(received_at + timeout)

    def test_non_positive_timeout_is_already_expired_not_eternal(self) -> None:
        """`if msg.timeout` would read 0 as falsy and give the question no
        deadline at all — the opposite of what a 0 timeout means."""
        from ctrlrelay.bridge.server import _deadline, _PendingQuestion

        for timeout in (0, -1):
            deadline = _deadline(1000.0, timeout)
            assert deadline == 1000.0
            q = _PendingQuestion(
                request_id="r", telegram_msg_id=1, writer=None,  # type: ignore[arg-type]
                expires_at=deadline,
            )
            assert q.is_expired(1000.0)

    def test_short_timeout_is_honoured_not_collapsed(self) -> None:
        """A small explicit timeout must still get its full window."""
        from ctrlrelay.bridge.server import _deadline

        assert _deadline(1000.0, 1) == 1001.0
        assert _deadline(1000.0, 5) == 1005.0


class TestStaleReplyToDoesNotMisroute:
    """A reply-to that resolves to a session which cannot take an answer
    must route nothing — never fall back to "there is only one row left,
    it must be that one".

    The bug these cover: `list_unanswered_pending_resumes` filters on
    `answered_at IS NULL`, so an already-answered session is simply absent
    from `rows`. The hinted lookup came back empty, control fell through to
    `elif len(rows) == 1`, and the operator's answer to session A was
    attached to session B. Nothing double-resumed, because
    `answer_pending_resume` is guarded — B just received an answer to a
    question it never asked, silently, and the sweeper acted on it.
    """

    @pytest.fixture
    def socket_path(self):
        d = tempfile.mkdtemp()
        yield Path(d) / "b.sock"
        shutil.rmtree(d, ignore_errors=True)

    async def _server_with_blocked(self, socket_path, tmp_path, *, extra=False):
        """Session A (pointed at by the reply-to) plus B, optionally plus C.

        ``extra`` adds C so that B is NOT the only row left once A is out
        of the running. Without it, a test asserting "the typed id routed"
        is indistinguishable from the single-row guess picking B anyway.
        """
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume(
            session_id="secops-owner-a-aaa",
            pipeline="secops",
            repo="owner/a",
            question="merge the alembic bump?",
        )
        db.add_pending_resume(
            session_id="secops-owner-b-bbb",
            pipeline="secops",
            repo="owner/b",
            question="merge the torch bump?",
        )
        server = BridgeServer(
            socket_path=socket_path, bot_token="test", chat_id=123, state_db=db,
        )
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]
        if extra:
            db.add_pending_resume(
                session_id="secops-owner-c-ccc",
                pipeline="secops",
                repo="owner/c",
                question="merge the mypy bump?",
            )
        # The operator is replying to the message we posted for session A.
        server._asked_sessions[999] = "secops-owner-a-aaa"
        return server, db, task

    @pytest.mark.asyncio
    async def test_reply_to_already_answered_session_leaves_the_other_alone(
        self, socket_path, tmp_path,
    ) -> None:
        server, db, task = await self._server_with_blocked(socket_path, tmp_path)

        # A has been answered already — by an earlier reply, or by the same
        # answer arriving twice from two chat clients.
        assert db.answer_pending_resume("secops-owner-a-aaa", "approved") is True

        await server._on_telegram_reply("approved", reply_to_message_id=999)

        b = db.get_pending_resume("secops-owner-b-bbb")
        assert b["answer"] is None, "B was given an answer meant for A"
        assert b["answered_at"] is None
        assert b["resumed_at"] is None

        # Only A's own answer is queued for the sweeper, not a second row.
        queued = db.list_pending_resumes_to_execute()
        assert [r["session_id"] for r in queued] == ["secops-owner-a-aaa"]

        sent = server._telegram.send.await_args.args[0]  # type: ignore[attr-defined]
        assert "already been answered" in sent
        assert "secops-owner-a-aaa" in sent

        db.close()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_reply_to_expired_session_leaves_the_other_alone(
        self, socket_path, tmp_path,
    ) -> None:
        """Same fall-through, reached via expiry instead of an answer.

        `expire_pending_resume` also removes a row from the unanswered
        listing, so a reply to a question retired by `question_ttl_seconds`
        hit the identical guess.
        """
        server, db, task = await self._server_with_blocked(socket_path, tmp_path)

        assert db.expire_pending_resume("secops-owner-a-aaa", "ttl") is True

        await server._on_telegram_reply("approved", reply_to_message_id=999)

        b = db.get_pending_resume("secops-owner-b-bbb")
        assert b["answer"] is None, "B was given an answer meant for expired A"
        assert b["answered_at"] is None
        assert db.list_pending_resumes_to_execute() == []

        sent = server._telegram.send.await_args.args[0]  # type: ignore[attr-defined]
        assert "expired" in sent

        db.close()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_a_typed_session_id_still_routes_when_the_hint_is_stale(
        self, socket_path, tmp_path,
    ) -> None:
        """The guard must refuse a GUESS, not an intention.

        Replying to the wrong message in a busy chat is an easy slip; typing
        a session_id is not. So when the reply-to is stale but the operator
        named another session explicitly, that answer still lands — the fix
        must not turn a deliberate answer into a refusal.

        THREE sessions on purpose. With only A and B, answering A leaves B
        as the single unanswered row, so the old `len(rows) == 1` guess
        would also have landed on B and this test would pass without the
        typed id mattering at all. C makes the typed id the only thing that
        can select B: guessing is no longer available, and the alternative
        outcome is a refusal.
        """
        server, db, task = await self._server_with_blocked(
            socket_path, tmp_path, extra=True,
        )

        assert db.answer_pending_resume("secops-owner-a-aaa", "approved") is True

        await server._on_telegram_reply(
            "secops-owner-b-bbb yes merge it", reply_to_message_id=999,
        )

        b = db.get_pending_resume("secops-owner-b-bbb")
        assert b["answer"] == "secops-owner-b-bbb yes merge it"
        assert b["answered_at"] is not None

        # C was never a candidate and must be untouched.
        c = db.get_pending_resume("secops-owner-c-ccc")
        assert c["answer"] is None
        assert c["answered_at"] is None

        sent = server._telegram.send.await_args.args[0]  # type: ignore[attr-defined]
        assert "Answer queued" in sent

        db.close()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_a_longer_session_id_is_not_read_as_naming_a_shorter_one(
        self, socket_path, tmp_path,
    ) -> None:
        """Naming one session must not route a different one by prefix.

        `session_id in text` is a substring test, not an identifier match.
        A reply naming `...-bbb2` also contains `...-bbb`, so the shorter
        row would claim an answer written for the longer one — the very
        wrong-session routing this change exists to stop, reintroduced
        through the branch that is supposed to honour an explicit id.
        """
        from unittest.mock import AsyncMock

        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.core.state import StateDB

        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume(
            session_id="secops-owner-b-bbb",
            pipeline="secops",
            repo="owner/b",
            question="merge the torch bump?",
        )
        db.add_pending_resume(
            session_id="secops-owner-c-ccc",
            pipeline="secops",
            repo="owner/c",
            question="merge the mypy bump?",
        )
        server = BridgeServer(
            socket_path=socket_path, bot_token="test", chat_id=123, state_db=db,
        )
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        server._telegram.send = AsyncMock()  # type: ignore[attr-defined]

        # Names a session that is not in the table at all, but whose id
        # has the live `...-bbb` as a prefix.
        await server._on_telegram_reply(
            "secops-owner-b-bbb2 approved", reply_to_message_id=None,
        )

        b = db.get_pending_resume("secops-owner-b-bbb")
        assert b["answer"] is None, "a prefix match claimed another id's answer"
        assert b["answered_at"] is None
        assert db.list_pending_resumes_to_execute() == []

        db.close()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_a_prefix_is_not_matched_on_the_stale_reply_path_either(
        self, socket_path, tmp_path,
    ) -> None:
        """The same prefix guard, on the branch this change actually added.

        The sibling test above sends `reply_to_message_id=None`, which
        reaches the pre-existing `matched_by_id` call site. It therefore
        says nothing about the `named` call site inside the stale-hint
        branch — a regression to substring matching there would pass every
        other test here. This drives the stale-reply path explicitly: A is
        answered and mapped to the replied-to message, B and C are live,
        and the text names a longer id that merely has B's as a prefix.
        """
        server, db, task = await self._server_with_blocked(
            socket_path, tmp_path, extra=True,
        )

        assert db.answer_pending_resume("secops-owner-a-aaa", "approved") is True

        await server._on_telegram_reply(
            "secops-owner-b-bbb2 approved", reply_to_message_id=999,
        )

        b = db.get_pending_resume("secops-owner-b-bbb")
        assert b["answer"] is None, "a prefix match claimed another id's answer"
        assert b["answered_at"] is None
        c = db.get_pending_resume("secops-owner-c-ccc")
        assert c["answer"] is None
        # Only A's deliberate answer is queued; no reply was routed.
        assert [r["session_id"] for r in db.list_pending_resumes_to_execute()] == [
            "secops-owner-a-aaa"
        ]

        db.close()
        await server.stop()
        task.cancel()

    def test_every_ascii_character_is_classified_correctly(self) -> None:
        """Exhaustive over ASCII, because ASCII is a closed set.

        Four review rounds went by adding one more invisible character to a
        list. This asserts the whole half of the problem that can be
        asserted whole: all 128 code points, boundary iff outside the id's
        own alphabet. It cannot be made stale by Unicode growing.

        The backtick is the case that made this necessary — the bridge's
        own notices wrap session ids in backticks, and an earlier rule
        based on Unicode punctuation classified it as part of the
        identifier, so a quoted id stopped routing.
        """
        import string

        from ctrlrelay.bridge.server import names_session

        sid = "secops-owner-b-bbb"
        alphabet = string.ascii_letters + string.digits + "-_"
        for code in range(128):
            ch = chr(code)
            separates = ch not in alphabet
            assert names_session(f"{sid}{ch}x", sid) is separates, (
                f"U+{code:04X} {ch!r} after the id"
            )
            assert names_session(f"x{ch}{sid} y", sid) is separates, (
                f"U+{code:04X} {ch!r} before the id"
            )

    def test_an_unrecognised_character_fails_toward_refusing(self) -> None:
        """Outside ASCII, anything we do not recognise as a separator is
        treated as part of the identifier.

        The direction is the point. Refusing to route tells the operator so
        and costs them a retry; matching on a guess about a character
        nobody can see puts an answer on a session that never asked, and
        the sweeper acts on it. These are the characters four rounds of
        review produced one at a time — they are covered by the policy
        rather than by being listed.
        """
        from ctrlrelay.bridge.server import names_session

        sid = "secops-owner-b-bbb"
        for label, ch in (
            ("ZERO WIDTH JOINER", "\u200d"),
            ("ZERO WIDTH NON-JOINER", "\u200c"),
            ("VARIATION SELECTOR-16", "\ufe0f"),
            ("COMBINING ENCLOSING KEYCAP", "\u20e3"),
            ("COMBINING ACUTE", "\u0301"),
            ("COMBINING GRAPHEME JOINER", "\u034f"),
            ("SOFT HYPHEN", "\u00ad"),
            ("BYTE ORDER MARK", "\ufeff"),
            ("BRAILLE PATTERN BLANK", "\u2800"),
            ("EMOJI MODIFIER FITZPATRICK-1-2", "\U0001f3fb"),
        ):
            assert not names_session(f"{sid}{ch} x", sid), f"{label} after"
            assert not names_session(f"x{ch}{sid} y", sid), f"{label} before"

    def test_recognised_non_ascii_separators_still_route(self) -> None:
        """The conservative half must not swallow real separators."""
        from ctrlrelay.bridge.server import names_session

        sid = "secops-owner-b-bbb"
        assert names_session(f"{sid}\u00a0ok", sid)       # NO-BREAK SPACE
        assert names_session(f"\u3001{sid}\u3002", sid)   # CJK comma, stop
        assert names_session(f"\u300c{sid}\u300d", sid)   # CJK quotes
        assert names_session(f"\u2014{sid}\u2014", sid)   # em dashes

    def test_names_session_requires_a_whole_token(self) -> None:
        """Direct coverage of the matcher, including both boundaries."""
        from ctrlrelay.bridge.server import names_session

        sid = "secops-owner-b-bbb"
        assert names_session(f"{sid} approved", sid)
        assert names_session(f"approved {sid}", sid)
        assert names_session(f"re {sid}, merge it", sid)
        assert names_session(f"`{sid}`", sid)
        assert names_session(sid, sid)
        # Longer ids must not match the shorter one, in either direction.
        assert not names_session(f"{sid}2 approved", sid)
        assert not names_session(f"{sid}-x approved", sid)
        assert not names_session(f"{sid}_x approved", sid)
        assert not names_session(f"x{sid} approved", sid)
        # Boundaries must be Unicode-aware: the ids are ASCII, the operator's
        # text is not, and an ASCII-only class ended the id at the accent.
        assert not names_session(f"{sid}\u00e9 approved", sid)
        assert not names_session(f"\u00e9{sid} approved", sid)
        # A combining mark renders as part of the neighbouring glyph but is
        # not a \w character, so the lookaround alone saw a boundary where
        # a reader sees none.
        assert not names_session(f"{sid}\u0301 approved", sid)
        assert not names_session(f"x\u0301{sid} approved", sid)
        # ...and a real occurrence later in the same message still routes,
        # rather than the first near-miss suppressing the whole message.
        assert names_session(f"{sid}\u0301 no wait, {sid} yes", sid)
        # Grapheme extenders with canonical combining class 0. The first
        # attempt used unicodedata.combining(), which reports that class
        # and so read every one of these as a boundary.
        for extender in (
            "\ufe0f",   # VARIATION SELECTOR-16          (Mn)
            "\u20e3",   # COMBINING ENCLOSING KEYCAP     (Me)
            "\u034f",   # COMBINING GRAPHEME JOINER      (Mn)
            "\u200d",   # ZERO WIDTH JOINER              (Cf)
            "\u200c",   # ZERO WIDTH NON-JOINER          (Cf)
            "\u00ad",   # SOFT HYPHEN                    (Cf)
            "\u200f",   # RIGHT-TO-LEFT MARK             (Cf)
            "\ufeff",   # ZERO WIDTH NO-BREAK SPACE/BOM  (Cf)
        ):
            assert not names_session(f"{sid}{extender} approved", sid), extender
            assert not names_session(f"x{extender}{sid} approved", sid), extender
        # Self-overlapping id. Review raised non-overlapping iteration as
        # a false-negative risk; measured, it cannot be one for THIS
        # predicate — an overlapping occurrence is by definition preceded
        # by a character of the first occurrence, which is an identifier
        # character, so it is rejected on its own merits regardless of
        # whether it was ever examined. Both of these are correctly False.
        assert names_session("\u0301abab", "abab") is False
        assert names_session("\u0301ababab", "abab") is False
        # The scan is still every-offset rather than every-match, so that
        # reasoning is not load-bearing. This is the case that needs it: a
        # rejected candidate followed by a genuinely separate one.
        assert names_session("\u0301abab abab", "abab") is True
        # Control characters DO end an identifier — excluded from the C
        # sweep for exactly this reason.
        assert names_session(f"{sid}\n approved", sid)
        assert names_session(f"{sid}\tapproved", sid)
        assert names_session(f"line one\n{sid}", sid)
        # Ordinary punctuation and quoting still read as boundaries.
        for wrapped in (f"({sid})", f'"{sid}"', f"{sid}.", f"{sid}, merge",
                        f"[{sid}]", f"<{sid}>", f"{sid}!"):
            assert names_session(wrapped, sid), wrapped
        assert not names_session("nothing here", sid)
        assert not names_session("anything", "")

    @pytest.mark.asyncio
    async def test_a_lookup_that_fails_says_so_rather_than_claiming_nothing_waits(
        self, socket_path, tmp_path,
    ) -> None:
        """"I could not look" must not be reported as "nothing is there".

        A mutation sweep caught this one: collapsing this reason back into
        `unknown` passed every other test in this class, which means the
        distinction had no test behind it at all. The operator-facing cost
        is a confident false sentence — "no question is waiting" — issued
        on the strength of a database error.
        """
        server, db, task = await self._server_with_blocked(socket_path, tmp_path)

        assert db.answer_pending_resume("secops-owner-a-aaa", "approved") is True

        real_get = db.get_pending_resume

        def boom(_session_id):
            raise sqlite3.OperationalError("database is locked")

        db.get_pending_resume = boom  # type: ignore[method-assign]

        await server._on_telegram_reply("approved", reply_to_message_id=999)

        sent = server._telegram.send.await_args.args[0]  # type: ignore[attr-defined]
        assert "could not be checked" in sent
        assert "no question waiting" not in sent
        # Routed nothing, which is the part that matters. A's own row is
        # queued by this test's setup — that is the deliberate answer, not
        # a misroute — so the assertion is about B.
        b = real_get("secops-owner-b-bbb")
        assert b["answer"] is None
        assert b["answered_at"] is None
        assert [r["session_id"] for r in db.list_pending_resumes_to_execute()] == [
            "secops-owner-a-aaa"
        ]

        db.close()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_a_reopened_session_is_called_superseded_not_unknown(
        self, socket_path, tmp_path,
    ) -> None:
        """A row re-opened between the two reads has a NEWER question.

        The listing is read first, then the hinted row. A session that
        re-blocks in that window is absent from the listing but live in the
        table. Refusing is right — this reply was about the old question —
        but telling the operator no question is waiting would be false,
        because one is.
        """
        server, db, task = await self._server_with_blocked(socket_path, tmp_path)

        # A is live and unanswered in the table, but the listing the router
        # works from does not contain it: exactly the state a concurrent
        # re-block produces between the two reads.
        real_list = db.list_unanswered_pending_resumes
        db.list_unanswered_pending_resumes = lambda: [  # type: ignore[method-assign]
            r for r in real_list() if r["session_id"] != "secops-owner-a-aaa"
        ]

        await server._on_telegram_reply("approved", reply_to_message_id=999)

        sent = server._telegram.send.await_args.args[0]  # type: ignore[attr-defined]
        assert "NEWER question" in sent
        assert "no question waiting" not in sent

        # Neither A's newer question nor B was answered by the old reply.
        assert db.get_pending_resume("secops-owner-a-aaa")["answer"] is None
        assert db.get_pending_resume("secops-owner-b-bbb")["answer"] is None

        db.close()
        await server.stop()
        task.cancel()
