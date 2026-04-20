"""In-process job scheduler for recurring background work.

Wraps APScheduler's AsyncIOScheduler with the project's conventions:

- MemoryJobStore only (cron triggers recompute the next fire time on every
  start, so persistence buys us nothing and adds SQLAlchemy as a runtime
  dep).
- ``coalesce=True`` + ``misfire_grace_time=3600s`` so a laptop that was
  asleep at the fire time still runs the job when it wakes within an hour,
  and multiple missed fires collapse into one run.
- Structured obs logging via ``log_event`` so job lifecycle shows up in
  the same log stream as the poller itself.

Cross-platform: the scheduler runs in the poller's asyncio loop, so macOS
(launchd) and Linux (systemd) behave identically — no per-OS timer unit
is required.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.scheduler")

JobFunc = Callable[[], Awaitable[None]]


# APScheduler's CronTrigger.from_crontab uses Mon=0..Sun=6 for numeric
# day-of-week, and rejects 7 entirely. Vixie cron (the one every reference
# and the orchestrator.yaml docs describe) uses Sun=0..Sat=6 with 7 as an
# alias of Sun. Users writing `0 6 * * 1` expecting Monday would silently
# get Tuesday runs under APScheduler's numbering. Normalize by remapping
# numeric DOW fields to APScheduler's named weekdays before building the
# trigger — names mean the same thing under either numbering scheme.
_VIXIE_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_VIXIE_NAME_TO_NUM = {name: idx for idx, name in enumerate(_VIXIE_DOW_NAMES)}


def _dow_to_vixie_num(tok: str) -> int | None:
    """Parse a DOW token as a Vixie number. Accepts digits 0..7 (with 7 as
    Sunday alias) and the standard three-letter names. Returns ``None`` if
    the token is neither (so callers can leave it for APScheduler to
    error on)."""
    if tok.isdigit():
        n = int(tok)
        if 0 <= n <= 7:
            return 0 if n == 7 else n
        return None
    return _VIXIE_NAME_TO_NUM.get(tok.lower())


def _dow_name(n: int) -> str:
    """Vixie DOW number → APScheduler name. 0 and 7 both = Sunday."""
    return _VIXIE_DOW_NAMES[0 if n == 7 else n]


def _expand_numeric_dow_range(
    start: int, end: int, step: int = 1
) -> list[str] | None:
    """Expand a numeric Vixie-style DOW range to a list of APScheduler names,
    or ``None`` if any endpoint is out of 0..7. Vixie ordering (Sun=0..Sat=6,
    7=Sun alias) is NOT compatible with APScheduler's named-weekday ordering
    (mon..sun), so a range like ``0-6`` cannot be rewritten as ``sun-sat`` —
    APScheduler would reject that as an inverted range. Expand to an
    explicit comma-list instead so the behavior is always well-defined."""
    if not (0 <= start <= 7 and 0 <= end <= 7 and step >= 1):
        return None
    if start > end:
        return None
    return [_dow_name(n) for n in range(start, end + 1, step)]


def _remap_dow_token(tok: str) -> str:
    """Convert a single Vixie DOW token (number, range, step, or name) to
    APScheduler's named-weekday form.

    Every range/step form — numeric OR named — is expanded into an
    explicit comma-separated name list. APScheduler orders weekdays
    ``mon..sun``, so a perfectly valid Vixie expression like ``sun-fri``
    looks inverted to APScheduler and gets rejected; expanding to a name
    list dodges the ordering mismatch. Stepped forms like ``mon/2`` also
    need expansion because APScheduler reads named-with-step as "every
    Nth named-weekday occurrence", not Vixie's "from base, every N days".
    """
    if "/" in tok:
        base, step_str = tok.split("/", 1)
        try:
            step = int(step_str)
        except ValueError:
            return tok
        if step < 1:
            return tok
        # Range-with-step "a-b/s" — endpoints can be numeric or named.
        if "-" in base:
            a, b = base.split("-", 1)
            a_num = _dow_to_vixie_num(a)
            b_num = _dow_to_vixie_num(b)
            if a_num is not None and b_num is not None:
                expanded = _expand_numeric_dow_range(a_num, b_num, step)
                if expanded is not None:
                    return ",".join(expanded)
            return tok
        # Wildcard with step: "*/s" — expand across the full week.
        if base == "*":
            expanded = _expand_numeric_dow_range(0, 6, step)
            return ",".join(expanded) if expanded else tok
        # Single base with step: "n/s" or "mon/s" — Vixie says "from base,
        # every s days until end of week". Expand explicitly.
        base_num = _dow_to_vixie_num(base)
        if base_num is not None:
            expanded = _expand_numeric_dow_range(base_num, 6, step)
            if expanded is not None:
                return ",".join(expanded)
        return tok
    # Range without step "a-b" — endpoints numeric or named.
    if "-" in tok:
        a, b = tok.split("-", 1)
        a_num = _dow_to_vixie_num(a)
        b_num = _dow_to_vixie_num(b)
        if a_num is not None and b_num is not None:
            expanded = _expand_numeric_dow_range(a_num, b_num)
            if expanded is not None:
                return ",".join(expanded)
    # Bare number: "0".."7"
    if tok.isdigit():
        n = int(tok)
        if 0 <= n <= 7:
            return _dow_name(n)
    return tok


def _build_vixie_trigger(cron_expr: str, timezone):
    """Build an APScheduler trigger that honors Vixie cron DOM/DOW OR
    semantics.

    Vixie cron: when BOTH day-of-month and day-of-week are non-wildcard,
    the expression fires when EITHER field matches (union). APScheduler's
    ``CronTrigger.from_crontab`` treats them as AND (intersection), which
    makes ``0 6 1 * mon`` fire only on Mondays that fall on the 1st — a
    much rarer schedule than the user wrote.

    When both fields are set, we split the expression into two triggers
    (``m h DOM mon *`` and ``m h * mon DOW``) wrapped in an ``OrTrigger``
    so APScheduler fires on either match. The rare case where a given
    minute matches both triggers (Aug 1 is a Monday + cron is
    ``0 6 1 8 mon``) produces a single fire because ``OrTrigger``
    coalesces simultaneous sub-trigger hits by fire time.

    If either DOM or DOW is a wildcard (the common case), we take the
    simple path and return a single ``CronTrigger`` — saves a log entry
    and an allocation.
    """
    normalized = _normalize_cron(cron_expr)
    parts = normalized.split()
    if len(parts) == 5:
        m, h, dom, mon, dow = parts
        if dom != "*" and dow != "*":
            dom_only = f"{m} {h} {dom} {mon} *"
            dow_only = f"{m} {h} * {mon} {dow}"
            return OrTrigger([
                CronTrigger.from_crontab(dom_only, timezone=timezone),
                CronTrigger.from_crontab(dow_only, timezone=timezone),
            ])
    return CronTrigger.from_crontab(normalized, timezone=timezone)


def _normalize_cron(expr: str) -> str:
    """Convert a Vixie-style 5-field cron expression to APScheduler syntax.

    Only the day-of-week field is rewritten (numeric → name); the other
    four fields share semantics across both systems. Returns the input
    unchanged if it isn't a 5-field expression so APScheduler's own
    parser can emit the real error message.

    Every DOW token is passed through ``_remap_dow_token`` individually
    so mixed expressions like ``sun,1`` or ``mon,5`` get normalized —
    leaving a bare numeric token in a mostly-named list would let
    APScheduler silently mis-interpret it (their 1 = Tuesday).
    """
    parts = expr.split()
    if len(parts) != 5:
        return expr
    m, h, dom, mon, dow = parts
    new_dow = ",".join(_remap_dow_token(t) for t in dow.split(","))
    return f"{m} {h} {dom} {mon} {new_dow}"


class Scheduler:
    """Thin wrapper so the poller doesn't import APScheduler directly.

    Instances are created with ``make_scheduler``. Lifecycle:

        scheduler = make_scheduler(timezone="America/Santiago")
        scheduler.add_cron_job("secops", "0 6 * * *", my_async_fn)
        scheduler.start()
        ...
        await scheduler.shutdown()  # must be awaited; see below

    The wrapper is intentionally narrow (no pause/resume, no job lookup):
    yagni — we have one caller and one job today.

    ``shutdown`` is async so it can cancel and await in-flight job tasks.
    ``AsyncIOScheduler.shutdown(wait=False)`` only posts the shutdown —
    the loop has to keep running for the pending job coroutines to finish
    their ``finally`` blocks (releasing state-DB locks, closing worktrees).
    Calling ``wait=True`` synchronously from inside the loop would
    deadlock because the jobs need the same loop to complete.
    """

    def __init__(self, impl: AsyncIOScheduler) -> None:
        self._impl = impl
        self._started = False
        self._running_jobs: set[asyncio.Task[None]] = set()

    def add_cron_job(
        self,
        name: str,
        cron_expr: str,
        func: JobFunc,
        *,
        misfire_grace_time: int = 3600,
        coalesce: bool = True,
    ) -> None:
        """Register an async function to fire on a cron schedule.

        Wraps ``func`` so exceptions are logged but don't poison the
        scheduler — the next fire should still go through. This matches how
        the poll loop isolates per-repo failures. Also tracks the running
        task so ``shutdown`` can cancel and await it cleanly.
        """
        trigger = _build_vixie_trigger(cron_expr, timezone=self._impl.timezone)

        async def _safe_job() -> None:
            task = asyncio.current_task()
            if task is not None:
                self._running_jobs.add(task)
            log_event(_logger, "scheduler.job.start", job=name, cron=cron_expr)
            try:
                await func()
                log_event(_logger, "scheduler.job.done", job=name)
            except asyncio.CancelledError:
                log_event(_logger, "scheduler.job.cancelled", job=name)
                raise
            except Exception as e:
                log_event(
                    _logger,
                    "scheduler.job.failed",
                    job=name,
                    error_type=type(e).__name__,
                    error=str(e)[:200],
                )
            finally:
                if task is not None:
                    self._running_jobs.discard(task)

        self._impl.add_job(
            _safe_job,
            trigger=trigger,
            id=name,
            name=name,
            misfire_grace_time=misfire_grace_time,
            coalesce=coalesce,
            replace_existing=True,
        )
        log_event(
            _logger,
            "scheduler.job.registered",
            job=name,
            cron=cron_expr,
            timezone=str(self._impl.timezone),
        )

    def start(self) -> None:
        self._impl.start()
        self._started = True
        log_event(_logger, "scheduler.started")

    async def shutdown(self, *, cancel_timeout: float = 150.0) -> None:
        """Stop the scheduler and await in-flight jobs to finalize.

        1. Signals APScheduler to stop accepting new fires.
        2. Cancels any currently running job tasks so their ``finally``
           blocks run (release DB locks, close transports).
        3. Awaits those tasks up to ``cancel_timeout`` seconds so the
           poller's ``loop.close()`` doesn't land mid-cleanup.

        ``cancel_timeout`` defaults to 150s — comfortably above the
        ``WorktreeManager._run_git`` 120s ceiling so a scheduled secops
        sweep that's mid ``git worktree prune`` when SIGTERM arrives
        gets a real chance to finish cleanup before ``loop.close()``
        terminates everything. If your launchd plist /
        systemd unit imposes a stricter ``ExitTimeOut`` /
        ``TimeoutStopSec``, raise that limit too — the scheduler can
        only keep the loop alive within the supervisor's kill window.

        Calling shutdown before ``start`` is a no-op.
        """
        if not self._started:
            return
        self._impl.shutdown(wait=False)
        self._started = False

        if self._running_jobs:
            in_flight = list(self._running_jobs)
            for task in in_flight:
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*in_flight, return_exceptions=True),
                    timeout=cancel_timeout,
                )
            except asyncio.TimeoutError:
                log_event(
                    _logger,
                    "scheduler.shutdown.jobs_timed_out",
                    count=len(in_flight),
                    timeout=cancel_timeout,
                )
        log_event(_logger, "scheduler.shutdown")


def make_scheduler(timezone: str) -> Scheduler:
    """Build a Scheduler configured for the orchestrator's timezone.

    Uses MemoryJobStore implicitly (APScheduler's default). The caller owns
    the lifecycle — call ``start()`` after your asyncio loop is up and
    ``shutdown()`` in a ``finally`` block alongside other teardown.
    """
    impl = AsyncIOScheduler(timezone=timezone)
    return Scheduler(impl)
