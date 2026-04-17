"""Tests for dev pipeline."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestDevPipeline:
    @pytest.mark.asyncio
    async def test_dev_pipeline_has_name(self) -> None:
        """Pipeline should have name 'dev'."""
        from dev_sync.pipelines.dev import DevPipeline

        pipeline = DevPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        assert pipeline.name == "dev"

    @pytest.mark.asyncio
    async def test_run_dispatches_claude_session(self, tmp_path: Path) -> None:
        """Should dispatch Claude session with issue context."""
        from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus
        from dev_sync.core.dispatcher import SessionResult
        from dev_sync.pipelines.base import PipelineContext
        from dev_sync.pipelines.dev import DevPipeline

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="dev-123",
            exit_code=0,
            stdout="",
            stderr="",
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.DONE,
                session_id="dev-123",
                timestamp="2026-04-17T12:00:00Z",
                summary="PR opened",
                outputs={"pr_url": "https://github.com/owner/repo/pull/42", "pr_number": 42},
            ),
        )

        pipeline = DevPipeline(
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        ctx = PipelineContext(
            session_id="dev-123",
            repo="owner/repo",
            worktree_path=tmp_path,
            context_path=tmp_path / "CLAUDE.md",
            state_file=tmp_path / "state.json",
            issue_number=123,
            extra={
                "issue_title": "Fix the bug",
                "issue_body": "There is a bug",
                "branch_name": "fix/issue-123",
            },
        )

        result = await pipeline.run(ctx)

        assert result.success
        assert result.outputs["pr_number"] == 42
        mock_dispatcher.spawn_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_dev_issue_full_flow(self, tmp_path: Path) -> None:
        """Should run full dev flow for a single issue."""
        from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus
        from dev_sync.core.dispatcher import SessionResult
        from dev_sync.core.state import StateDB
        from dev_sync.pipelines.dev import run_dev_issue

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="dev-123",
            exit_code=0,
            stdout="",
            stderr="",
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.DONE,
                session_id="dev-123",
                timestamp="2026-04-17T12:00:00Z",
                summary="PR #42 opened",
                outputs={"pr_url": "https://github.com/o/r/pull/42", "pr_number": 42},
            ),
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = tmp_path / "worktree"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {
            "number": 123,
            "title": "Fix bug",
            "body": "Bug description",
        }

        state_db = StateDB(tmp_path / "state.db")

        result = await run_dev_issue(
            repo="owner/repo",
            issue_number=123,
            branch_template="fix/issue-{n}",
            dispatcher=mock_dispatcher,
            github=mock_github,
            worktree=mock_worktree,
            dashboard=None,
            state_db=state_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )

        assert result.success
        assert result.outputs["pr_number"] == 42
        mock_worktree.create_worktree_with_new_branch.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_returns_blocked_when_needs_input(self, tmp_path: Path) -> None:
        """Should return blocked result when Claude needs input."""
        from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus
        from dev_sync.core.dispatcher import SessionResult
        from dev_sync.pipelines.base import PipelineContext
        from dev_sync.pipelines.dev import DevPipeline

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="dev-123",
            exit_code=0,
            stdout="",
            stderr="",
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
                session_id="dev-123",
                timestamp="2026-04-17T12:00:00Z",
                question="Should I use async or sync for this API?",
            ),
        )

        pipeline = DevPipeline(
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        ctx = PipelineContext(
            session_id="dev-123",
            repo="owner/repo",
            worktree_path=tmp_path,
            context_path=tmp_path / "CLAUDE.md",
            state_file=tmp_path / "state.json",
            issue_number=123,
            extra={},
        )

        result = await pipeline.run(ctx)

        assert not result.success
        assert result.blocked
        assert "async or sync" in result.question
