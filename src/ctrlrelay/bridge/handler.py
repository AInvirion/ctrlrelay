"""The seam between the bridge and whichever chat app it talks to.

``BridgeServer`` holds one of these and knows nothing else about the chat
side. Everything a handler has to answer is here, including the two things
that look like details and are not: how its library signals an *ambiguous*
delivery, and what its posted-message identifier is.

**Post ids are strings at this boundary, always.** Telegram's are integers
and Mattermost's are 26-character strings, and the bridge keys a
``post_id -> session_id`` map on them to route a late reply to the right
paused session. A handler that leaked its native type would make that map
hold two kinds of key, and the first collision would attach an operator's
answer to the wrong pipeline. Stringifying at the edge costs nothing and
makes the map's type honest.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol, runtime_checkable

# (message text, id of the post being replied to or None for a fresh message)
IncomingMessageHandler = Callable[[str, "str | None"], Awaitable[None]]


@runtime_checkable
class ChatHandler(Protocol):
    """Outbound posting plus an inbound stream of operator replies."""

    @property
    def transport_name(self) -> str:
        """Short label for logs and error codes, e.g. ``"telegram"``.

        It reaches structured log records and the ``error`` field of a
        protocol ERROR frame, so a reader can tell which chat app refused
        without inferring it from the configuration.
        """
        ...

    @property
    def destination(self) -> str:
        """Where posts go, for the log only, e.g. ``"telegram:chat=123"``.

        Never a secret: these records land in files with no retention
        policy, so a token or a full URL with credentials must not appear.
        """
        ...

    async def send(self, text: str) -> str:
        """Post a message. Returns its post id."""
        ...

    async def ask(
        self, question: str, options: list[str] | None = None
    ) -> str:
        """Post a question. Returns its post id.

        ``options`` is a hint, not a contract. A handler that cannot offer
        tappable choices must still deliver the question — rendering the
        options as text is a correct implementation, dropping them is not.
        """
        ...

    async def start_polling(self, handler: IncomingMessageHandler) -> None:
        """Begin delivering incoming operator messages to ``handler``.

        Must be idempotent, and must **not** deliver the bot's own posts.
        Measured on Mattermost: a bot's post arrives back on the bot's own
        socket, so a handler without that filter reads the question it just
        asked as the answer to that question, immediately and every time.
        """
        ...

    async def stop_polling(self) -> None:
        """Stop the inbound stream if running."""
        ...

    async def close(self) -> None:
        """Release the client. Implies ``stop_polling``."""
        ...

    def is_ambiguous_delivery(self, exc: BaseException) -> bool:
        """True when ``exc`` leaves delivery genuinely unknown.

        Delivery is only ever *confirmed* by the far side answering. The
        rest divides in two: the post definitely did not land, or we cannot
        tell. A timeout is the common case of the second — the chat server
        may well have accepted the message after we stopped waiting, in
        which case the question is in front of the operator and calling it
        a failure puts a lie in the log.

        Each handler owns this because the taxonomy belongs to its own
        library, and both libraries we use get it counter-intuitively
        wrong in a way worth encoding next to the code that imports them.
        """
        ...
