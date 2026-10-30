"""SQLite state management for ctrlrelay orchestrator."""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.state")

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
        """
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
