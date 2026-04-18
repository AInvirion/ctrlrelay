"""Tests for observability / structured logging helpers."""

from __future__ import annotations

import io
import json
import logging

import pytest


class TestObsModule:
    def test_get_logger_returns_namespaced_logger(self) -> None:
        from dev_sync.core.obs import get_logger

        logger = get_logger("transport.socket")
        assert logger.name == "dev_sync.transport.socket"

    def test_configure_logging_is_idempotent(self) -> None:
        from dev_sync.core.obs import configure_logging

        configure_logging()
        root = logging.getLogger("dev_sync")
        count = len(root.handlers)
        configure_logging()
        assert len(root.handlers) == count

    def test_log_event_emits_json_with_fields(self) -> None:
        from dev_sync.core.obs import JSONFormatter, log_event

        logger = logging.getLogger("dev_sync.test_event")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.propagate = False

        log_event(
            logger,
            "dev.question.posted",
            session_id="s1",
            repo="owner/repo",
            issue_number=42,
        )

        line = buf.getvalue().strip()
        payload = json.loads(line)
        assert payload["event"] == "dev.question.posted"
        assert payload["session_id"] == "s1"
        assert payload["repo"] == "owner/repo"
        assert payload["issue_number"] == 42
        assert payload["level"] == "INFO"
        assert "ts" in payload

    def test_hash_text_is_stable(self) -> None:
        from dev_sync.core.obs import hash_text

        assert hash_text("hello") == hash_text("hello")
        assert hash_text("hello") != hash_text("world")
        # Hash should be short enough for log lines
        assert 8 <= len(hash_text("hello")) <= 32


class TestSocketTransportLogging:
    @pytest.mark.asyncio
    async def test_ask_logs_question_posted_and_answer_received(
        self, caplog
    ) -> None:
        """SocketTransport.ask should emit question.posted then answer.received."""
        import asyncio
        import shutil
        import tempfile
        from pathlib import Path

        from dev_sync.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from dev_sync.transports.socket_client import SocketTransport

        # Short tmpdir to stay under AF_UNIX 104-char limit on macOS.
        tmp_dir = Path(tempfile.mkdtemp())
        socket_path = tmp_dir / "t.sock"

        # Stand up a stub server that answers ASK messages with ANSWER.
        async def handle(reader, writer):
            try:
                line = await reader.readline()
                if not line:
                    return
                from dev_sync.bridge.protocol import parse_message

                msg = parse_message(line.decode())
                if msg.op == BridgeOp.ASK:
                    resp = BridgeMessage(
                        op=BridgeOp.ANSWER,
                        request_id=msg.request_id,
                        answer="yes",
                    )
                    writer.write(serialize_message(resp).encode())
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_unix_server(handle, path=str(socket_path))
        try:
            transport = SocketTransport(socket_path)
            await transport.connect()

            caplog.clear()
            with caplog.at_level(logging.INFO, logger="dev_sync"):
                answer = await transport.ask(
                    "Proceed?",
                    options=["yes", "no"],
                    timeout=5,
                    session_id="dev-owner-repo-42-abc",
                    repo="owner/repo",
                    issue_number=42,
                )

            assert answer == "yes"

            events = [r for r in caplog.records if r.name.startswith("dev_sync")]
            names = [r.getMessage() for r in events]
            assert "dev.question.posted" in names
            assert "dev.answer.received" in names

            posted = next(r for r in events if r.getMessage() == "dev.question.posted")
            assert posted.__dict__["session_id"] == "dev-owner-repo-42-abc"
            assert posted.__dict__["repo"] == "owner/repo"
            assert posted.__dict__["issue_number"] == 42
            assert posted.__dict__["transport"] == "socket"

            received = next(
                r for r in events if r.getMessage() == "dev.answer.received"
            )
            assert received.__dict__["session_id"] == "dev-owner-repo-42-abc"
            assert received.__dict__["repo"] == "owner/repo"
            assert received.__dict__["issue_number"] == 42
            assert "elapsed_ms" in received.__dict__

            await transport.close()
        finally:
            server.close()
            await server.wait_closed()
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestFileMockTransportLogging:
    @pytest.mark.asyncio
    async def test_ask_logs_question_and_answer(self, tmp_path, caplog) -> None:
        from dev_sync.transports.file_mock import FileMockTransport

        inbox = tmp_path / "inbox.txt"
        outbox = tmp_path / "outbox.txt"
        inbox.write_text("sure\n")
        outbox.touch()

        transport = FileMockTransport(inbox=inbox, outbox=outbox)

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="dev_sync"):
            answer = await transport.ask(
                "Go?",
                timeout=5,
                session_id="s-1",
                repo="o/r",
                issue_number=7,
            )

        assert answer == "sure"

        events = [r for r in caplog.records if r.name.startswith("dev_sync")]
        names = [r.getMessage() for r in events]
        assert "dev.question.posted" in names
        assert "dev.answer.received" in names

        posted = next(r for r in events if r.getMessage() == "dev.question.posted")
        assert posted.__dict__["session_id"] == "s-1"
        assert posted.__dict__["transport"] == "file_mock"


class TestBridgeServerLogging:
    @pytest.mark.asyncio
    async def test_ask_op_logs_question_posted_with_chat_id(self, caplog) -> None:
        """BridgeServer should log dev.question.posted when ASK op is handled."""
        import asyncio
        import shutil
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from dev_sync.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from dev_sync.bridge.server import BridgeServer

        # Short path to avoid AF_UNIX limit on macOS
        d = tempfile.mkdtemp()
        socket_path = Path(d) / "b.sock"

        mock_handler = AsyncMock()
        mock_handler.ask = AsyncMock(return_value=99)

        try:
            with patch(
                "dev_sync.bridge.server.TelegramHandler", return_value=mock_handler
            ):
                server = BridgeServer(
                    socket_path=socket_path, bot_token="x", chat_id=12345
                )
                task = asyncio.create_task(server.start())
                await asyncio.sleep(0.1)

                reader, writer = await asyncio.open_unix_connection(str(socket_path))

                caplog.clear()
                with caplog.at_level(logging.INFO, logger="dev_sync"):
                    ask = serialize_message(
                        BridgeMessage(
                            op=BridgeOp.ASK,
                            request_id="r-1",
                            question="Continue?",
                            session_id="dev-o-r-1-a",
                            repo="o/r",
                            issue_number=1,
                        )
                    )
                    writer.write(ask.encode())
                    await writer.drain()
                    # Wait for ACK
                    await asyncio.wait_for(reader.readline(), timeout=1)

                events = [
                    r
                    for r in caplog.records
                    if r.name.startswith("dev_sync.bridge")
                ]
                posted = [
                    r for r in events if r.getMessage() == "dev.question.posted"
                ]
                assert posted, (
                    f"expected dev.question.posted, got: "
                    f"{[r.getMessage() for r in events]}"
                )

                record = posted[0]
                assert record.__dict__["session_id"] == "dev-o-r-1-a"
                assert record.__dict__["repo"] == "o/r"
                assert record.__dict__["issue_number"] == 1
                assert record.__dict__["transport"] == "telegram"
                assert record.__dict__["destination"] == "telegram:chat=12345"

                writer.close()
                await writer.wait_closed()
                await server.stop()
                task.cancel()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestSessionResumeLogging:
    @pytest.mark.asyncio
    async def test_dev_pipeline_resume_logs_session_resumed(
        self, tmp_path, caplog
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus
        from dev_sync.core.dispatcher import SessionResult
        from dev_sync.pipelines.base import PipelineContext
        from dev_sync.pipelines.dev import DevPipeline

        dispatcher = MagicMock()
        dispatcher.spawn_session = AsyncMock(
            return_value=SessionResult(
                session_id="dev-o-r-3-abc",
                exit_code=0,
                state=CheckpointState(
                    status=CheckpointStatus.DONE,
                    session_id="dev-o-r-3-abc",
                    summary="done",
                ),
            )
        )

        pipeline = DevPipeline(
            dispatcher=dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        ctx = PipelineContext(
            session_id="dev-o-r-3-abc",
            repo="o/r",
            worktree_path=tmp_path,
            context_path=tmp_path / "ctx",
            state_file=tmp_path / "state.json",
            issue_number=3,
        )

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="dev_sync"):
            await pipeline.resume(ctx, "proceed")

        events = [r for r in caplog.records if r.name.startswith("dev_sync")]
        resumed = [r for r in events if r.getMessage() == "dev.session.resumed"]
        assert resumed, f"expected dev.session.resumed, got: {[r.getMessage() for r in events]}"
        r = resumed[0]
        assert r.__dict__["session_id"] == "dev-o-r-3-abc"
        assert r.__dict__["repo"] == "o/r"
        assert r.__dict__["issue_number"] == 3
        assert r.__dict__["pipeline"] == "dev"
