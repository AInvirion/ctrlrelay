"""Tests for ArchivedRepoTracker (issue #163).

The tracker gates every repo the daemon works on, so the tests below
lean hard on the fail-open rule: only a definite ``isArchived: true``
may skip a repo, and no negative derived from an error is ever cached.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.core.archived import ArchivedRepoTracker
from ctrlrelay.core.github import GitHubError

LOGGER = "ctrlrelay.core.archived"


class TestArchivedRepoTracker:
    @pytest.mark.asyncio
    async def test_confirmed_archived_is_cached_and_logged_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A confirmed archived repo is remembered for the tracker's
        lifetime: one `gh` call, one `poll.repo.archived` log, even
        across many checks."""
        github = MagicMock()
        github.repo_is_archived = AsyncMock(return_value=True)
        tracker = ArchivedRepoTracker(github=github)

        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert await tracker.is_archived("owner/old") is True
            assert await tracker.is_archived("owner/old") is True
            assert await tracker.is_archived("owner/old") is True

        assert github.repo_is_archived.await_count == 1
        archived_logs = [
            r for r in caplog.records
            if r.getMessage() == "poll.repo.archived"
        ]
        assert len(archived_logs) == 1
        assert archived_logs[0].repo == "owner/old"

    @pytest.mark.asyncio
    async def test_active_repo_is_rechecked_and_not_cached(self) -> None:
        """A confirmed ``isArchived: false`` must not be cached — a repo
        archived while the daemon runs still gets detected on a later
        cycle."""
        github = MagicMock()
        github.repo_is_archived = AsyncMock(side_effect=[False, False, True])
        tracker = ArchivedRepoTracker(github=github)

        assert await tracker.is_archived("owner/live") is False
        assert await tracker.is_archived("owner/live") is False
        assert await tracker.is_archived("owner/live") is True
        assert github.repo_is_archived.await_count == 3

    @pytest.mark.asyncio
    async def test_lookup_failure_treats_repo_as_active(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed lookup must NOT skip the repo and must NOT be cached:
        one flaky call would otherwise disable a repo for the whole
        daemon lifetime."""
        github = MagicMock()
        github.repo_is_archived = AsyncMock(
            side_effect=GitHubError("gh failed: connection reset")
        )
        tracker = ArchivedRepoTracker(github=github)

        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert await tracker.is_archived("owner/flaky") is False
            assert await tracker.is_archived("owner/flaky") is False

        # Re-probed every time — nothing negative was cached.
        assert github.repo_is_archived.await_count == 2
        assert not any(
            r.getMessage() == "poll.repo.archived" for r in caplog.records
        )
        failures = [
            r for r in caplog.records
            if r.getMessage() == "poll.repo.archived_check_failed"
        ]
        # Logged on the first failure, then deduped until a probe succeeds
        # so a persistently unreachable repo doesn't spam every cycle.
        assert len(failures) == 1
        assert failures[0].repo == "owner/flaky"

    @pytest.mark.asyncio
    async def test_failure_after_success_logs_again(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A successful probe clears the failure-dedup memory so a NEW
        outage is still visible to the operator."""
        github = MagicMock()
        github.repo_is_archived = AsyncMock(
            side_effect=[
                GitHubError("boom"),
                False,
                GitHubError("boom again"),
            ]
        )
        tracker = ArchivedRepoTracker(github=github)

        with caplog.at_level(logging.INFO, logger=LOGGER):
            await tracker.is_archived("owner/flaky")
            await tracker.is_archived("owner/flaky")
            await tracker.is_archived("owner/flaky")

        failures = [
            r for r in caplog.records
            if r.getMessage() == "poll.repo.archived_check_failed"
        ]
        assert len(failures) == 2

    @pytest.mark.asyncio
    async def test_non_boolean_result_treated_as_active(self) -> None:
        """A malformed response (missing field, string, None) is not a
        confirmation — the repo keeps being polled and nothing is
        cached."""
        github = MagicMock()
        github.repo_is_archived = AsyncMock(side_effect=[None, "true", 1])
        tracker = ArchivedRepoTracker(github=github)

        assert await tracker.is_archived("owner/weird") is False
        assert await tracker.is_archived("owner/weird") is False
        assert await tracker.is_archived("owner/weird") is False
        assert tracker.archived_repos == set()

    @pytest.mark.asyncio
    async def test_timeout_treats_repo_as_active(self) -> None:
        """A timeout is an unknown, not an archive."""
        github = MagicMock()
        github.repo_is_archived = AsyncMock(side_effect=TimeoutError())
        tracker = ArchivedRepoTracker(github=github)

        assert await tracker.is_archived("owner/slow") is False
        assert tracker.archived_repos == set()

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        """CancelledError must escape so daemon shutdown isn't swallowed
        into a fail-open 'not archived'."""
        github = MagicMock()
        github.repo_is_archived = AsyncMock(side_effect=asyncio.CancelledError())
        tracker = ArchivedRepoTracker(github=github)

        with pytest.raises(asyncio.CancelledError):
            await tracker.is_archived("owner/repo")

    @pytest.mark.asyncio
    async def test_fresh_tracker_recovers_unarchived_repo(self) -> None:
        """Restart is the invalidation boundary: a new tracker (new
        daemon) re-probes and picks the repo back up once unarchived."""
        github = MagicMock()
        github.repo_is_archived = AsyncMock(return_value=True)
        old = ArchivedRepoTracker(github=github)
        assert await old.is_archived("owner/repo") is True

        github.repo_is_archived = AsyncMock(return_value=False)
        fresh = ArchivedRepoTracker(github=github)
        assert await fresh.is_archived("owner/repo") is False
