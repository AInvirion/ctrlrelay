"""Tests for checkpoint protocol."""

from datetime import datetime, timezone

import pytest

from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus


class TestCheckpointState:
    def test_done_state_valid(self) -> None:
        """DONE state requires summary."""
        state = CheckpointState(
            status=CheckpointStatus.DONE,
            session_id="sess-123",
            summary="Merged 3 PRs",
        )
        assert state.status == CheckpointStatus.DONE
        assert state.summary == "Merged 3 PRs"
        assert state.version == "1"

    def test_blocked_state_requires_question(self) -> None:
        """BLOCKED_NEEDS_INPUT state requires question."""
        state = CheckpointState(
            status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
            session_id="sess-123",
            question="Which version?",
            question_context={"options": ["2.4.1", "2.5.0"]},
        )
        assert state.question == "Which version?"

    def test_failed_state_requires_error(self) -> None:
        """FAILED state requires error message."""
        state = CheckpointState(
            status=CheckpointStatus.FAILED,
            session_id="sess-123",
            error="gh CLI returned 404",
            recoverable=False,
        )
        assert state.error == "gh CLI returned 404"
        assert state.recoverable is False

    def test_timestamp_auto_generated(self) -> None:
        """Timestamp should be auto-generated if not provided."""
        state = CheckpointState(
            status=CheckpointStatus.DONE,
            session_id="sess-123",
            summary="Done",
        )
        assert state.timestamp is not None
