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


class TestPRWatches:
    """pr_watches stores in-flight merge watchers so a poller restart
    rehydrates them. Rows are written on `dev.pr.watching` and removed
    on the terminal merged / timed-out events only — a cancellation
    (shutdown) deliberately leaves the row behind."""

    def test_pr_watches_table_is_created(self, tmp_path: Path) -> None:
        db = StateDB(tmp_path / "state.db")
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {row[0] for row in tables}
        assert "pr_watches" in table_names
        db.close()

    def test_pr_watches_schema_has_expected_columns(self, tmp_path: Path) -> None:
        """Acceptance: (session_id, repo, issue_number, pr_number, pr_url,
        started_at)."""
        db = StateDB(tmp_path / "state.db")
        cols = {
            row[1]
            for row in db.execute("PRAGMA table_info(pr_watches)").fetchall()
        }
        expected = {
            "session_id", "repo", "issue_number", "pr_number",
            "pr_url", "started_at",
        }
        assert expected.issubset(cols)
        db.close()

    def test_add_and_list_pr_watch(self, tmp_path: Path) -> None:
        db = StateDB(tmp_path / "state.db")
        db.add_pr_watch(
            repo="owner/r1", pr_number=42,
            issue_number=77, session_id="sess-1",
            pr_url="https://github.com/owner/r1/pull/42",
        )
        rows = db.list_pr_watches()
        assert len(rows) == 1
        assert rows[0]["repo"] == "owner/r1"
        assert rows[0]["pr_number"] == 42
        assert rows[0]["issue_number"] == 77
        assert rows[0]["session_id"] == "sess-1"
        assert rows[0]["pr_url"] == "https://github.com/owner/r1/pull/42"
        assert rows[0]["started_at"] > 0
        db.close()

    def test_remove_pr_watch_returns_true_when_present(
        self, tmp_path: Path
    ) -> None:
        db = StateDB(tmp_path / "state.db")
        db.add_pr_watch(
            repo="owner/r1", pr_number=42,
            issue_number=77, session_id=None, pr_url=None,
        )
        assert db.remove_pr_watch("owner/r1", 42) is True
        assert db.list_pr_watches() == []
        db.close()

    def test_remove_pr_watch_returns_false_when_missing(
        self, tmp_path: Path
    ) -> None:
        db = StateDB(tmp_path / "state.db")
        assert db.remove_pr_watch("owner/r1", 42) is False
        db.close()

    def test_add_pr_watch_is_idempotent_on_repo_pr_number(
        self, tmp_path: Path
    ) -> None:
        """Re-inserting the same (repo, pr_number) refreshes the row
        rather than raising — covers the rehydrate → re-spawn path
        where the existing task re-inserts."""
        db = StateDB(tmp_path / "state.db")
        db.add_pr_watch(
            repo="owner/r1", pr_number=42, issue_number=77,
            session_id="sess-1", pr_url="u1", started_at=1000,
        )
        db.add_pr_watch(
            repo="owner/r1", pr_number=42, issue_number=77,
            session_id="sess-2", pr_url="u2", started_at=2000,
        )
        rows = db.list_pr_watches()
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-2"
        assert rows[0]["pr_url"] == "u2"
        assert rows[0]["started_at"] == 2000
        db.close()

    def test_add_pr_watch_preserves_started_at_on_conflict_without_explicit_ts(
        self, tmp_path: Path
    ) -> None:
        """Production rehydrate path: re-inserting the same PR without
        supplying ``started_at`` must preserve the original row's
        timestamp. Otherwise every poller restart resets the 7-day
        deadline and abandoned PRs never time out (codex P2 on PR #111)."""
        db = StateDB(tmp_path / "state.db")
        db.add_pr_watch(
            repo="owner/r1", pr_number=42, issue_number=77,
            session_id="sess-1", pr_url="u1", started_at=1000,
        )
        # Re-insert WITHOUT started_at, simulating the rehydrated
        # pr_watch_task calling add_pr_watch again on spawn. Metadata
        # updates, timestamp does not.
        db.add_pr_watch(
            repo="owner/r1", pr_number=42, issue_number=77,
            session_id="sess-2", pr_url="u2",
        )
        rows = db.list_pr_watches()
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-2"
        assert rows[0]["pr_url"] == "u2"
        assert rows[0]["started_at"] == 1000
        db.close()

    def test_add_pr_watch_without_started_at_new_row_uses_now(
        self, tmp_path: Path
    ) -> None:
        """New inserts (no existing row) still get ``now()`` when the
        caller omits ``started_at``. Regression guard for the conflict
        path so it doesn't accidentally break initial inserts."""
        import time

        db = StateDB(tmp_path / "state.db")
        before = int(time.time())
        db.add_pr_watch(
            repo="owner/r1", pr_number=42, issue_number=77,
            session_id="sess-1", pr_url="u1",
        )
        after = int(time.time())
        rows = db.list_pr_watches()
        assert len(rows) == 1
        assert before <= rows[0]["started_at"] <= after
        db.close()

    def test_list_pr_watches_oldest_first(self, tmp_path: Path) -> None:
        db = StateDB(tmp_path / "state.db")
        db.add_pr_watch(
            repo="owner/r1", pr_number=1, issue_number=10,
            session_id=None, pr_url=None, started_at=200,
        )
        db.add_pr_watch(
            repo="owner/r2", pr_number=2, issue_number=20,
            session_id=None, pr_url=None, started_at=100,
        )
        rows = db.list_pr_watches()
        assert [r["started_at"] for r in rows] == [100, 200]
        db.close()

    def test_pr_watches_table_added_to_preexisting_db(
        self, tmp_path: Path
    ) -> None:
        """Acceptance: an older state.db lacking the pr_watches table
        must transparently gain it when opened by the new code. The
        existing schema pattern is CREATE TABLE IF NOT EXISTS, so just
        re-opening the DB should add the table without disturbing
        existing rows."""
        import sqlite3

        db_path = tmp_path / "state.db"
        # Simulate a pre-pr_watches DB: create the sessions table only,
        # with an existing session row we want to see survive the open.
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
        # Sanity check: the old DB has no pr_watches table.
        assert conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='pr_watches'"
        ).fetchone() is None
        conn.close()

        # Open with the new StateDB — migration/creation must add the
        # table and not clobber the existing session row.
        db = StateDB(db_path)
        table_names = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "pr_watches" in table_names
        # Existing sessions row intact.
        row = db.execute(
            "SELECT id FROM sessions WHERE id = ?", ("old-session",)
        ).fetchone()
        assert row is not None
        # And the new table works.
        db.add_pr_watch(
            repo="owner/r1", pr_number=1, issue_number=1,
            session_id=None, pr_url=None,
        )
        assert len(db.list_pr_watches()) == 1
        db.close()


class TestPendingResumes:
    """pending_resumes stores BLOCKED sessions + operator answers so a
    Telegram reply arriving after a session has torn down still drives a
    pipeline resume."""

    def test_add_and_fetch_oldest_unanswered(self, tmp_path: Path) -> None:
        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume(
            session_id="secops-1", pipeline="secops",
            repo="owner/r1", question="merge or close?",
        )
        db.add_pending_resume(
            session_id="secops-2", pipeline="secops",
            repo="owner/r2", question="patch or defer?",
        )
        row = db.get_oldest_unanswered_pending_resume()
        assert row is not None
        assert row["session_id"] == "secops-1"
        db.close()

    def test_answered_rows_skipped_by_unanswered_fetch(
        self, tmp_path: Path
    ) -> None:
        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume(
            session_id="s1", pipeline="secops",
            repo="o/r", question="?",
        )
        assert db.answer_pending_resume("s1", "merge #286") is True
        # Already answered — second attempt is a no-op returning False.
        assert db.answer_pending_resume("s1", "second try") is False
        assert db.get_oldest_unanswered_pending_resume() is None
        db.close()

    def test_list_to_execute_returns_answered_not_resumed(
        self, tmp_path: Path
    ) -> None:
        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume("s1", "secops", "o/a", "?")
        db.add_pending_resume("s2", "secops", "o/b", "?")
        db.answer_pending_resume("s1", "merge it")
        # s2 never answered → not in the execute list.
        # s1 answered but not resumed → should appear.
        rows = db.list_pending_resumes_to_execute()
        assert [r["session_id"] for r in rows] == ["s1"]
        db.mark_pending_resume_resumed("s1")
        assert db.list_pending_resumes_to_execute() == []
        db.close()

    def test_add_is_idempotent_refreshes_row(self, tmp_path: Path) -> None:
        """Re-inserting the same session_id (a resume that re-blocks) wipes
        stale answer state so a new Telegram reply routes cleanly."""
        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume("s1", "secops", "o/a", "q1")
        db.answer_pending_resume("s1", "prior answer")
        # Re-register as a fresh BLOCKED (e.g., resume re-blocked).
        db.add_pending_resume("s1", "secops", "o/a", "q2 — need more info")
        row = db.get_oldest_unanswered_pending_resume()
        assert row is not None
        assert row["session_id"] == "s1"
        assert row["question"] == "q2 — need more info"
        assert row["answer"] is None
        db.close()

    def test_mark_resumed_is_noop_on_refreshed_unanswered_row(
        self, tmp_path: Path
    ) -> None:
        """Codex P1: a resume that re-blocks ends the sequence:
           sweeper picks up answered row → pipeline re-blocks →
           add_pending_resume clears answer/answered_at →
           sweeper calls mark_pending_resume_resumed.
        If mark_pending_resume_resumed stamped the freshly-cleared
        row, the NEXT operator reply would set answered_at but leave
        resumed_at populated, hiding the row from
        list_pending_resumes_to_execute forever. Guard on
        answered_at IS NOT NULL makes it a no-op instead."""
        db = StateDB(tmp_path / "state.db")
        db.add_pending_resume("s1", "secops", "o/a", "q1")
        db.answer_pending_resume("s1", "first answer")
        # Pipeline re-blocked during the resume and refreshed the row.
        db.add_pending_resume("s1", "secops", "o/a", "need more info")
        # Sweeper now blindly calls mark_resumed. Must be a no-op.
        marked = db.mark_pending_resume_resumed("s1")
        assert marked is False
        # Row stays visible to the next orphan-reply routing.
        unanswered = db.list_unanswered_pending_resumes()
        assert [r["session_id"] for r in unanswered] == ["s1"]
        assert unanswered[0]["resumed_at"] is None
        # And to the next sweeper tick after the operator answers again.
        db.answer_pending_resume("s1", "second answer")
        pending = db.list_pending_resumes_to_execute()
        assert [r["session_id"] for r in pending] == ["s1"]
        # This time mark_resumed succeeds because answered_at is set.
        assert db.mark_pending_resume_resumed("s1") is True
        assert db.list_pending_resumes_to_execute() == []
        db.close()
