"""Tests for the status page's read-only snapshot (#209).

Every database here is built by the real ``StateDB`` and its write methods
(sessions are inserted with the same statement the pipelines use), and
read back through ``ctrlrelay.web.status``, which must never write to it.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ctrlrelay.core.state import StateDB
from ctrlrelay.web import status as status_module
from ctrlrelay.web.status import (
    build_status,
    list_feed,
    make_sessions_reader,
    open_readonly,
)
from ctrlrelay.web.transcript import transcript_dir_for

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"
# The fixture's last tool use (read off the file by hand): the agent opening its PR.
FIXTURE_LAST_TS = int(datetime(2026, 10, 31, 3, 59, 16, 739000, tzinfo=UTC).timestamp())
FIXTURE_LAST_COMMAND = "gh pr … && tail …"

ALPHA = "example-org/alpha"
BETA = "example-org/beta"

WAITING_OK = {
    "items": [],
    "status": "ok",
    "checked": None,
    "checked_ts": None,
    "last_good": None,
    "last_good_ts": None,
    "error": None,
}

TOP_KEYS = {
    "version", "host", "generated", "generated_ts", "running", "needs_you",
    "waiting", "today", "feed", "feed_total", "feed_truncated", "feed_limit",
    "errors",
}
RUNNING_KEYS = {
    "session_id", "pipeline", "repo", "issue_number", "title", "started",
    "started_ts", "elapsed_s", "phase", "phase_source", "last_command",
    "last_step", "last_step_ts", "looks_stuck", "reason",
}
NEEDS_KEYS = {
    "session_id", "pipeline", "repo", "issue_number", "title", "question",
    "asked", "asked_ts", "held_until", "held_until_ts",
}
TODAY_KEYS = {"dispatched", "pr_opened", "merged", "failed", "blocked", "messages_sent"}
FEED_KEYS = {"ts", "time", "kind", "repo", "issue_number", "session_id", "detail"}


# --- helpers ---------------------------------------------------------------


def _db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.db")


def _session(
    db: StateDB,
    session_id: str,
    *,
    pipeline: str = "dev",
    repo: str = ALPHA,
    issue_number: int | None = 215,
    worktree: str | None = None,
    status: str = "running",
    started_at: int,
) -> None:
    # The statement dev.py uses (secops.py omits issue_number, i.e. NULL).
    db.execute(
        """INSERT INTO sessions
           (id, pipeline, repo, worktree_path, status, started_at, issue_number)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, pipeline, repo, worktree, status, started_at, issue_number),
    )
    db.commit()


def _worktree(home: Path, name: str) -> str:
    return str(home / ".ctrlrelay" / "worktrees" / name)


def _fixture_transcript(home: Path, worktree: str) -> None:
    d = transcript_dir_for(worktree, home)
    d.mkdir(parents=True)
    shutil.copy(FIXTURE, d / "t.jsonl")


def _one_step_transcript(home: Path, worktree: str, ts: int) -> None:
    import json

    d = transcript_dir_for(worktree, home)
    d.mkdir(parents=True)
    stamp = datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")
    row = {
        "type": "assistant",
        "timestamp": stamp,
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "x", "name": "Edit",
             "input": {"file_path": worktree + "/src/app.py"}},
        ]},
    }
    (d / "t.jsonl").write_text(json.dumps(row) + "\n")


def _status(tmp_path: Path, now: int, **kw) -> dict:
    return build_status(
        db_path=tmp_path / "state.db",
        waiting=kw.pop("waiting", WAITING_OK),
        version="9.9.9",
        host="testhost",
        now=now,
        home=kw.pop("home", tmp_path),
        **kw,
    )


def _only(items: list[dict], session_id: str) -> dict:
    matches = [i for i in items if i["session_id"] == session_id]
    assert len(matches) == 1, items
    return matches[0]


# --- running ----------------------------------------------------------------


def test_running_dev_session_with_transcript_phase(tmp_path: Path) -> None:
    db = _db(tmp_path)
    wt = _worktree(tmp_path, "example-org-alpha-dev-215-49a5")
    _fixture_transcript(tmp_path, wt)
    started = FIXTURE_LAST_TS - 600
    _session(db, "s-dev", worktree=wt, started_at=started)

    now = FIXTURE_LAST_TS + 60
    out = _status(tmp_path, now)

    assert out["errors"] == []
    card = _only(out["running"], "s-dev")
    assert card["phase"] == "pr"
    assert card["phase_source"] == "transcript"
    assert card["last_command"] == FIXTURE_LAST_COMMAND
    assert card["last_step_ts"] == FIXTURE_LAST_TS
    assert card["last_step"] == datetime.fromtimestamp(FIXTURE_LAST_TS, UTC).isoformat()
    assert card["started_ts"] == started
    assert card["elapsed_s"] == 660
    assert card["looks_stuck"] is False
    assert card["repo"] == ALPHA and card["issue_number"] == 215
    assert card["pipeline"] == "dev"


def test_only_running_sessions_are_listed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _session(db, "s-run", started_at=1_000)
    _session(db, "s-done", status="done", started_at=1_000, issue_number=216)
    out = _status(tmp_path, 2_000)
    assert [c["session_id"] for c in out["running"]] == ["s-run"]


def test_open_ci_event_overrides_transcript(tmp_path: Path) -> None:
    db = _db(tmp_path)
    wt = _worktree(tmp_path, "w1")
    _fixture_transcript(tmp_path, wt)
    _session(db, "s1", worktree=wt, started_at=FIXTURE_LAST_TS - 60)
    db.record_event("ci_started", session_id="s1", repo=ALPHA,
                    detail={"pr_number": 7}, ts=FIXTURE_LAST_TS + 5)

    # The transcript's last step is two hours old.
    out = _status(tmp_path, FIXTURE_LAST_TS + 7200)

    card = _only(out["running"], "s1")
    assert card["phase"] == "ci"
    assert card["phase_source"] == "pipeline"
    assert card["looks_stuck"] is False
    # The agent's last step is still shown.
    assert card["last_command"] == FIXTURE_LAST_COMMAND


def test_a_finished_ci_no_longer_overrides(tmp_path: Path) -> None:
    """Control for the override: once CI finished, the transcript's phase
    and the stuck check are back."""
    db = _db(tmp_path)
    wt = _worktree(tmp_path, "w1")
    _fixture_transcript(tmp_path, wt)
    _session(db, "s1", worktree=wt, started_at=FIXTURE_LAST_TS - 60)
    db.record_event("ci_started", session_id="s1", detail={"pr_number": 7},
                    ts=FIXTURE_LAST_TS + 5)
    db.record_event("ci_finished", session_id="s1",
                    detail={"pr_number": 7, "ok": True}, ts=FIXTURE_LAST_TS + 60)

    card = _only(_status(tmp_path, FIXTURE_LAST_TS + 7200)["running"], "s1")
    assert card["phase"] == "pr"
    assert card["phase_source"] == "transcript"
    assert card["looks_stuck"] is True


def test_review_after_ci_is_the_phase(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _session(db, "s1", worktree=None, started_at=1_000)
    db.record_event("ci_started", session_id="s1", ts=1_100)
    db.record_event("ci_finished", session_id="s1", ts=1_200)
    db.record_event("review_started", session_id="s1", ts=1_300)
    # Another session's events never close or open this one's phase.
    db.record_event("review_finished", session_id="other", ts=1_400)

    card = _only(_status(tmp_path, 5_000)["running"], "s1")
    assert card["phase"] == "review"
    assert card["phase_source"] == "pipeline"
    assert card["looks_stuck"] is False


def test_a_poller_restart_closes_an_open_ci_wait(tmp_path: Path) -> None:
    """The CI wait runs inside the poller; a restart after it started means
    nobody is waiting any more, so it must not keep suppressing the stuck mark."""
    db = _db(tmp_path)
    wt = _worktree(tmp_path, "w1")
    _one_step_transcript(tmp_path, wt, 1_000)
    _session(db, "s1", worktree=wt, started_at=900)
    db.record_event("ci_started", session_id="s1", ts=1_100)
    db.record_event("restart", detail={"process": "poller", "version": "x"}, ts=1_200)

    card = _only(_status(tmp_path, 1_000 + 3_600)["running"], "s1")
    assert card["phase"] == "coding"
    assert card["phase_source"] == "transcript"
    assert card["looks_stuck"] is True


def test_a_bridge_restart_does_not_close_a_ci_wait(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _session(db, "s1", worktree=None, started_at=900)
    db.record_event("ci_started", session_id="s1", ts=1_100)
    db.record_event("restart", detail={"process": "bridge", "version": "x"}, ts=1_200)

    card = _only(_status(tmp_path, 5_000)["running"], "s1")
    assert card["phase"] == "ci"


@pytest.mark.parametrize(("age_min", "stuck"), [(16, True), (14, False)])
def test_looks_stuck_after_threshold(tmp_path: Path, age_min: int, stuck: bool) -> None:
    db = _db(tmp_path)
    wt = _worktree(tmp_path, "w1")
    step = 1_800_000_000
    _one_step_transcript(tmp_path, wt, step)
    _session(db, "s1", worktree=wt, started_at=step - 60)

    card = _only(_status(tmp_path, step + age_min * 60)["running"], "s1")
    assert card["phase"] == "coding"
    assert card["looks_stuck"] is stuck


def test_no_transcript_is_unknown_with_reason(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _session(db, "s1", worktree=_worktree(tmp_path, "nothing-here"), started_at=1_000)
    card = _only(_status(tmp_path, 100_000)["running"], "s1")
    assert card["phase"] == "unknown"
    assert card["phase_source"] == "none"
    assert card["reason"] == "no transcript"
    assert card["last_step_ts"] is None and card["last_step"] is None
    assert card["looks_stuck"] is False


def test_secops_session_has_no_issue(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.execute(
        """INSERT INTO sessions (id, pipeline, repo, worktree_path, status, started_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("s-sec", "secops", BETA, _worktree(tmp_path, "sec"), "running", 1_000),
    )
    db.commit()

    out = _status(tmp_path, 2_000)

    assert out["errors"] == []
    card = _only(out["running"], "s-sec")
    assert card["pipeline"] == "secops"
    assert card["issue_number"] is None
    assert card["title"] is None
    assert set(card) == RUNNING_KEYS


def test_task_session_renders(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _session(db, "s-task", pipeline="task", issue_number=31, worktree=None,
             started_at=1_000)
    out = _status(tmp_path, 2_000)
    card = _only(out["running"], "s-task")
    assert card["pipeline"] == "task"
    assert card["phase"] == "unknown"
    assert card["reason"] == "no worktree"


def test_titles_come_only_from_the_titles_map(tmp_path: Path) -> None:
    """Titles come only from the tracker's unfiltered map. A title in the
    waiting line's items is never used, even when the map has none."""
    db = _db(tmp_path)
    _session(db, "s1", issue_number=215, worktree=None, started_at=1_000)
    _session(db, "s2", repo=BETA, issue_number=215, worktree=None, started_at=1_000)
    waiting = {**WAITING_OK, "items": [
        {"repo": ALPHA, "issue_number": 215, "title": "Stale waiting title",
         "reason": "next poll", "updated_ts": 1, "updated": None},
        {"repo": BETA, "issue_number": 215, "title": "Only in waiting",
         "reason": "next poll", "updated_ts": 1, "updated": None},
    ]}
    out = _status(tmp_path, 2_000, waiting=waiting,
                  titles={f"{ALPHA}#215": "From the map"})
    assert _only(out["running"], "s1")["title"] == "From the map"
    assert _only(out["running"], "s2")["title"] is None
    # No map at all: no titles, whatever the waiting line holds.
    out = _status(tmp_path, 2_000, waiting=waiting)
    assert {c["title"] for c in out["running"]} == {None}


def test_titles_map_fills_running_titles(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _session(db, "s1", issue_number=215, worktree=None, started_at=1_000)
    _session(db, "s2", issue_number=216, worktree=None, started_at=1_000)
    titles = {f"{ALPHA.upper()}#215": "From the last search", f"{BETA}#216": "Other repo"}
    out = _status(tmp_path, 2_000, titles=titles)
    # Repo names compare as GitHub does, case-insensitively.
    assert _only(out["running"], "s1")["title"] == "From the last search"
    # Absent from the map (only another repo's #216 is there): null.
    assert _only(out["running"], "s2")["title"] is None


# --- needs you ---------------------------------------------------------------


def test_needs_you_lists_open_unanswered_questions(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _session(db, "s-open", issue_number=215, status="blocked", started_at=1_000)
    db.execute(
        """INSERT INTO sessions (id, pipeline, repo, worktree_path, status, started_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("s-held", "secops", BETA, None, "blocked", 1_000),
    )
    db.commit()
    db.add_pending_resume("s-open", "dev", ALPHA, "Which branch?")
    db.add_pending_resume("s-held", "secops", BETA, "Bump the major?",
                          held_until=1_900_000_000, posted=False)
    db.add_pending_resume("s-answered", "dev", ALPHA, "Answered one")
    db.answer_pending_resume("s-answered", "main")
    db.add_pending_resume("s-expired", "dev", ALPHA, "Expired one")
    db.expire_pending_resume("s-expired", "ttl")
    created = db.get_pending_resume("s-open")["created_at"]

    out = _status(tmp_path, int(time.time()), titles={f"{ALPHA}#215": "Pick a branch"})

    assert out["errors"] == []
    assert sorted(q["session_id"] for q in out["needs_you"]) == ["s-held", "s-open"]
    open_q = _only(out["needs_you"], "s-open")
    assert set(open_q) == NEEDS_KEYS
    assert open_q["question"] == "Which branch?"
    assert open_q["issue_number"] == 215
    assert open_q["title"] == "Pick a branch"
    assert open_q["pipeline"] == "dev" and open_q["repo"] == ALPHA
    assert open_q["asked_ts"] == created
    assert open_q["asked"] == datetime.fromtimestamp(created, UTC).isoformat()
    assert open_q["held_until_ts"] is None and open_q["held_until"] is None
    held = _only(out["needs_you"], "s-held")
    # A secops question has no issue, so no title either.
    assert held["issue_number"] is None and held["title"] is None
    assert held["held_until_ts"] == 1_900_000_000
    assert held["held_until"] == datetime.fromtimestamp(1_900_000_000, UTC).isoformat()


# --- today -------------------------------------------------------------------


@pytest.fixture
def tz_plus5() -> Iterator[None]:
    """Local time five hours ahead of UTC, so the local day and the UTC day
    start at different instants."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "XXX-05"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_today_counts_from_events(tmp_path: Path, tz_plus5: None) -> None:
    db = _db(tmp_path)
    # now: 2026-10-30 02:00 local = 2026-10-29 21:00 UTC.
    now = int(datetime(2026, 10, 29, 21, 0, tzinfo=UTC).timestamp())
    # 2026-10-29 23:00 local (18:00 UTC): yesterday locally, today in UTC.
    yesterday_local = int(datetime(2026, 10, 29, 18, 0, tzinfo=UTC).timestamp())
    # 2026-10-30 00:30 local (19:30 UTC): today locally.
    today_local = int(datetime(2026, 10, 29, 19, 30, tzinfo=UTC).timestamp())

    for kind in ("dispatched", "pr_opened", "merged", "failed", "blocked", "message_sent"):
        db.record_event(kind, ts=yesterday_local)
    db.record_event("dispatched", ts=today_local)
    db.record_event("dispatched", ts=today_local + 1)
    db.record_event("pr_opened", ts=today_local)
    db.record_event("merged", ts=today_local)
    db.record_event("failed", ts=today_local)
    db.record_event("blocked", ts=today_local)
    db.record_event("message_sent", ts=today_local)
    db.record_event("message_sent", ts=today_local)
    db.record_event("message_sent", ts=today_local)
    db.record_event("ci_started", ts=today_local)  # not counted anywhere

    out = _status(tmp_path, now)

    assert out["today"] == {
        "dispatched": 2, "pr_opened": 1, "merged": 1, "failed": 1,
        "blocked": 1, "messages_sent": 3,
    }


# --- feed --------------------------------------------------------------------


def test_feed_in_status_is_newest_first_within_history(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = 1_800_000_000
    db.record_event("dispatched", repo=ALPHA, issue_number=1, session_id="a",
                    detail={"pipeline": "dev"}, ts=now - 8 * 86_400)
    db.record_event("dispatched", repo=ALPHA, issue_number=2, session_id="b",
                    detail={"pipeline": "dev"}, ts=now - 3_600)
    db.record_event("pr_opened", repo=ALPHA, issue_number=2, session_id="b",
                    detail={"pr_number": 9, "pr_url": None}, ts=now - 60)

    out = _status(tmp_path, now, history_days=7)

    assert [e["kind"] for e in out["feed"]] == ["pr_opened", "dispatched"]
    first = out["feed"][0]
    assert set(first) == FEED_KEYS
    assert first["detail"] == {"pr_number": 9, "pr_url": None}
    assert first["ts"] == now - 60
    assert first["time"] == datetime.fromtimestamp(now - 60, UTC).isoformat()
    assert first["repo"] == ALPHA and first["issue_number"] == 2
    assert first["session_id"] == "b"


def test_feed_since(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.record_event("dispatched", ts=100, detail={"pipeline": "dev"})
    db.record_event("merged", ts=200, detail={"pr_number": 3, "pr_url": None})
    db.record_event("message_sent", ts=300, detail={"request_id": None, "chars": 5})

    out = list_feed(db_path=tmp_path / "state.db", since_ts=200)

    assert out["error"] is None
    assert [e["kind"] for e in out["events"]] == ["message_sent", "merged"]
    assert out["events"][1]["detail"] == {"pr_number": 3, "pr_url": None}
    one = list_feed(db_path=tmp_path / "state.db", since_ts=200, limit=1)["events"]
    assert [e["kind"] for e in one] == ["message_sent"]


def test_feed_on_a_missing_db_is_unknown_not_empty(tmp_path: Path) -> None:
    out = list_feed(db_path=tmp_path / "nope.db", since_ts=0)
    assert out["events"] is None
    assert out["error"] and "nope.db" in out["error"]


def test_a_feed_over_the_limit_says_it_was_cut_and_by_how_much(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(status_module, "FEED_LIMIT", 2)
    db = _db(tmp_path)
    now = 1_800_000_000
    db.record_event("dispatched", ts=now - 30 * 86_400)  # outside the window
    for i in range(3):
        db.record_event("dispatched", issue_number=i, ts=now - 300 + i)

    out = _status(tmp_path, now, history_days=7)

    assert [e["issue_number"] for e in out["feed"]] == [2, 1]
    assert out["feed_total"] == 3
    assert out["feed_truncated"] is True
    assert out["feed_limit"] == 2


def test_a_feed_within_the_limit_is_not_truncated(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = 1_800_000_000
    db.record_event("dispatched", ts=now - 60)
    db.record_event("merged", ts=now - 30)

    out = _status(tmp_path, now)

    assert len(out["feed"]) == 2
    assert out["feed_total"] == 2
    assert out["feed_truncated"] is False
    assert out["feed_limit"] == status_module.FEED_LIMIT


def test_an_unreadable_feed_has_an_unknown_total(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.record_event("merged", ts=10**15)  # makes the feed section unknown
    out = _status(tmp_path, 2_000)
    assert out["feed"] is None
    assert out["feed_total"] is None
    assert out["feed_truncated"] is False

    missing = build_status(db_path=tmp_path / "nope.db", waiting=WAITING_OK,
                           version="1", host="h", now=2_000, home=tmp_path)
    assert missing["feed_total"] is None and missing["feed_truncated"] is False
    assert missing["feed_limit"] == status_module.FEED_LIMIT


def test_list_feed_reports_its_limit_total_and_truncation(tmp_path: Path) -> None:
    db = _db(tmp_path)
    for ts in (100, 200, 300):
        db.record_event("dispatched", ts=ts)

    cut = list_feed(db_path=tmp_path / "state.db", since_ts=150, limit=1)
    assert [e["ts"] for e in cut["events"]] == [300]
    assert cut["feed_total"] == 2 and cut["feed_truncated"] is True
    assert cut["limit"] == 1

    whole = list_feed(db_path=tmp_path / "state.db", since_ts=0)
    assert len(whole["events"]) == 3
    assert whole["feed_total"] == 3 and whole["feed_truncated"] is False
    assert whole["limit"] == status_module.FEED_LIMIT

    gone = list_feed(db_path=tmp_path / "nope.db", since_ts=0, limit=5)
    assert gone["events"] is None and gone["feed_total"] is None
    assert gone["feed_truncated"] is False and gone["limit"] == 5


# --- contract ----------------------------------------------------------------


def test_contract_keys(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _session(db, "s1", worktree=None, started_at=1_000)
    db.add_pending_resume("s2", "dev", ALPHA, "Q?")
    db.record_event("dispatched", session_id="s1", ts=1_000)
    out = _status(tmp_path, 2_000)

    assert set(out) == TOP_KEYS
    assert set(out["running"][0]) == RUNNING_KEYS
    assert set(out["needs_you"][0]) == NEEDS_KEYS
    assert set(out["today"]) == TODAY_KEYS
    assert set(out["feed"][0]) == FEED_KEYS
    assert out["version"] == "9.9.9" and out["host"] == "testhost"
    assert out["generated_ts"] == 2_000
    assert out["generated"] == datetime.fromtimestamp(2_000, UTC).isoformat()
    assert out["waiting"] == WAITING_OK


# --- failure modes -------------------------------------------------------------


def test_missing_db_returns_unknown_sections(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "state.db"
    out = build_status(db_path=path, waiting=WAITING_OK, version="1", host="h",
                       now=2_000, home=tmp_path)

    assert out["errors"] and any(str(path) in e for e in out["errors"])
    # Unknown, not "nothing running": null, never an empty list or zero.
    assert out["running"] is None
    assert out["needs_you"] is None
    assert out["feed"] is None
    assert out["today"] == dict.fromkeys(TODAY_KEYS)
    # The waiting line is the tracker's, whatever the DB did.
    assert out["waiting"] == WAITING_OK
    # Reading never created the file (or its directory).
    assert not path.parent.exists()


def test_locked_db_reports_error(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _session(db, "s1", worktree=None, started_at=1_000)
    db.close()
    # A database still in rollback-journal mode (as before WAL), with a
    # writer holding an exclusive lock: a reader cannot get in at all.
    holder = sqlite3.connect(str(tmp_path / "state.db"), isolation_level=None)
    holder.execute("PRAGMA journal_mode=DELETE")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        out = _status(tmp_path, 2_000)
        elapsed = time.monotonic() - start
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert elapsed < 3.0
    assert out["errors"]
    assert out["running"] is None and out["needs_you"] is None and out["feed"] is None
    assert out["today"] == dict.fromkeys(TODAY_KEYS)


def test_db_older_than_the_events_table(tmp_path: Path) -> None:
    db = _db(tmp_path)
    wt = _worktree(tmp_path, "w1")
    # The agent's last step is an hour old: stuck, unless CI or review is open.
    _one_step_transcript(tmp_path, wt, 1_000)
    _session(db, "s1", worktree=wt, started_at=900)
    db.add_pending_resume("s2", "dev", ALPHA, "Q?")
    db.execute("DROP TABLE events")
    db.commit()
    db.close()

    out = _status(tmp_path, 1_000 + 3_600)

    assert [c["session_id"] for c in out["running"]] == ["s1"]
    assert [q["session_id"] for q in out["needs_you"]] == ["s2"]
    # No session row for the question: no issue to show, not a guess.
    assert out["needs_you"][0]["issue_number"] is None
    assert out["feed"] is None
    assert out["today"] == dict.fromkeys(TODAY_KEYS)
    assert any("events" in e for e in out["errors"])
    # Without events nobody can say whether CI or review is open, so the
    # card says so rather than calling the session stuck.
    card = out["running"][0]
    assert card["phase"] == "coding" and card["phase_source"] == "transcript"
    assert card["looks_stuck"] is False
    assert card["reason"] and "events" in card["reason"]


def test_a_corrupt_detail_is_null_not_an_exception(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.execute("INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
               (100, "dispatched", "{not json"))
    db.commit()
    out = list_feed(db_path=tmp_path / "state.db", since_ts=0)
    assert out["error"] is None
    assert out["events"][0]["detail"] is None


# --- read only -------------------------------------------------------------------


def _stat(path: Path) -> dict[str, tuple[int, int] | None]:
    out: dict[str, tuple[int, int] | None] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(path) + suffix)
        out[suffix] = (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
    return out


def _exercise_all_readers(tmp_path: Path, now: int) -> dict:
    path = tmp_path / "state.db"
    out = _status(tmp_path, now)
    list_feed(db_path=path, since_ts=0)
    make_sessions_reader(path)()
    return out


def test_reading_while_the_daemon_has_it_open_changes_no_mtime_size_or_files(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)  # stays open, as the poller's does: -wal and -shm exist
    _session(db, "s1", worktree=None, started_at=1_000)
    db.add_pending_resume("s2", "dev", ALPHA, "Q?")
    db.record_event("dispatched", session_id="s1", ts=1_000)
    path = tmp_path / "state.db"
    before = _stat(path)
    assert before["-wal"] is not None and before["-shm"] is not None
    time.sleep(0.02)

    out = _exercise_all_readers(tmp_path, 2_000)

    assert out["errors"] == []
    assert out["running"] and out["feed"]
    assert _stat(path) == before
    db.close()


def test_reading_when_nobody_has_it_open_changes_no_mtime_size_or_files(
    tmp_path: Path,
) -> None:
    """With no daemon connection a WAL database has no -wal or -shm, and a
    plain read-only open would create both. Reading must not."""
    db = _db(tmp_path)
    _session(db, "s1", worktree=None, started_at=1_000)
    db.record_event("dispatched", session_id="s1", ts=1_000)
    db.close()
    path = tmp_path / "state.db"
    before = _stat(path)
    assert before["-wal"] is None and before["-shm"] is None
    time.sleep(0.02)

    out = _exercise_all_readers(tmp_path, 2_000)

    assert out["errors"] == []
    assert [c["session_id"] for c in out["running"]] == ["s1"]
    assert _stat(path) == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("state.db")) == [
        "state.db"
    ]


def test_open_readonly_refuses_writes(tmp_path: Path) -> None:
    _db(tmp_path).close()
    conn = open_readonly(tmp_path / "state.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO events (ts, kind) VALUES (1, 'x')")
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_a_daemon_opening_mid_read_discards_the_unlocked_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nobody holding the database it is read without locks. If the
    daemon opens it during that read, the read is thrown away and taken
    again with locks, never returned as if it were current."""
    db = _db(tmp_path)
    _session(db, "s1", worktree=None, started_at=1_000)
    db.close()
    path = tmp_path / "state.db"
    opened: list[StateDB] = []
    real = status_module._read_running

    def racing(conn):
        rows = real(conn)
        if not opened:
            daemon = StateDB(path)  # creates -wal and -shm
            _session(daemon, "s2", worktree=None, started_at=1_500)
            opened.append(daemon)
        return rows

    monkeypatch.setattr(status_module, "_read_running", racing)
    try:
        out = _status(tmp_path, 2_000)
    finally:
        for d in opened:
            d.close()

    assert out["errors"] == []
    assert sorted(c["session_id"] for c in out["running"]) == ["s1", "s2"]


# --- sessions reader for the waiting line -----------------------------------------


def test_make_sessions_reader_returns_running_and_locks(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _session(db, "s-old", issue_number=215, started_at=1_000)
    db.execute(
        """INSERT INTO sessions (id, pipeline, repo, worktree_path, status, started_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("s-sec", "secops", BETA, None, "running", 2_000),
    )
    _session(db, "s-done", status="done", issue_number=1, started_at=500)
    db.commit()
    assert db.acquire_lock(ALPHA, "s-old")

    running, locks = make_sessions_reader(tmp_path / "state.db")()

    assert running == [
        {"session_id": "s-old", "repo": ALPHA, "issue_number": 215, "pipeline": "dev"},
        {"session_id": "s-sec", "repo": BETA, "issue_number": None, "pipeline": "secops"},
    ]
    assert locks == {ALPHA: "s-old"}


def test_make_sessions_reader_raises_on_a_missing_db(tmp_path: Path) -> None:
    reader = make_sessions_reader(tmp_path / "absent.db")
    with pytest.raises(Exception):
        reader()
    assert not (tmp_path / "absent.db").exists()


def test_make_sessions_reader_raises_when_locked(tmp_path: Path) -> None:
    _db(tmp_path).close()
    holder = sqlite3.connect(str(tmp_path / "state.db"), isolation_level=None)
    holder.execute("PRAGMA journal_mode=DELETE")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            make_sessions_reader(tmp_path / "state.db")()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_build_status_is_safe_to_call_from_threads(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _session(db, "s1", worktree=None, started_at=1_000)
    results: list[dict] = []

    def run() -> None:
        results.append(_status(tmp_path, 2_000))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(results) == 4
    assert all(r["errors"] == [] and len(r["running"]) == 1 for r in results)
    db.close()


# --- review round: retries and old question tables ----------------------------


def _closed_db(tmp_path: Path) -> Path:
    db = _db(tmp_path)
    _session(db, "s1", worktree=None, started_at=1_000)
    db.close()
    return tmp_path / "state.db"


def test_a_lock_free_read_that_raises_once_is_retried(tmp_path: Path) -> None:
    path = _closed_db(tmp_path)  # nobody has it open: the lock-free path
    calls: list[int] = []

    def flaky(conn):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return status_module._read_running(conn)

    rows = status_module._read(path, flaky)

    assert [r["id"] for r in rows] == ["s1"]
    assert len(calls) == 2


def test_a_lock_free_read_that_keeps_raising_gives_up(tmp_path: Path) -> None:
    path = _closed_db(tmp_path)
    calls: list[int] = []

    def broken(conn):
        calls.append(1)
        raise sqlite3.DatabaseError("database disk image is malformed")

    with pytest.raises(sqlite3.DatabaseError):
        status_module._read(path, broken)
    assert len(calls) == status_module._ATTEMPTS


def test_a_locked_read_is_not_retried(tmp_path: Path) -> None:
    """Retrying a busy database would multiply the lock wait."""
    db = _db(tmp_path)  # held open: the locked (normal) path
    calls: list[int] = []

    def busy(conn):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        status_module._read(tmp_path / "state.db", busy)
    assert len(calls) == 1
    db.close()


def _old_pending_resumes(tmp_path: Path, columns: str) -> None:
    db = _db(tmp_path)
    db.execute("DROP TABLE pending_resumes")
    db.execute(f"CREATE TABLE pending_resumes ({columns})")
    db.commit()
    db.close()


def test_pending_resumes_without_held_until_is_unknown(tmp_path: Path) -> None:
    _old_pending_resumes(
        tmp_path,
        "session_id TEXT PRIMARY KEY, pipeline TEXT, repo TEXT, question TEXT,"
        " created_at INTEGER, answered_at INTEGER, expired_at INTEGER",
    )
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    conn.execute(
        "INSERT INTO pending_resumes VALUES ('q1', 'dev', ?, 'Which?', 100, NULL, NULL)",
        (ALPHA,),
    )
    conn.commit()
    conn.close()

    out = _status(tmp_path, 2_000)

    # Unknown whether it is held back, so the section is unknown: never a
    # list that reads as "not held".
    assert out["needs_you"] is None
    # Named by the column check, which names the table, not by the query
    # tripping over its alias.
    assert "pending_resumes: no such column: pending_resumes.held_until" in out["errors"]
    assert out["running"] == []


def test_pending_resumes_missing_a_required_column_is_unknown(tmp_path: Path) -> None:
    _old_pending_resumes(
        tmp_path,
        "session_id TEXT PRIMARY KEY, pipeline TEXT, repo TEXT, question TEXT,"
        " created_at INTEGER",  # no answered_at, no expired_at
    )
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    conn.execute(
        "INSERT INTO pending_resumes VALUES ('q1', 'dev', ?, 'Which?', 100)",
        (ALPHA,),
    )
    conn.commit()
    conn.close()

    out = _status(tmp_path, 2_000)

    assert out["needs_you"] is None
    # Every missing column is named, not just the first the query trips on.
    assert any(
        "pending_resumes" in e and "answered_at" in e and "expired_at" in e
        for e in out["errors"]
    )
    # The rest of the database still reads.
    assert out["running"] == []


def test_a_question_row_that_cannot_be_shaped_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    db.add_pending_resume("good", "dev", ALPHA, "Fine?")
    db.add_pending_resume("bad", "dev", ALPHA, "Broken?")
    db.close()
    real = status_module._read_needs

    def one_bad_row(conn):
        rows = real(conn)
        for r in rows:
            if r["session_id"] == "bad":
                del r["question"]
        return rows

    monkeypatch.setattr(status_module, "_read_needs", one_bad_row)
    out = _status(tmp_path, 2_000)

    # Never a shorter list that reads as complete.
    assert out["needs_you"] is None
    assert any("pending_resumes" in e for e in out["errors"])


# --- review round 2: whole sections or null -----------------------------------


def test_open_readonly_never_reads_without_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock-free open is safe only inside _read, which checks the file
    did not change. The public opener never uses it."""
    _closed_db(tmp_path)  # nobody has it open: where _read goes lock-free
    uris: list[str] = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        uris.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(status_module.sqlite3, "connect", spy)
    conn = open_readonly(tmp_path / "state.db")
    conn.close()

    assert len(uris) == 1
    assert "mode=ro" in uris[0]
    assert "immutable" not in uris[0]


def test_sessions_reader_goes_through_the_guarded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _closed_db(tmp_path)
    used: list[Path] = []
    real = status_module._read

    def spy(path, fn):
        used.append(path)
        return real(path, fn)

    monkeypatch.setattr(status_module, "_read", spy)
    running, _ = make_sessions_reader(tmp_path / "state.db")()
    assert [r["session_id"] for r in running] == ["s1"]
    assert used == [tmp_path / "state.db"]


def test_a_running_row_with_a_corrupt_start_is_unknown_not_an_exception(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _session(db, "s1", worktree=None, started_at=1_000)
    db.execute(
        """INSERT INTO sessions (id, pipeline, repo, status, started_at)
           VALUES ('s2', 'dev', ?, 'running', 'not a number')""",
        (ALPHA,),
    )
    db.record_event("dispatched", ts=1_500)
    db.commit()

    out = _status(tmp_path, 2_000)

    assert out["running"] is None
    assert any("sessions" in e for e in out["errors"])
    # Other sections are still whole.
    assert [e["kind"] for e in out["feed"]] == ["dispatched"]
    assert out["today"] is not None


def test_an_event_with_an_out_of_range_time_makes_the_feed_unknown(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _session(db, "s1", worktree=None, started_at=1_000)
    db.record_event("dispatched", ts=1_500)
    db.record_event("merged", ts=10**15)  # no datetime can hold it

    out = _status(tmp_path, 2_000)

    assert out["feed"] is None
    assert any("events" in e for e in out["errors"])
    assert [c["session_id"] for c in out["running"]] == ["s1"]

    alone = list_feed(db_path=tmp_path / "state.db", since_ts=0)
    assert alone["events"] is None and alone["error"]


def test_reads_a_database_whose_wal_switch_was_deferred(tmp_path: Path) -> None:
    """A daemon may run on a rollback-journal file (its WAL switch was
    deferred at startup). The page reads it like any other, alongside
    another reader."""
    db = _db(tmp_path)
    _session(db, "s1", worktree=None, started_at=1_000)
    db.record_event("dispatched", session_id="s1", ts=1_500)
    db.close()
    path = tmp_path / "state.db"
    conv = sqlite3.connect(str(path))
    assert conv.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    conv.close()

    other = sqlite3.connect(str(path), isolation_level=None)
    other.execute("BEGIN")
    other.execute("SELECT count(*) FROM sessions").fetchone()
    try:
        out = _status(tmp_path, 2_000)
        feed = list_feed(db_path=path, since_ts=0)
        running, _locks = make_sessions_reader(path)()
    finally:
        other.execute("COMMIT")
        other.close()

    assert out["errors"] == []
    assert [c["session_id"] for c in out["running"]] == ["s1"]
    assert [e["kind"] for e in out["feed"]] == ["dispatched"]
    assert [e["kind"] for e in feed["events"]] == ["dispatched"]
    assert [r["session_id"] for r in running] == ["s1"]
    # Reading did not switch the file or leave sidecars behind.
    assert not os.path.exists(f"{path}-wal")
    check = sqlite3.connect(str(path))
    assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    check.close()
