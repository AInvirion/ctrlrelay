"""#214: a question nobody answers is held back on a widening schedule
instead of being re-posted by every morning's sweep, and stays answerable
while it is held."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.question_expiry import (
    EXPIRY_REASON_RESOLVED,
    EXPIRY_REASON_TTL,
    expire_stale_questions,
    find_question_hold,
    find_repeat_history,
)
from ctrlrelay.core.state import StateDB
from tests.test_question_expiry import MINER, MINER_Q

DAY = 86400
HOUR = 3600
TTL = 2 * DAY
# The question the agent asks today, in the wording of the real chain.
TODAY_Q = MINER_Q[1]


@pytest.fixture
def db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.db")


def _posted(
    db: StateDB,
    session_id: str,
    question: str,
    *,
    created_at: int,
    expired: str | None = None,
    answered_at: int | None = None,
    held_until: int | None = None,
) -> None:
    """A row as the secops lane leaves it: written through the real
    writer after the ask, then stamped with the times the test needs.
    An expired row's expiry is stamped TTL after its creation, as the
    hourly sweep does."""
    db.add_pending_resume(
        session_id=session_id, pipeline="secops", repo=MINER,
        question=question, held_until=held_until,
    )
    db.execute(
        "UPDATE pending_resumes SET created_at = ?, answered_at = ?, "
        "answer = ?, expired_at = ?, expired_reason = ? "
        "WHERE session_id = ?",
        (
            created_at,
            answered_at,
            "yes" if answered_at is not None else None,
            created_at + TTL if expired else None,
            expired,
            session_id,
        ),
    )
    db.commit()


def _hold(db: StateDB, now: int, session: str = "now"):
    return find_question_hold(
        db, repo=MINER, question=TODAY_Q, exclude_session_id=session,
        now=now,
    )


class TestSchedule:
    """1 day after the first unanswered post, then 2, then 4, then a week
    from there on. "now" is a day boundary plus a little, the way a daily
    cron lands."""

    T0 = 1793188800  # 28 Oct 2026 12:00 UTC

    def _chain(self, db: StateDB, posts: int) -> int:
        """`posts` posts a day apart, all expired unanswered. Returns the
        last post's time."""
        for i in range(posts):
            _posted(
                db, f"d{i}", MINER_Q[i % 3],
                created_at=self.T0 + i * DAY, expired=EXPIRY_REASON_TTL,
            )
        return self.T0 + (posts - 1) * DAY

    @pytest.mark.parametrize(
        "jitter", [0, -40 * 60, 40 * 60], ids=["on-time", "early", "late"],
    )
    @pytest.mark.asyncio
    async def test_a_daily_sweep_posts_on_days_0_1_3_7_14_21(
        self, db: StateDB, jitter: int,
    ) -> None:
        """Every morning's sweep reaches the same question, nobody ever
        answers, and the hourly expiry runs in between. The question is
        posted on these days and held on the rest. `jitter` moves every
        other morning's sweep earlier or later, as an agent's run length
        does."""
        posted_days = []
        for day in range(23):
            now = self.T0 + day * DAY + (jitter if day % 2 else 0)
            await expire_stale_questions(db, None, ttl_seconds=TTL, now=now)
            session = f"secops-ai-miner-{day}"
            hold = find_question_hold(
                db, repo=MINER, question=MINER_Q[day % 3],
                exclude_session_id=session, now=now,
            )
            if hold is None:
                posted_days.append(day)
            # The session's row, written after the ask or the hold.
            _posted(
                db, session, MINER_Q[day % 3], created_at=now,
                held_until=None if hold is None else hold.next_post_at,
            )

        assert posted_days == [0, 1, 3, 7, 14, 21]

    @pytest.mark.parametrize(
        ("posts", "days"), [(1, 1), (2, 2), (3, 4), (4, 7), (9, 7)],
    )
    def test_held_until_the_delay_then_posted(
        self, db: StateDB, posts: int, days: int,
    ) -> None:
        last = self._chain(db, posts)

        # The day before it is due: held, and it says when.
        if days > 1:
            hold = _hold(db, now=last + (days - 1) * DAY + 10 * 60)
            assert hold is not None
            assert hold.missed_posts == posts
            assert hold.last_posted_at == last
            assert hold.next_post_at > last + (days - 1) * DAY
            assert hold.next_post_at <= last + days * DAY
        # The sweep that lands on the due day, a few minutes EARLIER in
        # the day than the last post was stamped: posted.
        assert _hold(db, now=last + days * DAY - 20 * 60) is None

    def test_a_first_ask_is_never_held(self, db: StateDB) -> None:
        assert _hold(db, now=self.T0) is None

    def test_a_post_still_open_is_a_missed_answer(self, db: StateDB) -> None:
        """Day 0 and day 1 were posted and are still open, inside the
        48h TTL. Nobody answered either: on day 2 that is two misses,
        so the next post waits 2 days from day 1."""
        _posted(db, "d0", MINER_Q[0], created_at=self.T0)
        _posted(db, "d1", MINER_Q[2], created_at=self.T0 + DAY)

        hold = _hold(db, now=self.T0 + 2 * DAY)
        assert hold is not None
        assert hold.missed_posts == 2
        assert hold.last_posted_at == self.T0 + DAY

    def test_a_post_younger_than_the_slack_is_not_yet_missed(
        self, db: StateDB,
    ) -> None:
        """Posted two hours ago: nobody has had the time to answer."""
        _posted(db, "d0", MINER_Q[0], created_at=self.T0)

        assert _hold(db, now=self.T0 + 2 * HOUR) is None

    def test_the_asking_sessions_own_row_never_counts(
        self, db: StateDB,
    ) -> None:
        """A row for the session asking now, open and old, is this very
        question, not an earlier post of it."""
        _posted(db, "now", MINER_Q[0], created_at=self.T0)

        # Inside the 1-day wait: another session would be held by it,
        # the session that owns it is not.
        assert _hold(db, now=self.T0 + 12 * HOUR, session="other") is not None
        assert _hold(db, now=self.T0 + 12 * HOUR, session="now") is None

    def test_resolved_elsewhere_is_not_a_missed_answer(
        self, db: StateDB,
    ) -> None:
        _posted(db, "d0", MINER_Q[0], created_at=self.T0,
                expired=EXPIRY_REASON_RESOLVED)

        # Inside the 1-day wait a miss would hold it; this is not one.
        assert _hold(db, now=self.T0 + 12 * HOUR) is None

    def test_the_delay_runs_from_the_latest_post(self, db: StateDB) -> None:
        """Two expiries, then a third post still open: three misses, and
        the 4-day wait is counted from that newest post."""
        last = self._chain(db, 2)
        _posted(db, "open", MINER_Q[2], created_at=last + 2 * DAY)

        hold = _hold(db, now=last + 5 * DAY)
        assert hold is not None
        assert hold.missed_posts == 3
        assert hold.last_posted_at == last + 2 * DAY
        assert _hold(db, now=last + 6 * DAY - 20 * 60) is None

    def test_a_held_row_does_not_extend_the_hold(self, db: StateDB) -> None:
        """Held rows were never posted. If they counted as posts, each
        day's hold would push the next post back a day, forever."""
        last = self._chain(db, 3)  # 4-day wait
        for i in (1, 2, 3):
            _posted(
                db, f"held{i}", MINER_Q[i % 3],
                created_at=last + i * DAY, held_until=last + 4 * DAY,
                expired=EXPIRY_REASON_TTL if i == 1 else None,
            )

        assert _hold(db, now=last + 4 * DAY - 20 * 60) is None

    def test_other_questions_and_repos_do_not_hold(self, db: StateDB) -> None:
        for i in range(4):
            _posted(
                db, f"other{i}", MINER_Q[1].replace("#201", "#197"),
                created_at=self.T0 + i * DAY, expired=EXPIRY_REASON_TTL,
            )
        db.add_pending_resume(
            session_id="zz", pipeline="secops", repo="AInvirion/zzsites",
            question=TODAY_Q,
        )

        assert _hold(db, now=self.T0 + 4 * DAY) is None


class TestAnAnswerResets:
    T0 = TestSchedule.T0

    def test_an_answer_on_the_asking_sessions_own_row_resets(
        self, db: StateDB,
    ) -> None:
        """The resume path asks again before it rewrites its row, so
        the answer that drove the resume is on that row. Nothing before
        it is a miss."""
        for i in range(3):
            _posted(db, f"d{i}", MINER_Q[i], created_at=self.T0 + i * DAY,
                    expired=EXPIRY_REASON_TTL)
        _posted(db, "now", MINER_Q[0], created_at=self.T0 + 3 * DAY,
                answered_at=self.T0 + 3 * DAY + HOUR)

        assert _hold(db, now=self.T0 + 3 * DAY + 2 * HOUR,
                     session="now") is None

    def test_an_answer_resets_the_schedule(self, db: StateDB) -> None:
        for i in range(3):
            _posted(db, f"d{i}", MINER_Q[i], created_at=self.T0 + i * DAY,
                    expired=EXPIRY_REASON_TTL)
        _posted(db, "answered", MINER_Q[0], created_at=self.T0 + 3 * DAY,
                answered_at=self.T0 + 3 * DAY + HOUR)

        assert _hold(db, now=self.T0 + 4 * DAY) is None

    def test_an_answer_survives_the_re_block_that_rewrites_its_row(
        self, db: StateDB,
    ) -> None:
        """The resume re-blocks and rewrites the row, clearing
        answered_at. The answer still happened, and still resets."""
        for i in range(3):
            _posted(db, f"d{i}", MINER_Q[i], created_at=self.T0 + i * DAY,
                    expired=EXPIRY_REASON_TTL)
        _posted(db, "answered", MINER_Q[0], created_at=self.T0 + 3 * DAY,
                answered_at=self.T0 + 3 * DAY + HOUR)

        # The resume's re-block: same session, a new question row.
        db.add_pending_resume(
            session_id="answered", pipeline="secops", repo=MINER,
            question=MINER_Q[2],
        )
        db.execute(
            "UPDATE pending_resumes SET created_at = ? WHERE session_id = ?",
            (self.T0 + 3 * DAY + 2 * HOUR, "answered"),
        )
        db.commit()

        row = db.get_pending_resume("answered")
        assert row["answered_at"] is None, "the row must take a new answer"
        assert row["last_answered_at"] == self.T0 + 3 * DAY + HOUR
        # Neither the backoff nor the repeat note counts the asks the
        # operator answered.
        assert _hold(db, now=self.T0 + 4 * DAY) is None
        assert find_repeat_history(
            db, repo=MINER, question=TODAY_Q, exclude_session_id="now",
        ).times_asked_before == 1  # the re-block's own open ask

    def test_the_rewrite_still_clears_everything_else(
        self, db: StateDB,
    ) -> None:
        """What INSERT OR REPLACE used to clear, the upsert clears too:
        a stale expiry or hold on a re-blocked row would hide it."""
        _posted(db, "s", MINER_Q[0], created_at=self.T0,
                expired=EXPIRY_REASON_TTL, held_until=self.T0 + DAY)
        db.execute(
            "UPDATE pending_resumes SET resumed_at = ? WHERE session_id = 's'",
            (self.T0 + 5,),
        )
        db.commit()

        db.add_pending_resume(
            session_id="s", pipeline="secops", repo=MINER, question="new?",
        )

        row = db.get_pending_resume("s")
        assert row["question"] == "new?"
        for col in ("answer", "answered_at", "resumed_at", "expired_at",
                    "expired_reason", "held_until"):
            assert row[col] is None, col
        assert [r["session_id"] for r in db.list_unanswered_pending_resumes()] \
            == ["s"]


class TestMigration:
    def test_an_existing_table_gains_the_columns(self, tmp_path: Path) -> None:
        import sqlite3

        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE pending_resumes (
                   session_id TEXT PRIMARY KEY, pipeline TEXT NOT NULL,
                   repo TEXT NOT NULL, question TEXT NOT NULL,
                   created_at INTEGER NOT NULL, answer TEXT,
                   answered_at INTEGER, resumed_at INTEGER,
                   expired_at INTEGER, expired_reason TEXT
               )"""
        )
        conn.execute(
            "INSERT INTO pending_resumes VALUES "
            "('s1','secops','o/r','Approve #1?',1,'yes',2,NULL,NULL,NULL)"
        )
        conn.commit()
        conn.close()

        db = StateDB(path)
        db.add_pending_resume(
            session_id="s1", pipeline="secops", repo="o/r", question="#1 again?",
        )

        assert db.get_pending_resume("s1")["last_answered_at"] == 2


# --- the secops lane, end to end through run_secops_all ---------------


def _blocked(question: str):
    from ctrlrelay.core.checkpoint import CheckpointStatus

    state = MagicMock()
    state.status = CheckpointStatus.BLOCKED_NEEDS_INPUT
    state.question = question
    state.summary = None
    state.outputs = {}
    state.error = None
    return state


async def _sweep(
    tmp_path: Path, db, transport, *, hold_repeats: bool = True,
    states=None,
):
    from ctrlrelay.core.dispatcher import SessionResult
    from ctrlrelay.pipelines.secops import run_secops_all

    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = [
        SessionResult(session_id="agent", exit_code=0, state=s)
        for s in (states or [_blocked(TODAY_Q)])
    ]
    worktree = AsyncMock()
    worktree.create_worktree.return_value = tmp_path / "wt"
    worktree.ensure_bare_repo.return_value = tmp_path / "bare"
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()
    repo = MagicMock()
    repo.name = MINER
    repo.automation = None
    results = await run_secops_all(
        repos=[repo],
        dispatcher=dispatcher,
        github=MagicMock(),
        worktree=worktree,
        dashboard=None,
        state_db=db,
        transport=transport,
        contexts_dir=tmp_path / "contexts",
        hold_repeats=hold_repeats,
    )
    assert len(results) == 1
    return results[0]


def _ttl_expired_chain(db: StateDB, n: int, *, last_posted: int) -> None:
    for i in range(n):
        _posted(db, f"d{i}", MINER_Q[i % 3],
                created_at=last_posted - (n - 1 - i) * DAY,
                expired=EXPIRY_REASON_TTL)


class TestTheSweepHoldsARepeat:
    @pytest.mark.asyncio
    async def test_held_then_posted_on_schedule(
        self, tmp_path: Path, db: StateDB,
    ) -> None:
        from ctrlrelay.pipelines.secops import QUESTION_HELD_UNTIL

        now = int(time.time())
        # Two missed answers, the last post a day ago: due in 2 days.
        _ttl_expired_chain(db, 2, last_posted=now - DAY)
        transport = AsyncMock()
        transport.ask.side_effect = TimeoutError("nobody answered")

        held = await _sweep(tmp_path, db, transport)

        transport.ask.assert_not_called()
        assert held.blocked and not held.success
        assert held.outputs[QUESTION_HELD_UNTIL] > now
        row = db.get_pending_resume(held.session_id)
        assert row is not None, "a held session must still be persisted"
        assert row["held_until"] == held.outputs[QUESTION_HELD_UNTIL]
        assert row["answered_at"] is None and row["expired_at"] is None
        status = db.execute(
            "SELECT status FROM sessions WHERE id = ?", (held.session_id,),
        ).fetchone()[0]
        assert status == "blocked"

        # Two days on (the held row expired meanwhile): due, so posted.
        db.execute(
            "UPDATE pending_resumes SET created_at = created_at - ?",
            (2 * DAY,),
        )
        db.commit()
        await expire_stale_questions(db, None, ttl_seconds=TTL)

        posted = await _sweep(tmp_path, db, transport)

        transport.ask.assert_awaited_once()
        assert QUESTION_HELD_UNTIL not in posted.outputs
        assert db.get_pending_resume(posted.session_id)["held_until"] is None

    @pytest.mark.asyncio
    async def test_a_held_question_is_answerable_and_still_expires(
        self, tmp_path: Path, db: StateDB,
    ) -> None:
        """Held is blocked-and-waiting, the same as nobody answering:
        an answer resumes it, and with no answer it expires on TTL."""
        now = int(time.time())
        _ttl_expired_chain(db, 3, last_posted=now - DAY)
        transport = AsyncMock()

        a = await _sweep(tmp_path, db, transport)
        b = await _sweep(tmp_path, db, transport)
        transport.ask.assert_not_called()

        assert db.answer_pending_resume(a.session_id, "yes, merge") is True
        assert [r["session_id"] for r in db.list_pending_resumes_to_execute()] \
            == [a.session_id]

        db.execute(
            "UPDATE pending_resumes SET created_at = ? WHERE session_id = ?",
            (now - TTL - 60, b.session_id),
        )
        db.commit()
        expired = await expire_stale_questions(db, None, ttl_seconds=TTL)
        assert b.session_id in [r["session_id"] for r in expired]
        assert db.get_pending_resume(b.session_id)["expired_reason"] == \
            EXPIRY_REASON_TTL

    @pytest.mark.asyncio
    async def test_a_lookup_failure_posts_the_question(
        self, tmp_path: Path, db: StateDB,
    ) -> None:
        now = int(time.time())
        _ttl_expired_chain(db, 3, last_posted=now - DAY)
        db.list_pending_resumes_for_repo = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("database is locked"),
        )
        transport = AsyncMock()
        transport.ask.side_effect = TimeoutError("nobody answered")

        result = await _sweep(tmp_path, db, transport)

        transport.ask.assert_awaited_once()
        assert db.get_pending_resume(result.session_id)["held_until"] is None

    @pytest.mark.asyncio
    async def test_a_run_started_by_hand_is_never_held(
        self, tmp_path: Path, db: StateDB,
    ) -> None:
        now = int(time.time())
        _ttl_expired_chain(db, 3, last_posted=now - DAY)
        transport = AsyncMock()
        transport.ask.side_effect = TimeoutError("nobody answered")

        await _sweep(tmp_path, db, transport, hold_repeats=False)

        transport.ask.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_follow_up_after_an_inline_answer_is_asked(
        self, tmp_path: Path, db: StateDB,
    ) -> None:
        """The operator is answering in-session, and the agent's
        follow-up is a question with a held history. They are engaged
        right now; the follow-up is asked, not held. (The first question
        is a new one, so it is posted either way.)"""
        from ctrlrelay.core.checkpoint import CheckpointStatus

        now = int(time.time())
        # TODAY_Q alone would be held: three misses, last post yesterday.
        _ttl_expired_chain(db, 3, last_posted=now - DAY)
        done = MagicMock()
        done.status = CheckpointStatus.DONE
        done.summary = "merged"
        done.outputs = {}
        done.error = None
        transport = AsyncMock()
        transport.ask.side_effect = ["yes", "yes again"]

        result = await _sweep(
            tmp_path, db, transport,
            states=[_blocked("Approve merging #197 torch 2.5?"),
                    _blocked(TODAY_Q), done],
        )

        assert transport.ask.await_count == 2
        assert result.success


# --- the sweep's fan-out ------------------------------------------------


class TestTheFanOutHoldsToo:
    @staticmethod
    def _repo(name: str):
        r = MagicMock()
        r.name = name
        return r

    @pytest.mark.asyncio
    async def test_a_held_repo_gets_no_message_of_its_own(self) -> None:
        from ctrlrelay.cli import announce_secops_sweep
        from ctrlrelay.pipelines.base import PipelineResult
        from ctrlrelay.pipelines.secops import QUESTION_HELD_UNTIL

        held = PipelineResult(
            success=False, blocked=True, session_id="secops-held",
            summary="held", question=TODAY_Q,
            outputs={QUESTION_HELD_UNTIL: 1793361600},  # 30 Oct 12:00 UTC
        )
        asked = PipelineResult(
            success=False, blocked=True, session_id="secops-asked",
            summary="blocked", question="Approve #7?",
        )
        transport = AsyncMock()

        sent = await announce_secops_sweep(
            transport,
            repos=[self._repo(MINER), self._repo("AInvirion/zzsites")],
            results=[held, asked],
        )

        assert len(sent) == 2
        assert sent[0].startswith("⏸️ Scheduled secops blocked on AInvirion/zzsites")
        assert transport.send.await_args_list[0].kwargs == {
            "session_id": "secops-asked", "repo": "AInvirion/zzsites",
        }
        assert TODAY_Q not in "".join(sent), "the held question was re-posted"
        assert sent[1] == (
            "📋 Scheduled secops sweep done: 0/2 ok, 2 blocked\n\n"
            "Not re-posted, asked before with no answer (reply to the "
            "earlier post to answer):\n"
            f"  - {MINER}: not before 2026-10-30 12:00 UTC"
        )

    @pytest.mark.asyncio
    async def test_a_failed_send_still_raises(self) -> None:
        """Holding is not faking delivery: a send that fails surfaces."""
        from ctrlrelay.cli import announce_secops_sweep
        from ctrlrelay.pipelines.base import PipelineResult

        transport = AsyncMock()
        transport.send.side_effect = RuntimeError("bridge down")

        with pytest.raises(RuntimeError):
            await announce_secops_sweep(
                transport,
                repos=[self._repo(MINER)],
                results=[PipelineResult(
                    success=False, blocked=True, session_id="s",
                    summary="b", question="Approve #7?",
                )],
            )
