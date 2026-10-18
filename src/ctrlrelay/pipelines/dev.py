"""Dev pipeline for issue-to-PR workflow."""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctrlrelay.core.checkpoint import CheckpointStatus
from ctrlrelay.core.code_review import (
    extract_verdict,
    format_review_comment,
    run_code_review,
)
from ctrlrelay.core.dispatcher import AgentAdapter, SessionResult
from ctrlrelay.core.github import GitHubCLI
from ctrlrelay.core.obs import get_logger, hash_text, log_event
from ctrlrelay.core.pr_verifier import PRVerifier, VerificationResult
from ctrlrelay.core.state import StateDB
from ctrlrelay.core.worktree import WorktreeManager
from ctrlrelay.dashboard.client import DashboardClient, EventPayload
from ctrlrelay.pipelines.base import (
    PipelineContext,
    PipelineResult,
    failure_text,
)
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

# Release-lock retry policy for transient SQLite errors. The final
# release() call in run_dev_issue's outer finally is the last chance
# to clean up; if that raises and we silently returned, the repo_locks
# row would linger forever. Short retry loop handles the common
# transient case; exhausted retries emit a loud wedged event so ops
# can reclaim manually.
_RELEASE_RETRY_ATTEMPTS = 3
_RELEASE_RETRY_SLEEP_SECONDS = 0.5

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

        Retries the DELETE a few times on transient failures (SQLite
        ``database is locked``, etc.). The outer ``finally`` is the
        last-chance call — if THAT raised and we silently returned,
        the ``repo_locks`` row would linger forever, wedging every
        future acquire on this repo until manual DB cleanup.

        After exhausting retries we emit a ``dev.lock.release_wedged``
        critical-level event naming the repo + session so an operator
        can reclaim the lock row. held is left True so an outer
        retry (if any) still has a chance.

        The inner retry's exception is swallowed only after the
        wedged event fires — release must not leak exceptions from a
        finally path where it would mask the caller's original error."""
        if not self.held:
            return
        last_exc: Exception | None = None
        for attempt in range(1, _RELEASE_RETRY_ATTEMPTS + 1):
            try:
                self.state_db.release_lock(self.repo, self.session_id)
            except Exception as e:
                last_exc = e
                log_event(
                    _logger,
                    "dev.lock.release_failed",
                    session_id=self.session_id,
                    repo=self.repo,
                    attempt=attempt,
                    max_attempts=_RELEASE_RETRY_ATTEMPTS,
                    reason=type(e).__name__,
                    error=str(e)[:200],
                )
                if attempt < _RELEASE_RETRY_ATTEMPTS:
                    time.sleep(_RELEASE_RETRY_SLEEP_SECONDS)
                continue
            self.held = False
            return
        # All retries exhausted. The row is presumed still present;
        # alert loudly so it doesn't silently wedge the repo.
        log_event(
            _logger,
            "dev.lock.release_wedged",
            session_id=self.session_id,
            repo=self.repo,
            reason=type(last_exc).__name__ if last_exc else "unknown",
            error=str(last_exc)[:200] if last_exc else "",
            operator_action=(
                "DELETE FROM repo_locks WHERE repo = ? AND session_id = ? "
                "manually, or restart the poller after confirming the "
                "session is not live."
            ),
        )

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

# The claim above is posted BEFORE the worker starts, and that stays:
# it is what stops two workers taking one issue. #204 is about the other
# end of it. On `AInvirion/zzsites#853` the claim went up two seconds
# before the worker started, the worker exited 1 four seconds later, and
# the beta tester's own ticket said a pull request was coming for
# EIGHTEEN HOURS. It was noticed only because a person went looking for
# the pull request and could not find it.
#
# From outside the machine the issue is the only visible surface, and
# `ctrlrelay:auto` + a claim comment + no branch is indistinguishable
# from work in progress. A reader cannot tell a dispatch that is running
# from one that died in four seconds.
#
# A marker of its own so a later run — or a person — can find the
# retraction, the same way `AGENT_CLAIM_MARKER` is findable. The claim
# comment is NOT deleted: deleting it would lose the record that the
# issue was taken, and `format_issue_comments` already drops both
# markers from the prompt so neither becomes agent input.
AGENT_OUTCOME_MARKER = "<!-- ctrlrelay:dispatch-failed -->"

# Every marker that identifies a comment the relay wrote itself. Read by
# `format_issue_comments` so none of our own prose is ever fed back to an
# agent as though an operator had written it. One tuple, so adding a
# marker is one edit rather than one edit plus remembering this filter.
OUR_COMMENT_MARKERS = (AGENT_CLAIM_MARKER, AGENT_OUTCOME_MARKER)


def claim_is_live(comments: list[dict[str, Any]] | None) -> bool:
    """Whether a claim stands on this issue with no retraction after it.

    Not "any comment carries the claim marker". The claim comment is
    deliberately never deleted, so after a reconciliation the marker is
    still there — and the retraction's own closing line invites a retry
    ("free to pick up... re-apply the automation label"). On that retry
    a marker-only test says the issue is already claimed, no fresh
    claim is posted, and the issue's LAST relay comment stays the
    retraction: "nothing is working on this issue", while a worker is
    running, and still after it succeeds, because success is announced
    on chat and not on the issue.

    For the beta tester, whose only surface is the issue, that is the
    eighteen-hour defect mirrored — so the question has to be which of
    the two markers came last.

    Comments arrive oldest-first from `gh issue view --json comments`,
    so the last marker wins.
    """
    live = False
    for comment in comments or []:
        body = comment.get("body") or ""
        if AGENT_CLAIM_MARKER in body:
            live = True
        elif AGENT_OUTCOME_MARKER in body:
            live = False
    return live


def claim_failure_comment(
    session_id: str,
    *,
    reason: str,
    exit_code: int | None,
    pr_number: int | None = None,
) -> str:
    """The comment that reconciles the claim with a failed outcome.

    Names the session id and the exit code because those are what
    make the row in `sessions` findable afterwards, and says plainly
    whether a pull request exists — a run that opened one and then
    failed to finish verifying it is a different situation for the
    reader than one that produced nothing, and the claim comment is
    equally wrong about both.

    Ends in what happens next. A retraction that only says "it
    failed" leaves a reader with the same question they started with.
    """
    if pr_number is not None:
        opening = (
            f"⚠️ **CTRLRelay did not finish this run.** Pull request "
            f"#{pr_number} was opened but the run ended before handover. "
            "This supersedes any claim above."
        )
        next_step = (
            f"Review PR #{pr_number} on its own merits — it is not "
            "waiting on anything from this relay."
        )
    else:
        opening = (
            "⚠️ **CTRLRelay did not finish this run, and no pull request "
            "was opened.** This supersedes any claim above — nothing is "
            "working on this issue."
        )
        next_step = (
            "This issue is free to pick up. Re-assign it (or re-apply the "
            "automation label) to have the relay try again."
        )

    exit_line = (
        f"- Agent exit code: `{exit_code}`"
        if exit_code is not None
        else "- Agent exit code: not recorded"
    )

    return (
        f"{opening}\n\n"
        f"- Session: `{session_id}`\n"
        f"{exit_line}\n"
        f"- Reason: {reason}\n\n"
        f"{next_step}\n\n"
        f"{AGENT_OUTCOME_MARKER}"
    )


async def reconcile_claim_on_failure(
    github: GitHubCLI,
    *,
    repo: str,
    issue_number: int,
    session_id: str,
    reason: str,
    exit_code: int | None,
    pr_number: int | None = None,
    claim_posted: bool = True,
) -> bool:
    """Post the retraction. Returns whether a comment was written.

    Best-effort by construction: this runs on a path that is already
    failing, and a `gh` call that throws here must not replace the
    pipeline's own failure with a different one — the caller would then
    report the wrong cause for the run. Every exception is logged and
    swallowed; a cancellation propagates.

    ``claim_posted=False`` skips the write. There is nothing to retract
    when no claim was ever made, and a bare "this failed" comment on an
    issue the relay never touched is noise on somebody else's ticket.
    The default is ``True`` because every current caller reaches this
    only after the claim block has run.
    """
    if not claim_posted:
        return False
    try:
        await github.comment_on_issue(
            repo=repo,
            issue_number=issue_number,
            body=claim_failure_comment(
                session_id,
                reason=reason,
                exit_code=exit_code,
                pr_number=pr_number,
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Logged loudly: the whole point of the ticket is that an
        # unretracted claim is invisible, so a retraction that failed
        # silently would reproduce it exactly.
        log_event(
            _logger,
            "dev.claim.retraction_failed",
            session_id=session_id,
            repo=repo,
            issue_number=issue_number,
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        return False
    log_event(
        _logger,
        "dev.claim.retracted",
        session_id=session_id,
        repo=repo,
        issue_number=issue_number,
        exit_code=exit_code,
        pr_number=pr_number,
    )
    return True

# Discussion budget for the prompt. Newest comments are kept because a
# redirect is far likelier to be the latest word than the first reply,
# and each body is clipped so one pasted log dump can't crowd out the
# rest of the thread.
MAX_PROMPT_COMMENTS = 20
MAX_PROMPT_COMMENT_CHARS = 2000

_logger = get_logger("pipeline.dev")


def format_issue_comments(
    comments: list[dict[str, Any]] | None,
    *,
    max_comments: int = MAX_PROMPT_COMMENTS,
    max_chars: int = MAX_PROMPT_COMMENT_CHARS,
) -> str:
    """Render the issue discussion for the dev prompt.

    The body states the problem as it was first filed. The thread is
    where an operator narrows, redirects or outright vetoes that plan,
    and nobody edits the body afterwards. A vulnq issue was filed asking
    for a client fix and then redirected in a comment to price up
    dropping the data source instead; the prompt carried only the body,
    so the agent spent a full session building the plan that had already
    been rejected in writing.

    Drops our own claim comment, and the failure retraction that #204
    added beside it — both are our noise, not the operator's, and the
    retraction is the more harmful of the two to leave in: a resumed
    or retried agent would read "CTRLRelay did not finish this run" as
    operator guidance about the work it is being asked to do.
    Keeps the newest ``max_comments`` but renders them oldest-first so
    the thread still reads in order.
    """
    if not comments:
        return "_No comments._"

    # Checked against the tuple rather than two `not in` clauses, so a
    # third marker added later is dropped by adding it to the tuple
    # instead of by remembering to extend a boolean expression.
    kept = [
        c for c in comments
        if not any(m in (c.get("body") or "") for m in OUR_COMMENT_MARKERS)
    ]
    if not kept:
        return "_No comments._"

    dropped = len(kept) - max_comments
    if dropped > 0:
        kept = kept[-max_comments:]

    blocks: list[str] = []
    if dropped > 0:
        blocks.append(
            f"_({dropped} earlier comment(s) omitted; read the issue on "
            f"GitHub if the thread below references them.)_"
        )

    for c in kept:
        author = (c.get("author") or {}).get("login") or "unknown"
        created = c.get("createdAt") or ""
        body = (c.get("body") or "").strip() or "_(empty)_"
        if len(body) > max_chars:
            body = body[:max_chars] + "\n_[...truncated]_"
        blocks.append(f"--- @{author} ({created}) ---\n{body}")

    return "\n\n".join(blocks)


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
        # The checkpoint contract is restated here, not just in the
        # initial prompt (#173). A resumed agent that is told "continue"
        # and nothing else can do its work, exit 0, write no state, and
        # be indistinguishable from one that crashed - which is what
        # happened on 2026-09-16, after three of four pull requests had
        # already been merged.
        contract = self._checkpoint_contract(
            str(ctx.state_file), ctx.session_id
        )
        prompt = (
            f"User answered: {answer}\n\n"
            "Continue from where you left off.\n\n"
            f"{contract}"
        )

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
            repo=ctx.repo,
            issue_number=ctx.issue_number,
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
        issue_comments = extra.get("issue_comments") or "_No comments._"
        branch_name = extra.get("branch_name", "")
        ci_wait_timeout_seconds = extra.get("ci_wait_timeout_seconds", 600)
        state_file_path = str(state_file) if state_file else "/tmp/state.json"

        return f"""You are working on issue #{issue_number} in repository {repo}.

**Issue Title:** {issue_title}

**Issue Body:**
{issue_body}

**Discussion:**
{issue_comments}

The body is the issue as first filed; the discussion is newer. Where a
comment narrows, redirects or rejects what the body asks for, the comment
wins — nobody goes back and edits the body after changing their mind in
the thread. If the discussion contradicts the body in a way you cannot
reconcile, signal BLOCKED and ask rather than picking one.

**Branch:** {branch_name}

Execute the following workflow:

1. Validate the issue still applies to the current codebase
2. If anything is unclear, signal BLOCKED (see below) to ask for clarification
3. Plan and implement the fix using TDD
4. Push the branch and open a PR that references the issue
5. Before signaling DONE, verify the PR is mergeable:
   - Wait for CI by running EXACTLY this command (do NOT improvise bash):
     `ctrlrelay ci wait --pr <PR> --repo {repo} --timeout {ci_wait_timeout_seconds}`
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

{self._checkpoint_contract(state_file_path, session_id)}"""

    def _checkpoint_contract(
        self, state_file_path: str, session_id: str
    ) -> str:
        """How the agent signals how it stopped.

        Extracted so the RESUME prompt can carry it too (#173). The
        initial prompt spent ~30 lines establishing this; the resume
        prompt was 58 characters and said only "continue", so a resumed
        agent could do its work, exit 0, write no state, and be
        indistinguishable from one that crashed.

        Measured case, 2026-09-16:
        secops-AInvirion-Product-Telemetry-bbb99925 resumed after an
        "Approved", ran 27.4s, exited 0 and wrote no checkpoint - having
        in fact merged three of four pull requests. The operator was told
        the resume failed, which was wrong in both directions: most of
        the work had landed, and the one thing outstanding was never
        named.

        Per-pipeline rather than shared, because the three contracts
        differ in substance and not only wording: the dev DONE block
        carries pr_url and pr_number outputs, and each names different
        conditions for DONE. One shared block would have quietly changed
        what two of them promise.
        """
        return f"""## Signaling Completion

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
                # No manufactured fallback. An empty stderr means there
                # is nothing to add, and `failure_text` then reports the
                # summary above plus the exit code — which is the whole
                # diagnosis, because exiting 0 without writing state is
                # a different fault from crashing (#174).
                # `or ""` before `.strip()`: the expression this
                # replaced tolerated a None stderr, and dropping that
                # tolerance would raise AttributeError *before* the
                # failure alert is sent - turning a reportable failure
                # into a silent crash, which is this ticket's own fault
                # class. Not reachable from the current dispatcher
                # (`stderr.decode()` always returns str), but
                # SessionResult is produced by anything implementing
                # AgentAdapter.
                error=(result.stderr or "").strip() or None,
                exit_code=result.exit_code,
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
            # #204: carried here too. An agent that writes a FAILED
            # checkpoint and exits non-zero is a different fault from
            # one that writes FAILED and exits 0, and this branch used
            # to drop the number that tells them apart — so the
            # operator-facing text said "exit code not recorded" about
            # a run whose exit code was right there.
            exit_code=result.exit_code,
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


@dataclass(frozen=True)
class ReviewReport:
    """What happened to the review, in a form the operator can be told.

    This exists because the old signature returned ``None``: every
    outcome — findings posted, reviewer missing, review switched off —
    produced the same value, so a caller could not tell a pull request
    that had been looked at from one that had not.

    On the forge those two are also indistinguishable, because both
    carry zero reviews and zero comments. That is #196, and the cost
    was ``AInvirion/zzsites#842``, which read as finished for forty
    minutes having never been looked at.

    ``posted`` is the only field that means "findings reached the pull
    request". ``status`` is deliberately wider than
    :class:`~ctrlrelay.core.code_review.ReviewStatus` — it also names
    the ways the review never got as far as running.
    """

    posted: bool
    status: str
    detail: str = ""
    # Set only by the two branches that KNOW the outcome was somebody's
    # configuration. Deciding this by matching ``status`` against a
    # list of strings was wrong in a way no test here could see:
    # :class:`~ctrlrelay.core.code_review.ReviewStatus` has a member
    # whose value is literally ``"skipped"``, and the branch that
    # forwards that enum does not know whether the operator chose it.
    # A reviewer that started returning it would have silenced the
    # warning about a pull request nobody read. An explicit flag set at
    # the site that knows cannot collide with anything.
    deliberate: bool = False

    @property
    def is_failure(self) -> bool:
        """Whether the operator needs telling.

        Two outcomes are somebody's own decision and not a fault:
        review switched off for the repo, and ``comment_on_pr`` off so
        the findings stay in the log. Both set ``deliberate``.

        Everything else means the pull request carries nothing a reader
        can use — which is not always the same as nobody having looked:
        on ``comment_failed`` the reviewer finished and only the posting
        failed. The reader is in the same position either way, which is
        why both are announced.
        """
        return not self.posted and not self.deliberate

    def failure_sentence(self) -> str:
        """One line for a chat notification, ending in what to do.

        A warning with no action in it gets dismissed, and a dismissed
        warning is the silent pass this ticket exists to remove.

        Only the failure case, because only the failure case is ever
        announced. An earlier version also had branches for a posted
        review, a switched-off one and a log-only one — none of which
        any caller could reach, since every caller checks
        :attr:`is_failure` first. Prose that cannot be printed reads as
        coverage to the next person.
        """
        reason = self.detail.strip() or self.status
        return (
            f"NO REVIEW ON THIS PR — the reviewer did not post ({reason}). "
            "Zero comments here does not mean it is clean. Read the diff "
            "before you merge."
        )


# What a caller believes when the dev lane never reported. Absent is
# not the same fact as reviewed, and rounding it towards "fine" is the
# defect this ticket is about — so it has a status of its own and reads
# as a failure.
REVIEW_NOT_REPORTED = ReviewReport(
    posted=False,
    status="not_reported",
    detail="the dev lane recorded no review outcome",
)


async def _review_and_comment(
    *,
    github: GitHubCLI,
    repo: str,
    session_id: str,
    pr_number: int,
    worktree_path: Path,
    base_branch: str,
    config: Any = None,
) -> ReviewReport:
    """Run the configured review and post what it found.

    No label, and no claim that a review happened — see
    :mod:`ctrlrelay.core.code_review` for why that claim cannot be made
    against a branch the reviewed party authored.

    Best-effort throughout: a reviewer that is missing, wedged or broken
    must never fail a PR whose code and CI are already fine. It must,
    however, SAY so — the return value is how, and every exit from this
    function carries one.
    """
    if getattr(config, "method", "cli") == "off":
        return ReviewReport(
            posted=False,
            status="skipped",
            detail="code_review.method is 'off' for this repo",
            deliberate=True,
        )

    outcome = await run_code_review(
        repo=repo,
        worktree_path=worktree_path,
        base_branch=base_branch,
        cli_command=getattr(
            config, "cli_command", 'codex review -c sandbox_mode="read-only"'
        ),
        timeout_seconds=getattr(config, "timeout_seconds", 900),
        session_id=session_id,
    )

    if not outcome.worth_posting:
        log_event(
            _logger,
            "dev.code_review.not_posted",
            session_id=session_id,
            repo=repo,
            pr_number=pr_number,
            status=outcome.status.value,
            error=outcome.error[:200],
        )
        return ReviewReport(
            posted=False,
            status=outcome.status.value,
            detail=outcome.error[:200],
        )

    if not getattr(config, "comment_on_pr", True):
        log_event(
            _logger,
            "dev.code_review.completed",
            session_id=session_id,
            repo=repo,
            pr_number=pr_number,
            findings=extract_verdict(outcome.output)[:2000],
        )
        return ReviewReport(
            posted=False,
            status="not_commented",
            detail="comment_on_pr is off for this repo",
            deliberate=True,
        )

    try:
        await github.comment_on_pr(
            repo,
            pr_number,
            format_review_comment(outcome, worktree_path=worktree_path),
        )
        log_event(
            _logger,
            "dev.code_review.commented",
            session_id=session_id,
            repo=repo,
            pr_number=pr_number,
        )
        return ReviewReport(posted=True, status="reviewed")
    except Exception as e:
        log_event(
            _logger,
            "dev.code_review.comment_failed",
            session_id=session_id,
            repo=repo,
            pr_number=pr_number,
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        return ReviewReport(
            posted=False,
            status="comment_failed",
            detail=f"{type(e).__name__}: {str(e)[:200]}",
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
    ci_wait_timeout_seconds: int = 600,
    code_review: Any = None,
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
    # Ownership signal: True when THIS session created the branch (either
    # from default or via the stale-merged delete+recreate path). False
    # when we reused an existing branch. Stays False until
    # create_worktree_with_new_branch returns so the exception-path
    # cleanup can't accidentally delete a pre-existing branch belonging
    # to another session. Issue #51: a pre-call snapshot of
    # branch_exists_locally goes stale the moment the worktree helper
    # detects a stale fully-merged local branch, deletes it, and
    # recreates it — so the flag has to come out of the helper itself.
    created_fresh = False
    # Pre-call snapshot of branch_exists_locally, used ONLY by the
    # exception-cleanup path to handle partial failures of
    # ``git worktree add -b`` that register the branch ref before the
    # directory setup crashes — in that case the helper raises before
    # returning created_fresh=True, so the snapshot is our only
    # signal that the leftover ref belongs to this session. Pessimistic
    # default (True) so we never delete a branch on a path that
    # didn't even get far enough to probe. The #51 fix (stale snapshot
    # after delete+recreate) doesn't apply here because the
    # delete+recreate path runs entirely INSIDE the helper and only
    # takes effect on successful return, where ``created_fresh``
    # wins over this snapshot.
    branch_existed_before = True

    # Whether a claim comment MAY stand on the issue, and therefore
    # whether there is anything for the failure path to reconcile
    # (#204). Starts False so the early failures — lock contention, an
    # unreadable issue — do not comment on an issue the relay never
    # touched.
    #
    # Set BEFORE the `comment_on_issue` await, not after. `gh` can
    # create the comment and then lose the response, and the two
    # mistakes are not symmetrical: a claim left standing with nothing
    # working on the issue is the eighteen-hour defect this ticket
    # exists for, while a reconciliation posted when the claim never
    # landed says "nothing is working on this issue" — which is true
    # either way. So the flag is deliberately optimistic about a claim
    # existing and the wording below does not depend on one being
    # visible.
    #
    # Also True when a previous attempt's marker was already there: a
    # stale claim from an earlier run is just as wrong, and the reader
    # of the issue cannot tell them apart.
    claim_may_stand = False

    # The last exit code a worker actually reported, for the exception
    # path's reconciliation comment. The exception path used to pass
    # `None` unconditionally under a comment claiming no exit code was
    # available — false whenever `pipeline.run` had already returned
    # one and a later step (verification, review, cleanup, a `gh` call)
    # raised. The comment then told a reader "not recorded" about a
    # number that was sitting in the result.
    last_exit_code: int | None = None

    try:
        # Get issue details
        issue = await github.get_issue(repo, issue_number)

        # Post claim comment so collaborators can see the agent picked it up.
        # Skip if a previous attempt already left the marker — keeps it idempotent
        # across retries / resumed sessions.
        existing_comments = issue.get("comments") or []
        already_claimed = claim_is_live(existing_comments)
        claim_may_stand = True
        if not already_claimed:
            await github.comment_on_issue(
                repo=repo,
                issue_number=issue_number,
                body=AGENT_CLAIM_COMMENT,
            )

        # Issue #51: ownership is signaled by ``created_fresh`` returned
        # from create_worktree_with_new_branch. True when this call
        # created the branch from default OR went through the
        # stale-merged delete+recreate path — either way THIS session
        # owns the resulting ref and cleanup on failure should delete it.
        #
        # Issue #52: pass the GitHubCLI so the reuse path can refuse a
        # branch that still backs an open PR (prior DONE session whose
        # PR is unmerged, or any external source). Without this, we'd
        # silently check out the reviewer's already-reviewed branch and
        # either hijack the PR or hit "A pull request already exists"
        # on `gh pr create` at the end.
        await worktree.ensure_bare_repo(repo)
        # Snapshot for the exception-path cleanup only (see
        # ``branch_existed_before`` comment at the top of this function).
        # We still need this because ``git worktree add -b`` can register
        # the branch ref before the directory step crashes, and the
        # helper raises before it can return ``created_fresh=True``.
        branch_existed_before = await worktree.branch_exists_locally(
            repo, branch_name,
        )
        worktree_path, created_fresh = await worktree.create_worktree_with_new_branch(
            repo=repo,
            session_id=session_id,
            new_branch=branch_name,
            github=github,
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
                "issue_comments": format_issue_comments(issue.get("comments")),
                "branch_name": branch_name,
                "ci_wait_timeout_seconds": ci_wait_timeout_seconds,
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
        last_exit_code = result.exit_code

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
            # A resumed worker is the one whose exit code the failure
            # comment should name. Assigned once before this loop, a
            # late exception reported the FIRST worker's code for a
            # session that had since been resumed several times.
            last_exit_code = result.exit_code

        # Verify PR is green & conflict-free before handing off. Resume the
        # session with a fix request if either is broken, up to max_fix_attempts.
        #
        # Issue #29: the lock handle is passed in so the verifier releases
        # the repo lock during its CI-polling window (can be up to 30 min)
        # and reacquires before any request_fix. Peer sessions targeting
        # the same repo can now run their own git-op phases while we wait.
        if result.success and result.outputs.get("pr_number") is not None:
            pr_number_for_review = int(result.outputs["pr_number"])
            # Starts as a failure, not as "fine". Every branch below
            # overwrites it; this value is what survives if a new
            # branch is ever added that forgets to.
            review_report = REVIEW_NOT_REPORTED
            verifier = pr_verifier or PRVerifier(github=github)
            result = await _verify_and_fix_pr(
                pipeline=pipeline,
                ctx=ctx,
                result=result,
                verifier=verifier,
                max_attempts=max_fix_attempts,
                lock_handle=lock,
            )

            # Review before handover: after verification so a PR still
            # being fixed is not reviewed mid-flight, before cleanup
            # while the worktree the reviewer reads still exists.
            #
            # pr_number is taken from the PRE-fix result: a fix round
            # returns the agent's own outputs, which need not carry it.
            # The whole call is guarded — a review must never fail a PR
            # whose code and CI are fine.
            if result.success and pr_number_for_review is not None:
                try:
                    review_base = await worktree.get_default_branch(repo)
                except Exception as e:
                    review_base = None
                    review_report = ReviewReport(
                        posted=False,
                        status="base_unknown",
                        detail=(
                            "could not resolve the base branch "
                            f"({type(e).__name__})"
                        ),
                    )
                    log_event(
                        _logger,
                        "dev.code_review.base_unknown",
                        session_id=session_id,
                        repo=repo,
                        error_type=type(e).__name__,
                    )
                if review_base is not None:
                    try:
                        review_report = await _review_and_comment(
                            github=github,
                            repo=repo,
                            session_id=session_id,
                            pr_number=pr_number_for_review,
                            worktree_path=worktree_path,
                            base_branch=review_base,
                            config=code_review,
                        )
                    except Exception as e:
                        review_report = ReviewReport(
                            posted=False,
                            status="errored",
                            detail=f"{type(e).__name__}: {str(e)[:200]}",
                        )
                        log_event(
                            _logger,
                            "dev.code_review.errored",
                            session_id=session_id,
                            repo=repo,
                            error_type=type(e).__name__,
                            error=str(e)[:200],
                        )

            # Carry the outcome out on the result's own field so
            # whoever announces the pull request can say whether it was
            # reviewed.
            #
            # A FIELD, not keys in `outputs`: `outputs` is merged with
            # the coding agent's own checkpoint, and the agent is the
            # party whose branch is being reviewed. An agent writing
            # `review_posted: true` would otherwise silence the warning
            # about its own unreviewed pull request — the adversary
            # `core.code_review`'s docstring is about, reaching the
            # announcement instead of the label.
            #
            # Outside the inner `if` as well, so every path that got as
            # far as having a pull request carries an outcome. The
            # branches above all set `review_report`; this line is what
            # makes "written on every path" true rather than a claim
            # about the paths I happened to look at.
            #
            # `replace` rather than re-listing the fields: a field
            # added to PipelineResult later is carried by construction
            # instead of being silently reset to its default.
            result = dataclasses.replace(result, review=review_report)

        # Reacquire the lock for the post-verify cleanup phase (remove
        # worktree / delete branch). Usually SHORT budget (cleanup is
        # best-effort, run_poll_loop awaits handlers serially).
        #
        # EXCEPT for the verify-contention error: that path signals
        # "PR needs another fix round that we couldn't enter," cli
        # will un-mark the issue for retry, and a fresh run would
        # fail at create_worktree if this one left the worktree
        # registered. Retry success DEPENDS on cleanup here, so use
        # the full fix budget — accepting a possible long wait in
        # this rare double-contention case beats deterministically
        # wedging the issue.
        if not lock.held:
            if result.error == _LOCK_CONTENDED_DURING_VERIFY_ERROR:
                reacquired = await lock.reacquire()  # full fix budget
            else:
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
                # Signal to cli.handle_issue that the worktree was NOT
                # torn down so the caller can skip any "unmark for
                # retry" logic. A fresh dev run against this issue
                # would fail at `create_worktree_with_new_branch`
                # because the abandoned worktree is still registered
                # on the bare repo. Requires operator cleanup (or the
                # next poller restart, which prunes stale worktrees).
                merged_outputs = dict(result.outputs)
                merged_outputs["cleanup_deferred"] = True
                result = PipelineResult(
                    success=result.success,
                    session_id=result.session_id,
                    summary=result.summary,
                    blocked=result.blocked,
                    question=result.question,
                    error=result.error,
                    outputs=merged_outputs,
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
        #              re-create `fix/issue-<n>` cleanly — BUT only if this
        #              session created the branch (``created_fresh`` — see
        #              issue #51) and it was never pushed (no recoverable
        #              work on origin).
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
                    created_fresh
                    and not await worktree.branch_exists_on_remote(
                        repo, branch_name
                    )
                ):
                    await worktree.delete_branch(repo, branch_name)

        # Reconcile the claim with the outcome (#204). BLOCKED is not a
        # failure — the operator is being asked something and the claim
        # is still true — and success speaks for itself through the
        # announcement. Only a run that ended without finishing leaves
        # the claim saying something false.
        if not result.success and not result.blocked:
            pr_raw = result.outputs.get("pr_number")
            try:
                pr_for_comment = None if pr_raw is None else int(pr_raw)
            except (TypeError, ValueError):
                # A non-numeric pr_number comes from the agent's own
                # checkpoint `outputs`, so it is untrusted input. Report
                # it as absent rather than letting a crash here replace
                # the run's real failure.
                pr_for_comment = None
            await reconcile_claim_on_failure(
                github,
                repo=repo,
                issue_number=issue_number,
                session_id=session_id,
                reason=failure_text(result),
                exit_code=result.exit_code,
                pr_number=pr_for_comment,
                claim_posted=claim_may_stand,
            )

        return result

    except Exception as e:
        state_db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? WHERE id = ?",
            ("failed", f"Error: {e}", int(time.time()), session_id),
        )
        state_db.commit()

        # Best-effort cleanup so a retry isn't blocked by leftover state.
        # Only touch the branch if THIS session created it (issue #51 —
        # ``created_fresh``) AND origin has no copy (no recoverable work
        # to orphan). Covers partial failures of `git worktree add -b`
        # that create the ref before the directory setup crashes, and
        # the stale-merged delete+recreate path where the branch now on
        # disk was put there by this very call. If we're mid-verify the
        # lock may be released — try to get it back for the cleanup.
        # Uses the SHORT cleanup budget (not the fix-path one): a
        # transient exception inside verifier.verify raising through
        # while a peer holds the lock would otherwise stall the serial
        # poll cycle for the full hour-long fix budget just to report
        # the failure.
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
            # Exception path ownership: three signals, ordered by
            # reliability.
            # 1. created_fresh covers the normal return-and-fail case
            #    (issue #51).
            # 2. StaleRecreatePartialFailureError signals the helper
            #    destroyed the old ref before `git worktree add -b`,
            #    so anything on disk with this branch name is ours
            #    regardless of what the pre-call snapshot said. The
            #    signal is per-call (exception attribute) so
            #    concurrent sessions on the shared WorktreeManager
            #    can't clobber each other's ownership state.
            # 3. Pre-call snapshot (`not branch_existed_before`) is
            #    the fallback for partial failures of the plain
            #    fresh-branch path where the helper raises before
            #    setting created_fresh.
            from ctrlrelay.core.worktree import StaleRecreatePartialFailureError
            stale_recreate_attempted = isinstance(
                e, StaleRecreatePartialFailureError,
            )
            we_own_branch = (
                created_fresh
                or stale_recreate_attempted
                or not branch_existed_before
            )
            if we_own_branch:
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

        # Same reconciliation as the normal failure path. This branch is
        # the one that actually ran on `zzsites#853`'s sibling faults —
        # an exception out of the worktree or `gh` layer leaves the claim
        # standing just as surely as a non-zero exit does, and it is
        # reached by more causes.
        #
        # The exit code is whatever the worker last reported, which is
        # `None` only when it never got as far as running. Passing
        # `None` unconditionally here was wrong in the common case: an
        # exception out of verification, review or cleanup arrives
        # AFTER `pipeline.run` returned an exit code, and the comment
        # then said "not recorded" about a number it had.
        await reconcile_claim_on_failure(
            github,
            repo=repo,
            issue_number=issue_number,
            session_id=session_id,
            reason=f"{type(e).__name__}: {str(e)[:200]}",
            exit_code=last_exit_code,
            claim_posted=claim_may_stand,
        )

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
            # Resume path: the branch should already exist (BLOCKED
            # sessions preserve both worktree and branch). We discard
            # ``created_fresh`` here — cleanup semantics don't apply to
            # resume, which never deletes the branch on exit; the
            # original run_dev_issue is the single owner of that
            # decision. Don't pass ``github`` either: an open PR for a
            # mid-BLOCKED session is possible (claude opened the PR and
            # blocked on a review question) and refusing reuse would
            # wedge the resume path.
            worktree_path, _ = await worktree.create_worktree_with_new_branch(
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
                "issue_comments": format_issue_comments(issue.get("comments")),
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
