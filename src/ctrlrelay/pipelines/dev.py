"""Dev pipeline for issue-to-PR workflow."""

from __future__ import annotations

import asyncio
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

# Reacquire policy for the repo lock after the unlocked verification phase.
# Verification releases the lock because CI polling doesn't touch git; when
# we need to run `request_fix` (which spawns claude + writes worktree) we
# must get it back. Contention here is NOT necessarily brief — a peer
# session may be running its own full claude pass on another issue in the
# same repo, which can take tens of minutes. A short retry budget would
# spuriously fail healthy same-repo parallelism; bound the wait at roughly
# one hour, long enough to outlast a normal peer run and short enough to
# escape a stuck/SIGKILL'd peer whose lock row is lingering.
# Tests monkey-patch these to speed contention scenarios without changing
# the call sites.
_REACQUIRE_LOCK_ATTEMPTS = 720
_REACQUIRE_LOCK_SLEEP_SECONDS = 5.0
# Log progress periodically so an operator watching can see we're alive
# and waiting, not hung. At 5s cadence this fires ~every 5 minutes.
_REACQUIRE_PROGRESS_LOG_EVERY = 60

# Post-verify CLEANUP (worktree rm, branch delete) is best-effort — if
# a peer grabs the lock while we're polling CI and we can't get it back
# fast, skipping cleanup is fine (next run / operator reclaims). Don't
# use the fix-path budget here: run_poll_loop awaits handlers serially,
# so waiting an hour for cleanup would stall the whole poll cycle AND
# delay the merge-watcher spawn that happens after run_dev_issue
# returns. Three quick retries cover a fleeting hiccup; anything longer
# belongs elsewhere.
_REACQUIRE_CLEANUP_ATTEMPTS = 3
_REACQUIRE_CLEANUP_SLEEP_SECONDS = 1.0

# Two distinct error messages so callers can tell which phase of the
# pipeline hit contention:
#   INITIAL  — nothing started, safe to retry from scratch (cli un-marks
#              the issue so the next poll picks it up).
#   DURING_VERIFY — a PR already exists; re-running from scratch would
#                   launch a duplicate dev pass against the open PR.
#                   cli leaves the issue marked seen and only spawns the
#                   PR merge watcher so the existing PR still auto-closes
#                   on merge.
_LOCK_CONTENDED_ERROR = "Repository locked by another session"
_LOCK_CONTENDED_DURING_VERIFY_ERROR = (
    "Repo lock reacquire contended during PR verification"
)


class _RepoLockHandle:
    """Tracks whether this session currently holds the repo lock and
    allows release/reacquire around phases that don't need exclusive git
    access (PR CI verification is pure `gh` polling — see issue #29).
    The ``finally`` block at the end of ``run_dev_issue`` calls
    ``release`` regardless; calling it twice is a no-op because
    ``StateDB.release_lock`` is idempotent (deletes rows, rowcount=0 the
    second time). Tracking ``held`` locally lets the pipeline decide
    whether worktree/branch cleanup is safe to attempt on error paths."""

    __slots__ = ("state_db", "repo", "session_id", "held")

    def __init__(self, state_db: StateDB, repo: str, session_id: str) -> None:
        self.state_db = state_db
        self.repo = repo
        self.session_id = session_id
        # Starts False; run_dev_issue wraps its initial acquire call and
        # flips this to True on success.
        self.held = False

    def release(self) -> None:
        """Release the lock if we hold it. Idempotent and safe to call
        from a ``finally`` block that may run before acquire ever
        succeeded.

        If the underlying DELETE raises (e.g. transient
        ``database is locked`` from SQLite), we keep ``held=True`` so
        subsequent release attempts retry. Flipping held=False after a
        failed DELETE would wedge the repo: our stale row stays in
        ``repo_locks`` rejecting peer acquires while our own code
        believes the lock is free and tries to proceed.

        The exception is logged and swallowed (release must not leak
        exceptions from a finally path, where it would mask the
        original error the caller was unwinding)."""
        if not self.held:
            return
        try:
            self.state_db.release_lock(self.repo, self.session_id)
        except Exception as e:
            log_event(
                _logger,
                "dev.lock.release_failed",
                session_id=self.session_id,
                repo=self.repo,
                reason=type(e).__name__,
                error=str(e)[:200],
            )
            # Don't flip held — a later call (outer finally, etc.) will
            # retry the DELETE once the transient condition clears.
            return
        self.held = False

    async def reacquire(
        self,
        *,
        attempts: int | None = None,
        sleep_seconds: float | None = None,
    ) -> bool:
        """Try to re-grab the lock after a release. Returns True on
        success, False if contention persists past the retry budget.

        Retry budget: ``attempts`` INSERT tries separated by
        ``sleep_seconds`` of sleep. Default 720×5s ≈ 1 hour. That's
        long enough to outlast a peer running its own full dev pass
        on another issue in the same repo (claude + CI can take
        tens of minutes), and short enough to escape a stuck or
        SIGKILL'd peer whose lock row never got cleaned up.

        Defaults resolve at call time (``None`` sentinel) so tests can
        monkey-patch the module constants to speed up contention
        scenarios without threading kwargs through every caller.
        """
        if self.held:
            return True
        effective_attempts = (
            _REACQUIRE_LOCK_ATTEMPTS if attempts is None else attempts
        )
        effective_sleep = (
            _REACQUIRE_LOCK_SLEEP_SECONDS
            if sleep_seconds is None
            else sleep_seconds
        )
        for attempt in range(max(1, effective_attempts)):
            if self.state_db.acquire_lock(self.repo, self.session_id):
                self.held = True
                return True
            # Surface a heartbeat every N attempts so an operator
            # tailing logs during a long peer hold can tell the
            # session is waiting, not wedged.
            if attempt > 0 and attempt % _REACQUIRE_PROGRESS_LOG_EVERY == 0:
                log_event(
                    _logger,
                    "dev.lock.reacquire_waiting",
                    session_id=self.session_id,
                    repo=self.repo,
                    attempt=attempt,
                    max_attempts=effective_attempts,
                )
            if attempt < effective_attempts - 1:
                await asyncio.sleep(effective_sleep)
        return False

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
    lock_handle: _RepoLockHandle | None = None,
) -> PipelineResult:
    """Loop: verify CI+mergeability, ask Claude to fix, re-verify.

    Lock discipline (issue #29): the verify phase is pure `gh` polling
    and holds no git state, so callers pass a ``lock_handle`` whose
    repo lock we release before each ``verifier.verify`` call and
    reacquire before each ``request_fix`` call (which spawns claude
    and writes to the worktree). This lets peer sessions targeting
    the same repo run their own git-op phases while we wait on CI.

    If reacquire fails after the retry budget, we surface a clear
    failed result rather than silently running ``request_fix``
    without the lock — another session may be mutating the shared
    bare repo and our claude process could corrupt its state.
    Cancellation during the unlocked phase is safe: the lock_handle
    tracks ``held=False`` so the outer ``finally`` no-ops its release.
    """
    pr_number_raw = result.outputs.get("pr_number")
    if pr_number_raw is None:
        return result
    pr_number = int(pr_number_raw)

    async def _run_verify() -> VerificationResult:
        """Release the lock around the pure-gh polling window, then
        hand the lock back to the caller on return. Reacquire is
        deferred to just before the fix path so peer sessions get the
        widest possible window."""
        if lock_handle is not None:
            lock_handle.release()
        try:
            return await verifier.verify(ctx.repo, pr_number)
        except asyncio.CancelledError:
            # Intentionally leave held=False so the outer finally's
            # release_lock is a no-op; the row is already gone. Re-raise
            # to let the cancellation propagate.
            raise

    verification = await _run_verify()
    # If CI is simply slow (timed_out) we hand the PR off rather than asking
    # Claude to "fix" something that isn't broken.
    if verification.timed_out:
        return result
    attempts = 0
    while not verification.ready and attempts < max_attempts:
        # Reacquire before request_fix — claude spawns inside the
        # worktree and pushes to the shared bare repo. If we can't
        # get the lock back in a reasonable window, bail cleanly
        # rather than racing another session.
        if lock_handle is not None and not lock_handle.held:
            reacquired = await lock_handle.reacquire()
            if not reacquired:
                log_event(
                    _logger,
                    "dev.verify.lock_reacquire_contended",
                    session_id=result.session_id,
                    repo=ctx.repo,
                    pr_number=pr_number,
                    attempts=_REACQUIRE_LOCK_ATTEMPTS,
                )
                return PipelineResult(
                    success=False,
                    session_id=result.session_id,
                    summary=(
                        f"PR #{pr_number} verification found work to do "
                        "but could not re-acquire repo lock after "
                        f"{_REACQUIRE_LOCK_ATTEMPTS} attempts"
                    ),
                    error=_LOCK_CONTENDED_DURING_VERIFY_ERROR,
                    outputs=result.outputs,
                )

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
        verification = await _run_verify()
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

    lock = _RepoLockHandle(state_db, repo, session_id)
    if not state_db.acquire_lock(repo, session_id):
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Could not acquire lock for {repo}",
            error="Repository locked by another session",
        )
    lock.held = True

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
                # Transport failed (bridge down, timeout, etc.). The
                # session IS still blocked — we just couldn't reach the
                # operator. Preserve blocked=True so the outer
                # persistence-on-blocked branch fires and writes a
                # pending_resumes row. Operator replying later via
                # Telegram then routes through the sweeper. Setting
                # blocked=False here (the prior behavior) wedged the
                # session permanently — codex P1.
                result = PipelineResult(
                    success=False,
                    blocked=True,
                    session_id=session_id,
                    summary=f"Blocked session deferred (transport): {e}",
                    error=str(e),
                    question=question,
                    outputs=result.outputs,
                )
                break
            rounds += 1
            result = await pipeline.resume(ctx, answer)

        # Verify PR is green & conflict-free before handing off. Resume the
        # session with a fix request if either is broken, up to max_fix_attempts.
        #
        # Issue #29: the lock handle is passed in so the verifier releases
        # the repo lock during its CI-polling window (can be up to 30 min)
        # and reacquires before any request_fix. Peer sessions targeting
        # the same repo can now run their own git-op phases while we wait.
        if result.success and result.outputs.get("pr_number") is not None:
            verifier = pr_verifier or PRVerifier(github=github)
            result = await _verify_and_fix_pr(
                pipeline=pipeline,
                ctx=ctx,
                result=result,
                verifier=verifier,
                max_attempts=max_fix_attempts,
                lock_handle=lock,
            )

        # Reacquire the lock for the post-verify cleanup phase (remove
        # worktree / delete branch). Uses the SHORT cleanup budget, not
        # the fix-path budget: cleanup is best-effort, and run_poll_loop
        # awaits handlers serially — blocking here for an hour would
        # stall the whole poll cycle AND delay the PR merge watcher
        # spawn (which happens in cli.handle_issue after we return).
        # If we can't get the lock back in a few seconds, log and skip
        # cleanup; the session still returns its verified result.
        if not lock.held:
            reacquired = await lock.reacquire(
                attempts=_REACQUIRE_CLEANUP_ATTEMPTS,
                sleep_seconds=_REACQUIRE_CLEANUP_SLEEP_SECONDS,
            )
            if not reacquired:
                log_event(
                    _logger,
                    "dev.cleanup.lock_reacquire_contended",
                    session_id=session_id,
                    repo=repo,
                    issue_number=issue_number,
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
        #
        # Cleanup mutates the shared bare repo (worktree metadata, branch
        # refs) so it runs only when we hold the lock. If reacquire above
        # failed, skip it and let the next retry / operator reclaim — the
        # verified result is already final either way.
        if lock.held:
            if result.success:
                worktree.remove_context_symlink(worktree_path)
                await worktree.remove_worktree(repo, session_id)
            elif not result.blocked:
                worktree.remove_context_symlink(worktree_path)
                await worktree.remove_worktree(repo, session_id)
                if (
                    not branch_preexisted
                    and not await worktree.branch_exists_on_remote(
                        repo, branch_name
                    )
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
        # crashes. If we're mid-verify the lock may be released — try to get
        # it back for the cleanup. Uses the SHORT cleanup budget (not the
        # fix-path one): a transient exception inside verifier.verify raising
        # through while a peer holds the lock would otherwise stall the
        # serial poll cycle for the full hour-long fix budget just to
        # report the failure.
        if not lock.held:
            try:
                await lock.reacquire(
                    attempts=_REACQUIRE_CLEANUP_ATTEMPTS,
                    sleep_seconds=_REACQUIRE_CLEANUP_SLEEP_SECONDS,
                )
            except Exception:
                pass
        if lock.held:
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
                    has_remote = await worktree.branch_exists_on_remote(
                        repo, branch_name
                    )
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
        # Safe even if we already released inside `_verify_and_fix_pr` —
        # the handle's `release` is idempotent (no-op when held=False)
        # and StateDB.release_lock is itself idempotent (DELETE with
        # rowcount=0 is a valid outcome). This also covers
        # CancelledError: if cancellation arrives during the unlocked
        # verify window, held is already False, so no stray DELETE runs.
        lock.release()


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

    lock = _RepoLockHandle(state_db, repo, session_id)
    if not state_db.acquire_lock(repo, session_id):
        return PipelineResult(
            success=False,
            session_id=session_id,
            summary=f"Could not acquire lock for {repo} to resume dev",
            error="Repository locked by another session",
        )
    lock.held = True

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
                # Keep blocked=True so outer persistence branch fires
                # and the sweeper can pick this up again when the
                # operator's next Telegram reply arrives. Same
                # reasoning as run_dev_issue's loop — transport
                # failure is not the same as a resolved session.
                result = PipelineResult(
                    success=False,
                    blocked=True,
                    session_id=session_id,
                    summary=(
                        f"Blocked session deferred during resume "
                        f"(transport): {e}"
                    ),
                    error=str(e),
                    question=question,
                    outputs=result.outputs,
                )
                break
            rounds += 1
            result = await pipeline.resume(ctx, next_answer)

        # Verify PR once the agent says DONE, matching run_dev_issue.
        # The lock handle is threaded through so the verifier releases
        # the repo lock during its CI-polling window (issue #29).
        if result.success and result.outputs.get("pr_number") is not None:
            verifier = pr_verifier or PRVerifier(github=github)
            result = await _verify_and_fix_pr(
                pipeline=pipeline,
                ctx=ctx,
                result=result,
                verifier=verifier,
                max_attempts=max_fix_attempts,
                lock_handle=lock,
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
        #
        # ``lock.release`` is idempotent, so this is a no-op if the
        # verify phase already released and never reacquired (CI-only
        # path with no fix attempts + no cleanup work). Matches issue
        # #29's release-during-verify semantics.
        try:
            lock.release()
        except Exception as lock_exc:
            log_event(
                _logger,
                "dev.resume.cleanup.lock_release_failed",
                session_id=session_id,
                repo=repo,
                error_type=type(lock_exc).__name__,
                error=str(lock_exc)[:200],
            )
