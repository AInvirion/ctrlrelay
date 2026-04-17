"""Tests for checkpoint protocol."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

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

    def test_blocked_without_question_raises(self) -> None:
        """BLOCKED_NEEDS_INPUT without question should raise."""
        with pytest.raises(ValueError, match="question is required"):
            CheckpointState(
                status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
                session_id="sess-123",
            )

    def test_failed_without_error_raises(self) -> None:
        """FAILED without error should raise."""
        with pytest.raises(ValueError, match="error is required"):
            CheckpointState(
                status=CheckpointStatus.FAILED,
                session_id="sess-123",
            )


class TestCheckpointHelpers:
    def test_done_writes_state_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """done() should write DONE state to state file."""
        from dev_sync.core.checkpoint import done

        state_file = tmp_path / "state.json"
        monkeypatch.setenv("DEV_SYNC_STATE_FILE", str(state_file))
        monkeypatch.setenv("DEV_SYNC_SESSION_ID", "sess-abc")

        done(summary="Completed task", outputs={"pr_url": "https://..."})

        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["status"] == "DONE"
        assert data["summary"] == "Completed task"
        assert data["outputs"]["pr_url"] == "https://..."

    def test_blocked_writes_state_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """blocked() should write BLOCKED_NEEDS_INPUT state."""
        from dev_sync.core.checkpoint import blocked

        state_file = tmp_path / "state.json"
        monkeypatch.setenv("DEV_SYNC_STATE_FILE", str(state_file))
        monkeypatch.setenv("DEV_SYNC_SESSION_ID", "sess-abc")

        blocked(
            question="Which version?",
            context={"options": ["2.4.1", "2.5.0"]},
        )

        data = json.loads(state_file.read_text())
        assert data["status"] == "BLOCKED_NEEDS_INPUT"
        assert data["question"] == "Which version?"

    def test_failed_writes_state_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """failed() should write FAILED state."""
        from dev_sync.core.checkpoint import failed

        state_file = tmp_path / "state.json"
        monkeypatch.setenv("DEV_SYNC_STATE_FILE", str(state_file))
        monkeypatch.setenv("DEV_SYNC_SESSION_ID", "sess-abc")

        failed(error="Connection timeout", recoverable=True)

        data = json.loads(state_file.read_text())
        assert data["status"] == "FAILED"
        assert data["error"] == "Connection timeout"
        assert data["recoverable"] is True

    def test_atomic_write_uses_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Checkpoint should write to .tmp then rename for atomicity."""
        from dev_sync.core.checkpoint import done

        state_file = tmp_path / "state.json"
        monkeypatch.setenv("DEV_SYNC_STATE_FILE", str(state_file))
        monkeypatch.setenv("DEV_SYNC_SESSION_ID", "sess-abc")

        done(summary="Test")

        # Temp file should not exist after completion
        assert not (tmp_path / "state.json.tmp").exists()
        # Final file should exist
        assert state_file.exists()

    def test_missing_env_raises_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should raise if DEV_SYNC_STATE_FILE not set."""
        from dev_sync.core.checkpoint import CheckpointError, done

        monkeypatch.delenv("DEV_SYNC_STATE_FILE", raising=False)
        monkeypatch.delenv("DEV_SYNC_SESSION_ID", raising=False)

        with pytest.raises(CheckpointError, match="DEV_SYNC_STATE_FILE"):
            done(summary="Test")
