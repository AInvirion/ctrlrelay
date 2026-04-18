"""PR merge watcher for monitoring PR state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from ctrlrelay.core.github import GitHubCLI, GitHubError
from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.pr_watcher")

# After this many CONSECUTIVE transient failures we give up on the watch.
# gh can raise "transient-looking" errors (GitHubError, TimeoutError,
# OSError) for permanent problems too — bad repo, expired auth,
# permission change, missing gh binary. Without a cap, those would
# silently loop for the full 7-day timeout and never surface the
# problem. 10 × poll_interval is enough to cover a reasonable network
# blip (several minutes on 60s polls) while still failing fast on a
# genuinely stuck watch.
_TRANSIENT_FAILURE_CAP = 10


@dataclass
class PRWatcher:
    """Watches PRs for merge events."""

    github: GitHubCLI
    poll_interval: int = 60

    async def check_merged(self, repo: str, pr_number: int) -> bool:
        """Check if a PR has been merged.

        Args:
            repo: Repository name (owner/repo)
            pr_number: PR number

        Returns:
            True if merged, False otherwise
        """
        pr_state = await self.github.get_pr_state(repo, pr_number)
        return pr_state.get("state") == "MERGED"

    async def wait_for_merge(
        self,
        repo: str,
        pr_number: int,
        timeout: int = 86400,
        on_poll: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Wait for a PR to be merged.

        Args:
            repo: Repository name
            pr_number: PR number
            timeout: Max seconds to wait (default 24h)
            on_poll: Optional callback after each poll

        Returns:
            True if merged within timeout, False otherwise

        Transient-failure handling: individual ``gh`` failures
        (``GitHubError``, ``TimeoutError``, network-level ``OSError``)
        during a multi-day watch MUST NOT abort the loop — otherwise a
        single flaky poll cycle permanently stops monitoring the PR.
        Log a structured ``pr_watch.transient_error`` event and keep
        polling. ``asyncio.CancelledError`` is always re-raised so a
        clean shutdown propagates.
        """
        elapsed = 0
        consecutive_failures = 0
        while elapsed < timeout:
            try:
                if await self.check_merged(repo, pr_number):
                    return True
                consecutive_failures = 0  # successful poll resets the counter
            except asyncio.CancelledError:
                raise
            except (GitHubError, TimeoutError, OSError) as e:
                consecutive_failures += 1
                log_event(
                    _logger, "pr_watch.transient_error",
                    repo=repo, pr_number=pr_number,
                    reason=type(e).__name__,
                    error=str(e)[:200],
                    elapsed=elapsed,
                    consecutive_failures=consecutive_failures,
                )
                if consecutive_failures >= _TRANSIENT_FAILURE_CAP:
                    # Likely permanent: bad repo, expired auth, 404,
                    # missing gh binary. Fail fast instead of zombie-
                    # sleeping for 7 days.
                    log_event(
                        _logger, "pr_watch.abandoned_after_too_many_errors",
                        repo=repo, pr_number=pr_number,
                        consecutive_failures=consecutive_failures,
                        last_reason=type(e).__name__,
                        last_error=str(e)[:200],
                    )
                    raise
                # Fall through to the sleep + retry.

            if on_poll:
                try:
                    await on_poll()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # on_poll is best-effort diagnostic plumbing

            await asyncio.sleep(self.poll_interval)
            elapsed += self.poll_interval

        return False
