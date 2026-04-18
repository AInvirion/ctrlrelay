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
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "status": "completed", "conclusion": "success"},
        ]
        mock_github.get_pr_state.return_value = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
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
    async def test_request_fix_resumes_session_with_instructions(self, tmp_path: Path) -> None:
        """request_fix should resume the session using the provided fix prompt."""
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
                summary="Fixed CI",
                outputs={"pr_url": "https://github.com/o/r/pull/42", "pr_number": 42},
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

        fix_prompt = "PR #42 has failing checks. Please investigate and fix."
        result = await pipeline.request_fix(ctx, fix_prompt)

        assert result.success
        call_kwargs = mock_dispatcher.spawn_session.call_args.kwargs
        assert call_kwargs["resume_session_id"] == "dev-123"
        assert call_kwargs["prompt"] == fix_prompt

    @pytest.mark.asyncio
    async def test_run_dev_issue_posts_claim_comment(self, tmp_path: Path) -> None:
        """Should post a claim comment on the issue when work begins."""
        from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus
        from dev_sync.core.dispatcher import SessionResult
        from dev_sync.core.pr_verifier import VerificationResult
        from dev_sync.core.state import StateDB
        from dev_sync.pipelines.dev import AGENT_CLAIM_MARKER, run_dev_issue

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
            "comments": [],
        }

        state_db = StateDB(tmp_path / "state.db")

        mock_pr_verifier = AsyncMock()
        mock_pr_verifier.verify.return_value = VerificationResult(ready=True)

        await run_dev_issue(
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
            pr_verifier=mock_pr_verifier,
        )

        mock_github.comment_on_issue.assert_called_once()
        call_args = mock_github.comment_on_issue.call_args
        assert call_args.kwargs.get("repo") == "owner/repo" or call_args.args[0] == "owner/repo"
        assert (
            call_args.kwargs.get("issue_number") == 123
            or 123 in call_args.args
        )
        body = call_args.kwargs.get("body") or call_args.args[-1]
        assert AGENT_CLAIM_MARKER in body
        assert "working on" in body.lower() or "checking" in body.lower()

    @pytest.mark.asyncio
    async def test_run_dev_issue_skips_claim_comment_if_already_posted(
        self, tmp_path: Path
    ) -> None:
        """Should not post a duplicate claim comment if marker is already present."""
        from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus
        from dev_sync.core.dispatcher import SessionResult
        from dev_sync.core.pr_verifier import VerificationResult
        from dev_sync.core.state import StateDB
        from dev_sync.pipelines.dev import AGENT_CLAIM_MARKER, run_dev_issue

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
            "comments": [
                {
                    "body": (
                        f"Agent is already on it\n\n{AGENT_CLAIM_MARKER}"
                    ),
                    "author": {"login": "alice"},
                }
            ],
        }

        state_db = StateDB(tmp_path / "state.db")

        mock_pr_verifier = AsyncMock()
        mock_pr_verifier.verify.return_value = VerificationResult(ready=True)

        await run_dev_issue(
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
            pr_verifier=mock_pr_verifier,
        )

        mock_github.comment_on_issue.assert_not_called()

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


class TestRunDevIssueVerification:
    """Verifies run_dev_issue waits for CI and checks mergeability before DONE."""

    @staticmethod
    def _make_done_state(session_id: str = "dev-123", pr_number: int = 42):
        from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus
        from dev_sync.core.dispatcher import SessionResult

        return SessionResult(
            session_id=session_id,
            exit_code=0,
            stdout="",
            stderr="",
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.DONE,
                session_id=session_id,
                timestamp="2026-04-17T12:00:00Z",
                summary=f"PR #{pr_number} opened",
                outputs={
                    "pr_url": f"https://github.com/o/r/pull/{pr_number}",
                    "pr_number": pr_number,
                },
            ),
        )

    @pytest.mark.asyncio
    async def test_run_dev_issue_verifies_before_returning_success(self, tmp_path: Path) -> None:
        """Should call get_pr_checks and get_pr_state before returning success."""
        from dev_sync.core.state import StateDB
        from dev_sync.pipelines.dev import run_dev_issue

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = self._make_done_state(pr_number=42)

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = tmp_path / "worktree"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {"number": 1, "title": "x", "body": "y"}
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "status": "completed", "conclusion": "success"},
        ]
        mock_github.get_pr_state.return_value = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }

        state_db = StateDB(tmp_path / "state.db")

        result = await run_dev_issue(
            repo="owner/repo",
            issue_number=1,
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
        mock_github.get_pr_checks.assert_called()
        mock_github.get_pr_state.assert_called()

    @pytest.mark.asyncio
    async def test_run_dev_issue_requests_fix_when_ci_fails(self, tmp_path: Path) -> None:
        """On failing CI, should resume the session with a fix prompt and re-verify."""
        from dev_sync.core.state import StateDB
        from dev_sync.pipelines.dev import run_dev_issue

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = self._make_done_state(pr_number=42)

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = tmp_path / "worktree"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {"number": 1, "title": "x", "body": "y"}
        # First verification: CI fails. Second: CI passes.
        mock_github.get_pr_checks.side_effect = [
            [{"name": "ci", "status": "completed", "conclusion": "failure"}],
            [{"name": "ci", "status": "completed", "conclusion": "success"}],
        ]
        mock_github.get_pr_state.return_value = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }

        state_db = StateDB(tmp_path / "state.db")

        result = await run_dev_issue(
            repo="owner/repo",
            issue_number=1,
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
        # Claude invoked twice: initial + fix request
        assert mock_dispatcher.spawn_session.call_count == 2
        fix_call = mock_dispatcher.spawn_session.call_args_list[1].kwargs
        assert fix_call["resume_session_id"] == fix_call["session_id"]
        assert "ci" in fix_call["prompt"].lower() or "check" in fix_call["prompt"].lower()

    @pytest.mark.asyncio
    async def test_run_dev_issue_requests_fix_when_conflicting(self, tmp_path: Path) -> None:
        """On CONFLICTING mergeable state, should resume the session with a conflict prompt."""
        from dev_sync.core.state import StateDB
        from dev_sync.pipelines.dev import run_dev_issue

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = self._make_done_state(pr_number=42)

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = tmp_path / "worktree"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {"number": 1, "title": "x", "body": "y"}
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "status": "completed", "conclusion": "success"},
        ]
        mock_github.get_pr_state.side_effect = [
            {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"},
            {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"},
        ]

        state_db = StateDB(tmp_path / "state.db")

        result = await run_dev_issue(
            repo="owner/repo",
            issue_number=1,
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
        assert mock_dispatcher.spawn_session.call_count == 2
        fix_call = mock_dispatcher.spawn_session.call_args_list[1].kwargs
        assert "conflict" in fix_call["prompt"].lower()

    @pytest.mark.asyncio
    async def test_run_dev_issue_fails_after_max_fix_attempts(self, tmp_path: Path) -> None:
        """Should give up and return failure after max fix attempts."""
        from dev_sync.core.state import StateDB
        from dev_sync.pipelines.dev import run_dev_issue

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = self._make_done_state(pr_number=42)

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = tmp_path / "worktree"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {"number": 1, "title": "x", "body": "y"}
        # Always failing
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "status": "completed", "conclusion": "failure"},
        ]
        mock_github.get_pr_state.return_value = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
        }

        state_db = StateDB(tmp_path / "state.db")

        result = await run_dev_issue(
            repo="owner/repo",
            issue_number=1,
            branch_template="fix/issue-{n}",
            dispatcher=mock_dispatcher,
            github=mock_github,
            worktree=mock_worktree,
            dashboard=None,
            state_db=state_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
            max_fix_attempts=2,
        )

        assert not result.success
        # 1 initial + 2 retries = 3 spawns
        assert mock_dispatcher.spawn_session.call_count == 3
        assert result.error is not None
        assert result.outputs.get("pr_number") == 42
