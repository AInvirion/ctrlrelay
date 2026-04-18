"""Tests for GitHub CLI wrapper."""

import json
from unittest.mock import patch

import pytest


class TestGitHubCLI:
    @pytest.mark.asyncio
    async def test_list_prs_returns_parsed_json(self) -> None:
        """Should parse gh pr list JSON output."""
        from dev_sync.core.github import GitHubCLI

        mock_output = json.dumps([
            {"number": 1, "title": "Bump requests", "author": {"login": "dependabot[bot]"}},
            {"number": 2, "title": "Fix bug", "author": {"login": "user"}},
        ])

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            prs = await gh.list_prs("owner/repo", state="open")

            assert len(prs) == 2
            assert prs[0]["number"] == 1
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_security_alerts(self) -> None:
        """Should fetch Dependabot alerts."""
        from dev_sync.core.github import GitHubCLI

        mock_output = json.dumps([
            {"number": 1, "state": "open", "dependency": {"package": {"name": "lodash"}}},
        ])

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            alerts = await gh.list_security_alerts("owner/repo")

            assert len(alerts) == 1
            assert alerts[0]["dependency"]["package"]["name"] == "lodash"

    @pytest.mark.asyncio
    async def test_merge_pr(self) -> None:
        """Should merge PR with squash."""
        from dev_sync.core.github import GitHubCLI

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = ""
            gh = GitHubCLI()
            await gh.merge_pr("owner/repo", 42, method="squash")

            mock_run.assert_called_once()
            args = mock_run.call_args[0]
            assert "merge" in args
            assert "--squash" in args

    @pytest.mark.asyncio
    async def test_get_pr_checks(self) -> None:
        """Should get PR check status using the bucket field."""
        from dev_sync.core.github import GitHubCLI

        mock_output = json.dumps([
            {"name": "tests", "state": "SUCCESS", "bucket": "pass"},
            {"name": "lint", "state": "SUCCESS", "bucket": "pass"},
        ])

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            checks = await gh.get_pr_checks("owner/repo", 42)

            assert len(checks) == 2
            assert all(c["bucket"] == "pass" for c in checks)

    @pytest.mark.asyncio
    async def test_get_pr_checks_returns_empty_on_non_zero_exit(self) -> None:
        """`gh pr checks` exits non-zero while any check is pending; we must
        treat that as "still pending" rather than propagating GitHubError."""
        from dev_sync.core.github import GitHubCLI, GitHubError

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.side_effect = GitHubError("gh failed: pending checks")
            gh = GitHubCLI()
            checks = await gh.get_pr_checks("owner/repo", 42)

            assert checks == []

    @pytest.mark.asyncio
    async def test_list_assigned_issues(self) -> None:
        """Should list issues assigned to a user."""
        from dev_sync.core.github import GitHubCLI

        mock_output = json.dumps([
            {"number": 10, "title": "Fix login bug", "state": "open", "assignees": [{"login": "alice"}]},
            {"number": 11, "title": "Add dark mode", "state": "open", "assignees": [{"login": "alice"}]},
        ])

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            issues = await gh.list_assigned_issues("owner/repo", assignee="alice")

            assert len(issues) == 2
            assert issues[0]["number"] == 10
            args = mock_run.call_args[0]
            assert "--assignee" in args
            assert "alice" in args

    @pytest.mark.asyncio
    async def test_get_issue(self) -> None:
        """Should get a single issue by number."""
        from dev_sync.core.github import GitHubCLI

        mock_output = json.dumps({
            "number": 42,
            "title": "Broken build",
            "body": "CI is failing on main",
            "state": "open",
            "labels": [{"name": "bug"}],
        })

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            issue = await gh.get_issue("owner/repo", 42)

            assert issue["number"] == 42
            assert issue["title"] == "Broken build"
            args = mock_run.call_args[0]
            assert "view" in args
            assert "42" in args

    @pytest.mark.asyncio
    async def test_create_pr(self) -> None:
        """Should create a PR and return its data."""
        from dev_sync.core.github import GitHubCLI

        mock_output = json.dumps({
            "number": 99,
            "title": "feat: add thing",
            "url": "https://github.com/owner/repo/pull/99",
        })

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            pr = await gh.create_pr(
                "owner/repo",
                title="feat: add thing",
                body="Implements the thing",
                head="feat/add-thing",
                base="main",
            )

            assert pr["number"] == 99
            args = mock_run.call_args[0]
            assert "create" in args
            assert "--title" in args
            assert "--head" in args

    @pytest.mark.asyncio
    async def test_get_pr_state(self) -> None:
        """Should get PR state including merge status."""
        from dev_sync.core.github import GitHubCLI

        mock_output = json.dumps({
            "number": 55,
            "state": "open",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        })

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            state = await gh.get_pr_state("owner/repo", 55)

            assert state["number"] == 55
            assert state["state"] == "open"
            assert state["mergeable"] == "MERGEABLE"
            args = mock_run.call_args[0]
            assert "view" in args
            assert "55" in args

    @pytest.mark.asyncio
    async def test_comment_on_issue(self) -> None:
        """Should post a comment on an issue."""
        from dev_sync.core.github import GitHubCLI

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = ""
            gh = GitHubCLI()
            await gh.comment_on_issue("owner/repo", 7, "hello there")

            mock_run.assert_called_once()
            args = mock_run.call_args[0]
            assert "issue" in args
            assert "comment" in args
            assert "7" in args
            assert "--body" in args
            assert "hello there" in args

    @pytest.mark.asyncio
    async def test_close_issue_without_comment(self) -> None:
        """Should close an issue without adding a comment."""
        from dev_sync.core.github import GitHubCLI

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = ""
            gh = GitHubCLI()
            await gh.close_issue("owner/repo", 7)

            mock_run.assert_called_once()
            args = mock_run.call_args[0]
            assert "close" in args
            assert "7" in args

    @pytest.mark.asyncio
    async def test_close_issue_with_comment(self) -> None:
        """Should close an issue and post a comment."""
        from dev_sync.core.github import GitHubCLI

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = ""
            gh = GitHubCLI()
            await gh.close_issue("owner/repo", 7, comment="Fixed in PR #99")

            assert mock_run.call_count == 2
            # First call: comment, second call: close (or vice versa)
            all_args = [mock_run.call_args_list[i][0] for i in range(2)]
            commands = [" ".join(a) for a in all_args]
            assert any("comment" in c for c in commands)
            assert any("close" in c for c in commands)
