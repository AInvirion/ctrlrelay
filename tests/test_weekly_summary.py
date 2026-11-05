"""The weekly chat summary (#230).

The state database is a real ``StateDB`` file, so a restart is a new
``StateDB`` and a new ``WeeklySummary`` on the same file. The transport
and the clock are fakes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ctrlrelay.core.config import WeeklySummaryConfig
from ctrlrelay.core.state import StateDB
from ctrlrelay.core.weekly_summary import (
    SEND_NAME,
    WeeklySummary,
    due_at,
    format_summary,
    read_week,
)

DAY = 86_400
# Monday 2026-11-09 09:00 UTC: the default send time.
MONDAY_9 = int(datetime(2026, 11, 9, 9, 0, tzinfo=UTC).timestamp())


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Chat:
    """A transport's send: records what was delivered, or fails."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_next = 0
        self.hang = False

    async def send(self, text: str) -> None:
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail_next:
            self.fail_next -= 1
            raise FileNotFoundError("bridge socket missing; bridge may be restarting")
        self.sent.append(text)


def _summary(db: StateDB, chat: Chat, clock: Clock, **kw) -> WeeklySummary:
    return WeeklySummary(
        state_db=db, node_id="node-a", day="mon", hour=9, timezone="UTC",
        send=chat.send, clock=clock, **kw,
    )


def _sent_last_week(db: StateDB) -> None:
    # A node already running last week: past its first check.
    db.record_sent(SEND_NAME, MONDAY_9 - 7 * DAY + 60)


def _run(db: StateDB, sid: str, *, started_at: int, status: str, issue: int) -> None:
    db.execute(
        """INSERT INTO sessions (id, pipeline, repo, status, started_at, issue_number)
           VALUES (?, 'dev', 'example-org/alpha', 'running', ?, ?)""",
        (sid, started_at, issue),
    )
    db.execute("UPDATE sessions SET status = ?, ended_at = ? WHERE id = ?",
               (status, started_at + 60, sid))
    db.commit()


@pytest.mark.asyncio
async def test_sent_once_a_week_across_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    chat = Chat()
    clock = Clock(MONDAY_9 - 60)
    db = StateDB(path)
    summary = _summary(db, chat, clock)

    # The first check only notes the time; the summary is not yet due.
    assert await summary.tick() is False
    assert db.last_sent(SEND_NAME) == MONDAY_9 - 60
    assert chat.sent == []
    clock.now = MONDAY_9 + 120
    assert await summary.tick() is True
    assert await summary.tick() is False
    db.close()

    # The poller restarts: a new process, the same database.
    clock.now += 3600
    db = StateDB(path)
    summary = _summary(db, chat, clock)
    assert await summary.tick() is False
    clock.now = MONDAY_9 + 6 * DAY
    assert await summary.tick() is False
    assert len(chat.sent) == 1

    clock.now = MONDAY_9 + 7 * DAY + 300
    assert await summary.tick() is True
    assert len(chat.sent) == 2
    assert db.last_sent(SEND_NAME) == MONDAY_9 + 7 * DAY + 300
    db.close()


@pytest.mark.asyncio
async def test_a_failed_send_is_logged_and_tried_next_tick(tmp_path: Path, caplog) -> None:
    db = StateDB(tmp_path / "state.db")
    _sent_last_week(db)
    chat = Chat()
    chat.fail_next = 1
    clock = Clock(MONDAY_9 + 60)
    summary = _summary(db, chat, clock)

    with caplog.at_level(logging.INFO, logger="ctrlrelay.core.weekly_summary"):
        assert await summary.tick() is False
    assert [r.getMessage() for r in caplog.records].count("summary.weekly.send_failed") == 1
    assert db.last_sent(SEND_NAME) == MONDAY_9 - 7 * DAY + 60  # unchanged

    clock.now += 300
    assert await summary.tick() is True
    assert len(chat.sent) == 1
    db.close()


@pytest.mark.asyncio
async def test_a_send_that_hangs_counts_as_failed(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    _sent_last_week(db)
    chat = Chat()
    chat.hang = True
    summary = _summary(db, chat, Clock(MONDAY_9 + 60), send_timeout=0.05)

    assert await summary.tick() is False
    assert db.last_sent(SEND_NAME) == MONDAY_9 - 7 * DAY + 60  # unchanged
    db.close()


@pytest.mark.asyncio
async def test_a_database_failure_does_not_raise(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    db.close()  # every read now raises
    chat = Chat()
    assert await _summary(db, chat, Clock(MONDAY_9 + 60)).tick() is False
    assert chat.sent == []


@pytest.mark.asyncio
async def test_the_message_carries_the_weeks_numbers(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    now = MONDAY_9 + 60
    _run(db, "a", started_at=now - DAY, status="done", issue=1)
    _run(db, "b", started_at=now - DAY, status="failed", issue=2)
    _run(db, "c", started_at=now - 8 * DAY, status="done", issue=3)  # last week
    db.record_event("restart", ts=now - 2 * DAY)  # events cover the runs
    db.record_event("pr_opened", session_id="a", ts=now - DAY + 600)
    db.record_issue_finish(repo="example-org/alpha", issue_number=1, pr_number=10,
                           session_id="a", finished_at=now - DAY + 900)
    db.record_reopen("example-org/alpha", 1, now - DAY + 900, now - 3600)
    _sent_last_week(db)
    chat = Chat()

    assert await _summary(db, chat, Clock(now)).tick() is True
    [text] = chat.sent
    assert "node-a" in text
    assert "runs started 2: done 1, failed 1, blocked 0, stuck 0" in text
    assert "success rate 50%" in text
    assert "time to PR: median 10m over 1 runs" in text
    assert "time to merge: none yet" in text
    assert "reopened: 1" in text
    assert "unknown" not in text
    db.close()


def test_a_number_that_cannot_be_read_is_unknown_not_zero(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    now = MONDAY_9 + 60
    _run(db, "a", started_at=now - DAY, status="done", issue=1)
    # No events kept at all, so the run's PR times cannot be read; and a
    # database from before reopens were recorded.
    db.execute("DROP TABLE issue_finishes")
    db.commit()

    text = format_summary("node-a", read_week(db, now), now)
    assert "time to PR: unknown" in text
    assert "time to merge: unknown" in text
    assert "reopened: unknown" in text
    assert "reopened: 0" not in text
    assert "reopened unknown: this database predates" in text
    # What could be read still is.
    assert "runs started 1: done 1" in text
    db.close()

    # The runs themselves could not be read: everything is unknown.
    text = format_summary("node-a", None, now)
    assert "runs started unknown: done unknown, failed unknown" in text
    assert "success rate unknown" in text
    assert "reopened: unknown" in text
    # No zero anywhere below the heading line (which carries the date).
    assert not re.search(r"\b0\b", text.split("\n", 1)[1])


def test_due_time_is_the_last_send_time_in_the_configured_zone() -> None:
    # Wednesday 2026-11-11 12:00 UTC: the last Monday 09:00 is the 9th.
    wed = int(datetime(2026, 11, 11, 12, 0, tzinfo=UTC).timestamp())
    assert due_at(wed, day="mon", hour=9, timezone="UTC") == MONDAY_9
    # Exactly at the time counts; a second before is the week before.
    assert due_at(MONDAY_9, day="mon", hour=9, timezone="UTC") == MONDAY_9
    assert due_at(MONDAY_9 - 1, day="mon", hour=9, timezone="UTC") == MONDAY_9 - 7 * DAY
    # Santiago is UTC-3 in November: 09:00 there is 12:00 UTC.
    santiago = due_at(wed, day="mon", hour=9, timezone="America/Santiago")
    assert santiago == MONDAY_9 + 3 * 3600


def test_config_defaults_and_validation() -> None:
    c = WeeklySummaryConfig()
    assert (c.enabled, c.day, c.hour) == (True, "mon", 9)
    assert WeeklySummaryConfig(day="Friday").day == "fri"
    assert WeeklySummaryConfig(day="SUN").day == "sun"
    for bad in ({"day": "funday"}, {"day": "mo"}, {"hour": 24}, {"hour": -1}):
        with pytest.raises(ValidationError):
            WeeklySummaryConfig(**bad)
