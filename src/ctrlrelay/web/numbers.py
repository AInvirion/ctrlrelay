"""How well this node's runs are going, over the last 7 and 30 days (#230).

Per node only: numbers across machines belong to the control plane (#251),
which can read each node's ``/api/status``. Everything here is computed
from what the state database already holds; nothing new is recorded.

A **run** is one row in ``sessions`` (one dispatch of one pipeline on one
repo; a blocked run resumed after an answer is the same row). A run is in
a window when its ``started_at`` is in ``[now - days * 86400, now]``, both
ends included. Its outcome is the row's ``status`` as it is now, so a run
that was blocked and has since been answered and finished counts as done.

Per window, overall and per repo:

- ``started``: runs started in the window.
- ``done``, ``failed``, ``blocked``: of those, the ones whose status is now
  that value. ``running``: still ``running`` (working now, or left behind by
  a daemon that stopped mid-run). ``other``: any other status, so the five
  always add up to ``started``.
- ``success_rate``: ``done / (done + failed + blocked)``, rounded to three
  places; ``null`` when none of the runs has finished. Runs still running
  are left out of both sides.
- ``time_to_pr_s``: ``{"median", "n"}`` over the runs ctrlrelay recorded a
  PR for: seconds from the run's ``started_at`` to its first ``pr_opened``
  event (recorded once the PR is confirmed on GitHub, before CI is waited
  for). ``n`` is how many runs that is; ``median`` is ``null`` when ``n`` is
  0. ``time_to_merge_s`` is the same, to the run's first ``merged`` event.
- ``came_back``: distinct issues dispatched again in the window after an
  earlier run on the same repo and issue had ended ``done`` (its
  ``ended_at`` is before the new run's ``started_at``). The earlier run may
  be older than the window. The window's ``came_back_issues`` lists them.
  Reopening on GitHub itself is not recorded by ctrlrelay, so an issue that
  was reopened and never picked up again is not counted.

``null`` where a number cannot be known, with the reason in the window's
``unavailable``, never a guess:

- ``cost_per_run``: always ``null``; cost per run is not recorded (#223).
- ``stuck``: always ``null``; no run records that it was stuck (#224). A
  stuck run that is still marked running is in ``running``.
- ``time_to_pr_s``/``time_to_merge_s``: ``null`` when the events table
  cannot be read, or when the events it still holds cannot cover every run
  in the window. The daemon deletes events older than ``web.history_days``
  (oldest first), so every event at or after the oldest one left is still
  there, and a run that started at or after it has all its events. A run
  that started before it may have lost its ``pr_opened``. So the times are
  shown only when no run in the window started before the oldest event
  kept (and, with runs in the window, at least one event is kept). This is
  decided from the database, never from the setting: raising
  ``web.history_days`` does not bring deleted events back.

Events stamped after ``now`` (a clock that was corrected) are not read.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ctrlrelay.web.links import github_url

WINDOWS = (7, 30)
# The most issues one window's ``came_back_issues`` lists; ``came_back``
# still counts all of them.
CAME_BACK_LIMIT = 50
_PR_KINDS = ("pr_opened", "merged")
_OUTCOMES = ("done", "failed", "blocked", "running")
COST_GAP = "cost per run is not recorded yet (#223)"
STUCK_GAP = (
    "no run records that it was stuck yet (#224); a stuck run still marked "
    "running is counted in running"
)
EVENTS_GAP = "events could not be read"


def window_start(now: int, days: int) -> int:
    return int(now) - int(days) * 86_400


# --- reads (inside the caller's read transaction) ------------------------------


def read_runs(conn: Any, since: int, now: int) -> list[dict[str, Any]]:
    """Every run started in ``[since, now]``."""
    rows = conn.execute(
        """SELECT id, repo, issue_number, status, started_at FROM sessions
           WHERE started_at >= ? AND started_at <= ?""",
        (int(since), int(now)),
    ).fetchall()
    return [dict(r) for r in rows]


def read_came_back(conn: Any, since: int, now: int) -> list[dict[str, Any]]:
    """Runs started in ``[since, now]`` on an issue where an earlier run had
    already ended ``done`` before this one started."""
    rows = conn.execute(
        """SELECT DISTINCT l.id, l.repo, l.issue_number, l.started_at
           FROM sessions l JOIN sessions d
             ON d.repo = l.repo AND d.issue_number = l.issue_number
            AND d.id != l.id AND d.status = 'done'
            AND d.ended_at IS NOT NULL AND d.ended_at < l.started_at
           WHERE l.issue_number IS NOT NULL
             AND l.started_at >= ? AND l.started_at <= ?""",
        (int(since), int(now)),
    ).fetchall()
    return [dict(r) for r in rows]


def read_pr_times(conn: Any, since: int, now: int) -> dict[str, Any]:
    """``{"times", "oldest_ts"}``: per session, the first ``pr_opened`` and
    ``merged`` event time in ``[since, now]``, and the time of the oldest
    event the table still holds (``None`` when it holds none)."""
    marks = ",".join("?" for _ in _PR_KINDS)
    rows = conn.execute(
        f"""SELECT session_id, kind, MIN(ts) AS ts FROM events
            WHERE session_id IS NOT NULL AND ts >= ? AND ts <= ?
              AND kind IN ({marks})
            GROUP BY session_id, kind""",
        (int(since), int(now), *_PR_KINDS),
    ).fetchall()
    times: dict[str, dict[str, int]] = {}
    for r in rows:
        times.setdefault(r["session_id"], {})[r["kind"]] = int(r["ts"])
    (oldest,) = conn.execute("SELECT MIN(ts) FROM events").fetchone()
    return {"times": times, "oldest_ts": None if oldest is None else int(oldest)}


def _coverage_gap(
    runs: list[dict[str, Any]], oldest_ts: int | None, days: int,
) -> str | None:
    """Why the kept events cannot give these runs' PR times, or ``None``
    when they can (see the module docstring)."""
    if not runs:
        return None
    if oldest_ts is None:
        return "no events are kept, so these runs' PR times cannot be read"
    early = sum(1 for r in runs if r["started_at"] < oldest_ts)
    if not early:
        return None
    kept = datetime.fromtimestamp(oldest_ts, UTC).isoformat()
    return (
        f"the oldest event kept is from {kept}, after {early} of these runs "
        f"started, so their PR times may have been deleted; keep events for "
        f"at least {days} days (web.history_days) to show this"
    )


# --- shaping -------------------------------------------------------------------


def _median(values: list[int]) -> dict[str, Any]:
    return {
        "median": int(round(statistics.median(values))) if values else None,
        "n": len(values),
    }


def _counts(
    runs: list[dict[str, Any]],
    pr_times: dict[str, dict[str, int]] | None,
    came_back: int,
) -> dict[str, Any]:
    by_status = dict.fromkeys(_OUTCOMES, 0)
    other = 0
    to_pr: list[int] = []
    to_merge: list[int] = []
    for run in runs:
        started = run["started_at"]
        if type(started) is not int:
            raise TypeError(f"session {run['id']!r} has started_at {started!r}")
        if run["status"] in by_status:
            by_status[run["status"]] += 1
        else:
            other += 1
        if pr_times is not None:
            times = pr_times.get(run["id"], {})
            if "pr_opened" in times:
                to_pr.append(max(0, times["pr_opened"] - started))
            if "merged" in times:
                to_merge.append(max(0, times["merged"] - started))
    finished = by_status["done"] + by_status["failed"] + by_status["blocked"]
    return {
        "started": len(runs),
        **by_status,
        "other": other,
        "success_rate": (
            round(by_status["done"] / finished, 3) if finished else None
        ),
        "time_to_pr_s": None if pr_times is None else _median(to_pr),
        "time_to_merge_s": None if pr_times is None else _median(to_merge),
        "came_back": came_back,
        "stuck": None,
        "cost_per_run": None,
    }


def _issues(rows: Iterable[dict[str, Any]]) -> list[tuple[str, int]]:
    return sorted({(r["repo"], int(r["issue_number"])) for r in rows})


def _window(
    days: int,
    *,
    now: int,
    runs: list[dict[str, Any]],
    came_back: list[dict[str, Any]],
    pr_times: dict[str, dict[str, int]] | None,
    oldest_ts: int | None,
    events_reason: str | None,
) -> dict[str, Any]:
    since = window_start(now, days)
    runs = [r for r in runs if r["started_at"] >= since]
    back = _issues(r for r in came_back if r["started_at"] >= since)

    unavailable = {"cost_per_run": COST_GAP, "stuck": STUCK_GAP}
    times = pr_times
    reason = events_reason
    if reason is None:
        reason = _coverage_gap(runs, oldest_ts, days)
    if reason is not None or times is None:
        times = None
        reason = reason or EVENTS_GAP
        unavailable["time_to_pr_s"] = reason
        unavailable["time_to_merge_s"] = reason

    repos = sorted({r["repo"] for r in runs} | {repo for repo, _ in back})
    by_repo = []
    for repo in repos:
        mine = [r for r in runs if r["repo"] == repo]
        n_back = sum(1 for rp, _ in back if rp == repo)
        by_repo.append({"repo": repo, **_counts(mine, times, n_back)})

    return {
        "days": days,
        "since_ts": since,
        "since": datetime.fromtimestamp(since, UTC).isoformat(),
        "until_ts": int(now),
        "until": datetime.fromtimestamp(now, UTC).isoformat(),
        "overall": _counts(runs, times, len(back)),
        "by_repo": by_repo,
        "came_back_issues": [
            {"repo": repo, "issue_number": n, "url": github_url(repo, n)}
            for repo, n in back[:CAME_BACK_LIMIT]
        ],
        "unavailable": unavailable,
    }


def build_numbers(
    *,
    now: int,
    runs: list[dict[str, Any]],
    came_back: list[dict[str, Any]],
    events: dict[str, Any] | None,
    events_reason: str | None = None,
) -> dict[str, Any]:
    """``{"windows": [<7 days>, <30 days>]}`` from rows read for the longest
    window. ``events`` is :func:`read_pr_times`'s answer, or ``None`` when
    the events could not be read (``events_reason`` says why)."""
    if events is None and events_reason is None:
        events_reason = EVENTS_GAP
    pr_times = None if events is None else events["times"]
    oldest_ts = None if events is None else events["oldest_ts"]
    return {
        "windows": [
            _window(
                days, now=now, runs=runs, came_back=came_back,
                pr_times=pr_times, oldest_ts=oldest_ts,
                events_reason=events_reason,
            )
            for days in WINDOWS
        ],
    }
