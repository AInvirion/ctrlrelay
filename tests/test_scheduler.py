"""Tests for the APScheduler-backed in-process job scheduler."""

from __future__ import annotations

import asyncio

import pytest
from apscheduler.triggers.cron import CronTrigger

from ctrlrelay.core.scheduler import Scheduler, make_scheduler


class TestMakeScheduler:
    def test_honors_timezone(self) -> None:
        """The scheduler's timezone must match the orchestrator config so
        cron expressions fire in the user's declared TZ, not UTC-by-default."""
        scheduler = make_scheduler(timezone="America/Santiago")
        assert str(scheduler._impl.timezone) == "America/Santiago"


class TestAddCronJob:
    def test_registers_job_with_cron_trigger(self) -> None:
        scheduler = make_scheduler(timezone="UTC")

        async def noop() -> None:
            return None

        scheduler.add_cron_job("secops", "0 6 * * *", noop)
        job = scheduler._impl.get_job("secops")
        assert job is not None
        assert isinstance(job.trigger, CronTrigger)
        # APScheduler's CronTrigger.from_crontab parses field-by-field;
        # verify the hour/minute round-tripped as expected.
        assert str(job.trigger.fields[job.trigger.FIELD_NAMES.index("hour")]) == "6"
        assert str(job.trigger.fields[job.trigger.FIELD_NAMES.index("minute")]) == "0"

    def test_registers_with_coalesce_and_misfire_grace(self) -> None:
        """Misfire policy must be set so a laptop asleep at 6am still runs
        secops on wake (within the grace window) and doesn't replay a dozen
        missed fires at once."""
        scheduler = make_scheduler(timezone="UTC")

        async def noop() -> None:
            return None

        scheduler.add_cron_job("secops", "0 6 * * *", noop)
        job = scheduler._impl.get_job("secops")
        assert job.coalesce is True
        assert job.misfire_grace_time == 3600


class TestJobIsolation:
    @pytest.mark.asyncio
    async def test_job_exception_is_swallowed(self) -> None:
        """An exception raised inside the job function must be logged but
        not re-raised into the scheduler — otherwise one bad run would
        prevent the next scheduled fire from happening."""
        scheduler = make_scheduler(timezone="UTC")

        async def boom() -> None:
            raise RuntimeError("scheduled job blew up")

        scheduler.add_cron_job("boom", "0 6 * * *", boom)
        job = scheduler._impl.get_job("boom")
        # The registered callable is the safe wrapper, not `boom` directly.
        # Calling it must NOT raise.
        await job.func()

    @pytest.mark.asyncio
    async def test_job_cancellation_propagates(self) -> None:
        """CancelledError must escape so poller shutdown can tear the
        scheduler down cleanly; swallowing it would leak running jobs."""
        scheduler = make_scheduler(timezone="UTC")

        async def cancelled() -> None:
            raise asyncio.CancelledError()

        scheduler.add_cron_job("cancelme", "0 6 * * *", cancelled)
        job = scheduler._impl.get_job("cancelme")
        with pytest.raises(asyncio.CancelledError):
            await job.func()


class TestLifecycle:
    def test_shutdown_is_idempotent_before_start(self) -> None:
        """Calling shutdown before start must be a no-op; the poller's
        finally block runs regardless of whether start() ever ran."""
        scheduler = make_scheduler(timezone="UTC")
        # Should not raise — scheduler never started.
        scheduler.shutdown()

    @pytest.mark.asyncio
    async def test_start_then_shutdown_idempotent(self) -> None:
        scheduler = make_scheduler(timezone="UTC")
        scheduler.start()
        scheduler.shutdown()
        # A second shutdown shouldn't blow up.
        scheduler.shutdown()

    def test_scheduler_class_accepts_impl_directly(self) -> None:
        """Scheduler is the narrow wrapper; make_scheduler is the normal
        entrypoint. Keep Scheduler directly constructible so tests can
        inject a pre-configured AsyncIOScheduler if needed."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        impl = AsyncIOScheduler(timezone="UTC")
        scheduler = Scheduler(impl)
        assert scheduler._impl is impl
