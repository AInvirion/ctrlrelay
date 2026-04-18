"""GitHub Issue Poller for ctrlrelay."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from ctrlrelay.core.github import GitHubCLI, GitHubError
from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.poller")

# Exceptions that are transient and should skip the current repo/iteration
# rather than tear the whole poll loop down. asyncio.CancelledError is
# deliberately excluded so a shutdown signal still propagates.
_TRANSIENT_POLL_ERRORS = (TimeoutError, GitHubError, OSError)


@dataclass
class IssuePoller:
    """Polls GitHub repos for newly assigned issues.

    Maintains a set of seen issue numbers per repo so that only genuinely new
    issues are surfaced on each call to ``poll()``.
    """

    github: GitHubCLI
    username: str
    repos: list[str]
    state_file: Path
    seen_issues: dict[str, set[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Load seen issues from the JSON state file (if it exists)."""
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
            raw = data.get("seen_issues", {})
            self.seen_issues = {repo: set(numbers) for repo, numbers in raw.items()}
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable state — start fresh
            self.seen_issues = {}

    def _save_state(self) -> None:
        """Persist seen issues and a ``last_poll`` timestamp to the state file."""
        data = {
            "seen_issues": {
                repo: sorted(numbers) for repo, numbers in self.seen_issues.items()
            },
            "last_poll": datetime.now(timezone.utc).isoformat(),
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(data, indent=2))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def poll(self) -> list[dict[str, Any]]:
        """Poll all configured repos for new issues assigned to ``self.username``.

        Returns:
            A list of ``{"repo": str, "issue": dict}`` entries for issues that
            have not been seen before. Updates ``seen_issues`` and persists
            state to disk.

        Per-repo resilience: a transient failure on one repo (network timeout,
        ``gh`` exit, OS error) is logged and skipped so the other repos still
        get polled. Only ``asyncio.CancelledError`` escapes, which allows a
        clean shutdown signal to propagate.
        """
        new_issues: list[dict[str, Any]] = []

        for repo in self.repos:
            try:
                issues = await self.github.list_assigned_issues(
                    repo, assignee=self.username
                )
            except asyncio.CancelledError:
                raise
            except _TRANSIENT_POLL_ERRORS as e:
                log_event(
                    _logger,
                    "poll.repo.skipped",
                    repo=repo,
                    reason=type(e).__name__,
                    error=str(e)[:200],
                )
                continue

            seen_for_repo = self.seen_issues.setdefault(repo, set())

            for issue in issues:
                number: int = issue["number"]
                if number not in seen_for_repo:
                    new_issues.append({"repo": repo, "issue": issue})
                    seen_for_repo.add(number)

        self._save_state()
        return new_issues

    def mark_seen(self, repo: str, issue_number: int) -> None:
        """Mark an issue as seen without triggering a poll.

        Useful for pre-seeding state from external sources (e.g. resuming
        after a crash where work was already started).
        """
        self.seen_issues.setdefault(repo, set()).add(issue_number)
        self._save_state()

    async def seed_current(self) -> None:
        """Seed seen_issues with all currently assigned issues.

        Call this on first startup to avoid treating existing assignments
        as new. Only issues assigned AFTER this seed will trigger handlers.

        Failure mode: if a per-repo lookup fails transiently, the seed skips
        that repo and logs ``poll.repo.skipped``. The consequence is that on
        next poll, any currently-assigned issues on the skipped repo will be
        treated as new and picked up — that's safer than crashing first-run.
        """
        for repo in self.repos:
            try:
                issues = await self.github.list_assigned_issues(
                    repo, assignee=self.username
                )
            except asyncio.CancelledError:
                raise
            except _TRANSIENT_POLL_ERRORS as e:
                log_event(
                    _logger,
                    "poll.repo.skipped",
                    repo=repo,
                    reason=type(e).__name__,
                    error=str(e)[:200],
                    phase="seed",
                )
                continue
            seen_for_repo = self.seen_issues.setdefault(repo, set())
            for issue in issues:
                seen_for_repo.add(issue["number"])
        self._save_state()


async def run_poll_loop(
    poller: IssuePoller,
    handler: Callable[[str, dict[str, Any]], Awaitable[None]],
    interval: int = 300,
    max_iterations: int | None = None,
) -> None:
    """Run the polling loop.

    Args:
        poller: IssuePoller instance
        handler: Async function to call for each new issue (repo, issue)
        interval: Seconds between polls
        max_iterations: Max iterations (None = infinite)

    Iteration resilience: any non-cancellation exception from the poll or a
    handler call is logged as ``poll.iteration.failed`` and the loop sleeps
    and continues. This keeps a single bad cycle (slow network, one flaky
    handler) from crashing the daemon and forcing a launchd restart.
    """
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        try:
            new_issues = await poller.poll()
            for item in new_issues:
                await handler(item["repo"], item["issue"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_event(
                _logger,
                "poll.iteration.failed",
                iteration=iterations,
                reason=type(e).__name__,
                error=str(e)[:200],
            )

        iterations += 1
        if max_iterations is None or iterations < max_iterations:
            await asyncio.sleep(interval)
