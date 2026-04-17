"""Tests for Telegram handler."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestTelegramHandler:
    @pytest.mark.asyncio
    async def test_send_message(self) -> None:
        """Should send message via Telegram API."""
        from dev_sync.bridge.telegram_handler import TelegramHandler

        with patch("dev_sync.bridge.telegram_handler.Bot") as MockBot:
            mock_bot = AsyncMock()
            MockBot.return_value = mock_bot

            handler = TelegramHandler(bot_token="test-token", chat_id=12345)
            await handler.send("Hello world")

            mock_bot.send_message.assert_called_once_with(
                chat_id=12345,
                text="Hello world",
            )

    @pytest.mark.asyncio
    async def test_ask_sends_with_keyboard(self) -> None:
        """Should send question with reply keyboard."""
        from dev_sync.bridge.telegram_handler import TelegramHandler

        with patch("dev_sync.bridge.telegram_handler.Bot") as MockBot:
            mock_bot = AsyncMock()
            mock_message = MagicMock()
            mock_message.message_id = 42
            mock_bot.send_message.return_value = mock_message
            MockBot.return_value = mock_bot

            handler = TelegramHandler(bot_token="test-token", chat_id=12345)
            msg_id = await handler.ask("Approve?", options=["yes", "no"])

            assert msg_id == 42
            mock_bot.send_message.assert_called_once()
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert "Approve?" in call_kwargs["text"]
            assert call_kwargs["reply_markup"] is not None
