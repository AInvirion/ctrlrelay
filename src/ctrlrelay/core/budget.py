"""A token budget for each run (#223).

One run that goes in circles could spend a big part of the weekly quota
before anyone noticed. This module measures what a run has used, live,
and decides when it has used too much. The dispatcher stops the agent
through the same path it uses for a stuck run (#224).

**What is counted.** The agent CLI writes a JSON-lines transcript as it
works (the same files :mod:`ctrlrelay.core.stuck` reads). Every assistant
row carries ``message.usage``::

    {"input_tokens": 2, "cache_creation_input_tokens": 28336,
     "cache_read_input_tokens": 18358, "output_tokens": 132, ...}

One API message is written as several rows (one per content block), each
repeating the same usage, so rows are counted once per ``message.id``.
Sub-agents write their own transcripts under ``<session>/subagents/``;
those are counted too. The budget counts ``input_tokens +
cache_creation_input_tokens + output_tokens``. Cache reads are recorded
but not counted: they are cheap and would dominate every number.

The final stdout ``result`` event may carry ``total_cost_usd``. It is
recorded as information only; nothing is decided on it.

**Unknown is not zero.** A row that cannot be read adds nothing to the
count and is counted as unreadable. The total is then shown as unknown
(``null``), never as a number that silently left something out. The
count of rows that could be read is still a true floor: the run used at
least that much, so the hard limit is checked against it. An unreadable
row on its own therefore never stops a run, and never lets one pass as
having used nothing.

**Per issue session.** A dev run spawns the agent more than once for one
session (the first run, resumes after a question, CI fix rounds). The
budget is for the whole session: each spawn starts from what the session
row already records. Lanes that pass no session scope get the limit per
spawn.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ctrlrelay.core.obs import get_logger, log_event

if TYPE_CHECKING:
    from ctrlrelay.core.config import BudgetConfig
    from ctrlrelay.core.state import StateDB

_logger = get_logger("core.budget")

# Every stop reason for an over-budget run starts with this. The stop
# reason travels in the same field as a stuck run's (one "stopped, and
# why" path), so this is how the chat line and the numbers tell them apart.
BUDGET_REASON_PREFIX = "budget: "
DEFAULT_POLL_SECONDS = 30.0

_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def budget_reason(used: int, hard: int) -> str:
    return f"{BUDGET_REASON_PREFIX}used {used:,} tokens, hard limit {hard:,}"


def is_budget_reason(reason: Any) -> bool:
    return isinstance(reason, str) and reason.startswith(BUDGET_REASON_PREFIX)


# --- what a run used ------------------------------------------------------------


@dataclass(frozen=True)
class RunUsage:
    """Tokens a run used, as far as its transcript could be read."""

    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    # Transcript rows that should have carried usage and could not be read.
    unreadable: int = 0
    # From the agent CLI's own result event; None when it reported none.
    cost_usd: float | None = None

    @property
    def counted(self) -> int:
        """What the budget counts. A floor when ``complete`` is False."""
        return self.input_tokens + self.cache_creation_tokens + self.output_tokens

    @property
    def complete(self) -> bool:
        return self.unreadable == 0

    def plus(self, other: RunUsage) -> RunUsage:
        if self.cost_usd is None:
            cost = other.cost_usd
        elif other.cost_usd is None:
            cost = self.cost_usd
        else:
            cost = self.cost_usd + other.cost_usd
        return RunUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cache_creation_tokens=(
                self.cache_creation_tokens + other.cache_creation_tokens
            ),
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            unreadable=self.unreadable + other.unreadable,
            cost_usd=cost,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "cache_creation_input_tokens": self.cache_creation_tokens,
            "cache_read_input_tokens": self.cache_read_tokens,
            "output_tokens": self.output_tokens,
            "unreadable_rows": self.unreadable,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_json(cls, data: Any) -> RunUsage:
        """Read back what :meth:`to_json` wrote. Raises on anything else."""
        if not isinstance(data, dict):
            raise ValueError("usage detail is not an object")
        values = {}
        for key, name in (
            ("input_tokens", "input_tokens"),
            ("cache_creation_input_tokens", "cache_creation_tokens"),
            ("cache_read_input_tokens", "cache_read_tokens"),
            ("output_tokens", "output_tokens"),
            ("unreadable_rows", "unreadable"),
        ):
            values[name] = _count(data.get(key))
        cost = data.get("cost_usd")
        return cls(**values, cost_usd=_cost(cost) if cost is not None else None)


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"not a token count: {value!r}")
    return value


def _cost(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"not a cost: {value!r}")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"not a cost: {value!r}")
    return value


class UnreadableUsageError(ValueError):
    """A transcript row that should carry usage and could not be read."""


def usage_of_row(row: Any) -> tuple[str | None, dict[str, int]] | None:
    """``(message id, usage)`` of one transcript row, or None when the row
    is not one that carries usage. Raises :class:`UnreadableUsageError` for an
    assistant row whose usage is missing or not four token counts."""
    if not isinstance(row, dict) or row.get("type") != "assistant":
        return None
    message = row.get("message")
    if not isinstance(message, dict):
        raise UnreadableUsageError("assistant row without a message")
    usage = message.get("usage")
    if not isinstance(usage, dict):
        raise UnreadableUsageError("assistant message without usage")
    try:
        counts = {name: _count(usage.get(name)) for name in _FIELDS}
    except ValueError as e:
        raise UnreadableUsageError(str(e)) from e
    msg_id = message.get("id")
    return (msg_id if isinstance(msg_id, str) and msg_id else None), counts


def _epoch(timestamp: Any) -> float | None:
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_result_cost(stdout_text: str) -> float | None:
    """``total_cost_usd`` from the last stream-json ``result`` event, or
    None when there is none or it is not a cost."""
    found: float | None = None
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line.startswith("{") or '"result"' not in line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or event.get("type") != "result":
            continue
        try:
            found = _cost(event.get("total_cost_usd"))
        except ValueError:
            found = None
    return found


class TranscriptMeter:
    """Adds up the usage in a run's transcripts, reading only what is new
    on each call.

    Rows stamped before ``since`` are left out, so a resumed session (whose
    transcript repeats the earlier conversation) is not charged again for
    what an earlier spawn already counted.

    Each file is read from where the last call stopped, so a poll costs
    what was appended since, not the whole transcript. A file that shrank
    or was replaced (another inode) is read again from the start; the
    message ids already seen keep that from counting anything twice.
    :meth:`read` is safe to call from a worker thread, one call at a time
    (a lock makes a second caller wait).
    """

    def __init__(self, folders: Sequence[Path], *, since: float) -> None:
        self.folders = list(folders)
        self.since = since
        self._offsets: dict[Path, int] = {}
        self._inodes: dict[Path, int] = {}
        self._by_message: dict[str, dict[str, int]] = {}
        self._unreadable: set[str] = set()
        self._lock = threading.Lock()
        # Bytes read from disk over every call, for tests and the log.
        self.bytes_read = 0

    def _files(self) -> Iterable[Path]:
        for folder in self.folders:
            try:
                yield from folder.glob("*.jsonl")
                yield from folder.glob("*/subagents/*.jsonl")
            except OSError:
                continue

    def read(self) -> RunUsage:
        """Usage so far. Raises :class:`OSError` only when a file it already
        started reading can no longer be read."""
        with self._lock:
            return self._read_locked()

    def _read_locked(self) -> RunUsage:
        for path in self._files():
            try:
                if not path.is_file() or path.stat().st_mtime < self.since:
                    continue
            except OSError:
                continue
            self._read_new(path)
        totals = dict.fromkeys(_FIELDS, 0)
        for counts in self._by_message.values():
            for name in _FIELDS:
                totals[name] += counts[name]
        return RunUsage(
            input_tokens=totals["input_tokens"],
            cache_creation_tokens=totals["cache_creation_input_tokens"],
            cache_read_tokens=totals["cache_read_input_tokens"],
            output_tokens=totals["output_tokens"],
            unreadable=len(self._unreadable - self._by_message.keys()),
        )

    def _read_new(self, path: Path) -> None:
        start = self._offsets.get(path, 0)
        with open(path, "rb") as fh:
            info = os.fstat(fh.fileno())
            if start and (
                info.st_size < start or self._inodes.get(path) != info.st_ino
            ):
                # Truncated or replaced: read it again from the start. Its
                # unreadable rows are judged again too.
                prefix = f"{path}:"
                self._unreadable = {
                    k for k in self._unreadable if not k.startswith(prefix)
                }
                start = 0
            self._inodes[path] = info.st_ino
            fh.seek(start)
            data = fh.read()
        self.bytes_read += len(data)
        end = data.rfind(b"\n")
        if end < 0:
            return  # no complete row yet
        offset = start
        for raw in data[: end + 1].split(b"\n")[:-1]:
            key_at = f"{path}:{offset}"
            offset += len(raw) + 1
            if not raw.strip():
                continue
            try:
                row = json.loads(raw.decode("utf-8", errors="replace"))
            except (ValueError, RecursionError):
                self._unreadable.add(key_at)
                continue
            if not isinstance(row, dict) or row.get("type") != "assistant":
                continue
            ts = _epoch(row.get("timestamp"))
            if ts is not None and ts < self.since:
                continue
            try:
                if ts is None:
                    raise UnreadableUsageError("assistant row without a timestamp")
                got = usage_of_row(row)
            except UnreadableUsageError:
                self._unreadable.add(key_at)
                continue
            if got is None:
                continue
            msg_id, counts = got
            key = msg_id or key_at
            seen = self._by_message.get(key)
            if seen is None:
                self._by_message[key] = counts
            else:
                # Rows of one message repeat its usage; a partial row
                # written while it streamed can only be smaller.
                self._by_message[key] = {
                    n: max(seen[n], counts[n]) for n in _FIELDS
                }
        self._offsets[path] = start + end + 1


class ExitReadingMeter:
    """The meter of a CLI that reports its usage only in its output (#387).

    It is never polled while the run goes: the dispatcher checks the limit
    once before the round starts, and hands the real reading to
    :meth:`BudgetWatch.finish_with` at exit. :meth:`read` says what it has
    read, which is nothing.
    """

    bytes_read = 0

    def read(self) -> RunUsage:
        return RunUsage()


# --- the limit --------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetLimits:
    soft: int | None = None
    hard: int | None = None
    # Which part of the config gave these, for the log.
    source: str = "agent.budget"

    @property
    def any(self) -> bool:
        return self.soft is not None or self.hard is not None


def resolve_limits(
    config: BudgetConfig | None, labels: Iterable[str] = ()
) -> BudgetLimits:
    """The limits for a run with these labels: the first rule naming any of
    them, else the top-level ones. Labels match without regard to case."""
    if config is None:
        return BudgetLimits()
    have = {str(label).strip().lower() for label in labels}
    for i, rule in enumerate(config.rules):
        if have & {label.strip().lower() for label in rule.labels}:
            soft, hard = config.limits_for(rule)
            return BudgetLimits(soft, hard, source=f"agent.budget.rules[{i}]")
    return BudgetLimits(config.soft_tokens, config.hard_tokens)


def label_names(issue: Any) -> list[str]:
    """The label names of an issue as ``gh issue view --json labels`` gives
    them (``[{"name": ...}]``), or plain strings. Anything else is skipped."""
    raw = issue.get("labels") if isinstance(issue, dict) else None
    out: list[str] = []
    for label in raw if isinstance(raw, list) else []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name:
            out.append(name)
    return out


# --- the session a spawn belongs to ----------------------------------------------

UsageHook = Callable[[RunUsage, BudgetLimits], Awaitable[None]]


@dataclass
class BudgetScope:
    """What a spawn needs to know about the run it belongs to.

    ``before`` is what earlier spawns of the same session used; the budget
    is checked against ``before`` plus this spawn. ``on_usage`` is called
    with that running total whenever it changes and once at the end;
    ``on_soft`` once when the total first passes the soft limit. Both are
    guarded by the caller: a hook that fails never stops a run.
    """

    labels: Sequence[str] = ()
    before: RunUsage = field(default_factory=RunUsage)
    on_usage: UsageHook | None = None
    on_soft: UsageHook | None = None


def session_scope(
    state_db: StateDB,
    session_id: str,
    *,
    labels: Sequence[str] = (),
    transport: Any = None,
    repo: str | None = None,
    issue_number: int | None = None,
) -> BudgetScope:
    """A scope that keeps the usage on the session row and warns in chat.

    What the row already holds is where this spawn starts. A row whose
    detail cannot be read starts from what ``usage_tokens`` says, counted
    as unreadable, so the total shows as unknown rather than restarting
    at zero."""
    before = RunUsage()
    try:
        row = state_db.get_session_row(session_id) or {}
        detail = row.get("usage_detail")
        if detail:
            try:
                before = RunUsage.from_json(json.loads(detail))
            except (ValueError, RecursionError):
                floor = row.get("usage_tokens")
                before = RunUsage(
                    input_tokens=floor if isinstance(floor, int) else 0,
                    unreadable=1,
                )
    except Exception as e:  # noqa: BLE001 - a read must never fail a run
        log_event(
            _logger, "budget.session_read_failed", session_id=session_id,
            error=str(e)[:200],
        )

    async def on_usage(total: RunUsage, limits: BudgetLimits) -> None:
        record_usage(state_db, session_id, total, limits)

    async def on_soft(total: RunUsage, limits: BudgetLimits) -> None:
        if transport is None:
            return
        where = f"#{issue_number} ({repo})" if issue_number else (repo or session_id)
        hard = f"{limits.hard:,}" if limits.hard is not None else "none"
        await transport.send(
            f"💰 {where} has used {total.counted:,} tokens, past its soft "
            f"limit of {limits.soft:,}. Hard limit: {hard}."
        )

    return BudgetScope(
        labels=list(labels), before=before, on_usage=on_usage, on_soft=on_soft,
    )


def record_usage(
    state_db: StateDB, session_id: str, total: RunUsage, limits: BudgetLimits
) -> None:
    """Write the session's usage to its row: ``usage_tokens`` is the counted
    total (a floor when some rows were unreadable), ``usage_detail`` the
    breakdown, the cost and the limits applied, as JSON."""
    detail = {
        **total.to_json(),
        "soft_tokens": limits.soft,
        "hard_tokens": limits.hard,
    }
    state_db.execute(
        "UPDATE sessions SET usage_tokens = ?, usage_detail = ? WHERE id = ?",
        (total.counted, json.dumps(detail), session_id),
    )
    state_db.commit()


# --- the watch --------------------------------------------------------------------


class BudgetWatch:
    """Checks a spawn's running total against its limits.

    :meth:`run` polls until the hard limit is reached and returns the stop
    reason; the dispatcher cancels it when the agent exits. :meth:`check`
    is one poll, also used once after the agent exits so the final
    reading is recorded and a soft limit passed in the last seconds still
    warns.
    """

    def __init__(
        self,
        meter: TranscriptMeter | ExitReadingMeter,
        *,
        limits: BudgetLimits,
        scope: BudgetScope,
        ident: dict[str, Any],
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.meter = meter
        self.limits = limits
        self.scope = scope
        self.ident = ident
        self.poll_seconds = poll_seconds
        before = scope.before.counted
        # Warned once per session: an earlier spawn that passed the soft
        # limit already said so.
        self._warned = limits.soft is not None and before >= limits.soft
        self._last: RunUsage | None = None
        self._logged_unreadable = 0
        self.spawn: RunUsage | None = None

    def total(self, spawn: RunUsage) -> RunUsage:
        return self.scope.before.plus(spawn)

    async def _hook(self, name: str, hook: UsageHook | None, total: RunUsage) -> None:
        if hook is None:
            return
        try:
            await hook(total, self.limits)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - must never stop a run
            _logger.warning(
                "budget.hook_failed",
                extra={
                    **self.ident, "hook": name,
                    "error_type": type(e).__name__, "error": str(e)[:200],
                },
            )

    async def check(self, spawn: RunUsage | None = None) -> str | None:
        """One poll. Returns the stop reason when the hard limit is reached."""
        if spawn is None:
            try:
                # Off the event loop: a first read of a long transcript
                # can take a while.
                spawn = await asyncio.to_thread(self.meter.read)
            except Exception as e:  # noqa: BLE001 - must never stop a run
                _logger.warning(
                    "budget.transcript_unreadable",
                    extra={**self.ident, "error_type": type(e).__name__},
                )
                return None
        self.spawn = spawn
        total = self.total(spawn)
        if total.unreadable > self._logged_unreadable:
            self._logged_unreadable = total.unreadable
            _logger.warning(
                "budget.usage_unreadable",
                extra={
                    **self.ident,
                    "unreadable_rows": total.unreadable,
                    "counted_floor": total.counted,
                },
            )
        if total != self._last:
            self._last = total
            await self._hook("on_usage", self.scope.on_usage, total)
        soft, hard = self.limits.soft, self.limits.hard
        if soft is not None and not self._warned and total.counted >= soft:
            self._warned = True
            log_event(
                _logger, "budget.soft_limit", **self.ident,
                used=total.counted, soft=soft, hard=hard,
                source=self.limits.source,
            )
            await self._hook("on_soft", self.scope.on_soft, total)
        if hard is not None and total.counted >= hard:
            return budget_reason(total.counted, hard)
        return None

    async def run(self) -> str:
        while True:
            reason = await self.check()
            if reason is not None:
                return reason
            await asyncio.sleep(self.poll_seconds)

    async def finish_with(self, spawn: RunUsage) -> RunUsage:
        """The final reading when it comes from the CLI's own output at exit
        (#387) rather than from a transcript. Recorded and checked like any
        other poll; returns ``spawn``. A hard limit passed by this round is
        logged: the round has already ended, so nothing is stopped, and the
        next round of the session is refused before it starts."""
        reason = await self.check(spawn)
        if reason is not None:
            log_event(
                _logger, "budget.hard_limit_passed_at_exit", **self.ident,
                reason=reason, source=self.limits.source,
            )
        return spawn

    async def finish(self, cost_usd: float | None) -> RunUsage | None:
        """The final reading after the agent exited, with the cost its
        result event reported. Returns this spawn's usage, or None when it
        could not be read at all."""
        try:
            spawn = await asyncio.to_thread(self.meter.read)
        except Exception as e:  # noqa: BLE001
            _logger.warning(
                "budget.transcript_unreadable",
                extra={**self.ident, "error_type": type(e).__name__},
            )
            spawn = self.spawn
            if spawn is None:
                return None
        spawn = replace(spawn, cost_usd=cost_usd)
        await self.check(spawn)
        return spawn
