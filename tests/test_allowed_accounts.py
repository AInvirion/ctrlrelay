"""#374: an allowlist of accounts whose assignment or trigger label hands
work to the relay, besides the relay's own account."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.config import Config
from ctrlrelay.core.poller import IssuePoller

ME = "alice"


def _issue(n: int, *, labels=(), assignees=(ME,)) -> dict:
    return {"number": n, "title": f"t{n}", "labels": [{"name": x} for x in labels],
            "assignees": [{"login": a} for a in assignees]}


def _gh(assigned: dict, labeled: dict | None = None, *,
        assigner: str = ME, label_by: str = ME) -> MagicMock:
    gh = MagicMock()

    async def search(*, repos, assignee=None, label=None, limit=1000):
        src = assigned if assignee is not None else (labeled or {}).get(label, {})
        return {r: list(src.get(r, [])) for r in repos}

    gh.search_open_issues = AsyncMock(side_effect=search)
    # As GitHub returns it: ``actor`` is the account assigned, ``assigner``
    # the one that assigned (#274).
    gh.list_assignment_events = AsyncMock(return_value=[{
        "event": "assigned", "actor": {"login": ME}, "assignee": {"login": ME},
        "assigner": {"login": assigner},
    }])
    gh.list_label_events = AsyncMock(return_value=[
        {"event": "labeled", "actor": {"login": label_by}, "label": {"name": "go"}},
    ])
    gh.repo_is_archived = AsyncMock(return_value=False)
    return gh


def _poller(gh, tmp_path: Path, repos, **kw) -> IssuePoller:
    return IssuePoller(github=gh, username=ME, repos=repos,
                       state_file=tmp_path / "s.json", **kw)


def _numbers(out) -> list[tuple[str, int]]:
    return [(x["repo"], x["issue"]["number"]) for x in out]


class TestAssignments:
    @pytest.mark.asyncio
    async def test_an_assignment_by_a_listed_account_is_taken(self, tmp_path: Path) -> None:
        gh = _gh({"o/a": [_issue(1)]}, assigner="bob")
        poller = _poller(gh, tmp_path, ["o/a"], allowed_accounts_by_repo={"o/a": {"bob"}})
        assert _numbers(await poller.poll()) == [("o/a", 1)]

    @pytest.mark.asyncio
    async def test_an_assignment_by_an_unlisted_account_is_not(self, tmp_path: Path) -> None:
        gh = _gh({"o/a": [_issue(1)]}, assigner="mallory")
        poller = _poller(gh, tmp_path, ["o/a"], allowed_accounts_by_repo={"o/a": {"bob"}})
        assert await poller.poll() == []

    @pytest.mark.asyncio
    async def test_the_list_is_per_repo(self, tmp_path: Path) -> None:
        gh = _gh({"o/b": [_issue(1)]}, assigner="bob")
        poller = _poller(gh, tmp_path, ["o/a", "o/b"],
                         allowed_accounts_by_repo={"o/a": {"bob"}, "o/b": set()})
        assert await poller.poll() == []

    @pytest.mark.asyncio
    async def test_logins_match_without_case(self, tmp_path: Path) -> None:
        gh = _gh({"o/a": [_issue(1)]}, assigner="BoB")
        poller = _poller(gh, tmp_path, ["o/a"], allowed_accounts_by_repo={"o/a": {"bob"}})
        assert _numbers(await poller.poll()) == [("o/a", 1)]

    @pytest.mark.asyncio
    async def test_the_relays_own_account_still_counts(self, tmp_path: Path) -> None:
        gh = _gh({"o/a": [_issue(1)]}, assigner=ME)
        poller = _poller(gh, tmp_path, ["o/a"], allowed_accounts_by_repo={"o/a": {"bob"}})
        assert _numbers(await poller.poll()) == [("o/a", 1)]

    @pytest.mark.asyncio
    async def test_an_event_with_no_assigner_is_refused_even_with_a_list(
        self, tmp_path: Path,
    ) -> None:
        gh = _gh({"o/a": [_issue(1)]})
        gh.list_assignment_events.return_value = [{
            "event": "assigned", "actor": {"login": ME}, "assignee": {"login": ME},
        }]
        poller = _poller(gh, tmp_path, ["o/a"], allowed_accounts_by_repo={"o/a": {"bob"}})
        assert await poller.poll() == []


class TestTriggerLabels:
    @pytest.mark.asyncio
    async def test_a_label_added_by_a_listed_account_starts_work(self, tmp_path: Path) -> None:
        gh = _gh({}, {"go": {"o/a": [_issue(1, labels=["go"], assignees=())]}},
                 label_by="bob")
        poller = _poller(gh, tmp_path, ["o/a"], include_labels_by_repo={"o/a": ["go"]},
                         allowed_accounts_by_repo={"o/a": {"bob"}})
        assert _numbers(await poller.poll()) == [("o/a", 1)]

    @pytest.mark.asyncio
    async def test_a_label_added_by_an_unlisted_account_does_not(self, tmp_path: Path) -> None:
        gh = _gh({}, {"go": {"o/a": [_issue(1, labels=["go"], assignees=())]}},
                 label_by="mallory")
        poller = _poller(gh, tmp_path, ["o/a"], include_labels_by_repo={"o/a": ["go"]},
                         allowed_accounts_by_repo={"o/a": {"bob"}})
        assert await poller.poll() == []


def _config(tmp_path: Path, *, intake=None, repos=()) -> Config:
    raw = {
        "node_id": "test-node",
        "paths": {k: str(tmp_path / k) for k in
                  ("state_db", "worktrees", "bare_repos", "contexts", "skills", "repo_root")},
        "transport": {"type": "file_mock", "file_mock": {
            "inbox": str(tmp_path / "in"), "outbox": str(tmp_path / "out")}},
        "dashboard": {"enabled": False},
        "repos": list(repos),
    }
    if intake is not None:
        raw["intake"] = intake
    return Config.model_validate(raw)


class TestConfig:
    def test_default_allows_nobody_extra(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, repos=[{"name": "o/a"}])
        assert cfg.allowed_accounts_by_repo() == {"o/a": set()}

    def test_a_repo_list_replaces_the_global_one_even_when_empty(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, intake={"allowed_accounts": ["Bob", "carol"]}, repos=[
            {"name": "o/a"},
            {"name": "o/b", "automation": {"allowed_accounts": ["dave"]}},
            {"name": "o/c", "automation": {"allowed_accounts": []}},
        ])
        assert cfg.allowed_accounts_by_repo() == {
            "o/a": {"bob", "carol"}, "o/b": {"dave"}, "o/c": set(),
        }

    @pytest.mark.parametrize("bad", ["@bob", "", "bob smith", "-bob", "bob-", "a--b", "x" * 40])
    def test_a_value_that_is_not_a_login_is_refused(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ValueError, match="not a GitHub login"):
            _config(tmp_path, intake={"allowed_accounts": [bad]})
        with pytest.raises(ValueError, match="not a GitHub login"):
            _config(tmp_path, repos=[{"name": "o/a", "automation": {"allowed_accounts": [bad]}}])

    def test_a_login_listed_twice_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="listed twice"):
            _config(tmp_path, intake={"allowed_accounts": ["bob", "BOB"]})

    @pytest.mark.parametrize("good", ["bob", "a-b", "B0b-9", "x" * 39])
    def test_real_logins_load(self, tmp_path: Path, good: str) -> None:
        cfg = _config(tmp_path, intake={"allowed_accounts": [good]})
        assert cfg.intake.allowed_accounts == [good]
