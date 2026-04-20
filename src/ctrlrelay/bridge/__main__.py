"""Bridge process entry point for daemon mode."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from ctrlrelay.bridge.server import BridgeServer


def main() -> None:
    parser = argparse.ArgumentParser(description="ctrlrelay Telegram bridge")
    parser.add_argument("--socket-path", required=True, help="Unix socket path")
    parser.add_argument(
        "--bot-token-env",
        default="CTRLRELAY_TELEGRAM_TOKEN",
        help="Environment variable holding the Telegram bot token",
    )
    parser.add_argument("--chat-id", type=int, required=True, help="Telegram chat ID")
    args = parser.parse_args()

    bot_token = os.environ.get(args.bot_token_env)
    if not bot_token:
        print(
            f"error: bot token env var '{args.bot_token_env}' is unset",
            file=sys.stderr,
        )
        sys.exit(2)

    socket_path = Path(args.socket_path)
    server = BridgeServer(
        socket_path=socket_path,
        bot_token=bot_token,
        chat_id=args.chat_id,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal(sig: int) -> None:
        loop.create_task(server.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    try:
        loop.run_until_complete(server.start())
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
