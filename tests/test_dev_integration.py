"""Integration test for dev pipeline."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


class TestDevIntegration:
    @pytest.mark.asyncio
    async def test_full_dev_flow_with_mocked_claude(self, tmp_path: Path) -> None:
        """Should run full dev flow from issue to PR."""
        from ctrlrelay.core.checkpoint import read_checkpoint
        from ctrlrelay.core.dispatcher import ClaudeDispatcher, SessionResult
        from ctrlrelay.core.github import GitHubCLI
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.core.worktree import WorktreeManager
        from ctrlrelay.pipelines.dev import run_dev_issue

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

        from ctrlrelay.core.pr_verifier import VerificationResult
        mock_pr_verifier = AsyncMock()
        mock_pr_verifier.verify.return_value = VerificationResult(ready=True)

        # Mock worktree methods
        with (
            patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock),
            patch.object(
                worktree, "create_worktree_with_new_branch", new_callable=AsyncMock
            ) as mock_create,
            patch.object(worktree, "remove_worktree", new_callable=AsyncMock),
            patch.object(worktree, "symlink_context"),
            patch.object(worktree, "remove_context_symlink"),
        ):

            worktree_path = tmp_path / "worktrees" / "test-worktree"
            worktree_path.mkdir(parents=True)
            mock_create.return_value = (worktree_path, True)

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
                pr_verifier=mock_pr_verifier,
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


class TestDevPipelineCleanup:
    """Regression tests for #17: worktree + branch cleanup on failure paths."""

    async def _run(
        self,
        tmp_path: Path,
        spawn_side_effect,
        *,
        branch_on_remote: bool = False,
        create_side_effect=None,
        created_fresh: bool = True,
        branch_existed_before: bool | None = None,
    ):
        """Run the dev pipeline with a stub dispatcher behavior, returning
        (result, mocks) so tests can assert on cleanup calls.

        - `branch_on_remote`: what branch_exists_on_remote() returns. False
          means the branch is local-only (safe to delete). True means origin
          has it (must preserve).
        - `create_side_effect`: if set, overrides create_worktree_with_new_branch
          to simulate setup failure before the branch is created.
        - `created_fresh`: the ownership flag returned from
          create_worktree_with_new_branch (issue #51). True when this
          session created the branch (fresh from default or via
          delete+recreate of a stale merged local branch); False when
          we reused an existing branch.
        - `branch_existed_before`: value returned by the pre-call
          ``branch_exists_locally`` snapshot. When None (default),
          derives from ``not created_fresh`` to match the typical
          pairing: fresh creations imply "didn't exist before", reuses
          imply "did". Override explicitly when exercising
          partial-create cleanup (branch_existed_before=False while
          create_worktree raises).
        """
        from ctrlrelay.core.dispatcher import ClaudeDispatcher
        from ctrlrelay.core.github import GitHubCLI
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.core.worktree import WorktreeManager
        from ctrlrelay.pipelines.dev import run_dev_issue

        state_db = StateDB(tmp_path / "state.db")

        worktree = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )

        mock_github = AsyncMock(spec=GitHubCLI)
        mock_github.get_issue.return_value = {
            "number": 13,
            "title": "Remove roadmap section from README",
            "body": "Outdated.",
        }

        mock_dispatcher = AsyncMock(spec=ClaudeDispatcher)
        mock_dispatcher.spawn_session.side_effect = spawn_side_effect

        with (
            patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock),
            patch.object(
                worktree, "create_worktree_with_new_branch", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                worktree, "remove_worktree", new_callable=AsyncMock
            ) as mock_remove_wt,
            patch.object(
                worktree, "delete_branch", new_callable=AsyncMock
            ) as mock_delete_branch,
            patch.object(
                worktree, "branch_exists_on_remote", new_callable=AsyncMock
            ) as mock_remote,
            patch.object(
                worktree, "branch_exists_locally", new_callable=AsyncMock
            ) as mock_local,
            patch.object(worktree, "symlink_context"),
            patch.object(worktree, "remove_context_symlink"),
        ):

            mock_remote.return_value = branch_on_remote
            # branch_exists_locally is used by run_dev_issue only for the
            # pre-call snapshot that backs the exception-path cleanup
            # fallback (see run_dev_issue.branch_existed_before). The
            # normal FAILED path keys off the returned ``created_fresh``
            # from create_worktree_with_new_branch (issue #51).
            effective_existed_before = (
                (not created_fresh)
                if branch_existed_before is None
                else branch_existed_before
            )
            mock_local.return_value = effective_existed_before

            if create_side_effect is not None:
                mock_create.side_effect = create_side_effect
            else:
                worktree_path = tmp_path / "worktrees" / "wt-13"
                worktree_path.mkdir(parents=True)
                mock_create.return_value = (worktree_path, created_fresh)

            from ctrlrelay.core.pr_verifier import VerificationResult
            mock_pr_verifier = AsyncMock()
            mock_pr_verifier.verify.return_value = VerificationResult(ready=True)

            result = await run_dev_issue(
                repo="owner/repo",
                issue_number=13,
                branch_template="fix/issue-{n}",
                dispatcher=mock_dispatcher,
                github=mock_github,
                worktree=worktree,
                dashboard=None,
                state_db=state_db,
                transport=None,
                contexts_dir=tmp_path / "contexts",
                pr_verifier=mock_pr_verifier,
            )

        state_db.close()
        return result, mock_remove_wt, mock_delete_branch

    @pytest.mark.asyncio
    async def test_dispatcher_exception_cleans_up_worktree_and_branch(
        self, tmp_path: Path
    ) -> None:
        """Regression for #17: if dispatcher raises (e.g. claude binary missing),
        the leftover worktree and fix/issue-<n> branch must be cleaned up so the
        next retry can re-create them."""

        async def spawn(**kwargs):
            raise FileNotFoundError(2, "No such file or directory", "claude")

        result, mock_remove_wt, mock_delete_branch = await self._run(tmp_path, spawn)

        assert not result.success
        assert not result.blocked
        mock_remove_wt.assert_awaited_once()
        mock_delete_branch.assert_awaited_once()
        assert mock_delete_branch.await_args.args[1] == "fix/issue-13"

    @pytest.mark.asyncio
    async def test_failed_checkpoint_cleans_up_worktree_and_branch(
        self, tmp_path: Path
    ) -> None:
        """A FAILED checkpoint (claude exited cleanly but flagged failure) must
        trigger the same cleanup as an exception — retry must not be blocked by
        a leftover branch."""
        from ctrlrelay.core.checkpoint import read_checkpoint
        from ctrlrelay.core.dispatcher import SessionResult

        async def spawn(**kwargs):
            state_file = kwargs["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                "version": "1",
                "status": "FAILED",
                "session_id": kwargs["session_id"],
                "timestamp": "2026-04-17T12:00:00Z",
                "error": "something broke",
            }))
            return SessionResult(
                session_id=kwargs["session_id"],
                exit_code=1,
                stdout="",
                stderr="",
                state=read_checkpoint(state_file),
            )

        result, mock_remove_wt, mock_delete_branch = await self._run(tmp_path, spawn)

        assert not result.success
        assert not result.blocked
        mock_remove_wt.assert_awaited_once()
        mock_delete_branch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_blocked_preserves_worktree_and_branch(
        self, tmp_path: Path
    ) -> None:
        """BLOCKED must NOT clean up — the user may resume the session."""
        from ctrlrelay.core.checkpoint import read_checkpoint
        from ctrlrelay.core.dispatcher import SessionResult

        async def spawn(**kwargs):
            state_file = kwargs["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                "version": "1",
                "status": "BLOCKED_NEEDS_INPUT",
                "session_id": kwargs["session_id"],
                "timestamp": "2026-04-17T12:00:00Z",
                "question": "pin or bump?",
            }))
            return SessionResult(
                session_id=kwargs["session_id"],
                exit_code=0,
                stdout="",
                stderr="",
                state=read_checkpoint(state_file),
            )

        result, mock_remove_wt, mock_delete_branch = await self._run(tmp_path, spawn)

        assert result.blocked
        mock_remove_wt.assert_not_awaited()
        mock_delete_branch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocked_exit_persists_to_pending_resumes(
        self, tmp_path: Path
    ) -> None:
        """BLOCKED exit must also write a pending_resumes row so a
        Telegram reply arriving after the session exited can still
        route to a resume via the pending_resume_sweeper."""
        from ctrlrelay.core.checkpoint import read_checkpoint
        from ctrlrelay.core.dispatcher import ClaudeDispatcher, SessionResult
        from ctrlrelay.core.github import GitHubCLI
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.core.worktree import WorktreeManager
        from ctrlrelay.pipelines.dev import run_dev_issue

        async def spawn(**kwargs):
            state_file = kwargs["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                "version": "1",
                "status": "BLOCKED_NEEDS_INPUT",
                "session_id": kwargs["session_id"],
                "timestamp": "2026-04-17T12:00:00Z",
                "question": "pin or bump typescript?",
            }))
            return SessionResult(
                session_id=kwargs["session_id"],
                exit_code=0,
                stdout="",
                stderr="",
                state=read_checkpoint(state_file),
            )

        state_db = StateDB(tmp_path / "state.db")
        worktree = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        mock_github = AsyncMock(spec=GitHubCLI)
        mock_github.get_issue.return_value = {
            "number": 42, "title": "t", "body": "b",
        }
        mock_dispatcher = AsyncMock(spec=ClaudeDispatcher)
        mock_dispatcher.spawn_session.side_effect = spawn

        with (
            patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock),
            patch.object(
                worktree, "create_worktree_with_new_branch",
                new_callable=AsyncMock,
            ) as mock_create,
            patch.object(worktree, "remove_worktree", new_callable=AsyncMock),
            patch.object(worktree, "delete_branch", new_callable=AsyncMock),
            patch.object(
                worktree, "branch_exists_on_remote", new_callable=AsyncMock
            ) as mock_remote,
            patch.object(
                worktree, "branch_exists_locally", new_callable=AsyncMock
            ) as mock_local,
            patch.object(worktree, "symlink_context"),
            patch.object(worktree, "remove_context_symlink"),
        ):
            mock_remote.return_value = False
            mock_local.return_value = False
            worktree_path = tmp_path / "worktrees" / "wt-42"
            worktree_path.mkdir(parents=True)
            mock_create.return_value = (worktree_path, True)

            result = await run_dev_issue(
                repo="owner/repo",
                issue_number=42,
                branch_template="fix/issue-{n}",
                dispatcher=mock_dispatcher,
                github=mock_github,
                worktree=worktree,
                dashboard=None,
                state_db=state_db,
                transport=None,  # no in-process BLOCKED loop
                contexts_dir=tmp_path / "contexts",
            )

        assert result.blocked
        # The new behavior: pending_resumes has a dev row for this session.
        unanswered = state_db.list_unanswered_pending_resumes()
        assert len(unanswered) == 1
        assert unanswered[0]["session_id"] == result.session_id
        assert unanswered[0]["pipeline"] == "dev"
        assert unanswered[0]["repo"] == "owner/repo"
        assert "pin or bump" in unanswered[0]["question"]
        state_db.close()

    @pytest.mark.asyncio
    async def test_resume_dev_from_pending_missing_session_row_errors(
        self, tmp_path: Path
    ) -> None:
        """Defensive: if the pending_resumes row points at a session_id
        with no matching sessions row (DB tampering, manual cleanup),
        the resume helper must fail cleanly, not crash. Sweeper then
        marks it resumed so it doesn't hot-loop."""
        from unittest.mock import AsyncMock, MagicMock

        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import resume_dev_from_pending

        state_db = StateDB(tmp_path / "state.db")

        result = await resume_dev_from_pending(
            session_id="dev-owner-repo-99-deadbeef",
            repo="owner/repo",
            answer="merge it",
            branch_template="fix/issue-{n}",
            dispatcher=AsyncMock(),
            github=AsyncMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=state_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )
        assert not result.success
        assert result.error == "session_row_missing"
        state_db.close()

    @pytest.mark.asyncio
    async def test_pushed_branch_is_preserved_on_failure(
        self, tmp_path: Path
    ) -> None:
        """Codex P1: if Claude pushed the branch to origin before failing,
        cleanup must NOT delete the local branch — the user's commits are on
        origin and deleting local state would orphan them from their clone."""

        async def spawn(**kwargs):
            raise RuntimeError("claude died after pushing")

        result, mock_remove_wt, mock_delete_branch = await self._run(
            tmp_path, spawn, branch_on_remote=True
        )

        assert not result.success
        # worktree can still be removed (branch ref persists locally anyway)
        mock_remove_wt.assert_awaited_once()
        # but the branch must be preserved
        mock_delete_branch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_setup_failure_does_not_delete_preexisting_branch(
        self, tmp_path: Path
    ) -> None:
        """Codex P2: if the branch already existed before this run (from a
        prior DONE session with an open PR) and worktree creation fails,
        cleanup must NOT call delete_branch — this session never owned it."""

        async def create_boom(**kwargs):
            raise RuntimeError("branch already exists (from a prior DONE run)")

        async def spawn(**kwargs):
            raise AssertionError("spawn should not be reached")

        result, mock_remove_wt, mock_delete_branch = await self._run(
            tmp_path,
            spawn,
            create_side_effect=create_boom,
            # Branch existed before the call; create_worktree raised
            # before returning, so no ``created_fresh`` signal is
            # available. The helper's default (False) combined with
            # branch_existed_before=True means cleanup must NOT delete.
            created_fresh=False,
        )

        assert not result.success
        # remove_worktree is called unconditionally to prune any stale metadata
        # — that's fine, it's keyed by session_id so it only touches our own.
        mock_remove_wt.assert_awaited_once()
        # but we must never touch the pre-existing branch
        mock_delete_branch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_worktree_create_still_cleans_up_branch(
        self, tmp_path: Path
    ) -> None:
        """Codex follow-up P2: `git worktree add -b` can create the branch
        ref before the worktree directory setup fails. In that case we
        *did* introduce the branch even though create_worktree_with_new_branch
        raised — cleanup must delete it so the next retry isn't blocked."""

        async def create_partial_fail(**kwargs):
            # Simulate: ref got created, but the worktree dir step crashed.
            raise RuntimeError(
                "worktree add failed after branch ref was created "
                "(disk full / permission denied on dir)"
            )

        async def spawn(**kwargs):
            raise AssertionError("spawn should not be reached")

        result, mock_remove_wt, mock_delete_branch = await self._run(
            tmp_path,
            spawn,
            create_side_effect=create_partial_fail,
            # This run introduced the ref, but create_worktree raised
            # before returning ``created_fresh=True``. Force
            # branch_existed_before=False so the exception-path
            # fallback correctly treats the leftover ref as ours.
            created_fresh=False,
            branch_existed_before=False,
        )

        assert not result.success
        # remove_worktree is called unconditionally so a partially-registered
        # worktree (metadata only, no dir) still gets pruned. Without this,
        # git would refuse to delete the branch that's still "checked out".
        mock_remove_wt.assert_awaited_once()
        # we owned the branch, so it must be deleted
        mock_delete_branch.assert_awaited_once()
        assert mock_delete_branch.await_args.args[1] == "fix/issue-13"

    @pytest.mark.asyncio
    async def test_success_preserves_branch(self, tmp_path: Path) -> None:
        """DONE removes the worktree but keeps the branch — the open PR
        references it, so deleting would break the PR."""
        from ctrlrelay.core.checkpoint import read_checkpoint
        from ctrlrelay.core.dispatcher import SessionResult

        async def spawn(**kwargs):
            state_file = kwargs["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                "version": "1",
                "status": "DONE",
                "session_id": kwargs["session_id"],
                "timestamp": "2026-04-17T12:00:00Z",
                "summary": "PR opened",
                "outputs": {"pr_url": "https://x/pr/1", "pr_number": 1},
            }))
            return SessionResult(
                session_id=kwargs["session_id"],
                exit_code=0,
                stdout="",
                stderr="",
                state=read_checkpoint(state_file),
            )

        result, mock_remove_wt, mock_delete_branch = await self._run(tmp_path, spawn)

        assert result.success
        mock_remove_wt.assert_awaited_once()
        mock_delete_branch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_with_created_fresh_deletes_branch_even_if_preexisting_locally(
        self, tmp_path: Path
    ) -> None:
        """Issue #51 regression: the branch ref may have existed locally
        before the call (leftover from a prior merged PR whose remote
        was auto-deleted), but create_worktree_with_new_branch detected
        it as stale-merged, deleted it, and recreated from default. The
        ref now on disk was created by THIS session, so a FAILED
        cleanup MUST delete it — even though a pre-call
        ``branch_exists_locally`` snapshot would have said "True".

        Before #51 fix: run_dev_issue snapshotted
        ``branch_preexisted = branch_exists_locally()`` BEFORE the call
        and keyed cleanup on ``not branch_preexisted``. Cleanup skipped
        delete_branch, partial commits from this session leaked into
        the next retry's reuse path and got classified as
        "local-only with unique commits"."""
        from ctrlrelay.core.checkpoint import read_checkpoint
        from ctrlrelay.core.dispatcher import SessionResult

        async def spawn(**kwargs):
            state_file = kwargs["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                "version": "1",
                "status": "FAILED",
                "session_id": kwargs["session_id"],
                "timestamp": "2026-04-17T12:00:00Z",
                "error": "claude gave up mid-retry",
            }))
            return SessionResult(
                session_id=kwargs["session_id"],
                exit_code=1,
                stdout="",
                stderr="",
                state=read_checkpoint(state_file),
            )

        # The scenario: branch_exists_locally says True (stale merged
        # ref from the previous session), but the helper detected it as
        # fully-merged, deleted the ref, and recreated fresh → returns
        # created_fresh=True. Cleanup must key on created_fresh, not
        # on the stale pre-call snapshot. Force both flags so the OR
        # combining them exercises the #51 semantic: with the old
        # ``not branch_preexisted`` cleanup gate, this scenario would
        # incorrectly skip delete_branch.
        result, mock_remove_wt, mock_delete_branch = await self._run(
            tmp_path,
            spawn,
            branch_on_remote=False,
            created_fresh=True,
            branch_existed_before=True,
        )

        assert not result.success
        assert not result.blocked
        mock_remove_wt.assert_awaited_once()
        # The key assertion: delete_branch ran even though the branch
        # "existed before" the call. created_fresh wins.
        mock_delete_branch.assert_awaited_once()
        assert mock_delete_branch.await_args.args[1] == "fix/issue-13"
