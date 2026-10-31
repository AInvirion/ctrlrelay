"""Best-effort event feed for the status page.

The daemon calls :func:`emit_event` at points where something worth showing
happens. A write must never change, delay or fail the work it describes, so
every exception is logged and swallowed here.
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
    """Record ``kind`` with ``fields`` (passed to ``StateDB.record_event``).

    No-op when ``state_db`` is None. Any exception is logged as
    ``events.write_failed`` and swallowed.
    """
    if state_db is None:
        return
    try:
        state_db.record_event(str(kind), **fields)
    except Exception as e:  # noqa: BLE001 - best-effort by design
        log_event(
            _logger,
            "events.write_failed",
            kind=str(kind),
            reason=type(e).__name__,
        )
