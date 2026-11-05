"""One chat message a week with this node's last-7-day numbers (#230).

The numbers are the status page's (``ctrlrelay.web.numbers``), read from
this node's state database when the message is due, so the chat and the
page never disagree. A number that cannot be read is written ``unknown``,
never 0.

The daemon calls :meth:`WeeklySummary.tick` every few minutes. A tick
sends when this week's send time (``schedules.weekly_summary``, in the
configured timezone) has passed and nothing was delivered since it. The
delivery time is written to the state database only after the send
returns, so a restart does not send the week again, and a send that fails
is simply tried again on the next tick. The first tick on a database only
writes the time, so the first summary goes at the next send time. A tick
never raises: the summary
is a report about the work and must not stop it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ctrlrelay.core.config import WEEKDAYS
from ctrlrelay.core.obs import get_logger, log_event
from ctrlrelay.web import numbers as run_numbers

if TYPE_CHECKING:
    from ctrlrelay.core.state import StateDB

_logger = get_logger("core.weekly_summary")

SEND_NAME = "weekly_summary"
DAYS = 7
UNKNOWN = "unknown"


def due_at(now: float, *, day: str, hour: int, timezone: str) -> int:
    """The latest ``day`` at ``hour``:00 in ``timezone`` at or before
    ``now``, as an epoch time."""
    zone = ZoneInfo(timezone)
    local = datetime.fromtimestamp(now, zone)
    back = (local.weekday() - WEEKDAYS.index(day)) % 7
    date = local.date() - timedelta(days=back)
    due = datetime.combine(date, clock_time(hour), tzinfo=zone)
    if due.timestamp() > now:
        due = datetime.combine(date - timedelta(days=7), clock_time(hour), tzinfo=zone)
    return int(due.timestamp())


def read_week(conn: Any, now: int) -> dict[str, Any] | None:
    """The status page's 7-day window, or ``None`` when the runs could not
    be read. A part that could not be read (PR times, reopens) comes back
    ``None`` inside the window, as on the page."""
    since = run_numbers.window_start(now, DAYS)
    try:
        runs = run_numbers.read_runs(conn, since, now)
        came_back = run_numbers.read_came_back(conn, since, now)
    except Exception as e:  # noqa: BLE001 - shown as unknown
        log_event(_logger, "summary.weekly.read_failed", part="sessions",
                  reason=type(e).__name__, error=str(e)[:200])
        return None
    try:
        events = run_numbers.read_pr_times(conn, since, now)
    except Exception as e:  # noqa: BLE001 - shown as unknown
        log_event(_logger, "summary.weekly.read_failed", part="events",
                  reason=type(e).__name__, error=str(e)[:200])
        events = None
    reopens_reason = None
    try:
        reopens = run_numbers.read_reopens(conn, since, now)
    except Exception as e:  # noqa: BLE001 - shown as unknown
        log_event(_logger, "summary.weekly.read_failed", part="issue_finishes",
                  reason=type(e).__name__, error=str(e)[:200])
        reopens = None
        reopens_reason = "the record of finished issues could not be read"
    windows = run_numbers.build_numbers(
        now=now, runs=runs, came_back=came_back, events=events,
        reopens=reopens, reopens_reason=reopens_reason,
    )["windows"]
    return next(w for w in windows if w["days"] == DAYS)


def _count(v: Any) -> str:
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else UNKNOWN


def _duration(seconds: int) -> str:
    m = max(0, int(seconds)) // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 48:
        return f"{h}h {m % 60}m"
    return f"{h // 24}d {h % 24}h"


def _median(m: Any) -> str:
    if not isinstance(m, dict):
        return UNKNOWN
    if m.get("median") is None:
        return "none yet"
    return f"median {_duration(m['median'])} over {_count(m.get('n'))} runs"


def format_summary(node_id: str, window: dict[str, Any] | None, now: int) -> str:
    """The message text. ``window`` is :func:`read_week`'s answer."""
    until = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d %H:%M UTC")
    c: dict[str, Any] = (window or {}).get("overall") or {}
    rate = c.get("success_rate")
    if isinstance(rate, (int, float)) and not isinstance(rate, bool):
        rate_text = f"{round(rate * 100)}%"
    elif isinstance(c.get("started"), int):
        rate_text = "none finished"
    else:
        rate_text = UNKNOWN
    lines = [
        f"Weekly numbers for {node_id}, the 7 days to {until}:",
        f"runs started {_count(c.get('started'))}: done {_count(c.get('done'))}, "
        f"failed {_count(c.get('failed'))}, blocked {_count(c.get('blocked'))}, "
        f"stuck {_count(c.get('stuck'))}",
        f"success rate {rate_text}",
        f"time to PR: {_median(c.get('time_to_pr_s'))}",
        f"time to merge: {_median(c.get('time_to_merge_s'))}",
        f"reopened: {_count(c.get('reopened'))}",
    ]
    if window is None:
        lines.append("(the runs could not be read)")
    else:
        gaps = {k: v for k, v in (window.get("unavailable") or {}).items()
                if k != "cost_per_run"}
        for key, reason in sorted(gaps.items()):
            lines.append(f"{key.removesuffix('_s').replace('_', ' ')} unknown: {reason}")
    return "\n".join(lines)


@dataclass
class WeeklySummary:
    state_db: StateDB
    node_id: str
    day: str
    hour: int
    timezone: str
    # Delivers the text; raises when it could not.
    send: Callable[[str], Awaitable[None]]
    clock: Callable[[], float] = time.time
    # A send that has not returned by then counts as failed.
    send_timeout: float = 60.0

    async def tick(self) -> bool:
        """Send the summary if it is due. True when it was sent. Never
        raises except to pass on a cancellation."""
        try:
            now = int(self.clock())
            due = due_at(now, day=self.day, hour=self.hour, timezone=self.timezone)
            last = self.state_db.last_sent(SEND_NAME)
            if last is None:
                # The first check on this database only notes the time, so
                # installing or upgrading mid-week does not send a summary
                # out of turn; the first one goes at the next send time.
                self.state_db.record_sent(SEND_NAME, now)
                log_event(_logger, "summary.weekly.first_check", next_due=due + 7 * 86_400)
                return False
            if last >= due:
                return False
            text = format_summary(self.node_id, read_week(self.state_db, now), now)
            await asyncio.wait_for(self.send(text), timeout=self.send_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - tried again next tick
            log_event(_logger, "summary.weekly.send_failed",
                      reason=type(e).__name__, error=str(e)[:200],
                      detail="not sent; the next check tries again")
            return False
        try:
            self.state_db.record_sent(SEND_NAME, now)
        except Exception as e:  # noqa: BLE001
            # Sent but not recorded: the next tick sends it again. Said in
            # the log so a repeat is explained.
            log_event(_logger, "summary.weekly.record_failed",
                      reason=type(e).__name__, error=str(e)[:200])
        log_event(_logger, "summary.weekly.sent", due_at=due)
        return True
