"""Bridge process entry point for daemon mode."""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

from dev_sync.bridge.server import BridgeServer


def main() -> None:
    parser = argparse.ArgumentParser(description="dev-sync Telegram bridge")
    parser.add_argument("--socket-path", required=True, help="Unix socket path")
    parser.add_argument("--bot-token", required=True, help="Telegram bot token")
    parser.add_argument("--chat-id", type=int, required=True, help="Telegram chat ID")
    args = parser.parse_args()

    socket_path = Path(args.socket_path)
    server = BridgeServer(
        socket_path=socket_path,
        bot_token=args.bot_token,
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
