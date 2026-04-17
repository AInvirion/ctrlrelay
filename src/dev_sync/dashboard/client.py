"""Dashboard client for event push and heartbeat."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel


class HeartbeatPayload(BaseModel):
    """Payload for heartbeat endpoint."""

    node_id: str
    timestamp: str = ""
    version: str = "0.1.0"
    uptime_seconds: int = 0
    platform: str = ""
    active_sessions: list[dict[str, Any]] = []
    last_github_poll: str | None = None
    last_github_poll_status: str = "ok"
    repos_configured: int = 0
    repos_active: int = 0

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


class EventPayload(BaseModel):
    """Payload for event endpoint."""

    level: str  # info, warning, error
    pipeline: str  # secops, dev
    repo: str
    message: str
    session_id: str | None = None
    timestamp: str = ""
    details: dict[str, Any] = {}

    def model_post_init(self, __context: Any) -> None:
        if not self.timestamp:
            object.__setattr__(
                self, "timestamp", datetime.now(timezone.utc).isoformat()
            )


@dataclass
class DashboardClient:
    """Client for dashboard API with offline queue."""

    url: str
    auth_token: str
    node_id: str
    queue_dir: Path | None = None
    timeout: int = 30
    max_retries: int = 3
    _queue: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.queue_dir:
            self.queue_dir = Path(self.queue_dir)
            self.queue_dir.mkdir(parents=True, exist_ok=True)
            self._load_queue()

    def _load_queue(self) -> None:
        """Load queued events from disk."""
        if not self.queue_dir:
            return
        queue_file = self.queue_dir / "event_queue.json"
        if queue_file.exists():
            try:
                self._queue = json.loads(queue_file.read_text())
            except json.JSONDecodeError:
                self._queue = []

    def _save_queue(self) -> None:
        """Save queued events to disk."""
        if not self.queue_dir:
            return
        queue_file = self.queue_dir / "event_queue.json"
        queue_file.write_text(json.dumps(self._queue))

    def _queue_event(self, event: EventPayload) -> None:
        """Add event to offline queue."""
        self._queue.append(event.model_dump())
        self._save_queue()

    async def push_event(self, event: EventPayload) -> bool:
        """Push event to dashboard, queue on failure."""
        payload = event.model_dump()
        payload["node_id"] = self.node_id

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.url}/event",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.auth_token}"},
                )
                response.raise_for_status()
                return True
        except (httpx.HTTPError, httpx.TimeoutException):
            self._queue_event(event)
            return False

    async def heartbeat(self, payload: HeartbeatPayload) -> bool:
        """Send heartbeat to dashboard."""
        data = payload.model_dump()
        data["node_id"] = self.node_id

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.url}/heartbeat",
                    json=data,
                    headers={"Authorization": f"Bearer {self.auth_token}"},
                )
                response.raise_for_status()
                return True
        except (httpx.HTTPError, httpx.TimeoutException):
            return False

    async def drain_queue(self) -> int:
        """Attempt to send queued events. Returns count of successfully sent."""
        if not self._queue:
            return 0

        sent = 0
        remaining = []

        for event_data in self._queue:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    event_data["node_id"] = self.node_id
                    response = await client.post(
                        f"{self.url}/event",
                        json=event_data,
                        headers={"Authorization": f"Bearer {self.auth_token}"},
                    )
                    response.raise_for_status()
                    sent += 1
            except (httpx.HTTPError, httpx.TimeoutException):
                remaining.append(event_data)

        self._queue = remaining
        self._save_queue()
        return sent

    @property
    def queue_size(self) -> int:
        """Number of events in offline queue."""
        return len(self._queue)
