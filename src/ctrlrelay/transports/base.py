"""Transport protocol definition."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class TransportError(Exception):
    """Raised when transport operations fail."""


class TransportUnknownDeliveryError(TransportError):
    """Raised when a request may or may not have been delivered.

    Delivery is only ever *confirmed* by the far side's acknowledgement.
    Everything else divides in two: we know the bytes never left (a
    definite failure), or we do not (unknown). Enumerating individual
    ambiguous cases does not converge — the honest discriminator is
    whether anything was written at all.

    Subclasses TransportError so existing handlers are unaffected.
    """


class TransportTimeoutError(TransportUnknownDeliveryError):
    """Sent, but no reply arrived in time.

    A specific unknown: the request went out and the far side may still
    act on it after we stop waiting.
    """


@runtime_checkable
class Transport(Protocol):
    """Protocol for orchestrator-to-human communication.

    Implementations accept optional correlation kwargs (``session_id``,
    ``repo``, ``issue_number``) used solely for structured logging — they are
    never part of the wire payload.
    """

    async def send(
        self,
        message: str,
        *,
        session_id: str | None = None,
        repo: str | None = None,
        issue_number: int | None = None,
    ) -> None:
        """Send a one-way message (no response expected)."""
        ...

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
        timeout: int | None = None,
        *,
        session_id: str | None = None,
        repo: str | None = None,
        issue_number: int | None = None,
    ) -> str:
        """Ask a question and wait for response.

        ``timeout`` of ``None`` means "use the implementation's configured
        default" — callers that have no opinion must not pin a literal here,
        or the operator's ``ask_timeout_seconds`` setting is silently
        bypassed.
        """
        ...

    async def close(self) -> None:
        """Close the transport connection."""
        ...
