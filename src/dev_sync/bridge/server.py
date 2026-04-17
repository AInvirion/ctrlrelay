"""Bridge server for Telegram communication."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from dev_sync.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    ProtocolError,
    parse_message,
    serialize_message,
)
from dev_sync.bridge.telegram_handler import TelegramHandler


class BridgeServer:
    """Unix socket server that bridges to Telegram."""

    def __init__(
        self,
        socket_path: Path,
        bot_token: str,
        chat_id: int,
    ) -> None:
        self.socket_path = socket_path
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._server: asyncio.Server | None = None
        self._running = False
        self._telegram: TelegramHandler | None = None
        self._pending_questions: dict[str, int] = {}

    async def start(self) -> None:
        """Start the bridge server."""
        self._telegram = TelegramHandler(
            bot_token=self.bot_token,
            chat_id=self.chat_id,
        )

        if self.socket_path.exists():
            self.socket_path.unlink()

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )

        os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)

        self._running = True
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop the bridge server."""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        if self._telegram:
            await self._telegram.close()

        if self.socket_path.exists():
            self.socket_path.unlink()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a client connection."""
        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break

                try:
                    msg = parse_message(line.decode())
                    response = await self._handle_message(msg)
                    if response:
                        writer.write(serialize_message(response).encode())
                        await writer.drain()
                except ProtocolError:
                    pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_message(self, msg: BridgeMessage) -> BridgeMessage | None:
        """Handle a single message and return response."""
        if msg.op == BridgeOp.PING:
            return BridgeMessage(op=BridgeOp.PONG)

        if msg.op == BridgeOp.SEND:
            try:
                assert self._telegram is not None
                await self._telegram.send(msg.text or "")
                return BridgeMessage(op=BridgeOp.ACK, request_id=msg.request_id, status="sent")
            except Exception as e:
                return BridgeMessage(
                    op=BridgeOp.ERROR,
                    request_id=msg.request_id,
                    error="telegram_api_error",
                    message=str(e),
                )

        if msg.op == BridgeOp.ASK:
            try:
                assert self._telegram is not None
                msg_id = await self._telegram.ask(msg.question or "", options=msg.options)
                if msg.request_id:
                    self._pending_questions[msg.request_id] = msg_id
                return BridgeMessage(op=BridgeOp.ACK, request_id=msg.request_id, status="pending")
            except Exception as e:
                return BridgeMessage(
                    op=BridgeOp.ERROR,
                    request_id=msg.request_id,
                    error="telegram_api_error",
                    message=str(e),
                )

        return None
