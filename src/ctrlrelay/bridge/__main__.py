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
    parser.add_argument(
        "--state-db",
        default=None,
        help=(
            "Path to the orchestrator state.db. When provided, orphan "
            "Telegram replies route to persisted BLOCKED sessions in "
            "pending_resumes. Required for the resume-via-Telegram flow."
        ),
    )
    args = parser.parse_args()

    bot_token = os.environ.get(args.bot_token_env)
    if not bot_token:
        print(
            f"error: bot token env var '{args.bot_token_env}' is unset",
            file=sys.stderr,
        )
        sys.exit(2)

    state_db = None
    if args.state_db:
        from ctrlrelay.core.state import StateDB
        state_db = StateDB(Path(args.state_db))

    socket_path = Path(args.socket_path)
    server = BridgeServer(
        socket_path=socket_path,
        bot_token=bot_token,
        chat_id=args.chat_id,
        state_db=state_db,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run_server() -> None:
        # Wrap start() in a finally that awaits stop() so the loop can't
        # close before _telegram.close() and the socket unlink complete.
        try:
            await server.start()
        finally:
            await server.stop()

    main_task = loop.create_task(_run_server())

    def handle_signal(sig: int) -> None:
        main_task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        if state_db is not None:
            try:
                state_db.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
