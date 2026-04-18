"""Transport protocol definition."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class TransportError(Exception):
    """Raised when transport operations fail."""


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
        timeout: int = 300,
        *,
        session_id: str | None = None,
        repo: str | None = None,
        issue_number: int | None = None,
    ) -> str:
        """Ask a question and wait for response."""
        ...

    async def close(self) -> None:
        """Close the transport connection."""
        ...
