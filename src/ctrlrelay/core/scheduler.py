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


class Scheduler:
    """Thin wrapper so the poller doesn't import APScheduler directly.

    Instances are created with ``make_scheduler``. Lifecycle:

        scheduler = make_scheduler(timezone="America/Santiago")
        scheduler.add_cron_job("secops", "0 6 * * *", my_async_fn)
        scheduler.start()
        ...
        scheduler.shutdown()

    The wrapper is intentionally narrow (no pause/resume, no job lookup):
    yagni — we have one caller and one job today.
    """

    def __init__(self, impl: AsyncIOScheduler) -> None:
        self._impl = impl
        self._started = False

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
        the poll loop isolates per-repo failures.
        """
        trigger = CronTrigger.from_crontab(cron_expr, timezone=self._impl.timezone)

        async def _safe_job() -> None:
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

    def shutdown(self, *, wait: bool = False) -> None:
        """Stop firing jobs. ``wait=False`` (the default) returns immediately
        even if a job is mid-run; the running coroutine is left to finish on
        its own task. The poller's shutdown path cancels the main task,
        which transitively cancels in-flight jobs — waiting here would risk
        deadlocking on a job that's blocked on a subprocess."""
        if not self._started:
            return
        self._impl.shutdown(wait=wait)
        self._started = False
        log_event(_logger, "scheduler.shutdown")


def make_scheduler(timezone: str) -> Scheduler:
    """Build a Scheduler configured for the orchestrator's timezone.

    Uses MemoryJobStore implicitly (APScheduler's default). The caller owns
    the lifecycle — call ``start()`` after your asyncio loop is up and
    ``shutdown()`` in a ``finally`` block alongside other teardown.
    """
    impl = AsyncIOScheduler(timezone=timezone)
    return Scheduler(impl)
