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
    def _page(self, rows, *, total=None, incomplete=False) -> str:
        return json.dumps({"incomplete": incomplete,
                           "total": len(rows) if total is None else total,
                           "items": rows})

    @pytest.mark.asyncio
    async def test_scoped_to_owners_kept_to_configured_repos_case_blind(self) -> None:
        from ctrlrelay.core.github import GitHubCLI

        rows = [{"number": 1, "repo": "Org/Repo"}, {"number": 2, "repo": "org/other"}]
        with patch.object(GitHubCLI, "_run_gh",
                          AsyncMock(return_value=self._page(rows))) as run:
            out = await GitHubCLI().search_open_issues(
                repos=["org/repo", "me/x"], assignee="alice",
            )
        assert out == {"org/repo": [rows[0]], "me/x": []}
        args = run.await_args.args
        assert args[:4] == ("api", "-X", "GET", "search/issues")
        q = args[args.index("q=is:issue is:open assignee:alice user:me user:org")]
        assert q

    @pytest.mark.asyncio
    @pytest.mark.parametrize("output", [
        "", "not json\n",
        json.dumps({"incomplete": True, "total": 1, "items": [{"number": 1, "repo": "o/a"}]}),
        json.dumps({"incomplete": False, "total": 1500, "items": [{"number": 1, "repo": "o/a"}]}),
    ])
    async def test_an_answer_that_may_be_partial_is_refused(self, output) -> None:
        from ctrlrelay.core.github import GitHubCLI

        with patch.object(GitHubCLI, "_run_gh", AsyncMock(return_value=output)):
            with pytest.raises((GitHubError, ValueError)):
                await GitHubCLI().search_open_issues(repos=["o/a"], label="go")

    @pytest.mark.asyncio
    async def test_pages_are_joined(self) -> None:
        from ctrlrelay.core.github import GitHubCLI

        p1 = self._page([{"number": 1, "repo": "o/a"}], total=2)
        p2 = self._page([{"number": 2, "repo": "o/a"}], total=2)
        with patch.object(GitHubCLI, "_run_gh", AsyncMock(return_value=p1 + "\n" + p2)):
            out = await GitHubCLI().search_open_issues(repos=["o/a"], assignee="alice")
        assert [r["number"] for r in out["o/a"]] == [1, 2]


class TestRoundOneFixes:
    @pytest.mark.asyncio
    async def test_a_failed_startup_seed_is_finished_before_any_poll(
        self, tmp_path: Path,
    ) -> None:
        gh = _gh(GitHubError("gh failed: HTTP 502"))
        poller = _poller(gh, tmp_path, ["o/a"])
        with patch("ctrlrelay.core.poller._SEED_LABEL_RETRY_SLEEP_SECONDS", 0):
            await poller.seed_current()
            with pytest.raises(RuntimeError, match="seed"):
                await poller.poll()
            # GitHub answers again: the poll seeds first, so the issue that
            # was already there is not new.
            gh.search_open_issues.side_effect = None
            gh.search_open_issues.return_value = {"o/a": [_issue(1)]}
            assert await poller.poll() == []
        assert 1 in poller.seen_issues["o/a"]

    @pytest.mark.asyncio
    async def test_any_trigger_label_the_operator_added_counts(self, tmp_path: Path) -> None:
        gh = _gh({}, {"go": {"o/a": [_issue(1, labels=["go", "now"], assignees=())]},
                      "now": {"o/a": [_issue(1, labels=["go", "now"], assignees=())]}})
        gh.list_label_events.return_value = [
            {"event": "labeled", "actor": {"login": "mallory"}, "label": {"name": "go"}},
            {"event": "labeled", "actor": {"login": ME}, "label": {"name": "now"}},
        ]
        poller = _poller(gh, tmp_path, ["o/a"], include_labels_by_repo={"o/a": ["go", "now"]})
        assert [x["issue"]["number"] for x in await poller.poll()] == [1]

    @pytest.mark.asyncio
    async def test_accepting_foreign_assignments_does_not_accept_foreign_labels(
        self, tmp_path: Path,
    ) -> None:
        gh = _gh({}, {"go": {"o/a": [_issue(1, labels=["go"], assignees=())]}},
                 label_by="mallory")
        poller = _poller(gh, tmp_path, ["o/a"], include_labels_by_repo={"o/a": ["go"]},
                         accept_foreign_assignments={"o/a"})
        assert await poller.poll() == []

    @pytest.mark.asyncio
    async def test_no_archive_probe_when_every_issue_is_already_handled(
        self, tmp_path: Path,
    ) -> None:
        gh = _gh({"o/a": [_issue(1)]})
        poller = _poller(gh, tmp_path, ["o/a"])
        poller.mark_seen("o/a", 1)
        assert await poller.poll() == []
        gh.repo_is_archived.assert_not_awaited()


class TestRoundTwoFixes:
    @pytest.mark.asyncio
    async def test_an_issue_labelled_before_a_failed_label_seed_is_not_new_later(
        self, tmp_path: Path,
    ) -> None:
        labeled = {"go": GitHubError("gh failed: HTTP 502")}
        gh = _gh({}, labeled)
        poller = _poller(gh, tmp_path, ["o/a"], include_labels_by_repo={"o/a": ["go"]})
        with patch("ctrlrelay.core.poller._SEED_LABEL_RETRY_SLEEP_SECONDS", 0):
            await poller.seed_current()
            labeled["go"] = {"o/a": [_issue(4, labels=["go"], assignees=())]}
            assert await poller.poll() == []
        assert 4 in poller.seen_issues["o/a"]

    @pytest.mark.asyncio
    async def test_logins_match_whatever_their_case(self, tmp_path: Path) -> None:
        gh = _gh({}, {"go": {"o/a": [_issue(1, labels=["go"], assignees=())]}},
                 label_by="ALICE")
        poller = _poller(gh, tmp_path, ["o/a"], include_labels_by_repo={"o/a": ["go"]})
        assert [x["issue"]["number"] for x in await poller.poll()] == [1]
        # And the other way round: configured with capitals, reported without.
        gh2 = _gh({}, {"go": {"o/a": [_issue(1, labels=["go"], assignees=())]}},
                  label_by="alice")
        upper = IssuePoller(github=gh2, username="ALICE", repos=["o/a"],
                            state_file=tmp_path / "s2.json",
                            include_labels_by_repo={"o/a": ["go"]})
        assert [x["issue"]["number"] for x in await upper.poll()] == [1]

    @pytest.mark.asyncio
    async def test_many_owners_split_into_few_short_queries(self) -> None:
        from ctrlrelay.core.github import _SEARCH_QUERY_MAX, GitHubCLI

        owners = [f"owner-with-a-long-name-{i:02d}" for i in range(40)]
        page = json.dumps({"incomplete": False, "total": 0, "items": []})
        with patch.object(GitHubCLI, "_run_gh", AsyncMock(return_value=page)) as run:
            await GitHubCLI().search_open_issues(
                repos=[f"{o}/r{j}" for o in owners for j in range(3)], assignee="alice",
            )
        queries = [c.args[c.args.index("-f") + 1][2:] for c in run.await_args_list]
        assert all(len(q) <= _SEARCH_QUERY_MAX for q in queries)
        assert 1 < len(queries) < 40  # by owner, never by repo
        named = {t[5:] for q in queries for t in q.split() if t.startswith("user:")}
        assert named == set(owners)
