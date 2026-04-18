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

AGENT_CLAIM_MARKER = "<!-- dev-sync:claimed -->"
AGENT_CLAIM_COMMENT = (
    "🤖 Agent is working on this issue. A PR will be opened for review.\n\n"
    f"{AGENT_CLAIM_MARKER}"
)


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
        prompt = self._build_prompt(
            ctx.repo, ctx.issue_number, ctx.extra,
            session_id=ctx.session_id,
            state_file=ctx.state_file,
        )

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
        session_id: str = "",
        state_file: Path | None = None,
    ) -> str:
        """Build the dev pipeline prompt."""
        issue_title = extra.get("issue_title", "")
        issue_body = extra.get("issue_body", "")
        branch_name = extra.get("branch_name", "")
        state_file_path = str(state_file) if state_file else "/tmp/state.json"

        return f"""You are working on issue #{issue_number} in repository {repo}.

**Issue Title:** {issue_title}

**Issue Body:**
{issue_body}

**Branch:** {branch_name}

Execute the following workflow:

1. Validate the issue still applies to the current codebase
2. If anything is unclear, signal BLOCKED (see below) to ask for clarification
3. Plan and implement the fix using TDD
4. Push the branch and open a PR that references the issue
5. Before signaling DONE, verify the PR is mergeable:
   - Poll `gh pr checks <PR>` until every check is `completed`; if any
     conclusion is `failure`/`cancelled`/`timed_out`, investigate, fix, push
     again, and re-poll.
   - Run `gh pr view <PR> --json mergeable,mergeStateStatus` — if `mergeable`
     is `CONFLICTING` or `mergeStateStatus` is `DIRTY`, rebase onto the base
     branch, resolve conflicts, and push again.
6. Signal DONE only when the PR is green AND conflict-free, with the PR URL.

Do NOT merge the PR - wait for human review.

The orchestrator re-verifies CI and mergeability after you hand off; if it
finds the PR broken it will resume this session asking you to fix it.

## Signaling Completion

**CRITICAL**: Before exiting, you MUST write a checkpoint file to signal completion.

STATE_FILE: {state_file_path}
SESSION_ID: {session_id}

**DONE** (PR opened AND verified green + conflict-free):
```bash
mkdir -p "$(dirname '{state_file_path}')"
printf '{{"version":"1","status":"DONE","session_id":"{session_id}",'\
'"timestamp":"%s","summary":"PR opened",'\
'"outputs":{{"pr_url":"%s","pr_number":%d}}}}' \\
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<PR_URL>" <PR_NUM> > '{state_file_path}'
```

**BLOCKED** (need input):
```bash
mkdir -p "$(dirname '{state_file_path}')"
printf '{{"version":"1","status":"BLOCKED_NEEDS_INPUT",'\
'"session_id":"{session_id}","timestamp":"%s","question":"%s"}}' \\
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<QUESTION>" > '{state_file_path}'
```

**FAILED**:
```bash
mkdir -p "$(dirname '{state_file_path}')"
printf '{{"version":"1","status":"FAILED","session_id":"{session_id}",'\
'"timestamp":"%s","error":"%s"}}' \\
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<ERROR>" > '{state_file_path}'
```"""

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
            "updated branch, then signal DONE with the same "
            f'outputs (pr_url, pr_number={pr_number}) once the PR reports '
            "MERGEABLE. Signal FAILED if conflicts cannot be resolved."
        )

    if verification.merge_state_status == "BEHIND":
        return (
            f"PR #{pr_number} is behind the base branch and base-branch "
            "protection requires it to be up-to-date before merge "
            "(mergeStateStatus=BEHIND). Rebase the branch onto the base and "
            "force-push, then signal DONE with the same "
            f"outputs (pr_url, pr_number={pr_number}) once the PR reports "
            "mergeStateStatus=CLEAN."
        )

    if verification.failing_checks:
        names = ", ".join(
            f"{c.get('name', '?')}={c.get('state') or c.get('bucket')}"
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

    worktree_path: Path | None = None
    # Pessimistic default: if we never got far enough to check, assume the
    # branch was pre-existing so cleanup never clobbers unrelated state.
    branch_preexisted = True

    try:
        # Get issue details
        issue = await github.get_issue(repo, issue_number)

        # Post claim comment so collaborators can see the agent picked it up.
        # Skip if a previous attempt already left the marker — keeps it idempotent
        # across retries / resumed sessions.
        existing_comments = issue.get("comments") or []
        already_claimed = any(
            AGENT_CLAIM_MARKER in (c.get("body") or "")
            for c in existing_comments
        )
        if not already_claimed:
            await github.comment_on_issue(
                repo=repo,
                issue_number=issue_number,
                body=AGENT_CLAIM_COMMENT,
            )

        # Snapshot branch ownership BEFORE we try to create it. If the ref
        # already exists in the bare repo, it came from another run (possibly
        # a prior DONE session whose PR is still open) and we must not touch
        # it. If it does not exist here, any ref we see later belongs to us —
        # even if `git worktree add -b` fails partway through and leaves the
        # ref behind without a usable worktree.
        await worktree.ensure_bare_repo(repo)
        branch_preexisted = await worktree.branch_exists_locally(repo, branch_name)
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

        # Cleanup rules:
        #   DONE    -> remove worktree, keep branch (the open PR references it)
        #   BLOCKED -> keep both (user may resume the session)
        #   FAILED  -> remove worktree AND delete branch so the next retry can
        #              re-create `fix/issue-<n>` cleanly — BUT only if the
        #              branch did not pre-exist (we own it) and it was never
        #              pushed (no recoverable work on origin).
        if result.success:
            worktree.remove_context_symlink(worktree_path)
            await worktree.remove_worktree(repo, session_id)
        elif not result.blocked:
            worktree.remove_context_symlink(worktree_path)
            await worktree.remove_worktree(repo, session_id)
            if not branch_preexisted and not await worktree.branch_exists_on_remote(
                repo, branch_name
            ):
                await worktree.delete_branch(repo, branch_name)

        return result

    except Exception as e:
        state_db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? WHERE id = ?",
            ("failed", f"Error: {e}", int(time.time()), session_id),
        )
        state_db.commit()

        # Best-effort cleanup so a retry isn't blocked by leftover state. Only
        # touch the branch if it didn't pre-exist (we own it) AND origin has
        # no copy (no recoverable work to orphan). Covers partial failures of
        # `git worktree add -b` that create the ref before the directory setup
        # crashes.
        if worktree_path is not None:
            try:
                worktree.remove_context_symlink(worktree_path)
            except Exception:
                pass
        # Always attempt remove_worktree + prune — handles the case where
        # `git worktree add -b` registered worktree metadata before the dir
        # step failed, leaving worktree_path unassigned but metadata in the
        # bare repo that would prevent the branch from being deleted.
        try:
            await worktree.remove_worktree(repo, session_id)
        except Exception:
            pass
        if not branch_preexisted:
            try:
                has_remote = await worktree.branch_exists_on_remote(repo, branch_name)
            except Exception:
                has_remote = True
            if not has_remote:
                try:
                    await worktree.delete_branch(repo, branch_name)
                except Exception:
                    pass

        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Error processing issue #{issue_number}",
            error=str(e),
        )

    finally:
        state_db.release_lock(repo, session_id)
