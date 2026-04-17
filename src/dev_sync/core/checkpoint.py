"""Checkpoint protocol for skill-orchestrator communication."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


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

    @model_validator(mode="after")
    def validate_status_fields(self) -> "CheckpointState":
        """Validate that status-dependent fields are present."""
        if self.status == CheckpointStatus.BLOCKED_NEEDS_INPUT and not self.question:
            raise ValueError("question is required when status is BLOCKED_NEEDS_INPUT")
        if self.status == CheckpointStatus.FAILED and not self.error:
            raise ValueError("error is required when status is FAILED")
        return self
