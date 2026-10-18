"""Tests for reconciling the claim comment with a failed run (#204).

The defect: the dev lane posts *"CTRLRelay is working on this issue. A
PR will be opened for review."* two seconds before the worker starts,
and nothing ever returns to the issue when the worker exits non-zero.
On `AInvirion/zzsites#853` that sentence stood on the beta tester's own
ticket for eighteen hours.

The integration class at the bottom drives the real ``run_dev_issue``
rather than the helper, because the helper being correct was never the
question — whether the failure path reaches it is. Both directions are
asserted: a failing run comments, and a succeeding or blocked one does
not.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ctrlrelay.pipelines.dev import (
    AGENT_CLAIM_MARKER,
    AGENT_OUTCOME_MARKER,
    claim_failure_comment,
    format_issue_comments,
    reconcile_claim_on_failure,
)


class TestClaimFailureComment:
    def test_it_names_the_session_and_the_exit_code(self) -> None:
        body = claim_failure_comment(
            "dev-AInvirion-zzsites-853-9f3c2a11", reason="boom", exit_code=1
        )
        assert "dev-AInvirion-zzsites-853-9f3c2a11" in body
        assert "`1`" in body
        assert "boom" in body

    def test_it_carries_a_findable_marker(self) -> None:
        body = claim_failure_comment("s", reason="r", exit_code=1)
        assert AGENT_OUTCOME_MARKER in body

    def test_without_a_pr_it_says_the_issue_is_free(self) -> None:
        body = claim_failure_comment("s", reason="r", exit_code=1)
        assert "no pull request" in body
        assert "free to pick up" in body
        # The sentence that was false for eighteen hours is contradicted
        # explicitly, not merely followed by a different one.
        assert "out of date" in body

    def test_with_a_pr_it_does_not_claim_nothing_was_opened(self) -> None:
        """A run that opened a PR and then failed to finish verifying it
        is a different situation for the reader.

        Telling them "no pull request was opened" when #42 exists sends
        them looking for work that is sitting there — a warning with the
        wrong action in it.
        """
        body = claim_failure_comment("s", reason="r", exit_code=1, pr_number=42)
        assert "#42" in body
        assert "no pull request" not in body
        assert "free to pick up" not in body

    def test_an_unknown_exit_code_says_so_rather_than_guessing(self) -> None:
        """`None` must not print as 0.

        "exited 0 and wrote nothing" and "never started" have different
        causes and different fixes, and inventing a number destroys the
        distinction the field exists to record.
        """
        body = claim_failure_comment("s", reason="r", exit_code=None)
        assert "not recorded" in body
        assert "`0`" not in body


class TestReconcileClaimOnFailure:
    @pytest.mark.asyncio
    async def test_it_posts_the_comment(self) -> None:
        github = AsyncMock()
        wrote = await reconcile_claim_on_failure(
            github,
            repo="o/r",
            issue_number=7,
            session_id="s",
            reason="exit 1",
            exit_code=1,
        )
        assert wrote is True
        github.comment_on_issue.assert_awaited_once()
        kwargs = github.comment_on_issue.await_args.kwargs
        assert kwargs["repo"] == "o/r"
        assert kwargs["issue_number"] == 7
        assert AGENT_OUTCOME_MARKER in kwargs["body"]

    @pytest.mark.asyncio
    async def test_no_claim_means_no_comment(self) -> None:
        """Nothing to retract on an issue the relay never claimed.

        Paired with the test above, which proves a comment IS written
        when there is a claim — otherwise this would pass for a
        function that never comments at all.
        """
        github = AsyncMock()
        wrote = await reconcile_claim_on_failure(
            github,
            repo="o/r",
            issue_number=7,
            session_id="s",
            reason="r",
            exit_code=1,
            claim_posted=False,
        )
        assert wrote is False
        github.comment_on_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_gh_call_is_swallowed_not_raised(self) -> None:
        """This runs on a path that is already failing.

        An exception here would replace the pipeline's real failure with
        a different one, so the operator would be told the wrong cause.
        """
        github = AsyncMock()
        github.comment_on_issue.side_effect = RuntimeError("gh exploded")
        wrote = await reconcile_claim_on_failure(
            github,
            repo="o/r",
            issue_number=7,
            session_id="s",
            reason="r",
            exit_code=1,
        )
        assert wrote is False

    @pytest.mark.asyncio
    async def test_a_cancellation_still_propagates(self) -> None:
        """Shutdown is not an error to swallow — the caller's cleanup
        depends on it arriving."""
        import asyncio

        github = AsyncMock()
        github.comment_on_issue.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await reconcile_claim_on_failure(
                github,
                repo="o/r",
                issue_number=7,
                session_id="s",
                reason="r",
                exit_code=1,
            )


class TestOurOwnCommentsAreNotFedBackToTheAgent:
    def test_an_operator_comment_survives(self) -> None:
        """The present control. Without it, the two tests below would
        pass for a filter that drops everything."""
        rendered = format_issue_comments(
            [{"author": {"login": "ovalenzuela"}, "body": "do it this way"}]
        )
        assert "do it this way" in rendered

    def test_the_claim_comment_is_dropped(self) -> None:
        rendered = format_issue_comments(
            [{"author": {"login": "bot"}, "body": f"claimed {AGENT_CLAIM_MARKER}"}]
        )
        assert "claimed" not in rendered

    def test_the_retraction_is_dropped_too(self) -> None:
        """Left in, a retried agent reads "CTRLRelay did not finish this
        run" as operator guidance about the work it is being asked to
        do."""
        body = claim_failure_comment("s", reason="exit 1", exit_code=1)
        rendered = format_issue_comments(
            [{"author": {"login": "bot"}, "body": body}]
        )
        assert "did not finish" not in rendered
        assert rendered == "_No comments._"


def _dev_issue_harness(tmp_path: Path, *, checkpoint: dict | None, exit_code: int):
    """Mocks for `run_dev_issue` that let a chosen outcome through.

    ``checkpoint=None`` reproduces the incident exactly: the worker
    exits non-zero and writes no state file, which is the
    `dispatcher.checkpoint.missing` + `subprocess_failed` pair from
    `poller.log`.
    """
    from ctrlrelay.core.checkpoint import read_checkpoint
    from ctrlrelay.core.dispatcher import ClaudeDispatcher, SessionResult
    from ctrlrelay.core.github import GitHubCLI
    from ctrlrelay.core.worktree import WorktreeManager

    worktree = WorktreeManager(
        worktrees_dir=tmp_path / "worktrees",
        bare_repos_dir=tmp_path / "repos",
    )
    bare = tmp_path / "repos" / "owner-repo.git"
    bare.mkdir(parents=True)
    (bare / "HEAD").write_text("ref: refs/heads/main\n")

    github = AsyncMock(spec=GitHubCLI)
    github.get_issue.return_value = {
        "number": 853,
        "title": "Contact form loses the address",
        "body": "...",
        "comments": [],
    }

    dispatcher = AsyncMock(spec=ClaudeDispatcher)

    async def spawn(**kwargs):
        state = None
        if checkpoint is not None:
            state_file = kwargs["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({
                "version": "1",
                "session_id": kwargs["session_id"],
                "timestamp": "2026-10-17T20:24:04Z",
                **checkpoint,
            }))
            state = read_checkpoint(state_file)
        return SessionResult(
            session_id=kwargs["session_id"],
            exit_code=exit_code,
            stdout="",
            stderr="",
            state=state,
        )

    dispatcher.spawn_session.side_effect = spawn
    return worktree, github, dispatcher


def _retraction_bodies(github) -> list[str]:
    return [
        call.kwargs["body"]
        for call in github.comment_on_issue.await_args_list
        if AGENT_OUTCOME_MARKER in call.kwargs.get("body", "")
    ]


class TestRunDevIssueReconcilesTheClaim:
    """Drives the real `run_dev_issue`.

    The helper above being right was never the question — whether the
    failure path reaches it is, and that is the thing the ticket is
    about.
    """

    async def _run(self, tmp_path: Path, *, checkpoint, exit_code: int):
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        worktree, github, dispatcher = _dev_issue_harness(
            tmp_path, checkpoint=checkpoint, exit_code=exit_code
        )
        state_db = StateDB(tmp_path / "state.db")
        verifier = AsyncMock()
        from ctrlrelay.core.pr_verifier import VerificationResult

        verifier.verify.return_value = VerificationResult(ready=True)

        with (
            patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock),
            patch.object(
                worktree,
                "create_worktree_with_new_branch",
                new_callable=AsyncMock,
            ) as create,
            patch.object(worktree, "remove_worktree", new_callable=AsyncMock),
            patch.object(
                worktree, "branch_exists_locally", new_callable=AsyncMock
            ) as exists_local,
            patch.object(
                worktree, "branch_exists_on_remote", new_callable=AsyncMock
            ) as exists_remote,
            patch.object(
                worktree, "delete_branch", new_callable=AsyncMock
            ),
            patch.object(
                worktree, "get_default_branch", new_callable=AsyncMock
            ) as default_branch,
            patch.object(worktree, "symlink_context"),
            patch.object(worktree, "remove_context_symlink"),
        ):
            wt = tmp_path / "worktrees" / "wt"
            wt.mkdir(parents=True)
            create.return_value = (wt, True)
            exists_local.return_value = False
            exists_remote.return_value = False
            default_branch.return_value = "main"
            result = await run_dev_issue(
                repo="owner/repo",
                issue_number=853,
                branch_template="fix/issue-{n}",
                dispatcher=dispatcher,
                github=github,
                worktree=worktree,
                dashboard=None,
                state_db=state_db,
                transport=None,
                contexts_dir=tmp_path / "contexts",
                pr_verifier=verifier,
                code_review=MagicMock(method="off"),
            )
        state_db.close()
        return result, github

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_with_no_checkpoint_retracts_the_claim(
        self, tmp_path: Path
    ) -> None:
        """The incident, reproduced: exit 1, no state file written."""
        result, github = await self._run(
            tmp_path, checkpoint=None, exit_code=1
        )
        assert not result.success
        bodies = _retraction_bodies(github)
        assert len(bodies) == 1, (
            "a dispatch that ended non-zero must reconcile its own claim "
            "comment on the issue"
        )
        assert "`1`" in bodies[0]
        assert result.session_id in bodies[0]

    @pytest.mark.asyncio
    async def test_the_claim_comment_itself_is_still_posted_first(
        self, tmp_path: Path
    ) -> None:
        """Posting the claim up front is deliberate and stays — it is
        what stops two workers taking one issue. The bug was never that
        it is posted early."""
        _, github = await self._run(tmp_path, checkpoint=None, exit_code=1)
        bodies = [
            c.kwargs["body"] for c in github.comment_on_issue.await_args_list
        ]
        assert AGENT_CLAIM_MARKER in bodies[0]
        assert AGENT_OUTCOME_MARKER in bodies[-1]

    @pytest.mark.asyncio
    async def test_a_successful_run_does_not_retract(
        self, tmp_path: Path
    ) -> None:
        """The negative control. Without it, "always comment" passes the
        test above."""
        result, github = await self._run(
            tmp_path,
            checkpoint={
                "status": "DONE",
                "summary": "PR #42 opened",
                "outputs": {
                    "pr_number": 42,
                    "pr_url": "https://github.com/owner/repo/pull/42",
                },
            },
            exit_code=0,
        )
        assert result.success
        assert _retraction_bodies(github) == []

    @pytest.mark.asyncio
    async def test_a_blocked_run_does_not_retract(
        self, tmp_path: Path
    ) -> None:
        """BLOCKED is not a failure: the operator is being asked
        something and the claim is still true."""
        result, github = await self._run(
            tmp_path,
            checkpoint={
                "status": "BLOCKED_NEEDS_INPUT",
                "question": "which column?",
            },
            exit_code=0,
        )
        assert result.blocked
        assert _retraction_bodies(github) == []

    @pytest.mark.asyncio
    async def test_an_exception_mid_run_also_retracts(
        self, tmp_path: Path
    ) -> None:
        """Reached by more causes than a non-zero exit is.

        An exception out of the worktree or `gh` layer leaves the claim
        standing just as surely, and that branch is the one most of the
        pipeline's failures travel through.
        """
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        worktree, github, dispatcher = _dev_issue_harness(
            tmp_path, checkpoint=None, exit_code=1
        )
        state_db = StateDB(tmp_path / "state.db")
        with (
            patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock),
            patch.object(
                worktree, "branch_exists_locally", new_callable=AsyncMock
            ) as exists_local,
            patch.object(
                worktree,
                "create_worktree_with_new_branch",
                new_callable=AsyncMock,
            ) as create,
            patch.object(worktree, "remove_worktree", new_callable=AsyncMock),
            patch.object(
                worktree, "branch_exists_on_remote", new_callable=AsyncMock
            ) as exists_remote,
            patch.object(worktree, "delete_branch", new_callable=AsyncMock),
            patch.object(worktree, "remove_context_symlink"),
        ):
            exists_local.return_value = False
            exists_remote.return_value = False
            create.side_effect = RuntimeError("worktree add failed")
            result = await run_dev_issue(
                repo="owner/repo",
                issue_number=853,
                branch_template="fix/issue-{n}",
                dispatcher=dispatcher,
                github=github,
                worktree=worktree,
                dashboard=None,
                state_db=state_db,
                transport=None,
                contexts_dir=tmp_path / "contexts",
            )
        state_db.close()
        assert not result.success
        bodies = _retraction_bodies(github)
        assert len(bodies) == 1
        assert "worktree add failed" in bodies[0]
        # No exit code is knowable on this path, and it says so rather
        # than printing a zero.
        assert "not recorded" in bodies[0]

    @pytest.mark.asyncio
    async def test_a_failure_before_the_claim_leaves_no_comment(
        self, tmp_path: Path
    ) -> None:
        """Lock contention returns before the claim block runs.

        A bare "this failed" comment on an issue the relay never
        claimed is noise on somebody else's ticket.
        """
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        worktree, github, dispatcher = _dev_issue_harness(
            tmp_path, checkpoint=None, exit_code=1
        )
        state_db = StateDB(tmp_path / "state.db")
        # A peer already holds the repo lock.
        state_db.acquire_lock("owner/repo", "someone-elses-session")
        result = await run_dev_issue(
            repo="owner/repo",
            issue_number=853,
            branch_template="fix/issue-{n}",
            dispatcher=dispatcher,
            github=github,
            worktree=worktree,
            dashboard=None,
            state_db=state_db,
            transport=None,
            contexts_dir=tmp_path / "contexts",
        )
        state_db.close()
        assert not result.success
        assert "locked by another session" in (result.error or "")
        github.comment_on_issue.assert_not_awaited()
