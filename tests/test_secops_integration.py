"""Integration tests for secops pipeline."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSecopsIntegration:
    @pytest.mark.asyncio
    async def test_full_secops_flow_with_mocked_claude(self, tmp_path: Path) -> None:
        """Should run complete secops flow with mocked Claude subprocess."""
        from dev_sync.core.dispatcher import ClaudeDispatcher
        from dev_sync.core.github import GitHubCLI
        from dev_sync.core.state import StateDB
        from dev_sync.core.worktree import WorktreeManager
        from dev_sync.pipelines.secops import run_secops_all

        db_path = tmp_path / "state.db"
        db = StateDB(db_path)

        worktrees_dir = tmp_path / "worktrees"
        bare_repos_dir = tmp_path / "repos"
        contexts_dir = tmp_path / "contexts"

        context_dir = contexts_dir / "owner-repo"
        context_dir.mkdir(parents=True)
        (context_dir / "CLAUDE.md").write_text("# Test context")

        dispatcher = ClaudeDispatcher(claude_binary="claude")
        github = GitHubCLI()
        worktree = WorktreeManager(
            worktrees_dir=worktrees_dir,
            bare_repos_dir=bare_repos_dir,
        )

        repo_config = MagicMock()
        repo_config.name = "owner/repo"
        repo_config.local_path = tmp_path / "repo"

        async def mock_spawn_session(**kwargs):
            state_file = kwargs["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                "version": "1",
                "status": "DONE",
                "session_id": kwargs["session_id"],
                "timestamp": "2026-04-17T12:00:00Z",
                "summary": "Merged 2 dependabot PRs",
                "outputs": {"merged_prs": [101, 102]},
            }))

            from dev_sync.core.checkpoint import read_checkpoint
            from dev_sync.core.dispatcher import SessionResult
            return SessionResult(
                session_id=kwargs["session_id"],
                exit_code=0,
                state=read_checkpoint(state_file),
            )

        async def mock_create_worktree(repo, session_id, **kwargs):
            wt_path = worktrees_dir / f"{repo.replace('/', '-')}-{session_id}"
            wt_path.mkdir(parents=True)
            (wt_path / ".git" / "info").mkdir(parents=True)
            (wt_path / ".git" / "info" / "exclude").write_text("")
            return wt_path

        with patch.object(dispatcher, "spawn_session", side_effect=mock_spawn_session), \
             patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock), \
             patch.object(worktree, "create_worktree", side_effect=mock_create_worktree), \
             patch.object(worktree, "remove_worktree", new_callable=AsyncMock):

            results = await run_secops_all(
                repos=[repo_config],
                dispatcher=dispatcher,
                github=github,
                worktree=worktree,
                dashboard=None,
                state_db=db,
                transport=None,
                contexts_dir=contexts_dir,
            )

            assert len(results) == 1
            assert results[0].success
            assert "Merged 2" in results[0].summary

            session = db.execute(
                "SELECT * FROM sessions WHERE pipeline = 'secops'"
            ).fetchone()
            assert session is not None
            assert session["status"] == "done"

        db.close()
