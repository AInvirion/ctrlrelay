"""SQLite state management for ctrlrelay orchestrator."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from ctrlrelay.core.agent_plugins import (
    AgentIdentity,
    AgentIdentityConflictError,
    agent_identity_from_mapping,
)
from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.state")

# The longest a starting daemon waits to switch the file to WAL before it
# gives up for this start (see StateDB._switch_to_wal).
WAL_SWITCH_TIMEOUT_MS = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    pipeline TEXT NOT NULL,
    repo TEXT NOT NULL,
    issue_number INTEGER,
    worktree_path TEXT,
    status TEXT NOT NULL,
    blocked_question TEXT,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    claude_exit_code INTEGER,
    summary TEXT,
    agent_session_id TEXT,
    -- Concrete plugin selected before the first dispatch (#379). Nullable
    -- for rows written before identity pinning; do not backfill by guessing
    -- from today's configuration.
    agent_type TEXT,
    agent_plugin_id TEXT,
    agent_plugin_version TEXT,
    agent_model TEXT,
    agent_provider TEXT,
    agent_fingerprint TEXT,
    -- Scrubbed, capped tail of the worker's final stdout/stderr (#204).
    -- The exit-code column above was declared from the start and left
    -- NULL on the failure path, so the row for the session that broke
    -- `zzsites#853` held `None` while the log line for the SAME session
    -- recorded `exit_code 1`. With no output kept anywhere either —
    -- `stderr_tail` was `""` and the log keeps a hash and a length, not
    -- the text — the only durable evidence of why 163 dispatches failed
    -- was that each took about two seconds. See
    -- `ctrlrelay.core.redact` for what is removed before it is stored.
    output_tail TEXT,
    -- The process that runs this session (#312). A session's status is
    -- only ever moved off 'running' by that process, so when it dies
    -- without moving it the row says 'running' for ever. The next poller
    -- start finds such rows by asking whether this process still exists.
    -- NULL on rows from versions before #312: those are left alone,
    -- because nothing then says which process owned them.
    owner_pid INTEGER,
    -- A random id of that process (``claims.PROCESS_RUN``). It tells the
    -- reaping process its own rows apart from an earlier process's that
    -- had the same pid. Another live ctrlrelay process that reused the pid
    -- still reads as the owner, and its row is left as it is.
    owner_run TEXT,
    -- The id of this machine's claim on the issue (#245), so a run that
    -- died can still have its claim released by id afterwards (#312).
    claim_id TEXT,
    -- Why the relay stopped this run as stuck (#224): going in circles in
    -- its transcript, or the same CI failures after fix round after fix
    -- round. NULL for every run that was not. Its status is 'failed'.
    -- A run stopped over its token budget (#223) is recorded here too,
    -- with a reason starting 'budget: '.
    stuck_reason TEXT,
    -- Tokens the session has used (#223): input + cache writes + output,
    -- over every spawn of the session. NULL when nothing was measured.
    -- A floor, not the total, when usage_detail counts unreadable rows.
    usage_tokens INTEGER,
    -- The breakdown, the reported cost and the limits applied, as JSON
    -- (see ctrlrelay.core.budget.record_usage). NULL with usage_tokens.
    usage_detail TEXT
);

CREATE TABLE IF NOT EXISTS repo_locks (
    repo TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    acquired_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS github_cursor (
    repo TEXT PRIMARY KEY,
    last_checked_at INTEGER NOT NULL,
    last_seen_issue_update TEXT
);

CREATE TABLE IF NOT EXISTS telegram_pending (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    question TEXT NOT NULL,
    asked_at INTEGER NOT NULL,
    answered_at INTEGER,
    answer TEXT
);

CREATE TABLE IF NOT EXISTS automation_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    operation TEXT NOT NULL,
    policy TEXT NOT NULL,
    item_id TEXT,
    decision TEXT,
    decided_by TEXT,
    decided_at INTEGER,
    context TEXT
);

-- Sessions that exited BLOCKED_NEEDS_INPUT and can be resumed by an
-- operator reply arriving AFTER the session has already torn down.
-- Without this, a Telegram reply to a closed session disappears silently
-- because the bridge's in-memory _pending_questions entry dies with the
-- session socket. A scheduled sweeper in the poller picks up rows where
-- answered_at IS NOT NULL AND resumed_at IS NULL and drives the resume.
CREATE TABLE IF NOT EXISTS pending_resumes (
    session_id TEXT PRIMARY KEY,
    pipeline TEXT NOT NULL,
    repo TEXT NOT NULL,
    question TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    answer TEXT,
    answered_at INTEGER,
    resumed_at INTEGER,
    expired_at INTEGER,
    expired_reason TEXT,
    -- Set when the question was held back because the same question
    -- went unanswered before (#214): the earliest time it is due again.
    held_until INTEGER,
    -- Whether this row's question reached the operator: 1 posted (the
    -- ask timed out waiting for an answer), 0 not posted (held back, no
    -- bridge, or the ask failed before delivery). NULL when the writer
    -- does not say, which is read as posted, as before #214.
    posted INTEGER,
    -- The answer time this row carried before a re-block rewrote it.
    -- The rewrite clears answered_at so the row can take a new answer;
    -- this keeps the fact that the question WAS answered.
    last_answered_at INTEGER,
    -- The question asked on the GitHub issue as well as in chat (#328):
    -- the issue, the relay's question comment and when it was posted.
    issue_number INTEGER,
    issue_comment_id INTEGER,
    issue_asked_at INTEGER,
    -- Where the answer came from: 'github', or NULL for chat (every
    -- writer before #328). For a GitHub answer, who wrote it and the
    -- comment's id.
    answer_source TEXT,
    answer_by TEXT,
    answer_ref INTEGER,
    -- When the other channel was told about the answer: the chat answer
    -- copied to the issue, or the chat told of the GitHub answer.
    answer_noted_at INTEGER
);

-- In-flight PR merge watchers that must survive a poller restart. Without
-- this, a launchd kickstart / crash / redeploy / reboot cancels the
-- in-memory asyncio watcher tasks and any PR that gets merged afterwards
-- silently loses its post-merge automation (issue close + Telegram ping)
-- for the remainder of the 7-day watch window. Poller startup rehydrates
-- one `pr_watch_task` per surviving row before `run_poll_loop` enters its
-- main loop. Rows are inserted on `dev.pr.watching` and deleted on the
-- terminal `dev.pr.merged` / `dev.pr.watch_timeout` events only — a
-- `dev.pr.watch_cancelled` (shutdown-driven cancellation, not operator
-- intent) deliberately leaves the row in place so the next startup
-- resumes the watch. `pr_number` is the unique row key because we watch
-- at most one PR per (repo, pr_number).
CREATE TABLE IF NOT EXISTS pr_watches (
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    session_id TEXT,
    issue_number INTEGER NOT NULL,
    pr_url TEXT,
    started_at INTEGER NOT NULL,
    cleanup_phase TEXT,
    PRIMARY KEY (repo, pr_number)
);

-- Whether the relay can still run a worker at all, as one row (#204).
-- Separate from `sessions` because the question is about the relay and
-- not about any one run: 163 consecutive non-zero exits spread across
-- 88 repos and two pipelines is one outage, and counting it per-session
-- is what made it invisible.
--
-- In the database rather than in the poller's memory because the poller
-- restarts. An in-memory run length resets on every restart, so a relay
-- that is broken AND being restarted is precisely the one that would
-- never reach the threshold — and the `alerted_at` latch would re-fire
-- on every boot.
CREATE TABLE IF NOT EXISTS dispatch_health (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    -- Set when an alert has been sent and not yet cleared by a success.
    -- NULL means "an alert may be sent", which is what makes the latch
    -- re-armable: a dedup that never clears is a guard that fires once
    -- per database lifetime (#200 is the opposite failure — the same
    -- message re-sent every morning).
    alerted_at INTEGER,
    -- Identifies WHICH claim the latch holds, so a failed delivery
    -- releases its own and not a newer one. Separate from `alerted_at`
    -- because that is a timestamp in seconds like every other `*_at`
    -- column here, and two processes claiming inside one second are
    -- indistinguishable by it -- which would let A's failed delivery
    -- clear B's claim and announce one outage twice.
    alert_claim_token TEXT,
    -- When an alert was actually DELIVERED. This, not the claim, is
    -- what suppresses a repeat. Claiming the latch and delivering on
    -- it are two facts, and conflating them meant a poller stopped
    -- mid-send (a `systemctl restart`, which is the first thing an
    -- operator does) came back with the latch held and nothing sent --
    -- so the rest of the outage was silent. Measured: cancel during
    -- the send, then 59 further failures against a healthy channel,
    -- zero messages.
    alert_delivered_at INTEGER,
    last_failure_at INTEGER,
    last_success_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repo);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_automation_repo ON automation_decisions(repo);
CREATE INDEX IF NOT EXISTS idx_pending_resumes_unanswered
    ON pending_resumes(answered_at) WHERE answered_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_pending_resumes_answered_unresumed
    ON pending_resumes(answered_at, resumed_at)
    WHERE answered_at IS NOT NULL AND resumed_at IS NULL;
-- The repeat lookup reads one repo's history on every posted question.
CREATE INDEX IF NOT EXISTS idx_pending_resumes_repo_created
    ON pending_resumes(repo, created_at);

-- Every branch head ctrlrelay itself pushed for an agent (#219). The
-- push lease is taken from here, so a remote branch ctrlrelay did not
-- put there is refused rather than force-pushed over.
CREATE TABLE IF NOT EXISTS relay_pushes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    session_id TEXT NOT NULL,
    sha TEXT NOT NULL,
    pushed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relay_pushes_branch
    ON relay_pushes(repo, branch, id);

-- What a dev run had finished, step by step (#231), so a run whose process
-- died can continue from the last finished step instead of starting over.
-- ``data`` is the JSON the later steps need (the plan, the agent's result,
-- the pull request); ``head_sha`` is the run branch's commit when the step
-- ended, checked before anything is restored.
CREATE TABLE IF NOT EXISTS run_steps (
    session_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    step TEXT NOT NULL,
    head_sha TEXT,
    data TEXT NOT NULL,
    finished_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, step)
);
CREATE INDEX IF NOT EXISTS idx_run_steps_issue
    ON run_steps(repo, issue_number);

-- When each scheduled chat message was last delivered (#230), so a
-- restart does not send this week's summary a second time.
CREATE TABLE IF NOT EXISTS scheduled_sends (
    name TEXT PRIMARY KEY,
    sent_at INTEGER NOT NULL
);

-- Feed for the status page. Written best-effort by the daemon through
-- ctrlrelay.core.events.emit_event; a failed write never affects the work.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    repo TEXT,
    issue_number INTEGER,
    session_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts);

"""

# Issues the relay finished, and whether each was reopened afterwards
# (#230). Kept apart from ``events`` because events are deleted after
# ``web.history_days``, and an issue can be reopened long after that.
# One row per merged PR; ``finished_at`` is when the relay saw the merge
# (it closes the issue right after), ``reopened_at`` the time of the
# first GitHub ``reopened`` event after it, NULL until one is seen.
# Created by StateDB._ensure_issue_finishes.
ISSUE_FINISHES_SCHEMA = """
CREATE TABLE issue_finishes (
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    pr_number INTEGER NOT NULL,
    finished_at INTEGER NOT NULL,
    session_id TEXT,
    reopened_at INTEGER,
    PRIMARY KEY (repo, issue_number, pr_number)
)
"""


class StateDB:
    """SQLite database for orchestrator state.

    Thread-safety: Each thread/async context should create its own StateDB instance.
    The underlying SQLite connection is not shared.
    """

    def __init__(self, db_path: Path | str) -> None:
        """Initialize the database, creating tables if needed.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Opened on first use by record_event_nowait; see there.
        self._event_conn: sqlite3.Connection | None = None

        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._switch_to_wal()
        # busy_timeout makes a briefly contended write wait instead of
        # failing at once.
        self._conn.execute("PRAGMA busy_timeout=5000")
        # An in-memory database reports an empty file name. A second
        # connection to it would be a different, empty database, so event
        # writes must stay on the main connection there.
        self._in_memory = (
            self._conn.execute("PRAGMA database_list").fetchone()[2] == ""
        )
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _switch_to_wal(self) -> None:
        """Put the file in WAL mode if it is not already, never failing.

        WAL lets the read-only status page read while the poller and
        bridge write. The mode is stored in the file, so after the first
        successful switch this is a no-op. The switch itself needs an
        exclusive lock: on a database still in rollback-journal mode, the
        other daemon process holding a read makes it fail with "database
        is locked". So it waits at most ``WAL_SWITCH_TIMEOUT_MS`` and, if
        still locked, logs ``state.wal_switch_deferred`` and carries on in
        the current mode; the next start tries again. Every reader and
        writer here works in either mode.
        """
        self._conn.execute(f"PRAGMA busy_timeout={WAL_SWITCH_TIMEOUT_MS}")
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
            _logger.warning(
                "state.wal_switch_deferred",
                extra={
                    "db_path": str(self.db_path),
                    "journal_mode": mode,
                    "error": str(exc),
                    "detail": (
                        "another process held the database while this one "
                        "started; running in the current journal mode, the "
                        "next start tries WAL again"
                    ),
                },
            )

    def _migrate(self) -> None:
        """Apply in-place schema additions to databases written by older versions.

        SQLite's ``CREATE TABLE IF NOT EXISTS`` preserves the *existing* shape
        if the table was created by a prior version, so additive column bumps
        need a guarded ALTER. We check ``PRAGMA table_info`` and only ALTER
        when the column is missing.
        """
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "agent_session_id" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN agent_session_id TEXT"
            )
        for column in (
            "agent_type", "agent_plugin_id", "agent_plugin_version",
            "agent_model", "agent_provider", "agent_fingerprint",
        ):
            if column not in existing:
                try:
                    self._conn.execute(
                        f"ALTER TABLE sessions ADD COLUMN {column} TEXT"
                    )
                except sqlite3.OperationalError as e:
                    # Another process may complete the same additive migration
                    # between our PRAGMA read and ALTER. Only that race is safe
                    # to ignore; every other schema error still fails startup.
                    if "duplicate column name" not in str(e).lower():
                        raise
        if "output_tail" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN output_tail TEXT"
            )
        if "owner_pid" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN owner_pid INTEGER"
            )
        if "owner_run" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN owner_run TEXT"
            )
        if "claim_id" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN claim_id TEXT"
            )
        if "stuck_reason" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN stuck_reason TEXT"
            )
        if "usage_tokens" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN usage_tokens INTEGER"
            )
        if "usage_detail" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN usage_detail TEXT"
            )
        # #231: set when the poller found the run's process dead, and
        # the session that continued it, so it is continued once.
        if "interrupted" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN interrupted INTEGER"
            )
        if "resumed_by" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN resumed_by TEXT"
            )

        pr_watches_cols = {
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info(pr_watches)"
            ).fetchall()
        }
        if pr_watches_cols and "cleanup_phase" not in pr_watches_cols:
            self._conn.execute(
                "ALTER TABLE pr_watches ADD COLUMN cleanup_phase TEXT"
            )

        pending_cols = {
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info(pending_resumes)"
            ).fetchall()
        }
        if pending_cols and "expired_at" not in pending_cols:
            self._conn.execute(
                "ALTER TABLE pending_resumes ADD COLUMN expired_at INTEGER"
            )
        if pending_cols and "expired_reason" not in pending_cols:
            self._conn.execute(
                "ALTER TABLE pending_resumes ADD COLUMN expired_reason TEXT"
            )
        if pending_cols and "held_until" not in pending_cols:
            self._conn.execute(
                "ALTER TABLE pending_resumes ADD COLUMN held_until INTEGER"
            )
        if pending_cols and "posted" not in pending_cols:
            self._conn.execute(
                "ALTER TABLE pending_resumes ADD COLUMN posted INTEGER"
            )
        if pending_cols and "last_answered_at" not in pending_cols:
            self._conn.execute(
                "ALTER TABLE pending_resumes "
                "ADD COLUMN last_answered_at INTEGER"
            )
        for col, kind in (
            ("issue_number", "INTEGER"),
            ("issue_comment_id", "INTEGER"),
            ("issue_asked_at", "INTEGER"),
            ("answer_source", "TEXT"),
            ("answer_by", "TEXT"),
            ("answer_ref", "INTEGER"),
            ("answer_noted_at", "INTEGER"),
        ):
            if pending_cols and col not in pending_cols:
                self._conn.execute(
                    f"ALTER TABLE pending_resumes ADD COLUMN {col} {kind}"
                )
        if pending_cols:
            # #412: an open dev question saved before its issue number was
            # stored with it. The dev session row names the issue, so
            # closing that issue can retire the question. Only open rows,
            # only where the number is missing, so this is a no-op once
            # every row has it.
            self._conn.execute(
                """UPDATE pending_resumes SET issue_number = (
                       SELECT s.issue_number FROM sessions s
                       WHERE s.id = pending_resumes.session_id)
                   WHERE pipeline = 'dev' AND issue_number IS NULL
                     AND answered_at IS NULL AND expired_at IS NULL
                     AND EXISTS (
                       SELECT 1 FROM sessions s
                       WHERE s.id = pending_resumes.session_id
                         AND s.issue_number IS NOT NULL)"""
            )

        health_cols = {
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info(dispatch_health)"
            ).fetchall()
        }
        if health_cols and "alert_claim_token" not in health_cols:
            self._conn.execute(
                "ALTER TABLE dispatch_health ADD COLUMN alert_claim_token TEXT"
            )
        if health_cols and "alert_delivered_at" not in health_cols:
            self._conn.execute(
                "ALTER TABLE dispatch_health "
                "ADD COLUMN alert_delivered_at INTEGER"
            )

        self._ensure_issue_finishes()
        self._ensure_dispatch_health_row()

    def _ensure_issue_finishes(self) -> None:
        """Create ``issue_finishes`` (#230), filling it from the ``merged``
        events still kept when the table is new.

        Created here rather than in ``SCHEMA`` so the backfill runs exactly
        once, on the start that creates the table. Without it, every issue
        the relay finished before this version could be reopened without
        anybody counting it. Events older than ``web.history_days`` are
        already gone, so those finishes stay unknown.
        """
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='issue_finishes'"
        ).fetchone()
        if exists:
            return
        self._conn.execute(ISSUE_FINISHES_SCHEMA)
        self._conn.execute(
            """INSERT OR IGNORE INTO issue_finishes
               (repo, issue_number, pr_number, finished_at, session_id)
               SELECT repo, issue_number,
                      CAST(json_extract(detail, '$.pr_number') AS INTEGER),
                      MIN(ts), MIN(session_id)
               FROM events
               WHERE kind = 'merged' AND repo IS NOT NULL
                 AND issue_number IS NOT NULL
                 AND json_valid(detail)
                 AND json_extract(detail, '$.pr_number') IS NOT NULL
               GROUP BY repo, issue_number,
                        json_extract(detail, '$.pr_number')"""
        )

    # Finished issues and reopens (#230)

    def record_issue_finish(
        self,
        *,
        repo: str,
        issue_number: int,
        pr_number: int,
        session_id: str | None,
        finished_at: int | None = None,
    ) -> None:
        """Note that the relay saw ``pr_number`` merge for this issue, after
        which its post-merge pipeline closes the issue. A PR merges once, so
        a watcher that finds the same merge again after a restart changes
        nothing. Raises on failure."""
        self._conn.execute(
            """INSERT OR IGNORE INTO issue_finishes
               (repo, issue_number, pr_number, finished_at, session_id)
               VALUES (?, ?, ?, ?, ?)""",
            (
                repo, int(issue_number), int(pr_number),
                int(time.time()) if finished_at is None else int(finished_at),
                session_id,
            ),
        )
        self._conn.commit()

    def list_open_finishes(self) -> dict[tuple[str, int], int]:
        """``{(repo, issue_number): finished_at}`` for every issue whose
        latest finish has no reopen recorded after it: the issues a reopen
        could still be noticed on."""
        rows = self._conn.execute(
            """SELECT f.repo, f.issue_number, f.finished_at
               FROM issue_finishes f
               WHERE f.reopened_at IS NULL
                 AND f.finished_at = (
                   SELECT MAX(g.finished_at) FROM issue_finishes g
                   WHERE g.repo = f.repo AND g.issue_number = f.issue_number)"""
        ).fetchall()
        return {(r["repo"], int(r["issue_number"])): int(r["finished_at"]) for r in rows}

    def record_reopen(
        self, repo: str, issue_number: int, finished_at: int, reopened_at: int,
    ) -> bool:
        """Mark the finish at ``finished_at`` as reopened at ``reopened_at``.
        True only for the call that recorded it, so one reopen is counted
        once however many polls see it."""
        cursor = self._conn.execute(
            """UPDATE issue_finishes SET reopened_at = ?
               WHERE repo = ? AND issue_number = ? AND finished_at = ?
                 AND reopened_at IS NULL""",
            (int(reopened_at), repo, int(issue_number), int(finished_at)),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # Scheduled messages (#230)

    def last_sent(self, name: str) -> int | None:
        """When the scheduled message ``name`` was last delivered, or None."""
        row = self._conn.execute(
            "SELECT sent_at FROM scheduled_sends WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else int(row["sent_at"])

    def record_sent(self, name: str, sent_at: int) -> None:
        self._conn.execute(
            """INSERT INTO scheduled_sends (name, sent_at) VALUES (?, ?)
               ON CONFLICT(name) DO UPDATE SET sent_at = excluded.sent_at""",
            (name, int(sent_at)),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection (and the event side connection)."""
        if self._event_conn is not None:
            self._event_conn.close()
            self._event_conn = None
        self._conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Execute a SQL statement.

        Args:
            sql: SQL statement to execute.
            params: Parameters for the statement.

        Returns:
            Cursor with results.
        """
        return self._conn.execute(sql, params)

    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()

    # Repo locks

    def acquire_lock(self, repo: str, session_id: str) -> bool:
        """Attempt to acquire a lock on a repository.

        Args:
            repo: Repository name (e.g., "owner/repo").
            session_id: Session ID acquiring the lock.

        Returns:
            True if lock was acquired, False if already held.
        """
        try:
            self._conn.execute(
                "INSERT INTO repo_locks (repo, session_id, acquired_at) VALUES (?, ?, ?)",
                (repo, session_id, int(time.time())),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def release_lock(self, repo: str, session_id: str) -> bool:
        """Release a lock on a repository.

        Only releases if the lock is held by the specified session.

        Args:
            repo: Repository name to unlock.
            session_id: Session ID that should own the lock.

        Returns:
            True if lock was released, False if not held by this session.
        """
        cursor = self._conn.execute(
            "DELETE FROM repo_locks WHERE repo = ? AND session_id = ?",
            (repo, session_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_lock_holder(self, repo: str) -> str | None:
        """Get the session ID holding a lock.

        Args:
            repo: Repository name.

        Returns:
            Session ID if locked, None otherwise.
        """
        row = self._conn.execute(
            "SELECT session_id FROM repo_locks WHERE repo = ?", (repo,)
        ).fetchone()
        return row["session_id"] if row else None

    def list_locks(self) -> list[dict[str, Any]]:
        """List all current locks.

        Returns:
            List of lock records.
        """
        rows = self._conn.execute("SELECT * FROM repo_locks").fetchall()
        return [dict(row) for row in rows]

    # Relay pushes (#219)

    def record_relay_push(
        self, *, repo: str, branch: str, session_id: str, sha: str
    ) -> None:
        """Remember a branch head ctrlrelay pushed itself."""
        self._conn.execute(
            "INSERT INTO relay_pushes (repo, branch, session_id, sha, pushed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (repo, branch, session_id, sha, int(time.time())),
        )
        self._conn.commit()

    def last_relay_push(self, *, repo: str, branch: str, session_id: str) -> str | None:
        """The last head this session pushed to ``branch``, or None."""
        row = self._conn.execute(
            "SELECT sha FROM relay_pushes WHERE repo = ? AND branch = ? "
            "AND session_id = ? ORDER BY id DESC LIMIT 1",
            (repo, branch, session_id),
        ).fetchone()
        return row["sha"] if row else None

    def relay_pushed_shas(self, *, repo: str, branch: str) -> frozenset[str]:
        """Every head ctrlrelay ever pushed to ``branch``, any session."""
        rows = self._conn.execute(
            "SELECT DISTINCT sha FROM relay_pushes WHERE repo = ? AND branch = ?",
            (repo, branch),
        ).fetchall()
        return frozenset(r["sha"] for r in rows)

    # Agent session ids

    def get_agent_identity(self, session_id: str) -> AgentIdentity | None:
        """The plugin pinned to this run, or None for a pre-#379 row."""
        row = self._conn.execute(
            "SELECT agent_type, agent_plugin_id, agent_plugin_version, "
            "agent_model, agent_provider, agent_fingerprint FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return agent_identity_from_mapping(dict(row) if row else None)

    def pin_agent_identity(self, session_id: str, identity: AgentIdentity) -> None:
        """First-write-wins identity persistence for one logical run.

        A conflict is never swallowed: writing the other backend's native
        session id afterwards would make an accidental switch much harder to
        diagnose. Legacy rows are pinned only after the caller explicitly
        resolves their compatibility policy.
        """
        existing = self.get_agent_identity(session_id)
        if existing is not None:
            if existing != identity:
                raise AgentIdentityConflictError(
                    f"session {session_id} is pinned to {existing!r}, "
                    f"not {identity!r}"
                )
            return
        cursor = self._conn.execute(
            "UPDATE sessions SET agent_type = ?, agent_plugin_id = ?, "
            "agent_plugin_version = ?, agent_model = ?, agent_provider = ?, "
            "agent_fingerprint = ? WHERE id = ? AND agent_type IS NULL",
            (
                identity.agent_type, identity.plugin_id, identity.plugin_version,
                identity.model, identity.provider, identity.fingerprint, session_id,
            ),
        )
        self._conn.commit()
        if cursor.rowcount != 1:
            current = self.get_agent_identity(session_id)
            if current != identity:
                raise AgentIdentityConflictError(
                    f"could not pin agent identity for session {session_id}"
                )

    def set_agent_session_id(
        self,
        session_id: str,
        agent_session_id: str,
        *,
        agent: AgentIdentity | None = None,
    ) -> None:
        """Persist the CLI's native session id after checking its identity."""
        if agent is not None:
            self.pin_agent_identity(session_id, agent)
        self._conn.execute(
            "UPDATE sessions SET agent_session_id = ? WHERE id = ?",
            (agent_session_id, session_id),
        )
        self._conn.commit()

    def get_agent_session_id(self, session_id: str) -> str | None:
        """Fetch Claude's session UUID for a given composite session id.

        Returns ``None`` if the row is missing or the column was never set
        (sessions that predate the agent-uuid capture).
        """
        row = self._conn.execute(
            "SELECT agent_session_id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        value = row["agent_session_id"]
        return value if value else None

    def get_session_row(self, session_id: str) -> dict[str, Any] | None:
        """Full sessions-row snapshot, used when the pending-resume
        sweeper needs pipeline-specific context (issue_number for dev,
        worktree_path for sanity checks, etc.) that isn't mirrored into
        pending_resumes."""
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_running_sessions(self) -> list[dict[str, Any]]:
        """Every ``sessions`` row whose status is still ``running``."""
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE status = 'running'"
        ).fetchall()
        return [dict(r) for r in rows]

    def fail_orphaned_session(self, session_id: str, summary: str) -> bool:
        """Mark a ``running`` session whose process is gone as failed, and
        drop any repo lock it still holds (#312).

        One transaction, so a reader never sees the row failed while its
        lock still wedges the repo. Guarded on ``status = 'running'``: if
        anything moved the row in the meantime, nothing is touched and
        ``False`` comes back. The lock is deleted only for this session
        id, never by repo, so a live run's lock is never taken.
        """
        ours = not self._conn.in_transaction
        try:
            cursor = self._conn.execute(
                "UPDATE sessions SET status = 'failed', summary = ?, "
                "ended_at = ?, interrupted = 1 "
                "WHERE id = ? AND status = 'running'",
                (summary, int(time.time()), session_id),
            )
            if cursor.rowcount > 0:
                self._conn.execute(
                    "DELETE FROM repo_locks WHERE session_id = ?",
                    (session_id,),
                )
            if ours:
                self._conn.commit()
        except BaseException:
            if ours and self._conn.in_transaction:
                self._conn.rollback()
            raise
        return cursor.rowcount > 0

    # Run steps (#231)

    def record_run_step(
        self,
        *,
        session_id: str,
        repo: str,
        issue_number: int,
        seq: int,
        step: str,
        head_sha: str | None,
        data: str,
    ) -> None:
        """Save that ``step`` of a dev run finished, with what later steps
        need (``data``, JSON). A step recorded twice keeps the later one."""
        self._conn.execute(
            "INSERT OR REPLACE INTO run_steps (session_id, repo, "
            "issue_number, seq, step, head_sha, data, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, repo, issue_number, seq, step, head_sha, data,
             int(time.time())),
        )
        self._conn.commit()

    def run_steps_for(self, session_id: str) -> list[dict[str, Any]]:
        """The steps a run finished, in the order they ran."""
        rows = self._conn.execute(
            "SELECT * FROM run_steps WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_dev_session(
        self, repo: str, issue_number: int, *, with_steps: bool = False,
    ) -> dict[str, Any] | None:
        """The newest dev session for an issue, or None.

        ``with_steps`` limits it to sessions that saved at least one step.
        Newest by start time, then by rowid, so two runs started in the
        same second still have an order.
        """
        sql = (
            "SELECT s.* FROM sessions s WHERE s.pipeline = 'dev' "
            "AND s.repo = ? AND s.issue_number = ?"
        )
        if with_steps:
            sql += (
                " AND EXISTS (SELECT 1 FROM run_steps r "
                "WHERE r.session_id = s.id)"
            )
        sql += " ORDER BY s.started_at DESC, s.rowid DESC LIMIT 1"
        row = self._conn.execute(sql, (repo, issue_number)).fetchone()
        return dict(row) if row else None

    def mark_resumed(self, session_id: str, resumed_by: str) -> bool:
        """Record that ``resumed_by`` continued ``session_id``. Guarded, so
        two runs cannot both continue it: False when one already did."""
        cursor = self._conn.execute(
            "UPDATE sessions SET resumed_by = ? WHERE id = ? "
            "AND resumed_by IS NULL",
            (resumed_by, session_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def record_session_outcome(
        self,
        session_id: str,
        *,
        exit_code: int | None,
        output_tail: str | None,
    ) -> bool:
        """Persist a worker's exit code and the tail of its output.

        Returns ``True`` when a row was updated. The boolean is not
        decoration: the UPDATE is keyed on ``session_id``, and an
        UPDATE that matches nothing is SQLite's idea of success. A
        dispatch whose session row was never inserted — the failure
        landed before the INSERT — would otherwise have its exit code
        recorded nowhere while this call reported fine, which is the
        same silence #204 is about, one layer in. The caller logs the
        miss.

        ``output_tail`` is expected to have been scrubbed and capped
        already by :func:`ctrlrelay.core.redact.worker_output_tail`,
        and is scrubbed AGAIN here. Redaction is idempotent, so the
        second pass costs nothing and it means the invariant "no
        credential shape reaches the database" is enforced at the one
        place every writer must go through, rather than depending on
        each caller remembering. An earlier version of this docstring
        argued the opposite — that a sanitiser in the writer can be
        bypassed — which is backwards: the writer is the chokepoint,
        and it is the CALLERS that can be added without it.

        An empty ``output_tail`` is stored as ``NULL`` rather than
        ``""`` — "the agent wrote nothing before it died" is a real
        and differently-shaped failure, and the two should not read
        the same on inspection.
        """
        from ctrlrelay.core.redact import redact_secrets

        # `None` when and only when there was no output: an empty or
        # absent `output_tail` skips the call, and `redact_secrets`
        # never maps a non-empty string to an empty one. A trailing
        # `or None` here was dead code — a mutation replacing it
        # changed no outcome, which reads as coverage to the next
        # person.
        scrubbed = redact_secrets(output_tail) if output_tail else None
        ours = False
        try:
            # Join a caller's transaction rather than committing it —
            # the same discipline `_begin_immediate` was corrected to,
            # and this writer reintroduced the defect by committing
            # unconditionally. The outcome hook runs mid-pipeline on
            # the pipeline's own connection, so an unconditional commit
            # here publishes whatever partial state that pipeline had
            # open.
            ours = not self._conn.in_transaction
            cursor = self._conn.execute(
                "UPDATE sessions SET claude_exit_code = ?, output_tail = ? "
                "WHERE id = ?",
                (exit_code, scrubbed, session_id),
            )
            if ours:
                self._conn.commit()
        except BaseException:
            # A `database is locked` out of the UPDATE or the commit
            # leaves the transaction open, and the caller
            # (`persist_dispatch_outcome`) swallows the exception so
            # nothing else would close it — leaving this connection
            # holding a write lock on a database the bridge also uses.
            # The same fault the read path had, arriving on an error
            # path instead of a happy one. Only our own transaction:
            # rolling back a caller's would discard work this method
            # merely joined.
            if ours and self._conn.in_transaction:
                self._conn.rollback()
            raise
        return cursor.rowcount > 0

    # Dispatch health (#204 — is the relay able to run a worker at all)

    def _ensure_dispatch_health_row(self) -> None:
        """Create the single ``dispatch_health`` row if it is absent.

        **Commits, but only a transaction of its own.** Two faults have
        lived here and they pull in opposite directions:

        - An early version left this INSERT uncommitted and was called
          from the READ accessors too, so a method that only reads
          opened a write transaction. SQLite then held a RESERVED lock
          for the life of that connection's transaction, and the next
          writer on ANY other connection to the same file failed with
          ``database is locked`` — measured: a bare
          ``dispatch_failure_count()`` on one connection made an
          ordinary ``INSERT INTO sessions`` on a second one fail. So it
          has to commit, and the reads must not call it.
        - A later version committed unconditionally, which publishes a
          caller's open transaction. The dispatch-outcome hook runs
          mid-pipeline on the pipeline's own connection, so that is a
          real caller and not a hypothetical one.

        The flag is therefore captured BEFORE the INSERT. The INSERT
        itself opens a transaction, so asking afterwards always answers
        "one is open" and the commit would never run — a guard that
        cannot fire, in the fix for a guard that fired too often.

        When the row is already there nothing is written at all. An
        ``INSERT OR IGNORE`` that ignores still takes a write lock and its
        commit needs an exclusive one, so on a rollback-journal database
        (one whose WAL switch was deferred) a daemon starting while the
        other holds a read would fail here instead.
        """
        if self._conn.execute(
            "SELECT 1 FROM dispatch_health WHERE id = 1"
        ).fetchone() is not None:
            return
        joined = self._conn.in_transaction
        self._conn.execute(
            "INSERT OR IGNORE INTO dispatch_health "
            "(id, consecutive_failures) VALUES (1, 0)"
        )
        if not joined:
            self._conn.commit()

    def _begin_immediate(self) -> bool:
        """Start a write transaction, taking the lock up front.

        Returns whether WE started it, so the caller knows whether the
        commit is its to make.

        ``isolation_level`` is sqlite3's default here, so an explicit
        ``BEGIN IMMEDIATE`` is what takes the write lock at the start
        rather than upgrading mid-transaction, which is how two writers
        deadlock.

        **If a transaction is already open, this joins it rather than
        committing it.** An earlier version committed, on the stated
        premise that "every writer in this class commits immediately
        after its execute" — which is false: ``StateDB.execute`` is
        public, callers commit separately, and the dispatch-outcome
        hook runs on the pipeline's own connection mid-pipeline. So
        that commit could publish a caller's half-finished write. It
        is logged either way, because a caller holding an open
        transaction when this is reached is a fact worth seeing.

        Joining costs the atomicity guarantee to the caller's
        transaction instead of ours, and if the caller rolls back we
        lose a counter increment — which delays an alert by one
        failure and is not silent. Publishing somebody's partial write
        is not recoverable, so this is the safe direction.
        """
        if self._conn.in_transaction:
            log_event(
                _logger,
                "state.transaction_left_open",
                detail=(
                    "a caller holds an open transaction on this "
                    "connection; joining it rather than committing it, so "
                    "this write is published by whoever commits theirs"
                ),
            )
            return False
        self._conn.execute("BEGIN IMMEDIATE")
        return True

    def dispatch_failure_count(self) -> int:
        """Length of the current run of failed dispatches (0 when healthy).

        A pure read. A missing row reads as 0 rather than being created
        here — see ``_ensure_dispatch_health_row`` on why a read must
        not write. ``_migrate`` creates the row, and the write paths
        re-create it if somebody deletes it by hand.
        """
        row = self._conn.execute(
            "SELECT consecutive_failures FROM dispatch_health WHERE id = 1"
        ).fetchone()
        return int(row["consecutive_failures"]) if row else 0

    def record_dispatch_failure(
        self, threshold: int, *, lease_seconds: int
    ) -> tuple[int, str | None]:
        """Extend the failure run and decide, in ONE transaction.

        Returns ``(consecutive_failures, claim_token)``. The token is
        ``None`` when no alert may be attempted, and otherwise is a
        LEASE on the right to attempt one — see
        :meth:`_may_claim_alert`.

        The increment, the threshold comparison and the claim are a
        single transaction because two pollers can share one
        ``state.db``. Done as three statements, a success recorded by
        the other process can land between this one's read and its
        claim, and a relay that has just recovered is announced as
        still failing.
        """
        if threshold < 1:
            raise ValueError(f"threshold must be at least 1, got {threshold!r}")
        ours = False
        try:
            ours = self._begin_immediate()
            self._conn.execute(
                "INSERT OR IGNORE INTO dispatch_health "
                "(id, consecutive_failures) VALUES (1, 0)"
            )
            self._conn.execute(
                "UPDATE dispatch_health SET "
                "consecutive_failures = consecutive_failures + 1, "
                "last_failure_at = ? WHERE id = 1",
                (int(time.time()),),
            )
            row = self._conn.execute(
                "SELECT consecutive_failures, alerted_at, "
                "alert_claim_token, alert_delivered_at "
                "FROM dispatch_health WHERE id = 1"
            ).fetchone()
            consecutive = int(row["consecutive_failures"])
            claimed: str | None = None
            if consecutive >= threshold and self._may_claim_alert(
                row, lease_seconds
            ):
                claimed = uuid.uuid4().hex
                self._conn.execute(
                    "UPDATE dispatch_health SET alerted_at = ?, "
                    "alert_claim_token = ? WHERE id = 1",
                    (int(time.time()), claimed),
                )
            if ours:
                self._conn.commit()
        except BaseException:
            if ours and self._conn.in_transaction:
                self._conn.rollback()
            raise
        return consecutive, claimed

    @staticmethod
    def _may_claim_alert(row: Any, lease_seconds: int) -> bool:
        """Whether the right to attempt one alert is available.

        Two conditions, and separating them is the whole point:

        - **Nothing has been DELIVERED for this run.** Delivery is what
          suppresses a repeat, not the claim. Latching on the claim
          meant a poller stopped mid-send — a ``systemctl restart``,
          which is the first thing an operator does — came back with
          the latch held and nothing sent, so the rest of the outage
          was silent. Measured: cancel during the send, then 59 more
          failures against a healthy channel, zero messages.
        - **No live claim.** A claim is a LEASE, so one abandoned by a
          process that was killed mid-send expires and the next failure
          retries. That is also what stops a retry storm: a delivery
          that fails leaves its claim to expire rather than releasing
          it, so during an outage at most one attempt is made per
          lease window instead of one per failed dispatch.

        One mechanism for both, which replaced an explicit release
        that handled neither the kill nor the storm.
        """
        if row["alert_delivered_at"] is not None:
            return False
        if row["alert_claim_token"] is None:
            return True
        held_since = row["alerted_at"]
        if held_since is None:
            # A token with no timestamp cannot be aged, so treat it as
            # expired rather than as permanent. "I cannot tell how old
            # this is" must not read as "it is current" — that is the
            # direction that goes silent.
            return True
        return int(time.time()) - int(held_since) >= lease_seconds

    def reset_dispatch_failures(self) -> int:
        """Clear the run, the claim and the delivery record.

        Returns the previous run length, which is what lets the caller
        log a recovery only when there was something to recover from —
        otherwise every successful dispatch on a healthy relay would
        log one, and a line that appears constantly is a line nobody
        reads.

        One transaction, for the same reason as
        :meth:`record_dispatch_failure`.
        """
        ours = False
        try:
            ours = self._begin_immediate()
            self._conn.execute(
                "INSERT OR IGNORE INTO dispatch_health "
                "(id, consecutive_failures) VALUES (1, 0)"
            )
            row = self._conn.execute(
                "SELECT consecutive_failures FROM dispatch_health WHERE id = 1"
            ).fetchone()
            previous = int(row["consecutive_failures"]) if row else 0
            self._conn.execute(
                "UPDATE dispatch_health SET consecutive_failures = 0, "
                "alerted_at = NULL, alert_claim_token = NULL, "
                "alert_delivered_at = NULL, last_success_at = ? "
                "WHERE id = 1",
                (int(time.time()),),
            )
            if ours:
                self._conn.commit()
        except BaseException:
            if ours and self._conn.in_transaction:
                self._conn.rollback()
            raise
        return previous

    def claim_dispatch_alert(self, *, lease_seconds: int) -> str | None:
        """Lease the right to attempt one alert, or return ``None``.

        Used by the whole-sweep check, which has no counter to
        increment. The per-failure path uses
        :meth:`record_dispatch_failure` so its increment and its claim
        cannot be separated.
        """
        ours = False
        try:
            ours = self._begin_immediate()
            self._conn.execute(
                "INSERT OR IGNORE INTO dispatch_health "
                "(id, consecutive_failures) VALUES (1, 0)"
            )
            row = self._conn.execute(
                "SELECT alerted_at, alert_claim_token, alert_delivered_at "
                "FROM dispatch_health WHERE id = 1"
            ).fetchone()
            token: str | None = None
            if self._may_claim_alert(row, lease_seconds):
                token = uuid.uuid4().hex
                self._conn.execute(
                    "UPDATE dispatch_health SET alerted_at = ?, "
                    "alert_claim_token = ? WHERE id = 1",
                    (int(time.time()), token),
                )
            if ours:
                self._conn.commit()
        except BaseException:
            if ours and self._conn.in_transaction:
                self._conn.rollback()
            raise
        return token

    def mark_dispatch_alert_delivered(self, token: str | None) -> bool:
        """Record that an alert actually reached the operator.

        This is the durable fact that suppresses a repeat until a
        dispatch succeeds. Conditional on the token, so a process whose
        claim has already expired cannot mark a delivery against a
        newer one's claim.

        Returns whether the row was updated. ``False`` means the claim
        was superseded while the send was in flight — the message did
        go out, but another process now owns the latch, so a duplicate
        is possible. That is the safe direction and it is the one this
        design chooses.
        """
        if token is None:
            log_event(
                _logger,
                "dispatch_health.delivery_without_claim",
                detail=(
                    "an alert delivery named no claim, so nothing was "
                    "recorded; the caller lost track of the claim it took"
                ),
            )
            return False
        # Same discipline: do not commit a caller's open transaction.
        ours = not self._conn.in_transaction
        cursor = self._conn.execute(
            "UPDATE dispatch_health SET alert_delivered_at = ? "
            "WHERE id = 1 AND alert_claim_token = ?",
            (int(time.time()), token),
        )
        if ours:
            self._conn.commit()
        return cursor.rowcount > 0

    def get_dispatch_health(self) -> dict[str, Any]:
        """The whole row. A pure read, used by the tests.

        Nothing in the CLI surfaces this yet. Said plainly because an
        earlier version of this line claimed `ctrlrelay status` reads
        it, which it does not.
        """
        row = self._conn.execute(
            "SELECT * FROM dispatch_health WHERE id = 1"
        ).fetchone()
        return dict(row) if row else {}


    # Pending resumes (BLOCKED sessions awaiting an operator answer)

    def add_pending_resume(
        self,
        session_id: str,
        pipeline: str,
        repo: str,
        question: str,
        held_until: int | None = None,
        posted: bool | None = None,
        issue_number: int | None = None,
    ) -> None:
        """Record that a session exited BLOCKED_NEEDS_INPUT and can be
        resumed if an operator reply arrives later. Idempotent: re-inserting
        the same session_id refreshes ``created_at`` and clears any stale
        answer so a new BLOCKED on the same session_id starts fresh.

        ``held_until`` is set only by a caller that held the question
        back (a repeat inside its backoff window). ``posted`` says
        whether the question reached the operator; the backoff counts
        only rows that did, so a bridge outage cannot stretch the
        silence. ``None`` means the caller does not track it and the
        row is read as posted, which is what every row was before.

        A re-block rewrites the row, and the answer that drove the resume
        used to leave the table with it. Its time is kept in
        ``last_answered_at``: repeat counting and backoff both reset at
        the latest answer, and losing it made a question the operator
        had just answered look like one nobody ever answered.

        ``issue_number`` is the issue the run belongs to (#412), set when
        the row is written whether or not the question is ever posted
        there, so closing that issue retires the question. Whether it
        was asked on the issue is ``issue_comment_id``, set only by
        ``record_issue_question``."""
        self._conn.execute(
            """INSERT INTO pending_resumes
               (session_id, pipeline, repo, question, created_at,
                answer, answered_at, resumed_at, expired_at,
                expired_reason, held_until, posted, last_answered_at,
                issue_number)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?,
                       NULL, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 pipeline = excluded.pipeline,
                 repo = excluded.repo,
                 question = excluded.question,
                 created_at = excluded.created_at,
                 answer = NULL,
                 answered_at = NULL,
                 resumed_at = NULL,
                 expired_at = NULL,
                 expired_reason = NULL,
                 held_until = excluded.held_until,
                 posted = excluded.posted,
                 issue_number = excluded.issue_number,
                 issue_comment_id = NULL,
                 issue_asked_at = NULL,
                 answer_source = NULL,
                 answer_by = NULL,
                 answer_ref = NULL,
                 answer_noted_at = NULL,
                 last_answered_at = COALESCE(
                     pending_resumes.answered_at,
                     pending_resumes.last_answered_at
                 )""",
            (session_id, pipeline, repo, question, int(time.time()),
             held_until, None if posted is None else int(posted),
             None if issue_number is None else int(issue_number)),
        )
        self._conn.commit()

    def park_blocked_question(
        self, session_id: str, pipeline: str, repo: str, question: str,
        issue_number: int | None = None,
    ) -> None:
        """Record a question BEFORE the run waits on the chat answer (#324).

        The session goes ``running`` -> ``blocked`` and its question gets a
        ``pending_resumes`` row, in one commit. While the poller waits in
        process, this is only a record; if the poller stops during the
        wait, it is what the bridge routes a late answer to and what the
        sweeper resumes. Without it, the only record of the question was a
        coroutine, and a restart lost it with the session row left
        ``running``.
        """
        try:
            self._conn.execute(
                "UPDATE sessions SET status = 'blocked' "
                "WHERE id = ? AND status = 'running'",
                (session_id,),
            )
            # Commits both statements.
            self.add_pending_resume(
                session_id=session_id, pipeline=pipeline, repo=repo,
                question=question, issue_number=issue_number,
            )
        except BaseException:
            # Never leave the half-done write open through the wait: it
            # would hold the database's write lock against the bridge, and
            # a later commit would keep only half of it.
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    def take_parked_answer(self, session_id: str, answer: str) -> bool:
        """The in-process wait got its answer (#324): mark the parked row
        answered AND resumed, so neither the bridge nor the sweeper acts on
        it again, and put the session back to ``running``. One commit.

        Only an unanswered, open row is taken. Returns ``False`` when no
        such row is there, and changes nothing: the row may have been
        answered some other way first (a reply the bridge routed to the
        saved question), expired, or be missing. The caller tells these
        apart; only the first means another answer stands."""
        now = int(time.time())
        ours = not self._conn.in_transaction
        try:
            cursor = self._conn.execute(
                "UPDATE pending_resumes SET answer = ?, answered_at = ?, "
                "resumed_at = ? WHERE session_id = ? AND answered_at IS NULL "
                "AND resumed_at IS NULL AND expired_at IS NULL",
                (answer, now, now, session_id),
            )
            if cursor.rowcount > 0:
                self._conn.execute(
                    "UPDATE sessions SET status = 'running' "
                    "WHERE id = ? AND status = 'blocked'",
                    (session_id,),
                )
            if ours:
                self._conn.commit()
        except BaseException:
            if ours and self._conn.in_transaction:
                self._conn.rollback()
            raise
        return cursor.rowcount > 0

    def mark_session_running(self, session_id: str) -> None:
        """Put a ``blocked`` session back to ``running`` (#324): its run
        resumed in process with an answer the saved question did not
        record (that row expired or is missing)."""
        self._conn.execute(
            "UPDATE sessions SET status = 'running' "
            "WHERE id = ? AND status = 'blocked'",
            (session_id,),
        )
        self._conn.commit()

    def has_open_pending_resume(self, session_id: str, question: str) -> bool:
        """Whether this session already has a row for this same question
        that is neither resumed nor expired. Re-writing such a row would
        clear an answer already attached to it (#324)."""
        row = self._conn.execute(
            "SELECT 1 FROM pending_resumes WHERE session_id = ? "
            "AND question = ? AND resumed_at IS NULL AND expired_at IS NULL",
            (session_id, question),
        ).fetchone()
        return row is not None

    def list_blocked_sessions_holding_locks(self) -> list[dict[str, Any]]:
        """``blocked`` sessions that still hold a repo lock, with the
        process that ran them (#324). A blocked run holds its lock only
        while that process waits in memory for the answer; see
        :func:`ctrlrelay.core.orphans.release_dead_blocked_locks`."""
        rows = self._conn.execute(
            "SELECT s.id, s.repo, s.owner_pid, s.owner_run "
            "FROM repo_locks l JOIN sessions s ON s.id = l.session_id "
            "WHERE s.status = 'blocked'"
        ).fetchall()
        return [dict(r) for r in rows]

    def release_blocked_session_lock(self, session_id: str) -> bool:
        """Drop the lock of a session that is still ``blocked`` (#324).
        Guarded on the status, so a session resumed in the meantime keeps
        its lock."""
        cursor = self._conn.execute(
            "DELETE FROM repo_locks WHERE session_id = ? AND EXISTS ("
            "SELECT 1 FROM sessions WHERE id = ? AND status = 'blocked')",
            (session_id, session_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_oldest_unanswered_pending_resume(
        self,
        pipeline: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the oldest BLOCKED session still awaiting an operator
        answer, optionally filtered by pipeline. Used by the bridge to
        route an orphan Telegram reply when there's no in-memory pending
        question to match against."""
        if pipeline is None:
            row = self._conn.execute(
                "SELECT * FROM pending_resumes "
                "WHERE answered_at IS NULL AND expired_at IS NULL "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM pending_resumes "
                "WHERE answered_at IS NULL AND expired_at IS NULL "
                "AND pipeline = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (pipeline,),
            ).fetchone()
        return dict(row) if row else None

    def list_unanswered_pending_resumes(self) -> list[dict[str, Any]]:
        """All BLOCKED sessions still awaiting an answer, oldest first.
        Used by the bridge to disambiguate when multiple repos are blocked
        at once — FIFO routing would otherwise send the operator's reply
        about repo B onto repo A."""
        rows = self._conn.execute(
            "SELECT * FROM pending_resumes "
            "WHERE answered_at IS NULL AND expired_at IS NULL "
            "ORDER BY created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_pending_resume(self, session_id: str) -> dict[str, Any] | None:
        """One pending-resume row by session_id, whatever state it is in.

        Deliberately unfiltered, unlike every other reader here. The bridge
        needs to tell "that question was already answered" from "that
        question expired" from "no such question", and all three look
        identical through ``list_unanswered_pending_resumes`` — they are
        simply absent. Being absent is what used to make the bridge fall
        back to guessing.
        """
        row = self._conn.execute(
            "SELECT * FROM pending_resumes WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def expire_pending_resume(self, session_id: str, reason: str) -> bool:
        """Retire an unanswered question so it stops being routable.

        A question nobody can act on is worse than no question: the
        bridge keeps offering it as a target for an orphan reply, and
        the operator keeps seeing it re-posted. Guarded on
        ``answered_at IS NULL`` so a reply that landed in the same tick
        wins the race — expiry must never discard a real answer.

        Returns True iff a row was expired."""
        cursor = self._conn.execute(
            """UPDATE pending_resumes
               SET expired_at = ?, expired_reason = ?
               WHERE session_id = ? AND answered_at IS NULL
                 AND expired_at IS NULL""",
            (int(time.time()), reason, session_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_expirable_pending_resumes(
        self,
        older_than_ts: int,
    ) -> list[dict[str, Any]]:
        """Unanswered, unexpired questions created at or before
        ``older_than_ts``. The TTL sweep's candidate set."""
        rows = self._conn.execute(
            "SELECT * FROM pending_resumes "
            "WHERE answered_at IS NULL AND expired_at IS NULL "
            "AND created_at <= ? "
            "ORDER BY created_at ASC",
            (older_than_ts,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_pending_resumes_for_repo(self, repo: str) -> list[dict[str, Any]]:
        """Every pending-resume row for one repo, whatever state it is
        in, oldest first.

        Unfiltered on purpose: deciding whether a question is a repeat
        needs the expired rows (asked, never answered) and the answered
        ones (an answer resets the count), which every other reader here
        hides. The retention prune bounds the AGE of finished rows, not
        their number: a repo's history is however many questions it was
        asked inside the window, plus every row the prune never deletes
        whatever its age (open, and answered but not yet resumed).
        """
        rows = self._conn.execute(
            "SELECT * FROM pending_resumes WHERE repo = ? "
            "ORDER BY created_at ASC",
            (repo,),
        ).fetchall()
        return [dict(row) for row in rows]

    def prune_finished_pending_resumes(self, older_than_ts: int) -> int:
        """Delete finished questions whose last activity is at or before
        ``older_than_ts``. Returns the number of rows deleted.

        Finished means expired or resumed. An open question (unanswered
        and unexpired) and an answered one still waiting for the sweeper
        to resume it are never deleted, however old: either one is a
        session somebody can still act on. Age is taken from the latest
        of the row's timestamps, so a question asked long ago and
        retired yesterday is kept for the whole window.
        """
        cursor = self._conn.execute(
            """DELETE FROM pending_resumes
               WHERE (expired_at IS NOT NULL OR resumed_at IS NOT NULL)
                 AND MAX(created_at,
                         COALESCE(answered_at, 0),
                         COALESCE(resumed_at, 0),
                         COALESCE(expired_at, 0)) <= ?""",
            (older_than_ts,),
        )
        self._conn.commit()
        return cursor.rowcount

    def answer_pending_resume(
        self,
        session_id: str,
        answer: str,
        *,
        source: str | None = None,
        by: str | None = None,
        ref: int | None = None,
    ) -> bool:
        """Attach an operator's answer to a pending resume, marking it
        ready for the sweeper to execute. Returns True if a row was
        updated, False if the session_id was unknown or already answered.

        The first answer wins: a second one, from any channel, finds
        ``answered_at`` set and changes nothing. ``source``, ``by`` and
        ``ref`` say where it came from (#328); a chat answer leaves them
        NULL, as every answer did before."""
        cursor = self._conn.execute(
            """UPDATE pending_resumes
               SET answer = ?, answered_at = ?, answer_source = ?,
                   answer_by = ?, answer_ref = ?
               WHERE session_id = ? AND answered_at IS NULL
                 AND expired_at IS NULL""",
            (answer, int(time.time()), source, by, ref, session_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def record_issue_question(
        self, session_id: str, issue_number: int, comment_id: int,
        asked_at: int,
    ) -> bool:
        """Note that this open question was also asked on its GitHub
        issue (#328), so the issue is watched for an answer. Only an open
        row takes it: one already answered or expired is not watched."""
        cursor = self._conn.execute(
            "UPDATE pending_resumes SET issue_number = ?, "
            "issue_comment_id = ?, issue_asked_at = ? "
            "WHERE session_id = ? AND answered_at IS NULL "
            "AND expired_at IS NULL",
            (issue_number, comment_id, asked_at, session_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_issue_questions_to_watch(self) -> list[dict[str, Any]]:
        """Open questions that were also asked on a GitHub issue (#328):
        the only issues read for an answer."""
        rows = self._conn.execute(
            "SELECT * FROM pending_resumes "
            "WHERE answered_at IS NULL AND expired_at IS NULL "
            "AND issue_comment_id IS NOT NULL "
            "ORDER BY created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_answer_noted(self, session_id: str, answered_at: int) -> bool:
        """Claim the one note that tells the other channel about this
        answer (#328). True for the first caller only, and only while the
        row still carries the answer given at ``answered_at``."""
        cursor = self._conn.execute(
            "UPDATE pending_resumes SET answer_noted_at = ? "
            "WHERE session_id = ? AND answered_at = ? "
            "AND answer_noted_at IS NULL",
            (int(time.time()), session_id, answered_at),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def release_answer_noted(self, session_id: str, answered_at: int) -> None:
        """Undo ``mark_answer_noted`` when the note could not be posted,
        so a later caller can try it again."""
        self._conn.execute(
            "UPDATE pending_resumes SET answer_noted_at = NULL "
            "WHERE session_id = ? AND answered_at = ?",
            (session_id, answered_at),
        )
        self._conn.commit()

    def list_pending_resumes_to_execute(self) -> list[dict[str, Any]]:
        """Rows that have been answered by the operator but not yet
        resumed. Poller's pending-resume sweeper loads these and drives
        the pipeline resume. Oldest first so FIFO semantics hold.

        Expired rows are excluded even though ``answer_pending_resume``
        already refuses to answer one. That guard only holds while every
        process runs the same code: the bridge writes the answer and the
        poller reads it back, they are separate daemons, and a deploy
        that restarts one before the other leaves a window where an old
        bridge stamps ``answered_at`` on a row this poller has already
        expired. Filtering on read closes that window from the side that
        actually acts on the row.
        """
        rows = self._conn.execute(
            "SELECT * FROM pending_resumes "
            "WHERE answered_at IS NOT NULL AND resumed_at IS NULL "
            "AND expired_at IS NULL "
            "ORDER BY answered_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_pending_resume_resumed(
        self,
        session_id: str,
        answered_at: int | None = None,
        answer: str | None = None,
    ) -> bool:
        """Mark a pending resume as executed so the sweeper doesn't pick
        it up again.

        Guarded on ``answered_at IS NOT NULL``: during a resume that
        re-blocks, the pipeline inserts a FRESH pending_resumes row
        (via ``add_pending_resume`` which is INSERT OR REPLACE,
        clearing answer/answered_at/resumed_at). If the sweeper then
        blindly stamped ``resumed_at`` on that fresh unanswered row,
        the next operator reply would set ``answered_at`` but leave
        ``resumed_at`` populated, hiding the row from
        ``list_pending_resumes_to_execute`` forever and permanently
        wedging the session. The guard makes this a no-op when the
        row has been refreshed.

        ``answered_at`` and ``answer``, when given, are those of the row
        the caller took (#324). Since the question is saved before the
        wait, a resume that re-blocks can have its NEW question answered
        (through the bridge) before this call; matching the answer and its
        time keeps this call from marking that new answer resumed, which
        would hide it for ever.

        Returns True iff a row was updated."""
        if answered_at is None:
            cursor = self._conn.execute(
                "UPDATE pending_resumes SET resumed_at = ? "
                "WHERE session_id = ? AND answered_at IS NOT NULL",
                (int(time.time()), session_id),
            )
        else:
            cursor = self._conn.execute(
                "UPDATE pending_resumes SET resumed_at = ? "
                "WHERE session_id = ? AND answered_at = ? "
                "AND answer IS ?",
                (int(time.time()), session_id, answered_at, answer),
            )
        self._conn.commit()
        return cursor.rowcount > 0

    # Automation decisions (operator answers persisted across sweeps)

    def record_automation_decision(
        self,
        *,
        repo: str,
        operation: str,
        item_id: str,
        decision: str,
        decided_by: str = "operator",
        context: str | None = None,
        policy: str = "",
    ) -> None:
        """Record an operator decision so future sweeps can avoid
        re-asking the same question.

        ``operation`` namespaces the decision domain
        (e.g. ``"dependabot_pr"`` or ``"codeql_alert"``); ``item_id``
        is the specific item the decision applies to (e.g. ``"#60"``
        for PR #60). ``decision`` is the operator's verbatim answer —
        the agent that reads this back interprets it; we don't try to
        normalize to yes/no here because Telegram replies are free-form.
        """
        self._conn.execute(
            """INSERT INTO automation_decisions
               (repo, operation, policy, item_id, decision,
                decided_by, decided_at, context)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (repo, operation, policy, item_id, decision,
             decided_by, int(time.time()), context),
        )
        self._conn.commit()

    def list_recent_automation_decisions(
        self,
        repo: str,
        *,
        operation: str | None = None,
        since_ts: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Recent operator decisions for this repo, newest first.

        ``operation`` namespaces the read so a Dependabot-PR consumer
        only sees ``dependabot_pr`` rows, not e.g. ``codeql_alert``
        decisions also tracked in the same repo. Without this filter,
        a "suppress this CVE" answer for a CodeQL alert would render
        into the Dependabot prompt as a prior-PR decision and the
        agent could act on it. Defaults to ``None`` (all operations)
        to preserve the original behaviour for callers that want a
        full audit view.

        ``since_ts`` (unix seconds) filters to decisions made
        at-or-after that point — used by the secops prompt builder to
        inject a 30-day rolling window into the agent's context so the
        agent can act on prior decisions instead of asking again.
        """
        clauses = ["repo = ?"]
        params: list[Any] = [repo]
        if operation is not None:
            clauses.append("operation = ?")
            params.append(operation)
        if since_ts is not None:
            clauses.append("decided_at >= ?")
            params.append(since_ts)
        sql = (
            "SELECT * FROM automation_decisions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY decided_at DESC LIMIT ?"
        )
        params.append(limit)
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    # PR watches (durable, cross-restart)

    def add_pr_watch(
        self,
        *,
        repo: str,
        pr_number: int,
        issue_number: int,
        session_id: str | None,
        pr_url: str | None,
        started_at: int | None = None,
    ) -> None:
        """Record an in-flight merge watcher so a poller restart can
        rehydrate it. Idempotent on ``(repo, pr_number)``.

        When ``started_at`` is None (production path, including the
        rehydrate re-insert), a new row gets ``now()`` and an existing
        row keeps its original ``started_at``. Preserving the original
        timestamp is load-bearing: if every rehydrate refreshed it, the
        7-day watch deadline would reset on every poller restart and
        abandoned PRs would never time out.

        When ``started_at`` is explicit (tests, seed flows), the row
        is overwritten outright.
        """
        if started_at is None:
            self._conn.execute(
                """INSERT INTO pr_watches
                   (repo, pr_number, session_id, issue_number, pr_url, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(repo, pr_number) DO UPDATE SET
                       session_id = excluded.session_id,
                       issue_number = excluded.issue_number,
                       pr_url = excluded.pr_url""",
                (repo, pr_number, session_id, issue_number, pr_url,
                 int(time.time())),
            )
        else:
            self._conn.execute(
                """INSERT OR REPLACE INTO pr_watches
                   (repo, pr_number, session_id, issue_number, pr_url, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (repo, pr_number, session_id, issue_number, pr_url,
                 int(started_at)),
            )
        self._conn.commit()

    def remove_pr_watch(self, repo: str, pr_number: int) -> bool:
        """Delete a PR watch row after a terminal outcome (merge
        detected, or watch window expired). Returns True iff a row was
        deleted so callers can tell whether the row was still present."""
        cursor = self._conn.execute(
            "DELETE FROM pr_watches WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_pr_watches(self) -> list[dict[str, Any]]:
        """Return all surviving PR watches, oldest first. Called by the
        poller at startup (before ``run_poll_loop``) to rehydrate any
        watchers cancelled by the previous shutdown."""
        rows = self._conn.execute(
            "SELECT * FROM pr_watches ORDER BY started_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    # Events (status page feed)

    # Seconds a best-effort event write may wait for a contended lock.
    EVENT_WRITE_TIMEOUT = 0.25

    @staticmethod
    def _insert_event(
        conn: sqlite3.Connection,
        kind: str,
        repo: str | None,
        issue_number: int | None,
        session_id: str | None,
        detail: dict[str, Any] | None,
        ts: int | None,
    ) -> None:
        conn.execute(
            """INSERT INTO events
               (ts, kind, repo, issue_number, session_id, detail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                int(time.time()) if ts is None else int(ts),
                str(kind),
                repo,
                issue_number,
                session_id,
                None if detail is None else json.dumps(detail),
            ),
        )
        conn.commit()

    def record_event(
        self,
        kind: str,
        *,
        repo: str | None = None,
        issue_number: int | None = None,
        session_id: str | None = None,
        detail: dict[str, Any] | None = None,
        ts: int | None = None,
    ) -> None:
        """Append one row to the events feed on the main connection and
        commit. Raises on failure, and a contended write waits for the
        main connection's busy timeout. The daemon uses
        ``events.emit_event`` instead, which goes through
        :meth:`record_event_nowait`."""
        self._insert_event(
            self._conn, kind, repo, issue_number, session_id, detail, ts
        )

    def record_event_nowait(
        self,
        kind: str,
        *,
        repo: str | None = None,
        issue_number: int | None = None,
        session_id: str | None = None,
        detail: dict[str, Any] | None = None,
        ts: int | None = None,
    ) -> None:
        """Like :meth:`record_event`, but through a separate connection to
        the same file that waits at most ``EVENT_WRITE_TIMEOUT`` seconds
        for a lock. Using its own connection keeps a failed event write
        from leaving the main connection mid-transaction. Raises on
        failure; the side connection is rolled back first.

        For an in-memory database there is no file to share, so this
        falls back to the main connection (and its busy timeout) - but
        only when no transaction is open there. Inserting would join that
        transaction and the commit would publish somebody else's
        half-finished write, so the event is skipped instead and
        ``events.write_skipped`` logged."""
        if self._in_memory:
            if self._main_busy_for_events(kind):
                return
            self.record_event(
                kind, repo=repo, issue_number=issue_number,
                session_id=session_id, detail=detail, ts=ts,
            )
            return
        conn = self._event_connection()
        try:
            self._insert_event(
                conn, kind, repo, issue_number, session_id, detail, ts
            )
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise

    def _main_busy_for_events(self, kind: str) -> bool:
        """For an in-memory database: whether the main connection has a
        transaction open, in which case an event write or prune must not
        touch it. Logs ``events.write_skipped`` when it does."""
        if not self._conn.in_transaction:
            return False
        log_event(
            _logger,
            "events.write_skipped",
            kind=str(kind),
            reason="main_connection_in_transaction",
        )
        return True

    # The "once" lookup. Equality on session_id leads, so it is answered
    # from idx_events_session (session_id, ts); kind, repo and the PR
    # number are checked on that session's few rows.
    _EVENT_ONCE_SQL = """INSERT INTO events
           (ts, kind, repo, issue_number, session_id, detail)
           SELECT ?, ?, ?, ?, ?, ?
           WHERE NOT EXISTS (
               SELECT 1 FROM events
               WHERE session_id IS ? AND kind = ? AND repo IS ?
                 AND json_extract(detail, '$.pr_number') = ?
           )"""

    def record_event_once_nowait(
        self,
        kind: str,
        *,
        pr_number: int,
        repo: str | None = None,
        issue_number: int | None = None,
        session_id: str | None = None,
        detail: dict[str, Any] | None = None,
        ts: int | None = None,
    ) -> bool:
        """Like :meth:`record_event_nowait`, but writes nothing when an
        event of ``kind`` already names ``pr_number`` (in
        ``detail.pr_number``) for this repo and session. Returns whether a
        row was written.

        The check and the insert are ONE statement and one commit on the
        side connection, so the lock wait is bounded by
        ``EVENT_WRITE_TIMEOUT`` once, not once per step, and two writers
        cannot both pass the check. In-memory: the main connection, under
        the same no-open-transaction rule as record_event_nowait."""
        params = (
            int(time.time()) if ts is None else int(ts),
            str(kind),
            repo,
            issue_number,
            session_id,
            None if detail is None else json.dumps(detail),
            session_id,
            str(kind),
            repo,
            int(pr_number),
        )
        if self._in_memory:
            if self._main_busy_for_events(kind):
                return False
            conn = self._conn
        else:
            conn = self._event_connection()
        try:
            cursor = conn.execute(self._EVENT_ONCE_SQL, params)
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        return cursor.rowcount == 1

    def _event_connection(self) -> sqlite3.Connection:
        """The short-timeout side connection for event writes, opened on
        first use."""
        if self._event_conn is None:
            self._event_conn = sqlite3.connect(
                str(self.db_path), timeout=self.EVENT_WRITE_TIMEOUT
            )
        return self._event_conn

    def list_events(
        self, *, since_ts: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Return events with ``ts >= since_ts``, newest first, at most
        ``limit`` rows. ``detail`` is decoded from JSON (None if absent)."""
        rows = self._conn.execute(
            """SELECT * FROM events WHERE ts >= ?
               ORDER BY ts DESC, id DESC LIMIT ?""",
            (int(since_ts), int(limit)),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw = item.get("detail")
            item["detail"] = None if raw is None else json.loads(raw)
            out.append(item)
        return out

    def prune_events(self, older_than_ts: int) -> int:
        """Delete events with ``ts < older_than_ts``; return rows deleted."""
        cursor = self._conn.execute(
            "DELETE FROM events WHERE ts < ?", (int(older_than_ts),)
        )
        self._conn.commit()
        return cursor.rowcount

    def prune_events_nowait(self, older_than_ts: int) -> int:
        """Like :meth:`prune_events`, but through the event side connection,
        so it waits at most ``EVENT_WRITE_TIMEOUT`` seconds for a lock.

        The daemon prunes with this one. A DELETE on the main connection
        would join (and commit) any transaction another coroutine has open
        there, and one that failed on a busy lock would leave the main
        connection holding a stale read snapshot, so the next real write on
        it could fail. Raises on failure; the side connection is rolled
        back first. An in-memory database falls back to the main
        connection, as for :meth:`record_event_nowait`."""
        if self._in_memory:
            # Same rule as record_event_nowait: never commit a transaction
            # somebody else has open on the main connection. Skipped, so
            # nothing was deleted.
            if self._main_busy_for_events("prune"):
                return 0
            return self.prune_events(older_than_ts)
        conn = self._event_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM events WHERE ts < ?", (int(older_than_ts),)
            )
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        return cursor.rowcount

    # Valid post-merge cleanup phases, in monotonic progression. Each
    # phase stamps *after* its side effect has succeeded, so if the
    # poller is killed mid-cleanup and rehydrates later, the resumed
    # handle_merge can skip every step the prior run already completed.
    _PR_WATCH_CLEANUP_PHASES = frozenset(
        {"commented", "closed", "notified"}
    )

    def set_pr_watch_cleanup_phase(
        self, repo: str, pr_number: int, phase: str
    ) -> None:
        """Stamp a durable marker that a given post-merge cleanup step
        has succeeded for this watch. Caller must invoke AFTER the
        side effect completes so a process kill between the stamp and
        the next step never leaves us claiming work we didn't do.

        Validated: ``phase`` must be one of the known monotonic values
        (``commented`` / ``closed`` / ``notified``). An invalid value
        raises rather than silently corrupting resume semantics.
        """
        if phase not in self._PR_WATCH_CLEANUP_PHASES:
            raise ValueError(
                f"invalid pr_watches cleanup_phase: {phase!r}; "
                f"expected one of {sorted(self._PR_WATCH_CLEANUP_PHASES)}"
            )
        self._conn.execute(
            "UPDATE pr_watches SET cleanup_phase = ? "
            "WHERE repo = ? AND pr_number = ?",
            (phase, repo, pr_number),
        )
        self._conn.commit()

    def get_pr_watch_cleanup_phase(
        self, repo: str, pr_number: int
    ) -> str | None:
        """Read the persisted cleanup phase for a watch, used by
        rehydration so handle_merge can skip completed steps. Returns
        None when the row is missing or the phase column is NULL
        (fresh watch that never reached cleanup)."""
        row = self._conn.execute(
            "SELECT cleanup_phase FROM pr_watches "
            "WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        ).fetchone()
        if row is None:
            return None
        value = row["cleanup_phase"]
        return value if value else None
