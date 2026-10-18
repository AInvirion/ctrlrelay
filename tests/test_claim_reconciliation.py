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
        #
        # "any claim above", not "the claim above": the flag that
        # triggers this is deliberately optimistic about a claim
        # existing, because a `gh` call can create the comment and lose
        # the response. The wording therefore must not assert that one
        # is visible.
        assert "supersedes any claim above" in body

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
        # It TRIED. Asserting only `wrote is False` passes for a
        # function that never calls GitHub at all, which is the
        # opposite defect wearing the shape of success.
        github.comment_on_issue.assert_awaited_once()

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
    async def test_a_failure_before_the_claim_is_posted_leaves_no_comment(
        self, tmp_path: Path
    ) -> None:
        """The `claim_posted = False` guard, exercised where it is read.

        `get_issue` raises, so the run enters the `try`, never reaches
        the claim block, and lands in the `except` — which is the one
        place that reads the flag. A bare "this failed" comment on an
        issue the relay never claimed is noise on somebody else's
        ticket.

        Found by mutation: the lock-contention test below returns
        BEFORE `claim_posted` is even assigned, so flipping its initial
        value to `True` changed nothing and the guard was unguarded.
        """
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        worktree, github, dispatcher = _dev_issue_harness(
            tmp_path, checkpoint=None, exit_code=1
        )
        github.get_issue.side_effect = RuntimeError("gh api 502")
        state_db = StateDB(tmp_path / "state.db")
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
        github.comment_on_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lock_contention_returns_without_touching_the_issue(
        self, tmp_path: Path
    ) -> None:
        """A peer holds the repo lock, so this run never starts.

        It returns before the `try` block, so it does not reach the
        claim OR the retraction. Kept as a separate claim from the
        test above, which is the one that covers the flag.
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


class TestTheExceptionPathNamesTheExitCodeItHas:
    """The exception path used to pass `exit_code=None` unconditionally,
    under a comment claiming no exit code was available.

    That is false whenever the worker already ran: an exception out of
    verification, review or cleanup arrives after `pipeline.run` has
    returned a result carrying the code, and the reconciliation comment
    then told a reader "not recorded" about a number it was holding.
    """

    @pytest.mark.asyncio
    async def test_a_late_exception_still_reports_the_workers_exit_code(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        worktree, github, dispatcher = _dev_issue_harness(
            tmp_path, checkpoint=None, exit_code=7
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
            patch.object(
                worktree, "remove_worktree", new_callable=AsyncMock
            ) as remove,
            patch.object(
                worktree, "branch_exists_on_remote", new_callable=AsyncMock
            ) as exists_remote,
            patch.object(worktree, "delete_branch", new_callable=AsyncMock),
            patch.object(worktree, "symlink_context"),
            patch.object(worktree, "remove_context_symlink"),
        ):
            wt = tmp_path / "worktrees" / "wt"
            wt.mkdir(parents=True)
            create.return_value = (wt, True)
            exists_local.return_value = False
            exists_remote.return_value = False
            # The worker ran and exited 7; cleanup is what blows up, so
            # the exception arrives AFTER the exit code is known.
            remove.side_effect = RuntimeError("worktree remove failed")
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
        assert "`7`" in bodies[0], (
            "the exception path reported no exit code although the worker "
            "had returned one"
        )
        assert "not recorded" not in bodies[0]


class TestAClaimWhoseResponseWasLostIsStillReconciled:
    @pytest.mark.asyncio
    async def test_a_comment_call_that_raises_still_reconciles(
        self, tmp_path: Path
    ) -> None:
        """`gh` can create the comment and then lose the response.

        The flag is therefore set BEFORE the await. The two mistakes
        are not symmetrical: a claim left standing with nothing working
        on the issue is the eighteen-hour defect this ticket exists
        for, while a reconciliation posted when the claim never landed
        still says only that nothing is working on the issue — true
        either way.
        """
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        worktree, github, dispatcher = _dev_issue_harness(
            tmp_path, checkpoint=None, exit_code=1
        )
        calls: list[str] = []

        async def flaky_comment(*, repo, issue_number, body):
            calls.append(body)
            if AGENT_CLAIM_MARKER in body:
                # Created on the server, lost on the wire.
                raise RuntimeError("timed out waiting for the response")

        github.comment_on_issue.side_effect = flaky_comment
        state_db = StateDB(tmp_path / "state.db")
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
        assert any(AGENT_OUTCOME_MARKER in b for b in calls), (
            "a claim whose response was lost left no reconciliation"
        )


class TestTheRealPipelineRecordsTheExitCode:
    """Every other persistence test inserts the `sessions` row by hand.

    That leaves two things unshown: whether the row exists at the
    moment the hook fires, and whether the pipeline's own end-of-run
    UPDATE overwrites what the hook wrote. Both are orderings inside
    `run_dev_issue`, so only driving it can answer them.
    """

    @pytest.mark.asyncio
    async def test_the_columns_are_readable_after_a_failed_run(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.cli import make_dispatch_outcome_hook
        from ctrlrelay.core.config import AlertsConfig
        from ctrlrelay.core.dispatcher import DispatchOutcome
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        state_db = StateDB(tmp_path / "state.db")
        config = MagicMock()
        config.alerts = AlertsConfig(
            enabled=True, consecutive_dispatch_failures=99
        )
        hook = make_dispatch_outcome_hook(config, state_db)

        worktree, github, dispatcher = _dev_issue_harness(
            tmp_path, checkpoint=None, exit_code=1
        )

        # The dispatcher calls the hook, as ClaudeDispatcher does — so
        # the hook fires at the real point in the pipeline's ordering.
        inner = dispatcher.spawn_session.side_effect
        secret = "ghp_" + "s" * 36

        async def spawn_and_report(**kwargs):
            result = await inner(**kwargs)
            await hook(
                DispatchOutcome(
                    session_id=kwargs["session_id"],
                    repo=kwargs.get("repo"),
                    issue_number=kwargs.get("issue_number"),
                    exit_code=1,
                    stdout="",
                    stderr=f"Invalid API key {secret} - please run /login",
                )
            )
            return result

        dispatcher.spawn_session.side_effect = spawn_and_report

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
            patch.object(worktree, "delete_branch", new_callable=AsyncMock),
            patch.object(worktree, "symlink_context"),
            patch.object(worktree, "remove_context_symlink"),
        ):
            wt = tmp_path / "worktrees" / "wt"
            wt.mkdir(parents=True)
            create.return_value = (wt, True)
            exists_local.return_value = False
            exists_remote.return_value = False
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

        row = state_db.get_session_row(result.session_id)
        health = state_db.get_dispatch_health()
        state_db.close()
        assert not result.success
        # The row existed when the hook fired.
        assert row is not None
        # And the pipeline's own end-of-run UPDATE (status, summary,
        # ended_at) did not clear what the hook wrote.
        assert row["status"] == "failed"
        assert row["claude_exit_code"] == 1, (
            "the exit code the hook recorded is not on the row after the "
            "pipeline finished"
        )
        assert "Invalid API key" in row["output_tail"]
        assert secret not in row["output_tail"]
        # The alert threshold is 99, so nothing was announced — this
        # test is about the record, not the notification.
        assert health["alert_delivered_at"] is None
        assert health["consecutive_failures"] == 1


class TestARetractionCancelsTheClaim:
    """`already_claimed` cannot be "any comment carries the marker".

    The claim comment is deliberately never deleted, so after a
    reconciliation the marker is still there — and the retraction's own
    closing line invites a retry. On that retry a marker-only test says
    the issue is already claimed, no fresh claim is posted, and the
    issue's LAST relay comment stays "nothing is working on this
    issue" while a worker runs, and after it succeeds too, because
    success is announced on chat and not on the issue.
    """

    @staticmethod
    def _claim() -> dict:
        from ctrlrelay.pipelines.dev import AGENT_CLAIM_COMMENT

        return {"author": {"login": "bot"}, "body": AGENT_CLAIM_COMMENT}

    @staticmethod
    def _retraction() -> dict:
        return {
            "author": {"login": "bot"},
            "body": claim_failure_comment("s", reason="r", exit_code=1),
        }

    def test_no_comments_is_not_a_claim(self) -> None:
        from ctrlrelay.pipelines.dev import claim_is_live

        assert claim_is_live([]) is False
        assert claim_is_live(None) is False

    def test_a_claim_alone_is_live(self) -> None:
        from ctrlrelay.pipelines.dev import claim_is_live

        assert claim_is_live([self._claim()]) is True

    def test_a_claim_followed_by_a_retraction_is_not_live(self) -> None:
        from ctrlrelay.pipelines.dev import claim_is_live

        assert claim_is_live([self._claim(), self._retraction()]) is False

    def test_a_fresh_claim_after_a_retraction_is_live_again(self) -> None:
        from ctrlrelay.pipelines.dev import claim_is_live

        assert (
            claim_is_live(
                [self._claim(), self._retraction(), self._claim()]
            )
            is True
        )

    def test_operator_comments_in_between_change_nothing(self) -> None:
        from ctrlrelay.pipelines.dev import claim_is_live

        chatter = {"author": {"login": "ovalenzuela"}, "body": "any news?"}
        assert (
            claim_is_live([self._claim(), chatter, self._retraction(), chatter])
            is False
        )

    @pytest.mark.asyncio
    async def test_a_retried_run_posts_a_fresh_claim(
        self, tmp_path: Path
    ) -> None:
        """Driven through `run_dev_issue`, because whether the retry
        posts a claim is a property of that function and not of the
        helper."""
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import AGENT_CLAIM_MARKER, run_dev_issue

        worktree, github, dispatcher = _dev_issue_harness(
            tmp_path, checkpoint=None, exit_code=1
        )
        # The issue as the previous failed run left it.
        github.get_issue.return_value = {
            "number": 853,
            "title": "t",
            "body": "b",
            "comments": [self._claim(), self._retraction()],
        }
        state_db = StateDB(tmp_path / "state.db")
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
            patch.object(worktree, "delete_branch", new_callable=AsyncMock),
            patch.object(worktree, "symlink_context"),
            patch.object(worktree, "remove_context_symlink"),
        ):
            wt = tmp_path / "worktrees" / "wt"
            wt.mkdir(parents=True)
            create.return_value = (wt, True)
            exists_local.return_value = False
            exists_remote.return_value = False
            await run_dev_issue(
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
        posted = [
            c.kwargs["body"] for c in github.comment_on_issue.await_args_list
        ]
        assert any(AGENT_CLAIM_MARKER in b for b in posted), (
            "a retry after a retraction posted no fresh claim, so the "
            "issue's last relay comment says nothing is working on it"
        )
        # And the claim comes before this run's own retraction.
        assert AGENT_CLAIM_MARKER in posted[0]
        assert AGENT_OUTCOME_MARKER in posted[-1]


class TestAResumedWorkersExitCodeIsTheOneReported:
    @pytest.mark.asyncio
    async def test_a_late_exception_names_the_resumed_workers_code(
        self, tmp_path: Path
    ) -> None:
        """`last_exit_code` is assigned once before the BLOCKED loop and
        again after each resume.

        Without the second assignment a late exception reported the
        FIRST worker's exit code for a session that had since been
        resumed — a number that was real and about a run that had been
        superseded.
        """
        import json as _json

        from ctrlrelay.core.checkpoint import read_checkpoint
        from ctrlrelay.core.dispatcher import ClaudeDispatcher, SessionResult
        from ctrlrelay.core.github import GitHubCLI
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.core.worktree import WorktreeManager
        from ctrlrelay.pipelines.dev import run_dev_issue

        worktree = WorktreeManager(
            worktrees_dir=tmp_path / "worktrees",
            bare_repos_dir=tmp_path / "repos",
        )
        bare = tmp_path / "repos" / "owner-repo.git"
        bare.mkdir(parents=True)
        (bare / "HEAD").write_text("ref: refs/heads/main\n")

        github = AsyncMock(spec=GitHubCLI)
        github.get_issue.return_value = {
            "number": 853, "title": "t", "body": "b", "comments": [],
        }

        # First spawn BLOCKS with exit 3; the resume exits 9.
        calls = {"n": 0}
        dispatcher = AsyncMock(spec=ClaudeDispatcher)

        async def spawn(**kwargs):
            calls["n"] += 1
            state_file = kwargs["state_file"]
            state_file.parent.mkdir(parents=True, exist_ok=True)
            if calls["n"] == 1:
                state_file.write_text(_json.dumps({
                    "version": "1",
                    "status": "BLOCKED_NEEDS_INPUT",
                    "session_id": kwargs["session_id"],
                    "timestamp": "2026-10-18T00:00:00Z",
                    "question": "which column?",
                }))
                return SessionResult(
                    session_id=kwargs["session_id"], exit_code=3,
                    stdout="", stderr="", state=read_checkpoint(state_file),
                )
            # The resumed worker: no checkpoint, exit 9.
            return SessionResult(
                session_id=kwargs["session_id"], exit_code=9,
                stdout="", stderr="", state=None,
            )

        dispatcher.spawn_session.side_effect = spawn

        transport = AsyncMock()
        transport.ask.return_value = "use the second column"

        state_db = StateDB(tmp_path / "state.db")
        with (
            patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock),
            patch.object(
                worktree,
                "create_worktree_with_new_branch",
                new_callable=AsyncMock,
            ) as create,
            patch.object(
                worktree, "remove_worktree", new_callable=AsyncMock
            ) as remove,
            patch.object(
                worktree, "branch_exists_locally", new_callable=AsyncMock
            ) as exists_local,
            patch.object(
                worktree, "branch_exists_on_remote", new_callable=AsyncMock
            ) as exists_remote,
            patch.object(worktree, "delete_branch", new_callable=AsyncMock),
            patch.object(worktree, "symlink_context"),
            patch.object(worktree, "remove_context_symlink"),
        ):
            wt = tmp_path / "worktrees" / "wt"
            wt.mkdir(parents=True)
            create.return_value = (wt, True)
            exists_local.return_value = False
            exists_remote.return_value = False
            # The late exception, after the resume has reported exit 9.
            remove.side_effect = RuntimeError("worktree remove failed")
            result = await run_dev_issue(
                repo="owner/repo",
                issue_number=853,
                branch_template="fix/issue-{n}",
                dispatcher=dispatcher,
                github=github,
                worktree=worktree,
                dashboard=None,
                state_db=state_db,
                transport=transport,
                contexts_dir=tmp_path / "contexts",
            )
        state_db.close()
        assert not result.success
        assert calls["n"] == 2, "the session was never resumed"
        bodies = _retraction_bodies(github)
        assert len(bodies) == 1
        assert "`9`" in bodies[0], (
            "the comment named the first worker's exit code for a session "
            "that had since been resumed"
        )
        assert "`3`" not in bodies[0]
