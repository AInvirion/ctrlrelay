"""Build the one chat handler the bridge will use, from config.

This is the only place that maps a transport type to a handler class. The
bridge server takes a handler and knows nothing else; the CLI and the
daemon entry point both come through here, so there is one answer to
"which chat app is this, and where is its token" rather than three.
"""

from __future__ import annotations

import os

from ctrlrelay.bridge.handler import ChatHandler
from ctrlrelay.core.config import TransportConfig, TransportType


class HandlerConfigError(Exception):
    """Configuration cannot produce a working handler."""


def token_env_var(transport: TransportConfig) -> str:
    """Name of the env var holding this transport's token.

    Exposed separately because the CLI and the installer both need to tell
    the operator *which variable to set* before any handler can be built —
    and the answer differs per transport.
    """
    block = getattr(transport, transport.type.value, None)
    env = getattr(block, "bot_token_env", None)
    if not env:
        raise HandlerConfigError(
            f"transport '{transport.type.value}' has no token env var setting"
        )
    return str(env)


def make_handler(transport: TransportConfig) -> ChatHandler:
    """Construct the handler for ``transport``.

    Raises ``HandlerConfigError`` with something an operator can act on.
    Deliberately eager about the token and the destination: the bridge is a
    daemon, so anything not checked at startup is discovered when a session
    blocks — which is hours later, on the one path whose job is to be
    there when something has gone wrong.
    """
    if transport.type == TransportType.TELEGRAM:
        cfg = transport.telegram
        if cfg is None:
            raise HandlerConfigError("telegram config missing")
        token = os.environ.get(cfg.bot_token_env)
        if not token:
            raise HandlerConfigError(
                f"env var '{cfg.bot_token_env}' is unset"
            )
        if not cfg.chat_id:
            raise HandlerConfigError(
                "transport.telegram.chat_id is unset — the bridge would "
                "post to chat 0 and every send would fail"
            )
        from ctrlrelay.bridge.telegram_handler import TelegramHandler

        return TelegramHandler(bot_token=token, chat_id=cfg.chat_id)

    if transport.type == TransportType.MATTERMOST:
        cfg = transport.mattermost
        if cfg is None:
            raise HandlerConfigError("mattermost config missing")
        token = os.environ.get(cfg.bot_token_env)
        if not token:
            raise HandlerConfigError(
                f"env var '{cfg.bot_token_env}' is unset"
            )
        if not cfg.channel_id:
            raise HandlerConfigError(
                "transport.mattermost.channel_id is unset — a post with no "
                "channel is rejected, so no question would ever arrive"
            )
        from ctrlrelay.bridge.mattermost_handler import MattermostHandler

        return MattermostHandler(
            url=cfg.url,
            bot_token=token,
            channel_id=cfg.channel_id,
        )

    raise HandlerConfigError(
        f"transport type '{transport.type.value}' has no chat handler. "
        "The bridge serves telegram and mattermost; file_mock does not use "
        "a bridge at all."
    )
