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


class TestProbeBudget:
    """One resolved-elsewhere pass must not outlive its own cron
    interval. Unbounded it is one serialized `gh api` call per cited
    number: 2000 backlogged rows against a slow GitHub is 8.3h of a job
    that fires hourly."""

    @pytest.mark.asyncio
    async def test_probe_count_is_capped_per_run(self, db: StateDB) -> None:
        from ctrlrelay.core import question_expiry

        cap = question_expiry._MAX_RESOLVED_PROBES_PER_RUN
        for i in range(cap + 25):
            _add(db, f"s{i}", f"Approve #{i + 1}?", age_seconds=60)

        github = AsyncMock()
        github.get_issue_or_pr_state.return_value = _state(closed=False)

        await expire_stale_questions(db, github, ttl_seconds=TTL)

        assert github.get_issue_or_pr_state.await_count <= cap

    @pytest.mark.asyncio
    async def test_deferred_rows_are_still_processed_next_run(
        self, db: StateDB
    ) -> None:
        """Hitting the cap defers work; it must not drop it."""
        from ctrlrelay.core import question_expiry

        cap = question_expiry._MAX_RESOLVED_PROBES_PER_RUN
        for i in range(cap + 5):
            _add(db, f"s{i}", f"Approve #{i + 1}?", age_seconds=60)

        github = AsyncMock()
        github.get_issue_or_pr_state.return_value = _state(closed=True)

        first = await expire_stale_questions(db, github, ttl_seconds=TTL)
        second = await expire_stale_questions(db, github, ttl_seconds=TTL)

        assert len(first) == cap
        assert len(second) == 5
        assert db.list_unanswered_pending_resumes() == []

    @pytest.mark.asyncio
    async def test_rows_citing_nothing_do_not_consume_budget(
        self, db: StateDB
    ) -> None:
        """A repo full of 'Enable Dependabot alerts?' questions must not
        starve the rows that can actually be resolution-expired."""
        for i in range(50):
            _add(db, f"noref{i}", "Enable Dependabot alerts?", age_seconds=60)
        _add(db, "real", "Approve #7?", age_seconds=60)

        github = AsyncMock()
        github.get_issue_or_pr_state.return_value = _state(closed=True)

        expired = await expire_stale_questions(db, github, ttl_seconds=TTL)

        assert [r["session_id"] for r in expired] == ["real"]
        assert github.get_issue_or_pr_state.await_count == 1

    @pytest.mark.asyncio
    async def test_a_single_row_cannot_overshoot_the_cap(
        self, db: StateDB
    ) -> None:
        """Checking `probes_used` alone lets one row blow past the cap:
        a question citing 300 numbers passes `0 >= 200` and then issues
        all 300 calls. The budget must be tested against what the row
        will cost, before any of it is spent."""
        from ctrlrelay.core import question_expiry

        cap = question_expiry._MAX_RESOLVED_PROBES_PER_RUN
        many = " ".join(f"#{i}" for i in range(1, cap))  # just under cap
        _add(db, "wide", f"Approve {many}?", age_seconds=60)
        _add(db, "also-wide", f"Approve {many}?", age_seconds=60)

        github = AsyncMock()
        github.get_issue_or_pr_state.return_value = _state(closed=False)

        await expire_stale_questions(db, github, ttl_seconds=TTL)

        assert github.get_issue_or_pr_state.await_count <= cap

    @pytest.mark.asyncio
    async def test_oversized_row_is_skipped_not_blocking(
        self, db: StateDB
    ) -> None:
        """A row citing more numbers than a whole run's budget can never
        be resolution-checked — expiry needs every reference closed. It
        must be skipped, not allowed to starve every row behind it."""
        from ctrlrelay.core import question_expiry

        cap = question_expiry._MAX_RESOLVED_PROBES_PER_RUN
        huge = " ".join(f"#{i}" for i in range(1, cap + 50))
        _add(db, "huge", f"Approve {huge}?", age_seconds=60)
        _add(db, "normal", "Approve #7?", age_seconds=60)

        github = AsyncMock()
        github.get_issue_or_pr_state.return_value = _state(closed=True)

        expired = await expire_stale_questions(db, github, ttl_seconds=TTL)

        # The oversized row is left alone; the one behind it still ran.
        assert [r["session_id"] for r in expired] == ["normal"]
        assert github.get_issue_or_pr_state.await_count == 1

    @pytest.mark.asyncio
    async def test_oversized_row_still_expires_on_age(
        self, db: StateDB
    ) -> None:
        """Skipping it for resolution must not make it immortal."""
        from ctrlrelay.core import question_expiry

        cap = question_expiry._MAX_RESOLVED_PROBES_PER_RUN
        huge = " ".join(f"#{i}" for i in range(1, cap + 50))
        _add(db, "huge", f"Approve {huge}?", age_seconds=TTL + 60)

        github = AsyncMock()
        expired = await expire_stale_questions(db, github, ttl_seconds=TTL)

        assert [r["session_id"] for r in expired] == ["huge"]
        assert expired[0]["expired_reason"] == EXPIRY_REASON_TTL


class TestReplyDuringSweepWins:
    """The sweep enumerates rows, then expires them one at a time, so a
    reply can land in between. The expiry UPDATE is guarded on
    `answered_at IS NULL`, so the reply wins that race — an operator
    answer is never traded for an expiry."""

    @pytest.mark.asyncio
    async def test_reply_between_enumeration_and_expiry_is_kept(
        self, db: StateDB
    ) -> None:
        _add(db, "racing", "Approve #12?", age_seconds=60)

        answered: list[str] = []

        async def _reply_mid_sweep(*_a: object, **_kw: object) -> dict[str, object]:
            # Fires while the sweep is probing this very row.
            if not answered:
                db.answer_pending_resume("racing", "yes, merge it")
                answered.append("racing")
            return _state(closed=True)

        github = AsyncMock()
        github.get_issue_or_pr_state.side_effect = _reply_mid_sweep

        expired = await expire_stale_questions(db, github, ttl_seconds=TTL)

        # Every reference read as closed, so the sweep tried to expire —
        # and the guard refused because the answer had landed.
        assert answered == ["racing"]
        assert expired == []
        assert [r["session_id"] for r in db.list_pending_resumes_to_execute()] == [
            "racing"
        ]


class TestExpiredRowsNeverResume:
    """`answer_pending_resume` refuses expired rows, but that guard only
    holds while every process runs the same code. The bridge writes the
    answer and the poller reads it back — separate daemons, so a deploy
    that restarts one before the other leaves a window where an old
    bridge stamps `answered_at` on a row the new poller already expired.
    Observed live: the bridge ran three-day-old code for an hour after
    the poller was upgraded."""

    def test_expired_row_answered_out_of_band_is_not_executed(
        self, db: StateDB
    ) -> None:
        _add(db, "s1", "Approve #1?", age_seconds=TTL + 60)
        assert db.expire_pending_resume("s1", EXPIRY_REASON_TTL)

        # An old bridge, unaware of expiry, writes the answer directly.
        db.execute(
            "UPDATE pending_resumes SET answer = ?, answered_at = ? "
            "WHERE session_id = ?",
            ("yes, merge it", 1, "s1"),
        )
        db.commit()

        assert db.list_pending_resumes_to_execute() == []

    def test_live_answered_rows_still_execute(self, db: StateDB) -> None:
        """The filter must not swallow ordinary work."""
        _add(db, "live", "Approve #2?", age_seconds=60)
        assert db.answer_pending_resume("live", "yes")

        assert [r["session_id"] for r in db.list_pending_resumes_to_execute()] == [
            "live"
        ]


# --- #200: a question re-asked every morning, unmarked and never pruned ---
#
# Questions below are real rows from one deployment's pending_resumes
# (AInvirion/ai-miner, 26-29 Oct 2026): the same approval, reworded by the
# agent each run, citing #168 on some days and not on others.
MINER = "AInvirion/ai-miner"
MINER_Q = [
    "ai-miner is not a self-merge repo (repo rules, merge authority: work "
    "stops at the PR), so I did not auto-merge the patch tier. Approve "
    "merging #201 ruff 0.17.1->0.17.2 (patch, all 7 checks green)? Prior "
    "ruff approval was #168 0.16.8->0.16.10, a different bump, so I am "
    "re-asking.",
    "ai-miner is not a self-merge repo (repo rules, merge authority: work "
    "stops at the PR), so I did not auto-merge the patch tier. Approve "
    "merging #201 ruff 0.17.1->0.17.2 (patch, all 7 checks green, "
    "MERGEABLE/CLEAN)?",
    "ai-miner is not a self-merge repo (repo rules, merge authority: work "
    "stops at the PR), so I did not auto-merge the patch tier. Approve "
    "merging #201 ruff 0.17.1->0.17.2 (patch, all 7 checks green)? Prior "
    "ruff approval was a different range (0.16.8->0.16.10 on 2026-10-06), "
    "so re-asking.",
]
DAY = 86400
# Noon UTC on 28 Oct 2026, so the calendar date is 28 Oct in any zone
# within twelve hours of UTC.
OCT_28_NOON = 1793188800


def _row(
    db: StateDB,
    session_id: str,
    question: str,
    *,
    created_at: int,
    repo: str = MINER,
    answered_at: int | None = None,
    resumed_at: int | None = None,
    expired_at: int | None = None,
    expired_reason: str | None = None,
) -> None:
    """Insert through the real writer, then stamp every timestamp the
    test depends on rather than inheriting `time.time()`."""
    db.add_pending_resume(
        session_id=session_id, pipeline="secops", repo=repo, question=question,
    )
    db.execute(
        "UPDATE pending_resumes SET created_at = ?, answered_at = ?, "
        "answer = ?, resumed_at = ?, expired_at = ?, expired_reason = ? "
        "WHERE session_id = ?",
        (
            created_at,
            answered_at,
            "yes" if answered_at is not None else None,
            resumed_at,
            expired_at,
            expired_reason,
            session_id,
        ),
    )
    db.commit()


class TestSameSubstance:
    def test_reworded_question_citing_the_same_pr_matches(self) -> None:
        from ctrlrelay.core.question_expiry import same_substance

        # #168 is in the first and not the second: one shared number is
        # enough, which is what makes this chain one question.
        assert same_substance(MINER_Q[0], MINER_Q[1])
        assert same_substance(MINER_Q[1], MINER_Q[2])

    def test_different_prs_do_not_match(self) -> None:
        from ctrlrelay.core.question_expiry import same_substance

        assert not same_substance(
            MINER_Q[1], MINER_Q[1].replace("#201", "#197")
        )

    def test_without_numbers_the_normalised_text_decides(self) -> None:
        from ctrlrelay.core.question_expiry import same_substance

        assert same_substance(
            "Grant merge authority here?", "  grant MERGE authority   here "
        )
        assert not same_substance(
            "Grant merge authority here?", "Leave it for Diego?"
        )

    def test_numbers_on_one_side_only_is_not_a_match(self) -> None:
        from ctrlrelay.core.question_expiry import same_substance

        assert not same_substance("Approve #201?", "Approve it?")


class TestRepeatHistory:
    def _history(self, db: StateDB, question: str, session: str = "now"):
        from ctrlrelay.core.question_expiry import find_repeat_history

        return find_repeat_history(
            db, repo=MINER, question=question, exclude_session_id=session,
        )

    def test_a_first_ask_is_not_a_repeat(self, db: StateDB) -> None:
        # Unrelated history in the same repo, and the same PR number in
        # another repo, so "None" is not just an empty table talking.
        _row(db, "other-pr", MINER_Q[1].replace("#201", "#197"),
             created_at=OCT_28_NOON - 3 * DAY,
             expired_at=OCT_28_NOON - DAY, expired_reason=EXPIRY_REASON_TTL)
        _row(db, "other-repo", MINER_Q[1], repo="AInvirion/zzsites",
             created_at=OCT_28_NOON - 3 * DAY,
             expired_at=OCT_28_NOON - DAY, expired_reason=EXPIRY_REASON_TTL)

        assert self._history(db, MINER_Q[2]) is None

    def test_counts_every_unanswered_earlier_ask(self, db: StateDB) -> None:
        """TTL-expired, resolved-elsewhere and still-open rows are all
        asks nobody answered. Unrelated rows are not counted."""
        _row(db, "d1", MINER_Q[0], created_at=OCT_28_NOON - 2 * DAY,
             expired_at=OCT_28_NOON, expired_reason=EXPIRY_REASON_TTL)
        _row(db, "d2", MINER_Q[1], created_at=OCT_28_NOON - DAY,
             expired_at=OCT_28_NOON, expired_reason=EXPIRY_REASON_RESOLVED)
        _row(db, "d3", MINER_Q[2], created_at=OCT_28_NOON)  # still open
        _row(db, "other-pr", MINER_Q[1].replace("#201", "#197"),
             created_at=OCT_28_NOON + 100)
        _row(db, "other-repo", MINER_Q[1], repo="AInvirion/zzsites",
             created_at=OCT_28_NOON + 200)

        h = self._history(db, MINER_Q[1])

        assert h is not None
        assert h.times_asked_before == 3
        assert h.last_asked_at == OCT_28_NOON

    def test_an_answer_resets_the_count(self, db: StateDB) -> None:
        """The cutoff is when the answer was given. Here the operator
        answers day -4's question on day -2, AFTER day -3 re-asked it, so
        day -3's ask is answered too and only day 0's counts."""
        _row(db, "old1", MINER_Q[0], created_at=OCT_28_NOON - 5 * DAY,
             expired_at=OCT_28_NOON - 3 * DAY,
             expired_reason=EXPIRY_REASON_TTL)
        _row(db, "answered", MINER_Q[1], created_at=OCT_28_NOON - 4 * DAY,
             answered_at=OCT_28_NOON - 2 * DAY,
             resumed_at=OCT_28_NOON - 2 * DAY + 120)
        _row(db, "before-answer", MINER_Q[0], created_at=OCT_28_NOON - 3 * DAY,
             expired_at=OCT_28_NOON - DAY, expired_reason=EXPIRY_REASON_TTL)
        _row(db, "after", MINER_Q[2], created_at=OCT_28_NOON,
             expired_at=OCT_28_NOON + DAY, expired_reason=EXPIRY_REASON_TTL)

        h = self._history(db, MINER_Q[1])

        assert h is not None
        assert h.times_asked_before == 1
        assert h.last_asked_at == OCT_28_NOON

    def test_the_asking_session_is_not_its_own_repeat(
        self, db: StateDB,
    ) -> None:
        """A resume that re-blocks rewrites the session's own row before
        the next ask; that row is this question, not an earlier one."""
        _row(db, "d1", MINER_Q[0], created_at=OCT_28_NOON - DAY,
             expired_at=OCT_28_NOON, expired_reason=EXPIRY_REASON_TTL)
        _row(db, "now", MINER_Q[1], created_at=OCT_28_NOON)

        h = self._history(db, MINER_Q[1], session="now")

        assert h is not None
        assert h.times_asked_before == 1
        assert h.last_asked_at == OCT_28_NOON - DAY

    def test_an_answer_on_the_asking_sessions_own_row_resets_it(
        self, db: StateDB,
    ) -> None:
        """The dev/task resume path asks again before it rewrites its
        row, so the answer that drove the resume is on that row when the
        re-ask is posted. It is the latest answer; nothing before it is
        a repeat."""
        _row(db, "d1", MINER_Q[0], created_at=OCT_28_NOON - 2 * DAY,
             expired_at=OCT_28_NOON, expired_reason=EXPIRY_REASON_TTL)
        _row(db, "now", MINER_Q[1], created_at=OCT_28_NOON - DAY,
             answered_at=OCT_28_NOON)

        assert self._history(db, MINER_Q[2], session="now") is None

    def test_the_month_does_not_follow_the_locale(self, monkeypatch) -> None:
        """`%b` follows the process locale. No non-English locale is
        installed on our hosts, so a real setlocale() would only ever
        skip; instead the module's datetime is swapped for one whose
        `%b` answers as a German host would, which is exactly what the
        C library does under de_DE."""
        from datetime import datetime

        import ctrlrelay.core.question_expiry as qe

        class GermanDatetime(datetime):
            def strftime(self, fmt: str) -> str:
                return super().strftime(fmt.replace("%b", "Dez"))

            def __format__(self, fmt: str) -> str:
                return self.strftime(fmt) if fmt else str(self)

        monkeypatch.setattr(qe, "datetime", GermanDatetime)
        dec = 1796299200  # 3 Dec 2026, noon UTC
        # Prove the stand-in bites: `%b` through it is no longer English.
        assert f"{GermanDatetime.fromtimestamp(dec):%b}" == "Dez"

        note = qe.format_repeat_note(
            qe.RepeatHistory(times_asked_before=1, last_asked_at=dec)
        )

        assert note == (
            "Repeat: asked once before, last on Dec 3 UTC, no answer yet."
        )

    def test_the_note_names_the_count_and_the_date(self) -> None:
        from ctrlrelay.core.question_expiry import (
            RepeatHistory,
            format_repeat_note,
        )

        assert format_repeat_note(
            RepeatHistory(times_asked_before=6, last_asked_at=OCT_28_NOON)
        ) == "Repeat: asked 6 times before, last on Oct 28 UTC, no answer yet."
        assert format_repeat_note(
            RepeatHistory(times_asked_before=1, last_asked_at=OCT_28_NOON)
        ) == "Repeat: asked once before, last on Oct 28 UTC, no answer yet."

    def test_the_date_is_utc_whatever_the_host_zone(self, monkeypatch) -> None:
        """02:00 UTC on 29 Oct is still 28 Oct on a Pacific host. The note
        says UTC, so it must be the UTC date."""
        from ctrlrelay.core.question_expiry import (
            RepeatHistory,
            format_repeat_note,
        )

        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        try:
            # Prove the zone took: without zone data TZ silently falls
            # back to UTC and this test would pass for any implementation.
            if time.localtime(OCT_28_NOON + 14 * 3600).tm_mday != 28:
                pytest.skip("America/Los_Angeles zone data not available")
            note = format_repeat_note(RepeatHistory(
                times_asked_before=2, last_asked_at=OCT_28_NOON + 14 * 3600,
            ))
        finally:
            monkeypatch.undo()
            time.tzset()

        assert note == (
            "Repeat: asked 2 times before, last on Oct 29 UTC, no answer yet."
        )


class TestRetention:
    # The real clock, not a fixed date: the TTL pass stamps `expired_at`
    # with time.time(), and a row it retires must read as recent.
    NOW = int(time.time())

    def _seed(self, db: StateDB) -> None:
        old = self.NOW - 120 * DAY
        _row(db, "old-ttl", "Approve #1?", created_at=old,
             expired_at=old + 2 * DAY, expired_reason=EXPIRY_REASON_TTL)
        _row(db, "old-resumed", "Approve #2?", created_at=old,
             answered_at=old + 60, resumed_at=old + 120)
        # Finished, but only recently: asked long ago, retired last week.
        _row(db, "late-finish", "Approve #3?", created_at=old,
             expired_at=self.NOW - 7 * DAY,
             expired_reason=EXPIRY_REASON_RESOLVED)
        _row(db, "recent-ttl", "Approve #4?", created_at=self.NOW - 10 * DAY,
             expired_at=self.NOW - 8 * DAY, expired_reason=EXPIRY_REASON_TTL)
        # Not finished, however old: an open question, and an answered one
        # the sweeper has not resumed yet.
        _row(db, "old-open", "Approve #5?", created_at=old)
        _row(db, "old-answered", "Approve #6?", created_at=old,
             answered_at=old + 60)

    def _ids(self, db: StateDB) -> set[str]:
        return {
            r["session_id"]
            for r in db.execute("SELECT session_id FROM pending_resumes")
        }

    def test_prunes_only_finished_rows_past_the_window(
        self, db: StateDB,
    ) -> None:
        self._seed(db)

        deleted = db.prune_finished_pending_resumes(self.NOW - 90 * DAY)

        assert deleted == 2
        assert self._ids(db) == {
            "late-finish", "recent-ttl", "old-open", "old-answered",
        }

    @pytest.mark.asyncio
    async def test_the_hourly_sweep_prunes_without_github(
        self, db: StateDB,
    ) -> None:
        """Through the function the scheduler actually calls, with the
        network pass disabled: retention must not depend on GitHub."""
        self._seed(db)

        await expire_stale_questions(
            db, None, ttl_seconds=TTL, retention_days=90, now=self.NOW,
        )

        ids = self._ids(db)
        assert "old-ttl" not in ids
        assert "old-resumed" not in ids
        assert {"late-finish", "recent-ttl", "old-answered"} <= ids
        # The open row is retired on TTL by the same sweep (expired_at is
        # stamped now), which makes it recent, so it is kept.
        assert "old-open" in ids

    @pytest.mark.asyncio
    async def test_the_window_is_the_callers(self, db: StateDB) -> None:
        self._seed(db)

        await expire_stale_questions(
            db, None, ttl_seconds=TTL, retention_days=5, now=self.NOW,
        )

        assert "recent-ttl" not in self._ids(db)
        assert "late-finish" not in self._ids(db)

    def test_retention_defaults_to_ninety_days_on_both_transports(
        self,
    ) -> None:
        from ctrlrelay.core.config import MattermostConfig, TelegramConfig

        assert TelegramConfig().question_retention_days == 90
        assert MattermostConfig(url="https://x").question_retention_days == 90
