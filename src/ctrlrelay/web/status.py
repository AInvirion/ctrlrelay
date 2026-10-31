"""The status page's JSON, built from a read-only snapshot of ``state.db`` (#209).

Never writes. Every read goes through :func:`open_readonly`; nothing here
opens the database any other way.

Never a confident wrong answer. A section that could not be read is
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
                  "phase_source", "last_command", "last_step", "last_step_ts",
                  "looks_stuck", "reason"}] | null,
     "needs_you": [{"session_id", "pipeline", "repo", "question", "asked",
                    "asked_ts", "held_until", "held_until_ts"}] | null,
     "waiting": <WaitingTracker.snapshot()>,
     "today": {"dispatched", "pr_opened", "merged", "failed", "blocked",
               "messages_sent"},   # ints, or all null if unreadable
     "feed": [{"ts", "time", "kind", "repo", "issue_number", "session_id",
               "detail"}] | null,
     "errors": [str]}

Phase: while ctrlrelay itself has a CI wait or a review open for a session
(a ``ci_started``/``review_started`` event with no later matching
``*_finished``, and no poller ``restart`` since), that is the phase
(``phase_source: "pipeline"``) and the card is never ``looks_stuck``.
Otherwise the phase comes from the agent's transcript, and the card looks
stuck when its last step is older than ``stale_after_s``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from ctrlrelay.web.transcript import Phase, read_activity
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


def _open(db_path: Path) -> tuple[sqlite3.Connection, bool]:
    """A read-only connection, and whether it reads without locks.

    A read-only open of a WAL database creates ``-wal`` and ``-shm`` when
    they are absent, which they are whenever no process has the database
    open. Then nothing can be in the WAL, so the main file is the whole
    database and is read as immutable, creating nothing. The caller must
    check the file did not change during such a read (see :func:`_read`).
    """
    db_path = Path(db_path)
    immutable = False
    if _is_wal(db_path):  # raises if the file is missing
        wal, shm = _sidecars(db_path)
        if not wal and not shm:
            immutable = True
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
    """Open ``db_path`` read-only (``mode=ro``) without creating any file.

    Raises if the file is missing. When nobody else has a WAL database open
    the connection reads it without locks; use it for one short read.
    """
    conn, _ = _open(db_path)
    return conn


def _fingerprint(db_path: Path) -> tuple[Any, ...]:
    st = os.stat(db_path)
    return (st.st_ino, st.st_mtime_ns, st.st_size, *_sidecars(db_path))


def _read(db_path: Path, fn: Callable[[sqlite3.Connection], T]) -> T:
    """Run ``fn`` on a read-only connection, inside one read transaction.

    A lock-free read (nobody had the database open) is kept only if the file
    and its sidecars are unchanged afterwards; otherwise someone opened or
    wrote it meanwhile and the read is taken again. Raises on any failure.
    """
    for _ in range(_ATTEMPTS):
        before = _fingerprint(db_path)
        conn, immutable = _open(db_path)
        try:
            conn.execute("BEGIN")
            result = fn(conn)
        finally:
            conn.close()
        if not immutable or _fingerprint(db_path) == before:
            return result
    raise sqlite3.OperationalError(f"{db_path} kept changing while being read")


def _schema_missing(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc)
    return msg.startswith("no such table") or msg.startswith("no such column")


def _section(errors: list[str], name: str, fn: Callable[[], T]) -> T | _Missing:
    """``fn()``, or ``_MISSING`` if this database lacks what it reads. Any
    other failure (busy, locked, corrupt) propagates and voids the read."""
    try:
        return fn()
    except sqlite3.OperationalError as exc:
        if not _schema_missing(exc):
            raise
        errors.append(f"{name}: {exc}")
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


def _read_needs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM pending_resumes
           WHERE answered_at IS NULL AND expired_at IS NULL
           ORDER BY created_at ASC, session_id ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def _read_phase_events(
    conn: sqlite3.Connection, since_ts: int,
) -> list[dict[str, Any]]:
    kinds = [*_OPENS, *_CLOSES, "restart"]
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


def _read_feed(conn: sqlite3.Connection, since_ts: int, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT ts, kind, repo, issue_number, session_id, detail FROM events
           WHERE ts >= ? ORDER BY ts DESC, id DESC LIMIT ?""",
        (int(since_ts), int(limit)),
    ).fetchall()
    return [_event(dict(r)) for r in rows]


def _detail(raw: Any) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _event(row: dict[str, Any]) -> dict[str, Any]:
    ts = int(row["ts"])
    return {
        "ts": ts,
        "time": _iso(ts),
        "kind": row["kind"],
        "repo": row["repo"],
        "issue_number": row["issue_number"],
        "session_id": row["session_id"],
        "detail": _detail(row["detail"]),
    }


# --- assembly --------------------------------------------------------------------


def _local_day(now: int) -> tuple[int, int]:
    """Epoch bounds of the local calendar day containing ``now``."""
    day = date.fromtimestamp(now)
    start = datetime.combine(day, datetime.min.time())
    end = datetime.combine(day + timedelta(days=1), datetime.min.time())
    return int(start.timestamp()), int(end.timestamp())


def _open_phases(events: list[dict[str, Any]]) -> dict[str, Phase]:
    """Per session, the most recently started CI wait or review still open."""
    open_: dict[str, dict[Phase, int]] = {}
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
            open_.setdefault(sid, {})[_OPENS[kind]] = order
        elif kind in _CLOSES:
            open_.get(sid, {}).pop(_CLOSES[kind], None)
    return {
        sid: max(phases, key=phases.__getitem__)
        for sid, phases in open_.items()
        if phases
    }


def _titles(waiting: dict[str, Any]) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    items = waiting.get("items") if isinstance(waiting, dict) else None
    for item in items or []:
        try:
            out[(str(item["repo"]).lower(), int(item["issue_number"]))] = str(item["title"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _card(
    row: dict[str, Any],
    *,
    now: int,
    pipeline_phases: dict[str, Phase] | None,
    titles: dict[tuple[str, int], str],
    stale_after_s: int,
    home: Path | None,
) -> dict[str, Any]:
    sid = row["id"]
    started = int(row["started_at"])
    activity = read_activity(row.get("worktree_path"), home=home)
    reasons = [activity.reason] if activity.reason else []

    pipeline_phase = None
    if pipeline_phases is None:
        reasons.append(_EVENTS_UNKNOWN)
    else:
        pipeline_phase = pipeline_phases.get(sid)

    if pipeline_phase is not None:
        phase, source = pipeline_phase, "pipeline"
    elif activity.phase != Phase.UNKNOWN:
        phase, source = activity.phase, "transcript"
    else:
        phase, source = Phase.UNKNOWN, "none"

    looks_stuck = (
        pipeline_phases is not None
        and pipeline_phase is None
        and activity.last_step_ts is not None
        and now - activity.last_step_ts > stale_after_s
    )
    issue = row.get("issue_number")
    title = None
    if issue is not None:
        title = titles.get((str(row["repo"]).lower(), int(issue)))
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
        "phase_source": source,
        "last_command": activity.last_command,
        "last_step": _iso(activity.last_step_ts),
        "last_step_ts": activity.last_step_ts,
        "looks_stuck": looks_stuck,
        "reason": "; ".join(reasons) or None,
    }


def _question(row: dict[str, Any]) -> dict[str, Any]:
    asked = row.get("created_at")
    held = row.get("held_until")
    return {
        "session_id": row["session_id"],
        "pipeline": row["pipeline"],
        "repo": row["repo"],
        "question": row["question"],
        "asked": _iso(asked),
        "asked_ts": asked,
        "held_until": _iso(held),
        "held_until_ts": held,
    }


def build_status(
    *,
    db_path: Path,
    waiting: dict,
    version: str,
    host: str,
    now: int | None = None,
    stale_after_s: int = 900,
    history_days: int = 7,
    home: Path | None = None,
) -> dict:
    """Everything the page shows, as the documented JSON contract. Never
    raises for a database problem; see the module docstring."""
    if now is None:
        now = int(datetime.now(UTC).timestamp())
    errors: list[str] = []
    day_start, day_end = _local_day(now)
    feed_since = now - history_days * 86_400

    def read_all(conn: sqlite3.Connection) -> dict[str, Any]:
        section_errors: list[str] = []
        running = _section(section_errors, "sessions", lambda: _read_running(conn))
        since = min((r["started_at"] for r in running), default=now) if isinstance(
            running, list) else now
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
                section_errors, "events", lambda: _read_feed(conn, feed_since, FEED_LIMIT)
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
    today: dict[str, int | None] = dict.fromkeys(_TODAY.values())
    if snap is not None:
        for err in snap["errors"]:
            if err not in errors:
                errors.append(err)
        phase_events = snap["phase_events"]
        pipeline_phases = (
            None if isinstance(phase_events, _Missing) else _open_phases(phase_events)
        )
        if not isinstance(snap["running"], _Missing):
            titles = _titles(waiting)
            running = [
                _card(
                    row, now=now, pipeline_phases=pipeline_phases, titles=titles,
                    stale_after_s=stale_after_s, home=home,
                )
                for row in snap["running"]
            ]
        if not isinstance(snap["needs"], _Missing):
            needs_you = [_question(r) for r in snap["needs"]]
        if not isinstance(snap["today"], _Missing):
            today = dict(snap["today"])
        if not isinstance(snap["feed"], _Missing):
            feed = snap["feed"]

    return {
        "version": version,
        "host": host,
        "generated": _iso(now),
        "generated_ts": now,
        "running": running,
        "needs_you": needs_you,
        "waiting": waiting,
        "today": today,
        "feed": feed,
        "errors": errors,
    }


def list_feed(*, db_path: Path, since_ts: int, limit: int = FEED_LIMIT) -> dict:
    """Events with ``ts >= since_ts``, newest first: ``{"events", "error"}``.
    ``events`` is ``null`` when they could not be read."""
    try:
        events = _read(Path(db_path), lambda conn: _read_feed(conn, since_ts, limit))
    except Exception as exc:
        return {
            "events": None,
            "error": f"events could not be read ({db_path}): {type(exc).__name__}: {exc}",
        }
    return {"events": events, "error": None}


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
