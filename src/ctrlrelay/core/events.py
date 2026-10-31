"""Best-effort event feed for the status page.

The daemon calls :func:`emit_event` at points where something worth showing
happens. A write must not fail the work it describes and may delay it by at
most ``StateDB.EVENT_WRITE_TIMEOUT`` seconds (a contended lock): it goes
through a separate short-timeout connection, and every exception, including
one raised while logging the failure, is swallowed here.

Everything an event carries is built INSIDE that guard: callers pass a
zero-argument callable, never values. An argument is evaluated before
the function it is passed to runs, so a value computed at the call site
(an attribute that is really a raising property, a conversion of
agent-supplied output) would raise in the caller, outside any guard,
and change the work the event was only meant to describe.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ctrlrelay.core.obs import get_logger, log_event

if TYPE_CHECKING:
    from ctrlrelay.core.state import StateDB

_logger = get_logger("core.events")


class EventKind(StrEnum):
    DISPATCHED = "dispatched"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    PR_OPENED = "pr_opened"
    MERGED = "merged"
    CI_STARTED = "ci_started"
    CI_FINISHED = "ci_finished"
    REVIEW_STARTED = "review_started"
    REVIEW_FINISHED = "review_finished"
    MESSAGE_SENT = "message_sent"
    QUESTION_POSTED = "question_posted"
    QUESTION_HELD = "question_held"
    ALERT_SENT = "alert_sent"
    RESTART = "restart"


# What a builder returns: the keyword arguments of
# ``StateDB.record_event_nowait`` (repo, issue_number, session_id,
# detail, ts), or None to record nothing.
EventBuilder = Callable[[], "dict[str, Any] | None"]


def _log_failure(kind_value: str, e: BaseException) -> None:
    try:
        log_event(
            _logger,
            "events.write_failed",
            kind=kind_value,
            reason=type(e).__name__,
        )
    except Exception:  # noqa: BLE001 - a broken log handler included
        pass


def emit_event(
    state_db: StateDB | None,
    kind: EventKind,
    build: EventBuilder | None = None,
) -> None:
    """Record ``kind`` with the fields ``build()`` returns.

    ``build`` runs inside the guard, so nothing it touches can raise into
    the caller. It returning None records nothing (a deliberate skip). No-op
    when ``state_db`` is None. Never raises: a failed build or write is
    logged as ``events.write_failed`` and swallowed, and a failure while
    logging is swallowed too.
    """
    if state_db is None:
        return
    # Set before the try so the failure log never has to touch ``kind``
    # again: if str() itself raises, the log names it as unprintable.
    kind_value = "<unprintable>"
    try:
        kind_value = str(kind)
        fields = {} if build is None else build()
        if fields is None:
            return
        state_db.record_event_nowait(kind_value, **fields)
    except Exception as e:  # noqa: BLE001 - best-effort by design
        _log_failure(kind_value, e)


def not_yet_recorded(
    state_db: StateDB,
    kind: EventKind,
    *,
    repo: str | None,
    session_id: str | None,
    pr_number: int,
) -> bool:
    """Whether no ``kind`` event names this PR for this session yet.

    For use inside a builder. A lookup that fails answers True: the
    choice is between a duplicate row and a missing one, and a feed that
    shows a PR twice is less wrong than one that never shows it."""
    try:
        return not state_db.has_pr_event_nowait(
            str(kind), repo=repo, session_id=session_id, pr_number=pr_number,
        )
    except Exception as e:  # noqa: BLE001 - best-effort by design
        try:
            log_event(
                _logger,
                "events.lookup_failed",
                kind=str(kind),
                reason=type(e).__name__,
            )
        except Exception:  # noqa: BLE001
            pass
        return True


# Session status (as written to ``sessions.status``) -> the event for it.
_OUTCOME_KINDS = {
    "done": EventKind.DONE,
    "blocked": EventKind.BLOCKED,
    "failed": EventKind.FAILED,
}

# Longest question text an outcome event carries.
QUESTION_DETAIL_CHARS = 300


def emit_outcome(
    state_db: StateDB | None,
    build: Callable[[], dict[str, Any]],
) -> None:
    """Record how a pipeline session ended.

    ``build()`` returns ``status`` (the value just written to
    ``sessions.status``: done, blocked or failed), ``pipeline``, ``repo``,
    ``issue_number``, ``session_id`` and optionally ``exit_code`` and
    ``question``. Those last two come from the agent's result, so they are
    kept only when they have the expected type. Never raises, like
    :func:`emit_event`; the builder runs inside the guard."""
    if state_db is None:
        return
    try:
        f = build()
        kind = _OUTCOME_KINDS[f["status"]]
        exit_code = f.get("exit_code")
        question = f.get("question")
        fields = {
            "repo": f.get("repo"),
            "issue_number": f.get("issue_number"),
            "session_id": f.get("session_id"),
            "detail": {
                "pipeline": f["pipeline"],
                "exit_code": (
                    exit_code
                    if isinstance(exit_code, int)
                    and not isinstance(exit_code, bool)
                    else None
                ),
                "question": (
                    question[:QUESTION_DETAIL_CHARS]
                    if isinstance(question, str) and question
                    else None
                ),
            },
        }
    except Exception as e:  # noqa: BLE001 - best-effort by design
        _log_failure("<outcome>", e)
        return
    emit_event(state_db, kind, lambda: fields)
