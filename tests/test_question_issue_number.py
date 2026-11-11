"""A dev question carries its run's issue number, posted or not (#412).

``pending_resumes.issue_number`` used to be written only after the question
was posted on the issue. A question persisted without being asked there (no
transport, rounds used up, or a failed post) had no issue number, so closing
the issue did not retire it and only the 48-hour TTL could.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ctrlrelay.core.agent_plugins import AgentIdentity
from ctrlrelay.core.dispatcher import RepoAgentRouter
from ctrlrelay.core.issue_questions import ask_on_issue, note_expiry_on_issue
from ctrlrelay.core.question_expiry import (
    EXPIRY_REASON_RESOLVED,
    expire_stale_questions,
)
from ctrlrelay.core.state import StateDB
from ctrlrelay.pipelines.dev import resume_dev_from_pending, run_dev_issue
from tests.test_question_survives_restart import (
    AGENT_UUID,
    QUESTION,
    REPO,
    _blocked,
    _github,
    _HangingTransport,
    _worktree,
)

TTL = 172800


async def _blocked_run_without_transport(tmp_path: Path, db: StateDB) -> str:
    """A dev run on issue 13 that ends BLOCKED with nobody to ask: the
    question is persisted and never posted."""
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = (
        lambda **kw: _blocked(kw["session_id"])
    )
    github = _github()
    result = await run_dev_issue(
        claims=None, repo=REPO, issue_number=13,
        branch_template="fix/issue-{n}", dispatcher=dispatcher,
        github=github, worktree=_worktree(tmp_path / "wt"),
        dashboard=None, state_db=db, transport=None,
        contexts_dir=tmp_path / "contexts",
    )
    assert result.blocked
    return result.session_id


@pytest.mark.asyncio
async def test_a_question_persisted_without_posting_names_its_issue(
    tmp_path: Path,
) -> None:
    db = StateDB(tmp_path / "state.db")
    sid = await _blocked_run_without_transport(tmp_path, db)

    row = db.get_pending_resume(sid)
    assert row["issue_number"] == 13
    # Not asked on the issue, so the issue is not watched for an answer.
    assert row["issue_comment_id"] is None
    assert db.list_issue_questions_to_watch() == []
    db.close()


@pytest.mark.asyncio
async def test_closing_the_issue_retires_an_unposted_question(
    tmp_path: Path,
) -> None:
    """The reported case, end to end: the question cites no ``#N``, its
    issue is closed elsewhere, and the next resolved-elsewhere sweep
    retires it. Nothing is labelled, since the question was never on the
    issue and was not left unanswered there."""
    db = StateDB(tmp_path / "state.db")
    sid = await _blocked_run_without_transport(tmp_path, db)
    assert "#" not in QUESTION
    github = AsyncMock()

    async def state(repo: str, number: int, **_kw: object) -> dict:
        return {"number": number, "state": "closed" if number == 13 else "open"}

    github.get_issue_or_pr_state.side_effect = state

    expired = await expire_stale_questions(db, github, ttl_seconds=TTL)

    assert [r["session_id"] for r in expired] == [sid]
    assert expired[0]["expired_reason"] == EXPIRY_REASON_RESOLVED
    probed = [c.args for c in github.get_issue_or_pr_state.await_args_list]
    assert probed == [(REPO, 13)]
    assert not await note_expiry_on_issue(github, expired[0])
    github.add_issue_label.assert_not_awaited()
    github.post_issue_comment.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
async def test_an_open_issue_keeps_an_unposted_question(tmp_path: Path) -> None:
    """The control: the same question with its issue still open stays."""
    db = StateDB(tmp_path / "state.db")
    sid = await _blocked_run_without_transport(tmp_path, db)
    github = AsyncMock()
    github.get_issue_or_pr_state.return_value = {"number": 13, "state": "open"}

    assert await expire_stale_questions(db, github, ttl_seconds=TTL) == []
    assert [r["session_id"] for r in db.list_unanswered_pending_resumes()] == [sid]
    db.close()


@pytest.mark.asyncio
async def test_a_question_saved_before_the_wait_names_its_issue(
    tmp_path: Path,
) -> None:
    """The row written before the in-process wait (#324) carries the issue
    too, so a poller stopped during the wait leaves a retirable question.
    Posting on the issue fails here, so the number cannot have come from
    the post."""
    db = StateDB(tmp_path / "state.db")
    dispatcher = AsyncMock()
    dispatcher.spawn_session.side_effect = (
        lambda **kw: _blocked(kw["session_id"])
    )
    github = _github()
    github.post_issue_comment.side_effect = RuntimeError("502")
    transport = _HangingTransport()
    task = asyncio.create_task(run_dev_issue(
        claims=None, repo=REPO, issue_number=13,
        branch_template="fix/issue-{n}", dispatcher=dispatcher,
        github=github, worktree=_worktree(tmp_path / "wt"),
        dashboard=None, state_db=db, transport=transport,
        contexts_dir=tmp_path / "contexts",
    ))
    await asyncio.wait_for(transport.asked.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (row,) = db.list_unanswered_pending_resumes()
    assert row["issue_comment_id"] is None
    assert row["issue_number"] == 13
    db.close()


@pytest.mark.asyncio
async def test_a_resumed_run_that_blocks_again_names_its_issue(
    tmp_path: Path,
) -> None:
    """The re-block path: a resume from a saved answer ends BLOCKED again
    with nobody to ask, and its new question is persisted unposted."""
    db = StateDB(tmp_path / "state.db")
    identity = AgentIdentity(agent_type="claude", plugin_id="claude")
    worktree_path = tmp_path / "wt"
    wt = _worktree(worktree_path)
    db.execute(
        "INSERT INTO sessions (id, pipeline, repo, issue_number, worktree_path, "
        "status, started_at, agent_session_id, agent_type, agent_plugin_id, "
        "agent_plugin_version) VALUES (?, 'dev', ?, 13, ?, 'blocked', 1, ?, ?, ?, '1')",
        ("dev-again", REPO, str(worktree_path), AGENT_UUID,
         identity.agent_type, identity.plugin_id),
    )
    db.commit()
    # The earlier question, answered: the re-block rewrites this row. It
    # carries a different issue number and a posted comment, so the rewrite
    # is seen replacing both rather than leaving them as they were.
    db.add_pending_resume(
        session_id="dev-again", pipeline="dev", repo=REPO,
        question="an earlier question", issue_number=99,
    )
    assert db.record_issue_question("dev-again", 99, 9000, 1)
    assert db.answer_pending_resume("dev-again", "continue")
    agent = AsyncMock()
    agent.agent_type = "claude"
    agent.agent_identity = identity
    agent.spawn_session.side_effect = lambda **kw: _blocked(kw["session_id"])
    router = RepoAgentRouter(default=agent, by_type={"claude": agent})

    result = await resume_dev_from_pending(
        session_id="dev-again", repo=REPO, answer="continue",
        branch_template="fix/issue-{n}", dispatcher=router,
        github=_github(), worktree=wt, dashboard=None, state_db=db,
        transport=None, contexts_dir=tmp_path / "contexts",
    )

    assert result.blocked
    row = db.get_pending_resume("dev-again")
    assert row is not None and row["answered_at"] is None
    assert row["question"] == QUESTION
    assert row["issue_comment_id"] is None
    assert row["issue_number"] == 13
    db.close()


@pytest.mark.asyncio
async def test_a_failed_post_is_still_not_watched(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    db.add_pending_resume(
        session_id="s-1", pipeline="dev", repo=REPO, question=QUESTION,
        issue_number=13,
    )
    github = AsyncMock()
    github.post_issue_comment.side_effect = RuntimeError("502")

    await ask_on_issue(
        github, db, session_id="s-1", repo=REPO, issue_number=13,
        question=QUESTION, mode="full",
    )

    row = db.get_pending_resume("s-1")
    assert row["issue_number"] == 13
    assert row["issue_comment_id"] is None
    assert db.list_issue_questions_to_watch() == []
    db.close()


def test_an_open_dev_row_from_before_is_backfilled(tmp_path: Path) -> None:
    """A database written by an older version: the open dev question has
    no issue number, its session row has one. Opening the database fills
    it in; answered, expired and non-dev rows are left as they are."""
    db = StateDB(tmp_path / "state.db")
    now = int(time.time())
    rows = (("open", "dev"), ("done", "dev"), ("said", "dev"), ("sec", "secops"))
    for sid, pipeline in rows:
        db.execute(
            "INSERT INTO sessions (id, pipeline, repo, issue_number, status, "
            "started_at) VALUES (?, ?, ?, 13, 'blocked', ?)",
            (sid, pipeline, REPO, now),
        )
        db.add_pending_resume(
            session_id=sid, pipeline=pipeline, repo=REPO, question=QUESTION,
        )
    assert db.expire_pending_resume("done", "ttl")
    assert db.answer_pending_resume("said", "go")
    db.close()

    again = StateDB(tmp_path / "state.db")
    numbers = {
        r["session_id"]: r["issue_number"]
        for r in again.execute(
            "SELECT session_id, issue_number FROM pending_resumes"
        ).fetchall()
    }
    assert numbers == {"open": 13, "done": None, "said": None, "sec": None}
    again.close()
