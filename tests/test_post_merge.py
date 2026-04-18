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
    async def test_transport_factory_called_only_after_merge_detected(self) -> None:
        """Codex P2: lazy transport. A 7-day watch would build the
        transport at start and hold it; bridge restarts during that
        window would leave us with a stale connection when the merge
        finally lands. The factory must be invoked only AFTER
        check_merged returns True, right before handle_merge."""
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        factory_call_count = 0

        async def factory():
            nonlocal factory_call_count
            factory_call_count += 1
            return AsyncMock()

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
        # Factory invoked exactly once and only because the watcher
        # reached the merged branch.
        assert factory_call_count == 1

    @pytest.mark.asyncio
    async def test_transport_factory_not_called_on_timeout(self) -> None:
        """No merge → no notification → factory must not be invoked.
        Avoids creating an unnecessary transport connection for a PR
        that sat in review and was eventually abandoned."""
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "OPEN"}

        factory_call_count = 0

        async def factory():
            nonlocal factory_call_count
            factory_call_count += 1
            return AsyncMock()

        outcome = await pr_watch_task(
            repo="owner/repo",
            issue_number=77,
            pr_url="",
            pr_number=42,
            session_id=None,
            github=mock_github,
            transport_factory=factory,
            poll_interval=0,
            timeout=0,
        )

        assert outcome["timed_out"] is True
        assert factory_call_count == 0

    @pytest.mark.asyncio
    async def test_retry_does_not_duplicate_close_issue_comment(
        self, caplog
    ) -> None:
        """Codex P2: if close_issue succeeds but transport.send fails,
        the retry MUST NOT re-run close_issue and post a duplicate
        "Closed by PR #N" comment on the issue."""
        import logging

        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        # close_issue succeeds on first call; no retries of it.
        # transport.send raises twice, succeeds third.
        send_calls = 0

        def make_transport():
            nonlocal send_calls
            t = AsyncMock()

            async def send(_msg):
                nonlocal send_calls
                send_calls += 1
                if send_calls < 3:
                    raise RuntimeError("bridge transient")

            t.send.side_effect = send
            return t

        attempt_transports: list = []

        async def factory():
            t = make_transport()
            attempt_transports.append(t)
            return t

        import ctrlrelay.pipelines.post_merge as pm
        orig = pm._HANDLE_MERGE_RETRY_BASE_DELAY
        pm._HANDLE_MERGE_RETRY_BASE_DELAY = 0
        try:
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
        finally:
            pm._HANDLE_MERGE_RETRY_BASE_DELAY = orig

        assert outcome["merged"] is True
        # close_issue called EXACTLY ONCE despite 3 retry attempts.
        assert mock_github.close_issue.call_count == 1
        # transport.send called 3 times (once per attempt).
        assert send_calls == 3

    @pytest.mark.asyncio
    async def test_retry_rebuilds_transport_on_each_attempt(
        self
    ) -> None:
        """Codex P2: factory is invoked inside each retry attempt, so a
        bridge that drops its socket between retries gets a fresh
        connection on the next try — not a stale dead socket."""
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}
        # close_issue fails twice, succeeds third.
        close_calls = 0

        async def flaky_close(repo, issue_number, comment):
            nonlocal close_calls
            close_calls += 1
            if close_calls < 3:
                raise RuntimeError("gh transient")

        mock_github.close_issue.side_effect = flaky_close

        factory_calls = 0
        built_transports: list = []

        async def factory():
            nonlocal factory_calls
            factory_calls += 1
            t = AsyncMock()
            built_transports.append(t)
            return t

        import ctrlrelay.pipelines.post_merge as pm
        orig = pm._HANDLE_MERGE_RETRY_BASE_DELAY
        pm._HANDLE_MERGE_RETRY_BASE_DELAY = 0
        try:
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
        finally:
            pm._HANDLE_MERGE_RETRY_BASE_DELAY = orig

        assert outcome["merged"] is True
        # One factory call per retry attempt (3 total).
        assert factory_calls == 3
        # Each built transport was closed at the end of its attempt.
        for t in built_transports:
            t.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_handle_merge_transient_failure_retries(self, caplog) -> None:
        """Codex P2: transient `gh issue close` failure at merge time must
        not abandon the post-merge cleanup. Retry with backoff; after
        a success, the issue is closed."""
        import logging

        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        # First two close_issue attempts fail; third succeeds.
        close_calls = 0

        async def flaky_close(repo, issue_number, comment):
            nonlocal close_calls
            close_calls += 1
            if close_calls < 3:
                raise RuntimeError("gh transient at close")

        mock_github.close_issue.side_effect = flaky_close

        async def factory():
            return AsyncMock()

        # Patch the retry base delay to 0 so the test doesn't sleep.
        import ctrlrelay.pipelines.post_merge as pm
        orig = pm._HANDLE_MERGE_RETRY_BASE_DELAY
        pm._HANDLE_MERGE_RETRY_BASE_DELAY = 0
        try:
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
        finally:
            pm._HANDLE_MERGE_RETRY_BASE_DELAY = orig

        assert outcome["merged"] is True
        # close_issue was retried until it succeeded.
        assert close_calls == 3
        # At least two retry events logged (the first two failures).
        retry_events = [
            r for r in caplog.records
            if "dev.pr.handle_merge_retry" in r.getMessage()
        ]
        assert len(retry_events) == 2

    @pytest.mark.asyncio
    async def test_handle_merge_exhausts_retries_and_logs_failure(
        self, caplog
    ) -> None:
        """After all retries exhausted, log dev.pr.watch_failed and
        surface outcome['failed'] so the operator can act."""
        import logging

        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}
        mock_github.close_issue.side_effect = RuntimeError("permanent auth error")

        async def factory():
            return AsyncMock()

        import ctrlrelay.pipelines.post_merge as pm
        orig = pm._HANDLE_MERGE_RETRY_BASE_DELAY
        pm._HANDLE_MERGE_RETRY_BASE_DELAY = 0
        try:
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
        finally:
            pm._HANDLE_MERGE_RETRY_BASE_DELAY = orig

        # Task records the failure but doesn't crash.
        assert outcome["failed"] == "RuntimeError"
        # All attempts consumed.
        assert mock_github.close_issue.call_count == pm._HANDLE_MERGE_RETRY_ATTEMPTS
        # Last event is dev.pr.watch_failed.
        assert any(
            "dev.pr.watch_failed" in r.getMessage() for r in caplog.records
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
