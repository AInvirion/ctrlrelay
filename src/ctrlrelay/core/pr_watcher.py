"""PR merge watcher for monitoring PR state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from ctrlrelay.core.github import GitHubCLI


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
        """
        elapsed = 0
        while elapsed < timeout:
            if await self.check_merged(repo, pr_number):
                return True

            if on_poll:
                await on_poll()

            await asyncio.sleep(self.poll_interval)
            elapsed += self.poll_interval

        return False
