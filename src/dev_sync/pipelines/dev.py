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
from dev_sync.core.pr_verifier import PRVerifier, VerificationResult
from dev_sync.core.state import StateDB
from dev_sync.core.worktree import WorktreeManager
from dev_sync.dashboard.client import DashboardClient, EventPayload
from dev_sync.pipelines.base import PipelineContext, PipelineResult
from dev_sync.transports.base import Transport

DEFAULT_MAX_FIX_ATTEMPTS = 3


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

    async def request_fix(
        self, ctx: PipelineContext, fix_instructions: str
    ) -> PipelineResult:
        """Resume the session with a fix request (failing CI or merge conflict)."""
        result = await self.dispatcher.spawn_session(
            session_id=ctx.session_id,
            prompt=fix_instructions,
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
6. Before marking the task complete, verify the PR is mergeable:
   - Poll `gh pr checks <PR>` until every check is completed; if any conclusion
     is failure/cancelled/timed_out, investigate, fix, push again, and re-poll.
   - Run `gh pr view <PR> --json mergeable,mergeStateStatus` — if mergeable is
     `CONFLICTING` or mergeStateStatus is `DIRTY`, rebase onto the base branch,
     resolve conflicts, and push again.
7. Use checkpoint.done() only when the PR is green AND conflict-free, passing
   outputs={{"pr_url": "...", "pr_number": N}}.

Do NOT merge the PR - wait for human review.

Use checkpoint.blocked() if you need human input.
Use checkpoint.failed() if something goes wrong.

The orchestrator re-verifies CI and mergeability after you hand off; if it
finds the PR broken it will resume this session asking you to fix it."""

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


def _build_fix_prompt(pr_number: int, verification: VerificationResult) -> str:
    """Build a resume-prompt asking Claude to fix CI failures or merge conflicts."""
    if verification.mergeable == "CONFLICTING":
        return (
            f"PR #{pr_number} has merge conflicts with the base branch "
            f"(mergeStateStatus={verification.merge_state_status}). "
            "Rebase the branch onto the base, resolve the conflicts, push the "
            "updated branch, then call checkpoint.done() with the same "
            f'outputs={{"pr_url": "...", "pr_number": {pr_number}}} once the PR '
            "reports MERGEABLE. Use checkpoint.failed() if conflicts cannot be "
            "resolved."
        )

    if verification.failing_checks:
        names = ", ".join(
            f"{c.get('name', '?')}={c.get('conclusion') or c.get('status')}"
            for c in verification.failing_checks
        )
        return (
            f"PR #{pr_number} has failing or incomplete CI checks: {names}. "
            "Investigate the failures (fetch logs via `gh run view` as needed), "
            "fix the underlying issues, commit and push, then wait for CI to go "
            "green. Call checkpoint.done() with the same outputs once all checks "
            "pass and the PR is MERGEABLE."
        )

    return (
        f"PR #{pr_number} is not ready to hand off: {verification.reason}. "
        "Investigate, push any required fixes, and call checkpoint.done() once "
        "CI is green and the PR is MERGEABLE."
    )


async def _verify_and_fix_pr(
    *,
    pipeline: DevPipeline,
    ctx: PipelineContext,
    result: PipelineResult,
    verifier: PRVerifier,
    max_attempts: int,
) -> PipelineResult:
    """Loop: verify CI+mergeability, ask Claude to fix, re-verify."""
    pr_number_raw = result.outputs.get("pr_number")
    if pr_number_raw is None:
        return result
    pr_number = int(pr_number_raw)

    verification = await verifier.verify(ctx.repo, pr_number)
    attempts = 0
    while not verification.ready and attempts < max_attempts:
        fix_prompt = _build_fix_prompt(pr_number, verification)
        fix_result = await pipeline.request_fix(ctx, fix_prompt)
        attempts += 1

        if not fix_result.success:
            # Preserve the original PR info in outputs so callers can still find it.
            merged_outputs = dict(result.outputs)
            merged_outputs.update(fix_result.outputs)
            return PipelineResult(
                success=False,
                session_id=fix_result.session_id,
                summary=fix_result.summary,
                blocked=fix_result.blocked,
                question=fix_result.question,
                error=fix_result.error,
                outputs=merged_outputs,
            )

        result = fix_result
        verification = await verifier.verify(ctx.repo, pr_number)

    if verification.ready:
        return result

    return PipelineResult(
        success=False,
        session_id=result.session_id,
        summary=(
            f"PR #{pr_number} not ready after {max_attempts} fix attempt(s): "
            f"{verification.reason}"
        ),
        error=verification.reason,
        outputs=result.outputs,
    )


async def run_dev_issue(
    repo: str,
    issue_number: int,
    branch_template: str,
    dispatcher: ClaudeDispatcher,
    github: GitHubCLI,
    worktree: WorktreeManager,
    dashboard: DashboardClient | None,
    state_db: StateDB,
    transport: Transport | None,
    contexts_dir: Path,
    max_fix_attempts: int = DEFAULT_MAX_FIX_ATTEMPTS,
    pr_verifier: PRVerifier | None = None,
) -> PipelineResult:
    """Run dev pipeline for a single issue."""
    session_id = f"dev-{repo.replace('/', '-')}-{issue_number}-{uuid.uuid4().hex[:8]}"
    branch_name = branch_template.replace("{n}", str(issue_number))

    if not state_db.acquire_lock(repo, session_id):
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Could not acquire lock for {repo}",
            error="Repository locked by another session",
        )

    try:
        # Get issue details
        issue = await github.get_issue(repo, issue_number)

        # Create worktree with new branch
        await worktree.ensure_bare_repo(repo)
        worktree_path = await worktree.create_worktree_with_new_branch(
            repo=repo,
            session_id=session_id,
            new_branch=branch_name,
        )

        # Symlink context
        context_path = contexts_dir / repo.replace("/", "-") / "CLAUDE.md"
        if context_path.exists():
            worktree.symlink_context(worktree_path, context_path)

        # Setup state file
        state_file = worktree_path / ".dev-sync" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)

        ctx = PipelineContext(
            session_id=session_id,
            repo=repo,
            worktree_path=worktree_path,
            context_path=context_path,
            state_file=state_file,
            issue_number=issue_number,
            extra={
                "issue_title": issue.get("title", ""),
                "issue_body": issue.get("body", ""),
                "branch_name": branch_name,
            },
        )

        # Record session
        state_db.execute(
            """INSERT INTO sessions
               (id, pipeline, repo, worktree_path, status, started_at, issue_number)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, "dev", repo, str(worktree_path),
                "running", int(time.time()), issue_number,
            ),
        )
        state_db.commit()

        # Run pipeline
        pipeline = DevPipeline(
            dispatcher=dispatcher,
            github=github,
            worktree=worktree,
            dashboard=dashboard,
            state_db=state_db,
            transport=transport,
        )
        result = await pipeline.run(ctx)

        # Verify PR is green & conflict-free before handing off. Resume the
        # session with a fix request if either is broken, up to max_fix_attempts.
        if result.success and result.outputs.get("pr_number") is not None:
            verifier = pr_verifier or PRVerifier(github=github)
            result = await _verify_and_fix_pr(
                pipeline=pipeline,
                ctx=ctx,
                result=result,
                verifier=verifier,
                max_attempts=max_fix_attempts,
            )

        # Update session status
        status = "done" if result.success else ("blocked" if result.blocked else "failed")
        state_db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? WHERE id = ?",
            (status, result.summary, int(time.time()), session_id),
        )
        state_db.commit()

        # Push event to dashboard
        if dashboard and result.success:
            await dashboard.push_event(EventPayload(
                level="info",
                pipeline="dev",
                repo=repo,
                message=result.summary,
                session_id=session_id,
                details={
                    "issue_number": issue_number,
                    "pr_number": result.outputs.get("pr_number"),
                },
            ))

        # Send notification via transport
        if transport and result.success:
            pr_url = result.outputs.get("pr_url", "")
            await transport.send(f"PR ready for review: {pr_url}")

        # Cleanup only if not blocked (blocked sessions need worktree for resume)
        if not result.blocked:
            worktree.remove_context_symlink(worktree_path)
            await worktree.remove_worktree(repo, session_id)

        return result

    except Exception as e:
        state_db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? WHERE id = ?",
            ("failed", f"Error: {e}", int(time.time()), session_id),
        )
        state_db.commit()
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Error processing issue #{issue_number}",
            error=str(e),
        )

    finally:
        state_db.release_lock(repo, session_id)
