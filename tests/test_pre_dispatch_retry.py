"""#234: a run that fails before the agent starts is retried, a bounded
number of times, instead of the issue being dropped as handled."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ctrlrelay.cli import (
    PRE_DISPATCH_ATTEMPTS,
    reclone_message,
    settle_pre_dispatch_failure,
)
from ctrlrelay.core.poller import IssuePoller
from ctrlrelay.pipelines.base import PipelineResult

REPO = "owner/repo"


def _poller(state_file: Path) -> IssuePoller:
    return IssuePoller(
        github=MagicMock(), username="tester", repos=[REPO],
        state_file=state_file,
    )


def _failed(before_dispatch: bool, error: str = "git failed: boom") -> PipelineResult:
    return PipelineResult(
        success=False, session_id="s", summary="x", error=error,
        outputs={"failed_before_dispatch": True} if before_dispatch else {},
    )


class TestSettle:
    def test_retries_then_gives_up(self, tmp_path: Path) -> None:
        poller = _poller(tmp_path / "state.json")
        messages = []
        for attempt in range(1, PRE_DISPATCH_ATTEMPTS + 1):
            poller.mark_seen(REPO, 7)  # what poll() does before the handler
            msg = settle_pre_dispatch_failure(poller, REPO, 7, _failed(True))
            messages.append(msg)
            if attempt < PRE_DISPATCH_ATTEMPTS:
                assert 7 not in poller.seen_issues[REPO], attempt
                assert poller.pre_dispatch_failures[REPO][7] == attempt
                assert f"try {attempt} of {PRE_DISPATCH_ATTEMPTS}" in msg
                assert "Will retry" in msg
        assert 7 in poller.seen_issues[REPO]
        assert 7 not in poller.pre_dispatch_failures.get(REPO, {})
        assert "not retrying" in messages[-1]
        assert "git failed: boom" in messages[-1]

    def test_a_run_that_reached_the_agent_clears_the_count(
        self, tmp_path: Path,
    ) -> None:
        poller = _poller(tmp_path / "state.json")
        poller.mark_seen(REPO, 7)
        settle_pre_dispatch_failure(poller, REPO, 7, _failed(True))
        poller.mark_seen(REPO, 7)

        assert settle_pre_dispatch_failure(poller, REPO, 7, _failed(False)) is None

        assert 7 in poller.seen_issues[REPO]
        assert 7 not in poller.pre_dispatch_failures.get(REPO, {})

    def test_success_and_blocked_are_left_alone(self, tmp_path: Path) -> None:
        poller = _poller(tmp_path / "state.json")
        poller.mark_seen(REPO, 7)
        ok = PipelineResult(success=True, session_id="s", summary="x",
                            outputs={"failed_before_dispatch": True})
        blocked = PipelineResult(success=False, session_id="s", summary="x",
                                 blocked=True,
                                 outputs={"failed_before_dispatch": True})

        assert settle_pre_dispatch_failure(poller, REPO, 7, ok) is None
        assert settle_pre_dispatch_failure(poller, REPO, 7, blocked) is None
        assert 7 in poller.seen_issues[REPO]

    def test_the_count_survives_a_restart(self, tmp_path: Path) -> None:
        state = tmp_path / "state.json"
        poller = _poller(state)
        poller.mark_seen(REPO, 7)
        settle_pre_dispatch_failure(poller, REPO, 7, _failed(True))

        on_disk = json.loads(state.read_text())
        assert on_disk["pre_dispatch_failures"] == {REPO: {"7": 1}}
        assert _poller(state).pre_dispatch_failures == {REPO: {7: 1}}

    @pytest.mark.parametrize("bad", [
        {"pre_dispatch_failures": {REPO: {"x": 1}}},
        {"pre_dispatch_failures": {REPO: [1]}},
        {"pre_dispatch_failures": "nope"},
    ])
    def test_unreadable_counts_start_fresh(self, tmp_path: Path, bad) -> None:
        state = tmp_path / "state.json"
        state.write_text(json.dumps({"seen_issues": {REPO: [1]}, **bad}))

        poller = _poller(state)

        assert poller.pre_dispatch_failures == {}


def test_reclone_message_names_where_the_old_copy_went() -> None:
    msg = reclone_message(REPO, Path("/r/owner-repo.git.replaced-20261031T000000Z"))
    assert REPO in msg and "/r/owner-repo.git.replaced-20261031T000000Z" in msg


def _dev_mocks(tmp_path: Path):
    worktree = AsyncMock()
    worktree.create_worktree_with_new_branch.return_value = (tmp_path / "wt", True)
    worktree.symlink_context = MagicMock()
    worktree.remove_context_symlink = MagicMock()
    worktree.branch_exists_locally.return_value = False
    worktree.branch_exists_on_remote.return_value = False
    github = AsyncMock()
    github.get_issue.return_value = {"number": 7, "title": "t", "body": "b"}
    return worktree, github


@pytest.mark.asyncio
async def test_dev_marks_a_clone_failure_as_before_dispatch(tmp_path: Path) -> None:
    from ctrlrelay.core.state import StateDB
    from ctrlrelay.core.worktree import WorktreeError
    from ctrlrelay.pipelines.dev import run_dev_issue

    worktree, github = _dev_mocks(tmp_path)
    worktree.ensure_bare_repo.side_effect = WorktreeError(
        "git failed: default branch diverged from origin (main)"
    )
    dispatcher = AsyncMock()

    result = await run_dev_issue(
        repo=REPO, issue_number=7, branch_template="fix/issue-{n}",
        dispatcher=dispatcher, github=github, worktree=worktree,
        dashboard=None, state_db=StateDB(tmp_path / "state.db"),
        transport=None, contexts_dir=tmp_path / "contexts",
    )

    assert not result.success
    assert result.outputs.get("failed_before_dispatch") is True
    dispatcher.spawn_session.assert_not_called()


@pytest.mark.asyncio
async def test_dev_does_not_mark_a_failure_after_the_agent_started(
    tmp_path: Path,
) -> None:
    from ctrlrelay.core.state import StateDB
    from ctrlrelay.pipelines.dev import run_dev_issue

    worktree, github = _dev_mocks(tmp_path)
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = RuntimeError("agent crashed")

    result = await run_dev_issue(
        repo=REPO, issue_number=7, branch_template="fix/issue-{n}",
        dispatcher=dispatcher, github=github, worktree=worktree,
        dashboard=None, state_db=StateDB(tmp_path / "state.db"),
        transport=None, contexts_dir=tmp_path / "contexts",
    )

    assert not result.success
    dispatcher.spawn_session.assert_called()
    assert "failed_before_dispatch" not in result.outputs


@pytest.mark.asyncio
async def test_task_marks_a_clone_failure_as_before_dispatch(tmp_path: Path) -> None:
    from ctrlrelay.core.state import StateDB
    from ctrlrelay.pipelines.task import run_task_issue

    worktree, github = _dev_mocks(tmp_path)
    worktree.ensure_bare_repo.side_effect = RuntimeError("clone failed")
    dispatcher = AsyncMock()

    result = await run_task_issue(
        repo=REPO, issue_number=7, dispatcher=dispatcher, github=github,
        worktree=worktree, dashboard=None,
        state_db=StateDB(tmp_path / "state.db"), transport=None,
        contexts_dir=tmp_path / "contexts",
    )

    assert not result.success
    assert result.outputs.get("failed_before_dispatch") is True
    dispatcher.spawn_session.assert_not_called()


@pytest.mark.asyncio
async def test_task_does_not_mark_a_failure_after_the_agent_started(
    tmp_path: Path,
) -> None:
    from ctrlrelay.core.state import StateDB
    from ctrlrelay.pipelines.task import run_task_issue

    worktree, github = _dev_mocks(tmp_path)
    worktree.create_worktree.return_value = tmp_path / "wt"
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = RuntimeError("agent crashed")

    result = await run_task_issue(
        repo=REPO, issue_number=7, dispatcher=dispatcher, github=github,
        worktree=worktree, dashboard=None,
        state_db=StateDB(tmp_path / "state.db"), transport=None,
        contexts_dir=tmp_path / "contexts",
    )

    assert not result.success
    dispatcher.spawn_session.assert_called()
    assert "failed_before_dispatch" not in result.outputs
