"""Telegram Bot API handler."""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import NetworkError, TimedOut

from ctrlrelay.bridge.handler import IncomingMessageHandler

_log = logging.getLogger(__name__)



def is_ambiguous_delivery(exc: BaseException) -> bool:
    """True when a send may have reached Telegram despite raising.

    Delivery is only confirmed by a response. A timeout or a bare
    transport error can be raised *after* Telegram accepted the message,
    so calling those a failed post would put a lie in the log while the
    question sits on the operator's phone.

    The taxonomy is not intuitive and is worth stating: `BadRequest`
    subclasses `NetworkError` here, so `isinstance(exc, NetworkError)`
    would sweep definite rejections into the ambiguous bucket. Only
    `TimedOut` and a bare `NetworkError` are genuinely unknown —
    `BadRequest`, `Forbidden`, `InvalidToken` and the rest are Telegram
    answering "no".
    """
    return isinstance(exc, TimedOut) or type(exc) is NetworkError

class TelegramHandler:
    """Handles Telegram Bot API communication — outbound (send/ask) and
    inbound (long-poll get_updates) for answers from the operator."""

    def __init__(self, bot_token: str, chat_id: int) -> None:
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id
        self._poll_task: asyncio.Task | None = None
        self._offset: int = 0

    def is_ambiguous_delivery(self, exc: BaseException) -> bool:
        """Per the ChatHandler contract. Delegates to the module-level
        function, which stays public because the taxonomy is a fact about
        python-telegram-bot rather than about this instance."""
        return is_ambiguous_delivery(exc)

    @property
    def transport_name(self) -> str:
        return "telegram"

    @property
    def destination(self) -> str:
        return f"telegram:chat={self.chat_id}"

    async def send(self, text: str) -> str:
        """Send a message to the configured chat. Returns its post id."""
        message = await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
        )
        # Stringified at the edge: Telegram's ids are ints and Mattermost's
        # are 26-char strings, and the bridge keys one map on both. See
        # ChatHandler for why that map must not hold two kinds of key.
        return str(message.message_id)

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
    ) -> str:
        """Send a question with optional reply keyboard."""
        reply_markup = None
        if options:
            keyboard = [[opt] for opt in options]
            reply_markup = ReplyKeyboardMarkup(
                keyboard,
                one_time_keyboard=True,
                resize_keyboard=True,
            )

        message = await self.bot.send_message(
            chat_id=self.chat_id,
            text=question,
            reply_markup=reply_markup or ReplyKeyboardRemove(),
        )
        return str(message.message_id)

    async def start_polling(self, handler: IncomingMessageHandler) -> None:
        """Start long-polling Telegram for incoming messages from the
        configured chat. For each message, invokes
        ``handler(text, reply_to_post_id)`` where reply_to_post_id is the
        id of the question the user replied to as a string (or None for a
        fresh message). Idempotent — a second call while the loop is
        running is a no-op; it does not replace the loop."""
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_loop(handler))

    async def stop_polling(self) -> None:
        """Stop the polling loop if running."""
        if self._poll_task is None:
            return
        self._poll_task.cancel()
        try:
            await self._poll_task
        except asyncio.CancelledError:
            pass
        self._poll_task = None

    async def _poll_loop(self, handler: IncomingMessageHandler) -> None:
        """Long-poll get_updates and forward messages from the configured chat."""
        while True:
            try:
                updates = await self.bot.get_updates(
                    offset=self._offset,
                    timeout=30,
                    allowed_updates=["message"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Transient network / auth error. Back off and keep trying.
                _log.warning("telegram get_updates failed: %s", e)
                await asyncio.sleep(5)
                continue

            for update in updates:
                self._offset = update.update_id + 1
                msg = update.message
                if msg is None or msg.chat is None:
                    continue
                if msg.chat.id != self.chat_id:
                    continue  # ignore messages from other chats
                text = (msg.text or "").strip()
                if not text:
                    continue
                reply_id = (
                    str(msg.reply_to_message.message_id)
                    if msg.reply_to_message is not None
                    else None
                )
                try:
                    await handler(text, reply_id)
                except Exception as e:
                    _log.warning("bridge answer handler raised: %s", e)

    async def close(self) -> None:
        """Close the bot session."""
        await self.stop_polling()
        await self.bot.close()
