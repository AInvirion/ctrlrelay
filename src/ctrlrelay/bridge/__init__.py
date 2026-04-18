"""Bridge process for Telegram communication."""

from ctrlrelay.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    ProtocolError,
    parse_message,
    serialize_message,
)
from ctrlrelay.bridge.server import BridgeServer
from ctrlrelay.bridge.telegram_handler import TelegramHandler

__all__ = [
    "BridgeMessage",
    "BridgeOp",
    "BridgeServer",
    "ProtocolError",
    "TelegramHandler",
    "parse_message",
    "serialize_message",
]
