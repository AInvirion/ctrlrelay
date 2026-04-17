"""File-based mock transport for testing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from dev_sync.transports.base import TransportError


class FileMockTransport:
    """Transport that reads/writes to files for testing."""

    def __init__(self, inbox: Path, outbox: Path) -> None:
        self.inbox = inbox
        self.outbox = outbox

    async def send(self, message: str) -> None:
        """Write message to outbox file."""
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.outbox.open("a") as f:
            f.write(f"[{timestamp}] {message}\n")

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
        timeout: int = 300,
    ) -> str:
        """Write question to outbox and poll inbox for answer."""
        timestamp = datetime.now(timezone.utc).isoformat()
        opts = f" [{'/'.join(options)}]" if options else ""
        with self.outbox.open("a") as f:
            f.write(f"[{timestamp}] QUESTION: {question}{opts}\n")

        start = asyncio.get_event_loop().time()
        while True:
            content = self.inbox.read_text().strip()
            if content:
                self.inbox.write_text("")
                return content.split("\n")[0].strip()

            if asyncio.get_event_loop().time() - start > timeout:
                raise TransportError(f"ask() timeout after {timeout}s")

            await asyncio.sleep(0.5)

    async def close(self) -> None:
        """No-op for file transport."""
        pass
