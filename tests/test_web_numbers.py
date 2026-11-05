"""Tests for the per-node run numbers on the status page (#230).

Every database is a real ``StateDB``; sessions are inserted with the
statement the pipelines use and updated the way they finish a run, with
every timestamp stamped explicitly. The numbers are read back through
``build_status``, the code ``/api/status`` serves.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ctrlrelay.core.state import StateDB
from ctrlrelay.web import numbers as numbers_module
from ctrlrelay.web.status import build_status

ALPHA = "example-org/alpha"
BETA = "example-org/beta"
DAY = 86_400
NOW = 1_800_000_000
SINCE_7 = NOW - 7 * DAY
SINCE_30 = NOW - 30 * DAY
WAITING_OK = {
    "items": [], "status": "ok", "checked": None, "checked_ts": None,
    "last_good": None, "last_good_ts": None, "error": None,
}
COUNT_KEYS = {
    "started", "done", "failed", "blocked", "running", "other",
    "success_rate", "time_to_pr_s", "time_to_merge_s", "came_back",
    "reopened", "stuck", "cost_per_run",
}


def _db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.db")


def _run(
    db: StateDB,
    sid: str,
    *,
    started_at: int,
    status: str = "done",
    repo: str = ALPHA,
    issue_number: int | None = 1,
    ended_at: int | None = None,
) -> None:
    # The insert dev.py makes, then the update it makes when the run ends.
    db.execute(
        """INSERT INTO sessions
           (id, pipeline, repo, worktree_path, status, started_at, issue_number)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sid, "dev", repo, None, "running", started_at, issue_number),
    )
    if status != "running":
        db.execute(
            "UPDATE sessions SET status = ?, summary = ?, ended_at = ? WHERE id = ?",
            (status, "s", ended_at if ended_at is not None else started_at + 60, sid),
        )
    db.commit()


def _numbers(tmp_path: Path, *, history_days: int = 7, now: int = NOW) -> dict:
    out = build_status(
        db_path=tmp_path / "state.db", waiting=WAITING_OK, version="9",
        host="h", now=now, home=tmp_path, server=lambda: {},
        history_days=history_days,
    )
    return out


def _window(out: dict, days: int) -> dict:
    [w] = [w for w in out["numbers"]["windows"] if w["days"] == days]
    return w


def _repo(window: dict, repo: str) -> dict:
    [r] = [r for r in window["by_repo"] if r["repo"] == repo]
    return r


def test_an_empty_database_is_zero_runs_not_unknown(tmp_path: Path) -> None:
    _db(tmp_path).close()
    out = _numbers(tmp_path)

    assert out["errors"] == []
    assert [w["days"] for w in out["numbers"]["windows"]] == [7, 30]
    w7, w30 = _window(out, 7), _window(out, 30)
    for w in (w7, w30):
        assert set(w["overall"]) == COUNT_KEYS
        assert w["overall"]["started"] == 0
        assert w["overall"]["done"] == 0 and w["overall"]["came_back"] == 0
        # Nothing finished: no rate, not 0% and not 100%.
        assert w["overall"]["success_rate"] is None
        assert w["by_repo"] == [] and w["came_back_issues"] == []
        assert w["overall"]["cost_per_run"] is None
        assert "#223" in w["unavailable"]["cost_per_run"]
        assert w["overall"]["stuck"] == 0
        assert "stuck" not in w["unavailable"]
        # No runs: no PR time is missing, there are none.
        assert w["overall"]["time_to_pr_s"] == {"median": None, "n": 0}
        assert "time_to_pr_s" not in w["unavailable"]
    assert w7["since_ts"] == SINCE_7 and w30["since_ts"] == SINCE_30
    assert w7["until_ts"] == NOW


def test_window_bounds_include_both_ends(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _run(db, "at-7d", started_at=SINCE_7)          # in both windows
    _run(db, "before-7d", started_at=SINCE_7 - 1)  # 30 days only
    _run(db, "at-30d", started_at=SINCE_30)        # 30 days only
    _run(db, "before-30d", started_at=SINCE_30 - 1)  # neither
    _run(db, "now", started_at=NOW, status="running")  # in both
    _run(db, "future", started_at=NOW + 1)          # neither
    db.close()

    out = _numbers(tmp_path)

    assert _window(out, 7)["overall"]["started"] == 2
    assert _window(out, 30)["overall"]["started"] == 4


def test_outcomes_success_rate_and_per_repo(tmp_path: Path) -> None:
    db = _db(tmp_path)
    t = NOW - DAY
    _run(db, "a1", started_at=t, status="done")
    _run(db, "a2", started_at=t, status="done", issue_number=2)
    _run(db, "a3", started_at=t, status="failed", issue_number=3)
    _run(db, "a4", started_at=t, status="running", issue_number=4)
    _run(db, "b1", started_at=t, status="blocked", repo=BETA)
    _run(db, "b2", started_at=t, status="weird", repo=BETA, issue_number=2)
    # Older than 7 days: only the 30-day window sees it.
    _run(db, "b3", started_at=NOW - 10 * DAY, status="failed", repo=BETA, issue_number=3)
    db.close()

    out = _numbers(tmp_path)
    w7, w30 = _window(out, 7), _window(out, 30)

    o = w7["overall"]
    assert (o["started"], o["done"], o["failed"], o["blocked"], o["running"],
            o["other"]) == (6, 2, 1, 1, 1, 1)
    # done / (done + failed + blocked): the running and unknown-status runs
    # are on neither side.
    assert o["success_rate"] == 0.5
    assert [r["repo"] for r in w7["by_repo"]] == [ALPHA, BETA]
    alpha = _repo(w7, ALPHA)
    assert (alpha["started"], alpha["done"], alpha["failed"], alpha["running"]) == (4, 2, 1, 1)
    assert alpha["success_rate"] == round(2 / 3, 3)
    beta = _repo(w7, BETA)
    assert beta["success_rate"] == 0.0
    assert _repo(w30, BETA)["started"] == 3
    assert _repo(w30, BETA)["failed"] == 1
    assert w30["overall"]["success_rate"] == round(2 / 5, 3)


def test_time_to_pr_and_merge_are_medians_from_the_run_start(tmp_path: Path) -> None:
    db = _db(tmp_path)
    t = NOW - DAY
    _run(db, "s1", started_at=t)
    _run(db, "s2", started_at=t, issue_number=2)
    _run(db, "s3", started_at=t, issue_number=3)
    _run(db, "s4", started_at=t, issue_number=4)  # no PR recorded
    # Each run's dispatch, as the pipelines record it: the events reach
    # back to the runs' start, so nothing they need can have been pruned.
    for sid in ("s1", "s2", "s3", "s4"):
        db.record_event("dispatched", session_id=sid, repo=ALPHA, ts=t)
    db.record_event("pr_opened", session_id="s1", repo=ALPHA, ts=t + 100,
                    detail={"pr_number": 10})
    # The first pr_opened counts, not a later one for the same run.
    db.record_event("pr_opened", session_id="s1", repo=ALPHA, ts=t + 5_000,
                    detail={"pr_number": 11})
    db.record_event("pr_opened", session_id="s2", repo=ALPHA, ts=t + 300,
                    detail={"pr_number": 12})
    db.record_event("pr_opened", session_id="s3", repo=ALPHA, ts=t + 1_000,
                    detail={"pr_number": 13})
    db.record_event("merged", session_id="s2", repo=ALPHA, ts=t + 3_600,
                    detail={"pr_number": 12})
    # Other kinds and other sessions do not count.
    db.record_event("ci_started", session_id="s4", repo=ALPHA, ts=t + 50)
    db.record_event("pr_opened", session_id="elsewhere", repo=ALPHA, ts=t + 1)
    db.close()

    out = _numbers(tmp_path, history_days=30)
    for days in (7, 30):
        o = _window(out, days)["overall"]
        assert o["time_to_pr_s"] == {"median": 300, "n": 3}
        assert o["time_to_merge_s"] == {"median": 3_600, "n": 1}
        assert "time_to_pr_s" not in _window(out, days)["unavailable"]
    assert _repo(_window(out, 7), ALPHA)["time_to_pr_s"] == {"median": 300, "n": 3}


def _pruned_at_7_days(tmp_path: Path) -> None:
    """Runs 1 and 20 days ago, each with a PR, then the daemon's prune at
    7 days: the older run's events are gone, its session row is not."""
    db = _db(tmp_path)
    for sid, age, n in (("recent", DAY, 1), ("old", 20 * DAY, 2)):
        _run(db, sid, started_at=NOW - age, issue_number=n)
        db.record_event("dispatched", session_id=sid, ts=NOW - age)
        db.record_event("pr_opened", session_id=sid, ts=NOW - age + 60)
    assert db.prune_events(NOW - 7 * DAY) == 2
    db.close()


@pytest.mark.parametrize("history_days", [7, 30])
def test_raising_history_days_does_not_make_pruned_times_known(
    tmp_path: Path, history_days: int,
) -> None:
    _pruned_at_7_days(tmp_path)

    # Raising web.history_days to 30 brings no deleted event back.
    out = _numbers(tmp_path, history_days=history_days)

    assert _window(out, 7)["overall"]["time_to_pr_s"] == {"median": 60, "n": 1}
    w30 = _window(out, 30)
    assert w30["overall"]["time_to_pr_s"] is None
    assert w30["overall"]["time_to_merge_s"] is None
    assert _repo(w30, ALPHA)["time_to_pr_s"] is None
    reason = w30["unavailable"]["time_to_pr_s"]
    assert "1 of these runs" in reason and "web.history_days" in reason
    # The counts do not need events and are still there.
    assert w30["overall"]["started"] == 2


def test_a_young_node_whose_runs_all_follow_its_first_event_is_known(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    # Nothing was ever pruned: the first event is the first run's dispatch.
    _run(db, "s1", started_at=NOW - 3 * DAY)
    db.record_event("dispatched", session_id="s1", ts=NOW - 3 * DAY)
    db.record_event("pr_opened", session_id="s1", ts=NOW - 3 * DAY + 90)
    db.close()

    out = _numbers(tmp_path)

    for days in (7, 30):
        assert _window(out, days)["overall"]["time_to_pr_s"] == {"median": 90, "n": 1}


def test_runs_with_no_events_kept_have_unknown_times(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _run(db, "s1", started_at=NOW - DAY)
    db.close()

    out = _numbers(tmp_path)

    w7 = _window(out, 7)
    assert w7["overall"]["time_to_pr_s"] is None
    assert "no events are kept" in w7["unavailable"]["time_to_pr_s"]


def test_an_event_stamped_after_now_is_not_read(tmp_path: Path) -> None:
    db = _db(tmp_path)
    t = NOW - DAY
    _run(db, "s1", started_at=t)
    _run(db, "s2", started_at=t, issue_number=2)
    db.record_event("dispatched", session_id="s1", ts=t)
    db.record_event("pr_opened", session_id="s1", ts=t + 100)
    # A clock that was ahead, since corrected: the only PR event s2 has.
    db.record_event("pr_opened", session_id="s2", ts=NOW + 3_600)
    db.record_event("merged", session_id="s2", ts=NOW + 7_200)
    db.close()

    o = _window(_numbers(tmp_path), 7)["overall"]

    assert o["time_to_pr_s"] == {"median": 100, "n": 1}
    assert o["time_to_merge_s"] == {"median": None, "n": 0}


def test_without_an_events_table_the_times_are_unknown_and_counts_stay(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _run(db, "s1", started_at=NOW - DAY)
    db.execute("DROP TABLE events")
    db.commit()
    db.close()

    out = _numbers(tmp_path, history_days=30)

    assert any(e.startswith("events:") for e in out["errors"])
    for days in (7, 30):
        w = _window(out, days)
        assert w["overall"]["started"] == 1 and w["overall"]["done"] == 1
        assert w["overall"]["time_to_pr_s"] is None
        assert w["overall"]["time_to_merge_s"] is None
        assert w["unavailable"]["time_to_pr_s"] == numbers_module.EVENTS_GAP


def test_a_missing_database_has_no_numbers(tmp_path: Path) -> None:
    out = build_status(
        db_path=tmp_path / "nope" / "state.db", waiting=WAITING_OK, version="9",
        host="h", now=NOW, home=tmp_path, server=lambda: {},
    )
    assert out["numbers"] is None
    assert any("state.db" in e for e in out["errors"])


def test_a_session_row_that_cannot_be_shaped_voids_the_numbers(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _run(db, "ok", started_at=NOW - DAY)
    # A real number stored as a float: SQLite keeps it, the filter admits it.
    db.execute(
        "INSERT INTO sessions (id, pipeline, repo, status, started_at) "
        "VALUES ('bad', 'dev', ?, 'done', ?)", (ALPHA, float(NOW - DAY) + 0.5),
    )
    db.commit()
    db.close()

    out = _numbers(tmp_path)

    assert out["numbers"] is None
    assert any(e.startswith("numbers:") for e in out["errors"])


def test_an_issue_dispatched_again_after_a_done_run_came_back(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Done long ago (outside both windows), then dispatched again 2 days ago.
    _run(db, "old-done", started_at=NOW - 40 * DAY, issue_number=7,
         ended_at=NOW - 40 * DAY + 60)
    _run(db, "again", started_at=NOW - 2 * DAY, issue_number=7, status="failed")
    # Twice more inside the window: still one issue.
    _run(db, "again2", started_at=NOW - DAY, issue_number=7, status="running")
    # Done 20 days ago, again 10 days ago: the 30-day window only.
    _run(db, "b-done", started_at=NOW - 20 * DAY, repo=BETA, issue_number=8)
    _run(db, "b-again", started_at=NOW - 10 * DAY, repo=BETA, issue_number=8)
    # An earlier run that FAILED is a retry, not a fix coming back.
    _run(db, "c-failed", started_at=NOW - 3 * DAY, issue_number=9, status="failed")
    _run(db, "c-retry", started_at=NOW - 2 * DAY, issue_number=9)
    # A done run that ended AFTER the next one started overlapped it.
    _run(db, "d-done", started_at=NOW - 3 * DAY, issue_number=10,
         ended_at=NOW - DAY)
    _run(db, "d-other", started_at=NOW - 2 * DAY, issue_number=10)
    # Same number, different repo: not the same issue.
    _run(db, "e-done", started_at=NOW - 3 * DAY, repo=BETA, issue_number=11)
    _run(db, "e-other", started_at=NOW - 2 * DAY, issue_number=11)
    # No issue (a secops sweep) never comes back.
    _run(db, "f-done", started_at=NOW - 3 * DAY, issue_number=None)
    _run(db, "f-again", started_at=NOW - 2 * DAY, issue_number=None)
    db.close()

    out = _numbers(tmp_path)
    w7, w30 = _window(out, 7), _window(out, 30)

    assert w7["overall"]["came_back"] == 1
    assert w7["came_back_issues"] == [{
        "repo": ALPHA, "issue_number": 7,
        "url": f"https://github.com/{ALPHA}/issues/7",
    }]
    assert _repo(w7, ALPHA)["came_back"] == 1
    assert w30["overall"]["came_back"] == 2
    assert [(i["repo"], i["issue_number"]) for i in w30["came_back_issues"]] == [
        (ALPHA, 7), (BETA, 8),
    ]
    assert _repo(w30, BETA)["came_back"] == 1


# --- the page ------------------------------------------------------------------


def _page_number_cells(counts: dict) -> list[str]:
    from tests.test_web_server import _js_function, _script

    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("node is not installed, so the page's script cannot run here")
    script = _script()
    funcs = "\n".join(
        f"function {name}({args}) {{{_js_function(script, name)}}}"
        for name, args in (("duration", "seconds"), ("medianText", "m"),
                           ("countText", "v"), ("numberCells", "c"))
    )
    program = funcs + (
        "\nprocess.stdout.write(JSON.stringify(numberCells("
        + json.dumps(counts) + ")));"
    )
    done = subprocess.run([node, "-e", program], capture_output=True, text=True,
                          timeout=30, check=True)
    return json.loads(done.stdout)


def test_page_shows_unknowns_as_unknown_never_zero(tmp_path: Path) -> None:
    _pruned_at_7_days(tmp_path)
    out = _numbers(tmp_path)
    cells_30 = _page_number_cells(_window(out, 30)["overall"])
    # runs, done, failed, blocked, running, stuck, success, PR, merge,
    # back, reopened, cost
    assert cells_30 == [
        "2", "2", "0", "0", "0", "0", "100%", "unknown", "unknown", "0", "0",
        "not recorded",
    ]
    (tmp_path / "state.db").unlink()
    _db(tmp_path).close()
    empty_7 = _window(_numbers(tmp_path), 7)["overall"]
    assert _page_number_cells(empty_7)[6:9] == [
        "none finished", "none yet", "none yet",
    ]
    assert _page_number_cells({}) == ["unknown"] * 11 + ["not recorded"]


def test_page_shows_rates_and_medians(tmp_path: Path) -> None:
    counts = {
        "started": 4, "done": 2, "failed": 1, "blocked": 1, "running": 0,
        "stuck": 1, "success_rate": 0.5, "time_to_pr_s": {"median": 300, "n": 3},
        "time_to_merge_s": {"median": 7_200, "n": 1}, "came_back": 1,
        "reopened": None, "cost_per_run": None,
    }
    # A reopen count that could not be read is unknown, not 0.
    assert _page_number_cells(counts) == [
        "4", "2", "1", "1", "0", "1", "50%", "5m (n=3)", "2h 0m (n=1)", "1",
        "unknown", "not recorded",
    ]
    assert _page_number_cells({**counts, "reopened": 2})[10] == "2"


def test_page_renders_the_numbers_section() -> None:
    from tests.test_web_server import _js_function, _page_source, _script

    assert 'id="numbers"' in _page_source()
    render = _js_function(_script(), "render")
    assert "renderNumbers(data)" in render
    body = _js_function(_script(), "renderNumbers")
    # A null section is drawn as unknown, not as an empty table.
    assert 'unknown("unknown: the runs could not be read")' in body


def test_a_run_stopped_as_stuck_is_counted_and_also_failed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _run(db, "a", started_at=NOW - DAY, status="failed")
    _run(db, "b", started_at=NOW - DAY, status="failed")
    _run(db, "c", started_at=NOW - DAY, status="done")
    # The update dev.py makes for a stuck run (#224).
    db.execute(
        "UPDATE sessions SET stuck_reason = ? WHERE id = ?",
        ("the agent ran the same command 5 times in a row", "b"),
    )
    db.commit()
    db.close()

    w = _window(_numbers(tmp_path), 7)
    assert w["overall"]["stuck"] == 1
    assert w["overall"]["failed"] == 2
    assert _repo(w, ALPHA)["stuck"] == 1
    assert "stuck" not in w["unavailable"]


def test_a_database_without_the_stuck_record_says_unknown(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _run(db, "a", started_at=NOW - DAY, status="failed")
    db.execute("ALTER TABLE sessions DROP COLUMN stuck_reason")
    db.commit()
    db._conn.close()

    w = _window(_numbers(tmp_path), 7)
    assert w["overall"]["stuck"] is None
    assert "#224" in w["unavailable"]["stuck"]
