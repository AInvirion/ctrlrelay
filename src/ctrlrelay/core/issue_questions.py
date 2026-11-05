"""A run's question, asked on its GitHub issue as well as in chat (#328).

When a dev run ends BLOCKED on an issue, the question is saved to
``pending_resumes`` first (#324), then posted to chat and as a comment on
the issue. The first answer from either channel wins:
``StateDB.answer_pending_resume`` sets an answer only on a row that has
none, so a second answer cannot overwrite the first.

Who counts as an answer on GitHub
    Anyone can comment on a public repository, so only a comment from an
    account with write access counts. The relay posts as the same account
    as the operator, so its own comments carry a ``<!-- ctrlrelay:``
    marker, and only a comment WITHOUT one, created AFTER the question
    comment, is an answer.

What is posted on a public repository
    A question can name machines, other repositories and internal plans.
    Unless the repository is set to allow it, a public repository gets a
    short notice instead of the question, and a chat answer is not copied
    to it either. ``RepoConfig.question_on_issue``: ``auto`` (full text on
    a private repository, the notice on a public one or one whose
    visibility could not be read), ``full``, or ``notice``.

Every function here is best-effort towards GitHub: a failed call is
logged and the question stays answerable in chat.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("issue_questions")

# Any relay comment carries a marker that starts with this. A comment that
# has one is never an answer: the relay comments as the operator's account.
RELAY_MARKER_PREFIX = "<!-- ctrlrelay:"

QUESTION_ON_ISSUE_MODES = ("auto", "full", "notice")

# author_association values that MAY have write access; the permission
# API decides. OWNER always does.
_MAYBE_WRITERS = frozenset({"MEMBER", "COLLABORATOR"})
_WRITE_PERMISSIONS = frozenset({"admin", "write", "maintain"})

Notify = Callable[[str], Awaitable[None]]


def _marker(kind: str, session_id: str) -> str:
    return f"{RELAY_MARKER_PREFIX}{kind} session={session_id} -->"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def question_comment(session_id: str, question: str, full: bool) -> str:
    """The question comment, or the short notice that stands for it."""
    if full:
        head = (
            "**The relay has a question** and this issue's run is paused "
            "until it is answered:\n\n" + _quote(question.strip())
        )
    else:
        head = (
            "**A question is waiting for the operator in chat.** This "
            "issue's run is paused until it is answered."
        )
    return (
        f"{head}\n\n"
        "_Answer here or in chat; the first answer wins. A reply here "
        "counts only from an account with write access to this "
        "repository._\n"
        f"{_marker('question', session_id)}"
    )


def chat_answer_comment(session_id: str, answer: str, full: bool) -> str:
    """The issue comment for an answer given in chat."""
    if full:
        body = "**Answered in chat:**\n\n" + _quote(answer.strip()) + (
            "\n\nWork continues."
        )
    else:
        body = "**Answered in chat.** Work continues."
    return f"{body}\n{_marker('answer', session_id)}"


def expired_comment(session_id: str, reason: str | None) -> str:
    why = f" ({reason})" if reason else ""
    return (
        f"**The question above expired unanswered{why}.** An answer to it "
        "is no longer applied, here or in chat.\n"
        f"{_marker('expired', session_id)}"
    )


def already_answered_comment(session_id: str, login: str) -> str:
    return (
        f"@{login}'s reply above was not applied: the question had already "
        "been answered in chat, and work continues with that answer.\n"
        f"{_marker('already-answered', session_id)}"
    )


def github_answer_note(repo: str, issue_number: int, login: str) -> str:
    """The chat note for an answer given on the issue."""
    return (
        f"#{issue_number} in {repo} was answered on GitHub by @{login}; "
        "work continues. No need to answer it here."
    )


async def resolve_full_text(github: Any, repo: str, mode: str | None) -> bool:
    """Whether the issue may carry the question's text. ``auto`` reads
    the repository's visibility; one that cannot be read is treated as
    public, the side that leaks nothing."""
    if mode == "full":
        return True
    if mode == "notice":
        return False
    try:
        return bool(await github.repo_is_private(repo))
    except Exception as e:
        log_event(
            _logger, "issue_question.visibility_unknown", repo=repo,
            error_type=type(e).__name__, error=str(e)[:200],
        )
        return False


async def ask_on_issue(
    github: Any,
    state_db: Any,
    *,
    session_id: str,
    repo: str,
    issue_number: int,
    question: str,
    mode: str | None = "auto",
) -> bool:
    """Post the question on its issue and record the comment, so the issue
    is watched for an answer. The ``pending_resumes`` row must already
    exist. Returns whether the question is now watched."""
    full = await resolve_full_text(github, repo, mode)
    asked_at = int(time.time())
    try:
        comment_id = await github.post_issue_comment(
            repo, issue_number, question_comment(session_id, question, full),
        )
    except Exception as e:
        log_event(
            _logger, "issue_question.post_failed", session_id=session_id,
            repo=repo, issue_number=issue_number,
            error_type=type(e).__name__, error=str(e)[:200],
        )
        return False
    try:
        watched = state_db.record_issue_question(
            session_id, issue_number, int(comment_id), asked_at,
        )
    except Exception as e:
        log_event(
            _logger, "issue_question.record_failed", session_id=session_id,
            repo=repo, error_type=type(e).__name__, error=str(e)[:200],
        )
        return False
    log_event(
        _logger, "issue_question.asked", session_id=session_id, repo=repo,
        issue_number=issue_number, comment_id=comment_id, full_text=full,
        watched=watched,
    )
    return watched


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _created_ts(comment: dict[str, Any]) -> float | None:
    raw = comment.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def _has_write_access(
    github: Any, repo: str, comment: dict[str, Any],
    cache: dict[tuple[str, str], bool | None],
) -> bool | None:
    """True or False, or None when it could not be checked (asked again
    on the next poll: "could not look" is not "no")."""
    login = (comment.get("user") or {}).get("login") or ""
    association = comment.get("author_association") or ""
    if not login:
        return False
    if association == "OWNER":
        return True
    if association not in _MAYBE_WRITERS:
        return False
    key = (repo, login)
    if key not in cache:
        try:
            permission = await github.get_collaborator_permission(repo, login)
            cache[key] = permission in _WRITE_PERMISSIONS
        except Exception as e:
            log_event(
                _logger, "issue_question.permission_unchecked", repo=repo,
                login=login, error_type=type(e).__name__, error=str(e)[:200],
            )
            cache[key] = None
    return cache[key]


async def _note(notify: Notify | None, text: str) -> None:
    if notify is None:
        return
    try:
        await notify(text)
    except Exception as e:
        log_event(
            _logger, "issue_question.note_failed",
            error_type=type(e).__name__, error=str(e)[:200],
        )


async def check_issue_answer(
    github: Any,
    state_db: Any,
    row: dict[str, Any],
    *,
    notify: Notify | None = None,
    permission_cache: dict[tuple[str, str], bool | None] | None = None,
) -> bool:
    """Read one watched question's issue for an answer. Returns whether
    this call recorded one.

    One comments call, with ``since`` the question's time. The first
    comment that is newer than the question comment, carries no relay
    marker and comes from an account with write access is the answer.
    Comments from anyone else are logged and ignored.
    """
    sid = row["session_id"]
    repo = row["repo"]
    issue_number = row.get("issue_number")
    question_id = row.get("issue_comment_id")
    asked_at = row.get("issue_asked_at")
    if issue_number is None or question_id is None or asked_at is None:
        return False
    cache = {} if permission_cache is None else permission_cache
    try:
        comments = await github.list_issue_comments_since(
            repo, int(issue_number), _iso(int(asked_at)),
        )
    except Exception as e:
        log_event(
            _logger, "issue_question.read_failed", session_id=sid, repo=repo,
            issue_number=issue_number, error_type=type(e).__name__,
            error=str(e)[:200],
        )
        return False
    for comment in comments:
        cid = comment.get("id")
        if not isinstance(cid, int) or cid <= int(question_id):
            continue
        created = _created_ts(comment)
        if created is not None and created < int(asked_at):
            continue
        body = comment.get("body") or ""
        if RELAY_MARKER_PREFIX in body or not body.strip():
            continue
        login = (comment.get("user") or {}).get("login") or ""
        allowed = await _has_write_access(github, repo, comment, cache)
        if allowed is None:
            # Could not check this one; do not skip past it to a later
            # comment, or a later reply could win over an earlier one.
            return False
        if not allowed:
            log_event(
                _logger, "issue_question.comment_ignored", session_id=sid,
                repo=repo, issue_number=issue_number, comment_id=cid,
                login=login,
                author_association=comment.get("author_association"),
                reason="no write access",
            )
            continue
        if state_db.answer_pending_resume(
            sid, body.strip(), source="github", by=login, ref=cid,
        ):
            log_event(
                _logger, "issue_question.answered_on_github", session_id=sid,
                repo=repo, issue_number=issue_number, comment_id=cid,
                login=login,
            )
            fresh = state_db.get_pending_resume(sid) or {}
            answered_at = fresh.get("answered_at")
            if answered_at is not None and state_db.mark_answer_noted(
                sid, int(answered_at),
            ):
                await _note(
                    notify, github_answer_note(repo, int(issue_number), login),
                )
            return True
        # Answered between our read and our write. If by this same
        # comment (another watcher got there first), nothing to say; if
        # in chat, tell the commenter their reply was not applied.
        fresh = state_db.get_pending_resume(sid) or {}
        if fresh.get("answer_ref") != cid and fresh.get("answered_at"):
            try:
                await github.post_issue_comment(
                    repo, int(issue_number),
                    already_answered_comment(sid, login),
                )
            except Exception:
                pass
        return False
    return False


async def watch_issue_answers(
    github: Any, state_db: Any, *, notify: Notify | None = None,
) -> int:
    """Read every watched question's issue once. Returns how many were
    answered. Only issues with an open question are read."""
    try:
        rows = state_db.list_issue_questions_to_watch()
    except Exception as e:
        log_event(
            _logger, "issue_question.list_failed",
            error_type=type(e).__name__, error=str(e)[:200],
        )
        return 0
    cache: dict[tuple[str, str], bool | None] = {}
    answered = 0
    for row in rows:
        if await check_issue_answer(
            github, state_db, row, notify=notify, permission_cache=cache,
        ):
            answered += 1
    return answered


async def note_chat_answer_on_issue(
    github: Any,
    state_db: Any,
    row: dict[str, Any],
    *,
    mode: str | None = "auto",
) -> bool:
    """Copy a chat answer to the issue the question was asked on. Once
    per answer; a GitHub answer, or a question never asked on the issue,
    is left alone. Returns whether a comment was posted."""
    if (
        row.get("issue_comment_id") is None
        or row.get("issue_number") is None
        or row.get("answered_at") is None
        or row.get("answer_source") == "github"
        or row.get("answer_noted_at") is not None
    ):
        return False
    sid = row["session_id"]
    repo = row["repo"]
    try:
        if not state_db.mark_answer_noted(sid, int(row["answered_at"])):
            return False
    except Exception as e:
        log_event(
            _logger, "issue_question.note_claim_failed", session_id=sid,
            error_type=type(e).__name__, error=str(e)[:200],
        )
        return False
    full = await resolve_full_text(github, repo, mode)
    try:
        await github.post_issue_comment(
            repo, int(row["issue_number"]),
            chat_answer_comment(sid, row.get("answer") or "", full),
        )
    except Exception as e:
        log_event(
            _logger, "issue_question.answer_copy_failed", session_id=sid,
            repo=repo, error_type=type(e).__name__, error=str(e)[:200],
        )
        return False
    return True


async def note_expiry_on_issue(github: Any, row: dict[str, Any]) -> bool:
    """Say on the issue that its question expired. Returns whether a
    comment was posted."""
    if row.get("issue_comment_id") is None or row.get("issue_number") is None:
        return False
    try:
        await github.post_issue_comment(
            row["repo"], int(row["issue_number"]),
            expired_comment(row["session_id"], row.get("expired_reason")),
        )
    except Exception as e:
        log_event(
            _logger, "issue_question.expiry_note_failed",
            session_id=row["session_id"], repo=row["repo"],
            error_type=type(e).__name__, error=str(e)[:200],
        )
        return False
    return True


__all__ = [
    "QUESTION_ON_ISSUE_MODES",
    "RELAY_MARKER_PREFIX",
    "ask_on_issue",
    "check_issue_answer",
    "note_chat_answer_on_issue",
    "note_expiry_on_issue",
    "watch_issue_answers",
]
