"""Checkpoint protocol for skill-orchestrator communication."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CheckpointStatus(str, Enum):
    """Status values for checkpoint state."""

    DONE = "DONE"
    BLOCKED_NEEDS_INPUT = "BLOCKED_NEEDS_INPUT"
    FAILED = "FAILED"


class CheckpointState(BaseModel):
    """State written by skills to communicate with orchestrator."""

    version: str = "1"
    status: CheckpointStatus
    session_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    summary: str | None = None

    question: str | None = None
    question_context: dict[str, Any] | None = None

    error: str | None = None
    recoverable: bool = True

    outputs: dict[str, Any] = Field(default_factory=dict)
