"""#282: one search per poll, not one GitHub call per repo; a trigger label
counts only when the operator added it."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ctrlrelay.core.github import GitHubError
from ctrlrelay.core.poller import IssuePoller

ME = "alice"


def _issue(n: int, *, labels=(), assignees=(ME,)) -> dict:
    return {"number": n, "title": f"t{n}", "labels": [{"name": x} for x in labels],
            "assignees": [{"login": a} for a in assignees]}


def _gh(assigned: dict, labeled: dict | None = None, *, label_by: str = ME) -> MagicMock:
    gh = MagicMock()

    async def search(*, repos, assignee=None, label=None, limit=1000):
        src = assigned if assignee is not None else (labeled or {}).get(label, {})
        if isinstance(src, Exception):
            raise src
        return {r: list(src.get(r, [])) for r in repos}

    gh.search_open_issues = AsyncMock(side_effect=search)
    gh.list_assignment_events = AsyncMock(return_value=[{
        "event": "assigned", "actor": {"login": ME}, "assignee": {"login": ME},
        "assigner": {"login": ME},
    }])
    gh.list_label_events = AsyncMock(return_value=[
        {"event": "labeled", "actor": {"login": label_by}, "label": {"name": "go"}},
    ])
    gh.repo_is_archived = AsyncMock(return_value=False)
    gh.list_assigned_issues = AsyncMock(side_effect=AssertionError("per-repo call"))
    gh.list_issues_by_label = AsyncMock(side_effect=AssertionError("per-repo call"))
    return gh


def _poller(gh, tmp_path: Path, repos, **kw) -> IssuePoller:
    return IssuePoller(github=gh, username=ME, repos=repos,
                       state_file=tmp_path / "s.json", **kw)


class TestOneSearchPerPoll:
    @pytest.mark.asyncio
    async def test_eighty_seven_repos_cost_one_search_and_no_per_repo_calls(
        self, tmp_path: Path,
    ) -> None:
        repos = [f"o/r{i}" for i in range(87)]
        gh = _gh({"o/r5": [_issue(1)]})
        out = await _poller(gh, tmp_path, repos).poll()

        assert [(x["repo"], x["issue"]["number"]) for x in out] == [("o/r5", 1)]
        assert gh.search_open_issues.await_count == 1
        # Only the repo with something in it is asked whether it is archived.
        assert [c.args[0] for c in gh.repo_is_archived.await_args_list] == ["o/r5"]

    @pytest.mark.asyncio
    async def test_a_failed_search_changes_nothing_and_the_next_poll_recovers(
        self, tmp_path: Path,
    ) -> None:
        gh = _gh(GitHubError("gh failed: HTTP 502"))
        poller = _poller(gh, tmp_path, ["o/a"])
        poller.mark_seen("o/a", 9)
        before = (tmp_path / "s.json").read_text()

        with pytest.raises(GitHubError):
            await poller.poll()
        assert poller.seen_issues == {"o/a": {9}}
        assert (tmp_path / "s.json").read_text() == before

        gh.search_open_issues.side_effect = None
        gh.search_open_issues.return_value = {"o/a": [_issue(10)]}
        out = await poller.poll()
        assert [x["issue"]["number"] for x in out] == [10]

    @pytest.mark.asyncio
    async def test_one_search_per_distinct_label_and_a_failed_one_stays_local(
        self, tmp_path: Path,
    ) -> None:
        gh = _gh({}, {"go": {"o/a": [_issue(1, labels=["go"], assignees=())]},
                      "stop": GitHubError("gh failed: HTTP 502")})
        poller = _poller(gh, tmp_path, ["o/a", "o/b", "o/c"],
                         include_labels_by_repo={"o/a": ["go"], "o/b": ["Go", "stop"],
                                                 "o/c": ["stop"]})
        out = await poller.poll()

        assert [(x["repo"], x["issue"]["number"]) for x in out] == [("o/a", 1)]
        labels = sorted(c.kwargs["label"] for c in gh.search_open_issues.await_args_list
                        if c.kwargs.get("label"))
        assert labels == ["go", "stop"]  # "go" and "Go" are one search

    @pytest.mark.asyncio
    async def test_a_failed_search_at_startup_seeds_nothing(self, tmp_path: Path) -> None:
        gh = _gh(GitHubError("gh failed: HTTP 502"))
        poller = _poller(gh, tmp_path, ["o/a"])
        with patch("ctrlrelay.core.poller._SEED_LABEL_RETRY_SLEEP_SECONDS", 0):
            await poller.seed_current()
        assert poller.seen_issues == {}


class TestWhoAddedTheLabel:
    def _labeled(self, tmp_path, *, label_by, assignees=()):
        gh = _gh({"o/a": [_issue(1, labels=["go"], assignees=assignees)]} if assignees else {},
                 {"go": {"o/a": [_issue(1, labels=["go"], assignees=assignees)]}},
                 label_by=label_by)
        return gh, _poller(gh, tmp_path, ["o/a"], include_labels_by_repo={"o/a": ["go"]})

    @pytest.mark.asyncio
    async def test_a_label_the_operator_added_is_taken(self, tmp_path: Path) -> None:
        _, poller = self._labeled(tmp_path, label_by=ME)
        assert [x["issue"]["number"] for x in await poller.poll()] == [1]

    @pytest.mark.asyncio
    async def test_a_label_someone_else_added_is_refused_and_left_unmarked(
        self, tmp_path: Path,
    ) -> None:
        _, poller = self._labeled(tmp_path, label_by="mallory")
        assert await poller.poll() == []
        assert 1 not in poller.seen_issues.get("o/a", set())

    @pytest.mark.asyncio
    async def test_someone_elses_label_on_an_issue_the_operator_self_assigned_is_taken(
        self, tmp_path: Path,
    ) -> None:
        _, poller = self._labeled(tmp_path, label_by="mallory", assignees=(ME,))
        assert [x["issue"]["number"] for x in await poller.poll()] == [1]

    @pytest.mark.asyncio
    async def test_a_failed_label_lookup_is_not_a_yes(self, tmp_path: Path) -> None:
        gh, poller = self._labeled(tmp_path, label_by=ME)
        gh.list_label_events.side_effect = GitHubError("gh failed: HTTP 502")
        assert await poller.poll() == []


class TestTheSearchCall:
    @pytest.mark.asyncio
    async def test_scoped_to_owners_kept_to_configured_repos_case_blind(self) -> None:
        from ctrlrelay.core.github import GitHubCLI

        rows = [
            {"number": 1, "repository": {"nameWithOwner": "Org/Repo"}},
            {"number": 2, "repository": {"nameWithOwner": "org/other"}},
        ]
        with patch.object(GitHubCLI, "_run_gh", AsyncMock(return_value=json.dumps(rows))) as run:
            out = await GitHubCLI().search_open_issues(
                repos=["org/repo", "me/x"], assignee="alice",
            )
        assert out == {"org/repo": [rows[0]], "me/x": []}
        args = run.await_args.args
        assert args[:2] == ("search", "issues")
        owners = [args[i + 1] for i, a in enumerate(args) if a == "--owner"]
        assert owners == ["me", "org"]
        assert args[args.index("--assignee") + 1] == "alice"

    @pytest.mark.asyncio
    async def test_a_full_page_is_refused_as_maybe_cut_short(self) -> None:
        from ctrlrelay.core.github import GitHubCLI

        rows = [{"number": i, "repository": {"nameWithOwner": "o/a"}} for i in range(5)]
        with patch.object(GitHubCLI, "_run_gh", AsyncMock(return_value=json.dumps(rows))):
            with pytest.raises(GitHubError, match="incomplete"):
                await GitHubCLI().search_open_issues(repos=["o/a"], label="go", limit=5)
