"""Tests for the pre-handoff code review gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ctrlrelay.core.code_review import (
    REVIEW_DONE_LABEL,
    CodeReviewOutcome,
    ReviewStatus,
    format_review_comment,
    looks_unreadable,
    run_code_review,
)


class TestAReviewThatReadNothingIsNotAPass:
    """The whole point of the gate. Review CLIs report "I could not read
    the diff" in prose and still exit 0, so an exit-code check alone
    certifies precisely the failure the marker exists to catch."""

    @pytest.mark.parametrize(
        "output",
        [
            "I could not access the repository contents to inspect HEAD",
            "all shell invocations fail with bwrap: loopback: Failed RTM_NEWADDR",
            "Review was interrupted. Please re-run and wait for it to complete.",
            "the patch should be considered unverified in this environment",
            "ERROR: You've hit your usage limit for the reviewer.",
            "The 'gpt-x' model is not supported when using this account.",
        ],
        ids=[
            "cannot-read", "sandbox-broken", "interrupted",
            "unverified", "rate-limited", "model-gone",
        ],
    )
    def test_unreadable_shapes_are_detected(self, output: str) -> None:
        assert looks_unreadable(output)

    def test_a_real_review_is_not_flagged(self) -> None:
        assert not looks_unreadable(
            "BLOCKING: none.\n\nCONCERN:\n- foo.py:12 off-by-one\n\nVERDICT: clean"
        )

    @pytest.mark.asyncio
    async def test_zero_exit_with_unreadable_output_is_not_reviewed(
        self, tmp_path: Path
    ) -> None:
        """The dangerous case: it looks like success."""
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(
            return_value=(b"I could not access the repository contents", b"")
        )

        with patch(
            "ctrlrelay.core.code_review.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            outcome = await run_code_review(
                repo="o/r", worktree_path=tmp_path, base_branch="main"
            )

        assert outcome.status is ReviewStatus.UNREADABLE
        assert outcome.may_mark_reviewed is False

    @pytest.mark.asyncio
    async def test_a_clean_review_is_marked(self, tmp_path: Path) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"VERDICT: clean", b""))

        with patch(
            "ctrlrelay.core.code_review.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            outcome = await run_code_review(
                repo="o/r", worktree_path=tmp_path, base_branch="main"
            )

        assert outcome.status is ReviewStatus.REVIEWED
        assert outcome.may_mark_reviewed is True

    @pytest.mark.asyncio
    async def test_a_missing_reviewer_is_not_marked(self, tmp_path: Path) -> None:
        with patch(
            "ctrlrelay.core.code_review.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError("no such binary")),
        ):
            outcome = await run_code_review(
                repo="o/r", worktree_path=tmp_path, base_branch="main"
            )

        assert outcome.status is ReviewStatus.FAILED
        assert outcome.may_mark_reviewed is False

    @pytest.mark.asyncio
    async def test_the_review_is_time_bounded(self, tmp_path: Path) -> None:
        """A wedged reviewer must not hold the dev session to its own
        timeout."""
        import asyncio

        proc = MagicMock()
        proc.returncode = None
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        with patch(
            "ctrlrelay.core.code_review.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            outcome = await run_code_review(
                repo="o/r",
                worktree_path=tmp_path,
                base_branch="main",
                timeout_seconds=1,
            )

        assert outcome.status is ReviewStatus.FAILED
        assert outcome.may_mark_reviewed is False
        proc.kill.assert_called_once()


class TestTheMarkerNamesNoTool:
    """Which backend reviewed is an implementation detail and must not
    reach a repository artifact."""

    def test_label_text_carries_no_vendor_identifier(self) -> None:
        lowered = REVIEW_DONE_LABEL.lower()
        for token in (
            "codex", "claude", "copilot", "cursor", "gpt", "openai",
            "anthropic", "ai", "llm", "model", "bot",
        ):
            assert token not in lowered.split(), REVIEW_DONE_LABEL
        assert "code_review" in lowered

    def test_comment_body_strips_backend_identity(self) -> None:
        body = format_review_comment(
            CodeReviewOutcome(
                status=ReviewStatus.REVIEWED,
                output="Codex reviewed this with GPT-5.6-Luna and found nothing.",
            )
        )

        lowered = body.lower()
        for token in ("codex", "gpt-5.6-luna", "gpt", "claude", "openai"):
            assert token not in lowered, body
        assert "found nothing" in lowered

    def test_an_empty_review_still_renders(self) -> None:
        body = format_review_comment(
            CodeReviewOutcome(status=ReviewStatus.REVIEWED, output="")
        )
        assert "No findings reported" in body


class TestPipelineMarksOnlyRealReviews:
    @pytest.mark.asyncio
    async def test_unreadable_review_leaves_the_pr_unmarked(self) -> None:
        from ctrlrelay.pipelines.dev import _review_and_mark

        github = AsyncMock()
        with patch(
            "ctrlrelay.pipelines.dev.run_code_review",
            AsyncMock(
                return_value=CodeReviewOutcome(
                    status=ReviewStatus.UNREADABLE, error="did not read"
                )
            ),
        ):
            await _review_and_mark(
                github=github, repo="o/r", session_id="s", pr_number=7,
                worktree_path=Path("/tmp"), base_branch="main",
            )

        github.add_label.assert_not_awaited()
        github.comment_on_pr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_real_review_comments_and_labels(self) -> None:
        from ctrlrelay.pipelines.dev import _review_and_mark

        github = AsyncMock()
        with patch(
            "ctrlrelay.pipelines.dev.run_code_review",
            AsyncMock(
                return_value=CodeReviewOutcome(
                    status=ReviewStatus.REVIEWED, output="VERDICT: clean"
                )
            ),
        ):
            await _review_and_mark(
                github=github, repo="o/r", session_id="s", pr_number=7,
                worktree_path=Path("/tmp"), base_branch="main",
            )

        github.comment_on_pr.assert_awaited_once()
        github.add_label.assert_awaited_once_with("o/r", 7, REVIEW_DONE_LABEL)

    @pytest.mark.asyncio
    async def test_method_off_skips_entirely(self) -> None:
        from ctrlrelay.core.config import CodeReviewConfig
        from ctrlrelay.pipelines.dev import _review_and_mark

        github = AsyncMock()
        with patch("ctrlrelay.pipelines.dev.run_code_review") as run:
            await _review_and_mark(
                github=github, repo="o/r", session_id="s", pr_number=7,
                worktree_path=Path("/tmp"), base_branch="main",
                config=CodeReviewConfig(method="off"),
            )

        run.assert_not_called()
        github.add_label.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_label_call_does_not_fail_the_pr(self) -> None:
        """Review is best-effort: a green PR must not fail because the
        marker could not be applied."""
        from ctrlrelay.pipelines.dev import _review_and_mark

        github = AsyncMock()
        github.add_label.side_effect = RuntimeError("gh down")

        with patch(
            "ctrlrelay.pipelines.dev.run_code_review",
            AsyncMock(
                return_value=CodeReviewOutcome(
                    status=ReviewStatus.REVIEWED, output="VERDICT: clean"
                )
            ),
        ):
            await _review_and_mark(
                github=github, repo="o/r", session_id="s", pr_number=7,
                worktree_path=Path("/tmp"), base_branch="main",
            )


class TestConfigDefaults:
    def test_default_does_not_lead_with_the_mcp_path(self) -> None:
        from ctrlrelay.core.config import CodeReviewConfig

        assert CodeReviewConfig().method == "cli"

    def test_legacy_mcp_then_cli_still_loads(self) -> None:
        """Existing configs must keep working — normalised to CLI."""
        from ctrlrelay.core.config import CodeReviewConfig

        assert CodeReviewConfig(method="mcp_then_cli").method == "cli"

    @pytest.mark.parametrize("value", ["off", "none", "disabled", "false", "no"])
    def test_off_ish_spellings_all_disable(self, value: str) -> None:
        from ctrlrelay.core.config import CodeReviewConfig

        assert CodeReviewConfig(method=value).method == "off"

    def test_an_unknown_method_does_not_block_startup(self) -> None:
        """This field was parsed and ignored for its whole existence, so
        configs in the wild may carry anything. Refusing to load would
        stop the daemon booting over a setting that never did anything —
        far worse than running the default."""
        from ctrlrelay.core.config import CodeReviewConfig

        assert CodeReviewConfig(method="bogus").method == "cli"
        assert CodeReviewConfig(method="").method == "cli"


class TestOnlyTheVerdictIsScanned:
    """The transcript contains the diff under review, so scanning all of
    it matches source code that merely *mentions* an unreadable-review
    phrase. That is not hypothetical: the first version of this detector
    read its own test fixtures back out of the reviewer's transcript and
    declared a perfectly good review unreadable, which would have meant
    no PR ever got marked."""

    def test_the_phrase_inside_reviewed_code_is_not_the_reviewer(self) -> None:
        transcript = (
            'diff --git a/tests/test_x.py b/tests/test_x.py\n'
            '+    "I could not access the repository contents",\n'
            '+    "bwrap: loopback: Failed RTM_NEWADDR",\n'
            "codex\n"
            "BLOCKING: none.\nVERDICT: clean\n"
        )

        assert looks_unreadable(transcript) is False

    def test_the_reviewer_saying_it_is_detected(self) -> None:
        transcript = (
            "diff --git a/a.py b/a.py\n+ ok\n"
            "codex\n"
            "I could not access the repository contents to inspect HEAD.\n"
        )

        assert looks_unreadable(transcript) is True

    def test_verdict_falls_back_to_the_tail_without_a_marker(self) -> None:
        from ctrlrelay.core.code_review import extract_verdict

        assert extract_verdict("a" * 9000).endswith("a")
        assert len(extract_verdict("a" * 9000)) == 4000

    def test_the_last_marker_wins(self) -> None:
        """A transcript can contain the word earlier; the conclusion is
        the final block."""
        from ctrlrelay.core.code_review import extract_verdict

        verdict = extract_verdict("codex\nfirst\ncodex\nVERDICT: clean\n")

        assert "VERDICT: clean" in verdict
        assert "first" not in verdict

    def test_the_comment_publishes_the_verdict_not_the_transcript(self) -> None:
        body = format_review_comment(
            CodeReviewOutcome(
                status=ReviewStatus.REVIEWED,
                output="diff --git a/a.py\n+ secret internals\ncodex\nVERDICT: clean\n",
            )
        )

        assert "VERDICT: clean" in body
        assert "secret internals" not in body


class TestSilenceIsNotAReview:
    """A misconfigured `cli_command` (`true`, or a wrapper that swallows
    its own output) exits 0 with nothing to say. Treating that as
    REVIEWED hands out the marker for work nobody did — the same hole as
    an unreadable run, only quieter."""

    @pytest.mark.asyncio
    async def test_empty_output_does_not_earn_the_marker(
        self, tmp_path: Path
    ) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch(
            "ctrlrelay.core.code_review.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            outcome = await run_code_review(
                repo="o/r", worktree_path=tmp_path, base_branch="main"
            )

        assert outcome.status is ReviewStatus.UNREADABLE
        assert outcome.may_mark_reviewed is False

    @pytest.mark.asyncio
    async def test_whitespace_only_verdict_does_not_count(
        self, tmp_path: Path
    ) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"transcript\ncodex\n   \n", b""))

        with patch(
            "ctrlrelay.core.code_review.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            outcome = await run_code_review(
                repo="o/r", worktree_path=tmp_path, base_branch="main"
            )

        assert outcome.may_mark_reviewed is False


class TestAReviewOfTheWrongTreeIsNotAPass:
    """Found by pointing the gate at its own PR. Reviewing code that
    itself invokes the reviewer caused a nested run in a scratch repo,
    whose verdict — "there are no code changes to review" — was about an
    empty temp directory. The gate called that REVIEWED.

    A false pass is worse than an unreadable one: it is confidently
    wrong, and it is exactly what the marker promises cannot happen. A
    branch under review always has a diff, so this claim can only mean
    the reviewer looked somewhere else."""

    @pytest.mark.parametrize(
        "verdict",
        [
            "The working tree is identical to the specified merge-base "
            "commit, so there are no code changes to review.",
            "There are no code changes to review.",
            "No changes to review in this branch.",
        ],
    )
    def test_no_changes_claims_are_rejected(self, verdict: str) -> None:
        assert looks_unreadable(f"transcript\ncodex\n{verdict}\n")

    @pytest.mark.asyncio
    async def test_such_a_review_earns_no_marker(self, tmp_path: Path) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(
            return_value=(
                b"transcript\ncodex\nThe working tree is identical to the "
                b"specified merge-base commit, so there are no code changes "
                b"to review.\n",
                b"",
            )
        )

        with patch(
            "ctrlrelay.core.code_review.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            outcome = await run_code_review(
                repo="o/r", worktree_path=tmp_path, base_branch="main"
            )

        assert outcome.status is ReviewStatus.UNREADABLE
        assert outcome.may_mark_reviewed is False
