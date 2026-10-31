"""The status page's waiting line (#209).

The poller works one batch at a time and does not look at GitHub again until
the current run is done, so it cannot say what is waiting. The web process
asks GitHub itself, once a minute, with one search across every repository,
and keeps only what the poller would pick up: configured repos, the poller's
own label rules (its static methods, not a copy of them), the poller's own
self-assignment check, and nothing the poller has already seen or is already
running. Repos with ``include_labels`` are also asked per label, as the
poller does, so an issue triggered by a label alone appears too.

Read only. A failed GitHub call leaves the line ``unknown`` with the last good
answer and its time, never an empty list that reads as "nothing waiting".
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctrlrelay.core.config import Config
from ctrlrelay.core.github import GitHubCLI
from ctrlrelay.core.poller import IssuePoller

REASON_CONFIRM = "waits for the poller to confirm the assignment"
REASON_NEXT_POLL = "next poll"

# How long a "not self-assigned" answer is trusted before it is asked again.
# A "self-assigned" answer is kept while the issue is unchanged: the cache is
# keyed on the issue's updatedAt, which GitHub bumps on any change to the
# issue, assignment included, so an unassign and reassign between two
# refreshes is checked again. An issue that leaves the search is dropped
# from the cache as well.
FOREIGN_RECHECK_S = 600

# `gh search issues` returns at most 1000 results. A search that comes back
# with exactly its limit may have been cut short, so the line is unknown.
SEARCH_LIMIT = 1000
TOO_MANY = "too many assigned issues to list"

# Marks a row that came from a per-label query rather than the assignee
# search, so it is only "assigned" if its own payload says so.
VIA_LABEL = "via_label"

# (running sessions, repo locks): what the poller is busy with right now.
SessionsReader = Callable[[], tuple[list[dict[str, Any]], dict[str, str]]]


@dataclass(frozen=True)
class WaitingItem:
    repo: str
    issue_number: int
    title: str
    reason: str
    updated_ts: int


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _parse_ts(value: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return 0


def seen_key(repo: str, issue_number: int) -> str:
    return f"{repo}#{issue_number}"


def load_seen(poller_state_file: Path) -> set[str]:
    """The poller's ``seen_issues`` as ``"owner/repo#N"`` keys.

    ``IssuePoller`` persists ``{"seen_issues": {repo: [numbers]}}``. A
    missing, unreadable or malformed file gives an empty set, which is what
    the poller itself starts from in that case.
    """
    try:
        data = json.loads(poller_state_file.read_text())
        raw = data.get("seen_issues", {})
        return {
            seen_key(repo, int(n))
            for repo, numbers in raw.items()
            for n in numbers
        }
    except Exception:
        return set()


def _label_name(label: Any) -> str:
    """A label's name, from a ``{"name": ...}`` dict or a plain string, the
    two shapes the poller's label checks accept."""
    if isinstance(label, dict):
        return str(label.get("name") or "")
    return str(label)


def _session_label(session: dict[str, Any]) -> str:
    repo = session.get("repo") or "?"
    number = session.get("issue_number")
    if number is not None:
        return f"{repo}#{number}"
    pipeline = session.get("pipeline")
    return f"{repo} ({pipeline})" if pipeline else repo


def _reason(
    repo: str, running: list[dict[str, Any]], locks: dict[str, str],
) -> str:
    holder = locks.get(repo)
    if holder is not None:
        session = next(
            (s for s in running if (s.get("session_id") or s.get("id")) == holder),
            None,
        )
        if session is not None and session.get("issue_number") is not None:
            return f"same repo as #{session['issue_number']}"
        if session is not None and session.get("pipeline"):
            return f"same repo as a running {session['pipeline']} session"
        return "same repo as a running session"
    if running:
        return f"poller busy with {_session_label(running[0])}"
    return REASON_NEXT_POLL


def compute_waiting(
    config: Config,
    issues: list[dict[str, Any]],
    *,
    seen: set[str],
    running: list[dict[str, Any]],
    locks: dict[str, str],
    assignments: Mapping[str, bool] | None = None,
    username: str = "",
) -> list[WaitingItem]:
    """What the poller would pick up next, in the order it would take it.

    ``issues`` are ``GitHubCLI.search_assigned_issues`` rows, followed by
    rows from ``list_issues_by_label`` marked ``VIA_LABEL``. The search is
    filtered by assignee on the server, so its rows are assignment
    triggers, exactly as the poller trusts its own ``--assignee`` query; a
    label row is assigned only if its payload lists ``username``.

    ``assignments`` holds the self-assignment check, keyed like ``seen``,
    for issues admitted by assignment alone in a repo that does not accept
    foreign assignments: ``True`` waits normally, ``False`` is dropped (the
    poller will not take it), and a missing key means the check could not
    be made, so the item says it waits for that confirmation.

    Order: config repo order (the poller walks ``config.repos``), then
    GitHub's order within a repo, assignee rows before label-only rows.
    """
    assignments = assignments or {}
    running_keys = {
        seen_key(s.get("repo") or "", int(s["issue_number"]))
        for s in running
        if s.get("issue_number") is not None
    }
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        by_repo.setdefault(str(issue.get("repo") or "").lower(), []).append(issue)

    items: list[WaitingItem] = []
    for repo_cfg in config.repos:
        repo = repo_cfg.name
        auto = repo_cfg.automation
        exclude = {label.lower() for label in auto.exclude_labels}
        include = {label.lower() for label in auto.include_labels}
        require = {label.lower() for label in auto.require_labels}
        for issue in by_repo.get(repo.lower(), []):
            try:
                number = int(issue["number"])
            except (KeyError, TypeError, ValueError):
                continue
            key = seen_key(repo, number)
            if key in seen or key in running_keys:
                continue
            if IssuePoller._matched_exclude_label(issue, exclude) is not None:
                continue
            label_match = IssuePoller._matched_include_label(issue, include)
            if issue.get(VIA_LABEL):
                is_assigned = IssuePoller._issue_is_assigned_to(issue, username)
            else:
                is_assigned = True
            if (
                is_assigned
                and require
                and IssuePoller._matched_require_label(issue, require) is None
            ):
                is_assigned = False
            if label_match is None and not is_assigned:
                continue
            if label_match is None and not auto.accept_foreign_assignments:
                # Assignment-only: the poller takes it only if the
                # assignment was self-made.
                confirmed = assignments.get(key)
                if confirmed is False:
                    continue
                reason = (
                    REASON_CONFIRM if confirmed is None
                    else _reason(repo, running, locks)
                )
            else:
                reason = _reason(repo, running, locks)
            items.append(WaitingItem(
                repo=repo,
                issue_number=number,
                title=str(issue.get("title") or ""),
                reason=reason,
                updated_ts=_parse_ts(issue.get("updatedAt")),
            ))
    return items


def _titles_of(issues: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for issue in issues:
        try:
            key = seen_key(str(issue["repo"]), int(issue["number"]))
        except (KeyError, TypeError, ValueError):
            continue
        title = issue.get("title")
        if isinstance(title, str) and title:
            out[key] = title
    return out


def _item_dict(item: WaitingItem) -> dict[str, Any]:
    out = asdict(item)
    out["updated"] = _iso(item.updated_ts)
    return out


class WaitingTracker:
    """Holds the waiting line between GitHub checks.

    ``refresh()`` runs on the server's background loop; ``snapshot()`` is
    read from request threads. Each refresh builds a new dict and swaps it
    in with one assignment, so a reader never sees half an update.
    """

    def __init__(
        self,
        config: Config,
        github: GitHubCLI,
        *,
        sessions: SessionsReader,
        poller_state_file: Path | None = None,
        clock: Callable[[], float] = time.time,
        search_limit: int = SEARCH_LIMIT,
    ) -> None:
        self._config = config
        self._search_limit = search_limit
        self._github = github
        self._sessions = sessions
        self._poller_state_file = (
            poller_state_file
            if poller_state_file is not None
            else config.paths.state_db.parent / "poller_state.json"
        )
        self._clock = clock
        self._username: str | None = None
        self._poller: IssuePoller | None = None
        # seen-key -> (self-assigned?, when it was checked, issue updated_ts)
        self._assignment_cache: dict[str, tuple[bool, int, int]] = {}
        self._last_good: list[dict[str, Any]] = []
        self._last_good_ts: int | None = None
        # "owner/repo#N" -> title, from the last search that succeeded,
        # before any filtering: running issues are in it too.
        self._titles: dict[str, str] = {}
        self._snapshot: dict[str, Any] = self._build(
            status="unknown", checked_ts=None, error="not checked yet",
        )

    def _build(
        self, *, status: str, checked_ts: int | None, error: str | None,
    ) -> dict[str, Any]:
        return {
            "items": [dict(i) for i in self._last_good],
            "status": status,
            "checked": _iso(checked_ts),
            "checked_ts": checked_ts,
            "last_good": _iso(self._last_good_ts),
            "last_good_ts": self._last_good_ts,
            "error": error,
        }

    async def refresh(self) -> None:
        now = int(self._clock())
        try:
            if self._username is None:
                self._username = await self._github.current_login()
            username = self._username
            issues = await self._fetch(username)
            self._titles = _titles_of(issues)
            running, locks = self._sessions()
            seen = load_seen(self._poller_state_file)
            # First pass: which items wait only on the self-assignment
            # check. Only those (already past the label rules) are checked.
            pending = compute_waiting(
                self._config, issues, seen=seen, running=running, locks=locks,
                username=username,
            )
            assignments: dict[str, bool] = {}
            for item in pending:
                if item.reason != REASON_CONFIRM:
                    continue
                # A failed check raises, and fails the whole refresh: an
                # unconfirmed line published as "ok" would read as current.
                assignments[seen_key(item.repo, item.issue_number)] = (
                    await self._self_assigned(
                        username, item.repo, item.issue_number,
                        item.updated_ts, now,
                    )
                )
            items = compute_waiting(
                self._config, issues, seen=seen, running=running, locks=locks,
                assignments=assignments, username=username,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # gh, timeout, DB: all leave the line unknown
            error = f"{type(exc).__name__}: {exc}"[:300]
            self._snapshot = self._build(
                status="unknown", checked_ts=now, error=error,
            )
            return
        self._last_good = [_item_dict(i) for i in items]
        self._last_good_ts = now
        self._snapshot = self._build(status="ok", checked_ts=now, error=None)

    async def _fetch(self, username: str) -> list[dict[str, Any]]:
        """The assignee search, plus one label query per include label,
        merged by issue number. Any failed query fails the refresh: a line
        missing a repo's labelled issues would read as complete.

        When both queries return an issue, the search row is kept (it is
        the assignment trigger) and gains any label only the label query
        saw, so a label added between the two calls still counts.
        """
        found = await self._github.search_assigned_issues(
            username, limit=self._search_limit,
        )
        if len(found) >= self._search_limit:
            raise RuntimeError(f"{TOO_MANY} ({len(found)} returned)")
        issues = [dict(i) for i in found]
        index = {
            (str(i.get("repo") or "").lower(), i.get("number")): i for i in issues
        }
        # A cached answer is only good while the issue stays assigned.
        current = {
            seen_key(str(i.get("repo") or ""), i.get("number")).lower()
            for i in issues
        }
        for key in list(self._assignment_cache):
            if key.lower() not in current:
                del self._assignment_cache[key]
        for repo_cfg in self._config.repos:
            for label in repo_cfg.automation.include_labels:
                rows = await self._github.list_issues_by_label(
                    repo_cfg.name, label=label,
                )
                for row in rows:
                    ident = (repo_cfg.name.lower(), row.get("number"))
                    existing = index.get(ident)
                    if existing is None:
                        merged = {**row, "repo": repo_cfg.name, VIA_LABEL: True}
                        index[ident] = merged
                        issues.append(merged)
                        continue
                    names = {_label_name(lab).lower() for lab in existing.get("labels") or []}
                    extra = [
                        lab for lab in row.get("labels") or []
                        if _label_name(lab).lower() not in names
                    ]
                    if extra:
                        existing["labels"] = [*(existing.get("labels") or []), *extra]
        return issues

    async def _self_assigned(
        self, username: str, repo: str, number: int, updated_ts: int, now: int,
    ) -> bool:
        """The poller's own self-assignment check, cached. A failed check
        raises and is not cached, so it is asked again next time."""
        key = seen_key(repo, number)
        cached = self._assignment_cache.get(key)
        if cached is not None:
            value, checked_ts, cached_updated_ts = cached
            if cached_updated_ts == updated_ts and (
                value or now - checked_ts < FOREIGN_RECHECK_S
            ):
                return value
        if self._poller is None:
            # Built only to borrow its check; it reads the poller's state
            # file and writes nothing unless polled, which it never is here.
            self._poller = IssuePoller(
                github=self._github,
                username=username,
                repos=[r.name for r in self._config.repos],
                state_file=self._poller_state_file,
            )
        value = await self._poller._is_self_assigned(repo, number)
        self._assignment_cache[key] = (value, now, updated_ts)
        return value

    def titles(self) -> dict[str, str]:
        """Issue titles keyed ``"owner/repo#N"``, from the last successful
        search (unfiltered). Empty until a search has succeeded; a failed
        refresh keeps the previous titles."""
        return dict(self._titles)

    def snapshot(self) -> dict[str, Any]:
        snap = self._snapshot
        return {**snap, "items": [dict(i) for i in snap["items"]]}
