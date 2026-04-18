"""Checkpoint protocol for skill-orchestrator communication."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
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


class CheckpointError(Exception):
    """Raised when checkpoint operations fail."""


def _get_state_file() -> Path:
    """Get state file path from environment."""
    path = os.environ.get("CTRLRELAY_STATE_FILE")
    if not path:
        raise CheckpointError("CTRLRELAY_STATE_FILE environment variable not set")
    return Path(path)


def _get_session_id() -> str:
    """Get session ID from environment."""
    session_id = os.environ.get("CTRLRELAY_SESSION_ID")
    if not session_id:
        raise CheckpointError("CTRLRELAY_SESSION_ID environment variable not set")
    return session_id


def _write_checkpoint(state: CheckpointState) -> None:
    """Write checkpoint state atomically."""
    state_file = _get_state_file()
    temp_file = state_file.with_suffix(".json.tmp")

    state_file.parent.mkdir(parents=True, exist_ok=True)

    # Write to temp file first
    temp_file.write_text(state.model_dump_json(indent=2))

    # Atomic rename
    temp_file.rename(state_file)


def done(summary: str, outputs: dict[str, Any] | None = None) -> None:
    """Report successful completion."""
    # Check state file first so error message is consistent
    _get_state_file()
    state = CheckpointState(
        status=CheckpointStatus.DONE,
        session_id=_get_session_id(),
        summary=summary,
        outputs=outputs or {},
    )
    _write_checkpoint(state)


def blocked(question: str, context: dict[str, Any] | None = None) -> None:
    """Report blocked on human input."""
    # Check state file first so error message is consistent
    _get_state_file()
    state = CheckpointState(
        status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
        session_id=_get_session_id(),
        question=question,
        question_context=context,
    )
    _write_checkpoint(state)


def failed(error: str, recoverable: bool = True) -> None:
    """Report failure."""
    # Check state file first so error message is consistent
    _get_state_file()
    state = CheckpointState(
        status=CheckpointStatus.FAILED,
        session_id=_get_session_id(),
        error=error,
        recoverable=recoverable,
    )
    _write_checkpoint(state)


def read_checkpoint(path: Path, delete_after: bool = False) -> CheckpointState:
    """Read and parse a checkpoint file.

    Used by the orchestrator to read skill results.

    Args:
        path: Path to the checkpoint file.
        delete_after: If True, delete the file after reading.

    Returns:
        Parsed CheckpointState.

    Raises:
        CheckpointError: If file not found or invalid.
    """
    if not path.exists():
        raise CheckpointError(f"Checkpoint file not found: {path}")

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise CheckpointError(f"Failed to parse checkpoint file: {e}") from e

    try:
        state = CheckpointState.model_validate(data)
    except Exception as e:
        raise CheckpointError(f"Invalid checkpoint data: {e}") from e

    if delete_after:
        path.unlink()

    return state
