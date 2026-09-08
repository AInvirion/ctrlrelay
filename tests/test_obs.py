"""Tests for observability / structured logging helpers."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestObsModule:
    def test_get_logger_returns_namespaced_logger(self) -> None:
        from ctrlrelay.core.obs import get_logger

        logger = get_logger("transport.socket")
        assert logger.name == "ctrlrelay.transport.socket"

    def test_configure_logging_is_idempotent(self) -> None:
        from ctrlrelay.core.obs import configure_logging

        configure_logging()
        root = logging.getLogger("ctrlrelay")
        count = len(root.handlers)
        configure_logging()
        assert len(root.handlers) == count

    def test_log_event_emits_json_with_fields(self) -> None:
        from ctrlrelay.core.obs import JSONFormatter, log_event

        logger = logging.getLogger("ctrlrelay.test_event")
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
        from ctrlrelay.core.obs import hash_text

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

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.transports.socket_client import SocketTransport

        # Short tmpdir to stay under AF_UNIX 104-char limit on macOS.
        tmp_dir = Path(tempfile.mkdtemp())
        socket_path = tmp_dir / "t.sock"

        # Stand up a stub server that answers ASK messages with ANSWER.
        async def handle(reader, writer):
            try:
                line = await reader.readline()
                if not line:
                    return
                from ctrlrelay.bridge.protocol import parse_message

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
            with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                answer = await transport.ask(
                    "Proceed?",
                    options=["yes", "no"],
                    timeout=5,
                    session_id="dev-owner-repo-42-abc",
                    repo="owner/repo",
                    issue_number=42,
                )

            assert answer == "yes"

            events = [r for r in caplog.records if r.name.startswith("ctrlrelay")]
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
        from ctrlrelay.transports.file_mock import FileMockTransport

        inbox = tmp_path / "inbox.txt"
        outbox = tmp_path / "outbox.txt"
        inbox.write_text("sure\n")
        outbox.touch()

        transport = FileMockTransport(inbox=inbox, outbox=outbox)

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            answer = await transport.ask(
                "Go?",
                timeout=5,
                session_id="s-1",
                repo="o/r",
                issue_number=7,
            )

        assert answer == "sure"

        events = [r for r in caplog.records if r.name.startswith("ctrlrelay")]
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

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.bridge.server import BridgeServer

        # Short path to avoid AF_UNIX limit on macOS
        d = tempfile.mkdtemp()
        socket_path = Path(d) / "b.sock"

        mock_handler = AsyncMock()
        mock_handler.ask = AsyncMock(return_value=99)

        try:
            with patch(
                "ctrlrelay.bridge.server.TelegramHandler", return_value=mock_handler
            ):
                server = BridgeServer(
                    socket_path=socket_path, bot_token="x", chat_id=12345
                )
                task = asyncio.create_task(server.start())
                await asyncio.sleep(0.1)

                reader, writer = await asyncio.open_unix_connection(str(socket_path))

                caplog.clear()
                with caplog.at_level(logging.INFO, logger="ctrlrelay"):
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
                    if r.name.startswith("ctrlrelay.bridge")
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

        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.base import PipelineContext
        from ctrlrelay.pipelines.dev import DevPipeline

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
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            await pipeline.resume(ctx, "proceed")

        events = [r for r in caplog.records if r.name.startswith("ctrlrelay")]
        resumed = [r for r in events if r.getMessage() == "dev.session.resumed"]
        assert resumed, f"expected dev.session.resumed, got: {[r.getMessage() for r in events]}"
        r = resumed[0]
        assert r.__dict__["session_id"] == "dev-o-r-3-abc"
        assert r.__dict__["repo"] == "o/r"
        assert r.__dict__["issue_number"] == 3
        assert r.__dict__["pipeline"] == "dev"


class TestSocketTransportDeliverySemantics:
    """dev.question.posted must never claim a delivery that did not happen."""

    @staticmethod
    async def _stub_server(socket_path, responses):
        """Serve one ASK, replying with each message in ``responses``."""
        import asyncio

        from ctrlrelay.bridge.protocol import parse_message, serialize_message

        async def handle(reader, writer):
            try:
                line = await reader.readline()
                if not line:
                    return
                msg = parse_message(line.decode())
                for build in responses:
                    writer.write(serialize_message(build(msg)).encode())
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        return await asyncio.start_unix_server(handle, path=str(socket_path))

    @pytest.mark.asyncio
    async def test_send_failure_logs_post_failed_and_not_posted(
        self, caplog
    ) -> None:
        """A socket write that never happens must not log a posted question."""
        import tempfile
        from pathlib import Path

        from ctrlrelay.transports.base import TransportError
        from ctrlrelay.transports.socket_client import SocketTransport

        # Never connected: _send_message raises before anything reaches a bridge.
        transport = SocketTransport(Path(tempfile.mkdtemp()) / "absent.sock")

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with pytest.raises(TransportError):
                await transport.ask("Deploy to prod?", timeout=1, session_id="s-1")

        names = [
            r.getMessage()
            for r in caplog.records
            if r.name.startswith("ctrlrelay")
        ]
        assert "dev.question.posted" not in names
        assert "dev.question.post_failed" in names

        failed = next(
            r
            for r in caplog.records
            if r.getMessage() == "dev.question.post_failed"
        )
        assert failed.__dict__["session_id"] == "s-1"
        assert failed.__dict__["transport"] == "socket"
        assert failed.__dict__["reason"] == "send_failed"
        assert "question" not in failed.__dict__

    @pytest.mark.asyncio
    async def test_bridge_error_logs_post_failed_and_not_posted(
        self, caplog
    ) -> None:
        """A bridge that rejects the ASK must not leave a posted event behind."""
        import shutil
        import tempfile
        from pathlib import Path

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp
        from ctrlrelay.transports.base import TransportError
        from ctrlrelay.transports.socket_client import SocketTransport

        tmp_dir = Path(tempfile.mkdtemp())
        server = await self._stub_server(
            tmp_dir / "t.sock",
            [
                lambda m: BridgeMessage(
                    op=BridgeOp.ERROR,
                    request_id=m.request_id,
                    error="telegram_api_error",
                    message="boom",
                )
            ],
        )
        try:
            transport = SocketTransport(tmp_dir / "t.sock")
            await transport.connect()

            caplog.clear()
            with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                with pytest.raises(TransportError):
                    await transport.ask("Deploy?", timeout=5, session_id="s-2")

            names = [
                r.getMessage()
                for r in caplog.records
                if r.name.startswith("ctrlrelay")
            ]
            assert "dev.question.posted" not in names
            assert "dev.question.post_failed" in names

            failed = next(
                r
                for r in caplog.records
                if r.getMessage() == "dev.question.post_failed"
            )
            assert failed.__dict__["reason"] == "bridge_error"

            await transport.close()
        finally:
            server.close()
            await server.wait_closed()
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_timeout_after_ack_keeps_posted_and_adds_no_failure(
        self, caplog
    ) -> None:
        """Once the bridge acknowledges, an unanswered question is still posted."""
        import shutil
        import tempfile
        from pathlib import Path

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp
        from ctrlrelay.transports.base import TransportError
        from ctrlrelay.transports.socket_client import SocketTransport

        tmp_dir = Path(tempfile.mkdtemp())
        server = await self._stub_server(
            tmp_dir / "t.sock",
            [
                lambda m: BridgeMessage(
                    op=BridgeOp.ACK, request_id=m.request_id, status="pending"
                )
            ],
        )
        try:
            transport = SocketTransport(tmp_dir / "t.sock")
            await transport.connect()

            caplog.clear()
            with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                with pytest.raises(TransportError):
                    await transport.ask("Deploy?", timeout=1, session_id="s-3")

            names = [
                r.getMessage()
                for r in caplog.records
                if r.name.startswith("ctrlrelay")
            ]
            assert "dev.question.posted" in names
            assert "dev.question.post_failed" not in names

            await transport.close()
        finally:
            server.close()
            await server.wait_closed()
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_posted_precedes_answer_received(self, caplog) -> None:
        """The ACK is the confirmation, so posted lands before the answer."""
        import shutil
        import tempfile
        from pathlib import Path

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp
        from ctrlrelay.transports.socket_client import SocketTransport

        tmp_dir = Path(tempfile.mkdtemp())
        server = await self._stub_server(
            tmp_dir / "t.sock",
            [
                lambda m: BridgeMessage(
                    op=BridgeOp.ACK, request_id=m.request_id, status="pending"
                ),
                lambda m: BridgeMessage(
                    op=BridgeOp.ANSWER, request_id=m.request_id, answer="yes"
                ),
            ],
        )
        try:
            transport = SocketTransport(tmp_dir / "t.sock")
            await transport.connect()

            caplog.clear()
            with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                assert await transport.ask("Deploy?", timeout=5) == "yes"

            names = [
                r.getMessage()
                for r in caplog.records
                if r.name.startswith("ctrlrelay")
            ]
            assert names.index("dev.question.posted") < names.index(
                "dev.answer.received"
            )

            await transport.close()
        finally:
            server.close()
            await server.wait_closed()
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestBridgeServerDeliverySemantics:
    @pytest.mark.asyncio
    async def test_telegram_failure_logs_post_failed_and_not_posted(
        self, caplog
    ) -> None:
        """TelegramHandler.ask blowing up must not log a posted question."""
        import asyncio
        import shutil
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.bridge.server import BridgeServer

        d = tempfile.mkdtemp()
        socket_path = Path(d) / "b.sock"

        mock_handler = AsyncMock()
        mock_handler.ask = AsyncMock(side_effect=RuntimeError("telegram down"))

        try:
            with patch(
                "ctrlrelay.bridge.server.TelegramHandler", return_value=mock_handler
            ):
                server = BridgeServer(
                    socket_path=socket_path, bot_token="x", chat_id=12345
                )
                task = asyncio.create_task(server.start())
                await asyncio.sleep(0.1)

                reader, writer = await asyncio.open_unix_connection(str(socket_path))

                caplog.clear()
                with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                    writer.write(
                        serialize_message(
                            BridgeMessage(
                                op=BridgeOp.ASK,
                                request_id="r-1",
                                question="Continue?",
                                session_id="dev-o-r-1-a",
                                repo="o/r",
                                issue_number=1,
                            )
                        ).encode()
                    )
                    await writer.drain()
                    await asyncio.wait_for(reader.readline(), timeout=1)

                names = [
                    r.getMessage()
                    for r in caplog.records
                    if r.name.startswith("ctrlrelay.bridge")
                ]
                assert "dev.question.posted" not in names
                assert "dev.question.post_failed" in names

                failed = next(
                    r
                    for r in caplog.records
                    if r.getMessage() == "dev.question.post_failed"
                )
                assert failed.__dict__["session_id"] == "dev-o-r-1-a"
                assert failed.__dict__["transport"] == "telegram"
                assert failed.__dict__["reason"] == "RuntimeError"
                assert "question" not in failed.__dict__

                writer.close()
                await writer.wait_closed()
                await server.stop()
                task.cancel()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_posted_carries_telegram_msg_id(self, caplog) -> None:
        """Posted after the send means the Telegram message id is known."""
        import asyncio
        import shutil
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.bridge.server import BridgeServer

        d = tempfile.mkdtemp()
        socket_path = Path(d) / "b.sock"

        mock_handler = AsyncMock()
        mock_handler.ask = AsyncMock(return_value=99)

        try:
            with patch(
                "ctrlrelay.bridge.server.TelegramHandler", return_value=mock_handler
            ):
                server = BridgeServer(
                    socket_path=socket_path, bot_token="x", chat_id=12345
                )
                task = asyncio.create_task(server.start())
                await asyncio.sleep(0.1)

                reader, writer = await asyncio.open_unix_connection(str(socket_path))

                caplog.clear()
                with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                    writer.write(
                        serialize_message(
                            BridgeMessage(
                                op=BridgeOp.ASK,
                                request_id="r-1",
                                question="Continue?",
                                session_id="dev-o-r-1-a",
                            )
                        ).encode()
                    )
                    await writer.drain()
                    await asyncio.wait_for(reader.readline(), timeout=1)

                posted = next(
                    r
                    for r in caplog.records
                    if r.getMessage() == "dev.question.posted"
                )
                assert posted.__dict__["telegram_msg_id"] == 99

                writer.close()
                await writer.wait_closed()
                await server.stop()
                task.cancel()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestSensitivePayloadsAreNotLogged:
    """Questions and answers are operator content; logs keep hash + length only."""

    # Field names that would carry raw human-entered content into the log
    # stream. ``*_hash`` / ``*_length`` derivatives are the supported path.
    FORBIDDEN_FIELDS = frozenset(
        {"question", "answer", "prompt", "text", "body", "stdout", "stderr"}
    )

    def test_no_log_event_call_passes_raw_sensitive_fields(self) -> None:
        """Static guard: no ``log_event(..., question=...)`` anywhere in src."""
        import ast
        from pathlib import Path

        import ctrlrelay

        src_root = Path(ctrlrelay.__file__).resolve().parent
        offenders: list[str] = []
        for path in sorted(src_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "id", None) != "log_event":
                    continue
                for kw in node.keywords:
                    if kw.arg in self.FORBIDDEN_FIELDS:
                        offenders.append(f"{path.name}:{node.lineno} {kw.arg}=")
        assert not offenders, (
            "log_event must not carry raw sensitive payloads; "
            f"use hash_text()/len() instead: {offenders}"
        )

    @pytest.mark.asyncio
    async def test_file_mock_logs_hash_not_plaintext(
        self, tmp_path, caplog
    ) -> None:
        from ctrlrelay.core.obs import hash_text
        from ctrlrelay.transports.file_mock import FileMockTransport

        inbox = tmp_path / "inbox.txt"
        outbox = tmp_path / "outbox.txt"
        inbox.write_text("ship it\n")
        outbox.touch()

        transport = FileMockTransport(inbox=inbox, outbox=outbox)

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            assert await transport.ask("Secret plan?", timeout=5) == "ship it"

        posted = next(
            r for r in caplog.records if r.getMessage() == "dev.question.posted"
        )
        assert "question" not in posted.__dict__
        assert posted.__dict__["question_hash"] == hash_text("Secret plan?")
        assert posted.__dict__["question_length"] == len("Secret plan?")

        received = next(
            r for r in caplog.records if r.getMessage() == "dev.answer.received"
        )
        assert "answer" not in received.__dict__
        assert received.__dict__["answer_hash"] == hash_text("ship it")
        assert received.__dict__["answer_length"] == len("ship it")

    @pytest.mark.asyncio
    async def test_socket_transport_logs_hash_not_plaintext(self, caplog) -> None:
        import asyncio
        import shutil
        import tempfile
        from pathlib import Path

        from ctrlrelay.bridge.protocol import (
            BridgeMessage,
            BridgeOp,
            parse_message,
            serialize_message,
        )
        from ctrlrelay.core.obs import hash_text
        from ctrlrelay.transports.socket_client import SocketTransport

        tmp_dir = Path(tempfile.mkdtemp())

        async def handle(reader, writer):
            try:
                line = await reader.readline()
                if not line:
                    return
                msg = parse_message(line.decode())
                writer.write(
                    serialize_message(
                        BridgeMessage(
                            op=BridgeOp.ANSWER,
                            request_id=msg.request_id,
                            answer="go ahead",
                        )
                    ).encode()
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_unix_server(
            handle, path=str(tmp_dir / "t.sock")
        )
        try:
            transport = SocketTransport(tmp_dir / "t.sock")
            await transport.connect()

            caplog.clear()
            with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                await transport.ask("Secret plan?", timeout=5)

            posted = next(
                r for r in caplog.records if r.getMessage() == "dev.question.posted"
            )
            assert "question" not in posted.__dict__
            assert posted.__dict__["question_hash"] == hash_text("Secret plan?")

            received = next(
                r for r in caplog.records if r.getMessage() == "dev.answer.received"
            )
            assert "answer" not in received.__dict__
            assert received.__dict__["answer_hash"] == hash_text("go ahead")

            await transport.close()
        finally:
            server.close()
            await server.wait_closed()
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_bridge_answer_logs_hash_not_plaintext(self, caplog) -> None:
        """The Telegram reply path logs the answer's hash, never its text."""
        import asyncio
        import shutil
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.core.obs import hash_text

        d = tempfile.mkdtemp()
        socket_path = Path(d) / "b.sock"

        mock_handler = AsyncMock()
        mock_handler.ask = AsyncMock(return_value=99)

        try:
            with patch(
                "ctrlrelay.bridge.server.TelegramHandler", return_value=mock_handler
            ):
                server = BridgeServer(
                    socket_path=socket_path, bot_token="x", chat_id=12345
                )
                task = asyncio.create_task(server.start())
                await asyncio.sleep(0.1)

                reader, writer = await asyncio.open_unix_connection(str(socket_path))
                writer.write(
                    serialize_message(
                        BridgeMessage(
                            op=BridgeOp.ASK,
                            request_id="r-1",
                            question="Continue?",
                            session_id="dev-o-r-1-a",
                        )
                    ).encode()
                )
                await writer.drain()
                await asyncio.wait_for(reader.readline(), timeout=1)

                caplog.clear()
                with caplog.at_level(logging.INFO, logger="ctrlrelay"):
                    await server._on_telegram_reply("my private answer", 99)

                received = next(
                    r
                    for r in caplog.records
                    if r.getMessage() == "dev.answer.received"
                )
                assert "answer" not in received.__dict__
                assert received.__dict__["answer_hash"] == hash_text(
                    "my private answer"
                )
                assert received.__dict__["answer_length"] == len("my private answer")

                writer.close()
                await writer.wait_closed()
                await server.stop()
                task.cancel()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestTimeoutIsNotClaimedAsFailedDelivery:
    """The bridge ACKs only after Telegram has accepted the message. A
    post slower than our wait means we time out first and the bridge
    succeeds afterwards — so logging `post_failed` there contradicts the
    bridge's own `posted`. That is the same false claim this change
    removes, pointing the other way."""

    @pytest.mark.asyncio
    async def test_timeout_logs_unknown_not_failed(self, tmp_path: Path) -> None:
        import asyncio

        from ctrlrelay.transports.base import TransportTimeoutError
        from ctrlrelay.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path=tmp_path / "s.sock")
        transport._writer = MagicMock()
        transport._writer.is_closing.return_value = False
        transport._writer.drain = AsyncMock()

        async def _never_answers(*_a: object, **_kw: object) -> None:
            raise asyncio.TimeoutError()

        with patch(
            "ctrlrelay.transports.socket_client.asyncio.wait_for", _never_answers
        ), patch("ctrlrelay.transports.socket_client.log_event") as log:
            with pytest.raises(TransportTimeoutError):
                await transport.ask("approve #1?", session_id="s", repo="o/r")

        events = [c.args[1] for c in log.call_args_list if len(c.args) > 1]
        assert "dev.question.post_unknown" in events
        assert "dev.question.post_failed" not in events

    @pytest.mark.asyncio
    async def test_a_real_write_failure_still_reports_failed(
        self, tmp_path: Path
    ) -> None:
        """A write that failed is a known non-delivery — that one keeps
        its definite label."""
        from ctrlrelay.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path=tmp_path / "s.sock")
        transport._writer = None  # connected is False -> _send_message raises

        with patch("ctrlrelay.transports.socket_client.log_event") as log:
            with pytest.raises(Exception):
                await transport.ask("approve #1?", session_id="s", repo="o/r")

        events = [c.args[1] for c in log.call_args_list if len(c.args) > 1]
        assert "dev.question.post_failed" in events
        assert "dev.question.post_unknown" not in events

    def test_timeout_is_catchable_as_transport_error(self) -> None:
        """Existing handlers filter on TransportError and must keep
        working."""
        from ctrlrelay.transports.base import TransportError, TransportTimeoutError

        assert issubclass(TransportTimeoutError, TransportError)


class TestOnlyDefiniteFailuresAreCalledFailures:
    """Delivery is only ever confirmed by the far side. Everything else
    splits in two: the bytes provably never left (definite failure), or
    they may have (unknown). Enumerating ambiguous cases one at a time
    does not converge — the discriminator is whether anything was
    written."""

    @pytest.mark.asyncio
    async def test_drain_failure_is_unknown_not_failed(
        self, tmp_path: Path
    ) -> None:
        """`write()` already handed the bytes to the transport, so the
        bridge may have received and acted on the ASK."""
        from ctrlrelay.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path=tmp_path / "s.sock")
        transport._writer = MagicMock()
        transport._writer.is_closing.return_value = False
        transport._writer.drain = AsyncMock(side_effect=ConnectionResetError())

        with patch("ctrlrelay.transports.socket_client.log_event") as log:
            with pytest.raises(Exception):
                await transport.ask("approve #1?", session_id="s", repo="o/r")

        events = [c.args[1] for c in log.call_args_list if len(c.args) > 1]
        assert "dev.question.post_unknown" in events
        assert "dev.question.post_failed" not in events

    @pytest.mark.asyncio
    async def test_never_connected_is_a_definite_failure(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path=tmp_path / "s.sock")
        transport._writer = None

        with patch("ctrlrelay.transports.socket_client.log_event") as log:
            with pytest.raises(Exception):
                await transport.ask("approve #1?", session_id="s", repo="o/r")

        events = [c.args[1] for c in log.call_args_list if len(c.args) > 1]
        assert "dev.question.post_failed" in events
        assert "dev.question.post_unknown" not in events


class TestTelegramAmbiguityTaxonomy:
    """`BadRequest` subclasses `NetworkError` in python-telegram-bot, so
    a plain isinstance check against NetworkError would sweep definite
    rejections into the ambiguous bucket."""

    def test_timeouts_and_bare_network_errors_are_ambiguous(self) -> None:
        from telegram.error import NetworkError, TimedOut

        from ctrlrelay.bridge.telegram_handler import is_ambiguous_delivery

        assert is_ambiguous_delivery(TimedOut())
        assert is_ambiguous_delivery(NetworkError("connection dropped"))

    def test_telegram_saying_no_is_definite(self) -> None:
        from telegram.error import BadRequest, Forbidden, InvalidToken

        from ctrlrelay.bridge.telegram_handler import is_ambiguous_delivery

        assert not is_ambiguous_delivery(BadRequest("chat not found"))
        assert not is_ambiguous_delivery(Forbidden("bot was blocked"))
        assert not is_ambiguous_delivery(InvalidToken())

    def test_badrequest_really_does_subclass_networkerror(self) -> None:
        """Guards the reason the helper cannot be a simple isinstance."""
        from telegram.error import BadRequest, NetworkError

        assert issubclass(BadRequest, NetworkError)


class TestBridgeClassificationIsNotReDerived:
    """The transport cannot see the Telegram exception, so a generic
    ERROR forced it to assume the worst — producing contradictory pairs
    for one request_id: post_unknown from the bridge, post_failed from
    the transport. The classification travels in the response instead."""

    @pytest.mark.asyncio
    async def test_bridge_saying_unknown_is_believed(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp
        from ctrlrelay.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path=tmp_path / "s.sock")
        transport._writer = MagicMock()
        transport._writer.is_closing.return_value = False
        transport._writer.drain = AsyncMock()

        async def _bridge_says_unknown(*_a: object, **_kw: object) -> BridgeMessage:
            return BridgeMessage(
                op=BridgeOp.ERROR,
                request_id="r-1",
                error="telegram_delivery_unknown",
                message="Timed out",
            )

        with patch(
            "ctrlrelay.transports.socket_client.asyncio.wait_for",
            _bridge_says_unknown,
        ), patch("ctrlrelay.transports.socket_client.log_event") as log:
            with pytest.raises(Exception):
                await transport.ask("approve #1?", session_id="s", repo="o/r")

        events = [c.args[1] for c in log.call_args_list if len(c.args) > 1]
        assert "dev.question.post_unknown" in events
        assert "dev.question.post_failed" not in events

    @pytest.mark.asyncio
    async def test_a_definite_bridge_rejection_stays_failed(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp
        from ctrlrelay.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path=tmp_path / "s.sock")
        transport._writer = MagicMock()
        transport._writer.is_closing.return_value = False
        transport._writer.drain = AsyncMock()

        async def _bridge_says_rejected(*_a: object, **_kw: object) -> BridgeMessage:
            return BridgeMessage(
                op=BridgeOp.ERROR,
                request_id="r-1",
                error="telegram_api_error",
                message="chat not found",
            )

        with patch(
            "ctrlrelay.transports.socket_client.asyncio.wait_for",
            _bridge_says_rejected,
        ), patch("ctrlrelay.transports.socket_client.log_event") as log:
            with pytest.raises(Exception):
                await transport.ask("approve #1?", session_id="s", repo="o/r")

        events = [c.args[1] for c in log.call_args_list if len(c.args) > 1]
        assert "dev.question.post_failed" in events
        assert "dev.question.post_unknown" not in events
