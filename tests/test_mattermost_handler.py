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
import logging

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

    def _status(self, code: int) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "https://chat.example.com/api/v4/posts")
        return httpx.HTTPStatusError(
            str(code), request=request,
            response=httpx.Response(code, request=request),
        )

    def test_a_4xx_is_a_definite_failure(self, h) -> None:
        """The server understood the request and refused it."""
        assert not h.is_ambiguous_delivery(self._status(403))
        assert not h.is_ambiguous_delivery(self._status(404))

    def test_a_5xx_is_unknown_not_a_failure(self, h) -> None:
        """The case that reads wrong and is not.

        A reverse proxy can forward the post, let Mattermost commit it,
        then lose the upstream response and return 502 or 504. The post
        exists; only our view of it failed. Calling that definite is how a
        question already on the operator's screen is reported as never
        sent.
        """
        assert h.is_ambiguous_delivery(self._status(500))
        assert h.is_ambiguous_delivery(self._status(502))
        assert h.is_ambiguous_delivery(self._status(504))

    def test_a_pool_timeout_is_a_definite_failure(self, h) -> None:
        """The surprising TransportError: it fires while waiting for a free
        connection, so nothing was ever sent."""
        assert not h.is_ambiguous_delivery(httpx.PoolTimeout("no slot"))

    def test_an_unrelated_exception_is_not_ambiguous(self, h) -> None:
        assert not h.is_ambiguous_delivery(ValueError("nothing to do with it"))


class _FakeSocket:
    """Yields canned frames then blocks, like a real idle socket.

    With ``then_raise`` it ends the way a real socket ends instead — by
    raising out of ``recv`` — which is what drives the reconnect loop.
    """

    def __init__(
        self, frames: list[dict], then_raise: BaseException | None = None
    ) -> None:
        self._frames = list(frames)
        self._then_raise = then_raise
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if self._frames:
            return json.dumps(self._frames.pop(0))
        if self._then_raise is not None:
            raise self._then_raise
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



async def _as_coro(value, note=None):
    if note is not None:
        note.append(value)
    return value


class _HostileCloseSocket(_FakeSocket):
    """A socket whose close() misbehaves, which the plain fake cannot.

    `_FakeSocket` has no `close()` at all, so every test using it skipped
    the close path entirely — which is why review found this and the tests
    did not.
    """

    def __init__(self, frames, *, mode: str = "hang") -> None:
        super().__init__(frames)
        self.mode = mode

    async def close(self) -> None:
        if self.mode == "hang":
            await asyncio.sleep(3600)
        raise OSError("connection reset during close")


class TestStartPollingWaitsForTheSocket:
    """The window this closes loses replies permanently.

    Mattermost has no update cursor. Telegram's `getUpdates` carries an
    offset, so a reply that arrives before we poll is still there when we
    do; a Mattermost `posted` event delivered while nothing is subscribed
    is gone. The bridge posts a question the moment a pipeline asks, so
    `start_polling` returning before the socket is authenticated opens a
    window where the operator's answer vanishes and looks, from every
    side, like they never replied.

    The review that found this was reading the code, not running it — all
    of this class exists because nothing was covering `_listen_loop`.
    """

    @pytest.mark.asyncio
    async def test_it_returns_once_the_server_acknowledges(self) -> None:
        h = _handler(_ok, connect_timeout=2.0)
        ws = _FakeSocket([{"status": "OK", "seq_reply": 1}])
        h._connect = lambda: _as_coro(ws)  # type: ignore[method-assign]

        async def noop(text: str, reply_to: str | None) -> None:
            return None

        await h.start_polling(noop)
        assert h._poll_task is not None
        # The challenge really was sent, and it names seq 1 so the reply
        # can be recognised.
        assert ws.sent[0]["action"] == "authentication_challenge"
        assert ws.sent[0]["seq"] == 1
        await h.stop_polling()
        await h.close()

    @pytest.mark.asyncio
    async def test_it_raises_rather_than_returning_unauthenticated(self) -> None:
        """A socket that upgrades and then never answers the challenge.

        Returning here would be the original bug: the bridge binds, posts a
        question, and the reply lands on nothing.
        """
        h = _handler(_ok, connect_timeout=0.3)
        ws = _FakeSocket([])  # upgrades, then silence
        h._connect = lambda: _as_coro(ws)  # type: ignore[method-assign]

        async def noop(text: str, reply_to: str | None) -> None:
            return None

        with pytest.raises(TimeoutError):
            await h.start_polling(noop)
        assert h._poll_task is None
        await h.close()

    @pytest.mark.asyncio
    async def test_a_refused_token_surfaces_as_an_auth_error(self) -> None:
        from ctrlrelay.bridge.mattermost_handler import MattermostAuthError

        h = _handler(_ok)
        ws = _FakeSocket([{
            "status": "FAIL", "seq_reply": 1,
            "error": {"message": "token expired"},
        }])

        async def collect(text: str, reply_to: str | None) -> None:
            return None

        with pytest.raises(MattermostAuthError, match="token expired"):
            await h._consume(ws, collect)
        await h.close()

    @pytest.mark.asyncio
    async def test_a_refused_token_fails_start_polling_at_once(self) -> None:
        """The operator-facing half of the test above.

        `_consume` raising is not the same as `bridge start` failing: the
        listen loop used to catch the refusal and retry with backoff, so
        `start_polling` sat out its whole connect timeout and then raised
        a generic "not authenticated within 30s" while the real reason —
        "token expired" — had gone to the log at warning level. On the
        first connection nothing has ever worked, so there is nothing to
        keep alive by retrying; the refusal is the answer.

        connect_timeout is deliberately long: if the loop retries instead
        of raising, this fails with TimeoutError, not by taking a while.
        """
        from ctrlrelay.bridge.mattermost_handler import MattermostAuthError

        h = _handler(_ok, connect_timeout=5.0)
        ws = _FakeSocket([{
            "status": "FAIL", "seq_reply": 1,
            "error": {"message": "token expired"},
        }])
        h._connect = lambda: _as_coro(ws)  # type: ignore[method-assign]

        async def noop(text: str, reply_to: str | None) -> None:
            return None

        with pytest.raises(MattermostAuthError, match="token expired"):
            await h.start_polling(noop)
        assert h._poll_task is None
        await h.close()

    @pytest.mark.asyncio
    async def test_cancelling_the_connect_wait_leaves_no_orphaned_task(
        self,
    ) -> None:
        """Ctrl+C during the connect wait. The internal `ready.wait()`
        task must be cancelled on that path too, or the loop reports
        "Task was destroyed but it is pending" at close — on a shutdown
        that was in fact clean."""
        h = _handler(_ok, connect_timeout=5.0)
        h._connect = lambda: _as_coro(_FakeSocket([]))  # type: ignore[method-assign]

        async def noop(text: str, reply_to: str | None) -> None:
            return None

        starting = asyncio.create_task(h.start_polling(noop))
        await asyncio.sleep(0.05)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        await h.close()
        orphans = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
            and t.get_coro().__qualname__ == "Event.wait"
        ]
        assert orphans == []

    @pytest.mark.asyncio
    async def test_a_posted_event_interleaved_with_the_ack_is_not_lost(
        self,
    ) -> None:
        """The auth reply is handled inline rather than read first.

        A separate "read the ack" step would discard whatever arrived
        alongside it — and what arrives alongside it is an answer.
        """
        h = _handler(_ok)
        got: list[tuple] = []

        async def collect(text: str, reply_to: str | None) -> None:
            got.append((text, reply_to))

        ws = _FakeSocket([
            _posted(message="early answer"),
            {"status": "OK", "seq_reply": 1},
            _posted(message="later answer"),
        ])
        ready = asyncio.Event()
        task = asyncio.create_task(h._consume(ws, collect, ready.set))
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
        assert [t for t, _ in got] == ["early answer", "later answer"]
        assert ready.is_set()
        await h.close()

    @pytest.mark.asyncio
    async def test_the_bot_id_is_resolved_before_the_socket_opens(self) -> None:
        """A /users/me failure inside the read loop would discard events
        already queued on that socket, so it happens first and a failure
        there stops start_polling instead."""
        calls: list[str] = []

        def responder(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/users/me"):
                return httpx.Response(401, json={"message": "bad token"})
            return _ok(request)

        h = _handler(responder, connect_timeout=0.3)
        connected = []
        h._connect = lambda: _as_coro(_FakeSocket([]), note=connected)  # type: ignore[method-assign]

        async def noop(text: str, reply_to: str | None) -> None:
            return None

        with pytest.raises(httpx.HTTPStatusError):
            await h.start_polling(noop)
        assert any(p.endswith("/users/me") for p in calls)
        assert connected == [], "socket was opened before the bot id resolved"
        await h.close()




async def _noop(text: str, reply_to: str | None) -> None:
    return None


class TestReconnectBackoff:
    """The listen loop's retry interval, read from its own warning records.

    `reconnect_delay` is the first interval and doubles to a 30s cap. The
    records carry the delay as a formatting argument, so the assertions
    are about the number the loop will sleep for rather than about how
    long the test took — timing would be flaky and would not distinguish
    "reset" from "small".
    """

    @staticmethod
    def _delays(caplog) -> list[float]:
        return [
            r.args[-1]
            for r in caplog.records
            if r.name == "ctrlrelay.bridge.mattermost_handler"
            and "reconnecting in" in r.getMessage()
        ]

    @staticmethod
    async def _until(cond, *, tries: int = 300) -> None:
        for _ in range(tries):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not met in time")

    @pytest.mark.asyncio
    async def test_backoff_restarts_after_an_authenticated_connection_drops(
        self, caplog
    ) -> None:
        """A connection that authenticated and then dropped was healthy.

        The reset used to sit after the `_consume` call — which never
        returns, because a socket ends by raising — so it was dead code and
        every drop doubled the delay: a bridge that lost its socket five
        times in a week was then waiting the full 30s on every later drop,
        and each of those seconds is a window in which a `posted` event is
        lost for good.
        """
        h = _handler(_ok, connect_timeout=2.0, reconnect_delay=0.01)
        connects: list[_FakeSocket] = []

        async def connect() -> _FakeSocket:
            ws = _FakeSocket(
                [{"status": "OK", "seq_reply": 1}],
                then_raise=ConnectionError("server restarted"),
            )
            connects.append(ws)
            return ws

        h._connect = connect  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING, logger="ctrlrelay"):
            await h.start_polling(_noop)
            await self._until(lambda: len(connects) >= 4)
        await h.close()

        delays = self._delays(caplog)
        assert len(delays) >= 3
        assert delays == [0.01] * len(delays), delays

    @pytest.mark.asyncio
    async def test_backoff_grows_while_connections_never_authenticate(
        self, caplog
    ) -> None:
        """The mirror: the reset must not fire for a connection that only
        upgraded. A server accepting the upgrade and dropping us before
        the ack would otherwise be retried at the floor forever."""
        h = _handler(_ok, connect_timeout=2.0, reconnect_delay=0.01)
        sockets = [
            _FakeSocket(
                [{"status": "OK", "seq_reply": 1}],
                then_raise=ConnectionError("dropped"),
            ),
        ]
        connects: list[_FakeSocket] = []

        async def connect() -> _FakeSocket:
            ws = sockets.pop(0) if sockets else _FakeSocket(
                [], then_raise=ConnectionError("dropped before ack")
            )
            connects.append(ws)
            return ws

        h._connect = connect  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING, logger="ctrlrelay"):
            await h.start_polling(_noop)
            await self._until(lambda: len(connects) >= 4)
        await h.close()

        delays = self._delays(caplog)
        # First drop followed an authenticated connection: floor. The next
        # two did not: doubling.
        assert delays[:3] == [0.01, 0.02, 0.04], delays

    @pytest.mark.asyncio
    async def test_a_refusal_after_startup_is_retried_not_fatal(
        self, caplog
    ) -> None:
        """A token rotated under a running bridge. The first connection
        refusing is fatal (see start_polling); a later one is not, because
        taking the bridge down loses every question in flight and the
        operator is not at a terminal to see why."""
        h = _handler(_ok, connect_timeout=2.0, reconnect_delay=0.01)
        sockets = [
            _FakeSocket(
                [{"status": "OK", "seq_reply": 1}],
                then_raise=ConnectionError("dropped"),
            ),
            _FakeSocket([{
                "status": "FAIL", "seq_reply": 1,
                "error": {"message": "token revoked"},
            }]),
            _FakeSocket([{"status": "OK", "seq_reply": 1}]),
        ]
        connects: list[_FakeSocket] = []

        async def connect() -> _FakeSocket:
            ws = sockets.pop(0) if sockets else _FakeSocket([])
            connects.append(ws)
            return ws

        h._connect = connect  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING, logger="ctrlrelay"):
            await h.start_polling(_noop)
            await self._until(lambda: len(connects) >= 3)
            await asyncio.sleep(0.05)
        assert h._poll_task is not None and not h._poll_task.done()
        await h.close()
        assert any(
            "token revoked" in r.getMessage() for r in caplog.records
        ), "the refusal must be in the log, not swallowed"


class TestPreflight:
    """What `start_polling` cannot prove: that the channel exists and the
    bot is in it. A bot outside the channel gets a 403 on its first post
    and — the part that matters — no reply events, so a question that
    somehow posted would never be answered."""

    @staticmethod
    def _server(member: bool, channel_exists: bool = True):
        calls: list[str] = []

        def responder(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            calls.append(path)
            if path.endswith(f"/channels/{CHANNEL}/members/{BOT_ID}"):
                if member:
                    return httpx.Response(200, json={"user_id": BOT_ID})
                return httpx.Response(404, json={"message": "no such member"})
            if path.endswith(f"/channels/{CHANNEL}"):
                if channel_exists:
                    return httpx.Response(200, json={"id": CHANNEL})
                return httpx.Response(404, json={"message": "no such channel"})
            return _ok(request)

        return responder, calls

    @pytest.mark.asyncio
    async def test_a_bot_outside_the_channel_is_refused_with_the_fix(
        self,
    ) -> None:
        from ctrlrelay.bridge.mattermost_handler import MattermostAuthError

        responder, _calls = self._server(member=False)
        h = _handler(responder)
        with pytest.raises(MattermostAuthError, match="/invite"):
            await h.preflight()
        await h.close()

    @pytest.mark.asyncio
    async def test_a_missing_channel_is_refused(self) -> None:
        responder, _calls = self._server(member=False, channel_exists=False)
        h = _handler(responder)
        with pytest.raises(httpx.HTTPStatusError):
            await h.preflight()
        await h.close()

    @pytest.mark.asyncio
    async def test_a_member_bot_passes_and_asks_the_three_questions(
        self,
    ) -> None:
        responder, calls = self._server(member=True)
        h = _handler(responder)
        await h.preflight()
        await h.close()
        assert any(p.endswith("/users/me") for p in calls)
        assert any(p.endswith(f"/channels/{CHANNEL}") for p in calls)
        assert any(p.endswith(f"/members/{BOT_ID}") for p in calls)

    @pytest.mark.asyncio
    async def test_the_bridge_server_runs_it_before_binding(
        self, tmp_path
    ) -> None:
        """The method existed and nothing called it: `verify_handler` was
        defined in the factory and reached from nowhere, and the config
        carried a `preflight: true` that nothing read. A bot outside its
        channel started cleanly and failed on the first ASK, hours later —
        the exact outcome the docstring said the check prevented."""
        from ctrlrelay.bridge import BridgeServer, HandlerConfigError

        responder, _calls = self._server(member=False)
        h = _handler(responder, connect_timeout=0.5)
        # Keep it off the network if the preflight is skipped: the socket
        # then upgrades and never acks, and start_polling times out.
        h._connect = lambda: _as_coro(_FakeSocket([]))  # type: ignore[method-assign]
        socket_path = tmp_path / "b.sock"
        server = BridgeServer(socket_path=socket_path, handler=h)
        with pytest.raises(HandlerConfigError, match="not a member"):
            await asyncio.wait_for(server.start(), timeout=2)
        assert h._poll_task is None, "polling must not start on a failed preflight"
        assert not socket_path.exists(), "the socket must not bind either"
        await server.stop()

    @pytest.mark.asyncio
    async def test_a_refusal_is_not_masked_by_a_hanging_close(self) -> None:
        """The refusal must reach the caller, not the close timeout.

        A server that refuses the token and then ignores the close
        handshake would otherwise hold the `finally` until start_polling's
        own timeout expired, and the operator would be told
        "not authenticated within 30s" while "token expired" sat in the
        log. The close is bounded for exactly this.
        """
        from ctrlrelay.bridge.mattermost_handler import MattermostAuthError

        h = _handler(_ok, connect_timeout=10.0)
        ws = _HostileCloseSocket([{
            "status": "FAIL", "seq_reply": 1,
            "error": {"message": "token expired"},
        }], mode="hang")
        h._connect = lambda: _as_coro(ws)  # type: ignore[method-assign]

        async def noop(text: str, reply_to: str | None) -> None:
            return None

        with pytest.raises(MattermostAuthError, match="token expired"):
            await asyncio.wait_for(h.start_polling(noop), timeout=6)
        await h.close()

    @pytest.mark.asyncio
    async def test_a_refusal_is_not_replaced_by_a_raising_close(self) -> None:
        """A close() that raises must not become the reported reason."""
        from ctrlrelay.bridge.mattermost_handler import MattermostAuthError

        h = _handler(_ok, connect_timeout=5.0)
        ws = _HostileCloseSocket([{
            "status": "FAIL", "seq_reply": 1,
            "error": {"message": "token expired"},
        }], mode="raise")
        h._connect = lambda: _as_coro(ws)  # type: ignore[method-assign]

        async def noop(text: str, reply_to: str | None) -> None:
            return None

        with pytest.raises(MattermostAuthError, match="token expired"):
            await asyncio.wait_for(h.start_polling(noop), timeout=6)
        await h.close()
