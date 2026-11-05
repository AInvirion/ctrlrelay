"""The review as a gate tied to the exact commit (#222).

- The verdict is recorded with the commit it reviewed: on the result's
  ``review`` field and on the ``review_finished`` event the status page
  and ``/api/status`` read.
- After a review the PR's head is read from GitHub. A different commit
  means somebody pushed after the review: the verdict is stale (a
  ``review_stale`` event and a PR comment saying it needs review again)
  and the new head is reviewed before the run reports done.
- With ``steps.review: required`` the PR is opened as a draft, a reused
  PR is put back to draft when this run pushes to it, and it is marked
  ready only once the latest review passed on its current head.

Each case drives the real ``run_dev_issue`` with the fakes the step
switch tests use, and checks what GitHub was told and what the run said.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ctrlrelay.core.code_review import CodeReviewOutcome, ReviewStatus
from ctrlrelay.core.state import StateDB
from tests.test_dev_step_switches import (
    CANNOT_RUN,
    PR,
    REVIEWED,
    _fakes,
    _repo,
    _reviewer,
    _run,
)

PUSHED = "c0ffee"  # what the fake push returns
LATER = "d" * 40  # a commit somebody pushed after the review
REQUIRED = {"review": "required"}


def _calls(github, worktree) -> list[tuple]:
    """GitHub and review-copy calls in the order they happened."""
    log: list[tuple] = []

    def track(mock, name, fn=None):
        original = mock.side_effect
        rv = mock.return_value

        async def side(*a, **kw):
            log.append((name, *a[1:], *sorted(kw.items())))
            if original is not None:
                if isinstance(original, BaseException) or (
                    isinstance(original, type)
                    and issubclass(original, BaseException)
                ):
                    raise original
                if callable(original):
                    return await original(*a, **kw)
                return next(original)
            return rv

        mock.side_effect = side

    track(github.get_pr_head, "head")
    track(github.set_pr_draft, "draft")
    track(github.comment_on_pr, "comment")
    track(worktree.make_review_copy, "copy")
    track(worktree.fetch_pr_head, "fetch")
    return log


def _after_first_copy(log: list[tuple]) -> list[tuple]:
    """The calls from the first review copy on: the run also fetches the
    bare clone once at its start, which is not the review's doing."""
    names = [c[0] for c in log]
    return log[names.index("copy"):]


def _heads(*shas):
    return iter([{"sha": s, "draft": True} for s in shas])


def _fetch_follows(github, worktree) -> None:
    """The PR-ref fetch brings in whatever head GitHub last reported."""
    seen: list[str] = []
    original = github.get_pr_head.side_effect

    async def head(*_a, **_kw):
        got = next(original) if not isinstance(original, BaseException) else None
        if got is None:
            raise original
        seen.append(got["sha"])
        return got

    async def fetch(*_a, **_kw):
        return seen[-1]

    github.get_pr_head.side_effect = head
    worktree.fetch_pr_head.side_effect = fetch


def _events(state_db: StateDB, kind: str) -> list[dict]:
    return [
        e["detail"] for e in reversed(state_db.list_events(limit=1000))
        if e["kind"] == kind
    ]


# --- the verdict names its commit -------------------------------------------


@pytest.mark.asyncio
async def test_the_verdict_is_recorded_with_the_commit_it_reviewed(
    tmp_path: Path, monkeypatch,
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    state_db = StateDB(tmp_path / "s.db")
    result, *_ = await _run(tmp_path, repo=_repo(), state_db=state_db)
    assert result.success, result
    assert result.review.posted
    assert result.review.head_sha == PUSHED
    assert _events(state_db, "review_finished") == [{
        "pr_number": PR, "ok": True, "head_sha": PUSHED,
        "status": "reviewed", "posted": True,
    }]
    assert _events(state_db, "review_stale") == []


# --- draft and ready ----------------------------------------------------------


@pytest.mark.asyncio
async def test_required_opens_a_draft_and_marks_it_ready_after_a_pass(
    tmp_path: Path, monkeypatch,
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    log = _calls(github, worktree)
    result, *_ = await _run(
        tmp_path, repo=_repo(steps=REQUIRED), worktree=worktree, github=github,
    )
    assert result.success, result
    assert github.create_pr.await_args.kwargs.get("draft") is True
    # Ready comes last: after the review, and after its head was confirmed.
    assert [c[0] for c in _after_first_copy(log)] == [
        "copy", "comment", "head", "draft",
    ]
    assert log[-1] == ("draft", PR, False)
    assert result.review.head_sha == PUSHED


@pytest.mark.asyncio
async def test_without_required_the_pr_is_not_a_draft(
    tmp_path: Path, monkeypatch,
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    for steps in ({}, {"review": "on"}):
        worktree, github = _fakes(tmp_path)
        result, *_ = await _run(
            tmp_path / str(len(steps)), repo=_repo(steps=steps),
            worktree=worktree, github=github,
        )
        assert result.success, (steps, result)
        assert not github.create_pr.await_args.kwargs.get("draft"), steps
        github.set_pr_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_puts_a_reused_ready_pr_back_to_draft(
    tmp_path: Path, monkeypatch,
) -> None:
    """A run pushing to a PR that already exists has added commits no
    review has read: the PR shows as not ready until this run's review."""
    _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    github.list_prs.return_value = [{
        "number": PR,
        "headRepositoryOwner": {"login": "owner"},
        "headRepository": {"name": "repo"},
        "baseRefName": "main",
    }]
    log = _calls(github, worktree)
    result, *_ = await _run(
        tmp_path, repo=_repo(steps=REQUIRED), worktree=worktree, github=github,
    )
    assert result.success, result
    github.create_pr.assert_not_awaited()
    drafts = [c for c in log if c[0] == "draft"]
    assert drafts == [("draft", PR, True), ("draft", PR, False)]
    assert log.index(("draft", PR, True)) < log.index(
        next(c for c in log if c[0] == "copy")
    )


@pytest.mark.asyncio
async def test_a_reused_pr_is_left_alone_without_required(
    tmp_path: Path, monkeypatch,
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    github.list_prs.return_value = [{
        "number": PR,
        "headRepositoryOwner": {"login": "owner"},
        "headRepository": {"name": "repo"},
        "baseRefName": "main",
    }]
    result, *_ = await _run(
        tmp_path, repo=_repo(), worktree=worktree, github=github,
    )
    assert result.success, result
    github.set_pr_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_review_that_fails_leaves_the_pr_a_draft(
    tmp_path: Path, monkeypatch,
) -> None:
    _reviewer(monkeypatch, CANNOT_RUN)
    worktree, github = _fakes(tmp_path)
    result, *_ = await _run(
        tmp_path, repo=_repo(steps=REQUIRED), worktree=worktree, github=github,
    )
    assert not result.success
    assert github.create_pr.await_args.kwargs.get("draft") is True
    github.set_pr_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_fails_when_the_pr_cannot_be_marked_ready(
    tmp_path: Path, monkeypatch,
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    github.set_pr_draft.side_effect = RuntimeError("gh broke")
    result, *_ = await _run(
        tmp_path, repo=_repo(steps=REQUIRED), worktree=worktree, github=github,
    )
    assert not result.success
    assert "could not be marked ready" in result.error
    assert f"PR #{PR} not handed over" in result.summary


# --- a push after the review ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_push_after_the_review_is_reviewed_before_ready(
    tmp_path: Path, monkeypatch,
) -> None:
    reviewer = _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    github.get_pr_head.side_effect = _heads(LATER, LATER)
    _fetch_follows(github, worktree)
    log = _calls(github, worktree)
    state_db = StateDB(tmp_path / "s.db")
    result, *_ = await _run(
        tmp_path, repo=_repo(steps=REQUIRED), worktree=worktree, github=github,
        state_db=state_db,
    )
    assert result.success, result
    assert reviewer.await_count == 2
    assert result.review.posted and result.review.head_sha == LATER
    # The new head was fetched, then reviewed from a copy of it, then
    # confirmed, and only then the PR marked ready.
    review_calls = _after_first_copy(log)
    copies = [c[1] for c in review_calls if c[0] == "copy"]
    assert copies == [PUSHED, LATER]
    names = [c[0] for c in review_calls]
    assert names.count("fetch") == 1
    assert names.index("fetch") < names.index("copy", 1)
    assert log[-1] == ("draft", PR, False)
    assert [c for c in log if c[0] == "draft"] == [("draft", PR, False)]
    # The stale verdict is visible: an event, and a comment on the PR.
    assert _events(state_db, "review_stale") == [{
        "pr_number": PR, "head_sha": PUSHED, "current_sha": LATER,
    }]
    assert [e["head_sha"] for e in _events(state_db, "review_finished")] == [
        PUSHED, LATER,
    ]
    stale = [c for c in log if c[0] == "comment" and "needs review again" in c[2]]
    assert len(stale) == 1 and PUSHED in stale[0][2] and LATER in stale[0][2]


@pytest.mark.asyncio
async def test_a_push_after_the_review_is_reviewed_again_when_not_required(
    tmp_path: Path, monkeypatch,
) -> None:
    reviewer = _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    github.get_pr_head.side_effect = _heads(LATER, LATER)
    _fetch_follows(github, worktree)
    result, *_ = await _run(
        tmp_path, repo=_repo(), worktree=worktree, github=github,
    )
    assert result.success, result
    assert reviewer.await_count == 2
    assert result.review.posted and result.review.head_sha == LATER
    github.set_pr_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_head_that_keeps_moving_is_not_a_pass(
    tmp_path: Path, monkeypatch,
) -> None:
    import ctrlrelay.pipelines.dev as dev

    reviewer = _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    moving = [c * 40 for c in "123456789"]
    github.get_pr_head.side_effect = _heads(*moving)
    _fetch_follows(github, worktree)
    result, *_ = await _run(
        tmp_path, repo=_repo(steps=REQUIRED), worktree=worktree, github=github,
    )
    assert not result.success
    assert reviewer.await_count == dev.MAX_REVIEW_ROUNDS
    assert result.review.status == "stale"
    assert moving[dev.MAX_REVIEW_ROUNDS - 1] in result.review.detail
    github.set_pr_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_new_head_that_cannot_be_fetched_is_not_reviewed(
    tmp_path: Path, monkeypatch,
) -> None:
    reviewer = _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    github.get_pr_head.side_effect = _heads(LATER)

    worktree.fetch_pr_head.side_effect = RuntimeError("network")
    result, *_ = await _run(
        tmp_path, repo=_repo(steps=REQUIRED), worktree=worktree, github=github,
    )
    assert not result.success
    assert reviewer.await_count == 1
    assert result.review.status == "stale"
    assert LATER in result.review.detail
    github.set_pr_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_with_an_unreadable_head_is_not_a_pass(
    tmp_path: Path, monkeypatch,
) -> None:
    _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    github.get_pr_head.side_effect = RuntimeError("gh broke")
    result, *_ = await _run(
        tmp_path, repo=_repo(steps=REQUIRED), worktree=worktree, github=github,
    )
    assert not result.success
    assert result.review.status == "head_unknown"
    github.set_pr_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_head_does_not_unpost_a_review_that_is_not_required(
    tmp_path: Path, monkeypatch,
) -> None:
    """Not a gate here: the comment already names the commit it read."""
    _reviewer(monkeypatch, REVIEWED)
    worktree, github = _fakes(tmp_path)
    github.get_pr_head.side_effect = RuntimeError("gh broke")
    result, *_ = await _run(
        tmp_path, repo=_repo(), worktree=worktree, github=github,
    )
    assert result.success
    assert result.review.posted and result.review.head_sha == PUSHED


@pytest.mark.asyncio
async def test_a_review_that_did_not_post_does_not_ask_for_the_head(
    tmp_path: Path, monkeypatch,
) -> None:
    _reviewer(monkeypatch, CodeReviewOutcome(
        status=ReviewStatus.NO_EVIDENCE, output="No findings.",
    ))
    worktree, github = _fakes(tmp_path)
    result, *_ = await _run(
        tmp_path, repo=_repo(), worktree=worktree, github=github,
    )
    assert not result.review.posted
    assert result.review.head_sha == PUSHED
    github.get_pr_head.assert_not_awaited()


# --- GitHubCLI ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pr_draft_flag() -> None:
    from ctrlrelay.core.github import GitHubCLI

    url = "https://github.com/owner/repo/pull/7\n"
    for draft, expected in ((True, True), (False, False)):
        with patch("ctrlrelay.core.github.GitHubCLI._run_gh") as run:
            run.return_value = url
            await GitHubCLI().create_pr(
                "owner/repo", title="t", body="b", head="h", base="main",
                draft=draft,
            )
            assert ("--draft" in run.call_args[0]) is expected


@pytest.mark.asyncio
async def test_set_pr_draft_and_ready() -> None:
    from ctrlrelay.core.github import GitHubCLI

    with patch("ctrlrelay.core.github.GitHubCLI._run_gh", new=AsyncMock()) as run:
        await GitHubCLI().set_pr_draft("owner/repo", 7, True)
        assert run.call_args[0] == (
            "pr", "ready", "7", "--repo", "owner/repo", "--undo",
        )
        await GitHubCLI().set_pr_draft("owner/repo", 7, False)
        assert run.call_args[0] == ("pr", "ready", "7", "--repo", "owner/repo")


@pytest.mark.asyncio
async def test_get_pr_head_reads_the_head_commit() -> None:
    from ctrlrelay.core.github import GitHubCLI

    with patch("ctrlrelay.core.github.GitHubCLI._run_gh", new=AsyncMock()) as run:
        run.return_value = json.dumps({"headRefOid": LATER, "isDraft": True})
        head = await GitHubCLI().get_pr_head("owner/repo", 7)
    assert head == {"sha": LATER, "draft": True}
    assert "headRefOid,isDraft" in run.call_args[0]


# --- drafts the repository cannot have (#222 review) ---------------------------


def _publisher(github, *, draft: bool = True):
    from ctrlrelay.pipelines.dev import _Publisher

    return _Publisher(
        github=github, worktree=MagicMock(), state_db=MagicMock(),
        repo="owner/repo", branch="fix/issue-1", issue_number=1,
        issue_title="t", session_id="s", pr_number=42, draft=draft,
    )


@pytest.mark.asyncio
async def test_a_repo_without_drafts_still_gets_a_pr_and_says_unreviewed() -> None:
    github = AsyncMock()
    calls: list[bool] = []

    async def create(repo, **kw):
        calls.append(bool(kw.get("draft")))
        if kw.get("draft"):
            raise RuntimeError(
                "gh failed: Draft pull requests are not supported in this "
                "repository."
            )
        return {"number": 9, "url": "https://github.com/owner/repo/pull/9"}

    github.create_pr.side_effect = create
    created = await _publisher(github)._create_pr(
        title="t", body="b", head="fix/issue-1", base="main",
    )
    assert created["number"] == 9
    assert calls == [True, False]
    [(args, _)] = [(c.args, c.kwargs) for c in github.comment_on_pr.await_args_list]
    assert args[1] == 9 and "Not reviewed yet" in args[2]


@pytest.mark.asyncio
async def test_any_other_create_failure_is_not_retried_without_draft() -> None:
    github = AsyncMock()
    github.create_pr.side_effect = RuntimeError("gh failed: 502")
    with pytest.raises(RuntimeError, match="502"):
        await _publisher(github)._create_pr(
            title="t", body="b", head="fix/issue-1", base="main",
        )
    assert github.create_pr.await_count == 1


@pytest.mark.asyncio
async def test_a_reused_pr_that_cannot_go_back_to_draft_says_so_on_it() -> None:
    github = AsyncMock()
    github.set_pr_draft.side_effect = RuntimeError("gh failed: 403")
    await _publisher(github)._back_to_draft()
    [(args, _)] = [(c.args, c.kwargs) for c in github.comment_on_pr.await_args_list]
    assert args[1] == 42 and "Not reviewed yet" in args[2]


@pytest.mark.asyncio
async def test_a_reused_pr_back_to_draft_posts_nothing() -> None:
    github = AsyncMock()
    await _publisher(github)._back_to_draft()
    github.set_pr_draft.assert_awaited_once_with("owner/repo", 42, True)
    github.comment_on_pr.assert_not_awaited()
