"""Dev pipeline for issue-to-PR workflow."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctrlrelay.core.checkpoint import CheckpointStatus
from ctrlrelay.core.dispatcher import AgentAdapter, SessionResult
from ctrlrelay.core.github import GitHubCLI
from ctrlrelay.core.obs import get_logger, hash_text, log_event
from ctrlrelay.core.pr_verifier import PRVerifier, VerificationResult
from ctrlrelay.core.state import StateDB
from ctrlrelay.core.worktree import WorktreeManager
from ctrlrelay.dashboard.client import DashboardClient, EventPayload
from ctrlrelay.pipelines.base import PipelineContext, PipelineResult
from ctrlrelay.transports.base import Transport

DEFAULT_MAX_FIX_ATTEMPTS = 3
DEFAULT_MAX_BLOCKED_ROUNDS = 5

AGENT_CLAIM_MARKER = "<!-- ctrlrelay:claimed -->"
AGENT_CLAIM_COMMENT = (
    "CTRLRelay is working on this issue. A PR will be opened for review.\n\n"
    f"{AGENT_CLAIM_MARKER}"
)

_logger = get_logger("pipeline.dev")


def _question_for_persist(session_id: str, result: PipelineResult) -> str:
    """Return a non-empty question string for pending_resumes storage.

    Mirrors the synthesized prompt the in-process BLOCKED loop uses
    when ``result.question`` is empty — without this, a BLOCKED exit
    with no question text would skip persistence and the session
    would become unresumable via Telegram."""
    q = (result.question or "").strip()
    if q:
        return q
    return (
        f"Session {session_id} is blocked but did not include a "
        "question. Reply with guidance to resume."
    )


@dataclass
class DevPipeline:
    """Dev pipeline for implementing issues and opening PRs."""

    dispatcher: AgentAdapter
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

        result = await self._spawn(ctx, prompt, resume=False)
        return self._session_to_result(result)

    async def resume(self, ctx: PipelineContext, answer: str) -> PipelineResult:
        """Resume blocked dev session with user answer."""
        prompt = f"User answered: {answer}\n\nContinue from where you left off."

        # Resume uses Claude's own session UUID (captured on the first spawn
        # and persisted to state_db). Passing our composite id here makes
        # `claude --resume` reject the call on v2.0.x+ CLI builds.
        resume_uuid = self.state_db.get_agent_session_id(ctx.session_id)

        log_event(
            _logger,
            "dev.session.resumed",
            session_id=ctx.session_id,
            repo=ctx.repo,
            issue_number=ctx.issue_number,
            pipeline=self.name,
            resume_session_id=resume_uuid,
            answer_length=len(answer),
            answer_hash=hash_text(answer),
        )

        result = await self._spawn(ctx, prompt, resume=True)
        return self._session_to_result(result)

    async def request_fix(
        self, ctx: PipelineContext, fix_instructions: str
    ) -> PipelineResult:
        """Resume the session with a fix request (failing CI or merge conflict)."""
        result = await self._spawn(ctx, fix_instructions, resume=True)
        return self._session_to_result(result)

    async def _spawn(
        self,
        ctx: PipelineContext,
        prompt: str,
        *,
        resume: bool,
    ) -> SessionResult:
        """Centralized dispatcher call: looks up the agent UUID on resume,
        persists any newly-captured UUID to state_db."""
        resume_uuid: str | None = None
        if resume:
            # If state_db has no stored UUID (session predates the capture, or
            # first spawn didn't emit JSON) we fall back to a fresh session —
            # better than hard-failing with `claude --resume <composite-id>`.
            resume_uuid = self.state_db.get_agent_session_id(ctx.session_id)

        result = await self.dispatcher.spawn_session(
            session_id=ctx.session_id,
            prompt=prompt,
            working_dir=ctx.worktree_path,
            state_file=ctx.state_file,
            resume_session_id=resume_uuid,
        )

        if result.agent_session_id:
            try:
                self.state_db.set_agent_session_id(
                    ctx.session_id, result.agent_session_id
                )
            except Exception:
                # state_db write failures must not mask the session result.
                pass

        return result

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
   - Wait for CI by running EXACTLY this command (do NOT improvise bash):
     `ctrlrelay ci wait --pr <PR> --repo {repo} --timeout 600`
     Exit codes: 0 = all checks passed, 1 = a check failed (investigate,
     fix, push, then re-run the wait), 2 = hard timeout while CI is still
     pending (treat as acceptable — hand off and let the orchestrator
     re-verify). Do NOT write your own `until` / `while` loops around
     `gh pr checks`; those have been miswritten in the past (inverted
     semantics, pipes swallowing exit codes) and burned the whole session
     timeout on PRs that were already green.
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
    # If CI is simply slow (timed_out) we hand the PR off rather than asking
    # Claude to "fix" something that isn't broken.
    if verification.timed_out:
        return result
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
        if verification.timed_out:
            # Same rule after a fix round: slow CI isn't a Claude task.
            return result

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
    dispatcher: AgentAdapter,
    github: GitHubCLI,
    worktree: WorktreeManager,
    dashboard: DashboardClient | None,
    state_db: StateDB,
    transport: Transport | None,
    contexts_dir: Path,
    max_fix_attempts: int = DEFAULT_MAX_FIX_ATTEMPTS,
    max_blocked_rounds: int = DEFAULT_MAX_BLOCKED_ROUNDS,
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
        state_file = worktree_path / ".ctrlrelay" / "state.json"
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

        # BLOCKED loop: if Claude needs input, post the question to the
        # transport (Telegram), wait for the operator's reply, and resume
        # the session. Loop until Claude signals DONE/FAILED or we run out
        # of trips. Each round-trip is capped at the transport's own
        # timeout (see Transport.ask signature — default 300s); the total
        # is bounded by max_blocked_rounds.
        rounds = 0
        while (
            result.blocked
            and transport is not None
            and rounds < max_blocked_rounds
        ):
            question = (result.question or "").strip() or (
                f"Session {session_id} is blocked but did not include a "
                "question. Reply with guidance to resume."
            )
            try:
                answer = await transport.ask(
                    question,
                    session_id=session_id,
                    repo=repo,
                    issue_number=issue_number,
                )
            except Exception as e:
                # Transport failed (bridge down, timeout, etc.) — give up
                # cleanly and fall through to the normal blocked cleanup.
                result = PipelineResult(
                    success=False,
                    blocked=False,
                    session_id=session_id,
                    summary=f"Blocked session abandoned: {e}",
                    error=str(e),
                    outputs=result.outputs,
                )
                break
            rounds += 1
            result = await pipeline.resume(ctx, answer)

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

        # Persist BLOCKED so a Telegram reply arriving AFTER this session
        # exits (max_blocked_rounds exhausted, or transport unavailable
        # during the session) still routes to a resume. The per-minute
        # pending_resume_sweeper drains these rows by calling
        # resume_dev_from_pending below.
        if result.blocked:
            try:
                state_db.add_pending_resume(
                    session_id=session_id,
                    pipeline="dev",
                    repo=repo,
                    question=_question_for_persist(session_id, result),
                )
            except Exception as e:
                log_event(
                    _logger,
                    "dev.pending_resume.insert_failed",
                    session_id=session_id,
                    repo=repo,
                    issue_number=issue_number,
                    error_type=type(e).__name__,
                    error=str(e)[:200],
                )

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


async def resume_dev_from_pending(
    session_id: str,
    repo: str,
    answer: str,
    branch_template: str,
    dispatcher: AgentAdapter,
    github: GitHubCLI,
    worktree: WorktreeManager,
    dashboard: DashboardClient | None,
    state_db: StateDB,
    transport: Transport | None,
    contexts_dir: Path,
    max_blocked_rounds: int = DEFAULT_MAX_BLOCKED_ROUNDS,
    max_fix_attempts: int = DEFAULT_MAX_FIX_ATTEMPTS,
    pr_verifier: PRVerifier | None = None,
) -> PipelineResult:
    """Resume a BLOCKED dev session using an answer that arrived via
    Telegram after ``run_dev_issue`` exited.

    Unlike secops, the dev pipeline keeps its worktree + branch alive on
    BLOCKED exits (so resume doesn't have to re-clone/re-apply). We try
    to reuse the stored worktree_path first; if it's missing (manual
    cleanup, daemon restart lost the mount, etc.) we fall back to
    ``create_worktree_with_new_branch`` which handles existing-branch
    reuse.

    Caller is responsible for flipping the pending_resumes row
    ``resumed_at`` after this returns — except in the lock-contention
    case, which is retryable; we surface it via
    ``error == "Repository locked by another session"``.
    """
    session_row = state_db.get_session_row(session_id)
    if session_row is None:
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=(
                f"Cannot resume dev session {session_id}: row missing "
                "from sessions table"
            ),
            error="session_row_missing",
        )
    issue_number = session_row.get("issue_number")
    if issue_number is None:
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=(
                f"Cannot resume dev session {session_id}: issue_number "
                "missing on sessions row"
            ),
            error="session_issue_number_missing",
        )

    branch_name = branch_template.replace("{n}", str(issue_number))

    if not state_db.acquire_lock(repo, session_id):
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Could not acquire lock for {repo} to resume dev",
            error="Repository locked by another session",
        )

    worktree_path: Path | None = None

    try:
        await worktree.ensure_bare_repo(repo)

        # Prefer re-using the existing worktree left alive by the BLOCKED
        # cleanup rule ("BLOCKED → keep both"). That preserves the
        # session's in-progress commits / uncommitted work without
        # another fetch round-trip.
        stored_worktree = session_row.get("worktree_path")
        if stored_worktree:
            candidate = Path(stored_worktree)
            if candidate.exists() and (candidate / ".git").exists():
                worktree_path = candidate

        if worktree_path is None:
            worktree_path = await worktree.create_worktree_with_new_branch(
                repo=repo,
                session_id=session_id,
                new_branch=branch_name,
            )

        context_path = contexts_dir / repo.replace("/", "-") / "CLAUDE.md"
        if context_path.exists():
            worktree.symlink_context(worktree_path, context_path)

        state_file = worktree_path / ".ctrlrelay" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)

        # Fetch issue context again so the resumed prompt has the same
        # metadata the first run did — avoids the agent hallucinating
        # details from stale state.
        issue = await github.get_issue(repo, int(issue_number))

        ctx = PipelineContext(
            session_id=session_id,
            repo=repo,
            worktree_path=worktree_path,
            context_path=context_path,
            state_file=state_file,
            issue_number=int(issue_number),
            extra={
                "issue_title": issue.get("title", ""),
                "issue_body": issue.get("body", ""),
                "branch_name": branch_name,
            },
        )

        state_db.execute(
            "UPDATE sessions SET status = ?, ended_at = NULL WHERE id = ?",
            ("running", session_id),
        )
        state_db.commit()

        pipeline = DevPipeline(
            dispatcher=dispatcher,
            github=github,
            worktree=worktree,
            dashboard=dashboard,
            state_db=state_db,
            transport=transport,
        )

        # Feed the operator's answer as the resume prompt. If the agent
        # re-blocks (common for multi-step decisions), the same
        # in-process BLOCKED loop that run_dev_issue uses kicks in here
        # — transport.ask() waits on the live socket — so the operator
        # can have a back-and-forth exchange on the resume path too.
        result = await pipeline.resume(ctx, answer)

        rounds = 0
        while (
            result.blocked
            and transport is not None
            and rounds < max_blocked_rounds
        ):
            question = (result.question or "").strip() or (
                f"Session {session_id} is blocked but did not include a "
                "question. Reply with guidance to resume."
            )
            try:
                next_answer = await transport.ask(
                    question,
                    session_id=session_id,
                    repo=repo,
                    issue_number=int(issue_number),
                )
            except Exception as e:
                result = PipelineResult(
                    success=False,
                    blocked=False,
                    session_id=session_id,
                    summary=f"Blocked session abandoned during resume: {e}",
                    error=str(e),
                    outputs=result.outputs,
                )
                break
            rounds += 1
            result = await pipeline.resume(ctx, next_answer)

        # Verify PR once the agent says DONE, matching run_dev_issue.
        if result.success and result.outputs.get("pr_number") is not None:
            verifier = pr_verifier or PRVerifier(github=github)
            result = await _verify_and_fix_pr(
                pipeline=pipeline,
                ctx=ctx,
                result=result,
                verifier=verifier,
                max_attempts=max_fix_attempts,
            )

        status = "done" if result.success else (
            "blocked" if result.blocked else "failed"
        )
        state_db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? "
            "WHERE id = ?",
            (status, result.summary, int(time.time()), session_id),
        )
        state_db.commit()

        if result.blocked:
            try:
                state_db.add_pending_resume(
                    session_id=session_id,
                    pipeline="dev",
                    repo=repo,
                    question=_question_for_persist(session_id, result),
                )
            except Exception as e:
                log_event(
                    _logger,
                    "dev.pending_resume.reblock_insert_failed",
                    session_id=session_id,
                    repo=repo,
                    issue_number=issue_number,
                    error_type=type(e).__name__,
                    error=str(e)[:200],
                )

        return result

    except Exception as e:
        state_db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? "
            "WHERE id = ?",
            ("failed", f"Resume error: {e}", int(time.time()), session_id),
        )
        state_db.commit()
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Error resuming dev session {session_id} on {repo}",
            error=str(e),
        )

    finally:
        # Intentionally leave the worktree in place — matches
        # run_dev_issue's "BLOCKED → keep both" contract and makes the
        # worktree available for a third resume if the second one
        # re-blocks. A follow-up can add teardown for the DONE/FAILED
        # outcomes (small disk leak, not a correctness issue; operator
        # can run `git worktree prune` in the bare repo to reclaim).
        try:
            state_db.release_lock(repo, session_id)
        except Exception as lock_exc:
            log_event(
                _logger,
                "dev.resume.cleanup.lock_release_failed",
                session_id=session_id,
                repo=repo,
                error_type=type(lock_exc).__name__,
                error=str(lock_exc)[:200],
            )
