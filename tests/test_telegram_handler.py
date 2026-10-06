"""Tests for Telegram handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTelegramHandler:
    @pytest.mark.asyncio
    async def test_send_message(self) -> None:
        """Should send message via Telegram API."""
        from ctrlrelay.bridge.telegram_handler import TelegramHandler

        with patch("ctrlrelay.bridge.telegram_handler.Bot") as mock_bot_class:
            mock_bot = AsyncMock()
            mock_bot_class.return_value = mock_bot

            handler = TelegramHandler(bot_token="test-token", chat_id=12345)
            await handler.send("Hello world")

            mock_bot.send_message.assert_called_once_with(
                chat_id=12345,
                text="Hello world",
            )

    @pytest.mark.asyncio
    async def test_ask_sends_with_keyboard(self) -> None:
        """Should send question with reply keyboard."""
        from ctrlrelay.bridge.telegram_handler import TelegramHandler

        with patch("ctrlrelay.bridge.telegram_handler.Bot") as mock_bot_class:
            mock_bot = AsyncMock()
            mock_message = MagicMock()
            mock_message.message_id = 42
            mock_bot.send_message.return_value = mock_message
            mock_bot_class.return_value = mock_bot

            handler = TelegramHandler(bot_token="test-token", chat_id=12345)
            msg_id = await handler.ask("Approve?", options=["yes", "no"])

            # str, not int: post ids are strings at the ChatHandler
            # boundary so one map can key both transports' ids.
            assert msg_id == "42"
            mock_bot.send_message.assert_called_once()
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert "Approve?" in call_kwargs["text"]
            assert call_kwargs["reply_markup"] is not None


class TestTelegramHandlerSatisfiesTheSeam:
    """The bridge depends on the ChatHandler contract, not on this class.

    These are cheap and they are the only thing standing between a handler
    and a bridge that cannot route its replies. The id type is the one that
    bites silently: an int post id never equals the string key the bridge
    stores, so every reply would fall through to the orphan path and look
    like an operator mistake.
    """

    def test_it_is_a_chat_handler(self) -> None:
        from ctrlrelay.bridge.handler import ChatHandler
        from ctrlrelay.bridge.telegram_handler import TelegramHandler

        assert isinstance(
            TelegramHandler(bot_token="x:y", chat_id=12345), ChatHandler
        )

    def test_it_names_itself_and_its_destination(self) -> None:
        from ctrlrelay.bridge.telegram_handler import TelegramHandler

        h = TelegramHandler(bot_token="x:y", chat_id=12345)
        assert h.transport_name == "telegram"
        assert h.destination == "telegram:chat=12345"

    def test_a_timeout_is_ambiguous_and_a_refusal_is_not(self) -> None:
        """BadRequest subclasses NetworkError in this library, so a naive
        isinstance(exc, NetworkError) sweeps definite refusals into the
        ambiguous bucket. Asserted here because it is counter-intuitive
        and the taxonomy is what decides whether we claim a question was
        delivered."""
        from telegram.error import BadRequest, NetworkError, TimedOut

        from ctrlrelay.bridge.telegram_handler import TelegramHandler

        h = TelegramHandler(bot_token="x:y", chat_id=12345)
        assert h.is_ambiguous_delivery(TimedOut())
        assert h.is_ambiguous_delivery(NetworkError("boom"))
        assert not h.is_ambiguous_delivery(BadRequest("nope"))
