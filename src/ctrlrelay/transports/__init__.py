"""Transport abstraction for orchestrator communication."""

from ctrlrelay.core.config import TransportConfig, TransportType
from ctrlrelay.transports.base import Transport, TransportError
from ctrlrelay.transports.file_mock import FileMockTransport
from ctrlrelay.transports.socket_client import SocketTransport


def get_transport(config: TransportConfig) -> Transport:
    """Create transport instance from config."""
    if config.type == TransportType.FILE_MOCK:
        assert config.file_mock is not None
        return FileMockTransport(
            inbox=config.file_mock.inbox,
            outbox=config.file_mock.outbox,
        )

    # Every chat transport reaches the bridge the same way — a Unix socket
    # and a timeout. The chat app is the bridge's concern, not the
    # pipeline's, which is why there is one branch here and not one per
    # app.
    chat = {
        TransportType.TELEGRAM: config.telegram,
        TransportType.MATTERMOST: config.mattermost,
    }.get(config.type)
    if chat is not None:
        return SocketTransport(
            socket_path=chat.socket_path,
            ask_timeout_seconds=chat.ask_timeout_seconds,
        )

    raise TransportError(f"Unknown transport type: {config.type}")


__all__ = [
    "FileMockTransport",
    "SocketTransport",
    "Transport",
    "TransportError",
    "get_transport",
]
