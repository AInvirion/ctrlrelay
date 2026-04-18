"""PR verification: wait for CI and confirm mergeability before hand-off."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from dev_sync.core.github import GitHubCLI

# `gh pr checks --json bucket` returns one of: pass, fail, pending, skipping, cancel.
# Treat skipping as pass (skipped jobs don't block a merge) and everything except
# pending as "terminal".
_PENDING_BUCKETS = frozenset({"pending"})
_PASSING_BUCKETS = frozenset({"pass", "skipping"})
_TERMINAL_MERGEABLE_VALUES = frozenset({"MERGEABLE", "CONFLICTING"})

# mergeStateStatus values GitHub returns (docs: PullRequestMergeStateStatus).
# CLEAN / HAS_HOOKS are obviously mergeable. BLOCKED is mergeable-pending-
# review on repos that require an approving review — the dev pipeline never
# auto-merges, so awaiting review is the intended terminal state for us.
# (Failing required checks would already have been caught in failing_checks
# above, so reaching here with BLOCKED means a human approval step.)
_READY_MERGE_STATE_STATUS = frozenset({"CLEAN", "HAS_HOOKS", "BLOCKED"})
# BEHIND = branch not up-to-date with base; base-branch protection commonly
# blocks merging in this state. Surface it with the same affordance as
# CONFLICTING so Claude rebases before hand-off.
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
        `poll_interval` before concluding "no CI configured"."""
        limit = self.check_timeout if timeout is None else timeout
        elapsed = 0
        empty_streak = 0
        checks: list[dict[str, Any]] = []
        while True:
            checks = await self.github.get_pr_checks(repo, pr_number)
            if not checks:
                empty_streak += 1
                if empty_streak >= _EMPTY_CHECKS_CONFIRM_POLLS:
                    return checks
            else:
                empty_streak = 0
                if all(c.get("bucket") not in _PENDING_BUCKETS for c in checks):
                    return checks
            if elapsed >= limit:
                return checks
            await asyncio.sleep(self.poll_interval)
            elapsed += self.poll_interval

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
        if pending:
            # wait_for_checks returned with pending entries still outstanding,
            # which only happens on timeout. Don't ask Claude to "fix" slow CI
            # — surface it as a separate outcome the caller can pass through.
            names = ", ".join(c.get("name", "?") for c in pending)
            return VerificationResult(
                ready=False,
                timed_out=True,
                reason=(
                    f"CI still running after timeout: {len(pending)} "
                    f"check(s) pending ({names})"
                ),
                pending_checks=pending,
                failing_checks=failing,
            )
        if failing:
            names = ", ".join(c.get("name", "?") for c in failing)
            return VerificationResult(
                ready=False,
                reason=f"{len(failing)} check(s) failing: {names}",
                failing_checks=failing,
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
        if merge_state not in _READY_MERGE_STATE_STATUS:
            # UNSTABLE (non-required failing check), BLOCKED (other protection),
            # DRAFT, UNKNOWN, etc. Refuse to hand off rather than declare a PR
            # ready when the merge UI would actually reject it.
            return VerificationResult(
                ready=False,
                reason=(
                    f"PR not ready to merge: mergeStateStatus={merge_state}"
                ),
                mergeable=mergeable,
                merge_state_status=merge_state,
            )

        return VerificationResult(
            ready=True,
            mergeable=mergeable,
            merge_state_status=merge_state,
        )
