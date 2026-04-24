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

    ``include_labels_by_repo`` is the opt-in complement (see #80). When a
    repo has any include-labels configured, the poller drops the
    ``--assignee`` filter on that repo's ``gh`` query and instead fetches
    all open issues, then accepts any issue that **either** is assigned
    to the operator (the pre-#80 behavior) **or** carries at least one of
    the configured labels. Matching is case-insensitive. Label-matched
    issues skip the self-assignment event check — the label itself is
    the trust signal that the operator opted into by configuring it.
    An issue that both carries a matching label and is assigned to the
    operator is accepted exactly once (not duplicated in ``new_issues``
    or ``seen_issues``). A repo with an empty / missing ``include_labels``
    keeps the pre-#80 ``--assignee``-filtered query so we don't
    over-fetch.
    """

    github: GitHubCLI
    username: str
    repos: list[str]
    state_file: Path
    seen_issues: dict[str, set[int]] = field(default_factory=dict)
    accept_foreign_assignments: set[str] = field(default_factory=set)
    exclude_labels_by_repo: dict[str, list[str]] = field(default_factory=dict)
    include_labels_by_repo: dict[str, list[str]] = field(default_factory=dict)
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

        Repos configured with ``include_labels_by_repo[repo]`` skip the
        server-side ``--assignee`` filter and fetch all open issues, then
        accept any that either (a) match one of the include-labels or
        (b) are assigned to the operator. Issues matching neither are
        left alone (NOT marked seen) so a later label-add or assignment
        will still surface them. See #80.
        """
        new_issues: list[dict[str, Any]] = []

        for repo in self.repos:
            # Repos with GitHub Issues disabled will never return issues; skip
            # before the `gh` call so we don't log the same error every cycle.
            if repo in self._issues_disabled_repos:
                continue
            # Per-repo include-labels controls the fetch strategy. With
            # no include-labels configured we keep the pre-#80
            # ``--assignee`` server-side filter (cheap, small result).
            # With include-labels configured we run one TARGETED query
            # per trigger — the assignee query plus one
            # ``--label <L>`` query per configured label — and merge by
            # issue number. An unfiltered ``gh issue list`` would cap at
            # --limit (default 100) and silently miss labeled issues on
            # later pages in a busy repo (codex P1 on the first cut).
            include_labels = self.include_labels_by_repo.get(repo, [])
            include_lowered = {label.lower() for label in include_labels}
            try:
                if include_lowered:
                    by_number: dict[int, dict[str, Any]] = {}
                    assignee_issues = await self.github.list_assigned_issues(
                        repo, assignee=self.username,
                    )
                    for i in assignee_issues:
                        try:
                            by_number[int(i["number"])] = i
                        except Exception:
                            # Malformed entry; let the per-issue guard
                            # below log it on the next loop pass.
                            pass
                    for label in include_labels:
                        labeled = await self.github.list_issues_by_label(
                            repo, label=label,
                        )
                        for i in labeled:
                            try:
                                by_number.setdefault(int(i["number"]), i)
                            except Exception:
                                pass
                    issues = list(by_number.values())
                else:
                    issues = await self.github.list_assigned_issues(
                        repo, assignee=self.username,
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

                # Two positive triggers, checked in this order:
                #   1. include-label match (#80): the operator opted this
                #      label into the pipeline, so the label IS the trust
                #      signal — no self-assignment check needed.
                #   2. assignment to operator: pre-#80 path, still needs
                #      the self-assignment event check.
                # With targeted queries (assignee + per-label) every
                # issue surfaced here already matched at least one
                # trigger, but we keep the is_assigned check so the
                # label-match branch below still knows when to skip the
                # self-assignment event lookup. On repos without
                # include_labels, the server already filtered to the
                # assignee, so every issue that makes it here is
                # assignment-triggered — we skip the client-side
                # assignment check to preserve pre-#80 behavior (some
                # payloads don't include ``assignees`` in fixtures or
                # truncated responses).
                label_match = self._matched_include_label(issue, include_lowered)
                if include_lowered:
                    is_assigned = self._issue_is_assigned_to(issue, self.username)
                    if label_match is None and not is_assigned:
                        # Defensive: a targeted query should never
                        # return an issue that matches neither trigger,
                        # but if gh's label filter ever has a quirk
                        # (renamed labels, case, pagination), we'd
                        # rather leave the issue unmarked than
                        # swallow it permanently.
                        continue
                else:
                    # Server-side ``--assignee`` already filtered; by
                    # construction every issue reaching here is
                    # assignment-triggered.
                    is_assigned = True

                # Mark seen before deciding whether to surface the issue so a
                # filtered (foreign-assigned) issue isn't re-checked every poll.
                # Set-add is idempotent: a label+assigned issue still lands
                # in the set exactly once, and the ``new_issues`` branch below
                # is OR-gated so it only fires once for the same issue.
                seen_for_repo.add(number)

                if label_match is not None:
                    log_event(
                        _logger,
                        "poll.issue.included_by_label",
                        repo=repo,
                        issue_number=number,
                        matched_label=label_match,
                    )
                    # Label match bypasses the self-assignment check; the
                    # operator's config choice is the trust boundary.
                    new_issues.append({"repo": repo, "issue": issue})
                    continue

                # Assignment-only path: run the self-assignment event
                # check exactly as before.
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

    @staticmethod
    def _matched_include_label(
        issue: dict[str, Any], include_lowered: set[str]
    ) -> str | None:
        """Return the first issue label that matches ``include_lowered``.

        Mirrors ``_matched_exclude_label`` — shares the string/dict
        tolerance and case-insensitive matching so operators can rely on
        identical semantics between the exclude and include knobs. The
        returned value is the label's original casing for logging. See
        #80.
        """
        if not include_lowered:
            return None
        for label in issue.get("labels") or []:
            if isinstance(label, dict):
                name = label.get("name", "")
            else:
                name = str(label)
            if name and name.lower() in include_lowered:
                return name
        return None

    @staticmethod
    def _issue_is_assigned_to(issue: dict[str, Any], username: str) -> bool:
        """Check whether ``username`` is listed in the issue's assignees.

        The ``gh issue list`` payload returns assignees as
        ``[{"login": "...", ...}]``. When we drop ``--assignee`` from the
        server-side query (include_labels mode) we need this to detect
        the pre-#80 assignment trigger client-side. Case-sensitive
        compare — GitHub logins are themselves case-preserving but
        case-insensitive for matching; the server-side filter
        historically uses exact login compare, so we match that.
        """
        if not username:
            return False
        for a in issue.get("assignees") or []:
            login = (a or {}).get("login") if isinstance(a, dict) else str(a)
            if login == username:
                return True
        return False

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
        """Seed seen_issues with all currently-triggered issues.

        Call this on first startup to avoid treating existing assignments
        (or label matches) as new. Only issues assigned / labeled AFTER
        this seed will trigger handlers.

        Repos with ``include_labels`` configured seed BOTH the assignee
        result and each label's targeted result (matching the poll()
        fetch strategy — see poll() for why an unfiltered issue list
        can silently cap at --limit on busy repos). Other repos keep
        the cheap ``--assignee`` query.

        Failure mode: if a per-repo lookup fails transiently, the seed skips
        that repo and logs ``poll.repo.skipped``. The consequence is that on
        next poll, any currently-assigned issues on the skipped repo will be
        treated as new and picked up — that's safer than crashing first-run.
        """
        for repo in self.repos:
            if repo in self._issues_disabled_repos:
                continue
            include_labels = self.include_labels_by_repo.get(repo, [])
            try:
                if include_labels:
                    by_number: dict[int, dict[str, Any]] = {}
                    assignee_issues = await self.github.list_assigned_issues(
                        repo, assignee=self.username,
                    )
                    for i in assignee_issues:
                        try:
                            by_number[int(i["number"])] = i
                        except (KeyError, TypeError, ValueError):
                            pass
                    for label in include_labels:
                        labeled = await self.github.list_issues_by_label(
                            repo, label=label,
                        )
                        for i in labeled:
                            try:
                                by_number.setdefault(int(i["number"]), i)
                            except (KeyError, TypeError, ValueError):
                                pass
                    issues = list(by_number.values())
                else:
                    issues = await self.github.list_assigned_issues(
                        repo, assignee=self.username,
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
                # Targeted queries in include-label mode already
                # guarantee every issue here matches at least one
                # trigger (assignee or a configured label), so we
                # can seed unconditionally.
                try:
                    seen_for_repo.add(int(issue["number"]))
                except (KeyError, TypeError, ValueError):
                    # Malformed entries shouldn't abort the seed —
                    # poll() has its own per-issue guard and will log
                    # them on the next cycle.
                    continue
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
