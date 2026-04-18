"""Tests for git worktree management."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


class TestWorktreeManager:
    @pytest.mark.asyncio
    async def test_create_worktree(self, tmp_path: Path) -> None:
        """Should create worktree with correct paths."""
        from ctrlrelay.core.worktree import WorktreeManager

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
        from ctrlrelay.core.worktree import WorktreeManager

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
        from ctrlrelay.core.worktree import WorktreeManager

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
        from ctrlrelay.core.worktree import WorktreeManager

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
    async def test_create_worktree_with_new_branch_reuses_existing_branch(
        self, tmp_path: Path
    ) -> None:
        """Regression for #28: if the target branch already exists in the
        bare repo (left over from a prior session that ran out of
        max_fix_attempts with its PR pushed to origin), the worktree creator
        must reuse that branch instead of tripping
        ``fatal: a branch named 'X' already exists`` from ``worktree add -b``,
        AND refresh the local ref from origin/<branch> first so a mid-flight
        out-of-band push doesn't start the retry on stale state."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            # Reuse happy-case: local behind origin, fast-forward via scratch ref.
            #  show-ref → ls-remote (on origin) →
            #  fetch +refs/heads/<b>:refs/ctrlrelay/sync/<b> →
            #  merge-base --is-ancestor <b> refs/ctrlrelay/sync/<b> (0) →
            #  update-ref refs/heads/<b> refs/ctrlrelay/sync/<b> →
            #  update-ref -d refs/ctrlrelay/sync/<b> → worktree add
            mock_git.side_effect = [
                "",                                  # worktree prune (unconditional)
                "",                                  # show-ref --verify
                "",                                  # worktree list --porcelain
                "abc\trefs/heads/fix/issue-5\n",     # ls-remote --heads origin
                "",                                  # fetch into scratch
                "",                                  # merge-base --is-ancestor (ok)
                "",                                  # update-ref refs/heads/...
                "",                                  # update-ref -d scratch
                "",                                  # worktree add
            ]

            wt = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-1",
                new_branch="fix/issue-5",
            )

        assert "retry-1" in str(wt)

        # Fetch goes into the dedicated scratch ref, NOT overwriting refs/heads.
        fetch_calls = [c for c in mock_git.call_args_list if "fetch" in c[0]]
        assert len(fetch_calls) == 1
        fargs = fetch_calls[0][0]
        assert "origin" in fargs
        refspec = [a for a in fargs if ":" in a and "refs/" in a]
        assert refspec, f"expected a refs/... refspec in fetch; got {fargs}"
        assert "refs/ctrlrelay/sync/fix/issue-5" in refspec[0]

        # Ancestor check compares local branch against scratch ref.
        ancestor_calls = [
            c for c in mock_git.call_args_list
            if "merge-base" in c[0] and "--is-ancestor" in c[0]
        ]
        assert len(ancestor_calls) == 1
        assert "refs/ctrlrelay/sync/fix/issue-5" in ancestor_calls[0][0]

        # Fast-forward via update-ref (NOT branch -f origin/...).
        update_calls = [c for c in mock_git.call_args_list if "update-ref" in c[0]]
        assert any(
            "refs/heads/fix/issue-5" in c[0] and "refs/ctrlrelay/sync/fix/issue-5" in c[0]
            and "-d" not in c[0]
            for c in update_calls
        ), f"expected the live-branch update-ref; got {update_calls}"

        # Scratch ref gets cleaned up.
        assert any(
            "-d" in c[0] and "refs/ctrlrelay/sync/fix/issue-5" in c[0]
            for c in update_calls
        ), f"expected cleanup of scratch ref; got {update_calls}"

        # worktree add without -b.
        worktree_add_calls = [
            c for c in mock_git.call_args_list
            if "worktree" in c[0] and "add" in c[0]
        ]
        assert len(worktree_add_calls) == 1
        args = worktree_add_calls[0][0]
        assert "-b" not in args
        assert "fix/issue-5" in args

    @pytest.mark.asyncio
    async def test_reuse_preserves_local_commits_when_ahead_of_origin(
        self, tmp_path: Path
    ) -> None:
        """Codex P1: if the prior attempt made commits locally but died
        before pushing, the local branch is AHEAD of origin. The sync MUST
        NOT update refs/heads/<branch> to the remote tip — those commits
        are the only recoverable copy."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            # First merge-base check (local→remote) fails: local NOT ancestor.
            # Second check (remote→local) SUCCEEDS: remote is ancestor of
            # local, meaning local is strictly ahead. Preserve local.
            mock_git.side_effect = [
                "",                                  # worktree prune (unconditional)
                "",                                  # show-ref
                "",                                  # worktree list --porcelain
                "abc\trefs/heads/fix/issue-9\n",     # ls-remote
                "",                                  # fetch into scratch
                WorktreeError("not an ancestor"),    # merge-base local→scratch
                "",                                  # merge-base scratch→local (ok → ahead)
                "",                                  # update-ref -d scratch (finally)
                "",                                  # worktree add (reuse as-is)
            ]

            wt = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-ahead",
                new_branch="fix/issue-9",
            )

        assert "retry-ahead" in str(wt)
        # CRITICAL: no update-ref that touches refs/heads/fix/issue-9 ran.
        refs_heads_updates = [
            c for c in mock_git.call_args_list
            if "update-ref" in c[0]
            and "refs/heads/fix/issue-9" in c[0]
            and "-d" not in c[0]
        ]
        assert refs_heads_updates == [], (
            "reuse must not update refs/heads/<branch> when local is ahead "
            "(would destroy unpushed commits)"
        )

    @pytest.mark.asyncio
    async def test_reuse_raises_on_diverged_branches(self, tmp_path: Path) -> None:
        """Codex P2: if local has commits origin doesn't, AND origin has
        commits local doesn't (true divergence), silently reusing either
        side would lose commits or produce a non-fast-forward push. Surface
        the conflict as a WorktreeError so the session fails cleanly."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            # Both ancestor checks fail → diverged.
            mock_git.side_effect = [
                "",                                  # worktree prune (unconditional)
                "",                                  # show-ref
                "",                                  # worktree list --porcelain
                "abc\trefs/heads/fix/issue-3\n",     # ls-remote
                "",                                  # fetch scratch
                WorktreeError("not an ancestor"),    # merge-base local→scratch
                WorktreeError("not an ancestor"),    # merge-base scratch→local
                "",                                  # update-ref -d scratch (finally)
            ]
            with pytest.raises(WorktreeError, match="diverged"):
                await manager.create_worktree_with_new_branch(
                    repo="owner/repo",
                    session_id="retry-diverged",
                    new_branch="fix/issue-3",
                )

        # Worktree add must NOT have been attempted.
        worktree_add_calls = [
            c for c in mock_git.call_args_list
            if "worktree" in c[0] and "add" in c[0]
        ]
        assert worktree_add_calls == []

    @pytest.mark.asyncio
    async def test_reuse_falls_back_to_local_when_remote_probe_fails(
        self, tmp_path: Path
    ) -> None:
        """Codex P2: if ls-remote itself fails (auth, network), we must NOT
        treat the probe failure as "remote exists" (that would skip the
        stale-merged recreate) nor as "remote doesn't exist" (that would
        potentially recreate a branch that DOES exist on origin). Safest
        is to reuse the local ref as-is, no mutations."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = [
                "",                                  # worktree prune (unconditional)
                "",                                  # show-ref
                "",                                  # worktree list --porcelain
                WorktreeError("gh ls-remote auth"),  # strict ls-remote raises
                "",                                  # worktree add (reuse as-is)
            ]
            wt = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-probe-fail",
                new_branch="fix/issue-77",
            )

        assert "retry-probe-fail" in str(wt)
        # No ref mutation happened — nothing to the live branch.
        mutating = [
            c for c in mock_git.call_args_list
            if ("update-ref" in c[0]) or ("branch" in c[0] and "-f" in c[0])
            or ("fetch" in c[0])
        ]
        assert mutating == [], (
            f"no mutations allowed when remote probe fails; got {mutating}"
        )
        worktree_add = [
            c for c in mock_git.call_args_list
            if "worktree" in c[0] and "add" in c[0]
        ]
        assert len(worktree_add) == 1
        # Reuse path (no -b).
        assert "-b" not in worktree_add[0][0]

    @pytest.mark.asyncio
    async def test_reuse_survives_fetch_timeout(self, tmp_path: Path) -> None:
        """Codex P2: if the sync fetch times out (asyncio.TimeoutError), the
        retry must still proceed with the local branch as-is rather than
        failing the whole worktree creation."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        import asyncio as _asyncio

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            # Fetch raises TimeoutError; helper returns early (no cleanup —
            # scratch ref was never created).
            mock_git.side_effect = [
                "",                                  # worktree prune (unconditional)
                "",                                  # show-ref
                "",                                  # worktree list --porcelain
                "abc\trefs/heads/fix/issue-42\n",    # ls-remote
                _asyncio.TimeoutError(),             # fetch scratch — hangs
                "",                                  # worktree add (proceed)
            ]

            wt = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-timeout",
                new_branch="fix/issue-42",
            )

        assert "retry-timeout" in str(wt)
        # Worktree add must still have been called.
        worktree_add_calls = [
            c for c in mock_git.call_args_list
            if "worktree" in c[0] and "add" in c[0]
        ]
        assert len(worktree_add_calls) == 1

    @pytest.mark.asyncio
    async def test_reuse_refuses_when_branch_checked_out_elsewhere(
        self, tmp_path: Path
    ) -> None:
        """Codex P1: if the branch is checked out by another worktree
        (e.g. a BLOCKED session that kept its worktree alive for resume),
        mutating or deleting refs/heads/<branch> would corrupt that live
        worktree. Raise WorktreeError before touching anything."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        porcelain = (
            "worktree /Users/x/.ctrlrelay/worktrees/owner-repo-blocked-sess\n"
            "HEAD abc123def456\n"
            "branch refs/heads/fix/issue-7\n"
        )

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = [
                "",                                  # worktree prune (unconditional)
                "",                  # show-ref (local exists)
                porcelain,           # worktree list: another worktree has the branch
            ]
            with pytest.raises(WorktreeError, match="already checked out"):
                await manager.create_worktree_with_new_branch(
                    repo="owner/repo",
                    session_id="retry-colliding",
                    new_branch="fix/issue-7",
                )

        # No ref mutation should have happened.
        mutating_calls = [
            c for c in mock_git.call_args_list
            if "update-ref" in c[0] or ("branch" in c[0] and "-f" in c[0])
            or ("worktree" in c[0] and "add" in c[0])
        ]
        assert mutating_calls == [], (
            f"no mutating git ops allowed when branch is checked out "
            f"elsewhere; got {mutating_calls}"
        )

    @pytest.mark.asyncio
    async def test_prunable_worktree_stanza_does_not_block_reuse(
        self, tmp_path: Path
    ) -> None:
        """Codex P2: if `git worktree list` still reports a stale stanza
        for a worktree that's been rm -rf'd but not yet pruned, the
        branch-checked-out probe must IGNORE it (the `prunable` marker
        means the checkout is gone). Otherwise a crash between worktree
        dir removal and `git worktree prune` wedges the branch for all
        future retries."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        stale_porcelain = (
            "worktree /Users/x/.ctrlrelay/worktrees/owner-repo-crashed-sess\n"
            "HEAD abc123\n"
            "branch refs/heads/fix/issue-88\n"
            "prunable gitdir file points to non-existent location\n"
            "\n"
        )

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = [
                "",                                  # worktree prune (unconditional)
                "",                                      # show-ref (local exists)
                stale_porcelain,                         # worktree list: prunable stanza
                "",                                      # ls-remote (no remote)
                "refs/heads/main\n",                     # get_default_branch
                "+ abc unique\n",                        # cherry: unique → reuse
                "",                                      # worktree add (reuse)
            ]
            wt = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-after-crash",
                new_branch="fix/issue-88",
            )

        assert "retry-after-crash" in str(wt)
        # Reuse must have happened — no `already checked out` error raised.
        worktree_add = [
            c for c in mock_git.call_args_list
            if "worktree" in c[0] and "add" in c[0]
        ]
        assert len(worktree_add) == 1
        assert "-b" not in worktree_add[0][0]

    @pytest.mark.asyncio
    async def test_local_only_with_unique_commits_is_reused(
        self, tmp_path: Path
    ) -> None:
        """Local-only branch with unique unpushed commits (prior session
        died before push) must be REUSED, not recreated — otherwise the
        operator's work is silently dropped."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = [
                "",                                  # worktree prune (unconditional)
                "",                                      # show-ref (exists)
                "",                                      # worktree list --porcelain
                "",                                      # ls-remote (empty, not on remote)
                "refs/heads/main\n",                     # get_default_branch → symbolic-ref HEAD
                "+ abc123 unpushed\n+ def456 more\n",    # cherry default branch — all unique
                "",                                      # worktree add (reuse)
            ]
            wt = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-keep",
                new_branch="fix/issue-9",
            )

        assert "retry-keep" in str(wt)
        # No delete + no fresh-branch creation.
        delete_calls = [
            c for c in mock_git.call_args_list
            if "update-ref" in c[0] and "-d" in c[0]
        ]
        assert delete_calls == []
        worktree_add_calls = [
            c for c in mock_git.call_args_list
            if "worktree" in c[0] and "add" in c[0]
        ]
        assert len(worktree_add_calls) == 1
        # Reuse path: no -b.
        assert "-b" not in worktree_add_calls[0][0]

    @pytest.mark.asyncio
    async def test_local_only_fully_merged_is_deleted_and_recreated(
        self, tmp_path: Path
    ) -> None:
        """Codex P2: local-only branch whose commits are all already in the
        default branch (prior PR was merged — any strategy — and the
        remote was auto-deleted) must NOT be reused. Delete + recreate
        fresh from default so the next PR doesn't resurrect already-merged
        changes."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = [
                "",                                  # worktree prune (unconditional)
                "",                                      # show-ref (local exists)
                "",                                      # worktree list --porcelain
                "",                                      # ls-remote (empty)
                "refs/heads/main\n",                     # get_default_branch (1st)
                "- abc already merged\n- def ditto\n",   # cherry: all "-" (patch-equivalent)
                "",                                      # update-ref -d refs/heads/<branch>
                "refs/heads/main\n",                     # get_default_branch (2nd, for fresh path)
                "",                                      # worktree add -b
            ]
            wt = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="rerun-merged",
                new_branch="fix/issue-7",
            )

        assert "rerun-merged" in str(wt)
        # update-ref -d refs/heads/<branch> was called.
        delete_calls = [
            c for c in mock_git.call_args_list
            if "update-ref" in c[0] and "-d" in c[0]
            and "refs/heads/fix/issue-7" in c[0]
        ]
        assert len(delete_calls) == 1
        # Fresh worktree add with -b.
        worktree_add_calls = [
            c for c in mock_git.call_args_list
            if "worktree" in c[0] and "add" in c[0]
        ]
        assert len(worktree_add_calls) == 1
        args = worktree_add_calls[0][0]
        assert "-b" in args
        assert "fix/issue-7" in args

    @pytest.mark.asyncio
    async def test_local_only_empty_cherry_is_treated_as_merged(
        self, tmp_path: Path
    ) -> None:
        """If `git cherry <default> <branch>` returns empty output, the
        branch has no commits that aren't in default (branch tip == or
        ancestor of default). Treat as fully merged → delete + recreate."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = [
                "",                                  # worktree prune (unconditional)
                "",                      # show-ref
                "",                      # worktree list --porcelain
                "",                      # ls-remote
                "refs/heads/main\n",     # get_default_branch
                "",                      # cherry: empty
                "",                      # update-ref -d
                "refs/heads/main\n",     # get_default_branch (fresh path)
                "",                      # worktree add -b
            ]
            await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="s",
                new_branch="fix/issue-x",
            )

        # Path took the stale-merged branch (delete + recreate).
        delete_calls = [
            c for c in mock_git.call_args_list
            if "update-ref" in c[0] and "-d" in c[0]
        ]
        assert len(delete_calls) == 1
        worktree_add_calls = [
            c for c in mock_git.call_args_list
            if "worktree" in c[0] and "add" in c[0]
        ]
        assert "-b" in worktree_add_calls[0][0]

    @pytest.mark.asyncio
    async def test_create_worktree_with_new_branch_uses_default_branch(
        self, tmp_path: Path
    ) -> None:
        """Should use default branch when base_branch is None."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            # prune → default-branch probe → worktree add
            mock_git.side_effect = ["", "refs/heads/main\n", ""]

            worktree_path = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="sess-789",
                new_branch="feature/auto-base",
            )

            assert worktree_path is not None
            # Calls: prune, then symbolic-ref HEAD (for default), then worktree add.
            first_call_args = mock_git.call_args_list[0][0]
            assert "prune" in first_call_args
            second_call_args = mock_git.call_args_list[1][0]
            assert "symbolic-ref" in second_call_args
            third_call_args = mock_git.call_args_list[2][0]
            assert "-b" in third_call_args
            assert "main" in third_call_args

    @pytest.mark.asyncio
    async def test_push_branch(self, tmp_path: Path) -> None:
        """Should push branch to origin with -u flag."""
        from ctrlrelay.core.worktree import WorktreeManager

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
        from ctrlrelay.core.worktree import WorktreeManager

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

    @pytest.mark.asyncio
    async def test_branch_exists_locally_true(self, tmp_path: Path) -> None:
        """show-ref returning 0 means the branch is a local ref."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "wt",
            bare_repos_dir=tmp_path / "repos",
        )
        bare_path = tmp_path / "repos" / "owner-repo.git"
        bare_path.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""
            assert await manager.branch_exists_locally("owner/repo", "fix/issue-13") is True

    @pytest.mark.asyncio
    async def test_branch_exists_locally_false(self, tmp_path: Path) -> None:
        """show-ref raising means the branch does not exist locally."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "wt",
            bare_repos_dir=tmp_path / "repos",
        )
        bare_path = tmp_path / "repos" / "owner-repo.git"
        bare_path.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = WorktreeError("bad ref")
            assert await manager.branch_exists_locally("owner/repo", "fix/issue-13") is False

    @pytest.mark.asyncio
    async def test_branch_exists_locally_no_bare_repo(self, tmp_path: Path) -> None:
        """With no bare repo yet, the branch cannot exist locally."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "wt",
            bare_repos_dir=tmp_path / "repos",
        )
        assert await manager.branch_exists_locally("owner/repo", "fix/issue-13") is False

    @pytest.mark.asyncio
    async def test_branch_exists_on_remote_true(self, tmp_path: Path) -> None:
        """ls-remote returning a ref line means the branch is on origin."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "wt",
            bare_repos_dir=tmp_path / "repos",
        )
        bare_path = tmp_path / "repos" / "owner-repo.git"
        bare_path.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = (
                "abc123\trefs/heads/fix/issue-13\n"
            )
            assert await manager.branch_exists_on_remote("owner/repo", "fix/issue-13") is True

    @pytest.mark.asyncio
    async def test_branch_exists_on_remote_false(self, tmp_path: Path) -> None:
        """Empty ls-remote output means the branch is not on origin."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "wt",
            bare_repos_dir=tmp_path / "repos",
        )
        bare_path = tmp_path / "repos" / "owner-repo.git"
        bare_path.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""
            assert await manager.branch_exists_on_remote("owner/repo", "fix/issue-13") is False

    @pytest.mark.asyncio
    async def test_branch_exists_on_remote_uses_short_timeout(
        self, tmp_path: Path
    ) -> None:
        """The remote probe must NOT inherit the default 120s timeout — a
        flaky network would otherwise hold the repo lock for 2 extra minutes
        on every failed cleanup."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "wt",
            bare_repos_dir=tmp_path / "repos",
        )
        bare_path = tmp_path / "repos" / "owner-repo.git"
        bare_path.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""
            await manager.branch_exists_on_remote("owner/repo", "fix/issue-13")
            assert mock_git.await_args.kwargs.get("timeout") == 10

    @pytest.mark.asyncio
    async def test_branch_exists_on_remote_fails_closed_on_timeout(
        self, tmp_path: Path
    ) -> None:
        """If ls-remote hangs / times out (credential prompt, flaky network),
        the probe must return True so callers preserve the branch instead of
        mistakenly treating it as local-only."""
        import asyncio as _asyncio

        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "wt",
            bare_repos_dir=tmp_path / "repos",
        )
        bare_path = tmp_path / "repos" / "owner-repo.git"
        bare_path.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = _asyncio.TimeoutError()
            assert await manager.branch_exists_on_remote("owner/repo", "fix/issue-13") is True

    @pytest.mark.asyncio
    async def test_branch_exists_on_remote_fails_closed_on_worktree_error(
        self, tmp_path: Path
    ) -> None:
        """Generic git failures (auth, network refused) also fail closed."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "wt",
            bare_repos_dir=tmp_path / "repos",
        )
        bare_path = tmp_path / "repos" / "owner-repo.git"
        bare_path.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = WorktreeError("fatal: could not read Username")
            assert await manager.branch_exists_on_remote("owner/repo", "fix/issue-13") is True
