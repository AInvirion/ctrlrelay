"""Post-merge handling for dev pipeline."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from ctrlrelay.core.github import GitHubCLI
from ctrlrelay.core.obs import get_logger, log_event
from ctrlrelay.core.pr_watcher import PRWatcher
from ctrlrelay.transports.base import Transport

_logger = get_logger("pipelines.post_merge")

# Default merge-watch window. Real PR review cycles commonly exceed a
# day (weekends, time-zone splits, larger changes), and a silent watch
# timeout means the linked issue never auto-closes and no merge
# notification fires. 7 days is the sweet spot: covers typical review
# lag without keeping a zombie task alive for months on an abandoned PR.
# Operators can still override via an explicit `timeout=` argument.
DEFAULT_PR_WATCH_TIMEOUT = 7 * 24 * 60 * 60

# After a merge is detected, handle_merge can fail transiently on
# `gh issue close` or the transport.send (network blip, rate limit).
# Without retry the whole automation is wasted — PR merged, issue
# stays open forever. Retry with modest backoff; if all attempts fail,
# the last failure is logged via dev.pr.watch_failed.
_HANDLE_MERGE_RETRY_ATTEMPTS = 5
_HANDLE_MERGE_RETRY_BASE_DELAY = 5  # seconds; doubled each attempt


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
    timeout: int = DEFAULT_PR_WATCH_TIMEOUT,
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


async def _handle_merge_with_retry(
    *,
    repo: str,
    pr_number: int,
    issue_number: int,
    github: GitHubCLI,
    transport_factory: Callable[[], Awaitable[Transport | None]] | None,
    session_id: str | None,
) -> None:
    """Run the post-merge close + notify steps with per-step idempotent
    retry, rebuilding the transport on each attempt so a bridge restart
    mid-retry doesn't permanently break the notification.

    Idempotency: ``github.close_issue`` is called at most once even
    across retries — if it succeeds but the notification step later
    fails, subsequent attempts skip the close so we don't post
    duplicate "Closed by PR #N" comments.

    Transport rebuild: ``transport_factory`` is invoked INSIDE each
    attempt. If the bridge socket drops between the watcher start and
    a merge several days later, or during a retry, the next attempt
    gets a fresh connection.

    On final exhaustion, raises the last exception; pr_watch_task's
    outer handler logs it as dev.pr.watch_failed.
    """
    issue_closed = False
    last_exc: Exception | None = None
    delay = _HANDLE_MERGE_RETRY_BASE_DELAY
    comment = f"Closed by PR #{pr_number}"
    notification = (
        f"Issue #{issue_number} closed after PR #{pr_number} merged in {repo}"
    )

    for attempt in range(1, _HANDLE_MERGE_RETRY_ATTEMPTS + 1):
        # Build a fresh transport for this attempt — an earlier attempt
        # may have dropped its socket; a stale one would guarantee a
        # doomed send(). Failure to build is non-fatal; we can still
        # close the issue, and surface the failure via log.
        transport: Transport | None = None
        if transport_factory is not None:
            try:
                transport = await transport_factory()
            except Exception as e:
                log_event(
                    _logger, "dev.pr.watch_transport_failed",
                    session_id=session_id, repo=repo,
                    issue_number=issue_number, pr_number=pr_number,
                    attempt=attempt,
                    reason=type(e).__name__, error=str(e)[:200],
                )
                transport = None

        try:
            if not issue_closed:
                await github.close_issue(repo, issue_number, comment)
                issue_closed = True
            if transport is not None:
                await transport.send(notification)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_exc = e
            log_event(
                _logger, "dev.pr.handle_merge_retry",
                session_id=session_id, repo=repo,
                issue_number=issue_number, pr_number=pr_number,
                attempt=attempt,
                max_attempts=_HANDLE_MERGE_RETRY_ATTEMPTS,
                issue_closed=issue_closed,
                reason=type(e).__name__, error=str(e)[:200],
            )
            if attempt >= _HANDLE_MERGE_RETRY_ATTEMPTS:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
        finally:
            if transport is not None:
                try:
                    await transport.close()
                except Exception:
                    pass
    assert last_exc is not None
    raise last_exc


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
    timeout: int = DEFAULT_PR_WATCH_TIMEOUT,
) -> dict[str, Any]:
    """Background-safe wrapper around the merge watcher that emits
    structured ``dev.pr.*`` events and manages a transport LAZILY —
    the transport is only created AFTER the merge is detected, so a
    bridge that's down or restarted during the multi-day watch window
    doesn't leave us with a stale connection at notification time.

    ``transport_factory`` is an async callable returning a connected
    transport (or None). Called once, right before ``handle_merge``;
    its result is closed immediately after the merge handler runs.
    A raising factory is logged and skipped (merge detection proceeds
    without the notification channel rather than aborting the close).

    Returns a dict describing the outcome:
      {"merged": bool, "timed_out": bool, "cancelled": bool, "failed": str|None}
    """
    outcome: dict[str, Any] = {
        "merged": False, "timed_out": False,
        "cancelled": False, "failed": None,
    }

    log_event(
        _logger, "dev.pr.watching",
        session_id=session_id, repo=repo, issue_number=issue_number,
        pr_number=pr_number, pr_url=pr_url,
    )
    try:
        watcher = PRWatcher(github=github, poll_interval=poll_interval)
        merged = await watcher.wait_for_merge(
            repo=repo, pr_number=pr_number, timeout=timeout,
        )
        if merged:
            # Defer transport construction to inside the retry loop so
            # each attempt gets a fresh connection — a bridge restart
            # during the watch or between retries doesn't leave us
            # hitting a dead socket forever.
            await _handle_merge_with_retry(
                repo=repo, pr_number=pr_number,
                issue_number=issue_number, github=github,
                transport_factory=transport_factory,
                session_id=session_id,
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
    return outcome
