"""Tests for secops pipeline."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestSecopsCleanupLogging:
    """Regression for codex round-4 [P3]: worktree cleanup failures must
    not be silently swallowed. Log them via the obs stream so operators
    can see leaked admin state instead of discovering it later via a
    "worktree already exists" failure on a subsequent run."""

    @pytest.mark.asyncio
    async def test_worktree_remove_failure_is_logged(
        self, tmp_path: Path
    ) -> None:
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
            session_id="sess",
            exit_code=0,
            state=mock_state,
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"
        # remove_worktree blows up — simulates a wedged `git worktree prune`.
        mock_worktree.remove_worktree.side_effect = RuntimeError(
            "worktree removal failed"
        )

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True
        mock_db.release_lock.return_value = True

        repo = MagicMock(name="owner/repo")
        repo.name = "owner/repo"

        with patch("ctrlrelay.pipelines.secops._logger") as mock_logger:
            results = await run_secops_all(
                repos=[repo],
                dispatcher=mock_dispatcher,
                github=MagicMock(),
                worktree=mock_worktree,
                dashboard=None,
                state_db=mock_db,
                transport=None,
                contexts_dir=tmp_path / "contexts",
            )

        # Cleanup failure should not break the pipeline (result is still
        # recorded) but it MUST be logged — `log_event` calls _logger.info
        # under the hood so it shows up somewhere in mock_calls.
        assert len(results) == 1
        assert "secops.cleanup.worktree_failed" in str(mock_logger.mock_calls), (
            "worktree removal failure must be logged via obs, not "
            "swallowed (codex round-4 [P3] regression)"
        )
        # The repo lock must still be released.
        mock_db.release_lock.assert_called()


class TestSecopsCancellation:
    """Regression for codex [P2]: when a scheduled secops sweep is
    cancelled mid-run (scheduler.shutdown → SIGTERM), the session row
    must not be left in 'running' and the worktree must still be
    removed. Previously only the `except Exception` path wrote the
    session row, so CancelledError bypassed cleanup entirely."""

    @pytest.mark.asyncio
    async def test_cancel_during_run_marks_session_cancelled_and_removes_worktree(
        self, tmp_path: Path
    ) -> None:
        import asyncio

        from ctrlrelay.pipelines.secops import run_secops_all

        # Pipeline blocks forever; we'll cancel from the outside.
        async def hang_forever(ctx):  # noqa: ARG001
            await asyncio.Event().wait()

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "worktree"
        mock_worktree.ensure_bare_repo.return_value = tmp_path / "bare"

        mock_db = MagicMock()
        mock_db.acquire_lock.return_value = True

        repo = MagicMock(name="owner/repo")
        repo.name = "owner/repo"

        with patch(
            "ctrlrelay.pipelines.secops.SecopsPipeline.run",
            side_effect=hang_forever,
        ):
            task = asyncio.create_task(
                run_secops_all(
                    repos=[repo],
                    dispatcher=AsyncMock(),
                    github=MagicMock(),
                    worktree=mock_worktree,
                    dashboard=None,
                    state_db=mock_db,
                    transport=None,
                    contexts_dir=tmp_path / "contexts",
                )
            )

            # Wait until the pipeline is actually running inside the try
            # block (sessions INSERT has happened), then cancel.
            for _ in range(20):
                await asyncio.sleep(0.01)
                execute_calls = [c.args[0] for c in mock_db.execute.call_args_list]
                if any("INSERT INTO sessions" in s for s in execute_calls):
                    break

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # Assert: session was updated to 'cancelled' before cleanup.
        cancel_updates = [
            c for c in mock_db.execute.call_args_list
            if c.args and "UPDATE sessions" in c.args[0]
            and len(c.args) > 1 and "cancelled" in c.args[1]
        ]
        assert cancel_updates, (
            "CancelledError path must write 'cancelled' status — "
            "codex [P2] regression"
        )

        # Assert: worktree cleanup was called in the finally block.
        assert mock_worktree.remove_worktree.called, (
            "finally block must remove the worktree on cancel"
        )

        # Assert: the per-repo lock was released.
        assert mock_db.release_lock.called
