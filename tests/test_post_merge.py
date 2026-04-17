"""Tests for post-merge handler."""

from unittest.mock import AsyncMock

import pytest


class TestPostMergeHandler:
    @pytest.mark.asyncio
    async def test_handle_merge_closes_issue(self) -> None:
        """Should close issue after successful merge."""
        from dev_sync.pipelines.post_merge import handle_merge

        mock_github = AsyncMock()
        mock_transport = AsyncMock()

        await handle_merge(
            repo="owner/repo",
            pr_number=42,
            issue_number=123,
            github=mock_github,
            transport=mock_transport,
        )

        mock_github.close_issue.assert_called_once_with(
            "owner/repo",
            123,
            "Closed by PR #42",
        )
        mock_transport.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_watch_and_handle_merge(self) -> None:
        """Should watch for merge then close issue."""
        from dev_sync.pipelines.post_merge import watch_and_handle_merge

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        mock_transport = AsyncMock()

        result = await watch_and_handle_merge(
            repo="owner/repo",
            pr_number=42,
            issue_number=123,
            github=mock_github,
            transport=mock_transport,
            poll_interval=1,
            timeout=5,
        )

        assert result is True
        mock_github.close_issue.assert_called_once()
