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
    @pytest.mark.asyncio
    async def test_shutdown_is_idempotent_before_start(self) -> None:
        """Calling shutdown before start must be a no-op; the poller's
        finally block runs regardless of whether start() ever ran."""
        scheduler = make_scheduler(timezone="UTC")
        # Should not raise — scheduler never started.
        await scheduler.shutdown()

    @pytest.mark.asyncio
    async def test_start_then_shutdown_idempotent(self) -> None:
        scheduler = make_scheduler(timezone="UTC")
        scheduler.start()
        await scheduler.shutdown()
        # A second shutdown shouldn't blow up.
        await scheduler.shutdown()

    def test_scheduler_class_accepts_impl_directly(self) -> None:
        """Scheduler is the narrow wrapper; make_scheduler is the normal
        entrypoint. Keep Scheduler directly constructible so tests can
        inject a pre-configured AsyncIOScheduler if needed."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        impl = AsyncIOScheduler(timezone="UTC")
        scheduler = Scheduler(impl)
        assert scheduler._impl is impl


class TestShutdownAwaitsInflightJobs:
    """Regression for codex [P1]: ``scheduler.shutdown`` must cancel and
    await any running job tasks so their ``finally`` blocks get to run.
    Without this, a scheduled secops sweep cancelled mid-run would leave
    state-DB locks held and worktrees dirty, wedging subsequent runs."""

    @pytest.mark.asyncio
    async def test_shutdown_cancels_and_awaits_running_job(self) -> None:
        scheduler = make_scheduler(timezone="UTC")
        scheduler.start()

        job_cancelled = asyncio.Event()
        cleanup_ran = asyncio.Event()
        job_started = asyncio.Event()

        async def long_running() -> None:
            job_started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                job_cancelled.set()
                # Simulate real cleanup work (close transport, release lock)
                await asyncio.sleep(0)
                cleanup_ran.set()
                raise

        scheduler.add_cron_job("longjob", "0 6 * * *", long_running)

        # Manually start the job via its registered callable (don't wait
        # for 6am). APScheduler fires safe-wrapped functions as tasks, so
        # use the same mechanism.
        job = scheduler._impl.get_job("longjob")
        task = asyncio.create_task(job.func())
        scheduler._running_jobs.add(task)
        task.add_done_callback(scheduler._running_jobs.discard)

        await job_started.wait()
        await scheduler.shutdown()

        assert job_cancelled.is_set(), (
            "shutdown must cancel the running job"
        )
        assert cleanup_ran.is_set(), (
            "shutdown must await the job's cleanup before returning "
            "(codex [P1] regression)"
        )

    @pytest.mark.asyncio
    async def test_shutdown_times_out_on_stuck_job(self) -> None:
        """A job that ignores cancellation must not hang shutdown forever —
        the timeout lets the poller tear down anyway."""
        scheduler = make_scheduler(timezone="UTC")
        scheduler.start()

        async def stubborn() -> None:
            while True:
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    # Eat the cancel and keep going — simulates a job that
                    # mishandles cancellation.
                    continue

        scheduler.add_cron_job("stubborn", "0 6 * * *", stubborn)
        job = scheduler._impl.get_job("stubborn")
        task = asyncio.create_task(job.func())
        scheduler._running_jobs.add(task)

        # Short timeout so the test is fast.
        await scheduler.shutdown(cancel_timeout=0.1)
        # Task still running, but shutdown returned — that's the contract.
        # Clean up the leaked task so the test runner doesn't complain.
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):  # noqa: BLE001
            pass
