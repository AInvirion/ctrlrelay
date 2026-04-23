"""Post-merge handling for dev pipeline."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from ctrlrelay.core.github import GitHubCLI
from ctrlrelay.core.obs import get_logger, log_event
from ctrlrelay.core.pr_watcher import PRWatcher
from ctrlrelay.core.state import StateDB
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


def _stamp_phase(
    state_db: StateDB | None,
    repo: str,
    pr_number: int,
    phase: str,
    session_id: str | None,
    issue_number: int,
) -> None:
    """Best-effort persist the current cleanup phase. Called after
    each side effect succeeds so a resumed watcher can skip work
    already done. A DB failure must never abort the cleanup loop —
    the alternative is abandoning a known-merged PR because SQLite
    had a transient hiccup. Worst case with a swallowed failure is
    a duplicate comment on the narrow restart window; best case
    (DB healthy, which is essentially always) resume is exact."""
    if state_db is None:
        return
    try:
        state_db.set_pr_watch_cleanup_phase(repo, pr_number, phase)
    except Exception as e:
        log_event(
            _logger, "dev.pr.watch_persist_failed",
            session_id=session_id, repo=repo,
            issue_number=issue_number, pr_number=pr_number,
            reason=type(e).__name__, error=str(e)[:200],
            phase=f"stamp:{phase}",
        )


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
    state_db: StateDB | None = None,
) -> None:
    """Run the post-merge close + notify steps with per-step idempotent
    retry, rebuilding the transport on each attempt so a bridge restart
    mid-retry doesn't permanently break the notification.

    Idempotency within a single call: each side-effect is gated by its
    own local flag so retries don't duplicate work.

    Idempotency across restarts: when ``state_db`` is supplied, each
    completed step stamps a ``cleanup_phase`` on the ``pr_watches``
    row (commented → closed → notified). On entry we read the phase
    and pre-set the local flags so a rehydrated watcher resumes from
    where the prior process left off instead of re-posting the close
    comment or re-firing the Telegram notification.

    Transport rebuild: ``transport_factory`` is invoked INSIDE each
    attempt so a bridge restart mid-retry gets a fresh socket.

    On final exhaustion, raises the last exception; pr_watch_task's
    outer handler logs it as dev.pr.watch_failed.
    """
    comment_posted = False
    issue_closed = False
    notification_sent = False
    if state_db is not None:
        # Rehydration: skip every step the prior process already
        # completed. The phase is monotonic (commented → closed →
        # notified) so higher phases imply all lower phases done.
        try:
            prior_phase = state_db.get_pr_watch_cleanup_phase(
                repo, pr_number
            )
        except Exception:
            prior_phase = None
        if prior_phase in ("commented", "closed", "notified"):
            comment_posted = True
        if prior_phase in ("closed", "notified"):
            issue_closed = True
        if prior_phase == "notified":
            notification_sent = True
    last_exc: Exception | None = None
    delay = _HANDLE_MERGE_RETRY_BASE_DELAY
    comment = f"Closed by PR #{pr_number}"
    notification = (
        f"Issue #{issue_number} closed after PR #{pr_number} merged in {repo}"
    )

    for attempt in range(1, _HANDLE_MERGE_RETRY_ATTEMPTS + 1):
        transport: Transport | None = None
        transport_build_error: Exception | None = None
        if not notification_sent and transport_factory is not None:
            try:
                transport = await transport_factory()
            except Exception as e:
                # Factory raised — bridge likely transiently unavailable.
                # Record the error so we can retry on the next attempt
                # instead of silently dropping the notification.
                transport_build_error = e
                log_event(
                    _logger, "dev.pr.watch_transport_failed",
                    session_id=session_id, repo=repo,
                    issue_number=issue_number, pr_number=pr_number,
                    attempt=attempt,
                    reason=type(e).__name__, error=str(e)[:200],
                )
                transport = None
            # Factory returned None legitimately (e.g. non-Telegram
            # config or socket intentionally absent) → no notification
            # channel is configured; do NOT retry. `transport_build_error`
            # remains None, so the "done" check below returns cleanly.

        try:
            if not comment_posted:
                await github.comment_on_issue(repo, issue_number, comment)
                comment_posted = True
                _stamp_phase(state_db, repo, pr_number, "commented",
                             session_id, issue_number)
            if not issue_closed:
                await github._run_gh(
                    "issue", "close", str(issue_number), "--repo", repo,
                )
                issue_closed = True
                _stamp_phase(state_db, repo, pr_number, "closed",
                             session_id, issue_number)
            if not notification_sent and transport is not None:
                await transport.send(notification)
                notification_sent = True
                _stamp_phase(state_db, repo, pr_number, "notified",
                             session_id, issue_number)

            # Done when either: we sent the notification this round, OR
            # the factory returned None meaning no channel was ever
            # configured. Fall through to the retry path ONLY if the
            # factory itself raised — that's a transient failure worth
            # retrying so a brief bridge outage doesn't drop the
            # notification permanently.
            if notification_sent or transport_build_error is None:
                return
            raise transport_build_error
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
                comment_posted=comment_posted,
                issue_closed=issue_closed,
                notification_sent=notification_sent,
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
    state_db: StateDB | None = None,
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

    ``state_db`` persists the watcher across poller restarts: a row is
    inserted on ``dev.pr.watching`` and removed on the terminal
    ``dev.pr.merged`` / ``dev.pr.watch_timeout`` events. It is NOT
    removed on ``dev.pr.watch_cancelled`` — a cancellation during
    shutdown must leave the row behind so the next poller startup can
    rehydrate the watcher. StateDB I/O is best-effort: a disk failure
    is swallowed so it can never prevent merge detection from
    continuing in-memory.

    Returns a dict describing the outcome:
      {"merged": bool, "timed_out": bool, "cancelled": bool, "failed": str|None}
    """
    outcome: dict[str, Any] = {
        "merged": False, "timed_out": False,
        "cancelled": False, "failed": None,
    }

    # Persist the watch before any long-running work so a crash between
    # `dev.pr.watching` and the first poll doesn't lose the row.
    # Idempotent: a rehydrated task calling this again just refreshes
    # started_at, which is fine.
    if state_db is not None:
        try:
            state_db.add_pr_watch(
                repo=repo, pr_number=pr_number,
                issue_number=issue_number,
                session_id=session_id,
                pr_url=pr_url,
            )
        except Exception as e:
            # Never let a state_db failure kill the watcher — degrade
            # to in-memory-only and log so ops can see the durability
            # gap.
            log_event(
                _logger, "dev.pr.watch_persist_failed",
                session_id=session_id, repo=repo,
                issue_number=issue_number, pr_number=pr_number,
                reason=type(e).__name__, error=str(e)[:200],
            )

    log_event(
        _logger, "dev.pr.watching",
        session_id=session_id, repo=repo, issue_number=issue_number,
        pr_number=pr_number, pr_url=pr_url,
    )
    try:
        # Rehydration shortcut: if the prior process already observed
        # the merge (any cleanup_phase stamped), skip wait_for_merge
        # entirely and resume cleanup. Re-running wait_for_merge here
        # would let a transient GH error or a brief rehydrate window
        # log dev.pr.watch_timeout and delete the row, permanently
        # dropping the remaining close/notify steps — exactly the
        # failure the phase tracking is supposed to prevent.
        resuming_cleanup = False
        if state_db is not None:
            try:
                prior_phase = state_db.get_pr_watch_cleanup_phase(
                    repo, pr_number
                )
            except Exception:
                prior_phase = None
            if prior_phase is not None:
                resuming_cleanup = True

        if resuming_cleanup:
            merged = True
            log_event(
                _logger, "dev.pr.watch_resumed",
                session_id=session_id, repo=repo,
                issue_number=issue_number, pr_number=pr_number,
                cleanup_phase=prior_phase,
            )
        else:
            watcher = PRWatcher(github=github, poll_interval=poll_interval)
            merged = await watcher.wait_for_merge(
                repo=repo, pr_number=pr_number, timeout=timeout,
            )
        # Record merge detection BEFORE running the post-merge handler,
        # so an exhausted retry loop (e.g. permanent bridge outage) still
        # leaves `outcome["merged"] = True`. The merge really did happen;
        # only the cleanup chain is degraded, and `outcome["failed"]`
        # already surfaces that via the except branch below.
        outcome["merged"] = merged
        outcome["timed_out"] = not merged
        log_event(
            _logger,
            "dev.pr.merged" if merged else "dev.pr.watch_timeout",
            session_id=session_id, repo=repo, issue_number=issue_number,
            pr_number=pr_number,
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
                state_db=state_db,
            )
        # Drop the durable row only AFTER post-merge cleanup fully
        # completes (merged path) or after we've given up on timeout.
        # If _handle_merge_with_retry raises — retries exhausted, bridge
        # permanently broken — the except Exception branch below owns
        # the row lifecycle and leaves it so the next poller startup
        # rehydrates and retries cleanup. wait_for_merge returns True
        # immediately on rehydrate since the PR is already merged.
        if state_db is not None:
            try:
                state_db.remove_pr_watch(repo, pr_number)
            except Exception as e:
                log_event(
                    _logger, "dev.pr.watch_persist_failed",
                    session_id=session_id, repo=repo,
                    issue_number=issue_number, pr_number=pr_number,
                    reason=type(e).__name__, error=str(e)[:200],
                    phase="remove",
                )
    except asyncio.CancelledError:
        outcome["cancelled"] = True
        log_event(
            _logger, "dev.pr.watch_cancelled",
            session_id=session_id, repo=repo, issue_number=issue_number,
            pr_number=pr_number,
        )
        # Deliberately DO NOT remove the pr_watches row on cancellation.
        # Cancellation is the shutdown signal — the next poller startup
        # must rehydrate this watcher or the PR's post-merge automation
        # is lost for the remainder of its 7-day window.
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
