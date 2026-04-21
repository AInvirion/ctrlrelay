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
#
# GitHubError is included because we can't distinguish transient (rate
# limit, 5xx, network) from permanent (bad repo name, expired auth, 404)
# without fragile error-message parsing — classifying both as skip avoids
# crashes. A persistent-failure counter (see below) makes permanent
# misconfiguration visible even though it's technically skipped here.
_TRANSIENT_POLL_ERRORS = (TimeoutError, GitHubError, OSError)

# After this many consecutive per-repo failures, escalate log level to
# WARNING so a persistent misconfiguration (expired auth, renamed repo,
# revoked access) stops hiding behind routine "transient" skip logs.
_REPO_FAILURE_WARN_THRESHOLD = 3


def _is_issues_disabled_error(exc: Exception) -> bool:
    """Detect the specific GitHubError raised when a repo has its Issues
    feature disabled. This is a permanent state (not a transient API
    failure), so callers should skip the repo rather than retry it on every
    poll cycle."""
    if not isinstance(exc, GitHubError):
        return False
    return "has disabled issues" in str(exc).lower()


@dataclass
class IssuePoller:
    """Polls GitHub repos for newly assigned issues.

    Maintains a set of seen issue numbers per repo so that only genuinely new
    issues are surfaced on each call to ``poll()``.

    By default, new issues are filtered to those where the most recent
    ``assigned`` event naming ``username`` was performed by ``username``
    themselves — i.e. self-assignment only. Repos listed in
    ``accept_foreign_assignments`` bypass this check.

    ``exclude_labels_by_repo`` gives the operator a way to mark issues as
    "not for the agent" (operator tasks, pure instructions, manual work).
    Matched issues are marked seen so they don't keep reappearing, logged
    under ``poll.issue.excluded_by_label``, and never handed to the dev
    pipeline. The exclusion check runs BEFORE the assignment-event lookup
    so it short-circuits the extra ``gh`` call for excluded issues.
    Matching is case-insensitive.
    """

    github: GitHubCLI
    username: str
    repos: list[str]
    state_file: Path
    seen_issues: dict[str, set[int]] = field(default_factory=dict)
    accept_foreign_assignments: set[str] = field(default_factory=set)
    exclude_labels_by_repo: dict[str, list[str]] = field(default_factory=dict)
    # Per-repo consecutive-skip counter; populated at runtime by poll() /
    # seed_current(). Not persisted — intentionally resets on daemon
    # restart so an operator fix is exercised before we re-escalate.
    _repo_failure_counts: dict[str, int] = field(default_factory=dict, repr=False)
    # Repos with GitHub Issues feature disabled — a permanent state, not a
    # transient fetch error. Populated on first encounter and kept for the
    # daemon lifetime so we don't spam WARNING logs every 120s cycle.
    # Resets on daemon restart so a fresh detection still runs if the repo
    # re-enables issues in the meantime.
    _issues_disabled_repos: set[str] = field(default_factory=set, repr=False)

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

    def _save_state_best_effort(self) -> None:
        """Try to persist state; log and continue on disk errors.

        Callers MUST NOT let a _save_state failure propagate out of poll() —
        doing so would drop the new-issues list on the floor while the
        in-memory seen_issues set has already been mutated, silently
        abandoning the work until the daemon restarts.
        """
        try:
            self._save_state()
        except OSError as e:
            log_event(
                _logger,
                "poll.save_state.failed",
                reason=type(e).__name__,
                error=str(e)[:200],
                state_file=str(self.state_file),
            )

    def _record_repo_failure(
        self,
        repo: str,
        exc: Exception,
        *,
        phase: str = "poll",
    ) -> None:
        """Bump the consecutive-failure counter and log with an escalated
        level once the threshold is reached. ``phase`` distinguishes
        poll-time vs seed-time skips in the event payload."""
        count = self._repo_failure_counts.get(repo, 0) + 1
        self._repo_failure_counts[repo] = count
        fields = {
            "repo": repo,
            "reason": type(exc).__name__,
            "error": str(exc)[:200],
            "consecutive_failures": count,
            "phase": phase,
        }
        if count >= _REPO_FAILURE_WARN_THRESHOLD:
            fields["persistent"] = True
            _logger.warning("poll.repo.skipped", extra=fields)
        else:
            log_event(_logger, "poll.repo.skipped", **fields)

    def _clear_repo_failure(self, repo: str) -> None:
        """Reset the failure counter after a successful repo lookup."""
        self._repo_failure_counts.pop(repo, None)

    def _mark_issues_disabled(self, repo: str) -> None:
        """Mark a repo as having GitHub Issues disabled. Logged once at INFO
        level so the operator can see which repos won't be polled; future
        cycles skip the `gh` call entirely until daemon restart."""
        if repo in self._issues_disabled_repos:
            return
        self._issues_disabled_repos.add(repo)
        # Any accumulated transient-failure count is meaningless once we've
        # identified the error as permanent — clear it so the restart counter
        # starts fresh if the repo ever re-enables issues.
        self._repo_failure_counts.pop(repo, None)
        log_event(
            _logger,
            "poll.repo.issues_disabled",
            repo=repo,
            action="skipping permanently until daemon restart",
        )

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

        Issues carrying any label from ``exclude_labels_by_repo[repo]`` are
        marked seen and dropped before the assignment-event check runs, so
        operator-only / instruction-only issues never reach the dev pipeline
        and we don't pay a second ``gh`` call for them.
        """
        new_issues: list[dict[str, Any]] = []

        for repo in self.repos:
            # Repos with GitHub Issues disabled will never return issues; skip
            # before the `gh` call so we don't log the same error every cycle.
            if repo in self._issues_disabled_repos:
                continue
            try:
                issues = await self.github.list_assigned_issues(
                    repo, assignee=self.username
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if _is_issues_disabled_error(e):
                    self._mark_issues_disabled(repo)
                    continue
                # Transient-ish (TimeoutError/GitHubError/OSError) goes through
                # the failure counter so persistent misconfig escalates; any
                # other unexpected exception is logged as a skip too so the
                # surrounding repos still get processed AND new_issues from
                # prior repos reaches the caller. Without this catch, a later
                # repo exploding would leave earlier repos' seen_issues
                # mutated but their new_issues list unreturned.
                if isinstance(e, _TRANSIENT_POLL_ERRORS):
                    self._record_repo_failure(repo, e, phase="poll")
                else:
                    log_event(
                        _logger,
                        "poll.repo.unexpected_error",
                        repo=repo,
                        reason=type(e).__name__,
                        error=str(e)[:200],
                        phase="poll",
                    )
                continue

            # Successful lookup — clear any accumulated failure count.
            self._clear_repo_failure(repo)

            seen_for_repo = self.seen_issues.setdefault(repo, set())
            exclude_lowered = {
                label.lower()
                for label in self.exclude_labels_by_repo.get(repo, [])
            }
            for issue in issues:
                # Per-issue guard so ONE malformed payload (missing 'number',
                # wrong type, non-dict entry) doesn't poison the remaining
                # good issues in the same repo's batch.
                try:
                    number = int(issue["number"])
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log_event(
                        _logger,
                        "poll.issue.malformed",
                        repo=repo,
                        reason=type(e).__name__,
                        error=str(e)[:200],
                    )
                    continue

                if number in seen_for_repo:
                    continue

                # Exclude-label filter runs before the assignment-event
                # lookup so operator-only issues never trigger a second
                # gh call, and are permanently marked seen so they stop
                # re-appearing in future polls.
                matched = self._matched_exclude_label(issue, exclude_lowered)
                if matched is not None:
                    seen_for_repo.add(number)
                    log_event(
                        _logger,
                        "poll.issue.excluded_by_label",
                        repo=repo,
                        issue_number=number,
                        matched_label=matched,
                    )
                    continue

                # Mark seen before deciding whether to surface the issue so a
                # filtered (foreign-assigned) issue isn't re-checked every poll.
                seen_for_repo.add(number)

                try:
                    accepted = await self._is_self_assigned(repo, number)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Treat any failure to check assignment events as
                    # foreign-equivalent: don't run the pipeline, but leave
                    # the issue marked seen so we don't hammer the events
                    # endpoint on every poll.
                    log_event(
                        _logger,
                        "poll.issue.assignment_check_failed",
                        repo=repo,
                        number=number,
                        reason=type(e).__name__,
                        error=str(e)[:200],
                    )
                    accepted = False

                if accepted:
                    new_issues.append({"repo": repo, "issue": issue})

        # Never propagate a save_state disk failure out of poll() — the
        # caller has work to do with new_issues. Log and move on.
        self._save_state_best_effort()
        return new_issues

    async def _is_self_assigned(self, repo: str, issue_number: int) -> bool:
        """Check if the most recent ``assigned`` event naming ``self.username``
        was performed by ``self.username`` themselves.

        Repos in ``accept_foreign_assignments`` short-circuit to ``True``.
        Foreign assignments (or an empty event list) emit a
        ``poll.issue.foreign_assignment`` log record and return ``False``.
        """
        if repo in self.accept_foreign_assignments:
            return True

        events = await self.github.list_assignment_events(repo, issue_number)
        relevant = [
            e
            for e in events
            if (e.get("assignee") or {}).get("login") == self.username
        ]
        if not relevant:
            log_event(
                _logger,
                "poll.issue.foreign_assignment",
                repo=repo,
                number=issue_number,
                assigner_login=None,
                reason="no_self_assignment_event",
            )
            return False

        # Events endpoint returns chronological order; the last one wins.
        latest = relevant[-1]
        assigner_login = (latest.get("actor") or {}).get("login")
        if assigner_login == self.username:
            return True

        log_event(
            _logger,
            "poll.issue.foreign_assignment",
            repo=repo,
            number=issue_number,
            assigner_login=assigner_login,
        )
        return False

    @staticmethod
    def _matched_exclude_label(
        issue: dict[str, Any], exclude_lowered: set[str]
    ) -> str | None:
        """Return the first issue label that matches ``exclude_lowered``.

        Labels come back from ``gh issue list`` as ``[{"name": "...", ...}]``;
        we also accept a plain list of strings for flexibility in tests.
        Matching is case-insensitive; the returned value is the label's
        original casing so log output reflects what's actually on the issue.
        """
        if not exclude_lowered:
            return None
        for label in issue.get("labels") or []:
            if isinstance(label, dict):
                name = label.get("name", "")
            else:
                name = str(label)
            if name and name.lower() in exclude_lowered:
                return name
        return None

    def mark_seen(self, repo: str, issue_number: int) -> None:
        """Mark an issue as seen without triggering a poll.

        Useful for pre-seeding state from external sources (e.g. resuming
        after a crash where work was already started).
        """
        self.seen_issues.setdefault(repo, set()).add(issue_number)
        self._save_state()

    def unmark_seen(self, repo: str, issue_number: int) -> None:
        """Remove an issue from the seen-set so the next poll picks it up
        again. Use this when a handler failed for a transient reason that
        retrying would fix — the canonical case is a per-repo lock
        conflict with a concurrent secops sweep. Without this, the
        issue would be silently dropped forever because
        ``poll()`` marks issues seen **before** handing them to the
        handler, so a single handler failure is fatal by default.
        Disk-save is best-effort; a failed save is logged but never
        propagates."""
        seen = self.seen_issues.get(repo)
        if seen and issue_number in seen:
            seen.discard(issue_number)
            self._save_state_best_effort()

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
            if repo in self._issues_disabled_repos:
                continue
            try:
                issues = await self.github.list_assigned_issues(
                    repo, assignee=self.username
                )
            except asyncio.CancelledError:
                raise
            except _TRANSIENT_POLL_ERRORS as e:
                if _is_issues_disabled_error(e):
                    self._mark_issues_disabled(repo)
                    continue
                self._record_repo_failure(repo, e, phase="seed")
                continue
            self._clear_repo_failure(repo)
            seen_for_repo = self.seen_issues.setdefault(repo, set())
            for issue in issues:
                seen_for_repo.add(issue["number"])
        self._save_state_best_effort()


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
        # Guard poll() separately from the handler dispatch: a malformed
        # poll result shouldn't lose queued work, and a handler failure
        # shouldn't skip the rest of the batch.
        try:
            new_issues = await poller.poll()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_event(
                _logger,
                "poll.iteration.failed",
                iteration=iterations,
                phase="poll",
                reason=type(e).__name__,
                error=str(e)[:200],
            )
            new_issues = []

        # Each handler invocation is isolated. A failure on one issue must
        # not cancel the remaining already-seen-and-persisted issues — those
        # would otherwise be silently dropped until daemon restart.
        for item in new_issues:
            try:
                await handler(item["repo"], item["issue"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                issue = item.get("issue") or {}
                log_event(
                    _logger,
                    "poll.handler.failed",
                    repo=item.get("repo"),
                    issue_number=issue.get("number"),
                    reason=type(e).__name__,
                    error=str(e)[:200],
                )

        iterations += 1
        if max_iterations is None or iterations < max_iterations:
            await asyncio.sleep(interval)
