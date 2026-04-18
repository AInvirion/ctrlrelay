"""Tests for secops pipeline."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestSecopsPipeline:
    def test_secops_pipeline_has_name(self) -> None:
        """SecopsPipeline should have name attribute."""
        from ctrlrelay.pipelines.secops import SecopsPipeline

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
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.base import PipelineContext
        from ctrlrelay.pipelines.secops import SecopsPipeline

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
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.base import PipelineContext
        from ctrlrelay.pipelines.secops import SecopsPipeline

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

    @pytest.mark.asyncio
    async def test_run_all_processes_multiple_repos(self, tmp_path: Path) -> None:
        """Should run secops on all configured repos."""
        from ctrlrelay.core.checkpoint import CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.secops import run_secops_all

        mock_dispatcher = AsyncMock()
        mock_state = MagicMock()
        mock_state.status = CheckpointStatus.DONE
        mock_state.summary = "Done"
        mock_state.outputs = {}
        mock_state.error = None

        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="sess-123",
            exit_code=0,
            state=mock_state,
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        mock_db.release_lock.return_value = True

        repos = [
            MagicMock(name="owner/repo1", local_path=tmp_path / "repo1"),
            MagicMock(name="owner/repo2", local_path=tmp_path / "repo2"),
        ]

        results = await run_secops_all(
            repos=repos,
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=mock_worktree,
            dashboard=None,
            state_db=mock_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )

        assert len(results) == 2
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_run_all_skips_locked_repos(self, tmp_path: Path) -> None:
        """Should skip repos that are already locked."""
        from ctrlrelay.pipelines.secops import run_secops_all

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = False

        repos = [MagicMock(name="owner/locked-repo")]

        results = await run_secops_all(
            repos=repos,
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=mock_db,
            transport=None,
            contexts_dir=tmp_path,
        )

        assert len(results) == 1
        assert not results[0].success
        assert "locked" in results[0].error.lower()
