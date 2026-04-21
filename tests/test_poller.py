"""Tests for IssuePoller."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.poller import IssuePoller


def make_issue(
    number: int,
    title: str = "Test issue",
    labels: list[dict] | list[str] | None = None,
) -> dict:
    return {
        "number": number,
        "title": title,
        "state": "open",
        "body": "",
        "labels": labels or [],
        "assignees": [],
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
    }


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "poller_state.json"


def make_assigned_event(actor_login: str, assignee_login: str) -> dict:
    return {
        "event": "assigned",
        "actor": {"login": actor_login},
        "assignee": {"login": assignee_login},
        "created_at": "2026-01-01T00:00:00Z",
    }


@pytest.fixture
def mock_github() -> MagicMock:
    gh = MagicMock()
    gh.list_assigned_issues = AsyncMock()
    # Default: every issue was self-assigned by "alice" so the filter is a no-op.
    gh.list_assignment_events = AsyncMock(
        return_value=[make_assigned_event("alice", "alice")]
    )
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
    async def test_poll_excludes_issues_with_matching_label(
        self,
        mock_github: MagicMock,
        state_file: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Issues carrying an exclude label are marked seen but NOT returned."""
        mock_github.list_assigned_issues.return_value = [
            make_issue(1, "Operator task", labels=[{"name": "manual"}]),
            make_issue(2, "Real code work", labels=[{"name": "bug"}]),
        ]

        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            exclude_labels_by_repo={"owner/repo-a": ["manual", "operator"]},
        )

        with caplog.at_level(logging.INFO, logger="ctrlrelay.poller"):
            results = await poller.poll()

        # Only issue #2 survives; #1 is filtered out by label.
        assert [r["issue"]["number"] for r in results] == [2]

        # Both issues are now marked seen — #1 will not re-appear.
        assert poller.seen_issues["owner/repo-a"] == {1, 2}

        # The excluded issue is logged under the agreed event name.
        records = [
            r for r in caplog.records if r.getMessage() == "poll.issue.excluded_by_label"
        ]
        assert len(records) == 1
        rec = records[0]
        assert rec.repo == "owner/repo-a"
        assert rec.issue_number == 1
        assert rec.matched_label == "manual"

    @pytest.mark.asyncio
    async def test_poll_without_exclude_labels_returns_everything(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Labeled issues pass through when no exclude_labels are configured."""
        mock_github.list_assigned_issues.return_value = [
            make_issue(1, "Looks operator-y but no exclude configured", labels=[{"name": "manual"}]),
        ]

        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
        )

        results = await poller.poll()

        assert len(results) == 1
        assert results[0]["issue"]["number"] == 1

    @pytest.mark.asyncio
    async def test_poll_exclude_match_is_case_insensitive(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """'Manual' label matches 'manual' in exclude config."""
        mock_github.list_assigned_issues.return_value = [
            make_issue(1, "Case mismatch", labels=[{"name": "Manual"}]),
        ]

        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            exclude_labels_by_repo={"owner/repo-a": ["manual"]},
        )

        results = await poller.poll()

        assert results == []
        assert 1 in poller.seen_issues["owner/repo-a"]

    @pytest.mark.asyncio
    async def test_poll_excluded_issue_not_rereported_across_polls(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Once excluded and marked seen, the issue stays excluded on next poll.

        Guards against the failure mode in #91: the issue keeps re-appearing
        and the dev pipeline keeps getting spawned for operator-only work.
        """
        mock_github.list_assigned_issues.return_value = [
            make_issue(1, "Operator task", labels=[{"name": "manual"}]),
        ]

        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            exclude_labels_by_repo={"owner/repo-a": ["manual"]},
        )

        # First poll: excluded, marked seen.
        assert await poller.poll() == []

        # Second poll with the same issue still present: still no handoff.
        assert await poller.poll() == []

    @pytest.mark.asyncio
    async def test_poll_exclude_labels_persist_in_state_file(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """An excluded issue survives a poller restart (written to state.json)."""
        mock_github.list_assigned_issues.return_value = [
            make_issue(1, "Op task", labels=[{"name": "manual"}]),
        ]

        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            exclude_labels_by_repo={"owner/repo-a": ["manual"]},
        )
        await poller.poll()

        saved = json.loads(state_file.read_text())
        assert saved["seen_issues"]["owner/repo-a"] == [1]

        # Restart: no exclude config this time. The issue still shouldn't
        # re-appear because it was persisted as seen.
        restarted = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
        )
        assert await restarted.poll() == []

    @pytest.mark.asyncio
    async def test_poll_accepts_string_labels(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Label matching tolerates labels given as plain strings (test robustness)."""
        mock_github.list_assigned_issues.return_value = [
            make_issue(1, "Plain string labels", labels=["manual", "bug"]),
        ]

        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            exclude_labels_by_repo={"owner/repo-a": ["manual"]},
        )

        results = await poller.poll()

        assert results == []

    @pytest.mark.asyncio
    async def test_run_poll_loop_processes_new_issues(self, tmp_path: Path) -> None:
        """Should call handler for each new issue."""
        from ctrlrelay.core.poller import IssuePoller, run_poll_loop

        mock_github = AsyncMock()
        mock_github.list_assigned_issues.return_value = [
            {"number": 123, "title": "Fix bug"},
        ]
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("testuser", "testuser"),
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
        mock_github.list_assignment_events = AsyncMock(
            return_value=[make_assigned_event("testuser", "testuser")]
        )

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
        mock_github.list_assignment_events = AsyncMock(
            return_value=[make_assigned_event("testuser", "testuser")]
        )

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
        mock_github.list_assignment_events = AsyncMock(
            return_value=[make_assigned_event("testuser", "testuser")]
        )

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
        mock_github.list_assignment_events = AsyncMock(
            return_value=[make_assigned_event("testuser", "testuser")]
        )

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
        mock_github.list_assignment_events = AsyncMock(
            return_value=[make_assigned_event("testuser", "testuser")]
        )

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
        # The malformed item is logged.
        assert any(
            "poll.issue.malformed" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_poll_malformed_issue_does_not_block_later_issues_in_same_repo(
        self, tmp_path: Path, caplog
    ) -> None:
        """Codex P2: a malformed issue MUST NOT prevent later good issues in
        the same repo's batch from being discovered. Otherwise the valid
        issues after the bad one are never processed until someone fixes the
        data upstream."""
        import logging

        from ctrlrelay.core.poller import IssuePoller

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(
            return_value=[
                {"number": 1, "title": "first good"},
                {"title": "no number — malformed"},
                {"number": 2, "title": "second good AFTER the bad one"},
            ]
        )
        mock_github.list_assignment_events = AsyncMock(
            return_value=[make_assigned_event("testuser", "testuser")]
        )

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/repo"],
            state_file=tmp_path / "poller_state.json",
        )

        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
            new = await poller.poll()

        numbers = sorted(x["issue"]["number"] for x in new)
        assert numbers == [1, 2], (
            "expected both good issues to be discovered even though there "
            f"was a malformed item between them, got {numbers}"
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
        mock_github.list_assignment_events = AsyncMock(
            return_value=[make_assigned_event("testuser", "testuser")]
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
        logged and the loop must continue to the next iteration."""
        import logging

        from ctrlrelay.core.poller import IssuePoller, run_poll_loop

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(
            return_value=[{"number": 1, "title": "x"}]
        )
        mock_github.list_assignment_events = AsyncMock(
            return_value=[make_assigned_event("testuser", "testuser")]
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

        msgs = [r.getMessage() for r in caplog.records]
        # The per-handler guard emits poll.handler.failed with the offending
        # repo+issue — distinct from the outer poll.iteration.failed which
        # is only for poll() itself crashing.
        assert any("poll.handler.failed" in m for m in msgs), msgs

    @pytest.mark.asyncio
    async def test_run_poll_loop_handler_failure_does_not_skip_rest_of_batch(
        self, tmp_path: Path, caplog
    ) -> None:
        """Codex P1: if handler fails on the first issue, the rest of the
        batch from the same poll cycle MUST still run. Otherwise those
        issues are marked seen (by poll()) but never handled — silently
        dropped until daemon restart."""
        import logging

        from ctrlrelay.core.poller import IssuePoller, run_poll_loop

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(
            return_value=[
                {"number": 1, "title": "will blow up handler"},
                {"number": 2, "title": "second"},
                {"number": 3, "title": "third"},
            ]
        )
        mock_github.list_assignment_events = AsyncMock(
            return_value=[make_assigned_event("testuser", "testuser")]
        )

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/repo"],
            state_file=tmp_path / "poller_state.json",
        )

        processed: list[int] = []

        async def handler(repo: str, issue: dict) -> None:
            if issue["number"] == 1:
                raise RuntimeError("handler blew up on first")
            processed.append(issue["number"])

        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
            await run_poll_loop(
                poller=poller,
                handler=handler,
                interval=0,
                max_iterations=1,
            )

        # Issues 2 and 3 MUST still have run — per-handler isolation.
        assert processed == [2, 3], processed
        assert any(
            "poll.handler.failed" in r.getMessage() for r in caplog.records
        )


class TestUnmarkSeen:
    """Regression for codex round-8 [P1]: the poller marks issues seen
    BEFORE the handler runs, so a handler failure would permanently
    drop the issue. `unmark_seen` lets a caller revert the claim for
    transient failures (e.g. per-repo lock conflict with a scheduled
    secops sweep) so the next poll picks it up again."""

    def test_unmark_removes_from_seen_set(self, state_file: Path) -> None:
        poller = IssuePoller(
            github=MagicMock(),
            username="tester",
            repos=["owner/repo"],
            state_file=state_file,
        )
        poller.mark_seen("owner/repo", 42)
        assert 42 in poller.seen_issues["owner/repo"]

        poller.unmark_seen("owner/repo", 42)
        assert 42 not in poller.seen_issues["owner/repo"]

    def test_unmark_unknown_repo_is_noop(self, state_file: Path) -> None:
        poller = IssuePoller(
            github=MagicMock(),
            username="tester",
            repos=["owner/repo"],
            state_file=state_file,
        )
        # Should not raise and should not materialize an empty entry.
        poller.unmark_seen("owner/other-repo", 99)

    def test_unmark_unknown_issue_is_noop(self, state_file: Path) -> None:
        poller = IssuePoller(
            github=MagicMock(),
            username="tester",
            repos=["owner/repo"],
            state_file=state_file,
        )
        poller.mark_seen("owner/repo", 1)
        poller.unmark_seen("owner/repo", 999)
        assert poller.seen_issues["owner/repo"] == {1}

    def test_unmark_persists_state_to_disk(self, state_file: Path) -> None:
        """State must persist so a daemon restart between the un-mark
        and the next poll doesn't forget the re-queue."""
        poller = IssuePoller(
            github=MagicMock(),
            username="tester",
            repos=["owner/repo"],
            state_file=state_file,
        )
        poller.mark_seen("owner/repo", 55)
        poller.unmark_seen("owner/repo", 55)

        on_disk = json.loads(state_file.read_text())
        assert 55 not in on_disk["seen_issues"].get("owner/repo", [])


class TestForeignAssignmentFilter:
    @pytest.mark.asyncio
    async def test_self_assigned_issue_is_accepted(
        self, poller: IssuePoller, mock_github: MagicMock
    ) -> None:
        """When the operator self-assigned the issue, it is picked up."""
        mock_github.list_assigned_issues.return_value = [make_issue(1)]
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("alice", "alice"),
        ]

        results = await poller.poll()

        assert len(results) == 2  # both repos return the same issue
        assert all(r["issue"]["number"] == 1 for r in results)

    @pytest.mark.asyncio
    async def test_foreign_assigned_issue_is_filtered(
        self,
        poller: IssuePoller,
        mock_github: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An issue assigned to the operator by someone else is filtered out."""
        import logging

        mock_github.list_assigned_issues.return_value = [make_issue(1)]
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("bob", "alice"),
        ]

        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            results = await poller.poll()

        assert results == []
        # Must still be marked seen so we don't re-check the same issue every poll
        assert 1 in poller.seen_issues["owner/repo-a"]
        assert 1 in poller.seen_issues["owner/repo-b"]

        # Emits a foreign_assignment log event with identifying fields
        foreign_records = [
            r for r in caplog.records if r.getMessage() == "poll.issue.foreign_assignment"
        ]
        assert len(foreign_records) == 2  # one per repo
        for rec in foreign_records:
            assert rec.number == 1
            assert rec.assigner_login == "bob"
            assert rec.repo in {"owner/repo-a", "owner/repo-b"}

    @pytest.mark.asyncio
    async def test_most_recent_assigned_event_wins(
        self, poller: IssuePoller, mock_github: MagicMock
    ) -> None:
        """If the issue was self-assigned first but later re-assigned by
        someone else, the most recent assigner (bob) is what matters."""
        mock_github.list_assigned_issues.return_value = [make_issue(1)]
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("alice", "alice"),
            make_assigned_event("bob", "alice"),
        ]

        results = await poller.poll()

        assert results == []

    @pytest.mark.asyncio
    async def test_ignores_events_for_other_assignees(
        self, poller: IssuePoller, mock_github: MagicMock
    ) -> None:
        """Only `assigned` events where assignee == operator count. A later
        assignment to someone else on the same issue shouldn't hide an
        earlier self-assignment of the operator."""
        mock_github.list_assigned_issues.return_value = [make_issue(1)]
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("alice", "alice"),
            # Irrelevant: someone else was later added as a co-assignee
            make_assigned_event("bob", "charlie"),
        ]

        results = await poller.poll()

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_bot_assignment_is_filtered(
        self, poller: IssuePoller, mock_github: MagicMock
    ) -> None:
        """Assignment by a GitHub App / bot is treated as foreign."""
        mock_github.list_assigned_issues.return_value = [make_issue(1)]
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("github-actions[bot]", "alice"),
        ]

        results = await poller.poll()

        assert results == []

    @pytest.mark.asyncio
    async def test_no_assigned_events_is_filtered(
        self, poller: IssuePoller, mock_github: MagicMock
    ) -> None:
        """Defensive: if the issue shows up in `gh issue list --assignee alice`
        but has no `assigned` events naming alice (odd edge case — e.g. the
        events endpoint truncated), we refuse to run rather than guess."""
        mock_github.list_assigned_issues.return_value = [make_issue(1)]
        mock_github.list_assignment_events.return_value = []

        results = await poller.poll()

        assert results == []

    @pytest.mark.asyncio
    async def test_accept_foreign_assignments_bypasses_filter(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Per-repo opt-in: foreign assignments are accepted when the repo
        is listed in `accept_foreign_assignments`."""
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a", "owner/repo-b"],
            state_file=state_file,
            accept_foreign_assignments={"owner/repo-a"},
        )

        mock_github.list_assigned_issues.return_value = [make_issue(1)]
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("bob", "alice"),
        ]

        results = await poller.poll()

        # repo-a: opted in → accepts bob's assignment
        # repo-b: default → filters out bob's assignment
        assert len(results) == 1
        assert results[0]["repo"] == "owner/repo-a"
        assert results[0]["issue"]["number"] == 1

    @pytest.mark.asyncio
    async def test_filter_runs_once_per_new_issue(
        self, poller: IssuePoller, mock_github: MagicMock
    ) -> None:
        """A filtered issue is marked seen, so the events endpoint is NOT hit
        again for it on the next poll."""
        mock_github.list_assigned_issues.return_value = [make_issue(1)]
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("bob", "alice"),
        ]

        await poller.poll()
        call_count_after_first = mock_github.list_assignment_events.call_count

        await poller.poll()

        # No additional events calls on the second poll — issue is already seen
        assert mock_github.list_assignment_events.call_count == call_count_after_first
