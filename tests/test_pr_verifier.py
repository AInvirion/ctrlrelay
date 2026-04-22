"""Tests for PR verification (CI checks + mergeability)."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest


class TestPRVerifier:
    @pytest.mark.asyncio
    async def test_wait_for_checks_returns_when_all_completed(self) -> None:
        """Should return immediately when every check has left the pending bucket."""
        from ctrlrelay.core.pr_verifier import PRVerifier

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
        from ctrlrelay.core.pr_verifier import PRVerifier

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
    async def test_wait_for_checks_honors_timeout_shorter_than_poll_interval(
        self,
    ) -> None:
        """Issue #90.1: timeout < poll_interval used to block the full
        poll_interval before noticing it was over budget. Now the sleep
        is capped at the remaining deadline so a 0.5s timeout with a
        15s interval returns within ~0.5s."""
        from ctrlrelay.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "IN_PROGRESS", "bucket": "pending"},
        ]

        verifier = PRVerifier(github=mock_github, poll_interval=15)
        start = time.monotonic()
        checks = await verifier.wait_for_checks(
            "owner/repo", 42, timeout=1
        )
        elapsed = time.monotonic() - start

        # Permissive upper bound — actual sleep should be ~1s, not 15s.
        # 5s is well under the broken 15s and well over the expected 1s.
        assert elapsed < 5, (
            f"wait_for_checks blocked {elapsed:.1f}s on a 1s timeout — "
            "sleep cap regression"
        )
        assert checks[0]["bucket"] == "pending"

    @pytest.mark.asyncio
    async def test_wait_for_checks_treats_gh_timeout_as_pending(self) -> None:
        """Issue #90.2: an asyncio.TimeoutError from the underlying gh
        subprocess used to escape as an unhandled exception out of
        wait_for_checks. Now it's logged and the loop retries — same
        treatment as a transient GitHubError."""
        from ctrlrelay.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        # First call: gh subprocess hangs and times out. Second call:
        # checks come back green. Without the fix, the first
        # TimeoutError would propagate and never reach the second call.
        mock_github.get_pr_checks.side_effect = [
            asyncio.TimeoutError("gh subprocess hung"),
            [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}],
        ]

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        checks = await verifier.wait_for_checks(
            "owner/repo", 42, timeout=10
        )

        assert mock_github.get_pr_checks.call_count == 2
        assert checks[0]["bucket"] == "pass"

    @pytest.mark.asyncio
    async def test_wait_for_checks_treats_gh_error_as_pending(self) -> None:
        """Same retry-on-transient behavior for GitHubError — covers the
        case where `gh` exits non-zero (rate limit back-off, brief auth
        glitch, network blip)."""
        from ctrlrelay.core.github import GitHubError
        from ctrlrelay.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.side_effect = [
            GitHubError("gh failed: HTTP 502"),
            [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}],
        ]

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        checks = await verifier.wait_for_checks(
            "owner/repo", 42, timeout=10
        )

        assert mock_github.get_pr_checks.call_count == 2
        assert checks[0]["bucket"] == "pass"

    @pytest.mark.asyncio
    async def test_wait_for_checks_returns_when_timeout_exceeded(self) -> None:
        """Should stop polling and return the last observed checks at timeout."""
        from ctrlrelay.core.pr_verifier import PRVerifier

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
        from ctrlrelay.core.pr_verifier import PRVerifier

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
        from ctrlrelay.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = []

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        checks = await verifier.wait_for_checks("owner/repo", 42, timeout=60)

        assert checks == []
        assert mock_github.get_pr_checks.call_count == 2

    @pytest.mark.asyncio
    async def test_verify_ready_when_all_checks_pass_and_mergeable(self) -> None:
        """Should report ready when CI is green and PR is mergeable."""
        from ctrlrelay.core.pr_verifier import PRVerifier

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
        from ctrlrelay.core.pr_verifier import PRVerifier

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
        from ctrlrelay.core.pr_verifier import PRVerifier

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
        from ctrlrelay.core.pr_verifier import PRVerifier

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
        from ctrlrelay.core.pr_verifier import PRVerifier

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
        from ctrlrelay.core.pr_verifier import PRVerifier

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
    async def test_verify_ready_when_unstable(self) -> None:
        """UNSTABLE (non-required failing check) is accepted as ready:
        required-check failures would have been caught in failing_checks
        above, and the dev pipeline never auto-merges — UNSTABLE is a
        human-review concern, not a Claude-fixable state."""
        from ctrlrelay.core.pr_verifier import PRVerifier

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

        assert result.ready is True
        assert result.merge_state_status == "UNSTABLE"

    @pytest.mark.asyncio
    async def test_verify_ready_when_blocked_awaiting_review(self) -> None:
        """On repos requiring review approval, mergeStateStatus=BLOCKED is the
        expected state after CI passes. The dev pipeline explicitly does NOT
        auto-merge — awaiting-review is the right terminal state to hand off."""
        from ctrlrelay.core.pr_verifier import PRVerifier

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
    async def test_verify_prioritizes_failing_over_pending_on_timeout(self) -> None:
        """Matrix build where one job already failed while another is still
        pending at timeout must be reported as failing (Claude can fix),
        not timed_out (hand off a known-bad PR)."""
        from ctrlrelay.core.pr_verifier import PRVerifier

        mock_github = AsyncMock()
        mock_github.get_pr_checks.return_value = [
            {"name": "lint", "state": "FAILURE", "bucket": "fail"},
            {"name": "long-ci", "state": "IN_PROGRESS", "bucket": "pending"},
        ]

        verifier = PRVerifier(github=mock_github, poll_interval=0)
        result = await verifier.verify("owner/repo", 42, timeout=0)

        assert result.ready is False
        assert result.timed_out is False
        assert len(result.failing_checks) == 1
        assert result.failing_checks[0]["name"] == "lint"

    @pytest.mark.asyncio
    async def test_verify_timed_out_when_checks_still_pending(self) -> None:
        """If wait_for_checks returns with pending entries (timeout hit),
        verify must mark the result timed_out=True rather than folding those
        checks into failing_checks — Claude can't 'fix' slow CI."""
        from ctrlrelay.core.pr_verifier import PRVerifier

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
        from ctrlrelay.core.pr_verifier import PRVerifier

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
        from ctrlrelay.core.pr_verifier import PRVerifier

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
