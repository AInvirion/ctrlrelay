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
            mock_git.return_value = "refs/heads/main\n"
            await manager.ensure_bare_repo("owner/repo")

        # symbolic-ref (default-branch lookup), the worktree listing
        # (#253: checked-out branches are left out), then the fetch.
        assert mock_git.await_count == 3
        assert mock_git.await_args_list[0].args[0] == "symbolic-ref"
        assert mock_git.await_args_list[1].args[:2] == ("worktree", "list")
        args = mock_git.await_args.args
        # Must be a fetch, not --all, with the explicit refspec.
        assert args[0] == "fetch"
        assert "origin" in args
        # Non-force refspec on purpose: a force fetch
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
                # Same owner AND same repo name as the target — this
                # is the PR we're about to collide with.
                "headRepositoryOwner": {"login": "owner"},
                "headRepository": {"name": "repo"},
            },
        ]

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            # Probe runs BEFORE ls-remote (branch-deleted-PR-still-open
            # is a real failure mode). Sequence: show-ref → worktree
            # list → probe raises.
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
        # before any mutating call. ls-remote is a pure read probe.
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
                "headRepository": {"name": "repo"},
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
    async def test_reuse_probes_open_pr_even_for_local_only_branch(
        self, tmp_path: Path
    ) -> None:
        """Codex P1 round-7 on #113: GitHub can keep a same-repo PR
        open after its head branch is deleted on origin. If we skip
        the probe for local-only branches, a retry would push the
        branch back to origin and re-attach this session's commits
        to the reviewer-owned PR. The probe therefore runs BEFORE
        the on-remote gate, not after."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        # An open PR for this branch exists even though the head
        # branch was deleted on origin (stale-merged-but-PR-still-
        # open scenario).
        mock_github = AsyncMock()
        mock_github.list_prs.return_value = [
            {
                "number": 88,
                "headRefName": "fix/issue-42",
                "headRepositoryOwner": {"login": "owner"},
                "headRepository": {"name": "repo"},
            },
        ]

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            # Only the first two calls are reached — probe raises
            # before ls-remote runs.
            mock_git.side_effect = [
                "",  # show-ref (branch exists)
                "",  # worktree list (no live checkout)
            ]
            with pytest.raises(WorktreeError, match="#88"):
                await manager.create_worktree_with_new_branch(
                    repo="owner/repo",
                    session_id="retry-hijack-risk",
                    new_branch="fix/issue-42",
                    github=mock_github,
                )

        # Probe MUST be called for local-only branches too.
        mock_github.list_prs.assert_awaited_once()
        # No ls-remote / fetch / add — probe aborted before any
        # ref-touching operation.
        mutating_calls = [
            c for c in mock_git.call_args_list
            if "ls-remote" in c[0]
            or "fetch" in c[0]
            or ("worktree" in c[0] and "add" in c[0])
        ]
        assert mutating_calls == []

    @pytest.mark.asyncio
    async def test_reuse_refuses_open_pr_with_casing_mismatch(
        self, tmp_path: Path
    ) -> None:
        """Codex P2 round-3 on #113: GitHub owner/repo names are
        case-insensitive. Config may be ``Owner/Repo`` while the API
        returns ``owner/repo``; exact equality misses the match and
        the veto is silently skipped. Normalize both sides."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        # Bare dir name derives from the repo string verbatim — the
        # code does `repo.replace("/", "-")` without lowering, so the
        # "Owner/Repo" config produces "Owner-Repo.git" on disk. On
        # Linux (case-sensitive FS) this must match exactly; macOS
        # would accept either casing.
        bare = tmp_path / "repos" / "Owner-Repo.git"
        bare.mkdir(parents=True)

        # Config says "Owner/Repo"; API returns lowercase.
        mock_github = AsyncMock()
        mock_github.list_prs.return_value = [
            {
                "number": 17,
                "headRefName": "fix/issue-13",
                "headRepositoryOwner": {"login": "owner"},
                "headRepository": {"name": "repo"},
            },
        ]

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = [
                "",  # show-ref
                "",  # worktree list
            ]
            with pytest.raises(WorktreeError, match="#17"):
                await manager.create_worktree_with_new_branch(
                    repo="Owner/Repo",
                    session_id="retry-case",
                    new_branch="fix/issue-13",
                    github=mock_github,
                )

    @pytest.mark.asyncio
    async def test_reuse_ignores_same_owner_fork_pr(
        self, tmp_path: Path
    ) -> None:
        """Codex P2 round-2 on #113: same-owner forks (``acme/repo-fork``
        targeting ``acme/repo``) must also not veto reuse. Matching
        on headRepositoryOwner.login alone conflated them; the filter
        must also check headRepository.name."""
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "acme-repo.git"
        bare.mkdir(parents=True)

        mock_github = AsyncMock()
        # Same owner but a different repo name (a fork under the same
        # org). The branch name collides but the PR has nothing to
        # do with our base repo.
        mock_github.list_prs.return_value = [
            {
                "number": 77,
                "headRefName": "fix/issue-5",
                "headRepositoryOwner": {"login": "acme"},
                "headRepository": {"name": "repo-fork"},
            },
        ]

        with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
            mock_git.side_effect = [
                "",                                      # show-ref
                "",                                      # worktree list
                "abc\trefs/heads/fix/issue-5\n",         # ls-remote
                "",                                      # fetch scratch
                "",                                      # merge-base
                "",                                      # update-ref refs/heads
                "",                                      # update-ref -d scratch
                "",                                      # worktree add
            ]
            wt, created_fresh = await manager.create_worktree_with_new_branch(
                repo="acme/repo",
                session_id="retry-same-owner-fork",
                new_branch="fix/issue-5",
                github=mock_github,
            )

        assert "retry-same-owner-fork" in str(wt)
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
    async def test_reuse_refuses_when_pr_probe_fails_closed(
        self, tmp_path: Path
    ) -> None:
        """Codex P1 round-5 on #113: the open-PR probe must fail
        CLOSED, not open. The dev pipeline pushes to the branch
        BEFORE `gh pr create`, so silently reusing on probe failure
        would let a push hijack an existing PR before any later
        error surfaces. Retry a few times for transient hiccups,
        then refuse with a clear message."""
        from ctrlrelay.core import worktree as worktree_mod
        from ctrlrelay.core.github import GitHubError
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        mock_github = AsyncMock()
        mock_github.list_prs.side_effect = GitHubError("network timeout")

        # Shrink the probe sleep so the test runs fast without
        # changing the retry count (that's part of the behavior).
        orig_sleep = worktree_mod._PR_PROBE_RETRY_SLEEP_SECONDS
        worktree_mod._PR_PROBE_RETRY_SLEEP_SECONDS = 0.0
        try:
            with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
                mock_git.side_effect = [
                    "",  # show-ref
                    "",  # worktree list
                ]
                with pytest.raises(
                    WorktreeError, match="backs an open PR"
                ):
                    await manager.create_worktree_with_new_branch(
                        repo="owner/repo",
                        session_id="retry-probe-flaky",
                        new_branch="fix/issue-1",
                        github=mock_github,
                    )
        finally:
            worktree_mod._PR_PROBE_RETRY_SLEEP_SECONDS = orig_sleep

        # Probe retried the configured number of times before raising.
        assert mock_github.list_prs.await_count == (
            worktree_mod._PR_PROBE_RETRY_ATTEMPTS
        )

    @pytest.mark.asyncio
    async def test_reuse_survives_transient_probe_error(
        self, tmp_path: Path
    ) -> None:
        """Codex P1 round-5: a single transient probe failure is
        recovered by retry — the operator shouldn't see WorktreeError
        every time gh has a blip. Second attempt succeeds with no
        open PR, reuse proceeds."""
        from ctrlrelay.core import worktree as worktree_mod
        from ctrlrelay.core.github import GitHubError
        from ctrlrelay.core.worktree import WorktreeManager

        manager = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)

        call_count = {"n": 0}

        async def flaky_list_prs(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise GitHubError("transient blip")
            return []

        mock_github = AsyncMock()
        mock_github.list_prs.side_effect = flaky_list_prs

        orig_sleep = worktree_mod._PR_PROBE_RETRY_SLEEP_SECONDS
        worktree_mod._PR_PROBE_RETRY_SLEEP_SECONDS = 0.0
        try:
            with patch.object(manager, "_run_git", new_callable=AsyncMock) as mock_git:
                mock_git.side_effect = [
                    "",                                      # show-ref
                    "",                                      # worktree list
                    "abc\trefs/heads/fix/issue-1\n",         # ls-remote
                    "",                                      # fetch scratch
                    "",                                      # merge-base
                    "",                                      # update-ref
                    "",                                      # update-ref -d
                    "",                                      # worktree add
                ]
                wt, created_fresh = await manager.create_worktree_with_new_branch(
                    repo="owner/repo",
                    session_id="retry-transient",
                    new_branch="fix/issue-1",
                    github=mock_github,
                )
        finally:
            worktree_mod._PR_PROBE_RETRY_SLEEP_SECONDS = orig_sleep

        assert "retry-transient" in str(wt)
        assert created_fresh is False
        # Retried exactly twice (first raised, second returned []).
        assert call_count["n"] == 2

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


class TestGitTimeoutIsDiagnosable:
    """A bare `asyncio.TimeoutError()` stringifies to "". That empty
    string propagated all the way to Telegram, where the secops alert
    rendered as "Error processing <repo>" with no cause attached."""

    @pytest.mark.asyncio
    async def test_git_timeout_carries_a_message(self, tmp_path: Path) -> None:
        import asyncio

        from ctrlrelay.core.worktree import WorktreeManager

        wt = WorktreeManager(
            worktrees_dir=tmp_path / "wt",
            bare_repos_dir=tmp_path / "bare",
            timeout=1,
        )

        async def _always_timeout(*_a: object, **_kw: object) -> None:
            raise asyncio.TimeoutError()

        with patch(
            "ctrlrelay.core.worktree.asyncio.wait_for", _always_timeout
        ):
            with pytest.raises(asyncio.TimeoutError) as excinfo:
                await wt._run_git("fetch", "--prune", "origin", timeout=42)

        message = str(excinfo.value)
        assert message, "timeout must not stringify to an empty string"
        assert "timed out after 42s" in message
        assert "fetch --prune origin" in message

    @pytest.mark.asyncio
    async def test_transfer_timeout_applies_to_clone_and_fetch(
        self, tmp_path: Path
    ) -> None:
        """Network transfers must not inherit the 120s plumbing budget.
        A 1.8 GB bare clone cannot finish inside it, and the failure was
        indistinguishable from a hung git."""
        from unittest.mock import AsyncMock

        from ctrlrelay.core.worktree import (
            _GIT_TRANSFER_TIMEOUT_SECONDS,
            WorktreeManager,
        )

        assert _GIT_TRANSFER_TIMEOUT_SECONDS > 120

        wt = WorktreeManager(
            worktrees_dir=tmp_path / "wt",
            bare_repos_dir=tmp_path / "bare",
        )
        wt._run_git = AsyncMock(return_value="")  # type: ignore[method-assign]

        # Bare repo absent -> clone path.
        await wt.ensure_bare_repo("owner/repo")
        clone_kwargs = wt._run_git.await_args_list[0].kwargs
        assert clone_kwargs["timeout"] == _GIT_TRANSFER_TIMEOUT_SECONDS

        # Bare repo present -> fetch path.
        wt._get_bare_repo_path("owner/repo").mkdir(parents=True, exist_ok=True)
        wt._run_git.reset_mock()
        await wt.ensure_bare_repo("owner/repo")
        fetch_kwargs = next(
            c.kwargs for c in wt._run_git.await_args_list
            if c.args and c.args[0] == "fetch"
        )
        assert fetch_kwargs["timeout"] == _GIT_TRANSFER_TIMEOUT_SECONDS


class TestFetchSurvivesRewrittenBranches:
    """A secops sweep died on `AInvirion/zzsites` with the message
    `WorktreeError: git failed: ... ! [rejected] ... (non-fast-forward)`
    — while the same fetch had already advanced `main`. Dependabot
    rebases its branches, which rewrites their tips, so under the
    non-force refspec every rebase is a rejection and `git fetch`
    exits 1. Twelve bare repos held such a ref when this was found.
    """

    @staticmethod
    def _git(*args: str, cwd: Path) -> str:
        import subprocess

        env = {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": str(cwd),
        }
        return subprocess.run(
            ["git", *args], cwd=cwd, env=env,
            capture_output=True, text=True, check=True,
        ).stdout

    @classmethod
    def _build_repos(cls, tmp_path: Path) -> tuple[Path, Path]:
        """origin with a rewritten dependabot branch and a rewritten
        `fix/` branch; a bare clone holding its own commit on each."""
        import subprocess

        origin = tmp_path / "origin"
        origin.mkdir()
        cls._git("init", "-q", "-b", "main", ".", cwd=origin)
        (origin / "f").write_text("base\n")
        cls._git("add", ".", cwd=origin)
        cls._git("commit", "-qm", "base", cwd=origin)
        cls._git("branch", "dependabot/uv/pkg-1.0", cwd=origin)
        cls._git("branch", "fix/issue-1", cwd=origin)

        bare = tmp_path / "bare.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(origin), str(bare)],
            check=True, capture_output=True,
        )

        # Rewrite both branches on origin so neither is a descendant of
        # what the bare clone now holds.
        for br, text in (
            ("dependabot/uv/pkg-1.0", "rebased"), ("fix/issue-1", "forced"),
        ):
            cls._git("checkout", "-q", br, cwd=origin)
            (origin / "f").write_text(text + "\n")
            cls._git("commit", "-qam", "v1", cwd=origin)
            cls._git("commit", "-q", "--amend", "-m", br, cwd=origin)

        # Give the bare repo a local-only commit on each branch.
        main_tree = cls._git(
            "rev-parse", "refs/heads/main^{tree}", cwd=bare
        ).strip()
        for br in ("dependabot/uv/pkg-1.0", "fix/issue-1"):
            sha = cls._git(
                "commit-tree", main_tree, "-p", "refs/heads/main",
                "-m", f"local work on {br}", cwd=bare,
            ).strip()
            cls._git("update-ref", f"refs/heads/{br}", sha, cwd=bare)
        return origin, bare

    @staticmethod
    def _sha(bare: Path, ref: str) -> str:
        import subprocess

        return subprocess.run(
            ["git", "rev-parse", ref], cwd=bare,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    @pytest.mark.asyncio
    async def test_rebased_dependabot_branch_no_longer_fails_the_sweep(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.worktree import WorktreeManager

        origin, bare = self._build_repos(tmp_path)
        before_fix = self._sha(bare, "refs/heads/fix/issue-1")

        wt = WorktreeManager(
            worktrees_dir=tmp_path / "wt", bare_repos_dir=tmp_path / "repos",
        )
        target = wt._get_bare_repo_path("owner/repo")
        target.parent.mkdir(parents=True, exist_ok=True)
        bare.rename(target)
        self._git("remote", "set-url", "origin", str(origin), cwd=target)

        # Must not raise — this is the reported failure.
        await wt.ensure_bare_repo("owner/repo")

        # Bot branch caught up to the rebase...
        assert self._sha(target, "refs/heads/dependabot/uv/pkg-1.0") == \
            self._sha(origin, "refs/heads/dependabot/uv/pkg-1.0")
        # ...and the local commit on fix/ was NOT discarded. This is the
        # protection the non-force refspec exists for; a blanket "+"
        # would silently destroy a prior session's unpushed work.
        assert self._sha(target, "refs/heads/fix/issue-1") == before_fix

    @pytest.mark.asyncio
    async def test_real_errors_still_raise(self, tmp_path: Path) -> None:
        """Failing open would turn an unreachable remote into a silent
        no-op and run pipelines against a stale tree."""
        from ctrlrelay.core.worktree import WorktreeError, WorktreeManager

        wt = WorktreeManager(
            worktrees_dir=tmp_path / "wt", bare_repos_dir=tmp_path / "repos",
        )
        bare = wt._get_bare_repo_path("owner/repo")
        bare.parent.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q", "--bare", str(bare), cwd=tmp_path)
        self._git(
            "remote", "add", "origin",
            str(tmp_path / "does-not-exist"), cwd=bare,
        )

        with pytest.raises(WorktreeError):
            await wt.ensure_bare_repo("owner/repo")


    def _diverged_setup(self, tmp_path: Path, monkeypatch):
        """A managed bare clone whose main diverged from origin's: origin
        rewrote main and the clone holds its own commit on it. Clones
        come from the local origin instead of github.com."""
        import subprocess

        from ctrlrelay.core.worktree import WorktreeManager

        origin, bare = self._build_repos(tmp_path)
        self._git("checkout", "-q", "main", cwd=origin)
        (origin / "f").write_text("rewritten\n")
        self._git("commit", "-qam", "x", cwd=origin)
        self._git("commit", "-q", "--amend", "-m", "force-pushed main",
                  cwd=origin)
        tree = self._git("rev-parse", "refs/heads/main^{tree}",
                         cwd=bare).strip()
        local = self._git("commit-tree", tree, "-p", "refs/heads/main",
                          "-m", "local main", cwd=bare).strip()
        self._git("update-ref", "refs/heads/main", local, cwd=bare)

        wt = WorktreeManager(
            worktrees_dir=tmp_path / "wt", bare_repos_dir=tmp_path / "repos",
        )
        monkeypatch.setattr(wt, "_clone_url", lambda repo: str(origin))
        target = wt._get_bare_repo_path("owner/repo")
        target.parent.mkdir(parents=True, exist_ok=True)
        bare.rename(target)
        subprocess.run(
            ["git", "remote", "set-url", "origin", str(origin)],
            cwd=target, check=True,
        )
        return wt, origin, target, local

    @pytest.mark.asyncio
    async def test_diverged_default_branch_is_set_aside_and_recloned(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """#234: a repo whose history was replaced used to fail every run
        until the clone was moved by hand. Now the clone is moved aside
        with its diverged main intact, and a fresh one matches origin."""
        wt, origin, target, local = self._diverged_setup(tmp_path, monkeypatch)
        told: list[tuple[str, Path]] = []

        async def on_reclone(repo: str, aside: Path) -> None:
            told.append((repo, aside))

        wt.on_reclone = on_reclone

        assert await wt.ensure_bare_repo("owner/repo") == target

        assert self._sha(target, "refs/heads/main") == self._git(
            "rev-parse", "refs/heads/main", cwd=origin).strip()
        asides = sorted(target.parent.glob("owner-repo.git.replaced-*"))
        assert len(asides) == 1
        assert self._sha(asides[0], "refs/heads/main") == local
        assert told == [("owner/repo", asides[0])]

    @pytest.mark.asyncio
    async def test_reclone_is_refused_while_a_worktree_uses_the_clone(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """A BLOCKED session keeps its worktree for the resume. Moving the
        clone would cut it off, so the old loud failure stands."""
        from ctrlrelay.core.worktree import DefaultBranchDivergedError

        wt, _origin, target, local = self._diverged_setup(tmp_path, monkeypatch)
        held = tmp_path / "wt" / "held"
        self._git("worktree", "add", "-q", "-b", "held", str(held), "main",
                  cwd=target)

        with pytest.raises(DefaultBranchDivergedError,
                           match="(?s)default branch diverged.*worktree"):
            await wt.ensure_bare_repo("owner/repo")

        assert self._sha(target, "refs/heads/main") == local
        assert not list(target.parent.glob("owner-repo.git.replaced-*"))

    @pytest.mark.asyncio
    async def test_reclone_is_refused_even_when_the_worktree_dir_is_missing(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """No prune before the check: a worktree whose directory is only
        briefly gone (an unmounted disk) is still a registered worktree."""
        import shutil as _shutil

        from ctrlrelay.core.worktree import DefaultBranchDivergedError

        wt, _origin, target, local = self._diverged_setup(tmp_path, monkeypatch)
        held = tmp_path / "wt" / "held"
        self._git("worktree", "add", "-q", "-b", "held", str(held), "main",
                  cwd=target)
        _shutil.rmtree(held)

        with pytest.raises(DefaultBranchDivergedError, match="(?s)worktree"):
            await wt.ensure_bare_repo("owner/repo")

        assert self._sha(target, "refs/heads/main") == local
        assert str(held) in self._git("worktree", "list", cwd=target)

    @pytest.mark.asyncio
    async def test_a_failed_reclone_puts_the_old_clone_back(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from ctrlrelay.core.worktree import WorktreeError

        wt, _origin, target, local = self._diverged_setup(tmp_path, monkeypatch)
        monkeypatch.setattr(wt, "_clone_url",
                            lambda repo: str(tmp_path / "nowhere"))
        real_clone = wt._clone_bare
        seen: list[bool] = []

        async def clone(repo: str, bare_path: Path) -> None:
            # The old clone has been moved aside by the time this runs.
            seen.append(bare_path.exists())
            await real_clone(repo, bare_path)

        monkeypatch.setattr(wt, "_clone_bare", clone)

        with pytest.raises(WorktreeError, match="nowhere"):
            await wt.ensure_bare_repo("owner/repo")

        assert seen == [False]
        assert self._sha(target, "refs/heads/main") == local
        assert not list(target.parent.glob("owner-repo.git.replaced-*"))

    @pytest.mark.asyncio
    async def test_a_failing_reclone_notice_does_not_fail_the_run(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        wt, _origin, target, _local = self._diverged_setup(tmp_path, monkeypatch)

        async def broken(repo: str, aside: Path) -> None:
            raise RuntimeError("chat is down")

        wt.on_reclone = broken

        assert await wt.ensure_bare_repo("owner/repo") == target


class TestNonFastForwardClassifier:
    """`_rejections_are_only_non_fast_forward` decides whether a
    non-zero `git fetch` is the refspec doing its job or a real
    failure. Getting it wrong in the permissive direction hides
    outages; in the strict direction it re-breaks the sweep."""

    def test_extracts_the_declined_refs(self) -> None:
        from ctrlrelay.core.worktree import (
            _rejections_are_only_non_fast_forward,
        )

        stderr = (
            "From github.com:AInvirion/zzsites\n"
            " - [deleted]         (none)     -> dependabot/uv/authlib-1.6.12\n"
            " ! [rejected]        dependabot/uv/playwright-1.62.0 -> "
            "dependabot/uv/playwright-1.62.0  (non-fast-forward)\n"
            " * [new branch]      feat/import-navigation -> feat/import-navigation\n"
            "   c6cfa84..f0a7911  main                   -> main\n"
        )
        assert _rejections_are_only_non_fast_forward(stderr) == [
            "dependabot/uv/playwright-1.62.0"
        ]

    def test_fatal_is_not_tolerated(self) -> None:
        from ctrlrelay.core.worktree import (
            _rejections_are_only_non_fast_forward,
        )

        stderr = (
            " ! [rejected]  a -> a  (non-fast-forward)\n"
            "fatal: could not read from remote repository\n"
        )
        assert _rejections_are_only_non_fast_forward(stderr) == []

    def test_other_rejection_reasons_are_not_tolerated(self) -> None:
        from ctrlrelay.core.worktree import (
            _rejections_are_only_non_fast_forward,
        )

        stderr = " ! [rejected]  v1 -> v1  (would clobber existing tag)\n"
        assert _rejections_are_only_non_fast_forward(stderr) == []

    def test_no_rejection_at_all_is_not_tolerated(self) -> None:
        """Exit 1 with no rejection line is an unexplained failure."""
        from ctrlrelay.core.worktree import (
            _rejections_are_only_non_fast_forward,
        )

        assert _rejections_are_only_non_fast_forward("") == []
        assert _rejections_are_only_non_fast_forward(
            "From github.com:o/r\n   abc..def  main -> main\n"
        ) == []


class TestFetchWithTheSessionsBranchCheckedOut:
    """#253: a dev run whose pull request went BEHIND mid-run died in its
    own fix path, on ``fatal: refusing to fetch into branch
    'refs/heads/fix/issue-4' checked out at '<the session's worktree>'``.

    Git refuses to fetch into a branch a worktree has checked out
    whenever the remote has a branch of that name, even an unchanged
    one, and refuses the whole fetch with it: not even the default
    branch, which the agent needs to rebase onto, is updated. Once the
    run's branch is published, that is every fetch of the session.
    """

    _git = staticmethod(TestFetchSurvivesRewrittenBranches._git)

    @classmethod
    def _setup(cls, tmp_path: Path):
        import subprocess

        from ctrlrelay.core.worktree import WorktreeManager

        origin = tmp_path / "origin"
        origin.mkdir()
        cls._git("init", "-q", "-b", "main", ".", cwd=origin)
        (origin / "f").write_text("base\n")
        cls._git("add", ".", cwd=origin)
        cls._git("commit", "-qm", "base", cwd=origin)
        cls._git("branch", "fix/issue-4", cwd=origin)

        wt = WorktreeManager(
            worktrees_dir=tmp_path / "wt", bare_repos_dir=tmp_path / "repos",
        )
        bare = wt._get_bare_repo_path("owner/repo")
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(origin), str(bare)],
            check=True, capture_output=True,
        )
        session = tmp_path / "wt" / "session"
        cls._git("worktree", "add", "-q", str(session), "fix/issue-4", cwd=bare)

        # The run's branch exists on origin (here with a commit the
        # clone does not have; an identical one is refused the same) ...
        cls._git("checkout", "-q", "fix/issue-4", cwd=origin)
        (origin / "g").write_text("the run's work\n")
        cls._git("add", ".", cwd=origin)
        cls._git("commit", "-qm", "the run's work", cwd=origin)
        # ... and another pull request merges into main meanwhile.
        cls._git("checkout", "-q", "main", cwd=origin)
        (origin / "f").write_text("main moved\n")
        cls._git("commit", "-qam", "main moved", cwd=origin)
        return wt, origin, bare

    @staticmethod
    def _sha(repo: Path, ref: str) -> str:
        return TestFetchSurvivesRewrittenBranches._sha(repo, ref)

    @pytest.mark.asyncio
    async def test_the_fetch_updates_main_and_leaves_the_checked_out_branch(
        self, tmp_path: Path,
    ) -> None:
        wt, origin, bare = self._setup(tmp_path)
        branch_before = self._sha(bare, "refs/heads/fix/issue-4")

        await wt.ensure_bare_repo("owner/repo")  # raised before #253

        # main is what the agent rebases onto, so it must be current.
        assert self._sha(bare, "refs/heads/main") == self._sha(origin, "refs/heads/main")
        # The session's checked-out branch is not moved under its worktree.
        assert self._sha(bare, "refs/heads/fix/issue-4") == branch_before


    @pytest.mark.asyncio
    async def test_the_default_branch_is_never_left_out(self, tmp_path: Path) -> None:
        """If a worktree ever had the default branch checked out, leaving
        it out would cut every later session from a stale tree. The
        loud refusal stays the answer there."""
        wt, origin, bare = self._setup(tmp_path)
        self._git("worktree", "add", "-q", "--detach", str(tmp_path / "d"), cwd=bare)
        self._git("checkout", "-q", "main", cwd=tmp_path / "d")
        from ctrlrelay.core.worktree import WorktreeError

        with pytest.raises(WorktreeError, match="refusing to fetch into branch 'refs/heads/main'"):
            await wt.ensure_bare_repo("owner/repo")

    @pytest.mark.asyncio
    async def test_a_dev_run_whose_pr_goes_behind_reaches_the_fix_prompt(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """The incident end to end, on real repositories: the run opens
        its pull request, another one merges into main, verification says
        BEHIND, and the fix round must reach the agent with the rebase
        prompt, with the PR branch checked out in the session worktree."""
        import json
        import subprocess
        from unittest.mock import AsyncMock, MagicMock

        from ctrlrelay.core.checkpoint import read_checkpoint
        from ctrlrelay.core.dispatcher import ClaudeDispatcher, SessionResult
        from ctrlrelay.core.github import GitHubCLI
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.core.worktree import WorktreeManager
        from ctrlrelay.pipelines.dev import run_dev_issue

        origin = tmp_path / "origin"
        origin.mkdir()
        self._git("init", "-q", "-b", "main", ".", cwd=origin)
        (origin / "f").write_text("base\n")
        self._git("add", ".", cwd=origin)
        self._git("commit", "-qm", "base", cwd=origin)
        # Pushes land on a non-bare origin's branches, not its HEAD.
        self._git("config", "receive.denyCurrentBranch", "ignore", cwd=origin)

        wt = WorktreeManager(
            worktrees_dir=tmp_path / "wt", bare_repos_dir=tmp_path / "repos",
        )
        monkeypatch.setattr(wt, "_clone_url", lambda repo: str(origin))

        async def push_to_url(repo, branch, **kw):
            # As the relay does: straight to the remote URL. (The agent's
            # commits already moved the clone's branch ref, which the
            # worktree shares; the push itself does not touch it.)
            path = wt._get_bare_repo_path(repo)
            sha = self._git("rev-parse", f"refs/heads/{branch}", cwd=path).strip()
            subprocess.run(
                ["git", "push", "-q", str(origin), f"{sha}:refs/heads/{branch}"],
                cwd=path, check=True, capture_output=True,
            )
            return sha

        monkeypatch.setattr(wt, "push_run_branch", push_to_url)

        github = AsyncMock(spec=GitHubCLI)
        github.get_issue.return_value = {"number": 4, "title": "t", "body": "b", "comments": []}
        github.get_default_branch.return_value = "main"
        github.list_prs.return_value = []
        github.create_pr.return_value = {"number": 46, "url": "https://x/pull/46"}

        prompts: list[str] = []

        async def spawn(**kw):
            prompts.append(kw["prompt"])
            work = Path(kw["working_dir"])
            (work / f"change{len(prompts)}").write_text("x\n")
            self._git("add", ".", cwd=work)
            self._git("commit", "-qm", f"agent {len(prompts)}", cwd=work)
            state_file = kw["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                "version": "1", "status": "DONE", "session_id": kw["session_id"],
                "timestamp": "2026-10-31T23:45:00Z", "summary": "done", "outputs": {},
            }))
            return SessionResult(
                session_id=kw["session_id"], exit_code=0, stdout="", stderr="",
                state=read_checkpoint(state_file),
            )

        dispatcher = AsyncMock(spec=ClaudeDispatcher)
        dispatcher.spawn_session.side_effect = spawn

        verdicts = iter([
            VerificationResult(ready=False, mergeable="MERGEABLE", merge_state_status="BEHIND"),
            VerificationResult(ready=True),
        ])

        async def verify(*a, **kw):
            verdict = next(verdicts)
            if verdict.merge_state_status == "BEHIND":
                # Another pull request merged while this run worked.
                self._git("checkout", "-q", "main", cwd=origin)
                (origin / "f").write_text("main moved\n")
                self._git("commit", "-qam", "main moved", cwd=origin)
            return verdict

        verifier = MagicMock()
        verifier.verify = AsyncMock(side_effect=verify)
        state_db = StateDB(tmp_path / "state.db")
        try:
            result = await run_dev_issue(
                claims=None,
                repo="owner/repo", issue_number=4, branch_template="fix/issue-{n}",
                dispatcher=dispatcher, github=github, worktree=wt, dashboard=None,
                state_db=state_db, transport=None, contexts_dir=tmp_path / "ctx",
                pr_verifier=verifier, code_review=MagicMock(method="off"),
            )
        finally:
            state_db.close()

        assert len(prompts) == 2, result.error
        assert "BEHIND" in prompts[1]
        assert result.success, result.error

    @pytest.mark.asyncio
    async def test_prune_keeps_a_checked_out_branch_origin_deleted(
        self, tmp_path: Path,
    ) -> None:
        """A branch deleted on GitHub while a session still has it checked
        out is kept (the worktree is using it); others are still pruned."""
        wt, origin, bare = self._setup(tmp_path)
        self._git("branch", "other", cwd=origin)
        await wt.ensure_bare_repo("owner/repo")
        assert "other" in self._git("branch", "--list", "other", cwd=bare)
        self._git("branch", "-D", "fix/issue-4", "other", cwd=origin)

        await wt.ensure_bare_repo("owner/repo")

        assert self._git("branch", "--list", "fix/issue-4", cwd=bare).strip()
        assert not self._git("branch", "--list", "other", cwd=bare).strip()

    @pytest.mark.asyncio
    async def test_an_unchanged_published_branch_was_refused_too(
        self, tmp_path: Path,
    ) -> None:
        """The usual case: the branch on origin is exactly the session's.
        Git refused that as well, so every fetch after publishing failed."""
        import subprocess

        from ctrlrelay.core.worktree import WorktreeManager

        origin = tmp_path / "origin"
        origin.mkdir()
        self._git("init", "-q", "-b", "main", ".", cwd=origin)
        (origin / "f").write_text("base\n")
        self._git("add", ".", cwd=origin)
        self._git("commit", "-qm", "base", cwd=origin)
        self._git("branch", "fix/issue-4", cwd=origin)
        wt = WorktreeManager(worktrees_dir=tmp_path / "wt", bare_repos_dir=tmp_path / "repos")
        bare = wt._get_bare_repo_path("owner/repo")
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(origin), str(bare)],
                       check=True, capture_output=True)
        self._git("worktree", "add", "-q", str(tmp_path / "s"), "fix/issue-4", cwd=bare)
        branch = "refs/heads/fix/issue-4"
        assert self._sha(bare, branch) == self._sha(origin, branch)
        # Plain git refuses this fetch although nothing would change ...
        refused = subprocess.run(
            ["git", "fetch", "origin", "refs/heads/*:refs/heads/*"],
            cwd=bare, capture_output=True, text=True,
        )
        assert refused.returncode != 0
        assert "refusing to fetch into branch" in refused.stderr

        # ... and the relay's fetch does not.
        await wt.ensure_bare_repo("owner/repo")

    @pytest.mark.asyncio
    async def test_with_no_default_branch_known_nothing_is_left_out(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from unittest.mock import AsyncMock

        from ctrlrelay.core.worktree import WorktreeError

        wt, origin, bare = self._setup(tmp_path)
        monkeypatch.setattr(
            wt, "get_default_branch", AsyncMock(side_effect=WorktreeError("no HEAD")),
        )
        with pytest.raises(WorktreeError, match="refusing to fetch"):
            await wt.ensure_bare_repo("owner/repo")


class TestAFailedGitCommandNamesTheReason:
    """#269: `_run_git` raised with stderr only, and the push runs with
    `--porcelain`, which puts the per-ref rejection reason on stdout.
    So every failed push was reported as

        git failed: error: failed to push some refs to '<url>'

    — git's summary line, and nothing else. The line naming the ref and
    why the remote refused it was produced, captured and discarded in
    the same call. Worse than terse: the one line that survived names
    the URL, which sends the reader after the transport or the
    credential when neither was at fault.
    """

    # A git command that writes to BOTH streams and exits non-zero. An
    # alias is the only portable way to get stdout out of a failing
    # git; what matters to the test is the two streams and the exit
    # code, not which subcommand produced them.
    _ALIAS = (
        '!printf "on-stdout\\n"; printf "on-stderr\\n" >&2; exit 3'
    )

    @staticmethod
    def _manager(tmp_path: Path):
        from ctrlrelay.core.worktree import WorktreeManager

        return WorktreeManager(
            worktrees_dir=tmp_path / "wt", bare_repos_dir=tmp_path / "repos",
        )

    @pytest.mark.asyncio
    async def test_both_streams_reach_the_message_and_the_exception(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.worktree import GitCommandError

        with pytest.raises(GitCommandError) as caught:
            await self._manager(tmp_path)._run_git(
                "-c", f"alias.boom={self._ALIAS}", "boom", cwd=tmp_path,
            )
        message = str(caught.value)
        assert "on-stderr" in message, "stderr dropped"
        assert "on-stdout" in message, "stdout dropped — the #269 defect"
        # stderr leads: it holds git's own fatal:/error: lines and is
        # what existing callers match on.
        assert message.index("on-stderr") < message.index("on-stdout")
        assert caught.value.stdout == "on-stdout"
        assert caught.value.stderr == "on-stderr"

    @pytest.mark.asyncio
    async def test_it_is_still_a_worktree_error(self, tmp_path: Path) -> None:
        """Every existing handler filters on WorktreeError."""
        from ctrlrelay.core.worktree import WorktreeError

        with pytest.raises(WorktreeError):
            await self._manager(tmp_path)._run_git(
                "-c", f"alias.boom={self._ALIAS}", "boom", cwd=tmp_path,
            )

    @pytest.mark.asyncio
    async def test_a_silent_failure_names_its_exit_code(
        self, tmp_path: Path
    ) -> None:
        """With nothing on either stream the exit code is all there is,
        so it is named rather than rendered as `git failed: `."""
        from ctrlrelay.core.worktree import GitCommandError

        with pytest.raises(GitCommandError) as caught:
            await self._manager(tmp_path)._run_git(
                "-c", "alias.quiet=!exit 7", "quiet", cwd=tmp_path,
            )
        assert "exited 7" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_credential_in_the_output_is_still_redacted(
        self, tmp_path: Path
    ) -> None:
        """Keeping stdout must not start writing tokens to the log."""
        from ctrlrelay.core.worktree import GitCommandError

        alias = (
            '!printf "Authorization: Bearer ghp_'
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n"; exit 1'
        )
        with pytest.raises(GitCommandError) as caught:
            await self._manager(tmp_path)._run_git(
                "-c", f"alias.leak={alias}", "leak", cwd=tmp_path,
            )
        assert "ghp_" not in str(caught.value)
        assert "[redacted]" in str(caught.value)


class TestParsePushRejection:
    """The `!` line of `git push --porcelain` is the only place the
    remote's reason exists: stderr gets `error: failed to push some
    refs to '<url>'` and nothing more.
    """

    # Real `git push --porcelain` output, tabs and all.
    _STALE = (
        "To https://github.com/owner/repo.git\n"
        "!\trefs/heads/fix/issue-7:refs/heads/fix/issue-7\t"
        "[rejected] (stale info)\n"
        "Done\n"
    )
    _HOOK = (
        "To https://github.com/owner/repo.git\n"
        "!\tfix/issue-7:refs/heads/fix/issue-7\t"
        "[remote rejected] (push declined due to repository rule "
        "violations)\n"
        "Done\n"
    )
    _OK = (
        "To https://github.com/owner/repo.git\n"
        "\trefs/heads/fix/issue-7:refs/heads/fix/issue-7\tabc123..def456\n"
        "Done\n"
    )

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (_STALE, "refs/heads/fix/issue-7:refs/heads/fix/issue-7 "
                     "[rejected] (stale info)"),
            (_HOOK, "fix/issue-7:refs/heads/fix/issue-7 [remote rejected] "
                    "(push declined due to repository rule violations)"),
            # The non-porcelain form carries the flag and reason too.
            (" ! [rejected]        fix/issue-7 -> fix/issue-7 "
             "(non-fast-forward)",
             "[rejected]        fix/issue-7 -> fix/issue-7 "
             "(non-fast-forward)"),
        ],
        ids=["stale-lease", "remote-rule", "non-porcelain"],
    )
    def test_the_reason_is_read_out(self, text: str, expected: str) -> None:
        from ctrlrelay.core.worktree import parse_push_rejection

        assert parse_push_rejection(text) == expected

    @pytest.mark.parametrize(
        "text", [_OK, "", "fatal: could not read Username for 'https://x'"],
        ids=["every-ref-updated", "empty", "died-before-any-ref"],
    )
    def test_no_rejected_ref_is_none_not_a_guess(self, text: str) -> None:
        """A push that died before it got a per-ref answer is a
        different failure; stderr is the whole story and must not be
        dressed up as a rejection."""
        from ctrlrelay.core.worktree import parse_push_rejection

        assert parse_push_rejection(text) is None

    def test_a_long_reason_is_capped(self) -> None:
        from ctrlrelay.core.worktree import (
            _PUSH_REASON_MAX_CHARS,
            parse_push_rejection,
        )

        text = "!\tr:r\t[remote rejected] (" + "x" * 5000 + ")"
        reason = parse_push_rejection(text)
        assert reason is not None
        assert len(reason) == _PUSH_REASON_MAX_CHARS
