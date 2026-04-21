"""Tests for SQLite state management."""

from pathlib import Path

from ctrlrelay.core.state import StateDB


class TestStateDBInit:
    def test_creates_database_file(self, tmp_path: Path) -> None:
        """StateDB should create the database file if it doesn't exist."""
        db_path = tmp_path / "state.db"
        assert not db_path.exists()

        db = StateDB(db_path)
        db.close()

        assert db_path.exists()

    def test_creates_tables(self, tmp_path: Path) -> None:
        """StateDB should create all required tables on init."""
        db_path = tmp_path / "state.db"
        db = StateDB(db_path)

        # Check tables exist
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {row[0] for row in tables}

        expected = {
            "sessions", "repo_locks", "github_cursor",
            "telegram_pending", "automation_decisions",
        }
        assert expected.issubset(table_names)

        db.close()


class TestRepoLocks:
    def test_acquire_lock_succeeds_when_free(self, tmp_path: Path) -> None:
        """Should acquire lock when repo is not locked."""
        db = StateDB(tmp_path / "state.db")
        result = db.acquire_lock("owner/repo", "session-123")
        assert result is True
        db.close()

    def test_acquire_lock_fails_when_held(self, tmp_path: Path) -> None:
        """Should fail to acquire lock when already held."""
        db = StateDB(tmp_path / "state.db")
        db.acquire_lock("owner/repo", "session-123")
        result = db.acquire_lock("owner/repo", "session-456")
        assert result is False
        db.close()

    def test_release_lock(self, tmp_path: Path) -> None:
        """Should release lock so it can be re-acquired."""
        db = StateDB(tmp_path / "state.db")
        db.acquire_lock("owner/repo", "session-123")
        released = db.release_lock("owner/repo", "session-123")
        assert released is True
        result = db.acquire_lock("owner/repo", "session-456")
        assert result is True
        db.close()

    def test_release_lock_wrong_session(self, tmp_path: Path) -> None:
        """Should not release lock if session doesn't match."""
        db = StateDB(tmp_path / "state.db")
        db.acquire_lock("owner/repo", "session-123")
        released = db.release_lock("owner/repo", "session-456")
        assert released is False
        holder = db.get_lock_holder("owner/repo")
        assert holder == "session-123"
        db.close()

    def test_get_lock_holder(self, tmp_path: Path) -> None:
        """Should return the session holding the lock."""
        db = StateDB(tmp_path / "state.db")
        db.acquire_lock("owner/repo", "session-123")
        holder = db.get_lock_holder("owner/repo")
        assert holder == "session-123"
        db.close()

    def test_get_lock_holder_when_free(self, tmp_path: Path) -> None:
        """Should return None when repo is not locked."""
        db = StateDB(tmp_path / "state.db")
        holder = db.get_lock_holder("owner/repo")
        assert holder is None
        db.close()


class TestAgentSessionId:
    """The sessions table persists the agent (Claude) session UUID separately
    from our composite orchestrator id, so resumes can feed the real UUID to
    `claude --resume`."""

    def test_sessions_table_has_agent_session_id_column(self, tmp_path: Path) -> None:
        db = StateDB(tmp_path / "state.db")
        cols = {
            row[1]
            for row in db.execute("PRAGMA table_info(sessions)").fetchall()
        }
        assert "agent_session_id" in cols
        db.close()

    def test_set_and_get_agent_session_id_roundtrip(self, tmp_path: Path) -> None:
        import time as _time

        db = StateDB(tmp_path / "state.db")
        session_id = "dev-owner-repo-1-deadbeef"
        agent_uuid = "b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10"

        db.execute(
            """INSERT INTO sessions
               (id, pipeline, repo, status, started_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, "dev", "owner/repo", "running", int(_time.time())),
        )
        db.commit()

        assert db.get_agent_session_id(session_id) is None

        db.set_agent_session_id(session_id, agent_uuid)
        assert db.get_agent_session_id(session_id) == agent_uuid

        db.close()

    def test_get_agent_session_id_returns_none_for_missing_session(
        self, tmp_path: Path
    ) -> None:
        db = StateDB(tmp_path / "state.db")
        assert db.get_agent_session_id("nope") is None
        db.close()

    def test_agent_session_id_migrates_onto_existing_db(self, tmp_path: Path) -> None:
        """Opening a pre-existing database written by an older version should
        transparently add the `agent_session_id` column (backfilled to NULL)."""
        import sqlite3

        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE sessions (
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
                summary TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("old-session", "dev", "owner/repo", "done", 1),
        )
        conn.commit()
        conn.close()

        db = StateDB(db_path)
        cols = {
            row[1]
            for row in db.execute("PRAGMA table_info(sessions)").fetchall()
        }
        assert "agent_session_id" in cols
        assert db.get_agent_session_id("old-session") is None
        db.close()
