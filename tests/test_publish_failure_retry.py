"""#268: a failed publish must not strand the issue, and the retraction
must not advise a retry route that does nothing.

`poll()` marks an issue seen BEFORE handing it to the handler, so a
publish failure that nobody un-marks drops the issue forever. The
retraction comment then told the operator to re-assign the issue or
re-apply the automation label — neither of which the skip decision
consults, because it is keyed on the issue number alone.

Two halves, tested separately:

* the comment, in `pipelines/dev.py`, which must say what actually
  happens next;
* the settle step, in `cli.py`, which un-marks a bounded number of
  times and says so on the issue when it stops.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ctrlrelay.cli import PUBLISH_ATTEMPTS, settle_publish_failure
from ctrlrelay.core.poller import IssuePoller
from ctrlrelay.core.publish import PushRefusedError
from ctrlrelay.pipelines.base import PipelineResult
from ctrlrelay.pipelines.dev import (
    AGENT_OUTCOME_MARKER,
    claim_failure_comment,
    publish_retries_exhausted_comment,
)
from tests.test_claim_reconciliation import (
    _dev_issue_harness,
    _retraction_bodies,
)

REPO = "owner/repo"

# The sentence the ticket is about. Both halves of it are useless: the
# skip decision reads no assignee and no label.
DEAD_ADVICE = ("Re-assign it", "re-apply the automation label")


def _poller(state_file: Path) -> IssuePoller:
    return IssuePoller(
        github=MagicMock(), username="tester", repos=[REPO],
        state_file=state_file,
    )


def _publish_failed(*, refused: bool = False, pr_number: int | None = None):
    outputs: dict[str, object] = {"publish_failed": True}
    if refused:
        outputs["publish_refused"] = True
    if pr_number is not None:
        outputs["pr_number"] = pr_number
    return PipelineResult(
        success=False, session_id="s", summary="not published",
        error="GitHubError: gh exploded", outputs=outputs,
    )


class TestTheRetractionNamesSomethingThatWorks:
    def test_the_dead_retry_advice_is_gone(self) -> None:
        """The default (no retry coming) must not send an operator to an
        action the skip decision never reads."""
        body = claim_failure_comment("s", reason="r", exit_code=1)
        for dead in DEAD_ADVICE:
            assert dead not in body
        # A person picking it up by hand is still true, and still said.
        assert "free to pick up" in body
        # And the route that does work is named.
        assert "seen_issues" in body
        assert "poller_state.json" in body

    def test_a_coming_retry_says_the_relay_will_do_it(self) -> None:
        body = claim_failure_comment(
            "s", reason="r", exit_code=1, will_retry=True,
        )
        assert "next poll" in body
        for dead in DEAD_ADVICE:
            assert dead not in body
        # Nothing for an operator to do, so the manual route is NOT
        # named — naming both would read as "do this as well".
        assert "poller_state.json" not in body

    def test_with_a_pr_a_coming_retry_does_not_say_nothing_is_waiting(
        self,
    ) -> None:
        """A fix round that failed to push still has the relay behind it.

        "it is not waiting on anything from this relay" is the opposite
        of true when the next poll will push to that very PR.
        """
        body = claim_failure_comment(
            "s", reason="r", exit_code=1, pr_number=42, will_retry=True,
        )
        assert "#42" in body
        assert "not waiting on anything" not in body
        assert "next poll" in body

    def test_with_a_pr_and_no_retry_the_old_wording_stands(self) -> None:
        """The negative control for the test above: without a retry the
        reader is still told to judge the PR on its own."""
        body = claim_failure_comment(
            "s", reason="r", exit_code=1, pr_number=42,
        )
        assert "on its own merits" in body

    def test_the_exhausted_comment_is_findable_and_actionable(self) -> None:
        body = publish_retries_exhausted_comment(
            "s", repo=REPO, issue_number=7, attempts=PUBLISH_ATTEMPTS,
            error="GitHubError: gh exploded",
        )
        assert AGENT_OUTCOME_MARKER in body
        assert str(PUBLISH_ATTEMPTS) in body
        assert "gh exploded" in body
        assert "seen_issues" in body
        for dead in DEAD_ADVICE:
            assert dead not in body


class TestSettlePublishFailure:
    @pytest.mark.asyncio
    async def test_the_issue_leaves_the_seen_set(self, tmp_path: Path) -> None:
        """The regression test the ticket asks for. Today's behaviour
        passes any test that only checks the claim was retracted."""
        poller = _poller(tmp_path / "state.json")
        github = AsyncMock()
        poller.mark_seen(REPO, 7)  # what poll() does before the handler

        message = await settle_publish_failure(
            github, poller, REPO, 7, _publish_failed(),
        )

        assert 7 not in poller.seen_issues[REPO]
        assert message is not None and "retry" in message.lower()
        # Nothing to announce on the issue yet — the pipeline's own
        # retraction already said a retry is coming.
        github.comment_on_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_gives_up_and_says_so_on_the_issue(
        self, tmp_path: Path,
    ) -> None:
        poller = _poller(tmp_path / "state.json")
        github = AsyncMock()
        messages = []
        for attempt in range(1, PUBLISH_ATTEMPTS + 1):
            poller.mark_seen(REPO, 7)
            messages.append(await settle_publish_failure(
                github, poller, REPO, 7, _publish_failed(),
            ))
            if attempt < PUBLISH_ATTEMPTS:
                assert 7 not in poller.seen_issues[REPO], attempt
                assert poller.publish_failures[REPO][7] == attempt

        assert 7 in poller.seen_issues[REPO]
        assert 7 not in poller.publish_failures.get(REPO, {})
        assert "not retrying" in messages[-1]
        [body] = [
            c.kwargs["body"]
            for c in github.comment_on_issue.await_args_list
        ]
        assert "seen_issues" in body

    @pytest.mark.asyncio
    async def test_a_refused_push_is_not_retried(self, tmp_path: Path) -> None:
        """A refusal is a decision, not a blip. Retrying re-runs a whole
        agent to reach the same refusal."""
        poller = _poller(tmp_path / "state.json")
        github = AsyncMock()
        poller.mark_seen(REPO, 7)

        message = await settle_publish_failure(
            github, poller, REPO, 7, _publish_failed(refused=True),
        )

        assert 7 in poller.seen_issues[REPO]
        assert message is not None and "refused" in message.lower()
        assert 7 not in poller.publish_failures.get(REPO, {})

    @pytest.mark.asyncio
    async def test_a_failure_elsewhere_is_left_alone(
        self, tmp_path: Path,
    ) -> None:
        """The present control. Without it, "always un-mark" passes
        every test above."""
        poller = _poller(tmp_path / "state.json")
        github = AsyncMock()
        poller.mark_seen(REPO, 7)
        other = PipelineResult(
            success=False, session_id="s", summary="agent exited 1",
            error="exit 1", outputs={},
        )

        assert await settle_publish_failure(
            github, poller, REPO, 7, other,
        ) is None
        assert 7 in poller.seen_issues[REPO]

    @pytest.mark.asyncio
    async def test_success_and_blocked_clear_the_count(
        self, tmp_path: Path,
    ) -> None:
        poller = _poller(tmp_path / "state.json")
        github = AsyncMock()
        poller.mark_seen(REPO, 7)
        await settle_publish_failure(github, poller, REPO, 7, _publish_failed())
        poller.mark_seen(REPO, 7)

        ok = PipelineResult(
            success=True, session_id="s", summary="done",
            outputs={"publish_failed": True},
        )
        assert await settle_publish_failure(github, poller, REPO, 7, ok) is None
        assert 7 in poller.seen_issues[REPO]
        assert 7 not in poller.publish_failures.get(REPO, {})

    @pytest.mark.asyncio
    async def test_the_count_survives_a_restart(self, tmp_path: Path) -> None:
        """A permanent cause must not get a fresh budget every restart."""
        state = tmp_path / "state.json"
        poller = _poller(state)
        github = AsyncMock()
        poller.mark_seen(REPO, 7)
        await settle_publish_failure(github, poller, REPO, 7, _publish_failed())

        on_disk = json.loads(state.read_text())
        assert on_disk["publish_failures"] == {REPO: {"7": 1}}
        assert _poller(state).publish_failures == {REPO: {7: 1}}

    @pytest.mark.asyncio
    async def test_a_failing_comment_does_not_raise(
        self, tmp_path: Path,
    ) -> None:
        """This runs after a failed run. A `gh` error here must not
        replace the run's own failure."""
        poller = _poller(tmp_path / "state.json")
        github = AsyncMock()
        github.comment_on_issue.side_effect = RuntimeError("gh exploded")
        for _ in range(PUBLISH_ATTEMPTS):
            poller.mark_seen(REPO, 7)
            message = await settle_publish_failure(
                github, poller, REPO, 7, _publish_failed(),
            )
        assert message is not None and "not retrying" in message
        github.comment_on_issue.assert_awaited_once()


class TestRunDevIssueFlagsThePublishFailure:
    """Drives the real `run_dev_issue`: the helpers being right was
    never the question — whether the publish path reaches them is."""

    async def _run(self, tmp_path: Path, *, push_raises):
        from ctrlrelay.core.pr_verifier import VerificationResult
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.pipelines.dev import run_dev_issue

        worktree, github, dispatcher = _dev_issue_harness(
            tmp_path,
            checkpoint={
                "status": "DONE", "summary": "done", "outputs": {},
            },
            exit_code=0,
        )
        state_db = StateDB(tmp_path / "state.db")
        verifier = AsyncMock()
        verifier.verify.return_value = VerificationResult(ready=True)

        with (
            patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock),
            patch.object(
                worktree, "create_worktree_with_new_branch",
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
            patch.object(
                worktree, "get_default_branch", new_callable=AsyncMock
            ) as default_branch,
            patch.object(worktree, "symlink_context"),
            patch.object(worktree, "remove_context_symlink"),
            patch.object(
                worktree, "push_run_branch", new_callable=AsyncMock,
                side_effect=push_raises,
            ),
        ):
            wt = tmp_path / "worktrees" / "wt"
            wt.mkdir(parents=True)
            create.return_value = (wt, True)
            exists_local.return_value = False
            exists_remote.return_value = False
            default_branch.return_value = "main"
            result = await run_dev_issue(
                claims=None,
                repo=REPO,
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
    async def test_a_transient_push_failure_promises_a_retry(
        self, tmp_path: Path,
    ) -> None:
        result, github = await self._run(
            tmp_path, push_raises=RuntimeError("remote hung up"),
        )
        assert not result.success
        assert result.outputs["publish_failed"] is True
        assert not result.outputs.get("publish_refused")
        [body] = _retraction_bodies(github)
        assert "next poll" in body
        for dead in DEAD_ADVICE:
            assert dead not in body

    @pytest.mark.asyncio
    async def test_a_refused_push_is_flagged_and_promises_nothing(
        self, tmp_path: Path,
    ) -> None:
        result, github = await self._run(
            tmp_path,
            push_raises=PushRefusedError("refusing to push to main"),
        )
        assert not result.success
        assert result.outputs["publish_refused"] is True
        [body] = _retraction_bodies(github)
        assert "next poll" not in body
        assert "poller_state.json" in body


class TestReviewFixes:
    def test_the_stop_notice_shows_the_error_as_inert_scrubbed_code(self) -> None:
        from ctrlrelay.pipelines.dev import publish_retries_exhausted_comment

        body = publish_retries_exhausted_comment(
            "s", repo=REPO, issue_number=7, attempts=3,
            error="push rejected: ``# Pwned`` @someone token=ghp_" + "c" * 36 + "\nsecond",
        )
        [line] = [ln for ln in body.splitlines() if ln.startswith("- Last error:")]
        assert line.startswith("- Last error: `") and line.endswith("`")
        inner = line[len("- Last error: `"):-1]
        assert "`" not in inner and "ghp_" not in inner and "second" not in body

    def test_the_stop_notice_does_not_claim_nothing_was_pushed(self) -> None:
        from ctrlrelay.pipelines.dev import publish_retries_exhausted_comment

        body = publish_retries_exhausted_comment(
            "s", repo=REPO, issue_number=7, attempts=3, error="gh pr create failed",
        )
        assert "nothing has been pushed" not in body
        assert "may already be there" in body

    @pytest.mark.asyncio
    async def test_a_failed_stop_notice_is_said_in_chat(self, tmp_path: Path) -> None:
        poller = _poller(tmp_path / "state.json")
        github = AsyncMock()
        github.comment_on_issue.side_effect = RuntimeError("gh down")
        message = None
        for _ in range(PUBLISH_ATTEMPTS):
            poller.mark_seen(REPO, 7)
            message = await settle_publish_failure(github, poller, REPO, 7, _publish_failed())
        assert "could not be posted on the issue" in message

    @pytest.mark.asyncio
    async def test_the_count_and_the_unmark_are_one_state_write(
        self, tmp_path: Path,
    ) -> None:
        """No stop can leave the count saved and the issue still seen, or
        the reverse."""
        poller = _poller(tmp_path / "state.json")
        poller.mark_seen(REPO, 7)
        saves: list[tuple[int, bool]] = []
        real_save = poller._save_state_best_effort

        def save() -> None:
            saves.append((
                poller.publish_failures.get(REPO, {}).get(7, 0),
                7 in poller.seen_issues.get(REPO, set()),
            ))
            real_save()

        poller._save_state_best_effort = save
        await settle_publish_failure(AsyncMock(), poller, REPO, 7, _publish_failed())
        assert saves == [(1, False)]


    @pytest.mark.parametrize("pr_number", [None, 12])
    def test_the_retraction_promises_only_what_a_running_poller_does(
        self, pr_number,
    ) -> None:
        from ctrlrelay.pipelines.dev import claim_failure_comment

        body = claim_failure_comment(
            "s", reason="publish failed", exit_code=0, pr_number=pr_number,
            will_retry=True,
        )
        assert "When the relay's poller is running" in body
        assert "says here if it stops" in body
        assert "Nothing is needed from you" not in body
