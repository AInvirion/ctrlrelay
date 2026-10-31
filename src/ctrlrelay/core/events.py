"""Best-effort event feed for the status page.

The daemon calls :func:`emit_event` at points where something worth showing
happens. A write must not fail the work it describes and may delay it by at
most ``StateDB.EVENT_WRITE_TIMEOUT`` seconds (a contended lock): it goes
through a separate short-timeout connection, and every exception, including
one raised while logging the failure, is swallowed here.
"""

from __future__ import annotations

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


def emit_event(state_db: StateDB | None, kind: EventKind, **fields: Any) -> None:
    """Record ``kind`` with ``fields`` (as for ``StateDB.record_event``).

    No-op when ``state_db`` is None. Never raises: a failed write is logged
    as ``events.write_failed`` and swallowed, and a failure while logging
    is swallowed too.
    """
    if state_db is None:
        return
    # Set before the try so the failure log never has to touch ``kind``
    # again: if str() itself raises, the log names it as unprintable.
    kind_value = "<unprintable>"
    try:
        kind_value = str(kind)
        state_db.record_event_nowait(kind_value, **fields)
    except Exception as e:  # noqa: BLE001 - best-effort by design
        try:
            log_event(
                _logger,
                "events.write_failed",
                kind=kind_value,
                reason=type(e).__name__,
            )
        except Exception:  # noqa: BLE001 - a broken log handler included
            pass


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
    status: str,
    *,
    pipeline: str,
    repo: str | None,
    issue_number: int | None,
    session_id: str | None,
    exit_code: Any = None,
    question: Any = None,
) -> None:
    """Record how a pipeline session ended: ``status`` is the value just
    written to ``sessions.status`` (``done``, ``blocked`` or ``failed``).

    ``exit_code`` and ``question`` come from the agent's result, so they
    are kept only when they have the expected type. Never raises, like
    :func:`emit_event`; building the detail is inside the guard too."""
    if state_db is None:
        return
    try:
        kind = _OUTCOME_KINDS[status]
        detail = {
            "pipeline": pipeline,
            "exit_code": (
                exit_code
                if isinstance(exit_code, int) and not isinstance(exit_code, bool)
                else None
            ),
            "question": (
                question[:QUESTION_DETAIL_CHARS]
                if isinstance(question, str) and question
                else None
            ),
        }
    except Exception as e:  # noqa: BLE001 - best-effort by design
        try:
            log_event(
                _logger,
                "events.write_failed",
                kind=f"outcome:{status!r:.40}",
                reason=type(e).__name__,
            )
        except Exception:  # noqa: BLE001
            pass
        return
    emit_event(
        state_db,
        kind,
        repo=repo,
        issue_number=issue_number,
        session_id=session_id,
        detail=detail,
    )
