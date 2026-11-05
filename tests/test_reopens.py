"""Reopened issues are recorded and counted (#230).

Every database is a real ``StateDB``. GitHub is a fake that answers the
one call the tracker makes (``list_reopen_times``) and counts it; the
clock is a fake too. Counts are read back through ``build_status``, the
code ``/api/status`` serves.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.poller import IssuePoller
from ctrlrelay.core.reopens import ReopenTracker
from ctrlrelay.core.state import StateDB
from ctrlrelay.web.status import build_status
from tests._poller_fakes import wire_search

REPO = "example-org/alpha"
OTHER = "example-org/beta"
DAY = 86_400
NOW = 1_800_000_000
FINISHED = NOW - 3 * DAY
WAITING_OK = {
    "items": [], "status": "ok", "checked": None, "checked_ts": None,
    "last_good": None, "last_good_ts": None, "error": None,
}


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    def __init__(self, reopens: dict[tuple[str, int], list[int]] | None = None,
                 fail: bool = False) -> None:
        self.reopens = reopens or {}
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    async def list_reopen_times(self, repo: str, number: int) -> list[str]:
        self.calls.append((repo, number))
        if self.fail:
            raise RuntimeError("gh: HTTP 502")
        return [_iso(t) for t in self.reopens.get((repo, number), [])]


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _status(tmp_path: Path) -> dict:
    return build_status(
        db_path=tmp_path / "state.db", waiting=WAITING_OK, version="9",
        host="h", now=NOW, home=tmp_path, server=lambda: {},
    )


def _window(out: dict, days: int) -> dict:
    [w] = [w for w in out["numbers"]["windows"] if w["days"] == days]
    return w


def _finish(db: StateDB, number: int = 7, *, repo: str = REPO,
            at: int = FINISHED, pr: int = 70) -> None:
    db.record_issue_finish(repo=repo, issue_number=number, pr_number=pr,
                           session_id=f"s-{number}", finished_at=at)


def _reopened_events(db: StateDB) -> list:
    return db.execute(
        "SELECT repo, issue_number FROM events WHERE kind = 'reopened'"
    ).fetchall()


@pytest.mark.asyncio
async def test_a_reopen_is_recorded_once_and_counted(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    _finish(db)
    gh = FakeGitHub({(REPO, 7): [FINISHED + 600]})
    tracker = ReopenTracker(state_db=db, github=gh, clock=Clock())

    assert await tracker.check({(REPO, 7)}) == 1
    # Seen open again on the next polls: nothing more is recorded, and
    # GitHub is not asked again.
    assert await tracker.check({(REPO, 7)}) == 0
    assert await tracker.check({(REPO, 7)}) == 0
    assert gh.calls == [(REPO, 7)]
    assert [tuple(r) for r in _reopened_events(db)] == [(REPO, 7)]
    db.close()

    out = _status(tmp_path)
    w7, w30 = _window(out, 7), _window(out, 30)
    assert w7["overall"]["reopened"] == 1
    assert w30["overall"]["reopened"] == 1
    assert "reopened" not in w7["unavailable"]
    [repo] = w7["by_repo"]
    assert repo["repo"] == REPO and repo["reopened"] == 1
    assert repo["started"] == 0  # a reopen with no run in the window
    assert w7["reopened_issues"] == [{
        "repo": REPO, "issue_number": 7,
        "url": f"https://github.com/{REPO}/issues/7",
    }]


@pytest.mark.asyncio
async def test_an_issue_the_relay_never_finished_is_not_counted(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    _finish(db, 7)
    gh = FakeGitHub({(REPO, 8): [FINISHED + 600], (OTHER, 7): [FINISHED + 600]})
    tracker = ReopenTracker(state_db=db, github=gh, clock=Clock())

    # Open and reopened on GitHub, but the relay never finished them
    # (OTHER#7 shares the number, not the repo).
    assert await tracker.check({(REPO, 8), (OTHER, 7)}) == 0
    assert gh.calls == []
    db.close()

    w7 = _window(_status(tmp_path), 7)
    assert w7["overall"]["reopened"] == 0
    assert w7["reopened_issues"] == []


@pytest.mark.asyncio
async def test_open_without_a_reopen_after_the_finish_is_not_counted(
    tmp_path: Path,
) -> None:
    # The close failed, or the search has not caught up: open, with only a
    # reopen from before the relay finished it. GitHub is asked again only
    # after an hour.
    db = StateDB(tmp_path / "state.db")
    _finish(db)
    clock = Clock()
    gh = FakeGitHub({(REPO, 7): [FINISHED - 600]})
    tracker = ReopenTracker(state_db=db, github=gh, clock=clock)

    assert await tracker.check({(REPO, 7)}) == 0
    clock.now += 1800
    assert await tracker.check({(REPO, 7)}) == 0
    assert len(gh.calls) == 1
    clock.now += 1800
    gh.reopens[(REPO, 7)].append(int(clock.now) - 60)
    assert await tracker.check({(REPO, 7)}) == 1
    assert len(gh.calls) == 2
    db.close()


@pytest.mark.asyncio
async def test_finished_again_and_reopened_again_counts_twice(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    _finish(db, at=FINISHED, pr=70)
    gh = FakeGitHub({(REPO, 7): [FINISHED + 600]})
    tracker = ReopenTracker(state_db=db, github=gh, clock=Clock())
    assert await tracker.check({(REPO, 7)}) == 1
    # Picked up again, a second PR merged, then reopened again.
    _finish(db, at=FINISHED + DAY, pr=71)
    gh.reopens[(REPO, 7)].append(FINISHED + DAY + 600)
    assert await tracker.check({(REPO, 7)}) == 1
    db.close()

    assert _window(_status(tmp_path), 7)["overall"]["reopened"] == 2


@pytest.mark.asyncio
async def test_a_github_failure_is_logged_and_tried_next_poll(
    tmp_path: Path, caplog,
) -> None:
    db = StateDB(tmp_path / "state.db")
    _finish(db)
    gh = FakeGitHub({(REPO, 7): [FINISHED + 600]}, fail=True)
    tracker = ReopenTracker(state_db=db, github=gh, clock=Clock())

    with caplog.at_level(logging.INFO, logger="ctrlrelay.core.reopens"):
        assert await tracker.check({(REPO, 7)}) == 0
    assert any(r.getMessage() == "reopen.check_failed" for r in caplog.records)
    gh.fail = False
    assert await tracker.check({(REPO, 7)}) == 1
    db.close()


@pytest.mark.asyncio
async def test_the_poller_finds_reopens_in_its_own_search(tmp_path: Path) -> None:
    # The issue was handled long ago (seen), so it is not new work; its
    # reopen is still noticed from the same search, and a tracker that
    # fails does not stop the poll.
    db = StateDB(tmp_path / "state.db")
    _finish(db)
    gh = MagicMock()
    gh.list_assigned_issues = AsyncMock(return_value=[
        {"number": 7, "title": "t", "labels": [], "assignees": [{"login": "alice"}]},
    ])
    gh.list_issues_by_label = AsyncMock(return_value=[])
    gh.list_reopen_times = AsyncMock(return_value=[_iso(FINISHED + 600)])
    wire_search(gh)
    poller = IssuePoller(github=gh, username="alice", repos=[REPO],
                         state_file=tmp_path / "poller.json")
    poller.seen_issues = {REPO: {7}}
    poller.reopen_tracker = ReopenTracker(state_db=db, github=gh, clock=Clock())

    assert await poller.poll() == []
    assert db.list_open_finishes() == {}
    gh.list_reopen_times.assert_awaited_once_with(REPO, 7)

    broken = MagicMock()
    broken.list_open_finishes.side_effect = RuntimeError("database is locked")
    poller.reopen_tracker = ReopenTracker(state_db=broken, github=gh, clock=Clock())
    assert await poller.poll() == []
    db.close()


@pytest.mark.asyncio
async def test_a_merge_the_watcher_sees_is_recorded_as_a_finish(tmp_path: Path) -> None:
    from ctrlrelay.pipelines.post_merge import pr_watch_task

    db = StateDB(tmp_path / "state.db")
    gh = AsyncMock()
    gh.get_pr_state.return_value = {"state": "MERGED"}

    for _ in range(2):  # a rehydrated watcher finding the same merge again
        await pr_watch_task(
            repo=REPO, issue_number=7, pr_url="", pr_number=70,
            session_id="s-7", github=gh, transport_factory=None,
            poll_interval=0, timeout=5, state_db=db,
        )

    assert list(db.list_open_finishes()) == [(REPO, 7)]
    rows = db.execute("SELECT pr_number, session_id FROM issue_finishes").fetchall()
    assert [tuple(r) for r in rows] == [(70, "s-7")]
    db.close()


def test_a_new_table_is_filled_from_the_merged_events_kept(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    db.record_event("merged", repo=REPO, issue_number=7, session_id="s-7",
                    detail={"pr_number": 70}, ts=FINISHED)
    db.record_event("merged", repo=REPO, issue_number=9, session_id="s-9",
                    detail={"pr_url": None}, ts=FINISHED)  # no PR number
    db.record_event("done", repo=REPO, issue_number=8, ts=FINISHED)
    # As a database from before this version: no table yet.
    db.execute("DROP TABLE issue_finishes")
    db.commit()
    db.close()

    db = StateDB(tmp_path / "state.db")
    assert db.list_open_finishes() == {(REPO, 7): FINISHED}
    db.close()


def test_a_database_without_the_table_shows_reopened_as_unknown(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    db.execute("DROP TABLE issue_finishes")
    db.commit()
    db.close()

    out = _status(tmp_path)
    for days in (7, 30):
        w = _window(out, days)
        assert w["overall"]["reopened"] is None
        assert "predates" in w["unavailable"]["reopened"]
    assert out["errors"] == []
