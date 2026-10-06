"""Tests for the Mattermost chat handler.

No network: the REST calls go through a stubbed httpx transport and the
WebSocket is driven by a fake that yields canned frames. The behaviours
asserted here were measured against a real server (Mattermost 11.7.11)
before the handler was written, so these tests encode observations rather
than guesses — most importantly that **a bot's own posts come back on its
own socket**, which without a filter makes the handler answer its own
question instantly.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ctrlrelay.bridge.handler import ChatHandler
from ctrlrelay.bridge.mattermost_handler import MattermostHandler

BOT_ID = "bot-user-id-0000000000000"
CHANNEL = "chan-0000000000000000000000"


def _handler(responder, **kw) -> MattermostHandler:
    """A handler whose HTTP client is backed by ``responder``."""
    h = MattermostHandler(
        url="https://chat.example.com",
        bot_token="tok",
        channel_id=CHANNEL,
        **kw,
    )
    h._client = httpx.AsyncClient(
        base_url="https://chat.example.com/api/v4",
        transport=httpx.MockTransport(responder),
    )
    return h


def _ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/users/me"):
        return httpx.Response(200, json={"id": BOT_ID})
    if request.url.path.endswith("/posts"):
        body = json.loads(request.content)
        return httpx.Response(201, json={"id": "post-abc", **body})
    return httpx.Response(404, json={"message": "no route"})


class TestItSatisfiesTheSeam:
    def test_it_is_a_chat_handler(self) -> None:
        assert isinstance(_handler(_ok), ChatHandler)

    def test_it_names_itself_and_its_destination(self) -> None:
        h = _handler(_ok)
        assert h.transport_name == "mattermost"
        assert h.destination == f"mattermost:chat.example.com/channel={CHANNEL}"

    def test_the_destination_carries_no_token(self) -> None:
        """These records land in log files with no retention policy."""
        assert "tok" not in _handler(_ok).destination


class TestPosting:
    @pytest.mark.asyncio
    async def test_send_returns_a_string_post_id(self) -> None:
        h = _handler(_ok)
        post_id = await h.send("hello")
        assert post_id == "post-abc"
        assert isinstance(post_id, str)
        await h.close()

    @pytest.mark.asyncio
    async def test_ask_posts_to_the_configured_channel(self) -> None:
        seen: list[dict] = []

        def responder(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/posts"):
                seen.append(json.loads(request.content))
            return _ok(request)

        h = _handler(responder)
        await h.ask("Merge #42?")
        assert seen[0]["channel_id"] == CHANNEL
        assert seen[0]["message"] == "Merge #42?"
        # A question is a new thread, never a reply to something else.
        assert "root_id" not in seen[0]
        await h.close()

    @pytest.mark.asyncio
    async def test_options_are_rendered_rather_than_dropped(self) -> None:
        """Mattermost has no reply keyboard without an inbound HTTP
        callback. Rendering the choices as text keeps the information the
        pipeline deliberately offered; dropping them silently loses it."""
        seen: list[dict] = []

        def responder(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/posts"):
                seen.append(json.loads(request.content))
            return _ok(request)

        h = _handler(responder)
        await h.ask("Merge?", options=["yes", "no, hold it"])
        message = seen[0]["message"]
        assert "Merge?" in message
        assert "yes" in message and "no, hold it" in message
        await h.close()


class TestAmbiguousDelivery:
    """Whether a failed post may nonetheless have landed.

    The division is *did the server get a chance to act on it*, which is
    finer than "was it a timeout" — and the counter-intuitive part is that
    a 500 is a better outcome than a read timeout, because it is an answer.
    """

    @pytest.fixture
    def h(self) -> MattermostHandler:
        return _handler(_ok)

    def test_a_read_timeout_is_unknown(self, h) -> None:
        assert h.is_ambiguous_delivery(httpx.ReadTimeout("slow"))

    def test_a_broken_stream_is_unknown(self, h) -> None:
        assert h.is_ambiguous_delivery(httpx.RemoteProtocolError("truncated"))

    def test_never_connecting_is_a_definite_failure(self, h) -> None:
        """Nothing was delivered, so claiming "unknown" would park a
        session as possibly-asked when it certainly was not."""
        assert not h.is_ambiguous_delivery(httpx.ConnectError("refused"))
        assert not h.is_ambiguous_delivery(httpx.ConnectTimeout("no route"))

    def test_an_http_error_is_a_definite_failure(self, h) -> None:
        request = httpx.Request("POST", "https://chat.example.com/api/v4/posts")
        exc = httpx.HTTPStatusError(
            "500", request=request, response=httpx.Response(500, request=request)
        )
        assert not h.is_ambiguous_delivery(exc)

    def test_an_unrelated_exception_is_not_ambiguous(self, h) -> None:
        assert not h.is_ambiguous_delivery(ValueError("nothing to do with it"))


class _FakeSocket:
    """Yields canned frames then blocks, like a real idle socket."""

    def __init__(self, frames: list[dict]) -> None:
        self._frames = list(frames)
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if self._frames:
            return json.dumps(self._frames.pop(0))
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


def _posted(**post) -> dict:
    base = {
        "id": "p1",
        "root_id": "",
        "user_id": "human-0000000000000000000",
        "channel_id": CHANNEL,
        "message": "approved",
    }
    base.update(post)
    return {"event": "posted", "data": {"post": json.dumps(base)}}


async def _drain(h: MattermostHandler, frames: list[dict]) -> list[tuple]:
    """Run one pass of the consume loop over ``frames``."""
    got: list[tuple] = []

    async def collect(text: str, reply_to: str | None) -> None:
        got.append((text, reply_to))

    ws = _FakeSocket(frames)
    task = asyncio.create_task(h._consume(ws, collect))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not ws._frames:
            break
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return got


class TestInboundRouting:
    @pytest.mark.asyncio
    async def test_a_threaded_reply_carries_the_question_post_id(self) -> None:
        """root_id is the direct counterpart of Telegram's
        reply_to_message_id, and is what lets a late answer reach the
        session that asked. Measured against a real server."""
        h = _handler(_ok)
        got = await _drain(h, [_posted(id="p2", root_id="question-post-id")])
        assert got == [("approved", "question-post-id")]
        await h.close()

    @pytest.mark.asyncio
    async def test_a_top_level_message_reports_no_reply_target(self) -> None:
        """Mattermost sends root_id="" for a fresh post. It must arrive as
        None so the bridge's "fresh message" branch means the same thing on
        both transports — "" is truthy enough to look like an id and would
        match nothing."""
        h = _handler(_ok)
        got = await _drain(h, [_posted(root_id="")])
        assert got == [("approved", None)]
        await h.close()

    @pytest.mark.asyncio
    async def test_the_bots_own_post_is_ignored(self) -> None:
        """THE one that matters. A bot's post arrives back on the bot's own
        socket — measured, not assumed. Without this filter the handler
        reads the question it just asked as the answer to that question,
        immediately and every time, and the session resumes with its own
        text as the operator's decision."""
        h = _handler(_ok)
        got = await _drain(h, [_posted(user_id=BOT_ID, message="Merge #42?")])
        assert got == []
        await h.close()

    @pytest.mark.asyncio
    async def test_another_channel_is_ignored(self) -> None:
        h = _handler(_ok)
        got = await _drain(h, [_posted(channel_id="some-other-channel")])
        assert got == []
        await h.close()

    @pytest.mark.asyncio
    async def test_non_post_events_and_junk_are_skipped(self) -> None:
        """A real socket carries typing notifications, status changes and
        presence events. None of them is an answer."""
        h = _handler(_ok)
        got = await _drain(h, [
            {"event": "typing", "data": {}},
            {"event": "posted", "data": {}},
            {"event": "posted", "data": {"post": "not json"}},
            _posted(message="   "),
            _posted(id="p9", message="real answer"),
        ])
        assert got == [("real answer", None)]
        await h.close()

    @pytest.mark.asyncio
    async def test_a_raising_callback_does_not_kill_the_stream(self) -> None:
        """One bad reply must not end the session's only channel back to
        the operator."""
        h = _handler(_ok)
        seen: list[str] = []

        async def explode(text: str, reply_to: str | None) -> None:
            seen.append(text)
            if len(seen) == 1:
                raise RuntimeError("routing blew up")

        ws = _FakeSocket([_posted(message="first"), _posted(message="second")])
        task = asyncio.create_task(h._consume(ws, explode))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not ws._frames:
                break
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert seen == ["first", "second"]
        await h.close()


class TestWebSocketSetup:
    def test_https_becomes_wss(self) -> None:
        assert _handler(_ok)._ws_url == "wss://chat.example.com/api/v4/websocket"

    def test_http_becomes_ws(self) -> None:
        """A self-hosted instance on a private network may be plain HTTP.
        Hardcoding wss there fails the handshake with a TLS error that
        reads like a certificate problem."""
        h = MattermostHandler(
            url="http://localhost:8065", bot_token="t", channel_id=CHANNEL
        )
        assert h._ws_url == "ws://localhost:8065/api/v4/websocket"

    @pytest.mark.asyncio
    async def test_start_polling_is_idempotent(self) -> None:
        h = _handler(_ok)

        async def noop(text: str, reply_to: str | None) -> None:
            return None

        # Never actually connects: the loop's first act is a connect that
        # fails against a host that does not resolve, and it backs off
        # rather than raising. What is asserted is that a second call does
        # not start a second task.
        await h.start_polling(noop)
        first = h._poll_task
        await h.start_polling(noop)
        assert h._poll_task is first
        await h.stop_polling()
        assert h._poll_task is None
        await h.close()
