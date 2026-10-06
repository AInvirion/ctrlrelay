"""Bridge process: a Unix socket on one side, a chat app on the other."""

from ctrlrelay.bridge.factory import (
    HandlerConfigError,
    make_handler,
    token_env_var,
)
from ctrlrelay.bridge.handler import ChatHandler, IncomingMessageHandler
from ctrlrelay.bridge.mattermost_handler import MattermostHandler
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
    "ChatHandler",
    "HandlerConfigError",
    "IncomingMessageHandler",
    "MattermostHandler",
    "ProtocolError",
    "TelegramHandler",
    "make_handler",
    "parse_message",
    "serialize_message",
    "token_env_var",
]
