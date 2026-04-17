"""Tests for PR merge watcher."""

from unittest.mock import AsyncMock

import pytest


class TestPRWatcher:
    @pytest.mark.asyncio
    async def test_check_merged_returns_true_when_merged(self) -> None:
        """Should return True when PR is merged."""
        from dev_sync.core.pr_watcher import PRWatcher

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
        from dev_sync.core.pr_watcher import PRWatcher

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
        from dev_sync.core.pr_watcher import PRWatcher

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
