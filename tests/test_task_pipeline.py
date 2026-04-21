"""Tests for the task pipeline (non-PR GitHub issues)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestTaskPipelineBasic:
    """Task pipeline: agent runs, posts comment, DONE. No branch, no PR."""

    @pytest.mark.asyncio
    async def test_task_done_marks_session_done_no_pr_in_outputs(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.task import run_task_issue

        async def spawn(**kwargs):
            return SessionResult(
                session_id=kwargs["session_id"],
                exit_code=0, stdout="", stderr="",
                state=CheckpointState(
                    version="1",
                    status=CheckpointStatus.DONE,
                    session_id=kwargs["session_id"],
                    timestamp="2026-04-21T12:00:00Z",
                    summary="Build ran clean; no errors.",
                ),
            )

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.side_effect = spawn

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "wt"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {
            "number": 55,
            "title": "Please run the build and report",
            "body": "The last main push might have broken the build.",
        }

        state_db = StateDB(tmp_path / "state.db")

        result = await run_task_issue(
            repo="owner/repo",
            issue_number=55,
            dispatcher=mock_dispatcher,
            github=mock_github,
            worktree=mock_worktree,
            dashboard=None,
            state_db=state_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )

        assert result.success
        assert "Build ran clean" in result.summary
        # Task pipeline must NOT put pr_url / pr_number in outputs —
        # that's how the cli routes its notification. A stray pr_url
        # would cause a "✅ PR ready: " notification with nothing after
        # it.
        assert "pr_url" not in result.outputs
        assert "pr_number" not in result.outputs

        # Session row marked done.
        row = state_db.get_session_row(result.session_id)
        assert row is not None
        assert row["pipeline"] == "task"
        assert row["status"] == "done"
        assert row["issue_number"] == 55

        # No pending_resumes row (only BLOCKED would create one).
        assert state_db.list_unanswered_pending_resumes() == []

        # No branch operations were even attempted — task pipeline
        # never calls create_worktree_with_new_branch or
        # delete_branch. The dev pipeline mocks those; this
        # assertion catches a refactor that accidentally reuses
        # dev's branch-touching code path.
        assert not hasattr(mock_worktree, 'create_worktree_with_new_branch') \
            or not mock_worktree.create_worktree_with_new_branch.await_count
        state_db.close()

    @pytest.mark.asyncio
    async def test_task_blocked_persists_to_pending_resumes(
        self, tmp_path: Path
    ) -> None:
        """A BLOCKED task exit must write a pipeline='task' row so a
        Telegram reply after exit can still drive the resume via the
        sweeper."""
        from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
        from ctrlrelay.core.dispatcher import SessionResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.task import run_task_issue

        async def spawn(**kwargs):
            return SessionResult(
                session_id=kwargs["session_id"],
                exit_code=0, stdout="", stderr="",
                state=CheckpointState(
                    version="1",
                    status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
                    session_id=kwargs["session_id"],
                    timestamp="2026-04-21T12:00:00Z",
                    question=(
                        "The build target isn't clear — is it "
                        "`npm run build` or `uv run pytest`?"
                    ),
                ),
            )

        mock_dispatcher = AsyncMock()
        mock_dispatcher.spawn_session.side_effect = spawn

        mock_worktree = AsyncMock()
        mock_worktree.create_worktree.return_value = tmp_path / "wt"
        mock_worktree.symlink_context = MagicMock()
        mock_worktree.remove_context_symlink = MagicMock()

        mock_github = AsyncMock()
        mock_github.get_issue.return_value = {
            "number": 77, "title": "Run build", "body": "",
        }

        state_db = StateDB(tmp_path / "state.db")

        result = await run_task_issue(
            repo="owner/repo",
            issue_number=77,
            dispatcher=mock_dispatcher,
            github=mock_github,
            worktree=mock_worktree,
            dashboard=None,
            state_db=state_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )

        assert result.blocked
        unanswered = state_db.list_unanswered_pending_resumes()
        assert len(unanswered) == 1
        assert unanswered[0]["pipeline"] == "task"
        assert unanswered[0]["repo"] == "owner/repo"
        assert "build target" in unanswered[0]["question"]
        state_db.close()

    def test_question_for_persist_synthesizes_fallback(self) -> None:
        """The helper backstops the (rare) case where a BLOCKED result
        has no question text — matches the codex P1 fix on dev/secops.
        Note: the CheckpointState schema rejects empty-question BLOCKED
        at ingest, so this branch is defensive; exercising the helper
        directly is the reliable way to pin behavior."""
        from ctrlrelay.pipelines.base import PipelineResult
        from ctrlrelay.pipelines.task import _question_for_persist

        # Non-empty question → returned verbatim.
        r = PipelineResult(
            success=False, blocked=True,
            session_id="s", summary="b",
            question="which build?",
        )
        assert _question_for_persist("s", r) == "which build?"

        # None / empty / whitespace → synthesized placeholder.
        for empty in (None, "", "   "):
            r = PipelineResult(
                success=False, blocked=True,
                session_id="s", summary="b",
                question=empty,
            )
            out = _question_for_persist("s", r)
            assert out.strip() != ""
            assert "blocked but did not include" in out

    @pytest.mark.asyncio
    async def test_resume_task_from_pending_missing_session_row(
        self, tmp_path: Path
    ) -> None:
        """Defensive: if pending_resumes points at a session_id with
        no matching sessions row, fail cleanly."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.task import resume_task_from_pending

        state_db = StateDB(tmp_path / "state.db")
        result = await resume_task_from_pending(
            session_id="task-nope",
            repo="owner/repo",
            answer="do it",
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


class TestTaskLabelRouting:
    """Poller's handle_issue must route by the task_labels list.

    We don't reach into cli.py's closure-scoped handle_issue directly
    — that's orchestration tested at CLI level. Here we assert the
    config surface works, and that the routing predicate is correct."""

    def test_default_task_labels_contains_task(self) -> None:
        from ctrlrelay.core.config import AutomationConfig
        cfg = AutomationConfig()
        assert "task" in [lbl.lower() for lbl in cfg.task_labels]

    def test_task_labels_is_configurable_per_repo(
        self, tmp_path: Path
    ) -> None:
        """A repo config can override task_labels (e.g., to add
        'build-check', 'investigate')."""
        from ctrlrelay.core.config import ConfigError, load_config
        cfg_path = tmp_path / "orchestrator.yaml"
        cfg_path.write_text(
            """
version: "1"
node_id: "test"
timezone: "UTC"
paths:
  state_db: "~/state.db"
  worktrees: "~/wt"
  bare_repos: "~/bare"
  contexts: "~/ctx"
  skills: "~/skills"
agent:
  type: "claude"
transport:
  type: "file_mock"
  file_mock:
    inbox: "~/in.txt"
    outbox: "~/out.txt"
repos:
  - name: "owner/a"
    local_path: "~/a"
    automation:
      task_labels: ["task", "investigate", "build-check"]
"""
        )
        try:
            cfg = load_config(cfg_path)
        except ConfigError as e:
            pytest.fail(f"config parse failed: {e}")
        assert cfg.repos[0].automation.task_labels == [
            "task", "investigate", "build-check"
        ]

    def test_task_and_exclude_disjoint_by_default(self) -> None:
        """Operator-only exclusion list and task-routing list don't
        overlap by default — `manual`/`operator`/`instruction` are
        skipped entirely; `task` runs the task pipeline."""
        from ctrlrelay.core.config import AutomationConfig
        cfg = AutomationConfig()
        exclude_lower = {lbl.lower() for lbl in cfg.exclude_labels}
        task_lower = {lbl.lower() for lbl in cfg.task_labels}
        assert exclude_lower.isdisjoint(task_lower)
