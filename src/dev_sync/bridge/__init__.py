"""Bridge process for Telegram communication."""

from dev_sync.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    parse_message,
    serialize_message,
)

__all__ = [
    "BridgeMessage",
    "BridgeOp",
    "parse_message",
    "serialize_message",
]
