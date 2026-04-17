"""Unix socket transport client for bridge communication."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from dev_sync.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    ProtocolError,
    parse_message,
    serialize_message,
)
from dev_sync.transports.base import TransportError


class SocketTransport:
    """Transport that connects to bridge via Unix socket."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[BridgeMessage]] = {}
        self._receive_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Connect to bridge socket."""
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                str(self.socket_path)
            )
            self._receive_task = asyncio.create_task(self._receive_loop())
        except (OSError, ConnectionRefusedError) as e:
            raise TransportError(f"Failed to connect to bridge: {e}") from e

    async def _receive_loop(self) -> None:
        """Background task to receive messages from bridge."""
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = parse_message(line.decode())
                    if msg.request_id and msg.request_id in self._pending:
                        self._pending[msg.request_id].set_result(msg)
                except ProtocolError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _send_message(self, msg: BridgeMessage) -> None:
        """Send message to bridge."""
        if not self.connected:
            raise TransportError("Transport not connected")
        assert self._writer is not None
        data = serialize_message(msg).encode()
        self._writer.write(data)
        await self._writer.drain()

    async def _send_and_wait(self, msg: BridgeMessage, timeout: int) -> BridgeMessage:
        """Send message and wait for response."""
        assert msg.request_id is not None
        future: asyncio.Future[BridgeMessage] = asyncio.get_event_loop().create_future()
        self._pending[msg.request_id] = future

        try:
            await self._send_message(msg)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TransportError("Timeout waiting for response") from e
        finally:
            self._pending.pop(msg.request_id, None)

    async def send(self, message: str) -> None:
        """Send a one-way message."""
        request_id = f"r-{uuid.uuid4().hex[:8]}"
        msg = BridgeMessage(op=BridgeOp.SEND, request_id=request_id, text=message)
        await self._send_message(msg)

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
        timeout: int = 300,
    ) -> str:
        """Ask a question and wait for response."""
        request_id = f"r-{uuid.uuid4().hex[:8]}"
        msg = BridgeMessage(
            op=BridgeOp.ASK,
            request_id=request_id,
            question=question,
            options=options,
            timeout=timeout,
        )
        response = await self._send_and_wait(msg, timeout)

        if response.op == BridgeOp.ERROR:
            raise TransportError(f"Bridge error: {response.message}")
        if response.op == BridgeOp.ANSWER and response.answer:
            return response.answer

        raise TransportError(f"Unexpected response: {response.op}")

    async def close(self) -> None:
        """Close the connection."""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        self._reader = None
        self._writer = None
