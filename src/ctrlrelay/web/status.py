"""The status page's JSON, built from a read-only snapshot of ``state.db`` (#209).

Read only. Every connection is opened by ``_open`` with ``mode=ro``.
Only ``_read`` may also ask for ``immutable=1`` (when nobody else has the
database open), because only it checks afterwards that the file did not
change; the public :func:`open_readonly` never does. Nothing here opens the
database any other way. It never modifies the database's content and never takes a write
lock. One known limit: if the daemon closes the database between the
sidecar check and the connect, SQLite may leave empty ``-wal``/``-shm``
files, holding no data, which are what the daemon itself creates on open.

Never a confident wrong answer. A section is either complete or ``null``,
never partial: one row that cannot be read or shaped makes its whole
section ``null``. A section that could not be read is
``null`` (``today``: every count ``null``) and ``errors`` says why. An empty
list means the database was read and holds nothing, and only that. A
database that is missing, locked or unreadable gives ``200``-shaped output
with every database section ``null``; one older than the ``events`` table
still gives its sessions and questions, with ``feed``/``today`` ``null``.

Contract (keys exactly; every timestamp is ``<name>_ts`` epoch seconds plus
``<name>`` ISO 8601 UTC)::

    {"version", "host", "generated", "generated_ts",
     "running": [{"session_id", "pipeline", "repo", "issue_number", "title",
                  "started", "started_ts", "elapsed_s", "phase",
                  "current_activity", "phase_source", "last_command",
                  "last_step", "last_step_ts", "looks_stuck", "reason", "url",
                  "pr_url"}] | null,
     "needs_you": [{"session_id", "pipeline", "repo", "issue_number",
                    "title", "question", "asked", "asked_ts", "held_until",
                    "held_until_ts", "url", "refs"}] | null,
     "waiting": <WaitingTracker.snapshot() without "skipped">,
     "skipped": [{"repo", "issue_number", "title", "reason", "url",
                  "updated", "updated_ts"}] | null,
     "server": <web.health.read_server_health()> | null,
     "today": {"dispatched", "pr_opened", "merged", "failed", "blocked",
               "messages_sent"},   # ints, or all null if unreadable
     "feed": [{"ts", "time", "kind", "repo", "issue_number", "session_id",
               "detail", "url"}] | null,   # newest first, at most FEED_LIMIT
     "feed_total": int | null,       # events in the history window
     "feed_truncated": bool,         # feed_total > len(feed)
     "feed_limit": int,              # the most events feed can hold
     "errors": [str]}

``feed`` holds at most ``feed_limit`` (``FEED_LIMIT``) events. ``feed_total`` counts every
event in the history window, read in the same transaction; it is ``null``
whenever ``feed`` is, and ``feed_truncated`` is then ``false``.

``issue_number`` in ``needs_you`` comes from the question's session row
(``null`` for secops, or when the row is gone). ``title`` (running and
needs_you) comes from the ``titles`` map, ``WaitingTracker.titles()``, keyed
``"owner/repo#N"`` and matched case-insensitively; ``null`` when absent.

``url`` (#232) is a github.com link built by ``web.links.github_url`` from
the repo and a number, or ``null``: the issue for running, needs_you,
waiting and skipped items; for a feed event, the PR when its detail has an
int ``pr_number``, else its issue. ``skipped`` (assigned open issues the
poller will not take, with why) is from the same GitHub answer as
``waiting``, so ``waiting.status`` and ``waiting.last_good_ts`` say how
current it is; it is ``null`` until GitHub has answered once. ``server``
is ``null`` (and ``errors`` says why) only when reading it failed as a
whole; a single number it could not read is ``null`` inside it, with the
reason in ``server.unavailable``.

Phase (#238): ``phase`` is the furthest step reached, in the order
reading < coding < testing < ci < review < pr, over the transcript tail and
the session's pipeline events (``ci_*``, ``review_*``, ``pr_opened``); the
bar never moves back, and it can come from history (a CI wait that has
finished). ``current_activity`` is the most recent step by time: an open
CI wait or review run by ctrlrelay itself (a ``ci_started``/
``review_started`` event with no later matching ``*_finished``, and no
poller ``restart`` since) when it started after the transcript's latest
classified step, else that transcript step. ``phase_source`` says where
``current_activity`` came from: ``"pipeline"``, ``"transcript"``, or
``"none"`` when it is ``"unknown"``. While such a wait or review is open the
card is never ``looks_stuck``; otherwise it looks stuck when the agent's
last step is older than ``stale_after_s``.
``pr_url`` is the session's pull request, built by ``github_url`` from the
repo and the last int ``pr_number`` its pipeline events carry, else
``null``; a ``pr_url`` in an event's detail is never used.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from ctrlrelay.web.fleet import node_activity
from ctrlrelay.web.links import github_url
from ctrlrelay.web.transcript import Phase, furthest_of, read_activity
from ctrlrelay.web.waiting import SessionsReader

T = TypeVar("T")

# Seconds a read waits for a lock before the database is reported busy.
READ_TIMEOUT = 1.0
# How often a lock-free read is retried when the file changed under it.
_ATTEMPTS = 3
FEED_LIMIT = 200

_TODAY = {
    "dispatched": "dispatched",
    "pr_opened": "pr_opened",
    "merged": "merged",
    "failed": "failed",
    "blocked": "blocked",
    # Everything the bridge posted to chat. Relay alerts are delivered
    # through the bridge too, so ``alert_sent`` is not added on top.
    "message_sent": "messages_sent",
}
_OPENS = {"ci_started": Phase.CI, "review_started": Phase.REVIEW}
_CLOSES = {"ci_finished": Phase.CI, "review_finished": Phase.REVIEW}
# The phase an event shows a session reached, whether or not it is still open.
_REACHED = {**_OPENS, **_CLOSES, "pr_opened": Phase.PR}
_EVENTS_UNKNOWN = "CI and review state unknown: events not readable"


class _Missing:
    """A section whose table or column this database does not have."""


_MISSING = _Missing()


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat()


# --- read-only access --------------------------------------------------------


def _is_wal(db_path: Path) -> bool:
    """Whether the file's header says WAL (bytes 18-19 are both 2)."""
    with open(db_path, "rb") as fh:
        header = fh.read(20)
    return len(header) == 20 and header[18] == 2 and header[19] == 2


def _sidecars(db_path: Path) -> tuple[bool, bool]:
    return (
        os.path.exists(f"{db_path}-wal"),
        os.path.exists(f"{db_path}-shm"),
    )


def _open(
    db_path: Path, *, lock_free_ok: bool,
) -> tuple[sqlite3.Connection, bool]:
    """A read-only connection, and whether it reads without locks.

    A read-only open of a WAL database creates ``-wal`` and ``-shm`` when
    they are absent, which they are whenever no process has the database
    open. Then nothing can be in the WAL, so the main file is the whole
    database and is read as immutable, creating nothing. The caller must
    check the file did not change during such a read (see :func:`_read`).

    The check and the connect are not atomic: if a daemon closes the
    database in between, the plain read-only open may leave empty sidecars
    behind (see the module docstring). Content is never modified.
    """
    db_path = Path(db_path)
    immutable = False
    if _is_wal(db_path):  # raises if the file is missing
        wal, shm = _sidecars(db_path)
        if not wal and not shm:
            immutable = lock_free_ok
        elif not shm:
            raise sqlite3.OperationalError(
                f"{db_path} has a -wal but no -shm; reading it would create one"
            )
    quoted = urllib.parse.quote(str(db_path.absolute()), safe="/")
    uri = f"file:{quoted}?mode=ro" + ("&immutable=1" if immutable else "")
    conn = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn, immutable


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` read-only (``mode=ro``); never modifies its content.

    Raises if the file is missing. Always a plain ``mode=ro`` connection,
    never a lock-free one: that is safe only inside ``_read``.
    """
    conn, _ = _open(db_path, lock_free_ok=False)
    return conn


def _fingerprint(db_path: Path) -> tuple[Any, ...]:
    st = os.stat(db_path)
    return (st.st_ino, st.st_mtime_ns, st.st_size, *_sidecars(db_path))


def _read(db_path: Path, fn: Callable[[sqlite3.Connection], T]) -> T:
    """Run ``fn`` on a read-only connection, inside one read transaction.

    A lock-free read (nobody had the database open) is kept only if the file
    and its sidecars are unchanged afterwards; otherwise someone opened or
    wrote it meanwhile and the read is taken again. A lock-free read that
    fails with a database error may have met a write in progress, so it is
    retried too, up to ``_ATTEMPTS`` in all. A read under locks is not
    retried (a busy database would only multiply the wait). Raises on
    failure.
    """
    last_error: sqlite3.Error | None = None
    for _ in range(_ATTEMPTS):
        before = _fingerprint(db_path)
        conn, immutable = _open(db_path, lock_free_ok=True)
        try:
            conn.execute("BEGIN")
            result = fn(conn)
        except sqlite3.Error as exc:
            if not immutable:
                raise
            last_error = exc
            continue
        finally:
            conn.close()
        if not immutable or _fingerprint(db_path) == before:
            return result
        last_error = None
    if last_error is not None:
        raise last_error
    raise sqlite3.OperationalError(f"{db_path} kept changing while being read")


def _schema_missing(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc)
    return msg.startswith("no such table") or msg.startswith("no such column")


def _section(errors: list[str], name: str, fn: Callable[[], T]) -> T | _Missing:
    """``fn()``, or ``_MISSING`` with an error when this database lacks what
    it reads or a row cannot be shaped. A database failure (busy, locked,
    corrupt) propagates and voids the whole read."""
    try:
        return fn()
    except sqlite3.OperationalError as exc:
        if not _schema_missing(exc):
            raise
        errors.append(f"{name}: {exc}")
        return _MISSING
    except sqlite3.Error:
        raise
    except Exception as exc:  # a row that cannot be shaped
        errors.append(f"{name}: unreadable row: {type(exc).__name__}: {exc}")
        return _MISSING


# --- queries -------------------------------------------------------------------


def _read_running(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, pipeline, repo, issue_number, worktree_path, started_at
           FROM sessions WHERE status = 'running'
           ORDER BY started_at ASC, id ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def _read_locks(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT repo, session_id FROM repo_locks").fetchall()
    return {r["repo"]: r["session_id"] for r in rows}


# ``held_until`` included: without it nobody can say whether a question is
# held back, and listing it as "not held" would be a guess.
_NEEDS_REQUIRED = (
    "session_id", "pipeline", "repo", "question", "created_at",
    "answered_at", "expired_at", "held_until",
)


def _read_needs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Open, unanswered questions. A missing column raises, naming every
    missing column, so the section is unknown."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(pending_resumes)")}
    if not columns:
        raise sqlite3.OperationalError("no such table: pending_resumes")
    missing = [c for c in _NEEDS_REQUIRED if c not in columns]
    if missing:
        raise sqlite3.OperationalError(
            f"no such column: pending_resumes.{', '.join(missing)}"
        )
    rows = conn.execute(
        """SELECT pr.session_id, pr.pipeline, pr.repo, pr.question,
                   pr.created_at, pr.held_until,
                   s.issue_number AS issue_number
            FROM pending_resumes pr
            LEFT JOIN sessions s ON s.id = pr.session_id
            WHERE pr.answered_at IS NULL AND pr.expired_at IS NULL
            ORDER BY pr.created_at ASC, pr.session_id ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def _read_phase_events(
    conn: sqlite3.Connection, since_ts: int,
) -> list[dict[str, Any]]:
    kinds = [*_REACHED, "restart"]
    marks = ",".join("?" for _ in kinds)
    rows = conn.execute(
        f"""SELECT id, ts, kind, session_id, detail FROM events
            WHERE ts >= ? AND kind IN ({marks})
            ORDER BY ts ASC, id ASC""",
        (int(since_ts), *kinds),
    ).fetchall()
    return [dict(r) for r in rows]


def _read_today(conn: sqlite3.Connection, start: int, end: int) -> dict[str, int]:
    kinds = list(_TODAY)
    marks = ",".join("?" for _ in kinds)
    rows = conn.execute(
        f"""SELECT kind, COUNT(*) AS n FROM events
            WHERE ts >= ? AND ts < ? AND kind IN ({marks})
            GROUP BY kind""",
        (start, end, *kinds),
    ).fetchall()
    counts = dict.fromkeys(_TODAY.values(), 0)
    for r in rows:
        counts[_TODAY[r["kind"]]] = int(r["n"])
    return counts


def _read_feed(
    conn: sqlite3.Connection, since_ts: int, limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """The newest ``limit`` events with ``ts >= since_ts``, and how many
    events that window holds in all (read in the same transaction)."""
    rows = conn.execute(
        """SELECT ts, kind, repo, issue_number, session_id, detail FROM events
           WHERE ts >= ? ORDER BY ts DESC, id DESC LIMIT ?""",
        (int(since_ts), int(limit)),
    ).fetchall()
    (total,) = conn.execute(
        "SELECT count(*) FROM events WHERE ts >= ?", (int(since_ts),)
    ).fetchone()
    return [_event(dict(r)) for r in rows], int(total)


def _detail(raw: Any) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _event_url(repo: Any, issue_number: Any, detail: Any) -> str | None:
    """The PR when the event names one by number, else the issue, else
    ``None``. Built from the parts only: a ``pr_url`` in the detail was
    reported by an agent and is never used as a link."""
    if isinstance(detail, dict):
        pr = github_url(repo, detail.get("pr_number"), kind="pull")
        if pr is not None:
            return pr
    return github_url(repo, issue_number)


def _scrubbed(detail: Any) -> Any:
    """``detail`` with credential shapes removed from its text: a question
    the agent asked is stored as it wrote it, and the page shows it."""
    from ctrlrelay.core.redact import redact_secrets

    if isinstance(detail, dict):
        return {k: _scrubbed(v) for k, v in detail.items()}
    if isinstance(detail, list):
        return [_scrubbed(v) for v in detail]
    if isinstance(detail, str):
        return redact_secrets(detail)
    return detail


def _event(row: dict[str, Any]) -> dict[str, Any]:
    ts = int(row["ts"])
    detail = _scrubbed(_detail(row["detail"]))
    return {
        "ts": ts,
        "time": _iso(ts),
        "kind": row["kind"],
        "repo": row["repo"],
        "issue_number": row["issue_number"],
        "session_id": row["session_id"],
        "detail": detail,
        "url": _event_url(row["repo"], row["issue_number"], detail),
    }


# --- assembly --------------------------------------------------------------------


def _local_day(now: int) -> tuple[int, int]:
    """Epoch bounds of the local calendar day containing ``now``."""
    day = date.fromtimestamp(now)
    start = datetime.combine(day, datetime.min.time())
    end = datetime.combine(day + timedelta(days=1), datetime.min.time())
    return int(start.timestamp()), int(end.timestamp())


def _open_phases(events: list[dict[str, Any]]) -> dict[str, tuple[Phase, int]]:
    """Per session, the most recently started CI wait or review still open,
    and the ``ts`` of the event that started it."""
    open_: dict[str, dict[Phase, tuple[int, int]]] = {}
    for order, ev in enumerate(events):
        kind = ev["kind"]
        if kind == "restart":
            detail = _detail(ev.get("detail"))
            # CI waits and reviews run inside the poller: a poller restart
            # ends every one of them, whatever its last event said.
            if isinstance(detail, dict) and detail.get("process") == "poller":
                open_.clear()
            continue
        sid = ev.get("session_id")
        if sid is None:
            continue
        if kind in _OPENS:
            open_.setdefault(sid, {})[_OPENS[kind]] = (order, int(ev["ts"]))
        elif kind in _CLOSES:
            open_.get(sid, {}).pop(_CLOSES[kind], None)
    out: dict[str, tuple[Phase, int]] = {}
    for sid, phases in open_.items():
        if phases:
            latest = max(phases, key=lambda p: phases[p][0])
            out[sid] = (latest, phases[latest][1])
    return out


def _reached(
    events: list[dict[str, Any]],
) -> dict[str, tuple[Phase, int | None]]:
    """Per session, the furthest phase its pipeline events show it reached,
    and the PR number ctrlrelay recorded for it (the latest positive int
    ``pr_number`` in those events' detail, else ``None``). A restart closes
    a wait but does not undo having reached it."""
    out: dict[str, tuple[Phase, int | None]] = {}
    for ev in events:
        sid = ev.get("session_id")
        phase = _REACHED.get(ev["kind"])
        if sid is None or phase is None:
            continue
        before, pr = out.get(sid, (Phase.UNKNOWN, None))
        detail = _detail(ev.get("detail"))
        number = detail.get("pr_number") if isinstance(detail, dict) else None
        if type(number) is int and number > 0:
            pr = number
        out[sid] = (furthest_of(before, phase), pr)
    return out


def _titles(titles: Mapping[str, str] | None) -> dict[tuple[str, int], str]:
    """(lower-cased repo, number) -> title, from the tracker's unfiltered
    ``titles`` map only."""
    out: dict[tuple[str, int], str] = {}
    for key, title in (titles or {}).items():
        repo, sep, number = str(key).rpartition("#")
        if not sep or not isinstance(title, str) or not title:
            continue
        try:
            out[(repo.lower(), int(number))] = title
        except ValueError:
            continue
    return out


def _title_for(
    titles: dict[tuple[str, int], str], repo: Any, issue: Any,
) -> str | None:
    if issue is None:
        return None
    try:
        return titles.get((str(repo).lower(), int(issue)))
    except (TypeError, ValueError):
        return None


def _card(
    row: dict[str, Any],
    *,
    now: int,
    pipeline_phases: dict[str, tuple[Phase, int]] | None,
    reached: dict[str, tuple[Phase, int | None]] | None,
    titles: dict[tuple[str, int], str],
    stale_after_s: int,
    home: Path | None,
) -> dict[str, Any]:
    sid = row["id"]
    started = int(row["started_at"])
    activity = read_activity(row.get("worktree_path"), home=home)
    reasons = [activity.reason] if activity.reason else []

    pipeline_phase = None
    pipeline_ts: int | None = None
    if pipeline_phases is None:
        reasons.append(_EVENTS_UNKNOWN)
    elif sid in pipeline_phases:
        pipeline_phase, pipeline_ts = pipeline_phases[sid]

    reached_phase, pr_number = (reached or {}).get(sid, (Phase.UNKNOWN, None))
    # The bar shows the furthest phase reached (#238); current_activity
    # shows what is happening now, which may legitimately be earlier (a
    # read after the PR, a fix round after review).
    phase = furthest_of(activity.furthest, pipeline_phase, reached_phase)
    # The most recent step is the current activity: an open CI wait or
    # review only when it started after the transcript's latest step.
    transcript_known = activity.phase != Phase.UNKNOWN
    pipeline_newer = pipeline_phase is not None and (
        not transcript_known
        or activity.phase_ts is None
        or (pipeline_ts is not None and pipeline_ts > activity.phase_ts)
    )
    if pipeline_newer:
        current, source = pipeline_phase, "pipeline"
    elif transcript_known:
        current, source = activity.phase, "transcript"
    else:
        current, source = Phase.UNKNOWN, "none"

    looks_stuck = (
        pipeline_phases is not None
        and pipeline_phase is None
        and activity.last_step_ts is not None
        and now - activity.last_step_ts > stale_after_s
    )
    issue = row.get("issue_number")
    title = _title_for(titles, row["repo"], issue)
    return {
        "session_id": sid,
        "pipeline": row["pipeline"],
        "repo": row["repo"],
        "issue_number": issue,
        "title": title,
        "started": _iso(started),
        "started_ts": started,
        "elapsed_s": max(0, now - started),
        "phase": str(phase),
        "current_activity": str(current),
        "phase_source": source,
        "last_command": activity.last_command,
        "last_step": _iso(activity.last_step_ts),
        "last_step_ts": activity.last_step_ts,
        "looks_stuck": looks_stuck,
        "reason": "; ".join(reasons) or None,
        "url": github_url(row["repo"], issue),
        # Built from the session's repo and the number ctrlrelay recorded,
        # never from a URL an agent reported.
        "pr_url": github_url(row["repo"], pr_number, kind="pull"),
    }


# A ``#N`` in a question's text: not after a word character, ``/`` or
# ``&`` (an HTML entity or a path is not a reference). The page uses the
# same pattern, QUESTION_REF, and links only the numbers listed here.
# ASCII, because a JavaScript ``\w``, ``\d`` and ``\b`` are ASCII only.
_QUESTION_REF = re.compile(r"(?<![\w/&])#(\d{1,7})\b", re.ASCII)
_MAX_REFS = 20


def _question_refs(repo: Any, question: Any) -> list[dict[str, Any]]:
    """#241: one ``{"number", "url"}`` per distinct ``#N`` in ``question``,
    in order and at most ``_MAX_REFS`` of them, with the link built by
    ``github_url`` from ``repo`` and N. A secops question has no issue, so
    these are its only links. Numbers that cannot be linked are left out."""
    if not isinstance(question, str):
        return []
    refs: list[dict[str, Any]] = []
    seen: set[int] = set()
    for m in _QUESTION_REF.finditer(question):
        number = int(m.group(1))
        url = github_url(repo, number)
        if number in seen or url is None:
            continue
        seen.add(number)
        refs.append({"number": number, "url": url})
        if len(refs) == _MAX_REFS:
            break
    return refs


def _question(
    row: dict[str, Any], titles: dict[tuple[str, int], str],
) -> dict[str, Any]:
    asked = row.get("created_at")
    held = row.get("held_until")
    issue = row.get("issue_number")
    return {
        "session_id": row["session_id"],
        "pipeline": row["pipeline"],
        "repo": row["repo"],
        "issue_number": issue,
        "title": _title_for(titles, row["repo"], issue),
        "question": row["question"],
        "asked": _iso(asked),
        "asked_ts": asked,
        "held_until": _iso(held),
        "held_until_ts": held,
        "url": github_url(row["repo"], issue),
        "refs": _question_refs(row["repo"], row["question"]),
    }


def build_status(
    *,
    db_path: Path,
    waiting: dict,
    server: Callable[[], dict],
    version: str,
    host: str,
    now: int | None = None,
    stale_after_s: int = 900,
    history_days: int = 7,
    home: Path | None = None,
    titles: Mapping[str, str] | None = None,
    node: Callable[[], dict] | None = None,
    heartbeat: Callable[[], dict | None] | None = None,
    silent_after_s: int = 300,
    fleet: Callable[[], dict] | None = None,
) -> dict:
    """Everything the page shows, as the documented JSON contract. Never
    raises for a database problem; see the module docstring.

    ``server`` reads the machine's health (``web.health``); it is called
    here so that a reader that raises makes the section ``null`` with an
    error, never a partial one. ``waiting`` is the tracker's snapshot; its
    ``skipped`` list is published as the top-level ``skipped``.

    ``node`` reads this machine's own block (#247: its usage); like
    ``server``, a reader that raises makes it ``null`` with an error."""
    if now is None:
        now = int(datetime.now(UTC).timestamp())
    errors: list[str] = []
    waiting = dict(waiting)
    skipped = waiting.pop("skipped", None)
    if waiting.get("last_good_ts") is None or not isinstance(skipped, list):
        # Before GitHub has answered once the tracker's list is empty, which
        # would read as "nothing skipped", so it is published as null.
        skipped = None
    try:
        server_health: dict | None = server()
    except Exception as exc:
        errors.append(f"server: {type(exc).__name__}: {exc}"[:300])
        server_health = None
    node_block: dict | None = None
    if node is not None:
        try:
            node_block = node()
        except Exception as exc:
            errors.append(f"node: {type(exc).__name__}: {exc}"[:300])
    day_start, day_end = _local_day(now)
    feed_since = now - history_days * 86_400
    feed_limit = FEED_LIMIT

    def read_all(conn: sqlite3.Connection) -> dict[str, Any]:
        section_errors: list[str] = []
        running = _section(section_errors, "sessions", lambda: _read_running(conn))
        starts = (
            [r["started_at"] for r in running] if isinstance(running, list) else []
        )
        # A corrupt start time is the sessions section's problem (it is
        # reported there); it must not break the events read.
        since = min((t for t in starts if type(t) is int), default=now)
        return {
            "running": running,
            "needs": _section(section_errors, "pending_resumes", lambda: _read_needs(conn)),
            "phase_events": _section(
                section_errors, "events", lambda: _read_phase_events(conn, since)
            ),
            "today": _section(
                section_errors, "events", lambda: _read_today(conn, day_start, day_end)
            ),
            "feed": _section(
                section_errors, "events", lambda: _read_feed(conn, feed_since, feed_limit)
            ),
            "errors": section_errors,
        }

    try:
        snap = _read(Path(db_path), read_all)
    except Exception as exc:  # missing, locked, corrupt: every DB section unknown
        errors.append(f"state.db could not be read ({db_path}): {type(exc).__name__}: {exc}")
        snap = None

    running: list[dict[str, Any]] | None = None
    needs_you: list[dict[str, Any]] | None = None
    feed: list[dict[str, Any]] | None = None
    feed_total: int | None = None
    today: dict[str, int | None] = dict.fromkeys(_TODAY.values())
    if snap is not None:
        for err in snap["errors"]:
            if err not in errors:
                errors.append(err)
        phase_events = snap["phase_events"]
        pipeline_phases = None
        reached = None
        if not isinstance(phase_events, _Missing):
            phases = _section(errors, "events", lambda: _open_phases(phase_events))
            pipeline_phases = None if isinstance(phases, _Missing) else phases
            got = _section(errors, "events", lambda: _reached(phase_events))
            reached = None if isinstance(got, _Missing) else got
        title_map = _titles(titles)
        # Shaping happens inside the section guard: one row that cannot be
        # shaped makes the whole section unknown, never a shorter list.
        if not isinstance(snap["running"], _Missing):
            cards = _section(errors, "sessions", lambda: [
                _card(
                    row, now=now, pipeline_phases=pipeline_phases, reached=reached,
                    titles=title_map,
                    stale_after_s=stale_after_s, home=home,
                )
                for row in snap["running"]
            ])
            running = None if isinstance(cards, _Missing) else cards
        if not isinstance(snap["needs"], _Missing):
            shaped = _section(errors, "pending_resumes", lambda: [
                _question(row, title_map) for row in snap["needs"]
            ])
            needs_you = None if isinstance(shaped, _Missing) else shaped
        if not isinstance(snap["today"], _Missing):
            today = dict(snap["today"])
        if not isinstance(snap["feed"], _Missing):
            feed, feed_total = snap["feed"]

    if node_block is not None:
        try:
            beat = heartbeat() if heartbeat is not None else None
        except Exception as exc:
            errors.append(f"heartbeat: {type(exc).__name__}: {exc}"[:300])
            beat = None
        node_block = {
            "version": version,
            **node_block,
            **node_activity(
                running=running, needs_you=needs_you, heartbeat=beat,
                paused=node_block.get("paused"), now=now,
                silent_after=silent_after_s,
            ),
        }
    fleet_block: dict | None = None
    if fleet is not None:
        try:
            fleet_block = fleet()
        except Exception as exc:
            errors.append(f"fleet: {type(exc).__name__}: {exc}"[:300])

    return {
        "version": version,
        "host": host,
        "node": node_block,
        "fleet": fleet_block,
        "generated": _iso(now),
        "generated_ts": now,
        "running": running,
        "needs_you": needs_you,
        "waiting": waiting,
        "skipped": skipped,
        "server": server_health,
        "today": today,
        "feed": feed,
        "feed_total": feed_total,
        "feed_truncated": feed is not None and feed_total is not None
        and feed_total > len(feed),
        "feed_limit": feed_limit,
        "errors": errors,
    }


def list_feed(*, db_path: Path, since_ts: int, limit: int | None = None) -> dict:
    """The newest ``limit`` (default ``FEED_LIMIT``) events with
    ``ts >= since_ts``, newest first:
    ``{"events", "feed_total", "feed_truncated", "limit", "error"}``.
    ``feed_total`` counts every event in the window; ``feed_truncated`` is
    true when that is more than ``events`` holds. ``events`` and
    ``feed_total`` are ``null`` (and ``feed_truncated`` false) when they
    could not be read."""
    if limit is None:
        limit = FEED_LIMIT
    try:
        events, total = _read(
            Path(db_path), lambda conn: _read_feed(conn, since_ts, limit)
        )
    except Exception as exc:
        return {
            "events": None,
            "feed_total": None,
            "feed_truncated": False,
            "limit": limit,
            "error": f"events could not be read ({db_path}): {type(exc).__name__}: {exc}",
        }
    return {
        "events": events,
        "feed_total": total,
        "feed_truncated": total > len(events),
        "limit": limit,
        "error": None,
    }


# The kinds a Today counter counts, which ``list_day_events`` lists (#257).
DAY_KINDS = frozenset(_TODAY)
# How much of a failed run's stored output and summary a row carries.
REASON_TAIL_CHARS = 2000
REASON_SUMMARY_CHARS = 500


def _read_day_events(
    conn: sqlite3.Connection, kind: str, start: int, end: int, limit: int,
) -> tuple[list[dict[str, Any]], int]:
    rows = conn.execute(
        """SELECT e.ts, e.kind, e.repo, e.issue_number, e.session_id, e.detail,
                  s.id AS s_id, s.claude_exit_code, s.summary, s.output_tail,
                  s.blocked_question
           FROM events e LEFT JOIN sessions s ON s.id = e.session_id
           WHERE e.kind = ? AND e.ts >= ? AND e.ts < ?
           ORDER BY e.ts DESC, e.id DESC LIMIT ?""",
        (kind, start, end, int(limit)),
    ).fetchall()
    (total,) = conn.execute(
        "SELECT count(*) FROM events WHERE kind = ? AND ts >= ? AND ts < ?",
        (kind, start, end),
    ).fetchone()
    out = []
    for r in rows:
        row = dict(r)
        event = _event(row)
        if kind in ("failed", "blocked"):
            event["reason"] = _reason(row, event["detail"])
        out.append(event)
    return out, int(total)


def _cut(text: str, limit: int) -> str:
    """``text`` up to ``limit`` characters, ending in "…" when cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _reason(row: dict[str, Any], detail: Any) -> dict[str, Any]:
    """Why a run failed or stopped, from what the relay stored: the exit
    code, its summary and output tail, and the question asked, each
    scrubbed of credential shapes again here. ``session`` says when the run's row is gone."""
    from ctrlrelay.core.redact import redact_secrets, tail

    detail = detail if isinstance(detail, dict) else {}
    exit_code = detail.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = row.get("claude_exit_code")
    question = detail.get("question") or row.get("blocked_question")
    summary = row.get("summary")
    output = row.get("output_tail")
    reason: dict[str, Any] = {
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "summary": (
            _cut(redact_secrets(summary), REASON_SUMMARY_CHARS)
            if isinstance(summary, str) and summary else None
        ),
        "output_tail": (
            tail(redact_secrets(output), REASON_TAIL_CHARS)
            if isinstance(output, str) and output else None
        ),
        "question": (
            _cut(redact_secrets(question), REASON_SUMMARY_CHARS)
            if isinstance(question, str) and question else None
        ),
    }
    if row.get("s_id") is None:
        reason["session"] = "not found"
    return reason


def list_day_events(
    *, db_path: Path, kind: str, now: int | None = None, limit: int | None = None,
) -> dict:
    """Today's events of one counted ``kind``, newest first, for the Today
    counters (#257): ``{"kind", "events", "total", "truncated", "error"}``.
    A failed or blocked event carries ``reason``. ``events`` and ``total``
    are ``null`` when they could not be read, never an empty list."""
    if limit is None:
        limit = FEED_LIMIT
    start, end = _local_day(int(now if now is not None else time.time()))
    try:
        events, total = _read(
            Path(db_path),
            lambda conn: _read_day_events(conn, kind, start, end, limit),
        )
    except Exception as exc:
        return {
            "kind": kind, "events": None, "total": None, "truncated": False,
            "error": f"events could not be read ({db_path}): {type(exc).__name__}: {exc}",
        }
    return {
        "kind": kind, "events": events, "total": total,
        "truncated": total > len(events), "error": None,
    }


def make_sessions_reader(db_path: Path) -> SessionsReader:
    """A ``SessionsReader`` for ``WaitingTracker``: running sessions (oldest
    first) and repo locks, read-only. Raises on any database failure, which
    the tracker reports as an unknown waiting line."""
    path = Path(db_path)

    def read() -> tuple[list[dict[str, Any]], dict[str, str]]:
        rows, locks = _read(path, lambda conn: (_read_running(conn), _read_locks(conn)))
        running = [
            {
                "session_id": r["id"],
                "repo": r["repo"],
                "issue_number": r["issue_number"],
                "pipeline": r["pipeline"],
            }
            for r in rows
        ]
        return running, locks

    return read
