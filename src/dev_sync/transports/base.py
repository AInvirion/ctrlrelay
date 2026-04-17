"""Transport protocol definition."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class TransportError(Exception):
    """Raised when transport operations fail."""


@runtime_checkable
class Transport(Protocol):
    """Protocol for orchestrator-to-human communication."""

    async def send(self, message: str) -> None:
        """Send a one-way message (no response expected)."""
        ...

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
        timeout: int = 300,
    ) -> str:
        """Ask a question and wait for response."""
        ...

    async def close(self) -> None:
        """Close the transport connection."""
        ...
