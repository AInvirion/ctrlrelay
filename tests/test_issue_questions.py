"""A run's question is asked on its GitHub issue as well as in chat (#328).

The first answer from either channel wins. These tests drive a dev run that
blocks, answer it from one channel or the other, and look at what each
channel was told and how the session was resumed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import ctrlrelay.pipelines.dev as dev
from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus
from ctrlrelay.core.dispatcher import SessionResult
from ctrlrelay.core.issue_questions import (
    RELAY_MARKER_PREFIX,
    answer_text,
    check_issue_answer,
    note_chat_answer_on_issue,
    note_expiry_on_issue,
    question_comment,
    watch_issue_answers,
)
from ctrlrelay.core.state import StateDB
from ctrlrelay.pipelines.dev import resume_dev_from_pending, run_dev_issue
from tests._publish_fakes import fake_relay_publish

REPO = "owner/repo"
QUESTION = "pin or bump typescript? (alkaturbo has the old lockfile)"
AGENT_UUID = "11111111-2222-3333-4444-555555555555"
QUESTION_COMMENT_ID = 100


def _blocked(session_id: str) -> SessionResult:
    return SessionResult(
        session_id=session_id, exit_code=0, stdout="", stderr="",
        agent_session_id=AGENT_UUID,
        state=CheckpointState(
            version="1", status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
            session_id=session_id, timestamp="2026-11-04T22:22:19Z",
            question=QUESTION,
        ),
    )


def _resumed(session_id: str) -> SessionResult:
    """FAILED, so the run ends without publishing or waiting on CI."""
    return SessionResult(
        session_id=session_id, exit_code=0, stdout="", stderr="",
        agent_session_id=AGENT_UUID,
        state=CheckpointState(
            version="1", status=CheckpointStatus.FAILED,
            session_id=session_id, timestamp="2026-11-04T23:00:00Z",
            error="stopped here by the test",
        ),
    )


def _worktree(path: Path) -> AsyncMock:
    wt = AsyncMock()
    wt.push_run_branch.return_value = "c0ffee"
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text("gitdir: elsewhere\n")
    wt.create_worktree_with_new_branch.return_value = (path, True)
    wt.symlink_context = MagicMock()
    wt.remove_context_symlink = MagicMock()
    return wt


class _GitHub:
    """Enough of GitHubCLI for the question flow, recording what it posts.
    Everything else is an AsyncMock."""

    def __init__(self, private: bool = True) -> None:
        self.mock = AsyncMock()
        fake_relay_publish(self.mock)
        self.mock.get_issue.return_value = {
            "number": 13, "title": "x", "body": "y", "comments": [],
        }
        self.mock.repo_is_private.return_value = private
        self.posted: list[tuple[int, str]] = []
        self.issue_comments: list[dict[str, Any]] = []
        self.permissions: dict[str, str] = {}
        self.on_post: Any = None
        self.mock.post_issue_comment.side_effect = self._post
        self.mock.list_issue_comments_since.side_effect = self._since
        self.mock.get_collaborator_permission.side_effect = self._permission

    async def _post(self, repo: str, issue_number: int, body: str) -> int:
        if self.on_post is not None:
            self.on_post(body)
        self.posted.append((issue_number, body))
        return QUESTION_COMMENT_ID + len(self.posted) - 1

    async def _since(self, repo: str, n: int, since: str) -> list[dict]:
        return list(self.issue_comments)

    async def _permission(self, repo: str, login: str) -> str:
        return self.permissions.get(login, "none")

    def bodies(self, marker: str) -> list[str]:
        return [b for _, b in self.posted if marker in b]


class _Transport:
    """Records sends; ``ask`` answers with ``answer``, or never."""

    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.asked = asyncio.Event()
        self.sent: list[str] = []
        self.on_ask: Any = None

    async def ask(self, question: str, **kw: Any) -> str:
        if self.on_ask is not None:
            self.on_ask(question)
        self.asked.set()
        if self.answer is None:
            await asyncio.Event().wait()
        return self.answer  # type: ignore[return-value]

    async def send(self, text: str, **kw: Any) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        return None


def _comment(cid: int, body: str, association: str, login: str) -> dict:
    return {
        "id": cid, "body": body, "created_at": "2099-01-01T00:00:00Z",
        "updated_at": "2099-01-01T00:00:00Z",
        "author_association": association, "user": {"login": login},
    }


def _run(tmp_path, state_db, github, transport, dispatcher=None):
    if dispatcher is None:
        dispatcher = AsyncMock()
        calls = {"n": 0}

        def spawn(**kw: Any) -> SessionResult:
            calls["n"] += 1
            return _blocked(kw["session_id"]) if calls["n"] == 1 \
                else _resumed(kw["session_id"])

        dispatcher.spawn_session.side_effect = spawn
    return run_dev_issue(
        claims=None, repo=REPO, issue_number=13,
        branch_template="fix/issue-{n}", dispatcher=dispatcher,
        github=github.mock, worktree=_worktree(tmp_path / "wt"),
        dashboard=None, state_db=state_db, transport=transport,
        contexts_dir=tmp_path / "contexts",
    ), dispatcher


def _session_id(state_db: StateDB) -> str:
    (row,) = state_db.execute(
        "SELECT id FROM sessions WHERE issue_number = 13"
    ).fetchall()
    return row["id"]


@pytest.mark.asyncio
async def test_question_goes_to_chat_and_issue_after_the_row_exists(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    github = _GitHub(private=True)
    transport = _Transport(answer=None)
    seen: dict[str, Any] = {}

    def rows() -> list[dict]:
        return state_db.list_unanswered_pending_resumes()

    github.on_post = lambda body: seen.setdefault("issue", rows()) \
        if "ctrlrelay:question" in body else None
    transport.on_ask = lambda q: seen.setdefault("chat", rows())

    coro, _ = _run(tmp_path, state_db, github, transport)
    task = asyncio.create_task(coro)
    await asyncio.wait_for(transport.asked.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    sid = _session_id(state_db)
    assert [r["session_id"] for r in seen["issue"]] == [sid]
    assert [r["session_id"] for r in seen["chat"]] == [sid]
    (body,) = github.bodies("ctrlrelay:question")
    assert QUESTION in body  # private repo: full text
    row = state_db.get_pending_resume(sid)
    assert row["issue_number"] == 13
    assert row["issue_comment_id"] == QUESTION_COMMENT_ID


@pytest.mark.asyncio
async def test_public_repo_gets_a_notice_not_the_question(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    github = _GitHub(private=False)
    transport = _Transport(answer=None)
    coro, _ = _run(tmp_path, state_db, github, transport)
    task = asyncio.create_task(coro)
    await asyncio.wait_for(transport.asked.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (body,) = github.bodies("ctrlrelay:question")
    assert "waiting for the operator in chat" in body
    assert "typescript" not in body and "alkaturbo" not in body


@pytest.mark.asyncio
async def test_answer_on_the_issue_resumes_the_same_session_and_tells_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dev, "ANSWER_POLL_SECONDS", 0.01)
    state_db = StateDB(tmp_path / "state.db")
    github = _GitHub(private=True)
    github.permissions["maintainer"] = "write"
    github.issue_comments = [
        _comment(200, "/answer pin it", "COLLABORATOR", "maintainer"),
    ]
    transport = _Transport(answer=None)  # chat never answers

    coro, dispatcher = _run(tmp_path, state_db, github, transport)
    result = await asyncio.wait_for(coro, timeout=5)

    assert result.blocked
    sid = _session_id(state_db)
    # The agent ran once; the run let go and left the answer to the sweeper.
    assert dispatcher.spawn_session.await_count == 1
    assert any("answered on GitHub by @maintainer" in m for m in transport.sent)
    (todo,) = state_db.list_pending_resumes_to_execute()
    assert todo["session_id"] == sid
    assert todo["answer"] == "pin it"
    assert todo["answer_source"] == "github"

    resumer = AsyncMock()
    resumer.spawn_session.side_effect = lambda **kw: _resumed(kw["session_id"])
    await resume_dev_from_pending(
        session_id=sid, repo=REPO, answer=todo["answer"],
        branch_template="fix/issue-{n}", dispatcher=resumer,
        github=github.mock, worktree=_worktree(tmp_path / "wt"),
        dashboard=None, state_db=state_db, transport=None,
        contexts_dir=tmp_path / "contexts",
    )
    call = resumer.spawn_session.await_args_list[0].kwargs
    assert call["resume_session_id"] == AGENT_UUID
    assert call["session_id"] == sid
    assert "pin it" in call["prompt"]


@pytest.mark.asyncio
async def test_chat_answer_first_is_copied_to_the_issue_and_wins(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    github = _GitHub(private=True)
    transport = _Transport(answer="bump it")

    coro, dispatcher = _run(tmp_path, state_db, github, transport)
    await asyncio.wait_for(coro, timeout=5)

    sid = _session_id(state_db)
    (copied,) = github.bodies("ctrlrelay:answer")
    assert "Answered in chat" in copied and "bump it" in copied
    assert dispatcher.spawn_session.await_count == 2  # first run + resume

    # A later comment on the issue is not a second answer.
    github.issue_comments = [_comment(300, "/answer pin it", "OWNER", "oscar")]
    assert await watch_issue_answers(github.mock, state_db) == 0
    assert state_db.list_pending_resumes_to_execute() == []
    assert state_db.get_pending_resume(sid)["answer"] == "bump it"


def _watched_row(state_db: StateDB, sid: str = "s-1") -> dict:
    state_db.add_pending_resume(sid, "dev", REPO, QUESTION)
    assert state_db.record_issue_question(sid, 13, QUESTION_COMMENT_ID, 1)
    return state_db.get_pending_resume(sid)


@pytest.mark.asyncio
async def test_comments_that_do_not_count_leave_the_row_unanswered(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    row = _watched_row(state_db)
    github = _GitHub()
    github.permissions["reader"] = "read"
    github.issue_comments = [
        _comment(201, "/answer do X", "NONE", "stranger"),
        _comment(202, "/answer do Y", "CONTRIBUTOR", "drive-by"),
        _comment(203, "/answer do Z", "COLLABORATOR", "reader"),
        _comment(
            204, f"/answer claim\n{RELAY_MARKER_PREFIX}claimed node=a id=1 -->",
            "OWNER", "oscar",
        ),
        # Older than the question comment.
        _comment(99, "/answer old reply", "OWNER", "oscar"),
    ]
    assert not await check_issue_answer(github.mock, state_db, row)
    fresh = state_db.get_pending_resume("s-1")
    assert fresh["answered_at"] is None
    # A stranger never even costs a permission lookup.
    looked_up = [
        c.args[1] for c in github.mock.get_collaborator_permission.await_args_list
    ]
    assert looked_up == ["reader"]


@pytest.mark.asyncio
async def test_an_unreadable_permission_does_not_let_a_later_comment_win(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    row = _watched_row(state_db)
    github = _GitHub()
    github.mock.get_collaborator_permission.side_effect = RuntimeError("503")
    github.issue_comments = [
        _comment(201, "/answer first", "MEMBER", "member"),
        _comment(202, "/answer second", "OWNER", "oscar"),
    ]
    assert not await check_issue_answer(github.mock, state_db, row)
    assert state_db.get_pending_resume("s-1")["answered_at"] is None


@pytest.mark.asyncio
async def test_two_watchers_reading_the_same_comment_note_it_once(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    row = _watched_row(state_db)
    github = _GitHub()
    github.issue_comments = [_comment(201, "/answer pin it", "OWNER", "oscar")]
    notes: list[str] = []

    async def notify(text: str) -> None:
        notes.append(text)

    assert await check_issue_answer(github.mock, state_db, row, notify=notify)
    # A second watcher read the issue before the first wrote the answer.
    assert not await check_issue_answer(
        github.mock, state_db, row, notify=notify,
    )
    assert len(notes) == 1
    assert github.bodies("already-answered") == []


@pytest.mark.asyncio
async def test_a_github_reply_after_a_chat_answer_is_told_it_lost(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    row = _watched_row(state_db)  # read before the chat answer landed
    assert state_db.answer_pending_resume("s-1", "bump it")
    github = _GitHub()
    github.issue_comments = [_comment(201, "/answer pin it", "OWNER", "oscar")]
    assert not await check_issue_answer(github.mock, state_db, row)
    assert state_db.get_pending_resume("s-1")["answer"] == "bump it"
    (note,) = github.bodies("already-answered")
    assert "@oscar" in note


@pytest.mark.asyncio
async def test_expiry_is_said_on_the_issue(tmp_path: Path) -> None:
    state_db = StateDB(tmp_path / "state.db")
    _watched_row(state_db)
    assert state_db.expire_pending_resume("s-1", "ttl")
    github = _GitHub()
    row = state_db.get_pending_resume("s-1")
    assert await note_expiry_on_issue(github.mock, row)
    (body,) = github.bodies("ctrlrelay:expired")
    assert "expired" in body
    # And it is no longer watched.
    assert state_db.list_issue_questions_to_watch() == []


def test_a_re_block_forgets_the_old_issue_question(tmp_path: Path) -> None:
    state_db = StateDB(tmp_path / "state.db")
    _watched_row(state_db)
    state_db.answer_pending_resume("s-1", "x", source="github", by="o", ref=5)
    state_db.add_pending_resume("s-1", "dev", REPO, "a new question")
    row = state_db.get_pending_resume("s-1")
    assert row["issue_comment_id"] is None
    assert row["answer_source"] is None and row["answer_ref"] is None


@pytest.mark.asyncio
async def test_claim_goes_back_to_working_after_a_held_wait() -> None:
    from ctrlrelay.core.claims import (
        HELD,
        WORKING,
        Claim,
        ClaimKeeper,
        ClaimSettings,
    )

    github = AsyncMock()
    github.list_issue_comments.return_value = []
    claim = Claim(
        comment_id=7, node="a", claim_id="c1", state=WORKING,
        updated_at=None, replaces=None,
    )
    settings = ClaimSettings(node="a", account="acct")
    keeper = ClaimKeeper(github, REPO, 13, claim, settings)
    assert await keeper.hold()
    assert await keeper.resume_working()
    states = [
        c.args[2].split("state=")[1].split()[0]
        for c in github.edit_issue_comment.await_args_list
    ]
    assert states == [HELD, WORKING]
    await keeper.stop()


@pytest.mark.asyncio
async def test_an_own_account_comment_without_the_command_is_not_an_answer(
    tmp_path: Path,
) -> None:
    # Every session comments as the operator's account, with no relay
    # marker: a claim note after the question must not become the answer.
    state_db = StateDB(tmp_path / "state.db")
    row = _watched_row(state_db)
    github = _GitHub()
    github.issue_comments = [
        _comment(201, "Taking over PR #331 by hand on alkathink.", "OWNER", "oscar"),
        _comment(202, "/answer pin it", "OWNER", "oscar"),
    ]
    assert await check_issue_answer(github.mock, state_db, row)
    fresh = state_db.get_pending_resume("s-1")
    assert fresh["answer"] == "pin it"
    assert fresh["answer_ref"] == 202


@pytest.mark.parametrize("body, expected", [
    ("/answer pin it", "pin it"),
    ("  /ANSWER  pin it  ", "pin it"),
    ("/answer\npin it\nand bump", "pin it\nand bump"),
    ("/answer\r\npin it\r\nand bump", "pin it\nand bump"),
    ("/answer\tpin it", "pin it"),
    ("/answer", None),
    ("/answer   ", None),
    ("/answered it already", None),
    ("pin it /answer", None),
    ("Taking over this issue", None),
    ("", None),
])
def test_answer_text(body: str, expected: str | None) -> None:
    assert answer_text(body) == expected


def test_the_question_comment_says_how_to_answer() -> None:
    assert "`/answer`" in question_comment("s-1", "which?", True)
    assert "`/answer`" in question_comment("s-1", "which?", False)


@pytest.mark.asyncio
async def test_a_chat_answer_copy_that_fails_can_be_tried_again(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    _watched_row(state_db)
    assert state_db.answer_pending_resume("s-1", "bump it")
    row = state_db.get_pending_resume("s-1")
    github = _GitHub()
    github.mock.post_issue_comment.side_effect = RuntimeError("502")
    assert not await note_chat_answer_on_issue(github.mock, state_db, row, mode="full")
    assert state_db.get_pending_resume("s-1")["answer_noted_at"] is None

    github.mock.post_issue_comment.side_effect = github._post
    row = state_db.get_pending_resume("s-1")
    assert await note_chat_answer_on_issue(github.mock, state_db, row, mode="full")
    assert len(github.bodies("ctrlrelay:answer")) == 1


@pytest.mark.asyncio
async def test_a_question_that_ages_out_labels_the_issue(tmp_path: Path) -> None:
    """#400: a run paused on its issue, nobody answers, the sweep retires
    the question on age. The issue must carry something findable, and the
    comment must say the run stopped and how to go on."""
    from ctrlrelay.core.issue_questions import UNANSWERED_LABEL
    from ctrlrelay.core.question_expiry import expire_stale_questions

    state_db = StateDB(tmp_path / "state.db")
    _watched_row(state_db)
    state_db.execute(
        "UPDATE pending_resumes SET created_at = created_at - 200000"
    )
    state_db.commit()
    github = _GitHub()
    github.mock.get_issue_or_pr_state.return_value = {"state": "open"}

    (row,) = await expire_stale_questions(state_db, github.mock, ttl_seconds=172800)
    assert await note_expiry_on_issue(github.mock, row)

    github.mock.add_issue_label.assert_awaited_once_with(REPO, 13, UNANSWERED_LABEL)
    (body,) = github.bodies("ctrlrelay:expired")
    assert "has stopped" in body
    assert UNANSWERED_LABEL in body
    assert "seen_issues" in body


@pytest.mark.asyncio
async def test_a_question_retired_as_resolved_does_not_label(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    _watched_row(state_db)
    assert state_db.expire_pending_resume("s-1", "resolved_elsewhere")
    github = _GitHub()

    assert await note_expiry_on_issue(github.mock, state_db.get_pending_resume("s-1"))

    github.mock.add_issue_label.assert_not_awaited()
    (body,) = github.bodies("ctrlrelay:expired")
    assert "this issue is closed" in body


@pytest.mark.asyncio
async def test_a_label_that_cannot_be_added_still_leaves_the_comment(
    tmp_path: Path,
) -> None:
    state_db = StateDB(tmp_path / "state.db")
    _watched_row(state_db)
    assert state_db.expire_pending_resume("s-1", "ttl")
    github = _GitHub()
    github.mock.add_issue_label.side_effect = RuntimeError("no permission")

    assert await note_expiry_on_issue(github.mock, state_db.get_pending_resume("s-1"))
    (body,) = github.bodies("ctrlrelay:expired")
    assert "has stopped" in body
    assert "labelled" not in body
