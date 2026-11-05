"""The poll loop reads each issue again before handling it (#326).

``poll()`` returns a batch and the loop handles it one issue at a time. With
one slot and long runs, the last issue in a batch can start an hour after the
poll saw it, after the operator removed its trigger label or closed it. These
tests change the issue on "GitHub" while the batch's first handler is running,
which is exactly the window the re-read exists for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.poller import IssuePoller, run_poll_loop
from tests._poller_fakes import wire_search

REPO = "owner/repo"
AUTO = "ctrlrelay:auto"


def _github(live: dict[int, dict[str, Any]]) -> MagicMock:
    """A fake GitHub whose issues are the ``live`` dict, read at call time:
    the poll's search and the re-read see whatever the dict holds then."""
    gh = MagicMock()

    def as_issue(n: int) -> dict[str, Any]:
        v = live[n]
        return {
            "number": n, "title": f"issue {n}", "state": v["state"],
            "labels": [{"name": x} for x in v["labels"]],
            "assignees": [{"login": x} for x in v["assignees"]],
        }

    async def assigned(repo: str, assignee: str) -> list[dict[str, Any]]:
        return [as_issue(n) for n, v in sorted(live.items())
                if v["state"] == "open" and assignee in v["assignees"]]

    async def labelled(repo: str, label: str) -> list[dict[str, Any]]:
        return [as_issue(n) for n, v in sorted(live.items())
                if v["state"] == "open" and label in v["labels"]]

    async def gate(repo: str, n: int) -> dict[str, Any]:
        v = live[n]
        return {"state": v["state"], "labels": list(v["labels"]),
                "assignees": list(v["assignees"])}

    gh.list_assigned_issues = AsyncMock(side_effect=assigned)
    gh.list_issues_by_label = AsyncMock(side_effect=labelled)
    gh.list_assignment_events = AsyncMock(return_value=[{
        "event": "assigned", "actor": {"login": "alice"},
        "assignee": {"login": "alice"}, "assigner": {"login": "alice"},
        "created_at": "2026-01-01T00:00:00Z",
    }])
    gh.get_issue_gate = AsyncMock(side_effect=gate)
    return wire_search(gh)


def _poller(gh: MagicMock, tmp_path: Path, **kw: Any) -> IssuePoller:
    return IssuePoller(
        github=gh, username="alice", repos=[REPO],
        state_file=tmp_path / "poller_state.json",
        require_labels_by_repo={REPO: [AUTO]}, **kw,
    )


def _live() -> dict[int, dict[str, Any]]:
    return {
        1: {"state": "open", "labels": [AUTO], "assignees": ["alice"]},
        2: {"state": "open", "labels": [AUTO], "assignees": ["alice"]},
    }


async def _run_batch(
    poller: IssuePoller, change_second: Any,
) -> list[dict[str, Any]]:
    """Run one poll iteration; ``change_second`` runs inside the handler
    for issue 1, before issue 2's turn. Returns what the handler got."""
    handled: list[dict[str, Any]] = []

    async def handler(repo: str, issue: dict[str, Any]) -> None:
        handled.append(issue)
        if issue["number"] == 1:
            change_second()

    await run_poll_loop(poller, handler, interval=0, max_iterations=1)
    return handled


@pytest.mark.asyncio
async def test_an_issue_that_loses_its_required_label_does_not_start(
    tmp_path: Path,
) -> None:
    live = _live()
    gh = _github(live)
    poller = _poller(gh, tmp_path)

    handled = await _run_batch(
        poller, lambda: live[2]["labels"].remove(AUTO),
    )

    assert [i["number"] for i in handled] == [1]
    assert 2 not in poller.seen_issues.get(REPO, set())
    assert 1 in poller.seen_issues[REPO]


@pytest.mark.asyncio
async def test_an_issue_closed_after_the_poll_does_not_start(
    tmp_path: Path,
) -> None:
    live = _live()
    gh = _github(live)
    poller = _poller(gh, tmp_path)

    handled = await _run_batch(
        poller, lambda: live[2].update(state="closed"),
    )

    assert [i["number"] for i in handled] == [1]
    assert 2 not in poller.seen_issues.get(REPO, set())


@pytest.mark.asyncio
async def test_an_issue_unassigned_after_the_poll_does_not_start(
    tmp_path: Path,
) -> None:
    live = _live()
    gh = _github(live)
    poller = _poller(gh, tmp_path)

    handled = await _run_batch(
        poller, lambda: live[2]["assignees"].clear(),
    )

    assert [i["number"] for i in handled] == [1]
    assert 2 not in poller.seen_issues.get(REPO, set())


@pytest.mark.asyncio
async def test_an_issue_given_an_exclude_label_after_the_poll_does_not_start(
    tmp_path: Path,
) -> None:
    live = _live()
    gh = _github(live)
    poller = _poller(gh, tmp_path, exclude_labels_by_repo={REPO: ["manual"]})

    handled = await _run_batch(
        poller, lambda: live[2]["labels"].append("Manual"),
    )

    assert [i["number"] for i in handled] == [1]


@pytest.mark.asyncio
async def test_an_unchanged_issue_still_starts_with_todays_labels(
    tmp_path: Path,
) -> None:
    """The control: nothing changed, so both start; and the handler routes
    on the labels read now, not the poll's."""
    live = _live()
    gh = _github(live)
    poller = _poller(gh, tmp_path)

    handled = await _run_batch(
        poller, lambda: live[2]["labels"].append("task"),
    )

    assert [i["number"] for i in handled] == [1, 2]
    assert {x["name"] for x in handled[1]["labels"]} == {AUTO, "task"}
    assert gh.get_issue_gate.await_count == 2


@pytest.mark.asyncio
async def test_an_issue_that_cannot_be_read_again_waits_for_the_next_poll(
    tmp_path: Path,
) -> None:
    live = _live()
    gh = _github(live)
    poller = _poller(gh, tmp_path)
    real_gate = gh.get_issue_gate.side_effect

    async def flaky(repo: str, n: int) -> dict[str, Any]:
        if n == 2:
            raise RuntimeError("gh: HTTP 502")
        return await real_gate(repo, n)

    gh.get_issue_gate.side_effect = flaky

    handled = await _run_batch(poller, lambda: None)

    assert [i["number"] for i in handled] == [1]
    assert 2 not in poller.seen_issues.get(REPO, set())

    # It was only skipped, not dropped: the next poll offers it again.
    gh.get_issue_gate.side_effect = real_gate
    again = await _run_batch(poller, lambda: None)
    assert [i["number"] for i in again] == [2]


@pytest.mark.asyncio
async def test_a_label_triggered_issue_whose_label_is_removed_does_not_start(
    tmp_path: Path,
) -> None:
    """On an ``include_labels`` repo an unassigned issue starts on the label
    alone, so losing the label leaves it no trigger."""
    live = {
        1: {"state": "open", "labels": ["go"], "assignees": []},
        2: {"state": "open", "labels": ["go"], "assignees": []},
    }
    gh = _github(live)
    poller = IssuePoller(
        github=gh, username="alice", repos=[REPO],
        state_file=tmp_path / "poller_state.json",
        include_labels_by_repo={REPO: ["go"]},
    )

    handled = await _run_batch(poller, lambda: live[2]["labels"].clear())

    assert [i["number"] for i in handled] == [1]
    assert 2 not in poller.seen_issues.get(REPO, set())
