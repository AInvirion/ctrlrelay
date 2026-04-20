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


def _remap_dow_token(tok: str) -> str:
    """Convert a single numeric Vixie DOW token (or a range/step/name) to
    APScheduler's named-weekday form. Leaves non-numeric tokens alone."""
    if "/" in tok:
        base, step = tok.split("/", 1)
        return f"{_remap_dow_token(base)}/{step}"
    if "-" in tok:
        a, b = tok.split("-", 1)
        return f"{_remap_dow_token(a)}-{_remap_dow_token(b)}"
    if tok.isdigit():
        n = int(tok)
        if 0 <= n <= 7:
            # 0 and 7 both = Sunday in Vixie cron.
            return _VIXIE_DOW_NAMES[0 if n == 7 else n]
    return tok


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
        trigger = CronTrigger.from_crontab(
            _normalize_cron(cron_expr), timezone=self._impl.timezone,
        )

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

    async def shutdown(self, *, cancel_timeout: float = 30.0) -> None:
        """Stop the scheduler and await in-flight jobs to finalize.

        1. Signals APScheduler to stop accepting new fires.
        2. Cancels any currently running job tasks so their ``finally``
           blocks run (release DB locks, close transports).
        3. Awaits those tasks up to ``cancel_timeout`` seconds so the
           poller's ``loop.close()`` doesn't land mid-cleanup.

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
