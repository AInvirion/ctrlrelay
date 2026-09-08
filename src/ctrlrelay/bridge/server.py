"""Bridge server for Telegram communication."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import time
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
from ctrlrelay.bridge.telegram_handler import (
    TelegramHandler,
    is_ambiguous_delivery,
)
from ctrlrelay.core.obs import get_logger, hash_text, log_event

if TYPE_CHECKING:
    from ctrlrelay.core.state import StateDB

_logger = get_logger("bridge.server")
_log = logging.getLogger(__name__)


# Upper bound on the telegram_msg_id -> session_id map. A reply landing
# further back than this many questions is vanishingly rare, and the map is
# a routing convenience, not a source of truth — losing an entry degrades to
# the session_id-substring path, never to a wrong resume.
_ASKED_SESSIONS_MAX = 500


def format_question(
    question: str,
    *,
    repo: str | None = None,
    session_id: str | None = None,
    issue_number: int | None = None,
) -> str:
    """Prefix an agent question with the repo and session it belongs to.

    A secops sweep posts one consolidated question per repo back-to-back,
    and the agent's own text almost never names its repo — the operator sees
    a run of near-identical "approve #387?" messages with nothing to tell
    them apart. The session_id matters just as much: when a reply arrives
    after the ASK socket has timed out, the orphan router can only
    disambiguate between several BLOCKED sessions if the operator can quote
    one, and until now it was never sent to them.

    Plain text on purpose — TelegramHandler sends without ``parse_mode``, so
    any Markdown here would reach the operator as literal backticks.
    """
    header: list[str] = []
    if repo:
        line = f"[{repo}]"
        if issue_number is not None:
            line += f" issue #{issue_number}"
        header.append(line)
    if session_id:
        header.append(f"session: {session_id}")
    if not header:
        return question
    header.append("Reply to this message to answer.")
    return "\n".join(header) + "\n\n" + question


def _deadline(received_at: float, timeout: int | None) -> float | None:
    """Monotonic instant after which a posted question is no longer live.

    Mirrors the client's own ``ask()`` deadline as closely as the two
    processes allow. The client starts counting when it writes the ASK to
    the socket and ``received_at`` is taken as we read it, so the two clocks
    differ only by unix-socket transit — microseconds. Anchoring here rather
    than after the Telegram post is what matters: that post takes on the
    order of a second, and a deadline set on its far side would leave the
    bridge treating a question as live for that whole second after the
    client had abandoned it.

    Deliberately no safety margin. Skewing early looks free but is not: a
    reply landing between the bridge's deadline and the client's is routed
    to a ``pending_resumes`` row the still-running pipeline has not written
    yet, so the answer is refused and the operator has to send it again. A
    margin trades a microsecond-wide hazard for a window thousands of times
    wider.

    ``None`` means the caller set no deadline; the entry then lives until
    its socket closes, the pre-existing behaviour. A non-positive timeout
    means the client gave up immediately, so the question is already
    expired rather than eternal.
    """
    if timeout is None:
        return None
    return received_at + max(float(timeout), 0.0)


class _PendingQuestion:
    """Question posted to Telegram, awaiting the operator's reply."""

    __slots__ = (
        "request_id", "telegram_msg_id", "writer", "expires_at",
        "repo", "session_id",
    )

    def __init__(
        self,
        request_id: str,
        telegram_msg_id: int,
        writer: asyncio.StreamWriter,
        expires_at: float | None = None,
        repo: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.request_id = request_id
        self.telegram_msg_id = telegram_msg_id
        self.writer = writer
        self.repo = repo
        self.session_id = session_id
        # Monotonic deadline mirroring the client's own ask() timeout. The
        # client stops waiting at this point and discards the request_id,
        # but it does NOT close the socket — a secops sweep reuses one
        # transport across every repo. Without this the bridge would keep
        # treating the question as live and write the answer into a request
        # nobody is listening for.
        self.expires_at = expires_at

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


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
        # telegram_msg_id -> session_id for every ASK we've posted, kept
        # after the question itself is gone. The pipeline process is what
        # dies on an ASK timeout; this bridge outlives it, so a reply-to on
        # a long-expired question can still name its session exactly instead
        # of falling back to "which of these 12 did you mean?".
        self._asked_sessions: OrderedDict[int, str] = OrderedDict()

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
                telegram_msg_id = await self._telegram.send(msg.text or "")
                # A SEND that names a session is answerable too: the secops
                # sweep fans out one "blocked on <repo>" message per repo
                # after the run, and that message is the most recent — and
                # most prominent — one the operator sees for that repo. If
                # its id isn't recorded, replying to it lands in the
                # ambiguous branch even though the session is unmistakable.
                if msg.session_id:
                    async with self._pending_lock:
                        self._remember_asked_session(
                            telegram_msg_id, msg.session_id
                        )
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
            question = msg.question or ""
            # Hash + length only: these records land in the journal and in
            # poller.log, so the question text itself would persist there in
            # plaintext indefinitely. The hash still joins this event to the
            # client's own dev.question.posted. Built outside the try so the
            # failure path can always describe what failed.
            post_fields = {
                "session_id": msg.session_id,
                "repo": msg.repo,
                "issue_number": msg.issue_number,
                "transport": "telegram",
                "destination": f"telegram:chat={self.chat_id}",
                "request_id": msg.request_id,
                "question_length": len(question),
                "question_hash": hash_text(question),
                "options": msg.options,
            }
            try:
                assert self._telegram is not None
                # Start the expiry clock before the Telegram round trip, not
                # after it: the client began counting when it wrote the ASK
                # to the socket. Anchoring on the far side of the post would
                # leave the bridge treating the question as live for a full
                # post latency after the client had abandoned it, and a
                # reply-to in that window writes the answer into a dead
                # request.
                received_at = time.monotonic()
                telegram_msg_id = await self._telegram.ask(
                    format_question(
                        question,
                        repo=msg.repo,
                        session_id=msg.session_id,
                        issue_number=msg.issue_number,
                    ),
                    options=msg.options,
                )
                async with self._pending_lock:
                    if msg.request_id:
                        self._pending_questions[msg.request_id] = _PendingQuestion(
                            request_id=msg.request_id,
                            telegram_msg_id=telegram_msg_id,
                            writer=writer,
                            expires_at=_deadline(received_at, msg.timeout),
                            repo=msg.repo,
                            session_id=msg.session_id,
                        )
                    if msg.session_id:
                        self._remember_asked_session(
                            telegram_msg_id, msg.session_id
                        )
                _log.info(
                    "bridge: ASK posted request_id=%s telegram_msg_id=%s",
                    msg.request_id, telegram_msg_id,
                )
                # Emitted here, not before the send: until Telegram has
                # accepted the message there is nothing posted to claim.
                log_event(
                    _logger,
                    "dev.question.posted",
                    **post_fields,
                    telegram_msg_id=telegram_msg_id,
                )
                return BridgeMessage(
                    op=BridgeOp.ACK, request_id=msg.request_id, status="pending",
                )
            except Exception as e:
                _log.warning("bridge: ASK failed, request_id=%s err=%s", msg.request_id, e)
                # Same rule as the transport: only claim a definite
                # failure when Telegram actually answered. A timeout or a
                # bare transport error can be raised after Telegram
                # accepted the message, in which case the question IS on
                # the operator's phone and "post_failed" is a lie.
                #
                # The taxonomy is not intuitive, so it lives next to the
                # library that defines it — see is_ambiguous_delivery.
                unknown = is_ambiguous_delivery(e)
                log_event(
                    _logger,
                    "dev.question.post_unknown"
                    if unknown
                    else "dev.question.post_failed",
                    **post_fields,
                    reason=type(e).__name__,
                    error=str(e)[:200],
                )
                return BridgeMessage(
                    op=BridgeOp.ERROR,
                    request_id=msg.request_id,
                    error="telegram_api_error",
                    message=str(e),
                )

        return None

    def _remember_asked_session(
        self, telegram_msg_id: int, session_id: str
    ) -> None:
        """Record which session a posted question belongs to.

        Caller must hold ``_pending_lock``."""
        self._asked_sessions[telegram_msg_id] = session_id
        self._asked_sessions.move_to_end(telegram_msg_id)
        while len(self._asked_sessions) > _ASKED_SESSIONS_MAX:
            self._asked_sessions.popitem(last=False)

    async def _on_telegram_reply(
        self,
        text: str,
        reply_to_message_id: int | None,
    ) -> None:
        """Route an incoming Telegram message to the matching pending question.

        Routing rules, in order:

        1. ``reply_to_message_id`` names a live pending question — deliver
           there. This is the only path that can be right when more than one
           question is outstanding, so it takes priority.
        2. The operator replied to *something*, but that question is no
           longer live (its ASK timed out, or the bridge restarted). We do
           NOT guess: fall through to the orphan router, which knows the
           session from ``_asked_sessions`` and can attach the answer to the
           right persisted row.
        3. A fresh message (no reply-to) with exactly one question
           outstanding — deliver there; there is nothing to confuse it with.
        4. A fresh message with several outstanding — refuse, and tell the
           operator to use Telegram's reply.

        The previous FIFO fallback ran for cases 2 and 4 alike: replying to
        the third question while the first was still open delivered the
        answer to the *first* one, silently, and the operator had no way to
        see it had happened.

        ``_pending_lock`` guards only the decision. Every Telegram send and
        every state_db write happens after it is released — holding it across
        a stalled Telegram call would serialize routing behind the network.
        Telegram updates are dispatched one at a time by
        ``TelegramHandler._poll_loop``, so releasing early cannot interleave
        two replies.
        """
        match: _PendingQuestion | None = None
        hinted_session_id: str | None = None
        live_listing: str | None = None
        ambiguous_notice: str | None = None

        async with self._pending_lock:
            self._drop_expired_questions()
            if reply_to_message_id is not None:
                for q in self._pending_questions.values():
                    if q.telegram_msg_id == reply_to_message_id:
                        match = q
                        break
            elif len(self._pending_questions) == 1:
                match = next(iter(self._pending_questions.values()))
            elif len(self._pending_questions) > 1:
                ambiguous_notice = (
                    "Your message wasn't routed - "
                    f"{len(self._pending_questions)} questions are waiting "
                    "and a plain message doesn't say which one you "
                    "mean.\n\n"
                    "Use Telegram's reply on the question you want to "
                    "answer.\n\n"
                    f"{self._live_question_listing()}"
                )

            if match is not None:
                self._pending_questions.pop(match.request_id, None)
            elif ambiguous_notice is None:
                # Snapshot what the orphan branch needs, so the lock can be
                # released before we touch Telegram or the database.
                hinted_session_id = (
                    self._asked_sessions.get(reply_to_message_id)
                    if reply_to_message_id is not None
                    else None
                )
                live_listing = (
                    self._live_question_listing()
                    if self._pending_questions
                    else None
                )

        if ambiguous_notice is not None:
            await self._send_notice(
                ambiguous_notice,
                log="bridge: ambiguous fresh reply, refusing to guess",
            )
            return

        if match is None:
            await self._route_orphan_reply(text, hinted_session_id, live_listing)
            return

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

    async def _route_orphan_reply(
        self,
        text: str,
        hinted_session_id: str | None,
        live_listing: str | None,
    ) -> None:
        """Handle a reply that matched no live question.

        Called with ``_pending_lock`` released; ``live_listing`` is the
        snapshot of what was outstanding at decision time.
        """
        _log.info(
            "bridge: incoming telegram msg with no pending question; "
            "text=%r", text[:80],
        )
        # Try to route to a persisted BLOCKED session in state_db so the
        # operator's reply actually drives a resume. Without this, the reply
        # disappears the instant the session's ASK socket closes — which is
        # exactly what happens when a scheduled secops sweep escalates
        # BLOCKED and exits.
        outcome = await self._queue_orphan_reply_as_resume_answer(
            text, session_id=hinted_session_id
        )
        if outcome["status"] == "queued":
            row = outcome["row"]
            await self._send_notice(
                "✅ Answer queued for BLOCKED session "
                f"`{row['session_id']}` "
                f"(pipeline={row['pipeline']}, repo={row['repo']}).\n"
                "The pending-resume sweeper will drive it on the next "
                "tick — you'll get another message with the result."
            )
        elif outcome["status"] == "ambiguous":
            pending_list = "\n".join(
                f"  • `{r['session_id']}` ({r['repo']}): "
                f"{(r['question'] or '')[:80]}"
                for r in outcome["rows"]
            )
            await self._send_notice(
                "⚠️ Your reply wasn't routed — multiple BLOCKED sessions "
                "are unanswered and the reply didn't point at one of "
                "them.\n\n"
                f"Pending:\n{pending_list}\n\n"
                "Use Telegram's reply on the question you mean, or paste "
                "its session_id anywhere in your message."
            )
        elif live_listing is not None:
            # Reply-to pointed at a message this bridge no longer tracks
            # (restarted, or evicted from _asked_sessions) and nothing is
            # persisted for it — but questions ARE live. Saying "no active
            # session is waiting" here would be flatly wrong and would stop
            # the operator retrying, so point them at what is waiting.
            await self._send_notice(
                "⚠️ Your reply wasn't routed — it pointed at a question "
                "this bridge no longer tracks, and nothing is persisted "
                f"for it.\n\n{live_listing}\n\n"
                "Reply to one of those messages to answer it."
            )
        else:
            await self._send_notice(
                "⚠️ Your reply wasn't routed — no active session is "
                "waiting on input and no persisted BLOCKED session is "
                "unanswered. To act manually, re-run the pipeline, e.g. "
                "`ctrlrelay run secops --repo <owner>/<repo>`."
            )

    def _drop_expired_questions(self) -> None:
        """Forget questions whose client has already stopped waiting.

        Caller must hold ``_pending_lock``. An expired entry is worse than
        no entry: it would match a reply-to and swallow the answer, and it
        inflates the "more than one question is live" count so that a plain
        answer to the one real question gets refused. Dropping it here sends
        the reply down the orphan path, which resolves the session's
        ``pending_resumes`` row via ``_asked_sessions``.
        """
        now = time.monotonic()
        expired = [
            rid for rid, q in self._pending_questions.items() if q.is_expired(now)
        ]
        for rid in expired:
            self._pending_questions.pop(rid, None)
        if expired:
            _log.info(
                "bridge: dropped %d expired question(s): %s",
                len(expired), ", ".join(expired),
            )

    def _live_question_listing(self) -> str:
        """Render the outstanding questions for an operator notice.

        Uses the same ``[repo] session:`` shape as the question header, so
        the operator can match a line here to a message in their chat by
        eye. Telegram never shows message ids in its UI and ``request_id``
        means nothing to a human, so neither belongs in operator-facing
        text.

        Caller must hold ``_pending_lock``."""
        rows = "\n".join(
            f"  - {q.repo or 'unknown repo'}"
            + (f" (session: {q.session_id})" if q.session_id else "")
            for q in self._pending_questions.values()
        )
        return f"Still waiting:\n{rows}"

    async def _send_notice(self, text: str, log: str | None = None) -> None:
        """Best-effort operator notice. A failure here must never take down
        reply routing — the answer has already been dealt with."""
        if log:
            _log.info("%s", log)
        if self._telegram is None:
            return
        try:
            await self._telegram.send(text)
        except Exception as e:
            _log.warning("bridge: failed to notify operator: %s", e)

    async def _queue_orphan_reply_as_resume_answer(
        self, text: str, *, session_id: str | None = None
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

        Disambiguation rules, in order:

        1. ``session_id`` (resolved from the Telegram reply-to) names an
           unanswered row — route there. This is exact: it comes from the
           message the operator actually replied to.
        2. The reply text contains exactly one unanswered session_id as a
           substring — route there.
        3. Exactly one unanswered row exists — route there.
        4. Anything else — ambiguous, and we refuse to guess.
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

        # A reply-to that resolves to a session we posted beats every
        # heuristic below — the operator pointed at the exact question.
        if session_id is not None:
            hinted = [r for r in rows if r["session_id"] == session_id]
            if hinted:
                return await self._attach_orphan_answer(hinted[0], text)

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

        return await self._attach_orphan_answer(target, text)

    async def _attach_orphan_answer(self, target: dict, text: str) -> dict:
        """Write an orphan answer onto one pending_resumes row."""
        assert self.state_db is not None
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
