"""Tests for secops pipeline."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSecopsPipeline:
    def test_secops_pipeline_has_name(self) -> None:
        """SecopsPipeline should have name attribute."""
        from dev_sync.pipelines.secops import SecopsPipeline

        pipeline = SecopsPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=MagicMock(),
            state_db=MagicMock(),
            transport=MagicMock(),
        )

        assert pipeline.name == "secops"

    @pytest.mark.asyncio
    async def test_run_dispatches_claude_session(self, tmp_path: Path) -> None:
        """Should dispatch Claude with secops prompt."""
        from dev_sync.core.checkpoint import CheckpointStatus
        from dev_sync.core.dispatcher import SessionResult
        from dev_sync.pipelines.base import PipelineContext
        from dev_sync.pipelines.secops import SecopsPipeline

        mock_dispatcher = AsyncMock()
        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.DONE
        mock_state.summary = "Merged 2 PRs"
        mock_state.outputs = {"merged_prs": [1, 2]}

        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess-123",
            exit_code=0,
            state=mock_state,
        )

        pipeline = SecopsPipeline(
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        ctx = PipelineContext(
            session_id="sess-123",
            repo="owner/repo",
            worktree_path=tmp_path,
            context_path=tmp_path / "CLAUDE.md",
            state_file=tmp_path / "state.json",
        )

        result = await pipeline.run(ctx)

        assert result.success
        assert result.summary == "Merged 2 PRs"
        mock_dispatcher.spawn_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_returns_blocked_when_needs_input(self, tmp_path: Path) -> None:
        """Should return blocked result when Claude needs input."""
        from dev_sync.core.checkpoint import CheckpointStatus
        from dev_sync.core.dispatcher import SessionResult
        from dev_sync.pipelines.base import PipelineContext
        from dev_sync.pipelines.secops import SecopsPipeline

        mock_dispatcher = AsyncMock()
        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.BLOCKED_NEEDS_INPUT
        mock_state.question = "Should I merge major version bump?"
        mock_state.summary = None
        mock_state.outputs = {}
        mock_state.error = None

        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess-123",
            exit_code=0,
            state=mock_state,
        )

        pipeline = SecopsPipeline(
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        ctx = PipelineContext(
            session_id="sess-123",
            repo="owner/repo",
            worktree_path=tmp_path,
            context_path=tmp_path / "CLAUDE.md",
            state_file=tmp_path / "state.json",
        )

        result = await pipeline.run(ctx)

        assert not result.success
        assert result.blocked
        assert "major version" in result.question
