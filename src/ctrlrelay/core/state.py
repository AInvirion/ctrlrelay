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
    agent_session_id TEXT
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
    resumed_at INTEGER
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
                "WHERE answered_at IS NULL "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM pending_resumes "
                "WHERE answered_at IS NULL AND pipeline = ? "
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
            "WHERE answered_at IS NULL "
            "ORDER BY created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def answer_pending_resume(self, session_id: str, answer: str) -> bool:
        """Attach an operator's answer to a pending resume, marking it
        ready for the sweeper to execute. Returns True if a row was
        updated, False if the session_id was unknown or already answered."""
        cursor = self._conn.execute(
            """UPDATE pending_resumes
               SET answer = ?, answered_at = ?
               WHERE session_id = ? AND answered_at IS NULL""",
            (answer, int(time.time()), session_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_pending_resumes_to_execute(self) -> list[dict[str, Any]]:
        """Rows that have been answered by the operator but not yet
        resumed. Poller's pending-resume sweeper loads these and drives
        the pipeline resume. Oldest first so FIFO semantics hold."""
        rows = self._conn.execute(
            "SELECT * FROM pending_resumes "
            "WHERE answered_at IS NOT NULL AND resumed_at IS NULL "
            "ORDER BY answered_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_pending_resume_resumed(self, session_id: str) -> None:
        """Mark a pending resume as executed so the sweeper doesn't pick
        it up again."""
        self._conn.execute(
            "UPDATE pending_resumes SET resumed_at = ? WHERE session_id = ?",
            (int(time.time()), session_id),
        )
        self._conn.commit()
