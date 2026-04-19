"""Tests for PR merge watcher."""

from unittest.mock import AsyncMock

import pytest


class TestPRWatcher:
    @pytest.mark.asyncio
    async def test_check_merged_returns_true_when_merged(self) -> None:
        """Should return True when PR is merged."""
        from ctrlrelay.core.pr_watcher import PRWatcher

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {
            "number": 42,
            "state": "MERGED",
            "mergedAt": "2026-04-17T12:00:00Z",
        }

        watcher = PRWatcher(github=mock_github)
        is_merged = await watcher.check_merged("owner/repo", 42)

        assert is_merged is True

    @pytest.mark.asyncio
    async def test_check_merged_returns_false_when_open(self) -> None:
        """Should return False when PR is still open."""
        from ctrlrelay.core.pr_watcher import PRWatcher

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {
            "number": 42,
            "state": "OPEN",
            "mergedAt": None,
        }

        watcher = PRWatcher(github=mock_github)
        is_merged = await watcher.check_merged("owner/repo", 42)

        assert is_merged is False

    @pytest.mark.asyncio
    async def test_wait_for_merge_times_out(self) -> None:
        """Should return False when timeout reached."""
        from ctrlrelay.core.pr_watcher import PRWatcher

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {
            "number": 42,
            "state": "OPEN",
        }

        watcher = PRWatcher(github=mock_github, poll_interval=1)

        # Short timeout for test
        result = await watcher.wait_for_merge("owner/repo", 42, timeout=2)

        assert result is False
        assert mock_github.get_pr_state.call_count >= 2

    @pytest.mark.asyncio
    async def test_wait_for_merge_survives_transient_gh_errors(
        self, caplog
    ) -> None:
        """Codex P1: a single `gh pr view` failure during a multi-day
        watch MUST NOT abort the loop. Transient errors are logged and
        the loop keeps polling."""
        import logging

        from ctrlrelay.core.github import GitHubError
        from ctrlrelay.core.pr_watcher import PRWatcher

        calls = 0

        async def flaky_then_merged(*_a, **_kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise GitHubError("gh failed: HTTP 503")
            if calls == 2:
                raise TimeoutError("gh timed out")
            return {"state": "MERGED"}

        mock_github = AsyncMock()
        mock_github.get_pr_state.side_effect = flaky_then_merged

        watcher = PRWatcher(github=mock_github, poll_interval=0)

        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.pr_watcher"):
            result = await watcher.wait_for_merge(
                "owner/repo", 42, timeout=60,
            )

        assert result is True
        assert calls == 3
        # Two transient errors logged as structured events.
        transient = [
            r for r in caplog.records
            if "pr_watch.transient_error" in r.getMessage()
        ]
        assert len(transient) == 2

    @pytest.mark.asyncio
    async def test_wait_for_merge_abandons_after_consecutive_failure_cap(
        self, caplog
    ) -> None:
        """Codex P2: permanent gh failures (expired auth, 404, missing
        binary) surface as "transient-looking" exceptions. Without a
        cap, they'd sleep+retry for the full 7-day window. Fail fast
        after _TRANSIENT_FAILURE_CAP consecutive errors."""
        import logging

        from ctrlrelay.core.github import GitHubError
        from ctrlrelay.core.pr_watcher import (
            _TRANSIENT_FAILURE_CAP,
            PRWatcher,
        )

        mock_github = AsyncMock()
        mock_github.get_pr_state.side_effect = GitHubError(
            "gh failed: GraphQL: Could not resolve to a Repository (permanent)"
        )

        watcher = PRWatcher(github=mock_github, poll_interval=0)

        with caplog.at_level(logging.INFO, logger="ctrlrelay.core.pr_watcher"):
            with pytest.raises(GitHubError):
                await watcher.wait_for_merge(
                    "owner/repo", 42, timeout=60 * 60 * 24 * 7,
                )

        # Exactly _TRANSIENT_FAILURE_CAP failures, then abandon.
        assert mock_github.get_pr_state.call_count == _TRANSIENT_FAILURE_CAP
        # Each transient_error logs the current count.
        transient = [
            r for r in caplog.records
            if "pr_watch.transient_error" in r.getMessage()
        ]
        assert len(transient) == _TRANSIENT_FAILURE_CAP
        # And the final abandon event fired.
        assert any(
            "pr_watch.abandoned_after_too_many_errors" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_wait_for_merge_failure_counter_resets_on_success(
        self
    ) -> None:
        """A successful poll must reset the consecutive-failure counter —
        a flaky network that recovers shouldn't eventually trip the
        abandon path just from accumulated intermittent errors."""
        from ctrlrelay.core.github import GitHubError
        from ctrlrelay.core.pr_watcher import PRWatcher

        calls = 0

        async def flaky_intermittent(*_a, **_kw):
            nonlocal calls
            calls += 1
            # Pattern: every 3rd call fails. Over a long watch that's
            # many failures in aggregate but never 10 in a row.
            if calls % 3 == 0:
                raise GitHubError("gh transient")
            if calls >= 30:
                return {"state": "MERGED"}
            return {"state": "OPEN"}

        mock_github = AsyncMock()
        mock_github.get_pr_state.side_effect = flaky_intermittent

        watcher = PRWatcher(github=mock_github, poll_interval=0)
        result = await watcher.wait_for_merge("owner/repo", 42, timeout=60)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_merge_propagates_cancellation(self) -> None:
        """Shutdown must not be swallowed by the transient-error guard."""
        import asyncio

        from ctrlrelay.core.pr_watcher import PRWatcher

        async def always_cancel(*_a, **_kw):
            raise asyncio.CancelledError()

        mock_github = AsyncMock()
        mock_github.get_pr_state.side_effect = always_cancel

        watcher = PRWatcher(github=mock_github, poll_interval=0)
        with pytest.raises(asyncio.CancelledError):
            await watcher.wait_for_merge("owner/repo", 42, timeout=60)
