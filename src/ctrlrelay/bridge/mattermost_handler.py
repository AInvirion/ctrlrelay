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
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import websockets

from ctrlrelay.bridge.handler import IncomingMessageHandler

_log = logging.getLogger(__name__)

# Mattermost has no reply-keyboard equivalent that works without an inbound
# HTTP callback, so options are rendered as a numbered list the operator
# types an answer to. Dropping them silently would lose information the
# pipeline deliberately offered.
_OPTIONS_PREAMBLE = "Reply in this thread with one of:"


class MattermostHandler:
    """Posts to one Mattermost channel and streams replies from it."""

    def __init__(
        self,
        url: str,
        bot_token: str,
        channel_id: str,
        *,
        request_timeout: float = 20.0,
    ) -> None:
        self.base_url = url.rstrip("/")
        self.channel_id = channel_id
        self._token = bot_token
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v4",
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=request_timeout,
        )
        self._poll_task: asyncio.Task | None = None
        # Resolved lazily on first use and cached: the handler must know
        # which user it is to ignore its own posts, and a constructor
        # cannot await.
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

    async def _post(self, message: str, *, root_id: str | None = None) -> str:
        payload: dict[str, Any] = {
            "channel_id": self.channel_id,
            "message": message,
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
        """Open the event socket. Idempotent — a second call is a no-op."""
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._listen_loop(handler))

    async def stop_polling(self) -> None:
        if self._poll_task is None:
            return
        self._poll_task.cancel()
        try:
            await self._poll_task
        except asyncio.CancelledError:
            pass
        self._poll_task = None

    @property
    def _ws_url(self) -> str:
        u = httpx.URL(self.base_url)
        scheme = "wss" if u.scheme == "https" else "ws"
        return f"{scheme}://{u.netloc.decode()}/api/v4/websocket"

    async def _listen_loop(self, handler: IncomingMessageHandler) -> None:
        """Hold the socket open, reconnecting with backoff.

        Mirrors the Telegram long-poll loop: a transient failure backs off
        and retries rather than ending the stream, because the bridge
        outliving a network blip is the whole point of it being a daemon.
        """
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self._ws_url, max_size=None) as ws:
                    await ws.send(json.dumps({
                        "seq": 1,
                        "action": "authentication_challenge",
                        "data": {"token": self._token},
                    }))
                    backoff = 1.0
                    await self._consume(ws, handler)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log.warning(
                    "mattermost websocket failed (%s), reconnecting in %.0fs",
                    e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _consume(self, ws: Any, handler: IncomingMessageHandler) -> None:
        bot_id = await self._me()
        while True:
            raw = await ws.recv()
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
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

        The division is *did the server get a chance to act on it*, which
        is finer than "was it a timeout":

        - ``ConnectError`` / ``ConnectTimeout`` — no connection was ever
          established, so nothing was delivered. A **definite** failure.
        - every other ``TransportError``, including ``ReadTimeout`` and
          ``RemoteProtocolError`` — the request went out and the reply did
          not come back. Mattermost may well have created the post.
          **Unknown.**
        - ``HTTPStatusError`` — the server answered, with a refusal.
          **Definite.** This is the counter-intuitive one worth stating:
          a 500 is a *better* outcome than a read timeout here, because it
          is an answer.
        """
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return False
        if isinstance(exc, httpx.HTTPStatusError):
            return False
        return isinstance(exc, httpx.TransportError)
