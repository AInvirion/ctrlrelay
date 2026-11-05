"""#336: a run that correctly changes nothing is not a failed publish.

Drives the real `run_dev_issue`, with the publish step's own refusal
(`NothingToPushError`) standing for a branch with no new commits.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ctrlrelay.cli import announce_dev_success
from ctrlrelay.core.publish import NothingToPushError, PushRefusedError
from ctrlrelay.pipelines.base import PipelineResult
from tests.test_claim_reconciliation import _dev_issue_harness

REPO = "owner/repo"
NOTHING = NothingToPushError(
    "push refused: 'fix/issue-853' has no commits that 'main' does not "
    "already have; the agent must commit its work on 'fix/issue-853'"
)
NO_CHANGE = {"no_changes": True, "no_changes_reason": "already on main"}


async def _run(tmp_path: Path, *, outputs: dict, push):
    from ctrlrelay.core.pr_verifier import VerificationResult
    from ctrlrelay.core.state import StateDB
    from ctrlrelay.pipelines.dev import run_dev_issue

    worktree, github, dispatcher = _dev_issue_harness(
        tmp_path,
        checkpoint={"status": "DONE", "summary": "done", "outputs": outputs},
        exit_code=0,
    )
    github.create_pr = AsyncMock(
        return_value={"number": 9, "url": "https://github.com/o/r/pull/9"},
    )
    github.find_open_pr_for_branch = AsyncMock(return_value=None)
    state_db = StateDB(tmp_path / "state.db")
    verifier = AsyncMock()
    verifier.verify.return_value = VerificationResult(ready=True)
    with (
        patch.object(worktree, "ensure_bare_repo", new_callable=AsyncMock),
        patch.object(
            worktree, "create_worktree_with_new_branch", new_callable=AsyncMock,
        ) as create,
        patch.object(worktree, "remove_worktree", new_callable=AsyncMock),
        patch.object(
            worktree, "branch_exists_locally", new_callable=AsyncMock,
            return_value=False,
        ),
        patch.object(
            worktree, "branch_exists_on_remote", new_callable=AsyncMock,
            return_value=False,
        ),
        patch.object(worktree, "delete_branch", new_callable=AsyncMock),
        patch.object(
            worktree, "get_default_branch", new_callable=AsyncMock,
            return_value="main",
        ),
        patch.object(worktree, "symlink_context"),
        patch.object(worktree, "remove_context_symlink"),
        patch.object(
            worktree, "push_run_branch", new_callable=AsyncMock, side_effect=push,
        ) as pushed,
    ):
        wt = tmp_path / "worktrees" / "wt"
        wt.mkdir(parents=True)
        create.return_value = (wt, True)
        result = await run_dev_issue(
            claims=None, repo=REPO, issue_number=853,
            branch_template="fix/issue-{n}", dispatcher=dispatcher,
            github=github, worktree=worktree, dashboard=None,
            state_db=state_db, transport=None,
            contexts_dir=tmp_path / "contexts", pr_verifier=verifier,
            code_review=MagicMock(method="off"),
        )
    status = state_db.execute("SELECT status FROM sessions").fetchone()[0]
    state_db.close()
    bodies = [c.kwargs.get("body", "") for c in github.comment_on_issue.await_args_list]
    return result, status, bodies, pushed, github


@pytest.mark.asyncio
async def test_a_declared_no_change_with_nothing_to_push_ends_done(tmp_path):
    result, status, bodies, _, github = await _run(
        tmp_path, outputs=NO_CHANGE, push=NOTHING,
    )
    assert result.success
    assert status == "done"
    assert result.outputs["no_change"] is True
    assert "already on main" in result.summary
    github.create_pr.assert_not_awaited()
    [note] = [b for b in bodies if "ctrlrelay:no-change" in b]
    assert "already on main" in note


@pytest.mark.asyncio
async def test_no_declaration_keeps_the_forgotten_commit_refusal(tmp_path):
    result, status, bodies, _, _ = await _run(tmp_path, outputs={}, push=NOTHING)
    assert not result.success
    assert status == "failed"
    assert result.outputs["publish_refused"] is True
    assert "no commits" in (result.error or "")
    assert not [b for b in bodies if "ctrlrelay:no-change" in b]


@pytest.mark.asyncio
async def test_a_declaration_on_a_branch_with_commits_is_published(tmp_path):
    result, status, _, pushed, github = await _run(
        tmp_path, outputs={**NO_CHANGE, "pr_title": "t", "pr_body": "b"},
        push=lambda *a, **k: "c0ffee",
    )
    pushed.assert_awaited_once()
    assert result.outputs.get("no_change") is not True
    assert result.outputs.get("pr_number") == 9


@pytest.mark.asyncio
async def test_the_agent_cannot_set_the_relay_owned_marker(tmp_path):
    # "no_change" is the relay's word for a confirmed outcome; the agent
    # setting it directly, with any other refusal, changes nothing.
    result, status, _, _, _ = await _run(
        tmp_path, outputs={"no_change": True, "no_changes": True},
        push=PushRefusedError("push refused: wrong base"),
    )
    assert not result.success
    assert status == "failed"


@pytest.mark.parametrize("declared", ["true", 1, "yes"])
@pytest.mark.asyncio
async def test_only_a_literal_true_declares_no_change(tmp_path, declared):
    result, _, _, _, _ = await _run(
        tmp_path, outputs={"no_changes": declared}, push=NOTHING,
    )
    assert not result.success


@pytest.mark.asyncio
async def test_chat_says_no_change_rather_than_pr_ready():
    transport = AsyncMock()
    result = PipelineResult(
        success=True, session_id="s", summary="x",
        outputs={"no_change": True, "no_change_reason": "already on main"},
    )
    [line] = await announce_dev_success(transport, repo=REPO, result=result)
    assert "no change needed: already on main" in line
    assert "PR ready" not in line


def test_the_prompt_says_how_to_declare_no_change():
    from ctrlrelay.pipelines.dev import DevPipeline

    text = DevPipeline._checkpoint_contract(
        MagicMock(), "/tmp/s/state.json", "dev-1",
    )
    assert '"no_changes": True' in text
    assert "no_changes_reason" in text


@pytest.mark.asyncio
async def test_no_change_is_refused_once_this_run_has_pushed() -> None:
    """A fix round or a resumed session with a pushed head or an open PR:
    the declaration is not trusted, and the refusal stands."""
    from ctrlrelay.pipelines.dev import _Publisher

    for pr_number, pushed in ((42, "c0ffee"), (None, "c0ffee"), (42, None)):
        worktree = AsyncMock()
        worktree.push_run_branch.side_effect = NOTHING
        state_db = MagicMock()
        state_db.last_relay_push.return_value = pushed
        publisher = _Publisher(
            github=AsyncMock(), worktree=worktree, state_db=state_db,
            repo=REPO, branch="fix/issue-853", issue_number=853,
            issue_title="t", session_id="s", default_branch="main",
            pr_number=pr_number, last_pushed_sha=pushed,
        )
        result = await publisher.publish(PipelineResult(
            success=True, session_id="s", summary="done", outputs=NO_CHANGE,
        ))
        assert not result.success, (pr_number, pushed)
        assert result.outputs.get("no_change") is not True
        assert result.outputs["publish_refused"] is True
