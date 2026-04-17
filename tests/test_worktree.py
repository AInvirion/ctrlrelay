"""Tests for git worktree management."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


class TestWorktreeManager:
    @pytest.mark.asyncio
    async def test_create_worktree(self, tmp_path: Path) -> None:
        """Should create worktree with correct paths."""
        from dev_sync.core.worktree import WorktreeManager

        worktrees_dir = tmp_path / "worktrees"
        bare_repos_dir = tmp_path / "repos"

        manager = WorktreeManager(
            worktrees_dir=worktrees_dir,
            bare_repos_dir=bare_repos_dir,
        )

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""

            worktree_path = await manager.create_worktree(
                repo="owner/repo",
                session_id="sess-123",
                branch="main",
            )

            assert worktree_path.parent == worktrees_dir
            assert "repo" in str(worktree_path)
            assert "sess-123" in str(worktree_path)

    @pytest.mark.asyncio
    async def test_ensure_bare_repo_clones_if_missing(self, tmp_path: Path) -> None:
        """Should clone bare repo if it doesn't exist."""
        from dev_sync.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""

            await manager.ensure_bare_repo("owner/repo")

            # Should have called git clone --bare
            calls = [str(c) for c in mock_git.call_args_list]
            assert any("clone" in str(c) and "--bare" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_remove_worktree(self, tmp_path: Path) -> None:
        """Should remove worktree and prune."""
        from dev_sync.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        worktree_path = manager._get_worktree_path("owner/repo", "sess-123")
        worktree_path.mkdir(parents=True)

        bare_path = manager._get_bare_repo_path("owner/repo")
        bare_path.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""

            await manager.remove_worktree("owner/repo", "sess-123")

            assert not worktree_path.exists()
            calls = [str(c) for c in mock_git.call_args_list]
            assert any("worktree" in str(c) and "prune" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_create_worktree_with_new_branch(self, tmp_path: Path) -> None:
        """Should create worktree using -b flag for new branch."""
        from dev_sync.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""

            worktree_path = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="sess-456",
                new_branch="feature/my-branch",
                base_branch="main",
            )

            assert worktree_path.parent == tmp_path / "worktrees"
            assert "sess-456" in str(worktree_path)

            # Verify -b flag was used with the new branch name
            call_args = mock_git.call_args
            args = call_args[0]
            assert "-b" in args
            assert "feature/my-branch" in args
            assert "worktree" in args
            assert "add" in args

    @pytest.mark.asyncio
    async def test_create_worktree_with_new_branch_uses_default_branch(
        self, tmp_path: Path
    ) -> None:
        """Should use default branch when base_branch is None."""
        from dev_sync.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = ["refs/heads/main\n", ""]

            worktree_path = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="sess-789",
                new_branch="feature/auto-base",
            )

            assert worktree_path is not None
            # First call should be symbolic-ref HEAD, second should be worktree add
            first_call_args = mock_git.call_args_list[0][0]
            assert "symbolic-ref" in first_call_args
            second_call_args = mock_git.call_args_list[1][0]
            assert "-b" in second_call_args
            assert "main" in second_call_args

    @pytest.mark.asyncio
    async def test_push_branch(self, tmp_path: Path) -> None:
        """Should push branch to origin with -u flag."""
        from dev_sync.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        worktree_path = tmp_path / "some-worktree"
        worktree_path.mkdir()

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""

            await manager.push_branch(worktree_path, "feature/my-branch")

            mock_git.assert_called_once_with(
                "push", "-u", "origin", "feature/my-branch",
                cwd=worktree_path,
            )

    @pytest.mark.asyncio
    async def test_symlink_context(self, tmp_path: Path) -> None:
        """Should symlink CLAUDE.md into worktree."""
        from dev_sync.core.worktree import WorktreeManager

        contexts_dir = tmp_path / "contexts"
        context_file = contexts_dir / "owner-repo" / "CLAUDE.md"
        context_file.parent.mkdir(parents=True)
        context_file.write_text("# Context")

        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        manager.symlink_context(
            worktree_path=worktree_path,
            context_path=context_file,
        )

        link = worktree_path / "CLAUDE.md"
        assert link.is_symlink()
        assert link.resolve() == context_file.resolve()
