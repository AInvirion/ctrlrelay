"""Tests for retiring BLOCKED questions nobody can act on."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ctrlrelay.core.question_expiry import (
    EXPIRY_REASON_RESOLVED,
    EXPIRY_REASON_TTL,
    expire_stale_questions,
    extract_referenced_numbers,
)
from ctrlrelay.core.state import StateDB

TTL = 172800  # 48h


@pytest.fixture
def db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.db")


def _add(db: StateDB, session_id: str, question: str, age_seconds: int) -> None:
    db.add_pending_resume(
        session_id=session_id,
        pipeline="secops",
        repo="owner/repo",
        question=question,
    )
    db.execute(
        "UPDATE pending_resumes SET created_at = ? WHERE session_id = ?",
        (int(time.time()) - age_seconds, session_id),
    )
    db.commit()


def _state(*, closed: bool, is_pr: bool = True) -> dict[str, object]:
    return {
        "number": 1,
        "state": "closed" if closed else "open",
        "is_pr": is_pr,
        "merged": closed and is_pr,
    }


class TestExtractReferencedNumbers:
    def test_matches_bare_and_prefixed_forms(self) -> None:
        q = "Merged #214. Approve PR #213 and #212?"
        assert extract_referenced_numbers(q) == ["214", "213", "212"]

    def test_ignores_cve_identifiers(self) -> None:
        """A CVE id must not be read as a PR number, or a question about
        an unfixed CVE would look 'resolved' the moment some unrelated
        low-numbered PR closed."""
        q = "pyarrow CVE-2026-25087 (high, needs >=23.0.1) has no PR"
        assert extract_referenced_numbers(q) == []


class TestTTLExpiry:
    @pytest.mark.asyncio
    async def test_expires_questions_past_the_ttl(self, db: StateDB) -> None:
        _add(db, "old", "Approve #1?", age_seconds=TTL + 60)

        expired = await expire_stale_questions(db, None, ttl_seconds=TTL)

        assert [r["session_id"] for r in expired] == ["old"]
        assert expired[0]["expired_reason"] == EXPIRY_REASON_TTL
        assert db.list_unanswered_pending_resumes() == []

    @pytest.mark.asyncio
    async def test_leaves_fresh_questions_alone(self, db: StateDB) -> None:
        _add(db, "fresh", "Approve #1?", age_seconds=TTL - 60)

        assert await expire_stale_questions(db, None, ttl_seconds=TTL) == []
        assert len(db.list_unanswered_pending_resumes()) == 1

    @pytest.mark.asyncio
    async def test_answered_questions_are_never_expired(
        self, db: StateDB
    ) -> None:
        """An answer that landed before the sweep must survive it —
        otherwise the sweeper would discard work the operator did."""
        _add(db, "answered", "Approve #1?", age_seconds=TTL + 60)
        assert db.answer_pending_resume("answered", "yes, merge")

        assert await expire_stale_questions(db, None, ttl_seconds=TTL) == []
        assert [r["session_id"] for r in db.list_pending_resumes_to_execute()] == [
            "answered"
        ]


class TestResolvedElsewhereExpiry:
    @pytest.mark.asyncio
    async def test_expires_when_every_reference_is_closed(
        self, db: StateDB
    ) -> None:
        _add(db, "done", "Approve #12 and #13?", age_seconds=60)
        github = AsyncMock()
        github.get_issue_or_pr_state.return_value = _state(closed=True)

        expired = await expire_stale_questions(db, github, ttl_seconds=TTL)

        assert [r["session_id"] for r in expired] == ["done"]
        assert expired[0]["expired_reason"] == EXPIRY_REASON_RESOLVED
        assert db.list_unanswered_pending_resumes() == []

    @pytest.mark.asyncio
    async def test_keeps_question_when_one_reference_is_open(
        self, db: StateDB
    ) -> None:
        _add(db, "partial", "Approve #12 and #13?", age_seconds=60)
        github = AsyncMock()
        github.get_issue_or_pr_state.side_effect = [
            _state(closed=True),
            _state(closed=False),
        ]

        assert await expire_stale_questions(db, github, ttl_seconds=TTL) == []
        assert len(db.list_unanswered_pending_resumes()) == 1

    @pytest.mark.asyncio
    async def test_keeps_question_when_it_cites_nothing(
        self, db: StateDB
    ) -> None:
        """'Enable Dependabot alerts?' names no PR. Vacuous truth over an
        empty list would expire every such question instantly."""
        _add(db, "no-refs", "Enable Dependabot alerts for this repo?", 60)
        github = AsyncMock()

        assert await expire_stale_questions(db, github, ttl_seconds=TTL) == []
        assert len(db.list_unanswered_pending_resumes()) == 1
        github.get_issue_or_pr_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probe_failure_keeps_the_question(self, db: StateDB) -> None:
        """Fail safe: re-asking a dead question is recoverable, dropping
        a live one is not."""
        from ctrlrelay.core.github import GitHubError

        _add(db, "flaky", "Approve #12?", age_seconds=60)
        github = AsyncMock()
        github.get_issue_or_pr_state.side_effect = GitHubError("api down")

        assert await expire_stale_questions(db, github, ttl_seconds=TTL) == []
        assert len(db.list_unanswered_pending_resumes()) == 1

    @pytest.mark.asyncio
    async def test_ttl_pass_runs_without_github(self, db: StateDB) -> None:
        """A GitHub outage must not stall age-based expiry."""
        _add(db, "old", "Approve #1?", age_seconds=TTL + 60)

        expired = await expire_stale_questions(db, None, ttl_seconds=TTL)

        assert [r["session_id"] for r in expired] == ["old"]


class TestExpiredRowsAreInert:
    @pytest.mark.asyncio
    async def test_expired_question_is_not_routable(self, db: StateDB) -> None:
        """The bridge routes an orphan reply to the oldest unanswered
        question. An expired one must not be that target."""
        _add(db, "old", "Approve #1?", age_seconds=TTL + 60)
        _add(db, "live", "Approve #2?", age_seconds=60)

        await expire_stale_questions(db, None, ttl_seconds=TTL)

        oldest = db.get_oldest_unanswered_pending_resume()
        assert oldest is not None
        assert oldest["session_id"] == "live"

    @pytest.mark.asyncio
    async def test_late_answer_cannot_revive_an_expired_question(
        self, db: StateDB
    ) -> None:
        _add(db, "old", "Approve #1?", age_seconds=TTL + 60)
        await expire_stale_questions(db, None, ttl_seconds=TTL)

        assert db.answer_pending_resume("old", "yes") is False
        assert db.list_pending_resumes_to_execute() == []

    @pytest.mark.asyncio
    async def test_expiry_is_idempotent(self, db: StateDB) -> None:
        _add(db, "old", "Approve #1?", age_seconds=TTL + 60)

        first = await expire_stale_questions(db, None, ttl_seconds=TTL)
        second = await expire_stale_questions(db, None, ttl_seconds=TTL)

        assert len(first) == 1
        assert second == []

    def test_reason_is_persisted(self, db: StateDB) -> None:
        _add(db, "old", "Approve #1?", age_seconds=TTL + 60)
        assert db.expire_pending_resume("old", EXPIRY_REASON_TTL)

        row = db.execute(
            "SELECT expired_at, expired_reason FROM pending_resumes "
            "WHERE session_id = ?",
            ("old",),
        ).fetchone()
        assert row["expired_at"] is not None
        assert row["expired_reason"] == EXPIRY_REASON_TTL


class TestMigration:
    def test_existing_db_without_expiry_columns_is_migrated(
        self, tmp_path: Path
    ) -> None:
        """Real deployments already have a pending_resumes table with 16
        rows in it; CREATE TABLE IF NOT EXISTS won't add the columns."""
        import sqlite3

        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE pending_resumes (
                   session_id TEXT PRIMARY KEY,
                   pipeline TEXT NOT NULL,
                   repo TEXT NOT NULL,
                   question TEXT NOT NULL,
                   created_at INTEGER NOT NULL,
                   answer TEXT,
                   answered_at INTEGER,
                   resumed_at INTEGER
               )"""
        )
        conn.execute(
            "INSERT INTO pending_resumes VALUES "
            "('s1','secops','o/r','Approve #1?',1,NULL,NULL,NULL)"
        )
        conn.commit()
        conn.close()

        db = StateDB(db_path)

        cols = {
            row[1]
            for row in db.execute("PRAGMA table_info(pending_resumes)").fetchall()
        }
        assert {"expired_at", "expired_reason"} <= cols
        # The pre-existing row survives and stays routable.
        assert len(db.list_unanswered_pending_resumes()) == 1
