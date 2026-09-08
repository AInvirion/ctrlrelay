"""Unix socket transport client for bridge communication."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ctrlrelay.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    ProtocolError,
    parse_message,
    serialize_message,
)
from ctrlrelay.core.obs import get_logger, hash_text, log_event
from ctrlrelay.transports.base import (
    TransportError,
    TransportTimeoutError,
    TransportUnknownDeliveryError,
)

_logger = get_logger("transport.socket")


class SocketTransport:
    """Transport that connects to bridge via Unix socket."""

    def __init__(
        self,
        socket_path: Path,
        ask_timeout_seconds: int = 300,
    ) -> None:
        self.socket_path = socket_path
        # Per-instance default for ask(). Callers construct this from
        # ``transport.telegram.ask_timeout_seconds`` so a single operator
        # setting governs every pipeline's BLOCKED wait; the 300s fallback
        # only applies to ad-hoc construction in tests and one-shot CLI
        # helpers that never ask.
        self.ask_timeout_seconds = ask_timeout_seconds
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[BridgeMessage]] = {}
        self._on_ack: dict[str, Callable[[], None]] = {}
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
                        # The bridge sends two messages back for an ASK:
                        # an intermediate ACK(status="pending") immediately
                        # after queuing for Telegram, then an ANSWER (or
                        # ERROR) once the operator replies. Resolving the
                        # future on the first ACK collapses the wait
                        # prematurely — caller sees "Unexpected response:
                        # BridgeOp.ACK" and throws. Skip those to keep
                        # waiting. Terminal ACKs (status="sent" from SEND)
                        # remain the final response and resolve normally.
                        if msg.op == BridgeOp.ACK and msg.status == "pending":
                            # This ACK is also the only proof the bridge got
                            # the question in front of the operator, so it is
                            # what "posted" is allowed to mean.
                            callback = self._on_ack.get(msg.request_id)
                            if callback is not None:
                                callback()
                            continue
                        self._pending[msg.request_id].set_result(msg)
                except ProtocolError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _send_message(self, msg: BridgeMessage) -> None:
        """Send message to bridge.

        Raises ``TransportError`` while nothing has been written — a
        definite non-delivery — and ``TransportUnknownDeliveryError``
        once bytes may have reached the far side. `drain()` failing does
        not mean the bridge never saw the request: `write()` has already
        handed the data to the transport, so the ASK may well have been
        received and acted on.
        """
        if not self.connected:
            raise TransportError("Transport not connected")
        assert self._writer is not None
        data = serialize_message(msg).encode()
        try:
            self._writer.write(data)
        except Exception as e:
            raise TransportError(f"Write failed: {e}") from e
        try:
            await self._writer.drain()
        except Exception as e:
            raise TransportUnknownDeliveryError(f"Drain failed: {e}") from e

    async def _send_and_wait(
        self,
        msg: BridgeMessage,
        timeout: int,
        *,
        on_ack: Callable[[], None] | None = None,
    ) -> BridgeMessage:
        """Send message and wait for response.

        ``on_ack`` fires from the receive loop when the bridge sends its
        intermediate ACK — the point at which the request is known to have
        been accepted, as opposed to merely written to the socket.
        """
        assert msg.request_id is not None
        future: asyncio.Future[BridgeMessage] = asyncio.get_event_loop().create_future()
        self._pending[msg.request_id] = future
        if on_ack is not None:
            self._on_ack[msg.request_id] = on_ack

        try:
            await self._send_message(msg)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TransportTimeoutError("Timeout waiting for response") from e
        finally:
            self._pending.pop(msg.request_id, None)
            self._on_ack.pop(msg.request_id, None)

    async def send(
        self,
        message: str,
        *,
        session_id: str | None = None,
        repo: str | None = None,
        issue_number: int | None = None,
    ) -> None:
        """Send a one-way message."""
        request_id = f"r-{uuid.uuid4().hex[:8]}"
        msg = BridgeMessage(
            op=BridgeOp.SEND,
            request_id=request_id,
            text=message,
            session_id=session_id,
            repo=repo,
            issue_number=issue_number,
        )
        await self._send_message(msg)

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
        timeout: int | None = None,
        *,
        session_id: str | None = None,
        repo: str | None = None,
        issue_number: int | None = None,
    ) -> str:
        """Ask a question and wait for response."""
        if timeout is None:
            timeout = self.ask_timeout_seconds
        request_id = f"r-{uuid.uuid4().hex[:8]}"
        msg = BridgeMessage(
            op=BridgeOp.ASK,
            request_id=request_id,
            question=question,
            options=options,
            timeout=timeout,
            session_id=session_id,
            repo=repo,
            issue_number=issue_number,
        )

        # The question itself never reaches the log stream: these records go
        # to stdout and are captured into ~/.ctrlrelay/logs, where operator
        # content would sit in plaintext with no retention policy. The hash
        # is enough to correlate one question across poller and bridge.
        common = {
            "session_id": session_id,
            "repo": repo,
            "issue_number": issue_number,
            "transport": "socket",
            "destination": str(self.socket_path),
            "request_id": request_id,
            "question_length": len(question),
            "question_hash": hash_text(question),
            "options": options,
        }

        posted = False

        def _mark_posted() -> None:
            # Fired by the receive loop on the bridge's ACK, which is the
            # first moment the question is known to have reached Telegram.
            # Logging before the write would claim a delivery that a failed
            # socket write or a Telegram outage never made.
            nonlocal posted
            posted = True
            log_event(_logger, "dev.question.posted", **common)

        sent_at = time.monotonic()
        try:
            response = await self._send_and_wait(msg, timeout, on_ack=_mark_posted)
        except Exception as e:
            if not posted:
                # A timeout is not a failed delivery. The bridge ACKs only
                # after Telegram has accepted the message, so a post slower
                # than our wait leaves us timing out first and the bridge
                # succeeding afterwards — logging "post_failed" there would
                # contradict the bridge's own "posted", which is the same
                # false-claim problem this change exists to remove, just
                # pointing the other way.
                # Only claim a definite failure when the request
                # provably never left. Anything past the write is
                # unknown: no ACK arrived, but the bridge may have
                # received and acted on it regardless.
                unknown = isinstance(e, TransportUnknownDeliveryError)
                log_event(
                    _logger,
                    "dev.question.post_unknown"
                    if unknown
                    else "dev.question.post_failed",
                    **common,
                    reason=type(e).__name__ if unknown else "send_failed",
                    error=str(e)[:200],
                )
            raise

        if response.op == BridgeOp.ERROR:
            if not posted:
                log_event(
                    _logger,
                    "dev.question.post_failed",
                    **common,
                    reason="bridge_error",
                    error=str(response.message)[:200],
                )
            raise TransportError(f"Bridge error: {response.message}")
        if response.op == BridgeOp.ANSWER and response.answer:
            if not posted:
                # An answer proves the question was posted even if the ACK
                # never arrived (older bridge, or the ACK was lost).
                _mark_posted()
            log_event(
                _logger,
                "dev.answer.received",
                session_id=session_id,
                repo=repo,
                issue_number=issue_number,
                transport="socket",
                request_id=request_id,
                answer_length=len(response.answer),
                answer_hash=hash_text(response.answer),
                elapsed_ms=int((time.monotonic() - sent_at) * 1000),
            )
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
