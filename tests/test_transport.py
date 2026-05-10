"""Tests for transport abstraction."""

import pytest


class TestTransportProtocol:
    def test_transport_is_protocol(self) -> None:
        """Transport should be a Protocol."""
        from ctrlrelay.transports.base import Transport

        assert hasattr(Transport, "__protocol_attrs__") or isinstance(
            Transport, type
        )

    def test_transport_has_send(self) -> None:
        """Transport should define send method."""
        from ctrlrelay.transports.base import Transport

        assert hasattr(Transport, "send")

    def test_transport_has_ask(self) -> None:
        """Transport should define ask method."""
        from ctrlrelay.transports.base import Transport

        assert hasattr(Transport, "ask")

    def test_transport_has_close(self) -> None:
        """Transport should define close method."""
        from ctrlrelay.transports.base import Transport

        assert hasattr(Transport, "close")


class TestFileMockTransport:
    @pytest.fixture
    def mock_files(self, tmp_path):
        """Create inbox/outbox files."""
        inbox = tmp_path / "inbox.txt"
        outbox = tmp_path / "outbox.txt"
        inbox.touch()
        outbox.touch()
        return inbox, outbox

    @pytest.mark.asyncio
    async def test_send_writes_to_outbox(self, mock_files) -> None:
        """send() should write message to outbox."""
        from ctrlrelay.transports.file_mock import FileMockTransport

        inbox, outbox = mock_files
        transport = FileMockTransport(inbox=inbox, outbox=outbox)

        await transport.send("Hello world")

        content = outbox.read_text()
        assert "Hello world" in content

    @pytest.mark.asyncio
    async def test_ask_writes_question_reads_answer(self, mock_files) -> None:
        """ask() should write question and read answer from inbox."""
        from ctrlrelay.transports.file_mock import FileMockTransport

        inbox, outbox = mock_files
        inbox.write_text("yes\n")

        transport = FileMockTransport(inbox=inbox, outbox=outbox)
        answer = await transport.ask("Approve?", options=["yes", "no"])

        assert answer == "yes"
        assert "Approve?" in outbox.read_text()

    @pytest.mark.asyncio
    async def test_ask_timeout_raises(self, mock_files) -> None:
        """ask() should raise on timeout with empty inbox."""
        from ctrlrelay.transports.base import TransportError
        from ctrlrelay.transports.file_mock import FileMockTransport

        inbox, outbox = mock_files
        transport = FileMockTransport(inbox=inbox, outbox=outbox)

        with pytest.raises(TransportError, match="timeout"):
            await transport.ask("Question?", timeout=1)

    @pytest.mark.asyncio
    async def test_implements_protocol(self, mock_files) -> None:
        """FileMockTransport should implement Transport protocol."""
        from ctrlrelay.transports.base import Transport
        from ctrlrelay.transports.file_mock import FileMockTransport

        inbox, outbox = mock_files
        transport = FileMockTransport(inbox=inbox, outbox=outbox)
        assert isinstance(transport, Transport)


class TestSocketTransport:
    @pytest.fixture
    def socket_path(self, tmp_path):
        """Create temp socket path."""
        return tmp_path / "test.sock"

    @pytest.mark.asyncio
    async def test_connect_fails_when_no_server(self, socket_path) -> None:
        """Should raise when bridge not running."""
        from ctrlrelay.transports.base import TransportError
        from ctrlrelay.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path)
        with pytest.raises(TransportError, match="connect"):
            await transport.connect()

    @pytest.mark.asyncio
    async def test_send_requires_connection(self, socket_path) -> None:
        """Should raise if not connected."""
        from ctrlrelay.transports.base import TransportError
        from ctrlrelay.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path)
        with pytest.raises(TransportError, match="not connected"):
            await transport.send("test")

    @pytest.mark.asyncio
    async def test_implements_protocol(self, socket_path) -> None:
        """SocketTransport should implement Transport protocol."""
        from ctrlrelay.transports.base import Transport
        from ctrlrelay.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path)
        assert isinstance(transport, Transport)


class TestSocketTransportAckThenAnswer:
    """Regression for bug #3: bridge sends back ACK(status=pending) for an
    ASK as soon as the question is queued for Telegram, then later sends
    ANSWER once the operator replies. The receive loop must skip the
    intermediate ACK and only resolve the future on ANSWER/ERROR/terminal-ACK,
    or transport.ask() raises TransportError("Unexpected response: BridgeOp.ACK")
    and every blocked secops session falls through to pending_resumes
    instead of reaching the operator on Telegram."""

    @pytest.mark.asyncio
    async def test_ask_waits_past_intermediate_ack_and_returns_answer(
        self,
    ) -> None:
        import asyncio
        import json
        import os
        import tempfile
        import uuid
        from pathlib import Path

        from ctrlrelay.transports.socket_client import SocketTransport

        # Use /tmp directly — macOS sun_path limit is ~104 chars and pytest's
        # tmp_path lives under /var/folders/... which often blows past it.
        sock_path = Path(tempfile.gettempdir()) / f"ctrlrelay-{uuid.uuid4().hex[:8]}.sock"

        async def fake_bridge(reader, writer):
            line = await reader.readline()
            msg = json.loads(line.decode())
            assert msg["op"] == "ask"
            request_id = msg["request_id"]
            ack = json.dumps({
                "op": "ack",
                "request_id": request_id,
                "status": "pending",
            }) + "\n"
            writer.write(ack.encode())
            await writer.drain()
            await asyncio.sleep(0.05)
            answer = json.dumps({
                "op": "answer",
                "request_id": request_id,
                "answer": "operator said yes",
            }) + "\n"
            writer.write(answer.encode())
            await writer.drain()

        server = await asyncio.start_unix_server(fake_bridge, path=str(sock_path))
        try:
            transport = SocketTransport(sock_path)
            await transport.connect()
            answer = await transport.ask(
                "Merge PR?", timeout=5, session_id="s1", repo="o/r",
            )
            assert answer == "operator said yes"
            await transport.close()
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_send_terminal_ack_still_resolves(self) -> None:
        """SEND ACK(status="sent") is the terminal response — must still
        resolve normally; only ACK(status="pending") is intermediate."""
        import asyncio
        import json
        import tempfile
        import uuid
        from pathlib import Path

        from ctrlrelay.transports.socket_client import SocketTransport

        sock_path = Path(tempfile.gettempdir()) / f"ctrlrelay-{uuid.uuid4().hex[:8]}.sock"

        async def fake_bridge(reader, writer):
            line = await reader.readline()
            msg = json.loads(line.decode())
            assert msg["op"] == "send"
            request_id = msg["request_id"]
            ack = json.dumps({
                "op": "ack",
                "request_id": request_id,
                "status": "sent",
            }) + "\n"
            writer.write(ack.encode())
            await writer.drain()

        server = await asyncio.start_unix_server(fake_bridge, path=str(sock_path))
        try:
            transport = SocketTransport(sock_path)
            await transport.connect()
            await transport.send("hello")  # Should return without error.
            await transport.close()
        finally:
            server.close()
            await server.wait_closed()


class TestTransportError:
    def test_error_exists(self) -> None:
        """TransportError should be defined."""
        from ctrlrelay.transports.base import TransportError

        assert issubclass(TransportError, Exception)


class TestGetTransport:
    def test_get_file_mock_transport(self, tmp_path) -> None:
        """Should return FileMockTransport for file_mock type."""
        from ctrlrelay.core.config import FileMockConfig, TransportConfig, TransportType
        from ctrlrelay.transports import get_transport

        config = TransportConfig(
            type=TransportType.FILE_MOCK,
            file_mock=FileMockConfig(
                inbox=tmp_path / "inbox.txt",
                outbox=tmp_path / "outbox.txt",
            ),
        )
        (tmp_path / "inbox.txt").touch()
        (tmp_path / "outbox.txt").touch()

        transport = get_transport(config)
        assert transport.__class__.__name__ == "FileMockTransport"

    def test_get_socket_transport(self, tmp_path) -> None:
        """Should return SocketTransport for telegram type."""
        from ctrlrelay.core.config import TelegramConfig, TransportConfig, TransportType
        from ctrlrelay.transports import get_transport

        config = TransportConfig(
            type=TransportType.TELEGRAM,
            telegram=TelegramConfig(
                chat_id=123,
                socket_path=tmp_path / "test.sock",
            ),
        )

        transport = get_transport(config)
        assert transport.__class__.__name__ == "SocketTransport"
