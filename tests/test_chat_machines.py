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
        """B restarted, or never knew A's post, and has a question of its
        own waiting in its database: A's answer must not be applied to it,
        and B must say nothing."""
        from ctrlrelay.core.state import StateDB

        channel, a, b = _bridges(tmp_path, shared=False)
        b.state_db = StateDB(tmp_path / "b.db")
        b.state_db.add_pending_resume("dev-b-1", "dev", "o/r", "merge it?")
        await _ask(a, "pin or bump?")
        a_post = next(iter(channel.posts))
        posts_before = dict(channel.posts)
        try:
            await b._on_reply("pin", reply_to_post_id=a_post)
            [row] = b.state_db.list_unanswered_pending_resumes()
            assert row["session_id"] == "dev-b-1"  # still unanswered
        finally:
            b.state_db.close()
        assert channel.posts == posts_before

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
    async def test_on_a_shared_chat_an_unreadable_post_is_left_alone(self, tmp_path) -> None:
        _, a, _ = _bridges(tmp_path, shared=True)
        a.handler.post_text = AsyncMock(side_effect=RuntimeError("502"))
        a._queue_orphan_reply_as_resume_answer = AsyncMock()
        await a._on_reply("pin", reply_to_post_id="p999")
        a._queue_orphan_reply_as_resume_answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_its_own_chat_an_unreadable_post_is_routed_as_before(
        self, tmp_path,
    ) -> None:
        channel, a, _ = _bridges(tmp_path, shared=False)
        a.handler.post_text = AsyncMock(side_effect=RuntimeError("502"))
        a._queue_orphan_reply_as_resume_answer = AsyncMock(
            return_value={"status": "none"},
        )
        a._send_notice = AsyncMock()
        await a._on_reply("pin", reply_to_post_id="p999")
        a._queue_orphan_reply_as_resume_answer.assert_awaited_once()


class TestTelegramOneReaderPerBot:
    """Telegram cuts off the OLDER reader when a second starts, so the
    machine that was running is the one that must say so and stop."""

    @pytest.mark.asyncio
    async def test_a_conflict_while_running_stops_instead_of_retrying(self, caplog) -> None:
        from telegram.error import Conflict

        from ctrlrelay.bridge.telegram_handler import TelegramHandler

        h = TelegramHandler.__new__(TelegramHandler)
        h._offset = 0
        h._replied_texts = {}
        h.node_label = None
        h.chat_id = 1
        h.bot = MagicMock()
        h.bot.get_updates = AsyncMock(side_effect=Conflict("terminated by other getUpdates"))
        h.send = AsyncMock()
        await h._poll_loop(AsyncMock())  # returns: no retry loop
        assert h.bot.get_updates.await_count == 1
        assert "each machine needs its own bot" in caplog.text
        # Said once in the chat too, where the operator will see it.
        [call] = h.send.await_args_list
        assert "Stopped reading replies on this machine" in call.args[0]

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
            reply_to_message=SimpleNamespace(
            message_id=42, text="[node-b] pin or bump?",
            from_user=SimpleNamespace(id=777, is_bot=True),
        ),
        ))
        calls = [[update]]

        async def get_updates(**kw):
            if calls:
                return calls.pop()
            raise __import__("asyncio").CancelledError

        h.bot = MagicMock()
        h.bot.id = 777
        h.bot.get_updates = get_updates
        with pytest.raises(__import__("asyncio").CancelledError):
            await h._poll_loop(AsyncMock())
        assert await h.post_text("42") == "[node-b] pin or bump?"



class TestRoundOneFixes:
    @pytest.mark.asyncio
    async def test_on_its_own_chat_an_answer_starting_with_its_name_is_kept_whole(
        self, tmp_path,
    ) -> None:
        _, a, _ = _bridges(tmp_path, shared=False)
        wa = await _ask(a, "status?")
        await a._on_reply("node-a is healthy", reply_to_post_id=None)
        assert _answers(wa) == ["node-a is healthy"]

    @pytest.mark.asyncio
    async def test_a_person_typing_a_tag_does_not_speak_for_a_machine(self) -> None:
        from ctrlrelay.bridge.mattermost_handler import MattermostHandler

        h = MattermostHandler.__new__(MattermostHandler)
        h._bot_user_id = "bot"
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"user_id": "a-person", "message": "[node-b] x"})
        h._client = MagicMock()
        h._client.get = AsyncMock(return_value=resp)
        assert await h.post_text("p1") == ""
        resp.json = MagicMock(return_value={"user_id": "bot", "message": "[node-b] x"})
        assert await h.post_text("p1") == "[node-b] x"

    @pytest.mark.asyncio
    async def test_on_telegram_only_the_bots_own_message_keeps_its_tag(self) -> None:
        import asyncio
        from types import SimpleNamespace

        from ctrlrelay.bridge.telegram_handler import TelegramHandler

        h = TelegramHandler.__new__(TelegramHandler)
        h._offset = 0
        h._replied_texts = {}
        h.chat_id = 1
        h.bot = MagicMock()
        h.bot.id = 777

        def update(uid: int, mid: int, sender_id: int, is_bot: bool):
            return SimpleNamespace(update_id=uid, message=SimpleNamespace(
                chat=SimpleNamespace(id=1), text="pin",
                reply_to_message=SimpleNamespace(
                    message_id=mid, text="[node-b] pin or bump?",
                    from_user=SimpleNamespace(id=sender_id, is_bot=is_bot),
                ),
            ))

        batches = [[update(1, 10, 777, True), update(2, 11, 5, False)]]

        async def get_updates(**kw):
            if batches:
                return batches.pop()
            raise asyncio.CancelledError

        h.bot.get_updates = get_updates
        with pytest.raises(asyncio.CancelledError):
            await h._poll_loop(AsyncMock())
        assert await h.post_text("10") == "[node-b] pin or bump?"
        assert await h.post_text("11") == ""


    @pytest.mark.asyncio
    async def test_on_a_shared_chat_a_reply_to_an_untagged_post_is_left_alone(
        self, tmp_path,
    ) -> None:
        channel, a, b = _bridges(tmp_path, shared=True)
        wa = await _ask(a, "pin or bump?")
        wb = await _ask(b, "merge it?")
        channel.posts["p-person"] = ""  # a person's post: reads back untagged
        a._queue_orphan_reply_as_resume_answer = AsyncMock()
        b._queue_orphan_reply_as_resume_answer = AsyncMock()
        posts_before = dict(channel.posts)
        await a._on_reply("pin", reply_to_post_id="p-person")
        await b._on_reply("pin", reply_to_post_id="p-person")
        a._queue_orphan_reply_as_resume_answer.assert_not_called()
        b._queue_orphan_reply_as_resume_answer.assert_not_called()
        assert channel.posts == posts_before
        assert _answers(wa) == [] and _answers(wb) == []

    @pytest.mark.asyncio
    async def test_naming_the_machine_in_a_reply_to_an_old_untagged_question_works(
        self, tmp_path,
    ) -> None:
        channel, a, b = _bridges(tmp_path, shared=True)
        channel.posts["p-old"] = ""  # a question from before this version
        a._queue_orphan_reply_as_resume_answer = AsyncMock(return_value={"status": "none"})
        a._send_notice = AsyncMock()
        b._queue_orphan_reply_as_resume_answer = AsyncMock()
        await a._on_reply("node-a: pin", reply_to_post_id="p-old")
        await b._on_reply("node-a: pin", reply_to_post_id="p-old")
        a._queue_orphan_reply_as_resume_answer.assert_awaited_once()
        assert a._queue_orphan_reply_as_resume_answer.await_args.args[0] == "pin"
        b._queue_orphan_reply_as_resume_answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_telegram_stop_notice_names_its_machine(self, tmp_path) -> None:
        from telegram.error import Conflict

        from ctrlrelay.bridge.server import BridgeServer
        from ctrlrelay.bridge.telegram_handler import TelegramHandler

        h = TelegramHandler.__new__(TelegramHandler)
        h._offset = 0
        h._replied_texts = {}
        h.node_label = None
        h.chat_id = 1
        h.bot = MagicMock()
        h.bot.get_updates = AsyncMock(side_effect=Conflict("terminated by other getUpdates"))
        h.send = AsyncMock()
        BridgeServer(socket_path=tmp_path / "s", handler=h, node="node-a")
        await h._poll_loop(AsyncMock())
        assert h.send.await_args.args[0].startswith("[node-a] ")
