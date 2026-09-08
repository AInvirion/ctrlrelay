"""Tests for GitHub CLI wrapper."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestGitHubCLI:
    @pytest.mark.asyncio
    async def test_list_prs_returns_parsed_json(self) -> None:
        """Should parse gh pr list JSON output."""
        from ctrlrelay.core.github import GitHubCLI

        mock_output = json.dumps([
            {"number": 1, "title": "Bump requests", "author": {"login": "dependabot[bot]"}},
            {"number": 2, "title": "Fix bug", "author": {"login": "user"}},
        ])

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            prs = await gh.list_prs("owner/repo", state="open")

            assert len(prs) == 2
            assert prs[0]["number"] == 1
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_prs_passes_head_filter(self) -> None:
        """Issue #52: the worktree reuse path uses ``list_prs(head=...)``
        to probe whether a branch still backs an open PR. ``head`` must
        be forwarded to ``gh pr list --head`` so the filter narrows
        server-side to that one branch."""
        from ctrlrelay.core.github import GitHubCLI

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = json.dumps([
                {"number": 42, "headRefName": "fix/issue-13"},
            ])
            gh = GitHubCLI()
            prs = await gh.list_prs(
                "owner/repo", state="open", head="fix/issue-13",
            )

            assert len(prs) == 1
            assert prs[0]["number"] == 42
            args = mock_run.call_args.args
            assert "--head" in args
            assert "fix/issue-13" in args
            assert "--state" in args
            assert "open" in args

    @pytest.mark.asyncio
    async def test_list_prs_without_head_omits_flag(self) -> None:
        """Unfiltered ``list_prs`` must not pass ``--head`` at all — an
        empty head would make gh return no PRs, silently breaking
        existing callers (poller, dashboard) that enumerate all PRs."""
        from ctrlrelay.core.github import GitHubCLI

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = "[]"
            gh = GitHubCLI()
            await gh.list_prs("owner/repo", state="open")
            args = mock_run.call_args.args
            assert "--head" not in args

    @pytest.mark.asyncio
    async def test_list_security_alerts(self) -> None:
        """Should fetch Dependabot alerts."""
        from ctrlrelay.core.github import GitHubCLI

        mock_output = json.dumps([
            {"number": 1, "state": "open", "dependency": {"package": {"name": "lodash"}}},
        ])

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            alerts = await gh.list_security_alerts("owner/repo")

            assert len(alerts) == 1
            assert alerts[0]["dependency"]["package"]["name"] == "lodash"

    @pytest.mark.asyncio
    async def test_merge_pr(self) -> None:
        """Should merge PR with squash."""
        from ctrlrelay.core.github import GitHubCLI

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = ""
            gh = GitHubCLI()
            await gh.merge_pr("owner/repo", 42, method="squash")

            mock_run.assert_called_once()
            args = mock_run.call_args[0]
            assert "merge" in args
            assert "--squash" in args

    @pytest.mark.asyncio
    async def test_get_pr_checks_uses_pr_view_not_pr_checks(self) -> None:
        """`gh pr checks --json` isn't available on gh releases before it
        shipped (confirmed absent on gh 2.45.0, the current Ubuntu 24.04
        package - #145) - get_pr_checks must use `gh pr view --json
        statusCheckRollup` instead, which has been stable since early gh 2.x."""
        from ctrlrelay.core.github import GitHubCLI

        mock_output = json.dumps({"statusCheckRollup": []})

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_output.encode(), b""))
        mock_proc.returncode = 0

        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)
        ) as mock_exec:
            gh = GitHubCLI()
            await gh.get_pr_checks("owner/repo", 42)

        args = mock_exec.call_args[0]
        assert "checks" not in args
        assert ("pr", "view", "42") == args[1:4]
        assert "--repo" in args and "owner/repo" in args
        assert "statusCheckRollup" in args

    @pytest.mark.asyncio
    async def test_get_pr_checks_normalizes_check_run_entries(self) -> None:
        """CheckRun entries (Actions jobs, CodeQL) use status/conclusion,
        not state - must be mapped into the {name, state, bucket, link}
        shape PRVerifier's bucket logic expects."""
        from ctrlrelay.core.github import GitHubCLI

        mock_output = json.dumps({"statusCheckRollup": [
            {
                "__typename": "CheckRun", "name": "pytest",
                "status": "COMPLETED", "conclusion": "SUCCESS",
                "detailsUrl": "https://example.com/1",
            },
            {
                "__typename": "CheckRun", "name": "lint",
                "status": "IN_PROGRESS", "conclusion": None,
                "detailsUrl": "https://example.com/2",
            },
            {
                "__typename": "CheckRun", "name": "flaky-e2e",
                "status": "COMPLETED", "conclusion": "FAILURE",
                "detailsUrl": "https://example.com/3",
            },
        ]})

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_output.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            gh = GitHubCLI()
            checks = await gh.get_pr_checks("owner/repo", 42)

        by_name = {c["name"]: c for c in checks}
        assert by_name["pytest"]["bucket"] == "pass"
        assert by_name["lint"]["bucket"] == "pending"
        assert by_name["flaky-e2e"]["bucket"] == "fail"

    @pytest.mark.asyncio
    async def test_get_pr_checks_normalizes_status_context_entries(self) -> None:
        """StatusContext entries (the legacy commit-status API, e.g. CLA
        bots) use `state` and `context` instead of CheckRun's fields."""
        from ctrlrelay.core.github import GitHubCLI

        mock_output = json.dumps({"statusCheckRollup": [
            {
                "__typename": "StatusContext",
                "context": "verification/cla-signed",
                "state": "SUCCESS",
                "targetUrl": "https://example.com/cla",
            },
        ]})

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(mock_output.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            gh = GitHubCLI()
            checks = await gh.get_pr_checks("owner/repo", 42)

        assert checks == [{
            "name": "verification/cla-signed",
            "state": "SUCCESS",
            "bucket": "pass",
            "link": "https://example.com/cla",
        }]

    @pytest.mark.asyncio
    async def test_get_pr_checks_maps_every_checkrun_conclusion(self) -> None:
        """Every terminal CheckRun conclusion GitHub can emit, not just the
        SUCCESS/FAILURE pair - an unmapped conclusion silently falling into
        the wrong bucket is what decides whether ctrlrelay merges or waits."""
        from ctrlrelay.core.github import GitHubCLI

        cases = [
            ("SUCCESS", "pass"),
            ("NEUTRAL", "pass"),
            ("SKIPPED", "skipping"),
            ("CANCELLED", "cancel"),
            ("FAILURE", "fail"),
            ("TIMED_OUT", "fail"),
            ("ACTION_REQUIRED", "fail"),
            ("STARTUP_FAILURE", "fail"),
            ("STALE", "fail"),
        ]
        for conclusion, expected_bucket in cases:
            mock_output = json.dumps({"statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "name": "job",
                    "status": "COMPLETED",
                    "conclusion": conclusion,
                    "detailsUrl": "https://example.com/run",
                },
            ]})

            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(mock_output.encode(), b""))
            mock_proc.returncode = 0

            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
                gh = GitHubCLI()
                checks = await gh.get_pr_checks("owner/repo", 42)

            assert checks[0]["bucket"] == expected_bucket, conclusion

    @pytest.mark.asyncio
    async def test_get_pr_checks_maps_every_status_context_state(self) -> None:
        """EXPECTED is the one that matters: it means a branch-protection
        status that hasn't reported yet. Bucketing it as `fail` rather than
        `pending` would abort a PR that is merely still waiting."""
        from ctrlrelay.core.github import GitHubCLI

        cases = [
            ("SUCCESS", "pass"),
            ("PENDING", "pending"),
            ("EXPECTED", "pending"),
            ("ERROR", "fail"),
            ("FAILURE", "fail"),
        ]
        for state, expected_bucket in cases:
            mock_output = json.dumps({"statusCheckRollup": [
                {
                    "__typename": "StatusContext",
                    "context": "legacy-ci",
                    "state": state,
                    "targetUrl": "https://example.com/build",
                },
            ]})

            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(mock_output.encode(), b""))
            mock_proc.returncode = 0

            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
                gh = GitHubCLI()
                checks = await gh.get_pr_checks("owner/repo", 42)

            assert checks[0]["name"] == "legacy-ci"
            assert checks[0]["bucket"] == expected_bucket, state
            assert checks[0]["link"] == "https://example.com/build"

    @pytest.mark.asyncio
    async def test_get_pr_checks_returns_empty_when_no_checks_reported(self) -> None:
        """A PR with no CI at all (or checks not registered yet) just comes
        back as an empty statusCheckRollup - gh pr view exits 0 either way,
        no stderr-message sniffing needed."""

        from ctrlrelay.core.github import GitHubCLI

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(
            json.dumps({"statusCheckRollup": []}).encode(), b"",
        ))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            gh = GitHubCLI()
            checks = await gh.get_pr_checks("owner/repo", 42)

        assert checks == []

    @pytest.mark.asyncio
    async def test_get_pr_checks_raises_on_genuine_failure(self) -> None:
        """Auth/network/missing-PR failures exit non-zero with an error on
        stderr. Must raise GitHubError (via _run_gh) rather than silently
        returning [] (which would be indistinguishable from 'no CI configured')."""

        from ctrlrelay.core.github import GitHubCLI, GitHubError

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(
            b"",
            b"HTTP 401: Bad credentials\n",
        ))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            gh = GitHubCLI()
            with pytest.raises(GitHubError, match="401"):
                await gh.get_pr_checks("owner/repo", 42)

    @pytest.mark.asyncio
    async def test_run_gh_kills_child_on_timeout(self) -> None:
        """A hung `gh` must be terminated on timeout — otherwise the
        long-running PR-watch daemon (which retries on TimeoutError up
        to _TRANSIENT_FAILURE_CAP times) would leak subprocesses while
        the network stalls."""
        import asyncio

        from ctrlrelay.core.github import GitHubCLI

        mock_proc = MagicMock()

        async def never_returns():
            await asyncio.Event().wait()

        mock_proc.communicate = AsyncMock(side_effect=never_returns)
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            gh = GitHubCLI(timeout=0.01)
            with pytest.raises(asyncio.TimeoutError):
                await gh._run_gh("issue", "list")

        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_pr_checks_kills_child_on_timeout(self) -> None:
        """Same reaper guarantee for get_pr_checks, which bypasses _run_gh."""
        import asyncio

        from ctrlrelay.core.github import GitHubCLI

        mock_proc = MagicMock()

        async def never_returns():
            await asyncio.Event().wait()

        mock_proc.communicate = AsyncMock(side_effect=never_returns)
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            gh = GitHubCLI(timeout=0.01)
            with pytest.raises(asyncio.TimeoutError):
                await gh.get_pr_checks("owner/repo", 42)

        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_list_assigned_issues(self) -> None:
        """Should list issues assigned to a user."""
        from ctrlrelay.core.github import GitHubCLI

        alice = [{"login": "alice"}]
        mock_output = json.dumps([
            {"number": 10, "title": "Fix login bug", "state": "open", "assignees": alice},
            {"number": 11, "title": "Add dark mode", "state": "open", "assignees": alice},
        ])

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
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
        from ctrlrelay.core.github import GitHubCLI

        mock_output = json.dumps({
            "number": 42,
            "title": "Broken build",
            "body": "CI is failing on main",
            "state": "open",
            "labels": [{"name": "bug"}],
        })

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
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
        from ctrlrelay.core.github import GitHubCLI

        mock_output = json.dumps({
            "number": 99,
            "title": "feat: add thing",
            "url": "https://github.com/owner/repo/pull/99",
        })

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
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
        from ctrlrelay.core.github import GitHubCLI

        mock_output = json.dumps({
            "number": 55,
            "state": "open",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        })

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
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
    async def test_list_assignment_events(self) -> None:
        """Should return only 'assigned' events for an issue."""
        from ctrlrelay.core.github import GitHubCLI

        mock_output = json.dumps([
            {
                "event": "assigned",
                "actor": {"login": "alice"},
                "assignee": {"login": "alice"},
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "event": "assigned",
                "actor": {"login": "bob"},
                "assignee": {"login": "alice"},
                "created_at": "2026-01-02T00:00:00Z",
            },
        ])

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = mock_output
            gh = GitHubCLI()
            events = await gh.list_assignment_events("owner/repo", 42)

            assert len(events) == 2
            assert events[0]["actor"]["login"] == "alice"
            assert events[1]["actor"]["login"] == "bob"

            args = mock_run.call_args[0]
            assert "api" in args
            assert "/repos/owner/repo/issues/42/events" in args

    @pytest.mark.asyncio
    async def test_list_assignment_events_empty_output(self) -> None:
        """Should return [] when there are no assignment events."""
        from ctrlrelay.core.github import GitHubCLI

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = ""
            gh = GitHubCLI()
            events = await gh.list_assignment_events("owner/repo", 42)

            assert events == []

    @pytest.mark.asyncio
    async def test_comment_on_issue(self) -> None:
        """Should post a comment on an issue."""
        from ctrlrelay.core.github import GitHubCLI

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
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
        from ctrlrelay.core.github import GitHubCLI

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
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
        from ctrlrelay.core.github import GitHubCLI

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = ""
            gh = GitHubCLI()
            await gh.close_issue("owner/repo", 7, comment="Fixed in PR #99")

            assert mock_run.call_count == 2
            # First call: comment, second call: close (or vice versa)
            all_args = [mock_run.call_args_list[i][0] for i in range(2)]
            commands = [" ".join(a) for a in all_args]
            assert any("comment" in c for c in commands)
            assert any("close" in c for c in commands)


class TestRepoIsArchived:
    """Issue #163: one cheap `gh repo view --json isArchived` call, and a
    hard rule that anything other than a boolean answer raises so the
    caller can fail open."""

    @pytest.mark.asyncio
    async def test_returns_true_for_archived_repo(self) -> None:
        from ctrlrelay.core.github import GitHubCLI

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = json.dumps({"isArchived": True})
            gh = GitHubCLI()
            assert await gh.repo_is_archived("owner/repo") is True

        args = mock_run.call_args.args
        assert args == ("repo", "view", "owner/repo", "--json", "isArchived")

    @pytest.mark.asyncio
    async def test_returns_false_for_active_repo(self) -> None:
        from ctrlrelay.core.github import GitHubCLI

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = json.dumps({"isArchived": False})
            gh = GitHubCLI()
            assert await gh.repo_is_archived("owner/repo") is False

    @pytest.mark.asyncio
    async def test_missing_field_raises(self) -> None:
        from ctrlrelay.core.github import GitHubCLI, GitHubError

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = json.dumps({"name": "repo"})
            gh = GitHubCLI()
            with pytest.raises(GitHubError):
                await gh.repo_is_archived("owner/repo")

    @pytest.mark.asyncio
    async def test_non_json_output_raises(self) -> None:
        from ctrlrelay.core.github import GitHubCLI, GitHubError

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = "not json at all"
            gh = GitHubCLI()
            with pytest.raises(GitHubError):
                await gh.repo_is_archived("owner/repo")

    @pytest.mark.asyncio
    async def test_timeout_is_forwarded(self) -> None:
        from ctrlrelay.core.github import GitHubCLI

        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as mock_run:
            mock_run.return_value = json.dumps({"isArchived": False})
            gh = GitHubCLI()
            await gh.repo_is_archived("owner/repo", timeout=15)

        assert mock_run.call_args.kwargs["timeout"] == 15
