"""Telegram Bot API handler."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from telegram import Bot, ReplyKeyboardMarkup, ReplyKeyboardRemove

_log = logging.getLogger(__name__)

IncomingMessageHandler = Callable[[str, int | None], Awaitable[None]]


class TelegramHandler:
    """Handles Telegram Bot API communication — outbound (send/ask) and
    inbound (long-poll get_updates) for answers from the operator."""

    def __init__(self, bot_token: str, chat_id: int) -> None:
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id
        self._poll_task: asyncio.Task | None = None
        self._offset: int = 0

    async def send(self, text: str) -> int:
        """Send a message to the configured chat."""
        message = await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
        )
        return message.message_id

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
    ) -> int:
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
        return message.message_id

    async def start_polling(self, handler: IncomingMessageHandler) -> None:
        """Start long-polling Telegram for incoming messages from the
        configured chat. For each message, invokes
        ``handler(text, reply_to_message_id)`` where reply_to_message_id is
        the id of the question the user replied to (or None for a fresh
        message). Idempotent — a second call replaces the running loop."""
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
                    msg.reply_to_message.message_id
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
