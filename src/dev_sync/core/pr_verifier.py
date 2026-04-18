"""PR verification: wait for CI and confirm mergeability before hand-off."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from dev_sync.core.github import GitHubCLI

_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_TERMINAL_MERGEABLE_VALUES = frozenset({"MERGEABLE", "CONFLICTING"})


@dataclass
class VerificationResult:
    """Outcome of verifying a PR is ready for hand-off."""

    ready: bool
    reason: str = ""
    failing_checks: list[dict[str, Any]] = field(default_factory=list)
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
        """Poll PR checks until every check is completed or timeout is reached."""
        limit = self.check_timeout if timeout is None else timeout
        elapsed = 0
        checks: list[dict[str, Any]] = []
        while True:
            checks = await self.github.get_pr_checks(repo, pr_number)
            if checks and all(c.get("status") == "completed" for c in checks):
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
        failing = [
            c for c in checks
            if c.get("status") != "completed"
            or c.get("conclusion") not in _PASSING_CONCLUSIONS
        ]
        if failing:
            names = ", ".join(c.get("name", "?") for c in failing)
            return VerificationResult(
                ready=False,
                reason=f"{len(failing)} check(s) failing or incomplete: {names}",
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

        return VerificationResult(
            ready=True,
            mergeable=mergeable,
            merge_state_status=merge_state,
        )
