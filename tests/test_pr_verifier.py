"""Tests for PR verification (CI checks + mergeability)."""

from unittest.mock import AsyncMock

import pytest


class TestPRVerifier:
    @pytest.mark.asyncio
    async def test_wait_for_checks_returns_when_all_completed(self) -> None:
        """Should return immediately when every check has left the pending bucket."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
            {"name": "lint", "state": "SUCCESS", "bucket": "pass"},
        ]

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        checks = await verifier.wait_for_checks("owner/repo", 42, timeout=5)

        assert len(checks) == 2
        assert all(c["bucket"] == "pass" for c in checks)
        mock_github.get_pr_checks.assert_called_once_with("owner/repo", 42)

    @pytest.mark.asyncio
    async def test_wait_for_checks_polls_until_completed(self) -> None:
        """Should keep polling while any check is still pending."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.side_effect = [
            [{"name": "ci", "state": "IN_PROGRESS", "bucket": "pending"}],
            [{"name": "ci", "state": "IN_PROGRESS", "bucket": "pending"}],
            [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}],
        ]

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        checks = await verifier.wait_for_checks("owner/repo", 42, timeout=5)

        assert mock_github.get_pr_checks.call_count == 3
        assert checks[0]["bucket"] == "pass"

    @pytest.mark.asyncio
    async def test_wait_for_checks_returns_when_timeout_exceeded(self) -> None:
        """Should stop polling and return the last observed checks at timeout."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "IN_PROGRESS", "bucket": "pending"},
        ]

        verifier = PRVerifier(github=mock_github, poll_interval=1)
        checks = await verifier.wait_for_checks("owner/repo", 42, timeout=2)

        # Still pending at timeout.
        assert checks[0]["bucket"] == "pending"
        assert mock_github.get_pr_checks.call_count >= 2

    @pytest.mark.asyncio
    async def test_wait_for_checks_returns_immediately_when_no_checks(self) -> None:
        """Repos with no CI configured must not block for `check_timeout`."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = []

        # If the bug were present this would hang ~30 minutes.
        verifier = PRVerifier(github=mock_github, poll_interval=60)
        checks = await verifier.wait_for_checks("owner/repo", 42, timeout=1800)

        assert checks == []
        mock_github.get_pr_checks.assert_called_once()

    @pytest.mark.asyncio
    async def test_verify_ready_when_all_checks_pass_and_mergeable(self) -> None:
        """Should report ready when CI is green and PR is mergeable."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
        ]
        mock_github.get_pr_state.return_value = {
            "number": 42,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        result = await verifier.verify("owner/repo", 42)

        assert result.ready is True
        assert result.mergeable == "MERGEABLE"
        assert result.failing_checks == []

    @pytest.mark.asyncio
    async def test_verify_ready_when_no_checks_and_mergeable(self) -> None:
        """Repos with no CI should still verify ready if mergeable."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = []
        mock_github.get_pr_state.return_value = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        result = await verifier.verify("owner/repo", 42)

        assert result.ready is True
        assert result.failing_checks == []

    @pytest.mark.asyncio
    async def test_verify_not_ready_when_check_fails(self) -> None:
        """Should report not-ready and list failing checks."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
            {"name": "lint", "state": "FAILURE", "bucket": "fail"},
        ]
        mock_github.get_pr_state.return_value = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        result = await verifier.verify("owner/repo", 42)

        assert result.ready is False
        assert len(result.failing_checks) == 1
        assert result.failing_checks[0]["name"] == "lint"
        assert "check" in result.reason.lower() or "fail" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_verify_not_ready_when_conflicting(self) -> None:
        """Should report not-ready when PR has merge conflicts."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
        ]
        mock_github.get_pr_state.return_value = {
            "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY",
        }

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        result = await verifier.verify("owner/repo", 42)

        assert result.ready is False
        assert result.mergeable == "CONFLICTING"
        assert "conflict" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_verify_treats_skipping_as_pass(self) -> None:
        """bucket=skipping must not block — skipped checks don't fail a PR."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "a", "state": "SUCCESS", "bucket": "pass"},
            {"name": "b", "state": "SKIPPED", "bucket": "skipping"},
        ]
        mock_github.get_pr_state.return_value = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        result = await verifier.verify("owner/repo", 42)

        assert result.ready is True

    @pytest.mark.asyncio
    async def test_verify_waits_for_mergeable_when_unknown(self) -> None:
        """Should retry get_pr_state while mergeable is UNKNOWN."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
        ]
        mock_github.get_pr_state.side_effect = [
            {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"},
            {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"},
            {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"},
        ]

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        result = await verifier.verify("owner/repo", 42)

        assert result.ready is True
        assert mock_github.get_pr_state.call_count == 3
