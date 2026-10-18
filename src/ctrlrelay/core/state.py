"""SQLite state management for ctrlrelay orchestrator."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

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
    -- Scrubbed, capped tail of the worker's final stdout/stderr (#204).
    -- The exit-code column above was declared from the start and left
    -- NULL on the failure path, so the row for the session that broke
    -- `zzsites#853` held `None` while the log line for the SAME session
    -- recorded `exit_code 1`. With no output kept anywhere either —
    -- `stderr_tail` was `""` and the log keeps a hash and a length, not
    -- the text — the only durable evidence of why 163 dispatches failed
    -- was that each took about two seconds. See
    -- `ctrlrelay.core.redact` for what is removed before it is stored.
    output_tail TEXT
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
    expired_reason TEXT
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

        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

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
        if "output_tail" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN output_tail TEXT"
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

        self._ensure_dispatch_health_row()

    def close(self) -> None:
        """Close the database connection."""
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

    # Agent session ids

    def set_agent_session_id(self, session_id: str, agent_session_id: str) -> None:
        """Persist Claude's session UUID against our composite session id.

        ``agent_session_id`` is what ``claude --resume`` needs — our
        composite id fails validation on newer CLI versions.
        """
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

        ``output_tail`` must already be scrubbed and capped by
        :func:`ctrlrelay.core.redact.worker_output_tail`. This method
        does not sanitise, deliberately: a sanitiser that lives in the
        writer is one that a second writer can bypass without
        noticing, and the scrubbing has its own tests.

        An empty ``output_tail`` is stored as ``NULL`` rather than
        ``""`` — "the agent wrote nothing before it died" is a real
        and differently-shaped failure, and the two should not read
        the same on inspection.
        """
        cursor = self._conn.execute(
            "UPDATE sessions SET claude_exit_code = ?, output_tail = ? "
            "WHERE id = ?",
            (exit_code, output_tail or None, session_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # Dispatch health (#204 — is the relay able to run a worker at all)

    def _ensure_dispatch_health_row(self) -> None:
        """Create the single ``dispatch_health`` row if it is absent.

        **Commits.** An earlier version left this INSERT uncommitted and
        called it from the read accessors too, which opened a write
        transaction on a method that only reads. SQLite then held a
        RESERVED lock for the life of that connection's transaction, and
        the next writer on ANY other connection to the same file failed
        with ``database is locked`` — measured: a bare
        ``dispatch_failure_count()`` on one connection made an ordinary
        ``INSERT INTO sessions`` on a second one fail. The poller and the
        bridge both open ``state.db``, so a read-shaped call could have
        wedged the live relay's whole database. Called from the write
        paths only, and it commits.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO dispatch_health "
            "(id, consecutive_failures) VALUES (1, 0)"
        )
        self._conn.commit()

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

    def record_dispatch_failure(self, threshold: int) -> tuple[int, bool]:
        """Extend the failure run and decide, in ONE transaction.

        Returns ``(consecutive_failures, may_alert)``.

        The increment, the threshold comparison and the latch claim are
        a single ``BEGIN IMMEDIATE`` transaction because two pollers can
        share one ``state.db`` — it has happened on this deployment. Done
        as three statements, a success recorded by the other process can
        land between this one's read and its claim, and a relay that has
        just recovered is announced as still failing. Reading the count
        back in a separate statement had the same hazard pointed the
        other way.

        ``may_alert`` is ``True`` at most once per failure run. It is the
        right to ATTEMPT one delivery, not a record that one happened —
        see :meth:`release_dispatch_alert`.
        """
        if threshold < 1:
            raise ValueError(f"threshold must be at least 1, got {threshold!r}")
        # `isolation_level` is sqlite3's default, so an explicit
        # BEGIN IMMEDIATE is needed to take the write lock up front
        # rather than upgrading mid-transaction and risking a deadlock
        # between two writers.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
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
                "SELECT consecutive_failures, alerted_at "
                "FROM dispatch_health WHERE id = 1"
            ).fetchone()
            consecutive = int(row["consecutive_failures"])
            may_alert = consecutive >= threshold and row["alerted_at"] is None
            if may_alert:
                self._conn.execute(
                    "UPDATE dispatch_health SET alerted_at = ? WHERE id = 1",
                    (int(time.time()),),
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return consecutive, may_alert

    def reset_dispatch_failures(self) -> int:
        """Clear the run and the alert latch; return the previous length.

        The return value is what lets the caller log a recovery only
        when there was something to recover from — otherwise every
        successful dispatch on a healthy relay would log one, and a
        line that appears constantly is a line nobody reads.

        One transaction, for the same reason as
        :meth:`record_dispatch_failure`.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
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
                "alerted_at = NULL, last_success_at = ? WHERE id = 1",
                (int(time.time()),),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return previous

    def claim_dispatch_alert(self) -> bool:
        """Take the right to attempt one alert, or return ``False``.

        ``True`` at most once per failure run: the UPDATE is conditional
        on ``alerted_at IS NULL``, so the claim and the test are one
        statement and two processes racing cannot both win.

        Used by the whole-sweep check, which has no counter to
        increment. The per-failure path uses
        :meth:`record_dispatch_failure` so its increment and its claim
        cannot be separated.
        """
        self._ensure_dispatch_health_row()
        cursor = self._conn.execute(
            "UPDATE dispatch_health SET alerted_at = ? "
            "WHERE id = 1 AND alerted_at IS NULL",
            (int(time.time()),),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def release_dispatch_alert(self) -> None:
        """Give the claim back, because delivery failed.

        Claiming the latch is the right to ATTEMPT one message, not a
        record that one arrived. Latching on the attempt means a single
        failed `send` — a bridge mid-restart, a socket that is not there
        yet — silences the alert for the whole outage, which is this
        ticket's own defect reproduced inside the fix for it. Measured
        before this method existed: with the channel absent for the
        first failure and healthy for the next fifty, zero messages were
        ever delivered.

        Does NOT clear the counter. The run is still running; only the
        right to announce it is handed back, so the next failure tries
        again.
        """
        self._conn.execute(
            "UPDATE dispatch_health SET alerted_at = NULL WHERE id = 1"
        )
        self._conn.commit()

    def get_dispatch_health(self) -> dict[str, Any]:
        """The whole row, for `ctrlrelay status` and for tests. A pure read."""
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
    ) -> None:
        """Record that a session exited BLOCKED_NEEDS_INPUT and can be
        resumed if an operator reply arrives later. Idempotent: re-inserting
        the same session_id refreshes ``created_at`` and clears any stale
        answer so a new BLOCKED on the same session_id starts fresh."""
        self._conn.execute(
            """INSERT OR REPLACE INTO pending_resumes
               (session_id, pipeline, repo, question, created_at,
                answer, answered_at, resumed_at)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)""",
            (session_id, pipeline, repo, question, int(time.time())),
        )
        self._conn.commit()

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

    def answer_pending_resume(self, session_id: str, answer: str) -> bool:
        """Attach an operator's answer to a pending resume, marking it
        ready for the sweeper to execute. Returns True if a row was
        updated, False if the session_id was unknown or already answered."""
        cursor = self._conn.execute(
            """UPDATE pending_resumes
               SET answer = ?, answered_at = ?
               WHERE session_id = ? AND answered_at IS NULL
                 AND expired_at IS NULL""",
            (answer, int(time.time()), session_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

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

    def mark_pending_resume_resumed(self, session_id: str) -> bool:
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

        Returns True iff a row was updated."""
        cursor = self._conn.execute(
            "UPDATE pending_resumes SET resumed_at = ? "
            "WHERE session_id = ? AND answered_at IS NOT NULL",
            (int(time.time()), session_id),
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
