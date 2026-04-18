"""Tests for post-merge handler."""

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest


class TestPostMergeHandler:
    @pytest.mark.asyncio
    async def test_handle_merge_closes_issue(self) -> None:
        """Should close issue after successful merge."""
        from ctrlrelay.pipelines.post_merge import handle_merge

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
        from ctrlrelay.pipelines.post_merge import watch_and_handle_merge

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


class TestPRWatchTask:
    """Cover the background-safe wrapper the poller uses (#55)."""

    def test_default_timeout_covers_typical_review_cycle(self) -> None:
        """Codex P2: review cycles commonly exceed 24h. Default must not
        abandon watchers before a normal merge lands."""
        from ctrlrelay.pipelines.post_merge import DEFAULT_PR_WATCH_TIMEOUT

        # 7 days in seconds; anything shorter risks silent timeouts on
        # normal review lag.
        assert DEFAULT_PR_WATCH_TIMEOUT >= 7 * 24 * 60 * 60

    @pytest.mark.asyncio
    async def test_merged_logs_event_and_closes_issue(self, caplog) -> None:
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        mock_transport = AsyncMock()

        async def factory():
            return mock_transport

        with caplog.at_level(logging.INFO, logger="ctrlrelay.pipelines.post_merge"):
            outcome = await pr_watch_task(
                repo="owner/repo",
                issue_number=77,
                pr_url="https://github.com/owner/repo/pull/42",
                pr_number=42,
                session_id="sess-1",
                github=mock_github,
                transport_factory=factory,
                poll_interval=0,
                timeout=5,
            )

        assert outcome == {
            "merged": True, "timed_out": False,
            "cancelled": False, "failed": None,
        }
        events = [r.getMessage() for r in caplog.records]
        assert any("dev.pr.watching" in e for e in events)
        assert any("dev.pr.merged" in e for e in events)
        mock_github.close_issue.assert_called_once()
        mock_transport.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timeout_logs_watch_timeout(self, caplog) -> None:
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "OPEN"}

        async def factory():
            return None

        with caplog.at_level(logging.INFO, logger="ctrlrelay.pipelines.post_merge"):
            outcome = await pr_watch_task(
                repo="owner/repo",
                issue_number=77,
                pr_url="https://github.com/owner/repo/pull/42",
                pr_number=42,
                session_id="sess-timeout",
                github=mock_github,
                transport_factory=factory,
                poll_interval=0,
                timeout=0,   # exits immediately; OPEN → timed out
            )

        assert outcome["timed_out"] is True
        assert outcome["merged"] is False
        assert any(
            "dev.pr.watch_timeout" in r.getMessage() for r in caplog.records
        )
        mock_github.close_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancellation_is_logged_and_reraised(self, caplog) -> None:
        """Poller shutdown cancels the watcher; the task must re-raise
        CancelledError so the caller's gather sees it."""
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        # Slow github that gets cancelled mid-poll.
        sleep_event = asyncio.Event()

        async def slow_get_state(*_a, **_kw):
            await sleep_event.wait()
            return {"state": "OPEN"}

        mock_github = AsyncMock()
        mock_github.get_pr_state.side_effect = slow_get_state

        async def factory():
            return None

        async def run():
            return await pr_watch_task(
                repo="owner/repo",
                issue_number=77,
                pr_url="",
                pr_number=42,
                session_id=None,
                github=mock_github,
                transport_factory=factory,
                poll_interval=0,
                timeout=60,
            )

        task = asyncio.create_task(run())
        await asyncio.sleep(0)  # let it start
        task.cancel()

        with caplog.at_level(logging.INFO, logger="ctrlrelay.pipelines.post_merge"):
            with pytest.raises(asyncio.CancelledError):
                await task

        assert any(
            "dev.pr.watch_cancelled" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_transport_factory_failure_is_non_fatal(self, caplog) -> None:
        """If the transport factory raises, we log and proceed with
        transport=None — the merge detection itself must not depend on
        the notification channel being up."""
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        async def factory():
            raise RuntimeError("bridge socket missing")

        with caplog.at_level(logging.INFO, logger="ctrlrelay.pipelines.post_merge"):
            outcome = await pr_watch_task(
                repo="owner/repo",
                issue_number=77,
                pr_url="",
                pr_number=42,
                session_id=None,
                github=mock_github,
                transport_factory=factory,
                poll_interval=0,
                timeout=5,
            )

        assert outcome["merged"] is True
        assert any(
            "dev.pr.watch_transport_failed" in r.getMessage()
            for r in caplog.records
        )
        # Merge detection still closed the issue even without transport.
        mock_github.close_issue.assert_called_once()
