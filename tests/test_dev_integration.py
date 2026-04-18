"""Integration test for dev pipeline."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDevIntegration:
    @pytest.mark.asyncio
    async def test_full_dev_flow_with_mocked_claude(self, tmp_path: Path) -> None:
        """Should run full dev flow from issue to PR."""
        from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus, read_checkpoint
        from dev_sync.core.dispatcher import ClaudeDispatcher, SessionResult
        from dev_sync.core.github import GitHubCLI
        from dev_sync.core.state import StateDB
        from dev_sync.core.worktree import WorktreeManager
        from dev_sync.pipelines.dev import run_dev_issue

        # Setup state DB
        state_db = StateDB(tmp_path / "state.db")

        # Setup mock worktree
        worktree = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        # Create fake bare repo
        bare_path = tmp_path / "repos" / "owner-repo.git"
        bare_path.mkdir(parents=True)
        (bare_path / "HEAD").write_text("ref: refs/heads/main\n")

        # Mock GitHub
        mock_github = AsyncMock(spec=GitHubCLI)
        mock_github.get_issue.return_value = {
            "number": 123,
            "title": "Fix the login bug",
            "body": "Users cannot log in when...",
        }
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "status": "completed", "conclusion": "success"},
        ]
        mock_github.get_pr_state.return_value = {
            "number": 42,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }

        # Mock dispatcher
        mock_dispatcher = AsyncMock(spec=ClaudeDispatcher)

        async def mock_spawn_session(**kwargs):
            state_file = kwargs["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                "version": "1",
                "status": "DONE",
                "session_id": kwargs["session_id"],
                "timestamp": "2026-04-17T12:00:00Z",
                "summary": "PR #42 opened for issue #123",
                "outputs": {
                    "pr_url": "https://github.com/owner/repo/pull/42",
                    "pr_number": 42,
                },
            }))
            return SessionResult(
                session_id=kwargs["session_id"],
                exit_code=0,
                stdout="",
                stderr="",
                state=read_checkpoint(state_file),
            )

        mock_dispatcher.spawn_session.side_effect = mock_spawn_session

        # Mock worktree methods
        with patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock), \
             patch.object(worktree, "create_worktree_with_new_branch", new_callable=AsyncMock) as mock_create, \
             patch.object(worktree, "remove_worktree", new_callable=AsyncMock), \
             patch.object(worktree, "symlink_context"), \
             patch.object(worktree, "remove_context_symlink"):

            worktree_path = tmp_path / "worktrees" / "test-worktree"
            worktree_path.mkdir(parents=True)
            mock_create.return_value = worktree_path

            result = await run_dev_issue(
                repo="owner/repo",
                issue_number=123,
                branch_template="fix/issue-{n}",
                dispatcher=mock_dispatcher,
                github=mock_github,
                worktree=worktree,
                dashboard=None,
                state_db=state_db,
                transport=None,
                contexts_dir=tmp_path / "contexts",
            )

        # Verify result
        assert result.success
        assert result.outputs["pr_number"] == 42

        # Verify branch was created with correct name
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["new_branch"] == "fix/issue-123"

        # Verify session was recorded
        rows = state_db.execute("SELECT * FROM sessions").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "done"
        assert rows[0]["issue_number"] == 123

        state_db.close()
