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
        """Should get PR check status."""
        from dev_sync.core.github import GitHubCLI

        mock_output = json.dumps([
            {"name": "tests", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "success"},
        ])

        with patch("dev_sync.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            checks = await gh.get_pr_checks("owner/repo", 42)

            assert len(checks) == 2
            assert all(c["conclusion"] == "success" for c in checks)
