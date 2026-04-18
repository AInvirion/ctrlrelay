"""Tests for IssuePoller."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.poller import IssuePoller


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
        from ctrlrelay.core.poller import IssuePoller, run_poll_loop

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

    @pytest.mark.asyncio
    async def test_poll_skips_repo_on_timeout_and_continues(
        self, tmp_path: Path, caplog
    ) -> None:
        """A transient TimeoutError on one repo must skip that repo for the
        round and return issues from the other repos — not crash the loop."""
        import logging

        from ctrlrelay.core.poller import IssuePoller

        async def per_repo(repo: str, *, assignee: str):  # noqa: ARG001
            if repo == "owner/flaky":
                raise TimeoutError("gh took too long")
            return [{"number": 42, "title": "fine"}]

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(side_effect=per_repo)

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/flaky", "owner/ok"],
            state_file=tmp_path / "poller_state.json",
        )

        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
            new = await poller.poll()

        # Healthy repo still produced its issue.
        assert [(x["repo"], x["issue"]["number"]) for x in new] == [
            ("owner/ok", 42)
        ]
        # Flaky repo logged a skip event with the transient reason.
        msgs = [r.getMessage() for r in caplog.records]
        assert any("poll.repo.skipped" in m for m in msgs), msgs

    @pytest.mark.asyncio
    async def test_poll_skips_repo_on_gh_error(self, tmp_path: Path) -> None:
        """GitHubError on one repo (non-zero gh exit) is also skipped."""
        from ctrlrelay.core.github import GitHubError
        from ctrlrelay.core.poller import IssuePoller

        async def per_repo(repo: str, *, assignee: str):  # noqa: ARG001
            if repo == "owner/broken":
                raise GitHubError("gh failed: HTTP 503")
            return [{"number": 1, "title": "ok"}]

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(side_effect=per_repo)

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/broken", "owner/ok"],
            state_file=tmp_path / "poller_state.json",
        )
        new = await poller.poll()
        assert [(x["repo"], x["issue"]["number"]) for x in new] == [
            ("owner/ok", 1)
        ]

    @pytest.mark.asyncio
    async def test_poll_propagates_cancellation(self, tmp_path: Path) -> None:
        """CancelledError MUST propagate so shutdown signals aren't swallowed."""
        import asyncio as _asyncio

        from ctrlrelay.core.poller import IssuePoller

        async def always_cancel(repo: str, *, assignee: str):  # noqa: ARG001
            raise _asyncio.CancelledError()

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(side_effect=always_cancel)

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/repo"],
            state_file=tmp_path / "poller_state.json",
        )
        with pytest.raises(_asyncio.CancelledError):
            await poller.poll()

    @pytest.mark.asyncio
    async def test_run_poll_loop_survives_iteration_failure(
        self, tmp_path: Path, caplog
    ) -> None:
        """A failing iteration must be logged + retried next cycle rather
        than crashing the whole daemon."""
        import logging

        from ctrlrelay.core.poller import IssuePoller, run_poll_loop

        call_count = 0

        async def flaky_then_fine(repo: str, *, assignee: str):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("first call times out")
            return [{"number": 77, "title": "second cycle"}]

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(side_effect=flaky_then_fine)

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/repo"],
            state_file=tmp_path / "poller_state.json",
        )

        handled: list[int] = []

        async def handler(repo: str, issue: dict) -> None:
            handled.append(issue["number"])

        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
            # 2 iterations: cycle 1 is caught by the per-repo skip handler
            # (not the outer safety net), cycle 2 succeeds.
            await run_poll_loop(
                poller=poller,
                handler=handler,
                interval=0,
                max_iterations=2,
            )

        # Cycle 2 delivered issue 77.
        assert handled == [77]
        # poll.repo.skipped fired on cycle 1.
        assert any("poll.repo.skipped" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_poll_unexpected_exception_on_one_repo_does_not_lose_prior_repos(
        self, tmp_path: Path, caplog
    ) -> None:
        """Codex P2: if an unexpected (non-transient) exception escapes the
        lookup for a later repo, poll() must still return the issues it
        already collected from earlier repos. Otherwise earlier repos'
        seen_issues is mutated in-memory and the handler never runs — silent
        drop until daemon restart."""
        import logging

        from ctrlrelay.core.poller import IssuePoller

        async def per_repo(repo: str, *, assignee: str):  # noqa: ARG001
            if repo == "owner/first":
                return [{"number": 1, "title": "fine"}]
            # RuntimeError is NOT in _TRANSIENT_POLL_ERRORS.
            raise RuntimeError("unexpected upstream glitch")

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(side_effect=per_repo)

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/first", "owner/second"],
            state_file=tmp_path / "poller_state.json",
        )

        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
            new = await poller.poll()

        assert [(x["repo"], x["issue"]["number"]) for x in new] == [
            ("owner/first", 1)
        ]
        # The unexpected error is logged distinctly so operators can spot it.
        assert any(
            "poll.repo.unexpected_error" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_poll_malformed_issue_payload_contained_to_that_repo(
        self, tmp_path: Path, caplog
    ) -> None:
        """Malformed issue data from one repo (missing 'number') must not
        poison other repos' results."""
        import logging

        from ctrlrelay.core.poller import IssuePoller

        async def per_repo(repo: str, *, assignee: str):  # noqa: ARG001
            if repo == "owner/bad":
                return [{"title": "no number field!"}]  # missing 'number'
            return [{"number": 10, "title": "fine"}]

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(side_effect=per_repo)

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/bad", "owner/ok"],
            state_file=tmp_path / "poller_state.json",
        )

        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
            new = await poller.poll()

        # Healthy repo still produces its issue.
        assert [(x["repo"], x["issue"]["number"]) for x in new] == [
            ("owner/ok", 10)
        ]
        # Bad repo's processing failure is logged.
        assert any(
            "poll.repo.processing_failed" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_poll_returns_new_issues_even_if_save_state_fails(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """Codex P2: if _save_state raises (disk full, permissions), poll()
        must still return the new-issues list so the handler runs — otherwise
        seen_issues was mutated in-memory and the work is silently lost until
        the daemon restarts."""
        import logging

        from ctrlrelay.core.poller import IssuePoller

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(
            return_value=[{"number": 42, "title": "work to do"}]
        )

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/repo"],
            state_file=tmp_path / "poller_state.json",
        )

        def boom(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(poller, "_save_state", boom)

        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
            new = await poller.poll()

        # New issue MUST flow to the caller even though save_state failed.
        assert [(x["repo"], x["issue"]["number"]) for x in new] == [
            ("owner/repo", 42)
        ]
        # And the failure is recorded for operator visibility.
        assert any(
            "poll.save_state.failed" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_persistent_repo_failure_escalates_to_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        """Codex P2: after _REPO_FAILURE_WARN_THRESHOLD consecutive failures
        on the same repo, the log level escalates to WARNING so a permanent
        misconfig (expired auth, renamed repo) stops hiding as routine
        transient skips."""
        import logging

        from ctrlrelay.core.github import GitHubError
        from ctrlrelay.core.poller import IssuePoller

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(
            side_effect=GitHubError("gh failed: HTTP 401 Bad credentials")
        )

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/expired-auth"],
            state_file=tmp_path / "poller_state.json",
        )

        # 1st and 2nd cycles: INFO-level skip.
        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
            await poller.poll()
            await poller.poll()

        info_skips = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "poll.repo.skipped" in r.getMessage()
        ]
        assert len(info_skips) == 2

        # 3rd cycle: hits threshold, escalates to WARNING.
        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
            await poller.poll()

        warn_skips = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "poll.repo.skipped" in r.getMessage()
        ]
        assert warn_skips, "expected a WARNING-level skip at the threshold"
        # 4th cycle: still WARNING (persistent).
        await poller.poll()
        assert len([
            r for r in caplog.records
            if r.levelno == logging.WARNING and "poll.repo.skipped" in r.getMessage()
        ]) >= 2

    @pytest.mark.asyncio
    async def test_repo_failure_counter_resets_on_success(
        self, tmp_path: Path, caplog
    ) -> None:
        """A successful lookup must reset the per-repo failure counter so a
        previously flaky repo doesn't stay escalated forever after it recovers."""
        import logging

        from ctrlrelay.core.poller import IssuePoller

        calls = 0

        async def flaky_then_fine(repo: str, *, assignee: str):  # noqa: ARG001
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise TimeoutError("first two fail")
            return [{"number": 5, "title": "ok"}]

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(side_effect=flaky_then_fine)

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/repo"],
            state_file=tmp_path / "poller_state.json",
        )
        # Two failures accumulate, then success — counter should reset.
        await poller.poll()
        await poller.poll()
        assert poller._repo_failure_counts.get("owner/repo") == 2

        await poller.poll()
        assert "owner/repo" not in poller._repo_failure_counts

    @pytest.mark.asyncio
    async def test_run_poll_loop_survives_handler_exception(
        self, tmp_path: Path, caplog
    ) -> None:
        """An exception from the handler (e.g. run_dev_issue crash) must be
        caught by the outer safety net so the next poll cycle still runs."""
        import logging

        from ctrlrelay.core.poller import IssuePoller, run_poll_loop

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(
            return_value=[{"number": 1, "title": "x"}]
        )

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/repo"],
            state_file=tmp_path / "poller_state.json",
        )

        async def handler(repo: str, issue: dict) -> None:
            raise RuntimeError("handler blew up")

        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
            await run_poll_loop(
                poller=poller,
                handler=handler,
                interval=0,
                max_iterations=1,
            )

        # The handler failure should have been logged as iteration.failed,
        # not propagated up as an unhandled exception.
        msgs = [r.getMessage() for r in caplog.records]
        assert any("poll.iteration.failed" in m for m in msgs), msgs
