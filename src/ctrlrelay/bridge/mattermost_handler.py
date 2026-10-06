"""Mattermost chat handler — REST out, WebSocket in.

Outbound is ``POST /api/v4/posts`` with a bot-account token. Inbound is a
persistent WebSocket on ``/api/v4/websocket``, authenticated with the same
token, filtered to one channel.

Webhooks were considered and rejected. An incoming webhook only posts; an
outgoing one would need this process to run an HTTP server the Mattermost
server can reach, and it carries no reference to the post being replied
to — so a threaded answer could not be matched to the question it answers,
which is the entire job here.

Measured against a real server (Mattermost 11.7.11, Entry edition) before
this was written, because two of these behaviours decide the design:

- a threaded reply's ``root_id`` **equals the question's ``post_id``**, so
  it is the exact counterpart of Telegram's ``reply_to_message_id``;
- **the bot's own posts arrive back on the bot's own socket.** Without the
  ``user_id`` filter below, the handler reads the question it just asked as
  the answer to that question, instantly and every time.

**Why ``start_polling`` waits for the socket.** Mattermost has no update
cursor. Telegram's ``getUpdates`` carries an offset, so a reply that
arrives before we poll is still there when we do; a Mattermost ``posted``
event delivered while nothing is subscribed is **gone for good**. The
bridge posts a question as soon as a pipeline asks, so returning from
``start_polling`` before the socket is authenticated opens a window where
the operator's answer is lost permanently and looks, from every side, like
they never replied.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable

import httpx
import websockets

from ctrlrelay.bridge.handler import IncomingMessageHandler

_log = logging.getLogger(__name__)

# The websockets library logs every frame it sends at DEBUG — and the first
# frame we send is the authentication challenge, whose payload is the bot
# token. Turning on DEBUG to debug an authentication problem would print the
# credential, which is exactly when somebody would do it.
#
# Pinning this logger at INFO means the DEBUG record is never created, so it
# cannot be re-enabled by configuring the root logger. That is the point: a
# redacting formatter still depends on whoever configures logging, and this
# must not.
_ws_log = logging.getLogger(f"{__name__}.ws")
_ws_log.setLevel(logging.INFO)

# Mattermost has no reply-keyboard equivalent that works without an inbound
# HTTP callback, so options are rendered as a numbered list the operator
# types an answer to. Dropping them silently would lose information the
# pipeline deliberately offered.
_OPTIONS_PREAMBLE = "Reply in this thread with one of:"

# How long to wait for the first authenticated connection before giving up
# and telling the operator. Generous: a cold server behind a proxy can be
# slow. Bounded: an unbounded wait here hangs `ctrlrelay bridge start` with
# no output, which is indistinguishable from a working daemon.
_CONNECT_TIMEOUT = 30.0

# A mention is neutralised by wrapping it in backticks, because Mattermost
# does not notify on a mention inside code formatting.
#
# The lookbehind excludes a preceding word character or dot so an email
# address is left alone — `ops@example.com` must not become
# ``ops`@example`.com``. Deliberately not a list of the broadcast mentions:
# `@all`, `@here` and `@channel` are the ones that wake a whole team, but an
# enumeration is a guess about what Mattermost will add next, and defusing
# every mention costs nothing we want.
_MENTION = re.compile(r"(?<![\w.])@([\w.\-]+)")


class MattermostAuthError(Exception):
    """The server refused our token on the WebSocket."""


def defuse_mentions(text: str) -> str:
    """Stop agent-written text from paging the whole team.

    Telegram sends without ``parse_mode``, so an agent question containing
    ``@channel`` is literal text there. Mattermost always renders Markdown
    and always resolves mentions, so the same string notifies everyone in
    the channel — a difference the seam would otherwise hide, with the
    blast arriving on somebody else's phone at 3am.

    The question text comes from an agent, which means it is effectively
    untrusted for this purpose: nothing upstream is checking it for
    mentions, and a repository name or a quoted diff can contain one by
    accident.
    """
    return _MENTION.sub(r"`@\1`", text)


class MattermostHandler:
    """Posts to one Mattermost channel and streams replies from it."""

    def __init__(
        self,
        url: str,
        bot_token: str,
        channel_id: str,
        *,
        request_timeout: float = 20.0,
        connect_timeout: float = _CONNECT_TIMEOUT,
        reconnect_delay: float = 1.0,
    ) -> None:
        self.base_url = url.rstrip("/")
        self.channel_id = channel_id
        self._token = bot_token
        self._connect_timeout = connect_timeout
        # First retry interval after the socket drops; doubles up to 30s
        # until a connection authenticates, then starts over from here.
        self._reconnect_delay = reconnect_delay
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v4",
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=request_timeout,
        )
        self._poll_task: asyncio.Task | None = None
        # Resolved before the socket opens, not inside the read loop: a
        # transient /users/me failure mid-loop would otherwise discard
        # events already queued on that socket.
        self._bot_user_id: str | None = None

    @property
    def transport_name(self) -> str:
        return "mattermost"

    @property
    def destination(self) -> str:
        # Host and channel only. The token is in the client's headers and
        # must never reach a log line.
        return f"mattermost:{httpx.URL(self.base_url).host}/channel={self.channel_id}"

    async def _me(self) -> str:
        if self._bot_user_id is None:
            r = await self._client.get("/users/me")
            r.raise_for_status()
            self._bot_user_id = str(r.json()["id"])
        return self._bot_user_id

    async def preflight(self) -> None:
        """Prove the token works and the bot can see its channel.

        ``BridgeServer.start`` runs this (via ``verify_handler``) before it
        opens the event socket or binds its own. Without it the bridge
        binds, reports success, and the first blocked session discovers a
        403 hours later — and a bot that is not a channel member receives
        no reply events either, so answers would never arrive even if
        posting somehow worked. ``start_polling`` alone does not catch
        this: it proves the token, not the channel.
        """
        await self._me()
        r = await self._client.get(f"/channels/{self.channel_id}")
        r.raise_for_status()
        members = await self._client.get(
            f"/channels/{self.channel_id}/members/{self._bot_user_id}"
        )
        if members.status_code == 404:
            raise MattermostAuthError(
                f"bot is not a member of channel {self.channel_id}. It "
                "cannot post there, and it will not receive reply events "
                "for it either — add it with /invite"
            )
        members.raise_for_status()

    async def _post(self, message: str, *, root_id: str | None = None) -> str:
        payload: dict[str, Any] = {
            "channel_id": self.channel_id,
            "message": defuse_mentions(message),
        }
        if root_id:
            payload["root_id"] = root_id
        r = await self._client.post("/posts", json=payload)
        r.raise_for_status()
        return str(r.json()["id"])

    async def send(self, text: str) -> str:
        return await self._post(text)

    async def ask(
        self, question: str, options: list[str] | None = None
    ) -> str:
        text = question
        if options:
            listed = "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, 1))
            text = f"{question}\n\n{_OPTIONS_PREAMBLE}\n{listed}"
        return await self._post(text)

    async def start_polling(self, handler: IncomingMessageHandler) -> None:
        """Open the event socket and **wait until it is authenticated**.

        Idempotent — a second call is a no-op.

        Raises rather than returning early. A caller that believes polling
        is live when it is not will post a question into a channel nobody
        is listening to, and Mattermost will not hand that reply over
        later. Failing `bridge start` is the loud version of the same
        fault.
        """
        if self._poll_task is not None and not self._poll_task.done():
            return

        # Before the socket, so a /users/me hiccup cannot cost us events.
        await self._me()

        ready = asyncio.Event()
        self._poll_task = asyncio.create_task(self._listen_loop(handler, ready))
        waiter = asyncio.create_task(ready.wait())
        try:
            done, _pending = await asyncio.wait(
                {waiter, self._poll_task},
                timeout=self._connect_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # Also on the way out through a cancellation (Ctrl+C during
            # the connect wait): an orphaned waiter is reported as
            # "Task was destroyed but it is pending" at loop close, which
            # reads like a bug in the shutdown that was in fact clean.
            waiter.cancel()
        if waiter in done:
            return
        # The loop task finishing first means it gave up — on the first
        # connection a refused token is fatal, see _listen_loop — so
        # surface its exception rather than a generic timeout.
        if self._poll_task in done:
            exc = self._poll_task.exception()
            await self.stop_polling()
            raise exc or MattermostAuthError(
                "mattermost websocket loop exited before authenticating"
            )
        await self.stop_polling()
        raise TimeoutError(
            f"mattermost websocket not authenticated within "
            f"{self._connect_timeout:.0f}s"
        )

    async def stop_polling(self) -> None:
        if self._poll_task is None:
            return
        self._poll_task.cancel()
        try:
            await self._poll_task
        except (asyncio.CancelledError, Exception):
            pass
        self._poll_task = None

    @property
    def _ws_url(self) -> str:
        u = httpx.URL(self.base_url)
        scheme = "wss" if u.scheme == "https" else "ws"
        return f"{scheme}://{u.netloc.decode()}/api/v4/websocket"

    async def _connect(self) -> Any:
        return await websockets.connect(
            self._ws_url, max_size=None, logger=_ws_log
        )

    async def _listen_loop(
        self, handler: IncomingMessageHandler, ready: asyncio.Event
    ) -> None:
        """Hold the socket open, reconnecting with backoff.

        Backoff resets only once **this** connection has authenticated,
        not when the HTTP upgrade succeeds and not because some earlier
        connection did. A server that accepts the upgrade and then refuses
        the token would otherwise reset the delay on every attempt, so the
        retry interval would sit at one second forever instead of backing
        off — a hot loop against a server that is telling us no. And a
        connection that authenticated and then held for a day is a healthy
        one: its drop must be retried promptly, not at whatever delay the
        last run of failures left behind.

        ``_consume`` never returns — the socket ends by raising — so the
        reset cannot live after the call. It is driven by the callback,
        which is the only thing that knows the ack arrived.

        A refused token on the **first** connection is fatal: nothing has
        ever worked, so there is nothing to keep alive, and the operator is
        at the terminal that started the bridge. After that it is retried
        with backoff — a token being rotated under a running bridge should
        not take the bridge down with it.
        """
        backoff = self._reconnect_delay
        while True:
            authenticated = False

            def _on_authenticated() -> None:
                nonlocal authenticated
                authenticated = True
                ready.set()

            failure = "closed"
            try:
                ws = await self._connect()
                try:
                    await ws.send(json.dumps({
                        "seq": 1,
                        "action": "authentication_challenge",
                        "data": {"token": self._token},
                    }))
                    await self._consume(ws, handler, _on_authenticated)
                finally:
                    close = getattr(ws, "close", None)
                    if close is not None:
                        await close()
            except asyncio.CancelledError:
                raise
            except MattermostAuthError as e:
                if not ready.is_set():
                    raise
                failure = f"authentication refused ({e})"
            except Exception as e:
                failure = f"failed ({e})"
            if authenticated:
                backoff = self._reconnect_delay
            # Warning level every time rather than once: a bridge that is
            # silently failing to reconnect is the same as one that is up.
            _log.warning(
                "mattermost websocket %s, reconnecting in %.0fs",
                failure, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _consume(
        self,
        ws: Any,
        handler: IncomingMessageHandler,
        on_authenticated: Callable[[], None] | None = None,
    ) -> None:
        """Read frames until the socket ends.

        Handles the authentication reply inline rather than reading it
        first, because ``posted`` events can be interleaved with it and a
        separate "read the ack" step would drop whatever arrived alongside.
        """
        bot_id = await self._me()
        while True:
            raw = await ws.recv()
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue

            # Reply to our authentication_challenge (seq 1).
            if event.get("seq_reply") == 1:
                status = event.get("status")
                if status == "OK":
                    if on_authenticated is not None:
                        on_authenticated()
                    continue
                if status == "FAIL":
                    raise MattermostAuthError(
                        str(event.get("error", {}).get("message", "refused"))
                    )
                continue

            if event.get("event") != "posted":
                continue
            try:
                post = json.loads(event["data"]["post"])
            except (KeyError, TypeError, ValueError):
                continue
            if post.get("channel_id") != self.channel_id:
                continue
            if str(post.get("user_id")) == bot_id:
                # Our own question coming back. See the module docstring:
                # this is measured behaviour, not a defensive guess.
                continue
            text = (post.get("message") or "").strip()
            if not text:
                continue
            # root_id is "" on a top-level post. Normalise to None so the
            # bridge's "fresh message" branch means the same thing on both
            # transports.
            root_id = post.get("root_id") or None
            try:
                await handler(text, root_id)
            except Exception as e:
                _log.warning("bridge answer handler raised: %s", e)

    async def close(self) -> None:
        await self.stop_polling()
        await self._client.aclose()

    def is_ambiguous_delivery(self, exc: BaseException) -> bool:
        """Whether a failed post may nonetheless have landed.

        The question is *could the server have acted on it*, which is finer
        than "was it a timeout" and finer than "did we get a response":

        - ``ConnectError`` / ``ConnectTimeout`` / ``PoolTimeout`` — no
          request was ever put on a connection. **Definite** failure.
          ``PoolTimeout`` is the surprising member: it is a
          ``TransportError`` like a read timeout, but it fires while
          waiting for a free connection, before anything is sent.
        - every other ``TransportError``, including ``ReadTimeout`` and
          ``RemoteProtocolError`` — the request went out and the reply did
          not come back. Mattermost may well have created the post.
          **Unknown.**
        - ``HTTPStatusError`` **4xx** — the server understood and refused.
          **Definite.**
        - ``HTTPStatusError`` **5xx** — **Unknown**, and this is the one
          that reads wrong. A reverse proxy can forward the post, let
          Mattermost commit it, then lose the upstream response and return
          502 or 504. The post exists; only our view of it failed. Calling
          that a definite failure is how a question already on the
          operator's screen gets reported as never sent.
        """
        if isinstance(
            exc,
            (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
        ):
            return False
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code >= 500
        return isinstance(exc, httpx.TransportError)
