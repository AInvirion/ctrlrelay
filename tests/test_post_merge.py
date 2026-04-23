"""Tests for post-merge handler."""

import asyncio
import logging
from pathlib import Path
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
        # Post-merge is now split into comment + close + notify for
        # idempotent retry. Happy path: each runs once.
        mock_github.comment_on_issue.assert_called_once()
        mock_github._run_gh.assert_called()
        mock_transport.send.assert_called_once()
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
        """Codex P2: if comment+close succeed but transport.send fails,
        the retry MUST NOT re-post the "Closed by PR #N" comment or
        re-run `gh issue close` — each side-effect runs at most once."""
        import logging

        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        # comment_on_issue + _run_gh succeed on first attempt.
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
        # Each github-side step runs EXACTLY ONCE despite 3 retry attempts.
        assert mock_github.comment_on_issue.call_count == 1
        close_calls = [
            c for c in mock_github._run_gh.call_args_list
            if c.args[:2] == ("issue", "close")
        ]
        assert len(close_calls) == 1
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
        # comment_on_issue fails twice, succeeds third — forces three
        # retry attempts, each of which should rebuild the transport.
        comment_calls = 0

        async def flaky_comment(repo, issue_number, comment):
            nonlocal comment_calls
            comment_calls += 1
            if comment_calls < 3:
                raise RuntimeError("gh transient")

        mock_github.comment_on_issue.side_effect = flaky_comment

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
        a success, the issue is closed. With idempotent splitting, a
        successful comment followed by a failing close should retry
        ONLY the close step, not re-post the comment."""
        import logging

        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        # First two `gh issue close` attempts fail; third succeeds.
        run_gh_close_calls = 0

        async def flaky_run_gh(*args, **_kw):
            # Only the close subcommand is flaky.
            if args[:2] == ("issue", "close"):
                nonlocal run_gh_close_calls
                run_gh_close_calls += 1
                if run_gh_close_calls < 3:
                    raise RuntimeError("gh transient at close")

        mock_github._run_gh.side_effect = flaky_run_gh

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
        # `gh issue close` was retried until it succeeded.
        assert run_gh_close_calls == 3
        # Comment is idempotent: posted exactly once even across retries.
        assert mock_github.comment_on_issue.call_count == 1
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
        # Permanent failure on `gh issue close` — comment succeeds once,
        # then every close attempt fails.
        mock_github._run_gh.side_effect = RuntimeError("permanent auth error")

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
        # All close attempts consumed (comment is idempotent, posted once).
        assert mock_github.comment_on_issue.call_count == 1
        close_calls = [
            c for c in mock_github._run_gh.call_args_list
            if c.args[:2] == ("issue", "close")
        ]
        assert len(close_calls) == pm._HANDLE_MERGE_RETRY_ATTEMPTS
        # Last event is dev.pr.watch_failed.
        assert any(
            "dev.pr.watch_failed" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_factory_returning_none_is_clean_no_retry(
        self, caplog
    ) -> None:
        """Codex P2: a factory that returns None legitimately (e.g.
        file_mock transport or Telegram socket absent) means "no
        notification channel configured". The retry loop MUST NOT
        treat that as a failure — every merge would otherwise pay full
        retry backoff and report outcome["failed"] on non-Telegram
        setups."""
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        factory_calls = 0

        async def factory():
            nonlocal factory_calls
            factory_calls += 1
            return None  # no notification channel configured

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

        # Clean success: merged + closed, no failure, no retry churn.
        assert outcome["merged"] is True
        assert outcome["failed"] is None
        assert factory_calls == 1
        mock_github.comment_on_issue.assert_called_once()
        close_calls = [
            c for c in mock_github._run_gh.call_args_list
            if c.args[:2] == ("issue", "close")
        ]
        assert len(close_calls) == 1
        # No retry events fired.
        retry_events = [
            r for r in caplog.records
            if "dev.pr.handle_merge_retry" in r.getMessage()
        ]
        assert retry_events == []

    @pytest.mark.asyncio
    async def test_transient_transport_factory_failure_retries_and_recovers(
        self, caplog
    ) -> None:
        """Codex P2: a transient bridge outage right at merge time must
        not permanently drop the notification. The factory is retried
        on each attempt, and once it recovers the notification lands."""
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        factory_calls = 0
        built: list = []

        async def factory():
            nonlocal factory_calls
            factory_calls += 1
            if factory_calls < 3:
                raise RuntimeError("bridge socket missing")
            t = AsyncMock()
            built.append(t)
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
        assert outcome["failed"] is None
        # Comment + close stayed idempotent across the retry loop.
        mock_github.comment_on_issue.assert_called_once()
        close_calls = [
            c for c in mock_github._run_gh.call_args_list
            if c.args[:2] == ("issue", "close")
        ]
        assert len(close_calls) == 1
        # Factory was retried until it recovered.
        assert factory_calls == 3
        # The recovered transport received the notification.
        assert len(built) == 1
        built[0].send.assert_awaited_once()
        # Transient failures were surfaced as structured events.
        failed_events = [
            r for r in caplog.records
            if "dev.pr.watch_transport_failed" in r.getMessage()
        ]
        assert len(failed_events) == 2

    @pytest.mark.asyncio
    async def test_persistent_transport_factory_failure_closes_issue_and_reports_failure(
        self, caplog
    ) -> None:
        """If the factory NEVER recovers, the merge is still detected,
        the issue is still closed exactly once (comment + gh close),
        and the task surfaces the failure in outcome["failed"] so an
        operator can see the notification was lost."""
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        async def factory():
            raise RuntimeError("bridge permanently gone")

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

        # Merge WAS detected — record it even though cleanup degraded.
        assert outcome["merged"] is True
        # But the task surfaces the notification failure.
        assert outcome["failed"] == "RuntimeError"
        # Issue closed exactly once — idempotency held across retries.
        mock_github.comment_on_issue.assert_called_once()
        close_calls = [
            c for c in mock_github._run_gh.call_args_list
            if c.args[:2] == ("issue", "close")
        ]
        assert len(close_calls) == 1
        # dev.pr.merged fired (merge was real) AND dev.pr.watch_failed
        # fired at the end (notification path exhausted retries).
        assert any(
            "dev.pr.merged" in r.getMessage() for r in caplog.records
        )
        assert any(
            "dev.pr.watch_failed" in r.getMessage() for r in caplog.records
        )


class TestPRWatchTaskPersistence:
    """pr_watch_task must round-trip the pr_watches state_db table so
    the watcher survives a poller restart. Row written on spawn,
    removed on merged / timed_out, LEFT BEHIND on cancel."""

    @pytest.mark.asyncio
    async def test_row_written_on_spawn(self, tmp_path: Path) -> None:
        """Acceptance: spawn writes a row keyed by (repo, pr_number)
        with all the columns from the issue body."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db = StateDB(tmp_path / "state.db")

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        async def factory():
            return None

        # Capture the row DURING the watch by observing it after run.
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
            state_db=db,
        )
        assert outcome["merged"] is True
        # Merged → row removed. The write DID happen — we verify that
        # in the cancel test. Here we assert the terminal cleanup.
        assert db.list_pr_watches() == []
        db.close()

    @pytest.mark.asyncio
    async def test_row_removed_on_merge(self, tmp_path: Path) -> None:
        """dev.pr.merged is terminal: row must be dropped so the next
        poller startup doesn't rehydrate a watcher that already did its
        job."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db = StateDB(tmp_path / "state.db")
        # Seed a stale row for this PR to simulate a restart scenario —
        # the row must be gone after the merge completes.
        db.add_pr_watch(
            repo="owner/repo", pr_number=42, issue_number=77,
            session_id="prev-sess",
            pr_url="https://github.com/owner/repo/pull/42",
        )
        assert len(db.list_pr_watches()) == 1

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        async def factory():
            return None

        outcome = await pr_watch_task(
            repo="owner/repo", issue_number=77,
            pr_url="https://github.com/owner/repo/pull/42",
            pr_number=42, session_id="sess-1",
            github=mock_github, transport_factory=factory,
            poll_interval=0, timeout=5, state_db=db,
        )
        assert outcome["merged"] is True
        assert db.list_pr_watches() == []
        db.close()

    @pytest.mark.asyncio
    async def test_row_removed_on_timeout(self, tmp_path: Path) -> None:
        """dev.pr.watch_timeout is terminal: the PR sat in review past
        the watch window; we're giving up. Drop the row so a restart
        doesn't resurrect the same dead watcher."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db = StateDB(tmp_path / "state.db")

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "OPEN"}

        async def factory():
            return None

        outcome = await pr_watch_task(
            repo="owner/repo", issue_number=77,
            pr_url="https://github.com/owner/repo/pull/42",
            pr_number=42, session_id="sess-1",
            github=mock_github, transport_factory=factory,
            poll_interval=0, timeout=0,  # immediate timeout
            state_db=db,
        )
        assert outcome["timed_out"] is True
        assert db.list_pr_watches() == []
        db.close()

    @pytest.mark.asyncio
    async def test_row_kept_on_cancellation(self, tmp_path: Path) -> None:
        """Acceptance: dev.pr.watch_cancelled must LEAVE the row
        behind. Cancellation is what a poller shutdown does to every
        in-flight watcher; if we deleted the row here, the next startup
        couldn't rehydrate and the PR's post-merge automation would
        silently vanish for the rest of the 7-day window."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db = StateDB(tmp_path / "state.db")

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
                repo="owner/repo", issue_number=77,
                pr_url="https://github.com/owner/repo/pull/42",
                pr_number=42, session_id="sess-1",
                github=mock_github, transport_factory=factory,
                poll_interval=0, timeout=60, state_db=db,
            )

        task = asyncio.create_task(run())
        # Yield so the task gets to add_pr_watch and into the watcher.
        await asyncio.sleep(0)
        # At this point the row MUST already be persisted — that's
        # what makes rehydration possible on the next startup.
        rows = db.list_pr_watches()
        assert len(rows) == 1
        assert rows[0]["repo"] == "owner/repo"
        assert rows[0]["pr_number"] == 42
        assert rows[0]["issue_number"] == 77
        assert rows[0]["session_id"] == "sess-1"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Cancellation must have LEFT the row behind.
        rows = db.list_pr_watches()
        assert len(rows) == 1
        assert rows[0]["pr_number"] == 42
        db.close()

    @pytest.mark.asyncio
    async def test_row_kept_when_handle_merge_raises(
        self, tmp_path: Path
    ) -> None:
        """P1 regression (codex review on PR #111): if post-merge cleanup
        fails (retries exhausted, bridge permanently dead), the durable
        row MUST stay so the next poller startup re-runs handle_merge.
        If the row is deleted before cleanup finishes, a shutdown in the
        window between merge detection and issue close silently loses
        the close + notify — exactly the bug persistence was meant to
        prevent."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines import post_merge as pm
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db = StateDB(tmp_path / "state.db")

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}
        mock_github._run_gh.side_effect = RuntimeError("gh unreachable")

        async def factory():
            return None

        orig = pm._HANDLE_MERGE_RETRY_BASE_DELAY
        pm._HANDLE_MERGE_RETRY_BASE_DELAY = 0
        try:
            outcome = await pr_watch_task(
                repo="owner/repo", issue_number=77,
                pr_url="https://github.com/owner/repo/pull/42",
                pr_number=42, session_id="sess-1",
                github=mock_github, transport_factory=factory,
                poll_interval=0, timeout=5, state_db=db,
            )
        finally:
            pm._HANDLE_MERGE_RETRY_BASE_DELAY = orig
        assert outcome["merged"] is True
        assert outcome["failed"] is not None
        rows = db.list_pr_watches()
        assert len(rows) == 1
        assert rows[0]["pr_number"] == 42
        db.close()

    @pytest.mark.asyncio
    async def test_phase_stamped_after_each_cleanup_step(
        self, tmp_path: Path
    ) -> None:
        """Each post-merge side effect must stamp a durable phase AFTER
        the effect succeeds, so a rehydrated watcher knows exactly
        what's left to do. Without this the codex P2 replay bug
        returns: crash between comment and close → restart posts a
        duplicate comment."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db = StateDB(tmp_path / "state.db")

        sent_messages: list[str] = []

        class FakeTransport:
            async def send(self, msg):
                sent_messages.append(msg)

            async def close(self):
                pass

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        async def factory():
            return FakeTransport()

        outcome = await pr_watch_task(
            repo="owner/repo", issue_number=77,
            pr_url="https://github.com/owner/repo/pull/42",
            pr_number=42, session_id="sess-1",
            github=mock_github, transport_factory=factory,
            poll_interval=0, timeout=5, state_db=db,
        )
        assert outcome["merged"] is True
        # Row was removed on clean completion — can't read phase from
        # a deleted row, but we can assert each side effect ran once:
        assert mock_github.comment_on_issue.call_count == 1
        assert mock_github._run_gh.call_count == 1  # the issue close
        assert len(sent_messages) == 1
        assert db.list_pr_watches() == []
        db.close()

    @pytest.mark.asyncio
    async def test_rehydrate_from_commented_phase_skips_comment(
        self, tmp_path: Path
    ) -> None:
        """Codex P2 fix: if the prior run posted the close comment but
        crashed before closing the issue, the rehydrated watcher must
        NOT post a duplicate comment. It should pick up from the close
        step."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db = StateDB(tmp_path / "state.db")
        # Simulate state left by a crashed prior run: row exists with
        # phase='commented' (comment was posted, then process died).
        db.add_pr_watch(
            repo="owner/repo", pr_number=42, issue_number=77,
            session_id="prev-sess",
            pr_url="https://github.com/owner/repo/pull/42",
        )
        db.set_pr_watch_cleanup_phase("owner/repo", 42, "commented")

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        async def factory():
            return None  # no transport configured

        outcome = await pr_watch_task(
            repo="owner/repo", issue_number=77,
            pr_url="https://github.com/owner/repo/pull/42",
            pr_number=42, session_id="sess-new",
            github=mock_github, transport_factory=factory,
            poll_interval=0, timeout=5, state_db=db,
        )
        assert outcome["merged"] is True
        # Comment was already posted by the prior run — must NOT be
        # re-posted.
        assert mock_github.comment_on_issue.call_count == 0
        # Issue close SHOULD run (that's the step that failed before).
        assert mock_github._run_gh.call_count == 1
        assert db.list_pr_watches() == []
        db.close()

    @pytest.mark.asyncio
    async def test_rehydrate_from_closed_phase_skips_comment_and_close(
        self, tmp_path: Path
    ) -> None:
        """If the prior run closed the issue but died before
        notification, the rehydrated watcher must re-send only the
        notification. No duplicate comment, no re-close on an
        already-closed issue."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db = StateDB(tmp_path / "state.db")
        db.add_pr_watch(
            repo="owner/repo", pr_number=42, issue_number=77,
            session_id="prev-sess",
            pr_url="https://github.com/owner/repo/pull/42",
        )
        db.set_pr_watch_cleanup_phase("owner/repo", 42, "closed")

        sent_messages: list[str] = []

        class FakeTransport:
            async def send(self, msg):
                sent_messages.append(msg)

            async def close(self):
                pass

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        async def factory():
            return FakeTransport()

        outcome = await pr_watch_task(
            repo="owner/repo", issue_number=77,
            pr_url="https://github.com/owner/repo/pull/42",
            pr_number=42, session_id="sess-new",
            github=mock_github, transport_factory=factory,
            poll_interval=0, timeout=5, state_db=db,
        )
        assert outcome["merged"] is True
        assert mock_github.comment_on_issue.call_count == 0
        assert mock_github._run_gh.call_count == 0
        assert len(sent_messages) == 1
        assert db.list_pr_watches() == []
        db.close()

    @pytest.mark.asyncio
    async def test_rehydrate_from_notified_phase_skips_all_steps(
        self, tmp_path: Path
    ) -> None:
        """Narrow terminal window: prior run completed every step but
        crashed before remove_pr_watch. Rehydrate must recognize the
        work is done and just drop the row — no duplicate anything."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db = StateDB(tmp_path / "state.db")
        db.add_pr_watch(
            repo="owner/repo", pr_number=42, issue_number=77,
            session_id="prev-sess",
            pr_url="https://github.com/owner/repo/pull/42",
        )
        db.set_pr_watch_cleanup_phase("owner/repo", 42, "notified")

        sent_messages: list[str] = []

        class FakeTransport:
            async def send(self, msg):
                sent_messages.append(msg)

            async def close(self):
                pass

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        async def factory():
            return FakeTransport()

        outcome = await pr_watch_task(
            repo="owner/repo", issue_number=77,
            pr_url="https://github.com/owner/repo/pull/42",
            pr_number=42, session_id="sess-new",
            github=mock_github, transport_factory=factory,
            poll_interval=0, timeout=5, state_db=db,
        )
        assert outcome["merged"] is True
        assert mock_github.comment_on_issue.call_count == 0
        assert mock_github._run_gh.call_count == 0
        assert sent_messages == []
        assert db.list_pr_watches() == []
        db.close()

    @pytest.mark.asyncio
    async def test_rehydrate_with_phase_skips_wait_for_merge(
        self, tmp_path: Path
    ) -> None:
        """Codex P1 round-4: when cleanup_phase is persisted, the prior
        process already observed the merge. Re-running wait_for_merge
        on rehydrate is unsafe — a transient GH error or an elapsed
        deadline could log watch_timeout and delete the row, losing
        the remaining cleanup work. The watcher must trust the phase
        and go straight to _handle_merge_with_retry.

        We verify by mocking get_pr_state to return OPEN (as if GH
        briefly lied during rehydrate) — cleanup must still complete."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db = StateDB(tmp_path / "state.db")
        db.add_pr_watch(
            repo="owner/repo", pr_number=42, issue_number=77,
            session_id="prev-sess",
            pr_url="https://github.com/owner/repo/pull/42",
        )
        db.set_pr_watch_cleanup_phase("owner/repo", 42, "closed")

        sent_messages: list[str] = []

        class FakeTransport:
            async def send(self, msg):
                sent_messages.append(msg)

            async def close(self):
                pass

        mock_github = AsyncMock()
        # GH says the PR is still OPEN — would normally fool
        # wait_for_merge into timing out. The phase shortcut must
        # trust the persisted state instead.
        mock_github.get_pr_state.return_value = {"state": "OPEN"}

        async def factory():
            return FakeTransport()

        outcome = await pr_watch_task(
            repo="owner/repo", issue_number=77,
            pr_url="https://github.com/owner/repo/pull/42",
            pr_number=42, session_id="sess-new",
            github=mock_github, transport_factory=factory,
            poll_interval=0, timeout=60, state_db=db,
        )
        assert outcome["merged"] is True
        # wait_for_merge MUST NOT have been called — trust the phase.
        assert mock_github.get_pr_state.call_count == 0
        # Cleanup picked up from closed → only notification sent.
        assert mock_github.comment_on_issue.call_count == 0
        assert mock_github._run_gh.call_count == 0
        assert len(sent_messages) == 1
        assert db.list_pr_watches() == []
        db.close()

    @pytest.mark.asyncio
    async def test_expired_row_with_phase_still_completes_cleanup(
        self, tmp_path: Path
    ) -> None:
        """Codex P2 round-3: rehydration must not discard a row just
        because its 7-day watch window elapsed. If the prior run was
        mid-cleanup (phase populated) or the PR merged just before the
        deadline, spawning a short-window watcher finishes the job.
        Dropping the row by age alone silently loses the post-merge
        automation — exactly what persistence was supposed to prevent."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db = StateDB(tmp_path / "state.db")
        # Seed an "expired" row (started_at 8 days ago) with cleanup
        # mid-flight — prior run posted the comment + closed the issue
        # but died before notifying.
        db.add_pr_watch(
            repo="owner/repo", pr_number=42, issue_number=77,
            session_id="prev-sess",
            pr_url="https://github.com/owner/repo/pull/42",
            started_at=1,  # deep in the past
        )
        db.set_pr_watch_cleanup_phase("owner/repo", 42, "closed")

        sent_messages: list[str] = []

        class FakeTransport:
            async def send(self, msg):
                sent_messages.append(msg)

            async def close(self):
                pass

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        async def factory():
            return FakeTransport()

        # Simulate cli.py rehydration passing the minimum grace timeout
        # even though DEFAULT_PR_WATCH_TIMEOUT - elapsed is negative.
        outcome = await pr_watch_task(
            repo="owner/repo", issue_number=77,
            pr_url="https://github.com/owner/repo/pull/42",
            pr_number=42, session_id="sess-new",
            github=mock_github, transport_factory=factory,
            poll_interval=0, timeout=60, state_db=db,
        )
        assert outcome["merged"] is True
        assert mock_github.comment_on_issue.call_count == 0
        assert mock_github._run_gh.call_count == 0
        assert len(sent_messages) == 1
        assert db.list_pr_watches() == []
        db.close()

    @pytest.mark.asyncio
    async def test_state_db_optional_no_op_when_absent(self) -> None:
        """Legacy callers can still pass no state_db; the watcher must
        work in-memory-only just like before."""
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        mock_github = AsyncMock()
        mock_github.get_pr_state.return_value = {"state": "MERGED"}

        async def factory():
            return None

        outcome = await pr_watch_task(
            repo="owner/repo", issue_number=77,
            pr_url="", pr_number=42, session_id=None,
            github=mock_github, transport_factory=factory,
            poll_interval=0, timeout=5,
            state_db=None,
        )
        assert outcome["merged"] is True


class TestPollerRestartRehydration:
    """Integration acceptance: start poller, spawn watcher, kill poller,
    start poller again, verify the watcher is respawned and the merge
    still fires handle_merge.

    We simulate the restart at the pr_watch_task level: StateDB is the
    durability boundary and `cli.py` just reads list_pr_watches() and
    spawns a task per row. Exercising the full launchd-path would
    require subprocessing the CLI; the list_pr_watches → pr_watch_task
    loop is the actual rehydration surface that must hold."""

    @pytest.mark.asyncio
    async def test_kill_then_restart_still_fires_handle_merge(
        self, tmp_path: Path, caplog
    ) -> None:
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.post_merge import pr_watch_task

        db_path = tmp_path / "state.db"

        # --- Poller "run 1": spawn the watcher, then "crash" mid-watch. ---
        db1 = StateDB(db_path)

        sleep_event = asyncio.Event()
        # Run 1: PR is still OPEN when the crash happens.
        async def run1_get_state(*_a, **_kw):
            await sleep_event.wait()
            return {"state": "OPEN"}

        mock_github_run1 = AsyncMock()
        mock_github_run1.get_pr_state.side_effect = run1_get_state

        async def factory_run1():
            return None

        async def runner1():
            return await pr_watch_task(
                repo="owner/repo", issue_number=77,
                pr_url="https://github.com/owner/repo/pull/42",
                pr_number=42, session_id="sess-1",
                github=mock_github_run1,
                transport_factory=factory_run1,
                poll_interval=0, timeout=60,
                state_db=db1,
            )

        task1 = asyncio.create_task(runner1())
        # Yield so the row is persisted and the watcher enters its loop.
        await asyncio.sleep(0)
        assert len(db1.list_pr_watches()) == 1

        # Simulate a poller shutdown: cancel the asyncio task + close
        # the connection (launchd kickstart / crash / redeploy).
        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1
        db1.close()

        # Row SURVIVED the restart — this is the durability claim.
        db2 = StateDB(db_path)
        surviving = db2.list_pr_watches()
        assert len(surviving) == 1
        row = surviving[0]
        assert row["repo"] == "owner/repo"
        assert row["pr_number"] == 42
        assert row["issue_number"] == 77
        assert row["session_id"] == "sess-1"
        assert row["pr_url"] == "https://github.com/owner/repo/pull/42"

        # --- Poller "run 2": rehydrate watcher, PR is now merged, handle_merge fires. ---
        mock_github_run2 = AsyncMock()
        mock_github_run2.get_pr_state.return_value = {"state": "MERGED"}

        sent_messages: list[str] = []

        class FakeTransport:
            async def send(self, msg):
                sent_messages.append(msg)

            async def close(self):
                pass

        async def factory_run2():
            return FakeTransport()

        with caplog.at_level(
            logging.INFO, logger="ctrlrelay.pipelines.post_merge"
        ):
            # This mirrors what cli.py does on startup: one
            # asyncio.create_task per row, same kwargs as the original
            # spawn plus the preserved state_db.
            outcome = await pr_watch_task(
                repo=row["repo"],
                issue_number=row["issue_number"],
                pr_url=row["pr_url"] or "",
                pr_number=row["pr_number"],
                session_id=row.get("session_id"),
                github=mock_github_run2,
                transport_factory=factory_run2,
                poll_interval=0,
                timeout=5,
                state_db=db2,
            )

        # The rehydrated watcher MUST detect the merge and close the issue.
        assert outcome["merged"] is True
        assert outcome["failed"] is None
        mock_github_run2.comment_on_issue.assert_called_once_with(
            "owner/repo", 77, "Closed by PR #42",
        )
        # `gh issue close` ran via _run_gh.
        close_calls = [
            c for c in mock_github_run2._run_gh.call_args_list
            if c.args[:2] == ("issue", "close")
        ]
        assert len(close_calls) == 1
        # Telegram notification fired.
        assert any(
            "closed after PR #42 merged" in m for m in sent_messages
        )
        # Terminal outcome → row was cleaned up on run 2.
        assert db2.list_pr_watches() == []
        db2.close()
