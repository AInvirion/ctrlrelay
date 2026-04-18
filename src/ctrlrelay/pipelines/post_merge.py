"""Post-merge handling for dev pipeline."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from ctrlrelay.core.github import GitHubCLI
from ctrlrelay.core.obs import get_logger, log_event
from ctrlrelay.core.pr_watcher import PRWatcher
from ctrlrelay.transports.base import Transport

_logger = get_logger("pipelines.post_merge")


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


async def pr_watch_task(
    *,
    repo: str,
    issue_number: int,
    pr_url: str,
    pr_number: int,
    session_id: str | None,
    github: GitHubCLI,
    transport_factory: Callable[[], Awaitable[Transport | None]] | None = None,
    poll_interval: int = 60,
    timeout: int = 86400,
) -> dict[str, Any]:
    """Background-safe wrapper around watch_and_handle_merge that manages
    its own transport lifecycle and emits structured ``dev.pr.*`` events.

    ``transport_factory`` is an async callable returning a CONNECTED
    transport (or None). It's invoked once at the start of the watch so
    the watcher's transport is independent of whichever one handled the
    initial issue dispatch (that one is closed when handle_issue returns).
    Returning None is fine — the merge notification is then suppressed.

    Returns a dict describing the outcome:
      {"merged": bool, "timed_out": bool, "cancelled": bool, "failed": str|None}
    """
    outcome: dict[str, Any] = {
        "merged": False, "timed_out": False,
        "cancelled": False, "failed": None,
    }
    transport: Transport | None = None
    if transport_factory is not None:
        try:
            transport = await transport_factory()
        except Exception as e:
            log_event(
                _logger,
                "dev.pr.watch_transport_failed",
                repo=repo, issue_number=issue_number, pr_number=pr_number,
                reason=type(e).__name__, error=str(e)[:200],
            )
            transport = None

    log_event(
        _logger, "dev.pr.watching",
        session_id=session_id, repo=repo, issue_number=issue_number,
        pr_number=pr_number, pr_url=pr_url,
    )
    try:
        merged = await watch_and_handle_merge(
            repo=repo,
            pr_number=pr_number,
            issue_number=issue_number,
            github=github,
            transport=transport,
            poll_interval=poll_interval,
            timeout=timeout,
        )
        outcome["merged"] = merged
        outcome["timed_out"] = not merged
        log_event(
            _logger,
            "dev.pr.merged" if merged else "dev.pr.watch_timeout",
            session_id=session_id, repo=repo, issue_number=issue_number,
            pr_number=pr_number,
        )
    except asyncio.CancelledError:
        outcome["cancelled"] = True
        log_event(
            _logger, "dev.pr.watch_cancelled",
            session_id=session_id, repo=repo, issue_number=issue_number,
            pr_number=pr_number,
        )
        raise
    except Exception as e:
        outcome["failed"] = type(e).__name__
        log_event(
            _logger, "dev.pr.watch_failed",
            session_id=session_id, repo=repo, issue_number=issue_number,
            pr_number=pr_number,
            reason=type(e).__name__, error=str(e)[:200],
        )
    finally:
        if transport is not None:
            try:
                await transport.close()
            except Exception:
                pass
    return outcome
