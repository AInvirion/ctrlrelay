"""Bridge server for Telegram communication."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ctrlrelay.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    ProtocolError,
    parse_message,
    serialize_message,
)
from ctrlrelay.bridge.telegram_handler import TelegramHandler
from ctrlrelay.core.obs import get_logger, hash_text, log_event

if TYPE_CHECKING:
    from ctrlrelay.core.state import StateDB

_logger = get_logger("bridge.server")
_log = logging.getLogger(__name__)


class _PendingQuestion:
    """Question posted to Telegram, awaiting the operator's reply."""

    __slots__ = ("request_id", "telegram_msg_id", "writer")

    def __init__(
        self,
        request_id: str,
        telegram_msg_id: int,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.request_id = request_id
        self.telegram_msg_id = telegram_msg_id
        self.writer = writer


class BridgeServer:
    """Unix socket server that bridges to Telegram — bidirectional.

    Outbound: clients send SEND/ASK over the socket and we hit Telegram.
    Inbound: we long-poll Telegram for messages; when a reply arrives it's
    matched to the oldest outstanding ASK (or by reply_to_message_id if
    available) and we push an ANSWER frame over that client's socket."""

    def __init__(
        self,
        socket_path: Path,
        bot_token: str,
        chat_id: int,
        state_db: "StateDB | None" = None,
    ) -> None:
        self.socket_path = socket_path
        self.bot_token = bot_token
        self.chat_id = chat_id
        # Optional: when provided, orphan Telegram replies (no live
        # _pending_question to match) are routed to the oldest unanswered
        # BLOCKED session in state_db's pending_resumes table. The poller's
        # pending-resume sweeper then picks up the answer and drives the
        # actual pipeline resume. Without state_db, orphan replies still
        # get a "didn't land" Telegram notice but nothing gets queued.
        self.state_db = state_db
        self._server: asyncio.Server | None = None
        self._running = False
        self._telegram: TelegramHandler | None = None
        # Insertion-ordered so FIFO dispatch is deterministic.
        self._pending_questions: OrderedDict[str, _PendingQuestion] = OrderedDict()
        self._pending_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the bridge server."""
        self._telegram = TelegramHandler(
            bot_token=self.bot_token,
            chat_id=self.chat_id,
        )
        await self._telegram.start_polling(self._on_telegram_reply)

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
                except ProtocolError:
                    continue

                response = await self._handle_message(msg, writer)
                if response is None:
                    continue

                # Response write races client disconnect: the transport
                # (SocketTransport) finishes a send/ask round-trip and closes
                # the socket while we're still flushing the ACK. Swallow the
                # expected disconnect errors instead of propagating a
                # traceback into bridge.error.log.
                if writer.is_closing():
                    break
                try:
                    writer.write(serialize_message(response).encode())
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    _log.debug(
                        "bridge: client disconnected mid-response "
                        "(op=%s request_id=%s err=%s)",
                        msg.op, msg.request_id, e,
                    )
                    break
        finally:
            # Client disconnected — drop any outstanding questions tied to
            # this writer so we don't try to answer a dead socket later.
            async with self._pending_lock:
                dead = [
                    rid for rid, q in self._pending_questions.items()
                    if q.writer is writer
                ]
                for rid in dead:
                    self._pending_questions.pop(rid, None)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_message(
        self,
        msg: BridgeMessage,
        writer: asyncio.StreamWriter,
    ) -> BridgeMessage | None:
        """Handle a single message and return response."""
        if msg.op == BridgeOp.PING:
            return BridgeMessage(op=BridgeOp.PONG)

        if msg.op == BridgeOp.SEND:
            try:
                assert self._telegram is not None
                await self._telegram.send(msg.text or "")
                _log.info("bridge: SEND delivered, request_id=%s", msg.request_id)
                return BridgeMessage(op=BridgeOp.ACK, request_id=msg.request_id, status="sent")
            except Exception as e:
                _log.warning("bridge: SEND failed, request_id=%s err=%s", msg.request_id, e)
                return BridgeMessage(
                    op=BridgeOp.ERROR,
                    request_id=msg.request_id,
                    error="telegram_api_error",
                    message=str(e),
                )

        if msg.op == BridgeOp.ASK:
            try:
                assert self._telegram is not None
                question = msg.question or ""
                log_event(
                    _logger,
                    "dev.question.posted",
                    session_id=msg.session_id,
                    repo=msg.repo,
                    issue_number=msg.issue_number,
                    transport="telegram",
                    destination=f"telegram:chat={self.chat_id}",
                    request_id=msg.request_id,
                    question=question,
                    question_length=len(question),
                    question_hash=hash_text(question),
                    options=msg.options,
                )
                telegram_msg_id = await self._telegram.ask(
                    question, options=msg.options
                )
                if msg.request_id:
                    async with self._pending_lock:
                        self._pending_questions[msg.request_id] = _PendingQuestion(
                            request_id=msg.request_id,
                            telegram_msg_id=telegram_msg_id,
                            writer=writer,
                        )
                _log.info(
                    "bridge: ASK posted request_id=%s telegram_msg_id=%s",
                    msg.request_id, telegram_msg_id,
                )
                return BridgeMessage(
                    op=BridgeOp.ACK, request_id=msg.request_id, status="pending",
                )
            except Exception as e:
                _log.warning("bridge: ASK failed, request_id=%s err=%s", msg.request_id, e)
                return BridgeMessage(
                    op=BridgeOp.ERROR,
                    request_id=msg.request_id,
                    error="telegram_api_error",
                    message=str(e),
                )

        return None

    async def _on_telegram_reply(
        self,
        text: str,
        reply_to_message_id: int | None,
    ) -> None:
        """Route an incoming Telegram message to the matching pending question.

        Priority: if reply_to_message_id matches a tracked question, use it.
        Otherwise fall back to FIFO (oldest outstanding question wins) —
        good enough for the single-operator case."""
        async with self._pending_lock:
            match: _PendingQuestion | None = None
            if reply_to_message_id is not None:
                for q in self._pending_questions.values():
                    if q.telegram_msg_id == reply_to_message_id:
                        match = q
                        break
            if match is None and self._pending_questions:
                # FIFO: pop oldest.
                match = next(iter(self._pending_questions.values()))
            if match is None:
                _log.info(
                    "bridge: incoming telegram msg with no pending question; "
                    "text=%r", text[:80],
                )
                # Try to route to a persisted BLOCKED session in state_db
                # so the operator's reply actually drives a resume. Without
                # this, the reply disappears the instant the session's ASK
                # socket closes — which is exactly what happens when a
                # scheduled secops sweep escalates BLOCKED and exits.
                outcome = await self._queue_orphan_reply_as_resume_answer(text)
                if self._telegram is not None:
                    try:
                        if outcome["status"] == "queued":
                            row = outcome["row"]
                            await self._telegram.send(
                                "✅ Answer queued for BLOCKED session "
                                f"`{row['session_id']}` "
                                f"(pipeline={row['pipeline']}, "
                                f"repo={row['repo']}).\n"
                                "The pending-resume sweeper will drive it "
                                "on the next tick — you'll get another "
                                "message with the result."
                            )
                        elif outcome["status"] == "ambiguous":
                            pending_list = "\n".join(
                                f"  • `{r['session_id']}` ({r['repo']}): "
                                f"{(r['question'] or '')[:80]}"
                                for r in outcome["rows"]
                            )
                            await self._telegram.send(
                                "⚠️ Your reply wasn't routed — multiple "
                                "BLOCKED sessions are unanswered and your "
                                "message didn't include a session_id to "
                                "disambiguate.\n\n"
                                "Pending:\n"
                                f"{pending_list}\n\n"
                                "Reply again with the session_id included "
                                "(just paste it anywhere in your message)."
                            )
                        else:
                            await self._telegram.send(
                                "⚠️ Your reply wasn't routed — no active "
                                "session is waiting on input and no "
                                "persisted BLOCKED session is unanswered. "
                                "To act manually, re-run the pipeline, "
                                "e.g. `ctrlrelay run secops --repo "
                                "<owner>/<repo>`."
                            )
                    except Exception as e:
                        _log.warning(
                            "bridge: failed to notify orphan-reply sender: %s",
                            e,
                        )
                return
            self._pending_questions.pop(match.request_id, None)

        _log.info(
            "bridge: delivering ANSWER request_id=%s len=%d",
            match.request_id, len(text),
        )
        log_event(
            _logger,
            "dev.answer.received",
            transport="telegram",
            source=f"telegram:chat={self.chat_id}",
            request_id=match.request_id,
            telegram_msg_id=match.telegram_msg_id,
            reply_to_message_id=reply_to_message_id,
            answer=text,
            answer_length=len(text),
            answer_hash=hash_text(text),
        )
        answer = BridgeMessage(
            op=BridgeOp.ANSWER,
            request_id=match.request_id,
            answer=text,
            answered_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            match.writer.write(serialize_message(answer).encode())
            await match.writer.drain()
        except Exception as e:
            _log.warning(
                "bridge: failed to deliver ANSWER request_id=%s err=%s",
                match.request_id, e,
            )

    async def _queue_orphan_reply_as_resume_answer(
        self, text: str
    ) -> dict:
        """Try to route an orphan Telegram reply to a persisted BLOCKED
        session so the pending-resume sweeper can pick it up and drive a
        pipeline resume.

        Returns a dict with ``status`` set to one of:
        - ``"queued"`` with ``row`` (dict) — answer was attached.
        - ``"ambiguous"`` with ``rows`` (list[dict]) — multiple BLOCKED
          sessions exist and the reply didn't name one, so we refuse to
          guess. The sender is told which session_ids exist so they can
          retry with one included.
        - ``"none"`` — no state_db, no unanswered rows, or DB error.

        Disambiguation rule: if the reply text contains exactly one of
        the unanswered session_ids as a substring, route to that row.
        Otherwise, with >1 unanswered rows and no substring match,
        return ambiguous. With exactly one unanswered row and no
        substring match, route anyway (single-repo case is unambiguous).
        """
        if self.state_db is None:
            return {"status": "none"}
        try:
            rows = self.state_db.list_unanswered_pending_resumes()
        except Exception as e:
            log_event(
                _logger,
                "bridge.pending_resume.list_failed",
                reason=type(e).__name__,
                error=str(e)[:200],
            )
            return {"status": "none"}

        if not rows:
            return {"status": "none"}

        matched_by_id = [r for r in rows if r["session_id"] in text]
        if len(matched_by_id) == 1:
            target = matched_by_id[0]
        elif len(matched_by_id) > 1:
            # Multiple session_ids named in the same reply — refuse to
            # pick one. Let the operator send a single-session reply.
            return {"status": "ambiguous", "rows": matched_by_id}
        elif len(rows) == 1:
            target = rows[0]
        else:
            # Multiple unanswered, no session_id hint — can't route safely.
            return {"status": "ambiguous", "rows": rows}

        try:
            if not self.state_db.answer_pending_resume(
                target["session_id"], text
            ):
                return {"status": "none"}
        except Exception as e:
            log_event(
                _logger,
                "bridge.pending_resume.update_failed",
                reason=type(e).__name__,
                error=str(e)[:200],
            )
            return {"status": "none"}

        log_event(
            _logger,
            "bridge.pending_resume.queued",
            session_id=target["session_id"],
            pipeline=target["pipeline"],
            repo=target["repo"],
            answer_length=len(text),
            answer_hash=hash_text(text),
        )
        return {"status": "queued", "row": target}
