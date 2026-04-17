"""Tests for IssuePoller."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from dev_sync.core.poller import IssuePoller


def make_issue(number: int, title: str = "Test issue") -> dict:
    return {
        "number": number,
        "title": title,
        "state": "open",
        "body": "",
        "labels": [],
        "assignees": [],
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
    }


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "poller_state.json"


@pytest.fixture
def mock_github() -> MagicMock:
    gh = MagicMock()
    gh.list_assigned_issues = AsyncMock()
    return gh


@pytest.fixture
def poller(mock_github: MagicMock, state_file: Path) -> IssuePoller:
    return IssuePoller(
        github=mock_github,
        username="alice",
        repos=["owner/repo-a", "owner/repo-b"],
        state_file=state_file,
    )


class TestIssuePoller:
    @pytest.mark.asyncio
    async def test_poll_returns_new_issues(
        self, poller: IssuePoller, mock_github: MagicMock
    ) -> None:
        """On first poll, all assigned issues are returned as new."""
        mock_github.list_assigned_issues.return_value = [
            make_issue(1, "First issue"),
            make_issue(2, "Second issue"),
        ]

        results = await poller.poll()

        assert len(results) == 4  # 2 repos × 2 issues each
        repos = [r["repo"] for r in results]
        assert "owner/repo-a" in repos
        assert "owner/repo-b" in repos
        issue_numbers = [r["issue"]["number"] for r in results]
        assert issue_numbers.count(1) == 2  # issue #1 from both repos
        assert issue_numbers.count(2) == 2  # issue #2 from both repos

    @pytest.mark.asyncio
    async def test_poll_filters_seen_issues(
        self, poller: IssuePoller, mock_github: MagicMock
    ) -> None:
        """Issues already marked seen are not returned on subsequent polls."""
        mock_github.list_assigned_issues.return_value = [
            make_issue(1, "Old issue"),
            make_issue(2, "New issue"),
        ]

        # Mark issue #1 as already seen for repo-a
        poller.mark_seen("owner/repo-a", 1)

        results = await poller.poll()

        # repo-a: only issue #2 is new; repo-b: both #1 and #2 are new
        repo_a_results = [r for r in results if r["repo"] == "owner/repo-a"]
        repo_b_results = [r for r in results if r["repo"] == "owner/repo-b"]

        assert len(repo_a_results) == 1
        assert repo_a_results[0]["issue"]["number"] == 2

        assert len(repo_b_results) == 2

    @pytest.mark.asyncio
    async def test_poll_saves_state(
        self, poller: IssuePoller, mock_github: MagicMock, state_file: Path
    ) -> None:
        """State file is written after a successful poll."""
        mock_github.list_assigned_issues.return_value = [
            make_issue(10, "Some issue"),
        ]

        assert not state_file.exists()
        await poller.poll()
        assert state_file.exists()

        saved = json.loads(state_file.read_text())
        assert "seen_issues" in saved
        assert "last_poll" in saved
        # Both repos should have issue #10 recorded
        assert "owner/repo-a" in saved["seen_issues"]
        assert 10 in saved["seen_issues"]["owner/repo-a"]
        assert "owner/repo-b" in saved["seen_issues"]
        assert 10 in saved["seen_issues"]["owner/repo-b"]

    @pytest.mark.asyncio
    async def test_poll_loads_state_on_init(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Pre-existing state file is loaded on construction."""
        # Write a state file with issue #5 already seen for repo-a
        state = {
            "seen_issues": {"owner/repo-a": [5]},
            "last_poll": "2026-01-01T00:00:00+00:00",
        }
        state_file.write_text(json.dumps(state))

        mock_github.list_assigned_issues.return_value = [make_issue(5, "Already seen")]

        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
        )

        results = await poller.poll()

        # Issue #5 in repo-a was already seen — should not be returned
        repo_a_results = [r for r in results if r["repo"] == "owner/repo-a"]
        assert len(repo_a_results) == 0

    def test_mark_seen_does_not_poll(
        self, poller: IssuePoller, mock_github: MagicMock
    ) -> None:
        """mark_seen records issue without calling the GitHub API."""
        poller.mark_seen("owner/repo-a", 99)

        mock_github.list_assigned_issues.assert_not_called()
        assert 99 in poller.seen_issues.get("owner/repo-a", set())

    @pytest.mark.asyncio
    async def test_run_poll_loop_processes_new_issues(self, tmp_path: Path) -> None:
        """Should call handler for each new issue."""
        from dev_sync.core.poller import IssuePoller, run_poll_loop

        mock_github = AsyncMock()
        mock_github.list_assigned_issues.return_value = [
            {"number": 123, "title": "Fix bug"},
        ]

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/repo"],
            state_file=tmp_path / "poller_state.json",
        )

        handled_issues = []

        async def handler(repo: str, issue: dict) -> None:
            handled_issues.append((repo, issue["number"]))

        # Run one iteration
        await run_poll_loop(
            poller=poller,
            handler=handler,
            max_iterations=1,
        )

        assert len(handled_issues) == 1
        assert handled_issues[0] == ("owner/repo", 123)
