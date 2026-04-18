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
    async def test_wait_for_checks_requires_two_empty_polls_to_conclude_no_ci(
        self,
    ) -> None:
        """GitHub registers check runs asynchronously after `gh pr create`, so
        a single empty read is ambiguous. Require a confirmation poll before
        concluding 'no CI configured', otherwise we'd skip CI entirely on a
        PR whose checks are about to register."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        # First read empty (checks not registered yet), second read has a
        # pending check, third is green.
        mock_github.get_pr_checks.side_effect = [
            [],
            [{"name": "ci", "state": "IN_PROGRESS", "bucket": "pending"}],
            [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}],
        ]

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        checks = await verifier.wait_for_checks("owner/repo", 42, timeout=60)

        assert mock_github.get_pr_checks.call_count == 3
        assert checks[0]["bucket"] == "pass"

    @pytest.mark.asyncio
    async def test_wait_for_checks_concludes_no_ci_after_confirmation(self) -> None:
        """Two consecutive empty reads = no CI, safe to exit."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = []

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        checks = await verifier.wait_for_checks("owner/repo", 42, timeout=60)

        assert checks == []
        assert mock_github.get_pr_checks.call_count == 2

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
    async def test_verify_not_ready_when_behind_base_branch(self) -> None:
        """mergeable=MERGEABLE + mergeStateStatus=BEHIND means branch protection
        requires up-to-date branches. Must not hand off as ready."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
        ]
        mock_github.get_pr_state.return_value = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BEHIND",
        }

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        result = await verifier.verify("owner/repo", 42)

        assert result.ready is False
        assert result.merge_state_status == "BEHIND"
        assert "behind" in result.reason.lower() or "rebase" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_verify_not_ready_when_unstable(self) -> None:
        """mergeable=MERGEABLE + mergeStateStatus=UNSTABLE (non-required failing
        check) must not be treated as ready — merge UI would reject it."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
        ]
        mock_github.get_pr_state.return_value = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "UNSTABLE",
        }

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        result = await verifier.verify("owner/repo", 42)

        assert result.ready is False
        assert result.merge_state_status == "UNSTABLE"

    @pytest.mark.asyncio
    async def test_verify_ready_when_blocked_awaiting_review(self) -> None:
        """On repos requiring review approval, mergeStateStatus=BLOCKED is the
        expected state after CI passes. The dev pipeline explicitly does NOT
        auto-merge — awaiting-review is the right terminal state to hand off."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
        ]
        mock_github.get_pr_state.return_value = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
        }

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        result = await verifier.verify("owner/repo", 42)

        assert result.ready is True
        assert result.merge_state_status == "BLOCKED"

    @pytest.mark.asyncio
    async def test_verify_timed_out_when_checks_still_pending(self) -> None:
        """If wait_for_checks returns with pending entries (timeout hit),
        verify must mark the result timed_out=True rather than folding those
        checks into failing_checks — Claude can't 'fix' slow CI."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        # Forever pending at a very short timeout.
        mock_github.get_pr_checks.return_value = [
            {"name": "long-ci", "state": "IN_PROGRESS", "bucket": "pending"},
        ]

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        result = await verifier.verify("owner/repo", 42, timeout=0)

        assert result.ready is False
        assert result.timed_out is True
        assert result.failing_checks == []
        assert len(result.pending_checks) == 1
        assert "pending" in result.reason.lower() or "timeout" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_verify_ready_when_has_hooks(self) -> None:
        """HAS_HOOKS is mergeable — the repo has pre-receive hooks but they
        don't block merge."""
        from dev_sync.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
        ]
        mock_github.get_pr_state.return_value = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "HAS_HOOKS",
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
