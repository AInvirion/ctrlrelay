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
    assignees: list[dict] | list[str] | None = None,
) -> dict:
    # Normalize assignees to the {"login": ...} shape gh returns, while
    # tolerating plain strings for test ergonomics.
    resolved_assignees: list[dict] = []
    for a in assignees or []:
        if isinstance(a, dict):
            resolved_assignees.append(a)
        else:
            resolved_assignees.append({"login": str(a)})
    return {
        "number": number,
        "title": title,
        "state": "open",
        "body": "",
        "labels": labels or [],
        "assignees": resolved_assignees,
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
    # Default: no labeled issues on any repo. Tests that enable
    # include_labels set a return value (or side_effect) per test.
    gh.list_issues_by_label = AsyncMock(return_value=[])
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
            make_issue(
                1,
                "Looks operator-y but no exclude configured",
                labels=[{"name": "manual"}],
            ),
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
    async def test_poll_marks_issues_disabled_repo_and_skips_next_cycle(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A repo whose Issues feature is disabled returns a GitHubError with
        the specific `has disabled issues` message. That's a permanent state,
        not a transient failure, so the poller must mark it and not hit the
        `gh` API again on subsequent cycles.
        """
        from ctrlrelay.core.github import GitHubError
        from ctrlrelay.core.poller import IssuePoller

        calls: list[str] = []

        async def per_repo(repo: str, *, assignee: str):  # noqa: ARG001
            calls.append(repo)
            if repo == "owner/no-issues":
                raise GitHubError(
                    "gh failed: the 'owner/no-issues' repository has "
                    "disabled issues"
                )
            return [{"number": 1, "title": "ok"}]

        mock_github = MagicMock()
        mock_github.list_assigned_issues = AsyncMock(side_effect=per_repo)
        mock_github.list_assignment_events = AsyncMock(
            return_value=[make_assigned_event("testuser", "testuser")]
        )

        poller = IssuePoller(
            github=mock_github,
            username="testuser",
            repos=["owner/no-issues", "owner/ok"],
            state_file=tmp_path / "poller_state.json",
        )

        with caplog.at_level("INFO"):
            await poller.poll()
            await poller.poll()

        # First cycle hits both repos; second cycle skips the disabled one
        # entirely so only `owner/ok` is called again — proving we're not
        # re-hitting the API. Three total calls, not four.
        assert calls == ["owner/no-issues", "owner/ok", "owner/ok"]
        assert "owner/no-issues" in poller._issues_disabled_repos

        # Log once at detection, not every cycle.
        disabled_logs = [
            r for r in caplog.records
            if "poll.repo.issues_disabled" in r.getMessage()
        ]
        assert len(disabled_logs) == 1

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


class TestIncludeLabelsFilter:
    """Per-repo include_labels opts issues into the dev pipeline by label
    rather than assignment. See #80. Behavior contract:

    - Empty / missing ``include_labels`` preserves the pre-#80 query
      (``--assignee <user>``) and the pre-#80 self-assignment filter.
    - A configured list runs TARGETED queries: the existing assignee
      query plus one ``list_issues_by_label`` call per configured
      label; results are merged by issue number. This keeps the
      label-trigger path scale-safe on busy repos (the first cut
      fetched all open issues and silently capped at --limit).
    - Matching is case-insensitive.
    - Label-matched issues bypass the self-assignment event check.
    - Issue labeled AND assigned is processed exactly once per cycle.
    - Mixed repos apply their own include_labels independently.
    """

    @pytest.mark.asyncio
    async def test_targeted_queries_only_on_include_labels_repos(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Per-repo: repo A (opted in) runs assignee + per-label
        targeted queries; repo B keeps just the assignee query. The
        plumbing must be per-repo to avoid extra gh calls on repos
        that never configured label triggers."""
        mock_github.list_assigned_issues.return_value = []
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a", "owner/repo-b"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        await poller.poll()

        assignee_calls = mock_github.list_assigned_issues.await_args_list
        by_repo = {c.args[0]: c.kwargs.get("assignee") for c in assignee_calls}
        # Both repos still hit the assignee query (targeted, cheap).
        assert by_repo == {
            "owner/repo-a": "alice",
            "owner/repo-b": "alice",
        }
        # Only repo-a runs the label query, and only for its
        # configured label.
        label_calls = mock_github.list_issues_by_label.await_args_list
        assert [
            (c.args[0], c.kwargs.get("label"))
            for c in label_calls
        ] == [("owner/repo-a", "ctrlrelay:auto")]

    @pytest.mark.asyncio
    async def test_label_matched_unassigned_issue_is_accepted(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Issue with matching include_label and no assignee → accepted.
        This is the primary use case: a teammate labels an issue safe
        for the bot, no need for the operator to self-assign."""
        mock_github.list_assigned_issues.return_value = []
        mock_github.list_issues_by_label.return_value = [
            make_issue(1, "Safe to hand off", labels=[{"name": "ctrlrelay:auto"}]),
        ]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        results = await poller.poll()

        assert [r["issue"]["number"] for r in results] == [1]
        # The self-assignment event check MUST NOT be consulted — the
        # label is its own trust signal.
        mock_github.list_assignment_events.assert_not_called()
        # And the issue is now marked seen so the next poll doesn't
        # re-handoff the same work.
        assert 1 in poller.seen_issues["owner/repo-a"]

    @pytest.mark.asyncio
    async def test_unlabeled_unassigned_issue_is_dropped(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """An issue with neither the allow-list label nor assignment to
        the operator doesn't appear in the targeted queries → no
        surface. Trivially safe under the targeted-query design (gh's
        own filters do the filtering), but we still keep this test as
        a regression guard in case someone reintroduces an unfiltered
        fetch in a later change."""
        mock_github.list_assigned_issues.return_value = []
        mock_github.list_issues_by_label.return_value = []
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        results = await poller.poll()

        assert results == []
        assert 42 not in poller.seen_issues.get("owner/repo-a", set())

    @pytest.mark.asyncio
    async def test_assigned_without_label_still_accepted_in_label_mode(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Enabling include_labels must NOT regress the assignment path:
        an issue assigned to the operator (no allow-list label) still
        runs through the pre-#80 self-assignment event check."""
        mock_github.list_assigned_issues.return_value = [
            make_issue(7, "Assigned to me", assignees=["alice"]),
        ]
        mock_github.list_issues_by_label.return_value = []
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("alice", "alice"),
        ]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        results = await poller.poll()

        assert [r["issue"]["number"] for r in results] == [7]
        # Self-assignment event endpoint was hit — assignment path
        # retains its trust check.
        mock_github.list_assignment_events.assert_called_once()

    @pytest.mark.asyncio
    async def test_label_plus_assigned_deduped_to_one(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """An issue that matches BOTH triggers (label + assignee) must
        be returned exactly once, not duplicated in new_issues or
        double-added to seen_issues. The targeted-query merge
        explicitly dedupes by issue number so the same row coming
        from both --assignee and --label resolves to one entry."""
        labeled_and_assigned = make_issue(
            99,
            "Labeled AND assigned",
            labels=[{"name": "ctrlrelay:auto"}],
            assignees=["alice"],
        )
        # Both queries return the SAME issue — gh's --assignee and
        # --label overlap when the issue has both attributes. The
        # merge keys on issue number so only one survives.
        mock_github.list_assigned_issues.return_value = [labeled_and_assigned]
        mock_github.list_issues_by_label.return_value = [labeled_and_assigned]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        results = await poller.poll()

        assert [r["issue"]["number"] for r in results] == [99]
        # seen_issues is a set — but verify the intent: single occurrence.
        assert poller.seen_issues["owner/repo-a"] == {99}
        # Label wins, so self-assignment event check is skipped (cheaper
        # + same outcome — the label is the opt-in).
        mock_github.list_assignment_events.assert_not_called()

    @pytest.mark.asyncio
    async def test_include_label_match_is_case_insensitive(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Mirrors exclude_labels semantics: ``CtrlRelay:Auto`` matches
        ``ctrlrelay:auto``. Operators shouldn't have to worry about the
        exact casing their teammates apply on GitHub. (gh's server-side
        label filter is case-insensitive too; this test exercises the
        client-side match that runs after the targeted query returns.)"""
        mock_github.list_assigned_issues.return_value = []
        mock_github.list_issues_by_label.return_value = [
            make_issue(3, "Case differs", labels=[{"name": "CtrlRelay:Auto"}]),
        ]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        results = await poller.poll()

        assert [r["issue"]["number"] for r in results] == [3]

    @pytest.mark.asyncio
    async def test_exclude_label_wins_over_include_label(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """If an issue carries both an exclude and include label, the
        exclude precedent from #91 holds: the operator explicitly opted
        OUT, that beats the opt-IN label."""
        conflicting = make_issue(
            5,
            "Conflicting labels",
            labels=[{"name": "manual"}, {"name": "ctrlrelay:auto"}],
        )
        mock_github.list_assigned_issues.return_value = []
        mock_github.list_issues_by_label.return_value = [conflicting]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            exclude_labels_by_repo={"owner/repo-a": ["manual"]},
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        results = await poller.poll()

        assert results == []
        assert 5 in poller.seen_issues["owner/repo-a"]

    @pytest.mark.asyncio
    async def test_mixed_repos_apply_own_include_labels(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Repo A has include_labels; repo B doesn't. The same payload
        (labeled-but-unassigned issue) is accepted on A and dropped on
        B. Per-repo config must NOT leak across repos."""
        # Both repos get their own assignee query (targeted). Only
        # repo A gets a label query — repo B never opted in.
        async def assignee_per_repo(repo: str, *, assignee):
            assert assignee == "alice"
            return []

        async def label_per_repo(repo: str, *, label):
            if repo == "owner/repo-a" and label == "ctrlrelay:auto":
                return [
                    make_issue(
                        10,
                        "Labeled on repo A",
                        labels=[{"name": "ctrlrelay:auto"}],
                    ),
                ]
            return []

        mock_github.list_assigned_issues = AsyncMock(side_effect=assignee_per_repo)
        mock_github.list_issues_by_label = AsyncMock(side_effect=label_per_repo)
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a", "owner/repo-b"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        results = await poller.poll()

        assert [(r["repo"], r["issue"]["number"]) for r in results] == [
            ("owner/repo-a", 10),
        ]
        assert poller.seen_issues["owner/repo-a"] == {10}
        # repo-b never runs a label query AND its assignee query
        # returned empty — no surface, no seen entries.
        assert poller.seen_issues.get("owner/repo-b", set()) == set()
        # And repo-b never touched list_issues_by_label at all.
        label_repos_touched = {
            c.args[0]
            for c in mock_github.list_issues_by_label.await_args_list
        }
        assert "owner/repo-b" not in label_repos_touched

    @pytest.mark.asyncio
    async def test_empty_include_labels_preserves_pre_80_behavior(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """include_labels_by_repo={} for a repo (or missing) must
        result in the exact same ``gh issue list --assignee ...``
        query and self-assignment event check as the pre-#80 path.
        Guard against accidental behavior change for operators who
        never opted in."""
        mock_github.list_assigned_issues.return_value = [make_issue(1)]
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("alice", "alice"),
        ]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            # Explicitly empty — same as missing.
            include_labels_by_repo={"owner/repo-a": []},
        )

        results = await poller.poll()

        # Server filtered by --assignee, self-assignment event check ran.
        call = mock_github.list_assigned_issues.await_args
        assert call.kwargs.get("assignee") == "alice"
        mock_github.list_assignment_events.assert_called_once()
        assert [r["issue"]["number"] for r in results] == [1]

    @pytest.mark.asyncio
    async def test_foreign_assigned_not_marked_seen_so_later_label_triggers(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Codex P1 round-2 on #115: in include_labels mode, an issue
        that's currently foreign-assigned-without-label must NOT be
        marked seen. Otherwise a teammate adding an opt-in label on
        the NEXT poll cycle would be silently ignored because the
        seen-check would short-circuit.

        Poll 1: issue is foreign-assigned, no label → dropped, NOT seen.
        Poll 2: teammate added the label → issue appears in label query
        → seen-check passes → surfaced as label-triggered."""
        # Poll 1: issue appears in assignee query (assigned to alice by
        # someone else), has no label.
        foreign_no_label = make_issue(
            42, "foreign-assigned", assignees=["alice"],
        )
        mock_github.list_assigned_issues.return_value = [foreign_no_label]
        mock_github.list_issues_by_label.return_value = []
        # Assignment events show bob assigned alice (foreign).
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("alice", "bob"),
        ]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        results = await poller.poll()
        assert results == []
        # CRITICAL: issue must NOT be in seen_for_repo, so poll 2 can
        # re-evaluate it when a label is added.
        assert 42 not in poller.seen_issues.get("owner/repo-a", set())

        # Poll 2: teammate added the label. Assignee query still
        # returns the issue; label query now also returns it.
        labeled = make_issue(
            42, "foreign-assigned", assignees=["alice"],
            labels=[{"name": "ctrlrelay:auto"}],
        )
        mock_github.list_assigned_issues.return_value = [labeled]
        mock_github.list_issues_by_label.return_value = [labeled]

        results = await poller.poll()
        assert [r["issue"]["number"] for r in results] == [42]
        # Now seen (processed once).
        assert 42 in poller.seen_issues["owner/repo-a"]

    @pytest.mark.asyncio
    async def test_pre_80_repos_still_mark_foreign_assigned_seen(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Migration guard: repos WITHOUT include_labels keep the
        pre-#80 foreign-assignment dedup. Otherwise we'd re-check the
        assignment-events endpoint every poll for every foreign
        assignment in every repo, which is unnecessary traffic when
        the operator hasn't opted into label triggers at all."""
        mock_github.list_assigned_issues.return_value = [
            make_issue(7, "foreign-assigned", assignees=["alice"]),
        ]
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("alice", "bob"),
        ]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            # No include_labels configured → pre-#80 semantics.
        )

        await poller.poll()
        # Foreign-assigned IS marked seen on pre-#80 repos.
        assert 7 in poller.seen_issues["owner/repo-a"]

    @pytest.mark.asyncio
    async def test_seed_only_currently_eligible_in_include_labels_mode(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Codex P1 round-2 on #115: seed_current must only persist
        issues that CURRENTLY trigger a rule. A foreign-assigned issue
        with no opt-in label should NOT be seeded; otherwise a later
        label addition can never surface it (the seed already consumed
        the number)."""
        foreign_no_label = make_issue(
            100, "not triggered", assignees=["alice"],
        )
        labeled_unassigned = make_issue(
            200, "team-labeled", labels=[{"name": "ctrlrelay:auto"}],
        )
        self_assigned = make_issue(
            300, "self-assigned", assignees=["alice"],
        )

        # Both targeted queries return their respective matches.
        mock_github.list_assigned_issues.return_value = [
            foreign_no_label, self_assigned,
        ]
        mock_github.list_issues_by_label.return_value = [labeled_unassigned]
        # #100 foreign (bob assigned alice); #300 self-assigned.
        mock_github.list_assignment_events.side_effect = [
            [make_assigned_event("alice", "bob")],     # #100
            [make_assigned_event("alice", "alice")],   # #300
        ]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        await poller.seed_current()
        seen = poller.seen_issues["owner/repo-a"]
        # 200 (label-matched) and 300 (self-assigned) seeded.
        assert 200 in seen
        assert 300 in seen
        # 100 (foreign, no label) NOT seeded — a later label add
        # must be free to trigger.
        assert 100 not in seen

    @pytest.mark.asyncio
    async def test_events_check_failure_marks_seen_even_in_include_labels_mode(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Codex P2 round-4 on #115: an events-API exception during
        poll should mark the issue seen even on include_labels repos,
        so we don't retry forever during a GitHub outage. Tradeoff
        with the round-2 "foreign-assigned stays unmarked" fix is
        intentional — confirmed-foreign and transient-failure are
        different cases."""
        from ctrlrelay.core.github import GitHubError

        mock_github.list_assigned_issues.return_value = [
            make_issue(66, "assigned", assignees=["alice"]),
        ]
        mock_github.list_issues_by_label.return_value = []
        mock_github.list_assignment_events = AsyncMock(
            side_effect=GitHubError("events endpoint 502"),
        )
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        results = await poller.poll()
        assert results == []
        # Despite include_labels being configured, the transient
        # failure marks the issue seen — otherwise every poll during
        # the outage would retry the same /events call.
        assert 66 in poller.seen_issues["owner/repo-a"]

    @pytest.mark.asyncio
    async def test_label_query_failure_does_not_starve_assignee_path(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Codex P2 round-4 on #115: a transient failure on one
        label query must NOT drop the already-fetched assignee
        issues. Previously the whole repo was discarded if any
        targeted query failed."""
        from ctrlrelay.core.github import GitHubError

        assigned_self = make_issue(
            77, "self-assigned", assignees=["alice"],
        )
        mock_github.list_assigned_issues.return_value = [assigned_self]
        # Label query fails transiently.
        mock_github.list_issues_by_label = AsyncMock(
            side_effect=GitHubError("search index 503"),
        )
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("alice", "alice"),
        ]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        results = await poller.poll()
        # The self-assigned issue still flows — the label query's
        # failure is isolated.
        assert [r["issue"]["number"] for r in results] == [77]
        assert 77 in poller.seen_issues["owner/repo-a"]

    @pytest.mark.asyncio
    async def test_migration_clears_seen_issues_on_first_enable(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Codex P2 round-6 on #115: when an operator adds
        include_labels to a repo that already has persisted
        seen_issues, pre-existing foreign-assigned entries would
        permanently block label triggers on those issue numbers.
        The one-shot migration clears seen_issues for such repos on
        daemon startup so the next poll re-evaluates everything."""
        import json as _json

        # Simulate pre-#80 state: seen_issues has foreign-assigned
        # numbers; no include_labels_migrated record yet.
        state_file.write_text(_json.dumps({
            "seen_issues": {"owner/repo-a": [100, 200, 300]},
            "last_poll": "2026-04-17T00:00:00+00:00",
        }))

        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            # First enable: include_labels freshly added to config.
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        # Migration cleared the repo's seen_issues.
        assert poller.seen_issues.get("owner/repo-a", set()) == set()
        assert "owner/repo-a" in poller.include_labels_migrated
        # State persisted — next daemon startup reads the migration
        # flag and skips re-clearing.
        persisted = _json.loads(state_file.read_text())
        assert persisted["include_labels_migrated"] == ["owner/repo-a"]

    @pytest.mark.asyncio
    async def test_migration_is_idempotent_across_restarts(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Once migrated, a repo's later polls can re-populate
        seen_issues under the new semantics. A subsequent daemon
        restart must NOT re-clear those entries (the migration
        already ran)."""
        import json as _json

        # Simulate post-migration state: repo is already in migrated
        # set and has accumulated new seen_issues under the new rules.
        state_file.write_text(_json.dumps({
            "seen_issues": {"owner/repo-a": [55]},
            "include_labels_migrated": ["owner/repo-a"],
            "last_poll": "2026-04-20T00:00:00+00:00",
        }))

        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        # No re-migration: seen_issues preserved.
        assert poller.seen_issues["owner/repo-a"] == {55}

    @pytest.mark.asyncio
    async def test_seed_isolates_per_label_query_failures(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Codex P2 round-5 on #115: the per-label failure isolation
        in poll() must also apply in seed_current(). Otherwise one
        flaky label query during startup drops the assignee-backlog
        seed, and the first successful poll spins up pipelines for
        pre-existing issues that should have been seen."""
        from ctrlrelay.core.github import GitHubError

        assigned_self = make_issue(
            11, "self-assigned pre-startup", assignees=["alice"],
        )
        mock_github.list_assigned_issues.return_value = [assigned_self]
        # Label query raises on startup.
        mock_github.list_issues_by_label = AsyncMock(
            side_effect=GitHubError("label index flaky"),
        )
        mock_github.list_assignment_events.return_value = [
            make_assigned_event("alice", "alice"),
        ]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        await poller.seed_current()
        # Assignee-backlog is still seeded despite the label query
        # failing — pre-existing self-assigned work doesn't get
        # treated as new on the first poll.
        assert 11 in poller.seen_issues["owner/repo-a"]

    @pytest.mark.asyncio
    async def test_seed_conservatively_seeds_when_events_api_flakes(
        self, mock_github: MagicMock, state_file: Path
    ) -> None:
        """Codex P1 round-3 on #115: if the timeline/events API fails
        transiently during seed_current, we must NOT leave the issue
        unseeded. The first poll would then treat a pre-existing
        assigned-backlog issue as new work and spin up a pipeline for
        it. Seed conservatively (treat as self-assigned-for-seeding)
        so backlog stays quiet; a later label addition can still
        trigger once the API recovers."""
        from ctrlrelay.core.github import GitHubError

        assigned_backlog = make_issue(
            55, "pre-existing backlog", assignees=["alice"],
        )
        mock_github.list_assigned_issues.return_value = [assigned_backlog]
        mock_github.list_issues_by_label.return_value = []
        # Timeline lookup fails — transient GH outage during startup.
        mock_github.list_assignment_events = AsyncMock(
            side_effect=GitHubError("API down"),
        )
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        await poller.seed_current()
        # Conservative seed: backlog is marked seen so first poll
        # doesn't treat it as new work.
        assert 55 in poller.seen_issues["owner/repo-a"]

    @pytest.mark.asyncio
    async def test_include_label_emits_structured_log(
        self,
        mock_github: MagicMock,
        state_file: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Label-triggered acceptance gets its own log event so
        operators can audit which issues the bot picked up and why."""
        mock_github.list_assigned_issues.return_value = []
        mock_github.list_issues_by_label.return_value = [
            make_issue(11, "t", labels=[{"name": "ctrlrelay:auto"}]),
        ]
        poller = IssuePoller(
            github=mock_github,
            username="alice",
            repos=["owner/repo-a"],
            state_file=state_file,
            include_labels_by_repo={"owner/repo-a": ["ctrlrelay:auto"]},
        )

        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.poller"):
            await poller.poll()

        records = [
            r for r in caplog.records
            if r.getMessage() == "poll.issue.included_by_label"
        ]
        assert len(records) == 1
        rec = records[0]
        assert rec.repo == "owner/repo-a"
        assert rec.issue_number == 11
        assert rec.matched_label == "ctrlrelay:auto"
