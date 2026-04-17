"""Dev pipeline for issue-to-PR workflow."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev_sync.core.checkpoint import CheckpointStatus
from dev_sync.core.dispatcher import ClaudeDispatcher, SessionResult
from dev_sync.core.github import GitHubCLI
from dev_sync.core.state import StateDB
from dev_sync.core.worktree import WorktreeManager
from dev_sync.dashboard.client import DashboardClient, EventPayload
from dev_sync.pipelines.base import PipelineContext, PipelineResult
from dev_sync.transports.base import Transport


@dataclass
class DevPipeline:
    """Dev pipeline for implementing issues and opening PRs."""

    dispatcher: ClaudeDispatcher
    github: GitHubCLI
    worktree: WorktreeManager
    dashboard: DashboardClient | None
    state_db: StateDB
    transport: Transport | None

    name: str = "dev"

    async def run(self, ctx: PipelineContext) -> PipelineResult:
        """Run dev pipeline on a single issue."""
        prompt = self._build_prompt(ctx.repo, ctx.issue_number, ctx.extra)

        result = await self.dispatcher.spawn_session(
            session_id=ctx.session_id,
            prompt=prompt,
            working_dir=ctx.worktree_path,
            state_file=ctx.state_file,
        )

        return self._session_to_result(result)

    async def resume(self, ctx: PipelineContext, answer: str) -> PipelineResult:
        """Resume blocked dev session with user answer."""
        prompt = f"User answered: {answer}\n\nContinue from where you left off."

        result = await self.dispatcher.spawn_session(
            session_id=ctx.session_id,
            prompt=prompt,
            working_dir=ctx.worktree_path,
            state_file=ctx.state_file,
            resume_session_id=ctx.session_id,
        )

        return self._session_to_result(result)

    def _build_prompt(
        self,
        repo: str,
        issue_number: int | None,
        extra: dict[str, Any],
    ) -> str:
        """Build the dev pipeline prompt."""
        issue_title = extra.get("issue_title", "")
        issue_body = extra.get("issue_body", "")
        branch_name = extra.get("branch_name", "")

        return f"""You are working on issue #{issue_number} in repository {repo}.

**Issue Title:** {issue_title}

**Issue Body:**
{issue_body}

**Branch:** {branch_name}

Execute the following workflow:

1. Validate the issue still applies to the current codebase
2. If anything is unclear, use checkpoint.blocked() to ask for clarification
3. Run /superpowers to plan and implement the fix using TDD
4. Run codex review and address any feedback
5. Push the branch and open a PR that references the issue
6. Use checkpoint.done() with the PR URL in outputs["pr_url"]

Do NOT merge the PR - wait for human review.

Use checkpoint.done() with outputs={{"pr_url": "...", "pr_number": N}} when PR is opened.
Use checkpoint.blocked() if you need human input.
Use checkpoint.failed() if something goes wrong."""

    def _session_to_result(self, result: SessionResult) -> PipelineResult:
        """Convert SessionResult to PipelineResult."""
        if result.state is None:
            return PipelineResult(
                success=False,
                session_id=result.session_id,
                summary="No checkpoint state returned",
                error=result.stderr or "Unknown error",
            )

        if result.state.status == CheckpointStatus.DONE:
            return PipelineResult(
                success=True,
                session_id=result.session_id,
                summary=result.state.summary or "Completed",
                outputs=result.state.outputs,
            )

        if result.state.status == CheckpointStatus.BLOCKED_NEEDS_INPUT:
            return PipelineResult(
                success=False,
                session_id=result.session_id,
                summary="Blocked on user input",
                blocked=True,
                question=result.state.question,
            )

        return PipelineResult(
            success=False,
            session_id=result.session_id,
            summary="Failed",
            error=result.state.error,
        )
