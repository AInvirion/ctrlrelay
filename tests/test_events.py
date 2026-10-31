"""Tests for the events table, WAL mode and the best-effort emit wrapper."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from ctrlrelay.core import state as state_mod
from ctrlrelay.core.events import EventKind, emit_event
from ctrlrelay.core.state import StateDB

_EVENTS_DDL_MARKER = "CREATE TABLE IF NOT EXISTS events"


def test_state_db_uses_wal(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "s.db")
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        db.close()


def test_state_db_sets_busy_timeout(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "s.db")
    try:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        db.close()


def test_wal_persists_for_a_plain_reader(tmp_path: Path) -> None:
    """WAL is a property of the file, so a separate (web) connection sees it."""
    path = tmp_path / "s.db"
    StateDB(path).close()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_record_and_list_events_newest_first(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "s.db")
    try:
        db.record_event("dispatched", repo="a/b", issue_number=7, ts=100)
        db.record_event(
            "merged", repo="a/b", session_id="s1", detail={"pr": 5}, ts=200
        )

        events = db.list_events()
        assert [e["kind"] for e in events] == ["merged", "dispatched"]
        assert events[0]["detail"] == {"pr": 5}
        assert events[0]["session_id"] == "s1"
        assert events[0]["ts"] == 200
        assert events[1]["detail"] is None
        assert events[1]["issue_number"] == 7

        later = db.list_events(since_ts=150)
        assert [e["kind"] for e in later] == ["merged"]
    finally:
        db.close()


def test_record_event_is_committed(tmp_path: Path) -> None:
    """A row is visible to another connection without the writer committing."""
    path = tmp_path / "s.db"
    db = StateDB(path)
    try:
        db.record_event("done", repo="a/b", ts=100)
        other = sqlite3.connect(str(path))
        try:
            assert other.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        finally:
            other.close()
    finally:
        db.close()


def test_record_event_defaults_ts_to_now(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(state_mod.time, "time", lambda: 1234.9)
    db = StateDB(tmp_path / "s.db")
    try:
        db.record_event("restart")
        assert db.list_events()[0]["ts"] == 1234
    finally:
        db.close()


def test_list_events_limit(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "s.db")
    try:
        for ts in (100, 200, 300):
            db.record_event("done", ts=ts)
        assert [e["ts"] for e in db.list_events(limit=2)] == [300, 200]
    finally:
        db.close()


def test_prune_events(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "s.db")
    try:
        db.record_event("dispatched", ts=100)
        db.record_event("done", ts=200)
        assert db.prune_events(150) == 1
        remaining = db.list_events()
        assert [e["ts"] for e in remaining] == [200]
        # "older than" is strict: a row exactly at the cutoff stays.
        assert db.prune_events(200) == 0
        assert [e["ts"] for e in db.list_events()] == [200]
    finally:
        db.close()


def test_existing_db_gains_events_table(tmp_path: Path) -> None:
    assert _EVENTS_DDL_MARKER in state_mod.SCHEMA
    old_schema = state_mod.SCHEMA[: state_mod.SCHEMA.index(_EVENTS_DDL_MARKER)]
    # The cut must leave the rest of the schema intact: everything after the
    # events DDL is its own indexes, nothing else.
    tail = state_mod.SCHEMA[state_mod.SCHEMA.index(_EVENTS_DDL_MARKER):]
    assert "CREATE TABLE" not in tail.replace(_EVENTS_DDL_MARKER, "", 1)

    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(old_schema)
    raw.commit()
    names = {
        r[0]
        for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    raw.close()
    assert "events" not in names
    assert "sessions" in names

    db = StateDB(path)
    try:
        db.record_event("done", repo="a/b", ts=100)
        assert [e["kind"] for e in db.list_events()] == ["done"]
    finally:
        db.close()


def test_emit_event_writes_a_row(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "s.db")
    try:
        emit_event(db, EventKind.PR_OPENED, repo="a/b", detail={"pr": 3}, ts=10)
        events = db.list_events()
        assert len(events) == 1
        assert events[0]["kind"] == "pr_opened"
        assert events[0]["detail"] == {"pr": 3}
    finally:
        db.close()


def test_emit_event_swallows_failure(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    db = StateDB(tmp_path / "s.db")
    try:
        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(db, "record_event", boom)
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            assert emit_event(db, EventKind.DONE, repo="a/b") is None
        failures = [
            r for r in caplog.records if r.getMessage() == "events.write_failed"
        ]
        assert len(failures) == 1
        assert failures[0].kind == "done"
        assert failures[0].reason == "OperationalError"
    finally:
        db.close()


def test_emit_event_none_db_is_noop() -> None:
    assert emit_event(None, EventKind.DONE, repo="a/b") is None


def test_event_kind_values() -> None:
    assert {k.value for k in EventKind} == {
        "dispatched", "done", "blocked", "failed", "pr_opened", "merged",
        "ci_started", "ci_finished", "review_started", "review_finished",
        "message_sent", "question_posted", "question_held", "alert_sent",
        "restart",
    }
    assert EventKind.DONE == "done"


@pytest.mark.parametrize("kind", ["done", EventKind.DONE])
def test_record_event_accepts_str_or_kind(tmp_path: Path, kind) -> None:
    db = StateDB(tmp_path / "s.db")
    try:
        db.record_event(kind, ts=1)
        assert db.list_events()[0]["kind"] == "done"
    finally:
        db.close()
