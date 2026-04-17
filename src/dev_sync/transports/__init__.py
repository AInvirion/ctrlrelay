"""Transport abstraction for orchestrator communication."""

from dev_sync.core.config import TransportConfig, TransportType
from dev_sync.transports.base import Transport, TransportError
from dev_sync.transports.file_mock import FileMockTransport
from dev_sync.transports.socket_client import SocketTransport


def get_transport(config: TransportConfig) -> Transport:
    """Create transport instance from config."""
    if config.type == TransportType.FILE_MOCK:
        assert config.file_mock is not None
        return FileMockTransport(
            inbox=config.file_mock.inbox,
            outbox=config.file_mock.outbox,
        )

    if config.type == TransportType.TELEGRAM:
        assert config.telegram is not None
        return SocketTransport(
            socket_path=config.telegram.socket_path,
        )

    raise TransportError(f"Unknown transport type: {config.type}")


__all__ = [
    "FileMockTransport",
    "SocketTransport",
    "Transport",
    "TransportError",
    "get_transport",
]
