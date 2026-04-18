"""Post-merge handling for dev pipeline."""

from __future__ import annotations

from ctrlrelay.core.github import GitHubCLI
from ctrlrelay.core.pr_watcher import PRWatcher
from ctrlrelay.transports.base import Transport


async def handle_merge(
    repo: str,
    pr_number: int,
    issue_number: int,
    github: GitHubCLI,
    transport: Transport | None = None,
) -> None:
    """Handle post-merge actions."""
    await github.close_issue(
        repo,
        issue_number,
        f"Closed by PR #{pr_number}",
    )

    if transport:
        await transport.send(
            f"Issue #{issue_number} closed after PR #{pr_number} merged in {repo}"
        )


async def watch_and_handle_merge(
    repo: str,
    pr_number: int,
    issue_number: int,
    github: GitHubCLI,
    transport: Transport | None = None,
    poll_interval: int = 60,
    timeout: int = 86400,
) -> bool:
    """Watch for PR merge and handle post-merge actions."""
    watcher = PRWatcher(github=github, poll_interval=poll_interval)

    merged = await watcher.wait_for_merge(repo, pr_number, timeout=timeout)

    if merged:
        await handle_merge(
            repo=repo,
            pr_number=pr_number,
            issue_number=issue_number,
            github=github,
            transport=transport,
        )
        return True

    return False
