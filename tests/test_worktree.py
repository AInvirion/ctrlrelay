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
    async def test_ensure_bare_repo_fetches_with_explicit_refspec(
        self, tmp_path: Path
    ) -> None:
        """When the bare already exists, fetch must use an explicit
        refspec — some bare clones have no remote.origin.fetch config
        so `git fetch --all` is a silent no-op. Without explicit
        refspec we saw a task-pipeline run report test counts from a
        commit two weeks behind origin. The fix writes
        ``refs/heads/*:refs/heads/*`` directly so ``refs/heads/main``
        stays in sync regardless of config state. Non-force on
        purpose so dev's branch-reuse path can still preserve
        unpushed local commits — see assertion below."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        # Pre-create the bare so ensure_bare_repo takes the fetch branch.
        bare = manager._get_bare_repo_path("owner/repo")
        bare.mkdir(parents=True)

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""
            await manager.ensure_bare_repo("owner/repo")

        assert mock_git.await_count == 1
        args = mock_git.await_args.args
        # Must be a fetch, not --all, with the explicit refspec.
        assert args[0] == "fetch"
        assert "origin" in args
        # Non-force refspec on purpose: codex P1 — a force fetch
        # would destroy unpushed local commits in the dev pipeline's
        # branch-reuse flow. Fast-forward-only fixes the stale-
        # default-branch case while leaving divergent dev branches
        # for the reuse logic to handle.
        assert "refs/heads/*:refs/heads/*" in args
        assert "+refs/heads/*:refs/heads/*" not in args
        assert "--prune" in args
        # Explicit refspec must win over the bare `--all` that used to
        # silently no-op on refspec-less repos.
        assert "--all" not in args

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

            worktree_path, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="sess-456",
                new_branch="feature/my-branch",
                base_branch="main",
            )

            assert worktree_path.parent == tmp_path / "worktrees"
            assert "sess-456" in str(worktree_path)
            # Fresh-branch path must signal ownership to the caller so
            # run_dev_issue's FAILED cleanup can delete the branch
            # (issue #51).
            assert created_fresh is True

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
                "",                                  # show-ref --verify
                "",                                  # worktree list --porcelain
                "abc\trefs/heads/fix/issue-5\n",     # ls-remote --heads origin
                "",                                  # fetch into scratch
                "",                                  # merge-base --is-ancestor (ok)
                "",                                  # update-ref refs/heads/...
                "",                                  # update-ref -d scratch
                "",                                  # worktree add
            ]

            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-1",
                new_branch="fix/issue-5",
            )

        assert "retry-1" in str(wt)
        # Reuse path: branch existed, we did NOT create it. Cleanup on
        # failure must NOT delete this branch — the prior PR may still
        # reference it (issue #51).
        assert created_fresh is False

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
                "",                                  # show-ref
                "",                                  # worktree list --porcelain
                "abc\trefs/heads/fix/issue-9\n",     # ls-remote
                "",                                  # fetch into scratch
                WorktreeError("not an ancestor"),    # merge-base local→scratch
                "",                                  # merge-base scratch→local (ok → ahead)
                "",                                  # update-ref -d scratch (finally)
                "",                                  # worktree add (reuse as-is)
            ]

            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-ahead",
                new_branch="fix/issue-9",
            )

        assert "retry-ahead" in str(wt)
        # Reused an existing branch (unpushed commits preserved) → not fresh.
        assert created_fresh is False
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
                "",                                  # show-ref
                "",                                  # worktree list --porcelain
                WorktreeError("gh ls-remote auth"),  # strict ls-remote raises
                "",                                  # worktree add (reuse as-is)
            ]
            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-probe-fail",
                new_branch="fix/issue-77",
            )

        assert "retry-probe-fail" in str(wt)
        # Probe failed → reuse local as-is, nothing was freshly created.
        assert created_fresh is False
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
                "",                                  # show-ref
                "",                                  # worktree list --porcelain
                "abc\trefs/heads/fix/issue-42\n",    # ls-remote
                _asyncio.TimeoutError(),             # fetch scratch — hangs
                "",                                  # worktree add (proceed)
            ]

            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-timeout",
                new_branch="fix/issue-42",
            )

        assert "retry-timeout" in str(wt)
        # Fetch timeout → reuse local as-is, not a fresh creation.
        assert created_fresh is False
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
    async def test_worktree_add_retries_after_targeted_prune_on_stale_admin(
        self, tmp_path: Path
    ) -> None:
        """Codex P1: if a prior worktree crashed between rmtree and prune,
        `git worktree add` fails with 'already checked out' even though no
        live checkout exists. The reuse path must detect the stale entry,
        run a scoped prune, and retry — NOT an unconditional up-front
        prune that would also remove admin state for unrelated worktrees
        whose paths are temporarily unavailable."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        # Stale admin dir is paired to a worktree path via its `gitdir`
        # file, NOT via basename. Deliberately use a non-matching admin
        # name to prove the resolver uses the canonical pointer.
        stale_worktree_path_str = str(
            tmp_path / "worktrees" / "owner-repo-crashed-sess"
        )
        admin_dir = bare / "worktrees" / "sanitized-differently"
        admin_dir.mkdir(parents=True)
        (admin_dir / "HEAD").write_text("ref: refs/heads/fix/issue-99\n")
        (admin_dir / "gitdir").write_text(
            f"{stale_worktree_path_str}/.git\n"
        )
        # Also create an unrelated admin dir that would be wrongly
        # targeted by the old basename heuristic, to prove we don't
        # touch it.
        decoy = bare / "worktrees" / "owner-repo-crashed-sess"
        decoy.mkdir(parents=True)
        (decoy / "HEAD").write_text("ref: refs/heads/other-branch\n")
        (decoy / "gitdir").write_text("/some/other/worktree/.git\n")

        stale_porcelain = (
            f"worktree {stale_worktree_path_str}\n"
            "HEAD abc\n"
            "branch refs/heads/fix/issue-99\n"
            "prunable gitdir file points to non-existent location\n"
            "\n"
        )

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = [
                "",                                      # show-ref (branch exists)
                "",                                      # worktree list (no live, prunable only)
                "",                                      # ls-remote (no remote)
                "refs/heads/main\n",                     # get_default_branch
                "+ abc unique\n",                        # cherry (unique — reuse path)
                # first worktree add attempt FAILS on stale admin
                WorktreeError("fatal: 'fix/issue-99' is already checked out at /stale"),
                stale_porcelain,                         # porcelain probe for stale entry
                "",                                      # worktree add (retry succeeds)
            ]
            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-after-crash",
                new_branch="fix/issue-99",
            )

        assert "retry-after-crash" in str(wt)
        # Cherry returned "unique" → reuse path (local-only with unpushed
        # work). Created_fresh stays False.
        assert created_fresh is False
        # No repo-wide `git worktree prune` was called.
        prune_calls = [
            c for c in mock_git.call_args_list
            if "prune" in c[0]
        ]
        assert prune_calls == [], (
            "must not run repo-wide prune; targeted rmtree should suffice"
        )
        # The stale admin dir for THIS branch (matched via gitdir pointer,
        # not basename) was removed.
        assert not admin_dir.exists()
        # The unrelated same-basename admin dir was NOT touched.
        assert decoy.exists(), (
            "basename-only matcher would have wrongly deleted this; "
            "gitdir-pointer resolver must spare it"
        )
        # Two add attempts were made: first failed, second retry succeeded.
        add_calls = [
            c for c in mock_git.call_args_list
            if "worktree" in c[0] and "add" in c[0]
        ]
        assert len(add_calls) == 2

    @pytest.mark.asyncio
    async def test_stale_recovery_refuses_paths_outside_managed_worktrees_dir(
        self, tmp_path: Path
    ) -> None:
        """Codex P1: a `prunable` stanza can also represent a worktree on
        a temporarily-unavailable path (network mount, removable drive).
        The recovery path must only touch stale entries UNDER our
        managed worktrees_dir; anything else (user-managed path, network
        mount) is surfaced as the original error so the operator can
        decide, not silently deleted."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        # Stale worktree is claimed under /Volumes/Backup (outside our dir).
        external_stale_path = "/Volumes/Backup/some-worktree-elsewhere"
        admin_dir = bare / "worktrees" / "something"
        admin_dir.mkdir(parents=True)
        (admin_dir / "gitdir").write_text(f"{external_stale_path}/.git\n")

        stale_porcelain = (
            f"worktree {external_stale_path}\n"
            "HEAD abc\n"
            "branch refs/heads/fix/issue-55\n"
            "prunable gitdir file points to non-existent location\n"
            "\n"
        )

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = [
                "",                                      # show-ref
                "",                                      # worktree list (no live)
                "",                                      # ls-remote
                "refs/heads/main\n",                     # get_default_branch
                "+ abc unique\n",                        # cherry
                WorktreeError(
                    "fatal: 'fix/issue-55' is already checked out at "
                    + external_stale_path
                ),                                       # first add fails
                stale_porcelain,                         # porcelain probe
            ]
            with pytest.raises(WorktreeError, match="already checked out"):
                await manager.create_worktree_with_new_branch(
                    repo="owner/repo",
                    session_id="retry-external-path",
                    new_branch="fix/issue-55",
                )

        # Admin dir MUST NOT have been removed (could be on a mount that
        # comes back later).
        assert admin_dir.exists()

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
                "",                                      # show-ref (local exists)
                stale_porcelain,                         # worktree list: prunable stanza
                "",                                      # ls-remote (no remote)
                "refs/heads/main\n",                     # get_default_branch
                "+ abc unique\n",                        # cherry: unique → reuse
                "",                                      # worktree add (reuse)
            ]
            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-after-crash",
                new_branch="fix/issue-88",
            )

        assert "retry-after-crash" in str(wt)
        # Prunable stanza was correctly ignored; reuse path fires → not fresh.
        assert created_fresh is False
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
                "",                                      # show-ref (exists)
                "",                                      # worktree list --porcelain
                "",                                      # ls-remote (empty, not on remote)
                "refs/heads/main\n",                     # get_default_branch → symbolic-ref HEAD
                "+ abc123 unpushed\n+ def456 more\n",    # cherry default branch — all unique
                "",                                      # worktree add (reuse)
            ]
            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-keep",
                new_branch="fix/issue-9",
            )

        assert "retry-keep" in str(wt)
        # Unique commits → reused, not freshly created.
        assert created_fresh is False
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
                "",                                      # show-ref (local exists)
                "",                                      # worktree list --porcelain
                "",                                      # ls-remote (empty)
                "refs/heads/main\n",                     # get_default_branch (1st)
                "- abc already merged\n- def ditto\n",   # cherry: all "-" (patch-equivalent)
                "",                                      # update-ref -d refs/heads/<branch>
                "refs/heads/main\n",                     # get_default_branch (2nd, for fresh path)
                "",                                      # worktree add -b
            ]
            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="rerun-merged",
                new_branch="fix/issue-7",
            )

        assert "rerun-merged" in str(wt)
        # Issue #51 regression: the branch in the repo AFTER this call
        # was created by THIS session (we deleted the stale merged local
        # ref and recreated from default). Cleanup on FAILED must treat
        # it as ours. Before #51 fix, run_dev_issue snapshotted
        # branch_preexisted=True before the call and incorrectly skipped
        # delete_branch, leaking partial commits into the next retry.
        assert created_fresh is True
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
                "",                      # show-ref
                "",                      # worktree list --porcelain
                "",                      # ls-remote
                "refs/heads/main\n",     # get_default_branch
                "",                      # cherry: empty
                "",                      # update-ref -d
                "refs/heads/main\n",     # get_default_branch (fresh path)
                "",                      # worktree add -b
            ]
            _wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="s",
                new_branch="fix/issue-x",
            )

        # Path took the stale-merged branch (delete + recreate) → fresh.
        assert created_fresh is True
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
    async def test_reuse_refuses_branch_backing_open_pr(
        self, tmp_path: Path
    ) -> None:
        """Issue #52: if ``new_branch`` already exists locally AND still
        backs an open PR on GitHub (prior DONE session whose PR wasn't
        merged, or any external source), the reuse path must refuse
        before any ref mutation. Running Claude on the reviewer's
        already-reviewed branch would hijack the PR or later trip
        ``gh pr create``'s "A pull request already exists"."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        mock_github = AsyncMock()
        mock_github.list_prs.return_value = [
            {
                "number": 42,
                "headRefName": "fix/issue-13",
                # Same owner as the target repo — this IS ours.
                "headRepositoryOwner": {"login": "owner"},
            },
        ]

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            # show-ref (branch exists) + worktree list (no live checkout).
            # Probe happens BEFORE ls-remote / cherry — nothing else runs.
            mock_git.side_effect = [
                "",        # show-ref (branch exists)
                "",        # worktree list --porcelain (no live checkout)
            ]
            with pytest.raises(WorktreeError, match="#42") as exc_info:
                await manager.create_worktree_with_new_branch(
                    repo="owner/repo",
                    session_id="retry-13",
                    new_branch="fix/issue-13",
                    github=mock_github,
                )

        # Error message names the branch and gives a concrete action.
        msg = str(exc_info.value)
        assert "fix/issue-13" in msg
        assert "open PR #42" in msg
        assert "close or merge" in msg.lower() or "close" in msg.lower()

        # GitHub was asked with state=open and head filter.
        mock_github.list_prs.assert_awaited_once()
        kwargs = mock_github.list_prs.await_args.kwargs
        assert kwargs.get("state") == "open"
        assert kwargs.get("head") == "fix/issue-13"

        # No ref mutation or worktree add happened — the refusal is
        # BEFORE any branch touches.
        mutating_calls = [
            c for c in mock_git.call_args_list
            if "update-ref" in c[0]
            or ("worktree" in c[0] and "add" in c[0])
            or "fetch" in c[0]
        ]
        assert mutating_calls == [], (
            f"no ref mutation allowed when branch backs an open PR; "
            f"got {mutating_calls}"
        )

    @pytest.mark.asyncio
    async def test_reuse_ignores_fork_pr_with_same_branch_name(
        self, tmp_path: Path
    ) -> None:
        """Codex P2 on #113: ``gh pr list --head`` filters on branch
        name alone and can't scope to ``<owner>:<branch>``. An
        unrelated contributor's fork PR using the same branch name
        (e.g. another ``fix/issue-13``) must NOT block our reuse —
        only a PR whose head lives in the target repo's owner veto.
        """
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        mock_github = AsyncMock()
        # A single PR from a fork with the same branch name. We're
        # "owner/repo"; the fork lives under "someforker".
        mock_github.list_prs.return_value = [
            {
                "number": 99,
                "headRefName": "fix/issue-5",
                "headRepositoryOwner": {"login": "someforker"},
            },
        ]

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = [
                "",                                      # show-ref
                "",                                      # worktree list
                "abc\trefs/heads/fix/issue-5\n",         # ls-remote
                "",                                      # fetch scratch
                "",                                      # merge-base local→remote
                "",                                      # update-ref refs/heads
                "",                                      # update-ref -d scratch
                "",                                      # worktree add
            ]
            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-fork",
                new_branch="fix/issue-5",
                github=mock_github,
            )

        # Reuse proceeded: fork PR doesn't veto.
        assert "retry-fork" in str(wt)
        assert created_fresh is False

    @pytest.mark.asyncio
    async def test_reuse_proceeds_when_no_open_pr(
        self, tmp_path: Path
    ) -> None:
        """Issue #52 happy path: the branch exists locally but has NO
        open PR on GitHub (prior PR was merged OR the branch is a local
        leftover from a session that died before pushing). Reuse must
        proceed normally — the open-PR probe is a filter, not a
        blanket block on reused branches."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        mock_github = AsyncMock()
        mock_github.list_prs.return_value = []  # no open PR

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            # Happy-case reuse: branch exists locally, no open PR, remote
            # has the branch → sync + reuse (no -b).
            mock_git.side_effect = [
                "",                                      # show-ref
                "",                                      # worktree list
                "abc\trefs/heads/fix/issue-5\n",         # ls-remote
                "",                                      # fetch scratch
                "",                                      # merge-base local→remote (ok)
                "",                                      # update-ref refs/heads
                "",                                      # update-ref -d scratch
                "",                                      # worktree add
            ]
            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-clean",
                new_branch="fix/issue-5",
                github=mock_github,
            )

        assert "retry-clean" in str(wt)
        # Reused existing branch; not a fresh creation.
        assert created_fresh is False
        mock_github.list_prs.assert_awaited_once()
        # Normal reuse path: worktree add WITHOUT -b.
        worktree_add = [
            c for c in mock_git.call_args_list
            if "worktree" in c[0] and "add" in c[0]
        ]
        assert len(worktree_add) == 1
        assert "-b" not in worktree_add[0][0]

    @pytest.mark.asyncio
    async def test_reuse_survives_pr_probe_failure(
        self, tmp_path: Path
    ) -> None:
        """Issue #52: if the PR probe itself fails (transient gh/network
        error), don't wedge the retry — the later ``gh pr create`` is
        defence in depth. This keeps a flaky GitHub API from blocking
        every retry on every issue."""
        from ctrlrelay.core.github import GitHubError
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        mock_github = AsyncMock()
        mock_github.list_prs.side_effect = GitHubError("network timeout")

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            # Happy-case reuse path after the probe swallowed the failure.
            mock_git.side_effect = [
                "",                                      # show-ref
                "",                                      # worktree list
                "abc\trefs/heads/fix/issue-1\n",         # ls-remote
                "",                                      # fetch scratch
                "",                                      # merge-base local→remote
                "",                                      # update-ref refs/heads
                "",                                      # update-ref -d scratch
                "",                                      # worktree add
            ]
            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="retry-probe-flaky",
                new_branch="fix/issue-1",
                github=mock_github,
            )

        assert "retry-probe-flaky" in str(wt)
        assert created_fresh is False

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
            mock_git.side_effect = ["refs/heads/main\n", ""]

            worktree_path, created_fresh = await manager.create_worktree_with_new_branch(
                repo="owner/repo",
                session_id="sess-789",
                new_branch="feature/auto-base",
            )

            assert worktree_path is not None
            assert created_fresh is True
            # First call: symbolic-ref HEAD (default branch probe).
            first_call_args = mock_git.call_args_list[0][0]
            assert "symbolic-ref" in first_call_args
            # Second call: worktree add -b <new> <base>.
            second_call_args = mock_git.call_args_list[1][0]
            assert "-b" in second_call_args
            assert "main" in second_call_args

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


class TestRunGitCancellation:
    """Regression for codex round-9 [P2]: `_run_git` only killed the child
    on TimeoutError; a CancelledError during `proc.communicate()`
    (scheduler shutdown during scheduled secops) left the git subprocess
    running in the background where it could mutate the bare repo /
    worktree after the poller exited."""

    @pytest.mark.asyncio
    async def test_run_git_kills_child_on_cancel(
        self, tmp_path: Path
    ) -> None:
        import asyncio
        from unittest.mock import MagicMock

        from ctrlrelay.core.worktree import WorktreeManager

        mgr = WorktreeManager(
            worktrees_dir=tmp_path / "wt",
            bare_repos_dir=tmp_path / "bare",
            timeout=60,
        )

        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.communicate.side_effect = asyncio.CancelledError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(asyncio.CancelledError):
                await mgr._run_git("status")

        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited()
