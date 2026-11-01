"""Chat with several machines on one bot (#248).

``Channel`` is one Mattermost channel shared by every machine's bridge:
every post lands in it with an id, every machine can read any post back,
and every message typed in it reaches every machine.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.bridge.node_tag import addressed, tag, tagged_node
from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, parse_message


class Channel:
    def __init__(self) -> None:
        self.posts: dict[str, str] = {}

    def handler(self) -> MagicMock:
        h = MagicMock()
        h.transport_name = "mattermost"
        h.destination = "mattermost:test"

        async def post(text: str, options=None) -> str:
            pid = f"p{len(self.posts) + 1}"
            self.posts[pid] = text
            return pid

        async def post_text(pid: str) -> str | None:
            return self.posts.get(pid)

        h.send = AsyncMock(side_effect=post)
        h.ask = AsyncMock(side_effect=post)
        h.post_text = AsyncMock(side_effect=post_text)
        h.is_ambiguous_delivery = MagicMock(return_value=False)
        return h


def _writer() -> MagicMock:
    w = MagicMock()
    w.drain = AsyncMock()
    return w


def _answers(writer: MagicMock) -> list[str]:
    out = []
    for call in writer.write.call_args_list:
        msg = parse_message(call.args[0].decode())
        if msg.op == BridgeOp.ANSWER:
            out.append(msg.answer)
    return out


def _bridges(tmp_path, *, shared: bool):
    from ctrlrelay.bridge.server import BridgeServer

    channel = Channel()
    a = BridgeServer(socket_path=tmp_path / "a.sock", handler=channel.handler(),
                     node="node-a", shared=shared)
    b = BridgeServer(socket_path=tmp_path / "b.sock", handler=channel.handler(),
                     node="node-b", shared=shared)
    return channel, a, b


async def _ask(server, question: str) -> MagicMock:
    w = _writer()
    await server._handle_message(
        BridgeMessage(op=BridgeOp.ASK, request_id=f"r-{question}", question=question), w,
    )
    return w


class TestTags:
    def test_tag_and_read_back(self) -> None:
        assert tag("node-a", "hello") == "[node-a] hello"
        assert tagged_node("[node-a] hello") == "node-a"
        assert tagged_node("hello [node-a]") is None
        assert tagged_node(None) is None

    @pytest.mark.parametrize("text,mine,rest", [
        ("node-a: retry", True, "retry"),
        ("@node-a retry", True, "retry"),
        ("node-a, retry", True, "retry"),
        ("node-b: retry", False, "node-b: retry"),
        ("retry please", False, "retry please"),
    ])
    def test_addressed(self, text, mine, rest) -> None:
        assert addressed(text, "node-a") == (mine, rest)


class TestTwoMachinesOneBot:
    @pytest.mark.asyncio
    async def test_the_chat_shows_which_machine_asked(self, tmp_path) -> None:
        channel, a, _ = _bridges(tmp_path, shared=True)
        await _ask(a, "pin or bump?")
        [text] = channel.posts.values()
        assert tagged_node(text) == "node-a"

    @pytest.mark.asyncio
    async def test_a_reply_resumes_the_machine_that_asked_and_the_other_does_nothing(
        self, tmp_path,
    ) -> None:
        channel, a, b = _bridges(tmp_path, shared=True)
        wa = await _ask(a, "pin or bump?")
        wb = await _ask(b, "merge it?")
        a_post = next(p for p, t in channel.posts.items() if t.startswith("[node-a]"))
        posts_before = dict(channel.posts)

        # Both machines hear the same reply.
        await a._on_reply("pin", reply_to_post_id=a_post)
        await b._on_reply("pin", reply_to_post_id=a_post)

        assert _answers(wa) == ["pin"]
        assert _answers(wb) == []  # B's own question untouched
        assert channel.posts == posts_before  # and B said nothing in the channel
        assert len(b._pending_questions) == 1

    @pytest.mark.asyncio
    async def test_a_reply_to_another_machines_old_question_is_not_routed_here(
        self, tmp_path,
    ) -> None:
        """B restarted, or never knew A's post: the orphan router must not
        apply A's answer to B's own persisted question."""
        channel, a, b = _bridges(tmp_path, shared=True)
        await _ask(a, "pin or bump?")
        a_post = next(iter(channel.posts))
        b._queue_orphan_reply_as_resume_answer = AsyncMock()
        await b._on_reply("pin", reply_to_post_id=a_post)
        b._queue_orphan_reply_as_resume_answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_a_shared_chat_a_plain_message_must_name_the_machine(
        self, tmp_path,
    ) -> None:
        _, a, b = _bridges(tmp_path, shared=True)
        wa = await _ask(a, "pin or bump?")
        wb = await _ask(b, "merge it?")

        # Names nobody: with one question each, both used to take it.
        await a._on_reply("pin", reply_to_post_id=None)
        await b._on_reply("pin", reply_to_post_id=None)
        assert _answers(wa) == [] and _answers(wb) == []

        await a._on_reply("node-a: pin", reply_to_post_id=None)
        await b._on_reply("node-a: pin", reply_to_post_id=None)
        assert _answers(wa) == ["pin"] and _answers(wb) == []

    @pytest.mark.asyncio
    async def test_one_machine_on_its_own_chat_keeps_the_plain_answer(self, tmp_path) -> None:
        _, a, _ = _bridges(tmp_path, shared=False)
        wa = await _ask(a, "pin or bump?")
        await a._on_reply("pin", reply_to_post_id=None)
        assert _answers(wa) == ["pin"]

    @pytest.mark.asyncio
    async def test_notices_name_the_machine_too(self, tmp_path) -> None:
        channel, a, _ = _bridges(tmp_path, shared=True)
        await a._send_notice("something")
        assert list(channel.posts.values()) == ["[node-a] something"]

    @pytest.mark.asyncio
    async def test_an_unreadable_post_is_routed_as_before(self, tmp_path) -> None:
        channel, a, _ = _bridges(tmp_path, shared=True)
        a.handler.post_text = AsyncMock(side_effect=RuntimeError("502"))
        a._queue_orphan_reply_as_resume_answer = AsyncMock(
            return_value={"status": "none"},
        )
        a._send_notice = AsyncMock()
        await a._on_reply("pin", reply_to_post_id="p999")
        a._queue_orphan_reply_as_resume_answer.assert_awaited_once()


class TestTelegramOneReaderPerBot:
    @pytest.mark.asyncio
    async def test_a_second_reader_fails_at_startup_with_one_clear_error(self) -> None:
        from telegram.error import Conflict

        from ctrlrelay.bridge.telegram_handler import TelegramBotInUseError, TelegramHandler

        h = TelegramHandler.__new__(TelegramHandler)
        h._offset = 0
        h.bot = MagicMock()
        h.bot.get_updates = AsyncMock(side_effect=Conflict("terminated by other getUpdates"))
        with pytest.raises(TelegramBotInUseError, match="each machine needs its own bot"):
            await h.preflight()

    @pytest.mark.asyncio
    async def test_a_conflict_while_running_stops_instead_of_retrying(self, caplog) -> None:
        from telegram.error import Conflict

        from ctrlrelay.bridge.telegram_handler import TelegramHandler

        h = TelegramHandler.__new__(TelegramHandler)
        h._offset = 0
        h._replied_texts = {}
        h.chat_id = 1
        h.bot = MagicMock()
        h.bot.get_updates = AsyncMock(side_effect=Conflict("terminated by other getUpdates"))
        await h._poll_loop(AsyncMock())  # returns: no retry loop
        assert h.bot.get_updates.await_count == 1
        assert "already being read" in caplog.text

    @pytest.mark.asyncio
    async def test_the_replied_to_text_is_kept_so_its_machine_can_be_read(self) -> None:
        from types import SimpleNamespace

        from ctrlrelay.bridge.telegram_handler import TelegramHandler

        h = TelegramHandler.__new__(TelegramHandler)
        h._offset = 0
        h._replied_texts = {}
        h.chat_id = 1
        update = SimpleNamespace(update_id=5, message=SimpleNamespace(
            chat=SimpleNamespace(id=1), text="pin",
            reply_to_message=SimpleNamespace(message_id=42, text="[node-b] pin or bump?"),
        ))
        calls = [[update]]

        async def get_updates(**kw):
            if calls:
                return calls.pop()
            raise __import__("asyncio").CancelledError

        h.bot = MagicMock()
        h.bot.get_updates = get_updates
        with pytest.raises(__import__("asyncio").CancelledError):
            await h._poll_loop(AsyncMock())
        assert await h.post_text("42") == "[node-b] pin or bump?"
