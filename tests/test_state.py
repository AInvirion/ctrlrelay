"""Tests for SQLite state management."""

from pathlib import Path

import pytest

from dev_sync.core.state import StateDB


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

        expected = {"sessions", "repo_locks", "github_cursor", "telegram_pending", "automation_decisions"}
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
        db.release_lock("owner/repo")
        result = db.acquire_lock("owner/repo", "session-456")
        assert result is True
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
