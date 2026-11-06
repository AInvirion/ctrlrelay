"""Each poll's batch runs in priority order, oldest first (#356).

Driven through the real ``run_poll_loop``: the fake GitHub answers the
poll's account-wide search (``search_open_issues``, the one call both intake
paths use) in a deliberately scrambled order, and the test reads the order
the handler was called in. Rows carry ``labels`` and ``createdAt`` as the
real search does (``_SEARCH_PAGE_JQ``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from ctrlrelay.core.config import Config, IntakeConfig
from ctrlrelay.core.poller import IssuePoller, run_poll_loop

REPO = "owner/repo"
OTHER = "owner/other"


def row(
    number: int, *labels: str, created: str | None = "2026-01-01T00:00:00Z",
    repo: str = REPO,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "number": number, "title": f"issue {number}", "state": "open",
        "body": "", "labels": [{"name": x} for x in labels],
        "assignees": [{"login": "alice"}], "repo": repo,
        "updatedAt": "2026-01-01T00:00:00Z",
    }
    if created is not None:
        out["createdAt"] = created
    return out


def github(
    assigned: list[dict[str, Any]],
    labelled: dict[str, list[dict[str, Any]]] | None = None,
) -> MagicMock:
    """A fake GitHub. ``assigned`` answers the assignee search,
    ``labelled`` (trigger label -> rows) the per-label searches, both in
    the order given. The re-read before dispatch (#326) echoes the row."""
    rows = {(r["repo"], r["number"]): r for r in assigned}
    for found in (labelled or {}).values():
        rows.update({(r["repo"], r["number"]): r for r in found})

    async def search(*, repos, assignee=None, label=None):
        source = assigned if assignee is not None else (labelled or {}).get(label, [])
        out: dict[str, list[dict[str, Any]]] = {r: [] for r in repos}
        for r in source:
            if r["repo"] in out:
                out[r["repo"]].append(dict(r))
        return out

    async def gate(repo: str, number: int) -> dict[str, Any]:
        r = rows[(repo, number)]
        return {
            "state": "open",
            "labels": [x["name"] for x in r["labels"]],
            "assignees": [x["login"] for x in r["assignees"]],
        }

    gh = MagicMock()
    gh.search_open_issues = AsyncMock(side_effect=search)
    gh.list_assignment_events = AsyncMock(return_value=[{
        "event": "assigned", "actor": {"login": "alice"},
        "assignee": {"login": "alice"}, "assigner": {"login": "alice"},
        "created_at": "2026-01-01T00:00:00Z",
    }])
    gh.list_label_events = AsyncMock(side_effect=lambda repo, n: [
        {"event": "labeled", "actor": {"login": "alice"}, "label": {"name": x}}
        for x in (labelled or {})
    ])
    gh.get_issue_gate = AsyncMock(side_effect=gate)
    gh.repo_is_archived = AsyncMock(return_value=False)
    return gh


async def run_once(
    gh: MagicMock, tmp_path: Path, **kw: Any,
) -> list[tuple[str, int]]:
    """One real poll-loop iteration; the (repo, number) pairs in the order
    the handler was called."""
    kw.setdefault("repos", [REPO])
    poller = IssuePoller(
        github=gh, username="alice",
        state_file=tmp_path / "poller_state.json", **kw,
    )
    handled: list[tuple[str, int]] = []

    async def handler(repo: str, issue: dict[str, Any]) -> None:
        handled.append((repo, issue["number"]))

    await run_poll_loop(poller, handler, interval=0, max_iterations=1)
    return handled


def numbers(handled: list[tuple[str, int]]) -> list[int]:
    return [n for _, n in handled]


@pytest.mark.asyncio
async def test_a_mixed_batch_runs_p1_p2_p3_then_unlabeled(tmp_path: Path) -> None:
    gh = github([row(3, "P3"), row(4), row(1, "P1"), row(2, "P2")])

    assert numbers(await run_once(gh, tmp_path)) == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_the_same_label_runs_oldest_first_then_by_number(
    tmp_path: Path,
) -> None:
    gh = github([
        row(10, "P2", created="2026-03-01T00:00:00Z"),
        row(30, "P2", created="2026-01-01T00:00:00Z"),
        row(21, "P2", created="2026-02-01T00:00:00Z"),
        row(20, "P2", created="2026-02-01T00:00:00Z"),
    ])

    assert numbers(await run_once(gh, tmp_path)) == [30, 20, 21, 10]


@pytest.mark.asyncio
async def test_labels_are_compared_without_case(tmp_path: Path) -> None:
    gh = github([row(1, "p3"), row(2, "bug"), row(3, "p1")])

    assert numbers(await run_once(
        gh, tmp_path, priority_labels=["P1", "P3"],
    )) == [3, 1, 2]


@pytest.mark.asyncio
async def test_an_issue_with_two_priority_labels_runs_at_the_earlier(
    tmp_path: Path,
) -> None:
    gh = github([
        row(1, "P2", created="2026-01-01T00:00:00Z"),
        row(2, "P3", "P1", created="2026-06-01T00:00:00Z"),
    ])

    assert numbers(await run_once(gh, tmp_path)) == [2, 1]


@pytest.mark.asyncio
async def test_an_empty_list_keeps_oldest_first_only(tmp_path: Path) -> None:
    gh = github([
        row(1, "P0", created="2026-05-01T00:00:00Z"),
        row(2, created="2026-01-01T00:00:00Z"),
        row(3, "P3", created="2026-03-01T00:00:00Z"),
    ])

    assert numbers(await run_once(gh, tmp_path, priority_labels=[])) == [2, 3, 1]


@pytest.mark.asyncio
async def test_a_custom_list_replaces_the_default(tmp_path: Path) -> None:
    gh = github([row(1, "P0"), row(2, "later"), row(3, "urgent")])

    # P0 is not in the custom list, so issue 1 counts as unlabeled.
    assert numbers(await run_once(
        gh, tmp_path, priority_labels=["urgent", "later"],
    )) == [3, 2, 1]


@pytest.mark.asyncio
async def test_rows_without_created_at_go_after_their_label_by_number(
    tmp_path: Path,
) -> None:
    gh = github([
        row(9, "P1", created=None),
        row(5, "P1", created="not a date"),
        row(7, "P1", created="2026-09-01T00:00:00Z"),
        row(8, "P2", created="2026-01-01T00:00:00Z"),
    ])

    assert numbers(await run_once(gh, tmp_path)) == [7, 5, 9, 8]


@pytest.mark.asyncio
async def test_both_intake_paths_and_repos_are_sorted_together(
    tmp_path: Path,
) -> None:
    gh = github(
        [row(1, "P3", repo=REPO), row(2, repo=OTHER)],
        labelled={"ctrlrelay:auto": [
            row(3, "ctrlrelay:auto", repo=OTHER),
            row(4, "ctrlrelay:auto", "P0", repo=OTHER),
        ]},
    )

    handled = await run_once(
        gh, tmp_path, repos=[REPO, OTHER],
        include_labels_by_repo={OTHER: ["ctrlrelay:auto"]},
    )

    assert handled == [(OTHER, 4), (REPO, 1), (OTHER, 2), (OTHER, 3)]


@pytest.mark.asyncio
async def test_the_order_is_logged_once_per_poll(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    gh = github([row(3, "P3"), row(4), row(1, "P1"), row(2, "P2")])

    with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
        await run_once(gh, tmp_path)

    records = [r for r in caplog.records if r.getMessage() == "poll.batch.order"]
    assert len(records) == 1
    assert records[0].order == [f"{REPO}#1", f"{REPO}#2", f"{REPO}#3", f"{REPO}#4"]
    assert records[0].count == 4
    assert records[0].priority_labels == ["P0", "P1", "P2", "P3"]


@pytest.mark.asyncio
async def test_an_empty_poll_logs_no_order(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    gh = github([])

    with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
        await run_once(gh, tmp_path)

    assert not [r for r in caplog.records if r.getMessage() == "poll.batch.order"]


class TestIntakeConfig:
    def test_default_is_p0_to_p3(self) -> None:
        assert IntakeConfig().priority_labels == ["P0", "P1", "P2", "P3"]

    def test_the_root_config_carries_it(self, sample_config_dict: dict) -> None:
        assert Config(**sample_config_dict).intake.priority_labels == [
            "P0", "P1", "P2", "P3",
        ]
        sample_config_dict["intake"] = {"priority_labels": ["urgent"]}
        assert Config(**sample_config_dict).intake.priority_labels == ["urgent"]

    def test_an_empty_list_is_accepted(self) -> None:
        assert IntakeConfig(priority_labels=[]).priority_labels == []

    def test_labels_are_stripped(self) -> None:
        assert IntakeConfig(priority_labels=[" P1 ", "P2"]).priority_labels == [
            "P1", "P2",
        ]

    @pytest.mark.parametrize("bad", [
        ["P1", "p1"],
        ["P1", ""],
        ["   "],
        "P1",
        [1],
    ])
    def test_bad_lists_are_refused(self, bad: Any) -> None:
        with pytest.raises(ValidationError):
            IntakeConfig(priority_labels=bad)

    def test_unknown_keys_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            IntakeConfig(priority_label=["P1"])
