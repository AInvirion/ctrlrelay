"""File-based mock transport for testing."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from dev_sync.core.obs import get_logger, hash_text, log_event
from dev_sync.transports.base import TransportError

_logger = get_logger("transport.file_mock")


class FileMockTransport:
    """Transport that reads/writes to files for testing."""

    def __init__(self, inbox: Path, outbox: Path) -> None:
        self.inbox = inbox
        self.outbox = outbox

    async def send(
        self,
        message: str,
        *,
        session_id: str | None = None,
        repo: str | None = None,
        issue_number: int | None = None,
    ) -> None:
        """Write message to outbox file."""
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.outbox.open("a") as f:
            f.write(f"[{timestamp}] {message}\n")

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
        """Write question to outbox and poll inbox for answer."""
        timestamp = datetime.now(timezone.utc).isoformat()
        opts = f" [{'/'.join(options)}]" if options else ""
        with self.outbox.open("a") as f:
            f.write(f"[{timestamp}] QUESTION: {question}{opts}\n")

        log_event(
            _logger,
            "dev.question.posted",
            session_id=session_id,
            repo=repo,
            issue_number=issue_number,
            transport="file_mock",
            destination=str(self.outbox),
            question=question,
            question_length=len(question),
            question_hash=hash_text(question),
            options=options,
        )

        sent_at = time.monotonic()
        start = asyncio.get_event_loop().time()
        while True:
            content = self.inbox.read_text().strip()
            if content:
                self.inbox.write_text("")
                answer = content.split("\n")[0].strip()
                log_event(
                    _logger,
                    "dev.answer.received",
                    session_id=session_id,
                    repo=repo,
                    issue_number=issue_number,
                    transport="file_mock",
                    answer=answer,
                    answer_length=len(answer),
                    answer_hash=hash_text(answer),
                    elapsed_ms=int((time.monotonic() - sent_at) * 1000),
                )
                return answer

            if asyncio.get_event_loop().time() - start > timeout:
                raise TransportError(f"ask() timeout after {timeout}s")

            await asyncio.sleep(0.5)

    async def close(self) -> None:
        """No-op for file transport."""
        pass
