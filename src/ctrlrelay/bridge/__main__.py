"""Bridge process entry point for daemon mode.

Takes a config path rather than per-transport flags. The alternative —
growing a flag per chat app — means every new transport touches this file,
the CLI that spawns it, and the two of them have to agree. Reading the same
config the CLI read removes that agreement from the design.

The token is never an argument. It is read from the environment variable
the config names, which the parent process passes through, because argv is
world-readable via ``ps`` and ``/proc/*/cmdline``.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from ctrlrelay.bridge import BridgeServer, HandlerConfigError, make_handler
from ctrlrelay.core.config import ConfigError, load_config, resolve_config_path


def main() -> None:
    parser = argparse.ArgumentParser(description="ctrlrelay chat bridge")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to orchestrator.yaml (default: auto-discover).",
    )
    parser.add_argument(
        "--socket-path",
        default=None,
        help="Unix socket path. Defaults to the configured transport's.",
    )
    parser.add_argument(
        "--state-db",
        default=None,
        help=(
            "Path to the orchestrator state.db. When provided, orphan "
            "replies route to persisted BLOCKED sessions in "
            "pending_resumes. Required for the resume-via-chat flow."
        ),
    )
    args = parser.parse_args()

    try:
        config = load_config(resolve_config_path(args.config))
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        handler = make_handler(config.transport)
    except HandlerConfigError as e:
        # Exit 2, not 1: this is a configuration fault the operator must
        # fix, not a transient failure worth a supervisor restart loop.
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    socket_path = (
        Path(args.socket_path).expanduser()
        if args.socket_path
        else config.transport.socket_path
    )
    if socket_path is None:
        print("error: no socket path in config or arguments", file=sys.stderr)
        sys.exit(2)

    state_db = None
    if args.state_db:
        from ctrlrelay.core.state import StateDB
        state_db = StateDB(Path(args.state_db))

    server = BridgeServer(
        socket_path=Path(socket_path),
        handler=handler,
        state_db=state_db,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run_server() -> None:
        # Wrap start() in a finally that awaits stop() so the loop cannot
        # close before the handler is closed and the socket unlinked.
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
