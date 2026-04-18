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
