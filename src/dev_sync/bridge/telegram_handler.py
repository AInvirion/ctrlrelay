"""Telegram Bot API handler."""

from __future__ import annotations

from telegram import Bot, ReplyKeyboardMarkup, ReplyKeyboardRemove


class TelegramHandler:
    """Handles Telegram Bot API communication."""

    def __init__(self, bot_token: str, chat_id: int) -> None:
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id

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

    async def close(self) -> None:
        """Close the bot session."""
        await self.bot.close()
