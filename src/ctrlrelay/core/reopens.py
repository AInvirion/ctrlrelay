"""Noticing that an issue the relay finished was reopened (#230).

The relay finishes an issue when it sees the issue's PR merge; its
post-merge pipeline then closes the issue (``issue_finishes`` in the state
database). A reopen is looked for in what the poller already fetched: each
poll's search returns the open issues assigned to the operator or wearing a
trigger label. An issue in that answer that the relay had finished is
either reopened, or was never closed at all (the close failed, or GitHub's
search has not caught up with it yet). Only GitHub's own ``reopened`` event
tells these apart, so that one issue's events are read, and the reopen is
recorded only when one is dated after the finish.

So an ordinary poll costs one database read and no GitHub call. A call is
made only for a finished issue that shows up open, and at most once an
hour per issue while it stays open without a reopen. A reopened issue the
search does not return (no longer assigned to the operator and without a
trigger label) is not noticed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ctrlrelay.core.events import EventKind, emit_event
from ctrlrelay.core.obs import get_logger, log_event

if TYPE_CHECKING:
    from ctrlrelay.core.state import StateDB

_logger = get_logger("core.reopens")

# How long an issue found open with no reopen after its finish is left
# before its events are read again.
RECHECK_SECONDS = 3600


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


@dataclass
class ReopenTracker:
    state_db: StateDB
    github: Any
    clock: Callable[[], float] = time.time
    recheck_seconds: int = RECHECK_SECONDS
    # (repo, issue) -> (finished_at, when its events were last read). In
    # memory only: after a restart such an issue is read once more.
    _checked: dict[tuple[str, int], tuple[int, float]] = field(
        default_factory=dict, repr=False,
    )

    async def check(self, open_issues: Iterable[tuple[str, int]]) -> int:
        """Record a reopen for each finished issue in ``open_issues`` that
        GitHub says was reopened after the relay finished it. Returns how
        many were recorded. Never raises except to pass on a cancellation:
        a failure is logged and the issue is looked at again next poll."""
        try:
            finished = self.state_db.list_open_finishes()
        except Exception as e:  # noqa: BLE001 - the poll must go on
            log_event(_logger, "reopen.read_failed",
                      reason=type(e).__name__, error=str(e)[:200])
            return 0
        recorded = 0
        for key in sorted(set(open_issues)):
            finished_at = finished.get(key)
            if finished_at is None:
                continue
            last = self._checked.get(key)
            now = self.clock()
            if (
                last is not None and last[0] == finished_at
                and now - last[1] < self.recheck_seconds
            ):
                continue
            repo, number = key
            try:
                times = await self.github.list_reopen_times(repo, number)
                after = sorted(t for t in map(_epoch, times) if t > finished_at)
                if not after:
                    self._checked[key] = (finished_at, now)
                    continue
                if self.state_db.record_reopen(repo, number, finished_at, after[0]):
                    recorded += 1
                    self._record_event(repo, number, finished_at, after[0])
                self._checked.pop(key, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - next poll tries again
                log_event(_logger, "reopen.check_failed", repo=repo,
                          issue_number=number, reason=type(e).__name__,
                          error=str(e)[:200])
        return recorded

    def _record_event(
        self, repo: str, number: int, finished_at: int, reopened_at: int,
    ) -> None:
        log_event(_logger, "reopen.recorded", repo=repo, issue_number=number,
                  finished_at=finished_at, reopened_at=reopened_at)
        emit_event(self.state_db, EventKind.REOPENED, lambda: {
            "repo": repo,
            "issue_number": number,
            "detail": {"finished_at": finished_at, "reopened_at": reopened_at},
        })
