"""Tests for dev pipeline."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestDevPipeline:
    @pytest.mark.asyncio
    async def test_dev_pipeline_has_name(self) -> None:
        """Pipeline should have name 'dev'."""
        from ctrlrelay.pipelines.dev import DevPipeline

        pipeline = DevPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        assert pipeline.name == "dev"

    def test_prompt_directs_claude_to_ci_wait_helper(self) -> None:
        """Prompt must tell Claude to call `ctrlrelay ci wait` (or equivalent
        approved command) rather than improvising a bash `until`/`while` loop.
        Issue #85: hand-written loops were inverted-semantics and ate exit
        codes, burning 30-min timeouts on PRs that had already gone green.
        """
        from ctrlrelay.pipelines.dev import DevPipeline

        pipeline = DevPipeline(
            dispatcher=MagicMock(),
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=MagicMock(),
            transport=None,
        )

        prompt = pipeline._build_prompt(
            repo="owner/repo",
            issue_number=42,
            extra={
                "issue_title": "t",
                "issue_body": "b",
                "branch_name": "fix/issue-42",
            },
            session_id="dev-42",
            state_file=Path("/tmp/state.json"),
        )

        # Points Claude at the approved waiter.
        assert "ctrlrelay ci wait" in prompt
        # Explicitly forbids the pattern that broke in issue #85.
        lower = prompt.lower()
        assert "until" in lower and "while" in lower, (
            "prompt should explicitly ban `until`/`while` bash CI-wait loops"
        )

    @pytest.mark.asyncio
    async def test_run_dispatches_claude_session(self, tmp_path: Path) -> None:
        """Should dispatch Claude session with issue context."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.base import PipelineContext
        from ctrlrelay.pipelines.dev import DevPipeline

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
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

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
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
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
        """request_fix should resume the session using the agent (Claude) UUID
        looked up from state_db, not our composite session id."""
        import time as _time

        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.base import PipelineContext
        from ctrlrelay.pipelines.dev import DevPipeline

        agent_uuid = "b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10"

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="dev-123",
            exit_code=0,
            stdout="",
            stderr="",
            agent_session_id=agent_uuid,
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.DONE,
                session_id="dev-123",
                timestamp="2026-04-17T12:00:00Z",
                summary="Fixed CI",
                outputs={"pr_url": "https://github.com/o/r/pull/42", "pr_number": 42},
            ),
        )

        state_db = StateDB(tmp_path / "state.db")
        state_db.execute(
            """INSERT INTO sessions (id, pipeline, repo, status, started_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("dev-123", "dev", "owner/repo", "running", int(_time.time())),
        )
        state_db.set_agent_session_id("dev-123", agent_uuid)
        state_db.commit()

        pipeline = DevPipeline(
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=state_db,
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
        assert call_kwargs["resume_session_id"] == agent_uuid
        assert call_kwargs["prompt"] == fix_prompt
        state_db.close()

    @pytest.mark.asyncio
    async def test_request_fix_skips_resume_when_no_agent_uuid(
        self, tmp_path: Path
    ) -> None:
        """If state_db has no agent UUID (e.g. session predates the fix),
        request_fix should fall back to a fresh spawn (no --resume) rather
        than hard-failing with `claude --resume <composite-id>`."""
        import time as _time

        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.base import PipelineContext
        from ctrlrelay.pipelines.dev import DevPipeline

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="dev-123",
            exit_code=0,
            stdout="",
            stderr="",
            agent_session_id=None,
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.DONE,
                session_id="dev-123",
                timestamp="2026-04-17T12:00:00Z",
                summary="ok",
                outputs={},
            ),
        )

        state_db = StateDB(tmp_path / "state.db")
        state_db.execute(
            """INSERT INTO sessions (id, pipeline, repo, status, started_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("dev-123", "dev", "owner/repo", "running", int(_time.time())),
        )
        state_db.commit()

        pipeline = DevPipeline(
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=state_db,
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

        await pipeline.request_fix(ctx, "fix it")

        call_kwargs = mock_dispatcher.spawn_session.call_args.kwargs
        assert call_kwargs["resume_session_id"] is None
        state_db.close()

    @pytest.mark.asyncio
    async def test_run_persists_agent_session_id_to_state_db(
        self, tmp_path: Path
    ) -> None:
        """After a spawn_session that returns an agent UUID, the pipeline
        must persist it to state_db so future resumes can look it up."""
        import time as _time

        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.base import PipelineContext
        from ctrlrelay.pipelines.dev import DevPipeline

        agent_uuid = "b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10"

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="dev-123",
            exit_code=0,
            stdout="",
            stderr="",
            agent_session_id=agent_uuid,
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.DONE,
                session_id="dev-123",
                timestamp="2026-04-17T12:00:00Z",
                summary="ok",
                outputs={"pr_number": 42, "pr_url": "https://github.com/o/r/pull/42"},
            ),
        )

        state_db = StateDB(tmp_path / "state.db")
        state_db.execute(
            """INSERT INTO sessions (id, pipeline, repo, status, started_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("dev-123", "dev", "owner/repo", "running", int(_time.time())),
        )
        state_db.commit()

        pipeline = DevPipeline(
            dispatcher=mock_dispatcher,
            github=MagicMock(),
            worktree=MagicMock(),
            dashboard=None,
            state_db=state_db,
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

        await pipeline.run(ctx)

        assert state_db.get_agent_session_id("dev-123") == agent_uuid
        state_db.close()

    @pytest.mark.asyncio
    async def test_run_dev_issue_posts_claim_comment(self, tmp_path: Path) -> None:
        """Should post a claim comment on the issue when work begins."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import AGENT_CLAIM_MARKER, run_dev_issue

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
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import AGENT_CLAIM_MARKER, run_dev_issue

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
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.pipelines.base import PipelineContext
        from ctrlrelay.pipelines.dev import DevPipeline

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
    def _make_done_state(
        session_id: str = "dev-123",
        pr_number: int = 42,
        agent_session_id: str | None = "b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10",
    ):
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult

        return SessionResult(
            session_id=session_id,
            exit_code=0,
            stdout="",
            stderr="",
            agent_session_id=agent_session_id,
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
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = self._make_done_state(pr_number=42)

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = tmp_path / "worktree"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {"number": 1, "title": "x", "body": "y"}
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
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
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

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
            [{"name": "ci", "state": "FAILURE", "bucket": "fail"}],
            [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}],
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
        # Resume must use Claude's UUID, not our composite id — newer claude
        # CLI versions reject non-UUID strings passed to --resume.
        assert fix_call["resume_session_id"] == "b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10"
        assert fix_call["resume_session_id"] != fix_call["session_id"]
        assert "ci" in fix_call["prompt"].lower() or "check" in fix_call["prompt"].lower()

    @pytest.mark.asyncio
    async def test_run_dev_issue_requests_fix_when_conflicting(self, tmp_path: Path) -> None:
        """On CONFLICTING mergeable state, should resume the session with a conflict prompt."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = self._make_done_state(pr_number=42)

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = tmp_path / "worktree"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {"number": 1, "title": "x", "body": "y"}
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
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
        assert fix_call["resume_session_id"] == "b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10"
        assert fix_call["resume_session_id"] != fix_call["session_id"]

    @pytest.mark.asyncio
    async def test_run_dev_issue_blocked_asks_transport_and_resumes(
        self, tmp_path: Path
    ) -> None:
        """If Claude signals BLOCKED with a question, run_dev_issue must post
        the question via the transport, wait for the reply, and resume the
        session with the answer. Loop until DONE."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        agent_uuid = "b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10"
        # 1st spawn = BLOCKED with a question. 2nd spawn (resume) = DONE.
        blocked = SessionResult(
            session_id="dev-123",
            exit_code=0,
            stdout="",
            stderr="",
            agent_session_id=agent_uuid,
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
                session_id="dev-123",
                timestamp="2026-04-17T12:00:00Z",
                question="pin or bump?",
            ),
        )
        done = SessionResult(
            session_id="dev-123",
            exit_code=0,
            stdout="",
            stderr="",
            agent_session_id=agent_uuid,
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.DONE,
                session_id="dev-123",
                timestamp="2026-04-17T12:01:00Z",
                summary="PR opened",
                outputs={
                    "pr_url": "https://github.com/o/r/pull/42",
                    "pr_number": 42,
                },
            ),
        )
        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.side_effect = [blocked, done]

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = tmp_path / "wt"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {
            "number": 13,
            "title": "x",
            "body": "y",
            "comments": [],
        }

        state_db = StateDB(tmp_path / "state.db")

        mock_transport = AsyncMock()
        mock_transport.ask.return_value = "pin to 2.4.1"
        mock_pr_verifier = AsyncMock()
        mock_pr_verifier.verify.return_value = VerificationResult(ready=True)

        result = await run_dev_issue(
            repo="owner/repo",
            issue_number=13,
            branch_template="fix/issue-{n}",
            dispatcher=mock_dispatcher,
            github=mock_github,
            worktree=mock_worktree,
            dashboard=None,
            state_db=state_db,
            transport=mock_transport,
            contexts_dir=tmp_path / "contexts",
            pr_verifier=mock_pr_verifier,
        )

        # Transport was asked the exact BLOCKED question.
        mock_transport.ask.assert_awaited_once()
        asked_q = mock_transport.ask.await_args.args[0]
        assert "pin or bump?" in asked_q

        # Dispatcher was called twice: initial run + resume-with-answer.
        assert mock_dispatcher.spawn_session.call_count == 2
        resume_kwargs = mock_dispatcher.spawn_session.await_args_list[1].kwargs
        # Resume must use Claude's UUID captured on the first spawn, not our
        # composite id (newer claude CLI rejects non-UUID --resume values).
        assert resume_kwargs["resume_session_id"] == agent_uuid
        assert resume_kwargs["resume_session_id"] != resume_kwargs["session_id"]
        assert "pin to 2.4.1" in resume_kwargs["prompt"]

        # Final result is the DONE state.
        assert result.success
        assert result.outputs["pr_number"] == 42

    @pytest.mark.asyncio
    async def test_run_dev_issue_blocked_no_transport_returns_blocked(
        self, tmp_path: Path
    ) -> None:
        """Without a transport there's no way to consume the answer, so we
        must return the BLOCKED result rather than spinning."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

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
                question="?",
            ),
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = tmp_path / "wt"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {
            "number": 13, "title": "x", "body": "y", "comments": [],
        }

        state_db = StateDB(tmp_path / "state.db")

        result = await run_dev_issue(
            repo="owner/repo",
            issue_number=13,
            branch_template="fix/issue-{n}",
            dispatcher=mock_dispatcher,
            github=mock_github,
            worktree=mock_worktree,
            dashboard=None,
            state_db=state_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )

        assert result.blocked
        # dispatcher called exactly once — no resume without a transport.
        assert mock_dispatcher.spawn_session.call_count == 1

    @pytest.mark.asyncio
    async def test_run_dev_issue_blocked_transport_failure_fails_clean(
        self, tmp_path: Path
    ) -> None:
        """If the transport raises (bridge down, timeout), the session must
        end cleanly as FAILED rather than leaving the caller with a blocked
        result we can't recover from."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

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
                question="?",
            ),
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = tmp_path / "wt"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {
            "number": 13, "title": "x", "body": "y", "comments": [],
        }

        state_db = StateDB(tmp_path / "state.db")

        mock_transport = AsyncMock()
        mock_transport.ask.side_effect = RuntimeError("bridge down")

        result = await run_dev_issue(
            repo="owner/repo",
            issue_number=13,
            branch_template="fix/issue-{n}",
            dispatcher=mock_dispatcher,
            github=mock_github,
            worktree=mock_worktree,
            dashboard=None,
            state_db=state_db,
            transport=mock_transport,
            contexts_dir=tmp_path / "contexts",
        )

        # New behavior (codex P1 fix): transport failure during the
        # in-process BLOCKED loop preserves blocked=True so the outer
        # persistence-on-blocked branch fires. A permanently-wedged
        # session would otherwise lose the operator's late reply.
        assert result.blocked
        assert not result.success
        assert "bridge down" in (result.error or "")
        # And the row is in pending_resumes ready for an orphan reply.
        unanswered = state_db.list_unanswered_pending_resumes()
        assert len(unanswered) == 1
        assert unanswered[0]["pipeline"] == "dev"
        assert unanswered[0]["repo"] == "owner/repo"
        state_db.close()

    @pytest.mark.asyncio
    async def test_run_dev_issue_does_not_retry_on_ci_timeout(
        self, tmp_path: Path
    ) -> None:
        """If CI is simply slow (verifier reports timed_out), _verify_and_fix_pr
        must NOT resume Claude — it hands off the PR as-is. Otherwise every
        long-running CI becomes a retry-until-max-attempts failure."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

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
        mock_pr_verifier.verify.return_value = VerificationResult(
            ready=False,
            timed_out=True,
            reason="CI still running after timeout: 1 check(s) pending (long-ci)",
            pending_checks=[
                {"name": "long-ci", "state": "IN_PROGRESS", "bucket": "pending"}
            ],
        )

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
            pr_verifier=mock_pr_verifier,
        )

        # Hand-off as success — CI is slow but the PR is opened.
        assert result.success
        assert result.outputs["pr_number"] == 42
        # No fix attempt was issued.
        assert mock_dispatcher.spawn_session.call_count == 1

    @pytest.mark.asyncio
    async def test_run_dev_issue_fails_after_max_fix_attempts(self, tmp_path: Path) -> None:
        """Should give up and return failure after max fix attempts."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

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
            {"name": "ci", "state": "FAILURE", "bucket": "fail"},
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


class TestRunDevIssueLockReleaseDuringVerify:
    """Issue #29: the repo lock is released while PR CI verification is
    polling GitHub (pure `gh` traffic, no git access), then reacquired
    before any ``request_fix`` call (which spawns claude and mutates the
    worktree). These tests exercise the release/reacquire boundary and
    verify peer sessions aren't blocked during the wait."""

    @pytest.mark.asyncio
    async def test_lock_released_during_verify_allows_concurrent_session(
        self, tmp_path: Path
    ) -> None:
        """While session A is in _verify_and_fix_pr's polling window,
        session B must be able to acquire the repo lock for its own
        git work. After A finishes verification it returns without
        needing the lock back (CI passed → no fix needed)."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="dev-a",
            exit_code=0,
            stdout="",
            stderr="",
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.DONE,
                session_id="dev-a",
                timestamp="2026-04-17T12:00:00Z",
                summary="PR #42 opened",
                outputs={
                    "pr_url": "https://github.com/o/r/pull/42",
                    "pr_number": 42,
                },
            ),
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = (
            tmp_path / "worktree"
        )
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {
            "number": 1, "title": "x", "body": "y", "comments": [],
        }

        state_db = StateDB(tmp_path / "state.db")

        peer_session_id = "dev-peer-session"
        peer_acquired: list[bool] = []

        mock_pr_verifier = AsyncMock()

        async def verify_checks_peer_can_acquire(*_args, **_kwargs):
            # During the verify call, simulate a peer session trying to
            # acquire the repo lock. Before #29 this would fail because
            # run_dev_issue held the lock for the entire run.
            peer_acquired.append(
                state_db.acquire_lock("owner/repo", peer_session_id)
            )
            if peer_acquired[-1]:
                # Release so run_dev_issue can reacquire for cleanup.
                state_db.release_lock("owner/repo", peer_session_id)
            return VerificationResult(ready=True)

        mock_pr_verifier.verify.side_effect = verify_checks_peer_can_acquire

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
            pr_verifier=mock_pr_verifier,
        )

        assert result.success
        # Peer was tried exactly once and SUCCEEDED — proving the lock
        # was released during verify.
        assert peer_acquired == [True]
        # Session A's lock is gone after run completes.
        assert state_db.get_lock_holder("owner/repo") is None
        state_db.close()

    @pytest.mark.asyncio
    async def test_lock_reacquired_before_request_fix(
        self, tmp_path: Path
    ) -> None:
        """When verification fails and request_fix is needed, the lock
        must be held again for the claude-spawn phase."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        session_id_a = "dev-a"
        agent_uuid = "b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10"

        done_state = SessionResult(
            session_id=session_id_a,
            exit_code=0,
            stdout="",
            stderr="",
            agent_session_id=agent_uuid,
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.DONE,
                session_id=session_id_a,
                timestamp="2026-04-17T12:00:00Z",
                summary="PR #42",
                outputs={
                    "pr_url": "https://github.com/o/r/pull/42",
                    "pr_number": 42,
                },
            ),
        )
        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = done_state

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = (
            tmp_path / "worktree"
        )
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {
            "number": 1, "title": "x", "body": "y", "comments": [],
        }

        state_db = StateDB(tmp_path / "state.db")

        holders_during_fix: list[str | None] = []

        async def fix_spawn(*_args, **kwargs):
            # Capture lock holder during the fix spawn to prove we hold
            # it across the claude-invocation window.
            holders_during_fix.append(
                state_db.get_lock_holder("owner/repo")
            )
            return done_state

        verify_calls = 0

        async def verify(*_args, **_kwargs):
            nonlocal verify_calls
            verify_calls += 1
            if verify_calls == 1:
                return VerificationResult(
                    ready=False,
                    reason="ci failed",
                    failing_checks=[
                        {"name": "ci", "state": "FAILURE", "bucket": "fail"}
                    ],
                )
            return VerificationResult(ready=True)

        # First spawn = the initial run; second spawn = the fix round.
        mock_dispatcher.spawn_session.side_effect = [done_state, None]

        async def spawn_side_effect(*args, **kwargs):
            if mock_dispatcher.spawn_session.call_count == 1:
                return done_state
            return await fix_spawn(*args, **kwargs)

        mock_dispatcher.spawn_session.side_effect = spawn_side_effect

        mock_pr_verifier = AsyncMock()
        mock_pr_verifier.verify.side_effect = verify

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
            pr_verifier=mock_pr_verifier,
        )

        assert result.success
        # fix_spawn ran once and the lock was held by THIS session at
        # the moment request_fix invoked the dispatcher.
        assert len(holders_during_fix) == 1
        # session_id is generated internally as
        # dev-owner-repo-1-<hex8>; just assert SOMEONE (i.e. this
        # session) holds it rather than nothing.
        assert holders_during_fix[0] is not None
        assert holders_during_fix[0].startswith("dev-owner-repo-1-")
        # Lock fully released when the run is done.
        assert state_db.get_lock_holder("owner/repo") is None
        state_db.close()

    @pytest.mark.asyncio
    async def test_lock_reacquire_fails_when_contended(
        self, tmp_path: Path
    ) -> None:
        """If verification finds work to do but a peer holds the lock
        forever, we must surface a typed error rather than run
        request_fix without exclusive access."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines import dev as dev_mod
        from ctrlrelay.pipelines.dev import run_dev_issue

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="dev-a",
            exit_code=0,
            stdout="",
            stderr="",
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.DONE,
                session_id="dev-a",
                timestamp="2026-04-17T12:00:00Z",
                summary="PR #42",
                outputs={
                    "pr_url": "https://github.com/o/r/pull/42",
                    "pr_number": 42,
                },
            ),
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = (
            tmp_path / "worktree"
        )
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {
            "number": 1, "title": "x", "body": "y", "comments": [],
        }

        state_db = StateDB(tmp_path / "state.db")

        async def verify(*_args, **_kwargs):
            # Returns not-ready so run_dev_issue has to reacquire the
            # lock before request_fix. Simulates a peer grabbing the
            # lock immediately after we release.
            holder = state_db.get_lock_holder("owner/repo")
            if holder is None:
                state_db.acquire_lock("owner/repo", "peer-session-blocking")
            return VerificationResult(
                ready=False,
                reason="ci failed",
                failing_checks=[
                    {"name": "ci", "state": "FAILURE", "bucket": "fail"}
                ],
            )

        mock_pr_verifier = AsyncMock()
        mock_pr_verifier.verify.side_effect = verify

        # Zero sleep so the test runs instantly; small attempt budget
        # so we fail fast after a handful of contention misses.
        monkey_attempts = dev_mod._REACQUIRE_LOCK_ATTEMPTS
        monkey_sleep = dev_mod._REACQUIRE_LOCK_SLEEP_SECONDS
        dev_mod._REACQUIRE_LOCK_ATTEMPTS = 2
        dev_mod._REACQUIRE_LOCK_SLEEP_SECONDS = 0.0

        try:
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
                pr_verifier=mock_pr_verifier,
            )
        finally:
            dev_mod._REACQUIRE_LOCK_ATTEMPTS = monkey_attempts
            dev_mod._REACQUIRE_LOCK_SLEEP_SECONDS = monkey_sleep

        assert not result.success
        # Post-verify contention uses a DISTINCT error string from the
        # initial acquire failure: the PR already exists, and if cli
        # saw the same "Repository locked..." string it'd unmark the
        # issue and a future poll would launch a duplicate dev pass.
        assert result.error == dev_mod._LOCK_CONTENDED_DURING_VERIFY_ERROR
        assert result.error != dev_mod._LOCK_CONTENDED_ERROR
        # cli.handle_issue keys its "retry from scratch" branch on the
        # substring below. This error must NOT match it — otherwise the
        # whole point of the distinct string is lost.
        assert "locked by another session" not in result.error.lower()
        # Peer still holds the lock, so cleanup reacquire also failed.
        # The outputs flag tells cli the worktree is still registered
        # and a fresh retry would fail at create_worktree — cli uses
        # this to decide whether to unmark the issue.
        assert result.outputs.get("cleanup_deferred") is True
        # Dispatcher called exactly once — initial spawn only, never
        # got as far as request_fix because we couldn't reacquire.
        assert mock_dispatcher.spawn_session.call_count == 1
        # Peer lock is still there (we never touched it).
        assert state_db.get_lock_holder("owner/repo") == "peer-session-blocking"
        state_db.close()

    @pytest.mark.asyncio
    async def test_cancelled_during_verify_does_not_leak_lock(
        self, tmp_path: Path
    ) -> None:
        """If asyncio.CancelledError arrives during the unlocked verify
        phase, the finally block must not hold any lock AND must not
        re-acquire one that was never released gracefully."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.return_value = SessionResult(
            session_id="dev-a",
            exit_code=0,
            stdout="",
            stderr="",
            state=CheckpointState(
                version="1",
                status=CheckpointStatus.DONE,
                session_id="dev-a",
                timestamp="2026-04-17T12:00:00Z",
                summary="PR #42",
                outputs={
                    "pr_url": "https://github.com/o/r/pull/42",
                    "pr_number": 42,
                },
            ),
        )

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree_with_new_branch.return_value = (
            tmp_path / "worktree"
        )
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {
            "number": 1, "title": "x", "body": "y", "comments": [],
        }

        state_db = StateDB(tmp_path / "state.db")

        async def verify(*_args, **_kwargs):
            # Confirm lock was released before raising cancellation.
            assert state_db.get_lock_holder("owner/repo") is None
            raise asyncio.CancelledError()

        mock_pr_verifier = AsyncMock()
        mock_pr_verifier.verify.side_effect = verify

        with pytest.raises(asyncio.CancelledError):
            await run_dev_issue(
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
                pr_verifier=mock_pr_verifier,
            )

        # Lock table must be clean — no leaked row from this session.
        assert state_db.get_lock_holder("owner/repo") is None
        state_db.close()


class TestRepoLockHandleReleaseSafety:
    """_RepoLockHandle.release() must not flip held=False when the
    underlying DELETE raised — otherwise our stale lock row stays in
    repo_locks while our code thinks the lock is free, wedging every
    peer session until someone cleans up manually (codex P2)."""

    def test_release_keeps_held_true_when_db_raises(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import _RepoLockHandle

        real = StateDB(tmp_path / "state.db")

        class FlakyDB:
            """Proxy that raises once on release, then delegates."""
            def __init__(self, inner: StateDB):
                self.inner = inner
                self.release_calls = 0

            def acquire_lock(self, *a, **kw):
                return self.inner.acquire_lock(*a, **kw)

            def release_lock(self, *a, **kw):
                self.release_calls += 1
                if self.release_calls == 1:
                    raise RuntimeError("transient sqlite hiccup")
                return self.inner.release_lock(*a, **kw)

            def get_lock_holder(self, *a, **kw):
                return self.inner.get_lock_holder(*a, **kw)

        flaky = FlakyDB(real)
        handle = _RepoLockHandle(flaky, "owner/repo", "sess-A")
        assert flaky.acquire_lock("owner/repo", "sess-A") is True
        handle.held = True

        # First release raises inside release_lock. held must stay True
        # so the caller knows the row may still be present.
        handle.release()
        assert handle.held is True, (
            "held must stay True after a failed release — otherwise the "
            "stale repo_locks row wedges peer sessions"
        )

        # Second release succeeds and clears held.
        handle.release()
        assert handle.held is False
        assert flaky.release_calls == 2
        assert real.get_lock_holder("owner/repo") is None
        real.close()

    def test_release_is_idempotent_when_not_held(
        self, tmp_path: Path
    ) -> None:
        """Safe to call from a finally that ran before acquire ever
        succeeded — no DB round-trip, no exceptions."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import _RepoLockHandle

        db = StateDB(tmp_path / "state.db")
        handle = _RepoLockHandle(db, "owner/repo", "sess-A")
        # Never acquired.
        handle.release()
        handle.release()
        assert handle.held is False
        db.close()


class TestRepoLockHandleReacquireBudget:
    """The default reacquire budget must be generous enough that a
    healthy peer session running a full claude pass doesn't cause a
    spurious contention failure (codex P1). Cleanup uses a SEPARATE
    short budget so a contended cleanup doesn't stall the serial
    poller for an hour (codex P1 round-2)."""

    def test_default_budget_outlasts_typical_peer_run(self) -> None:
        """A peer claude run can take 10-30 minutes. The default budget
        must comfortably exceed that — otherwise same-repo parallelism
        deterministically fails whenever a peer is mid-run."""
        from ctrlrelay.pipelines import dev as dev_mod

        total_seconds = (
            dev_mod._REACQUIRE_LOCK_ATTEMPTS
            * dev_mod._REACQUIRE_LOCK_SLEEP_SECONDS
        )
        # >= 30 minutes. Change-detector test: if someone knocks this
        # back toward the old 30s cap, they need to justify why.
        assert total_seconds >= 30 * 60, (
            f"Reacquire budget of {total_seconds}s is too short to "
            "outlast a normal peer claude run (10-30 min)"
        )

    def test_cleanup_budget_is_short_so_poller_does_not_stall(self) -> None:
        """Cleanup reacquire budget must stay small. run_poll_loop
        awaits handlers serially and spawns the PR watcher AFTER
        run_dev_issue returns; if cleanup waited the full fix-path
        budget, a single contended cleanup would delay every other
        repo's polling by up to an hour and hold back the watcher
        for a PR that already passed verification."""
        from ctrlrelay.pipelines import dev as dev_mod

        cleanup_seconds = (
            dev_mod._REACQUIRE_CLEANUP_ATTEMPTS
            * dev_mod._REACQUIRE_CLEANUP_SLEEP_SECONDS
        )
        # Cap at 30s: anything longer would meaningfully stall the poll.
        assert cleanup_seconds <= 30, (
            f"Cleanup reacquire budget of {cleanup_seconds}s is too "
            "long; would stall the serial poll cycle"
        )
        # And it must be MUCH shorter than the fix-path budget — if
        # someone lazily equated them in a refactor, this catches it.
        fix_seconds = (
            dev_mod._REACQUIRE_LOCK_ATTEMPTS
            * dev_mod._REACQUIRE_LOCK_SLEEP_SECONDS
        )
        assert cleanup_seconds < fix_seconds / 10
