"""PR verification: wait for CI and confirm mergeability before hand-off."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ctrlrelay.core.github import GitHubCLI, GitHubError
from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.pr_verifier")

# `gh pr checks --json bucket` returns one of: pass, fail, pending, skipping, cancel.
# Treat skipping as pass (skipped jobs don't block a merge) and everything except
# pending as "terminal".
_PENDING_BUCKETS = frozenset({"pending"})
_PASSING_BUCKETS = frozenset({"pass", "skipping"})
_TERMINAL_MERGEABLE_VALUES = frozenset({"MERGEABLE", "CONFLICTING"})

# The dev pipeline's contract is "open a PR and hand it to humans for review",
# not "merge the PR". So the verifier does NOT require the PR be in a directly-
# mergeable state — human-gated states (awaiting review, unresolved comments,
# pending deployments, merge-queue requirements) are all expected terminal
# states for us. What we DO reject is state that (a) indicates broken code
# (failing checks) or (b) the orchestrator can itself fix before hand-off
# (conflicts, behind base). All other mergeStateStatus values are accepted.
_REBASE_REQUIRED_MERGE_STATE_STATUS = frozenset({"BEHIND"})

# Require 2 consecutive empty check-list responses separated by a poll interval
# before concluding "no CI configured". GitHub registers check runs a few
# seconds after `gh pr create`, so a single-shot empty read is unreliable.
_EMPTY_CHECKS_CONFIRM_POLLS = 2


@dataclass
class VerificationResult:
    """Outcome of verifying a PR is ready for hand-off."""

    ready: bool
    reason: str = ""
    failing_checks: list[dict[str, Any]] = field(default_factory=list)
    pending_checks: list[dict[str, Any]] = field(default_factory=list)
    # Set True when wait_for_checks returned with pending entries (timeout hit
    # while CI was still running). Callers use this to distinguish "needs a
    # fix" from "just slow CI" and avoid burning retry budget on the latter.
    timed_out: bool = False
    mergeable: str | None = None
    merge_state_status: str | None = None


@dataclass
class PRVerifier:
    """Verifies a PR is green and conflict-free before declaring a dev task done."""

    github: GitHubCLI
    poll_interval: int = 30
    check_timeout: int = 1800
    mergeable_poll_attempts: int = 10

    async def wait_for_checks(
        self,
        repo: str,
        pr_number: int,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        """Poll PR checks until every check has left the 'pending' bucket or
        the timeout is reached.

        Empty-check handling: GitHub registers check runs asynchronously after
        `gh pr create` so a single empty read is ambiguous — the repo might
        have no CI, or CI just hasn't registered yet. We require
        `_EMPTY_CHECKS_CONFIRM_POLLS` consecutive empty reads separated by
        `poll_interval` before concluding "no CI configured".

        Transient `gh` failures (subprocess timeout, GitHubError) are
        treated as "still pending" and retried on the next iteration —
        a flaky network or a GitHub rate-limit back-off shouldn't abort
        the wait. The outer `limit` deadline is the safety net.

        Sleep cap: when called with `timeout < poll_interval` the loop
        used to block the full `poll_interval` before noticing the
        deadline (issue #90). Now caps `sleep` at `max(0, limit -
        elapsed)` so a 1-second timeout with a 15-second interval
        returns within ~1 second.
        """
        limit = self.check_timeout if timeout is None else timeout
        loop = asyncio.get_event_loop()
        # Wall-clock deadline (monotonic) so the loop terminates even
        # if `poll_interval` is 0 and every poll errors — accumulating
        # sleep durations would loop forever in that case because
        # max(0, 0) is still 0.
        deadline = loop.time() + limit
        empty_streak = 0
        checks: list[dict[str, Any]] = []
        had_successful_read = False
        last_transient_error: Exception | None = None
        while True:
            try:
                checks = await self.github.get_pr_checks(repo, pr_number)
            except (GitHubError, asyncio.TimeoutError) as e:
                # Transient gh failure — treat as still-pending, retry
                # on next iteration. Unconditional re-raise of
                # asyncio.TimeoutError used to surface as an ugly
                # traceback in `ctrlrelay ci wait` (issue #90).
                last_transient_error = e
                log_event(
                    _logger,
                    "pr_verifier.transient_gh_error",
                    repo=repo,
                    pr_number=pr_number,
                    reason=type(e).__name__,
                    error=str(e)[:200],
                )
            else:
                had_successful_read = True
                last_transient_error = None
                if not checks:
                    empty_streak += 1
                    if empty_streak >= _EMPTY_CHECKS_CONFIRM_POLLS:
                        return checks
                else:
                    empty_streak = 0
                    if all(
                        c.get("bucket") not in _PENDING_BUCKETS
                        for c in checks
                    ):
                        return checks
            remaining = deadline - loop.time()
            if remaining <= 0:
                # Fail closed if every poll failed: an empty `checks`
                # list would be misread as "no CI configured" and the
                # caller would silently greenlight the PR while
                # GitHub was actually unavailable. Surface the last
                # transient error so the CLI / verify() can react —
                # the widened except clause in `ci_wait` catches it
                # cleanly. (Codex P1 caught on the first PR pass.)
                if not had_successful_read and last_transient_error is not None:
                    raise last_transient_error
                return checks
            sleep_for = min(self.poll_interval, remaining)
            await asyncio.sleep(sleep_for)

    async def verify(
        self,
        repo: str,
        pr_number: int,
        timeout: int | None = None,
    ) -> VerificationResult:
        """Wait for CI, then check mergeability. Report ready only when both are green."""
        checks = await self.wait_for_checks(repo, pr_number, timeout=timeout)
        pending = [c for c in checks if c.get("bucket") in _PENDING_BUCKETS]
        failing = [
            c for c in checks
            if c.get("bucket") not in _PENDING_BUCKETS
            and c.get("bucket") not in _PASSING_BUCKETS
        ]
        # Failing checks take priority over pending. A matrix where lint
        # already failed but a long integration run is still pending must be
        # reported as broken, not timed out — otherwise the caller would hand
        # off a known-bad PR.
        if failing:
            names = ", ".join(c.get("name", "?") for c in failing)
            return VerificationResult(
                ready=False,
                reason=f"{len(failing)} check(s) failing: {names}",
                failing_checks=failing,
                pending_checks=pending,
            )
        if pending:
            # All failing paths ruled out; we simply hit the timeout while
            # everything still in flight was healthy. Don't ask Claude to
            # "fix" slow CI — surface it as a distinct outcome so the caller
            # hands off the PR as-is.
            names = ", ".join(c.get("name", "?") for c in pending)
            return VerificationResult(
                ready=False,
                timed_out=True,
                reason=(
                    f"CI still running after timeout: {len(pending)} "
                    f"check(s) pending ({names})"
                ),
                pending_checks=pending,
            )

        mergeable: str | None = None
        merge_state: str | None = None
        for _ in range(self.mergeable_poll_attempts):
            state = await self.github.get_pr_state(repo, pr_number)
            mergeable = state.get("mergeable")
            merge_state = state.get("mergeStateStatus")
            if mergeable in _TERMINAL_MERGEABLE_VALUES:
                break
            await asyncio.sleep(self.poll_interval)

        if mergeable == "CONFLICTING":
            return VerificationResult(
                ready=False,
                reason="PR has merge conflicts with the base branch",
                mergeable=mergeable,
                merge_state_status=merge_state,
            )
        if mergeable != "MERGEABLE":
            return VerificationResult(
                ready=False,
                reason=f"PR mergeable state unresolved: {mergeable}",
                mergeable=mergeable,
                merge_state_status=merge_state,
            )
        if merge_state in _REBASE_REQUIRED_MERGE_STATE_STATUS:
            return VerificationResult(
                ready=False,
                reason=(
                    "PR is behind the base branch and must be rebased before "
                    "merge (mergeStateStatus=BEHIND)"
                ),
                mergeable=mergeable,
                merge_state_status=merge_state,
            )

        # Any remaining state (CLEAN, HAS_HOOKS, BLOCKED, UNSTABLE, DRAFT,
        # etc.) is accepted. CI is verified green above, conflicts and
        # behind-base are handled explicitly, so what's left is either
        # directly mergeable or human-gated — both are valid hand-off states
        # for a pipeline that never auto-merges.
        return VerificationResult(
            ready=True,
            mergeable=mergeable,
            merge_state_status=merge_state,
        )
