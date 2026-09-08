"""Tests for the pre-handoff review.

This publishes findings. It does not certify that a review happened —
see the module docstring for the two attempts at that and why they
failed. So these tests check that findings reach the PR, that nothing
here can fail a green PR, and that no output claims verification.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ctrlrelay.core.code_review import (
    CodeReviewOutcome,
    ReviewStatus,
    find_inspection_evidence,
    format_review_comment,
    run_code_review,
)

GENUINE = (
    "OpenAI Codex\nworkdir: /w\n--------\nuser\nchanges against main\n"
    "exec\n/bin/bash -lc \"git diff --stat abc123\" in /w\n"
    " succeeded in 40ms:\n src/a.py | 2 +-\n"
    "codex\nBLOCKING: none.\nVERDICT: clean\n"
)

DID_NOTHING = (
    "OpenAI Codex\nworkdir: /w\n--------\nuser\nchanges against main\n"
    "codex\nNo findings. The change is correct and ready to merge.\n"
)


def _proc(output: bytes, returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(output, b""))
    return proc


class TestTheCommentClaimsNothingItCannotSupport:
    """Two attempts to certify "a reviewer read this diff" were defeated
    by the party under review. What is left is an opinion, and it must
    read as one."""

    def test_the_comment_says_it_is_unverified(self) -> None:
        body = format_review_comment(
            CodeReviewOutcome(status=ReviewStatus.REVIEWED, output=GENUINE)
        )

        assert "unverified" in body.lower()

    def test_no_trust_label_exists_to_be_applied(self) -> None:
        """Guards the decision, not an implementation detail: reviving a
        marker means reviving a promise this cannot keep."""
        import ctrlrelay.core.code_review as module
        import ctrlrelay.pipelines.dev as dev
        from ctrlrelay.core.github import GitHubCLI

        assert not hasattr(module, "REVIEW_DONE_LABEL")
        assert not hasattr(GitHubCLI, "add_label")
        assert not hasattr(dev, "_review_and_mark")

    def test_the_verdict_is_published_not_the_transcript(self) -> None:
        body = format_review_comment(
            CodeReviewOutcome(status=ReviewStatus.REVIEWED, output=GENUINE)
        )

        assert "VERDICT: clean" in body
        assert "git diff --stat" not in body

    def test_absolute_worktree_paths_are_stripped(self) -> None:
        wt = Path("/home/op/.ctrlrelay/worktrees/o-r-abc")
        body = format_review_comment(
            CodeReviewOutcome(
                status=ReviewStatus.REVIEWED,
                output=f"codex\n- [P2] bug at {wt}/src/a.py:12\n",
            ),
            worktree_path=wt,
        )

        assert str(wt) not in body
        assert "src/a.py:12" in body

    def test_the_duplicated_final_message_is_collapsed(self) -> None:
        line = "BLOCKING: none. The change is coherent and ready to merge."
        body = format_review_comment(
            CodeReviewOutcome(
                status=ReviewStatus.REVIEWED, output=f"codex\n{line}\n{line}\n"
            )
        )

        assert body.count("BLOCKING: none.") == 1


class TestARunThatDidNothingIsNotPublished:
    """Absence of any diff-reading command is forgeable as a positive but
    serviceable as a negative: a run that executed nothing has nothing to
    say, and posting its "No findings" would be actively misleading."""

    def test_a_real_run_shows_commands(self) -> None:
        assert find_inspection_evidence(GENUINE)

    def test_a_run_that_executed_nothing_shows_none(self) -> None:
        assert find_inspection_evidence(DID_NOTHING) == ()

    def test_writing_git_diff_in_the_verdict_does_not_count(self) -> None:
        assert find_inspection_evidence(
            "user\nreview\ncodex\nCONCERN: run `git diff` to see it.\n"
        ) == ()

    @pytest.mark.asyncio
    async def test_such_a_run_is_not_posted(self, tmp_path: Path) -> None:
        with patch(
            "ctrlrelay.core.code_review.asyncio.create_subprocess_exec",
            AsyncMock(return_value=_proc(DID_NOTHING.encode())),
        ):
            outcome = await run_code_review(
                repo="o/r", worktree_path=tmp_path, base_branch="main"
            )

        assert outcome.status is ReviewStatus.NO_EVIDENCE
        assert outcome.worth_posting is False


class TestNothingHereCanFailAGreenPR:
    """The PR's code and CI are already verified by the time this runs."""

    @pytest.mark.asyncio
    async def test_missing_reviewer_returns_rather_than_raises(
        self, tmp_path: Path
    ) -> None:
        with patch(
            "ctrlrelay.core.code_review.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError("no binary")),
        ):
            outcome = await run_code_review(
                repo="o/r", worktree_path=tmp_path, base_branch="main"
            )

        assert outcome.status is ReviewStatus.FAILED
        assert outcome.worth_posting is False

    @pytest.mark.asyncio
    async def test_an_unbalanced_cli_command_does_not_escape(
        self, tmp_path: Path
    ) -> None:
        outcome = await run_code_review(
            repo="o/r",
            worktree_path=tmp_path,
            base_branch="main",
            cli_command='codex review "',
        )

        assert outcome.status is ReviewStatus.FAILED

    @pytest.mark.asyncio
    async def test_timeout_kills_the_child(self, tmp_path: Path) -> None:
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

        assert outcome.worth_posting is False
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_stdin_is_closed(self, tmp_path: Path) -> None:
        """The CLI blocks until inherited stdin hits EOF; under a
        supervisor holding the pipe open every review burns its
        timeout."""
        import asyncio

        spawn = AsyncMock(return_value=_proc(GENUINE.encode()))
        with patch(
            "ctrlrelay.core.code_review.asyncio.create_subprocess_exec", spawn
        ):
            await run_code_review(
                repo="o/r", worktree_path=tmp_path, base_branch="main"
            )

        assert spawn.call_args.kwargs["stdin"] is asyncio.subprocess.DEVNULL

    @pytest.mark.asyncio
    async def test_a_failing_comment_call_does_not_raise(self) -> None:
        from ctrlrelay.pipelines.dev import _review_and_comment

        github = AsyncMock()
        github.comment_on_pr.side_effect = RuntimeError("gh down")

        with patch(
            "ctrlrelay.pipelines.dev.run_code_review",
            AsyncMock(
                return_value=CodeReviewOutcome(
                    status=ReviewStatus.REVIEWED, output=GENUINE
                )
            ),
        ):
            await _review_and_comment(
                github=github, repo="o/r", session_id="s", pr_number=7,
                worktree_path=Path("/tmp"), base_branch="main",
            )


class TestPipelineIntegration:
    @pytest.mark.asyncio
    async def test_findings_are_posted(self) -> None:
        from ctrlrelay.pipelines.dev import _review_and_comment

        github = AsyncMock()
        with patch(
            "ctrlrelay.pipelines.dev.run_code_review",
            AsyncMock(
                return_value=CodeReviewOutcome(
                    status=ReviewStatus.REVIEWED, output=GENUINE
                )
            ),
        ):
            await _review_and_comment(
                github=github, repo="o/r", session_id="s", pr_number=7,
                worktree_path=Path("/tmp"), base_branch="main",
            )

        github.comment_on_pr.assert_awaited_once()
        assert "unverified" in github.comment_on_pr.await_args.args[2].lower()

    @pytest.mark.asyncio
    async def test_a_run_that_did_nothing_posts_nothing(self) -> None:
        from ctrlrelay.pipelines.dev import _review_and_comment

        github = AsyncMock()
        with patch(
            "ctrlrelay.pipelines.dev.run_code_review",
            AsyncMock(
                return_value=CodeReviewOutcome(
                    status=ReviewStatus.NO_EVIDENCE, error="nothing ran"
                )
            ),
        ):
            await _review_and_comment(
                github=github, repo="o/r", session_id="s", pr_number=7,
                worktree_path=Path("/tmp"), base_branch="main",
            )

        github.comment_on_pr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_method_off_skips_entirely(self) -> None:
        from ctrlrelay.core.config import CodeReviewConfig
        from ctrlrelay.pipelines.dev import _review_and_comment

        github = AsyncMock()
        with patch("ctrlrelay.pipelines.dev.run_code_review") as run:
            await _review_and_comment(
                github=github, repo="o/r", session_id="s", pr_number=7,
                worktree_path=Path("/tmp"), base_branch="main",
                config=CodeReviewConfig(method="off"),
            )

        run.assert_not_called()
        github.comment_on_pr.assert_not_awaited()


class TestConfigDefaults:
    def test_the_reviewer_runs_read_only(self) -> None:
        """Bounds the damage — it cannot write or reach the network. It
        does not stop branch code executing."""
        from ctrlrelay.core.config import CodeReviewConfig

        assert "read-only" in CodeReviewConfig().cli_command

    def test_default_does_not_lead_with_the_mcp_path(self) -> None:
        from ctrlrelay.core.config import CodeReviewConfig

        assert CodeReviewConfig().method == "cli"

    @pytest.mark.parametrize("value", ["off", "none", "disabled", "false", "no"])
    def test_off_ish_spellings_disable(self, value: str) -> None:
        from ctrlrelay.core.config import CodeReviewConfig

        assert CodeReviewConfig(method=value).method == "off"

    def test_an_unknown_method_does_not_block_startup(self) -> None:
        from ctrlrelay.core.config import CodeReviewConfig

        assert CodeReviewConfig(method="bogus").method == "cli"
