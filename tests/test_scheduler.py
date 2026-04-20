"""Tests for the APScheduler-backed in-process job scheduler."""

from __future__ import annotations

import asyncio

import pytest
from apscheduler.triggers.cron import CronTrigger

from ctrlrelay.core.scheduler import Scheduler, make_scheduler


class TestCronDowNormalization:
    """Regression for codex [P1]: APScheduler treats numeric DOW as
    Mon=0..Sun=6 and rejects 7; Vixie cron (what users write and what
    our docs describe) treats it as Sun=0..Sat=6 with 7=Sun. The
    scheduler must remap numeric DOWs so ``0 6 * * 1`` really means
    Mondays and ``0 6 * * 7`` doesn't fail at load."""

    def test_numeric_monday_parses_as_monday(self) -> None:
        from ctrlrelay.core.scheduler import _normalize_cron

        # Vixie: 1 = Monday. After normalization, APScheduler sees "mon".
        assert _normalize_cron("0 6 * * 1") == "0 6 * * mon"

    def test_numeric_sunday_zero_parses_as_sunday(self) -> None:
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * 0") == "0 6 * * sun"

    def test_numeric_sunday_seven_is_alias_of_sunday(self) -> None:
        """Vixie allows 7 as an alias for Sunday; APScheduler outright
        rejects 7, so normalization must rewrite it to `sun`."""
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * 7") == "0 6 * * sun"

    def test_dow_range_is_expanded_to_name_list(self) -> None:
        """Numeric ranges are expanded to an explicit name list because
        APScheduler orders weekdays mon..sun — a range like `sun-sat`
        (what naïve remapping of `0-6` would produce) is an inverted
        range under that ordering and gets rejected."""
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * 1-5") == "0 6 * * mon,tue,wed,thu,fri"

    def test_sunday_spanning_range_expands_to_every_day(self) -> None:
        """Regression for codex round-4 [P2]: ``0-6`` must not collapse
        to the invalid ``sun-sat`` inverted-range form. Full-week Vixie
        ranges are valid and must survive normalization."""
        from ctrlrelay.core.scheduler import _normalize_cron

        assert (
            _normalize_cron("0 6 * * 0-6")
            == "0 6 * * sun,mon,tue,wed,thu,fri,sat"
        )

    def test_sunday_spanning_step_range_expands_correctly(self) -> None:
        """Step-form range starting at Sunday: ``0-6/2`` = Sun, Tue, Thu, Sat."""
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * 0-6/2") == "0 6 * * sun,tue,thu,sat"

    def test_numeric_step_range_not_wrapping_sunday_still_works(self) -> None:
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * 1-5/2") == "0 6 * * mon,wed,fri"

    def test_numeric_step_without_range_expands(self) -> None:
        """Regression for codex round-5 [P2]: ``1/2`` was passing through as
        ``mon/2``, but APScheduler reads ``mon/2`` as 'every 2nd Monday',
        not Vixie's 'from Mon, every 2 days' = Mon, Wed, Fri."""
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * 1/2") == "0 6 * * mon,wed,fri"

    def test_wildcard_step_expands_to_every_other_day(self) -> None:
        """``*/2`` in Vixie DOW = Sun, Tue, Thu, Sat. APScheduler's own
        ``*/2`` interpretation differs, so expand explicitly."""
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * */2") == "0 6 * * sun,tue,thu,sat"

    def test_late_week_step_has_only_remaining_days(self) -> None:
        """``5/3`` = from Fri, step 3. Only Fri fits within Sun..Sat."""
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * 5/3") == "0 6 * * fri"

    def test_dow_list_is_remapped(self) -> None:
        from ctrlrelay.core.scheduler import _normalize_cron

        # Mon, Wed, Fri.
        assert _normalize_cron("0 6 * * 1,3,5") == "0 6 * * mon,wed,fri"

    def test_named_dow_range_is_expanded(self) -> None:
        """Named ranges expand to explicit name lists too — APScheduler
        orders mon..sun, so even ``mon-fri`` is unambiguous, but the goal
        is one consistent normalized form regardless of input style."""
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * mon-fri") == "0 6 * * mon,tue,wed,thu,fri"

    def test_sun_fri_named_inverted_range_is_expanded(self) -> None:
        """Regression for codex round-6 [P2]: ``sun-fri`` is valid in
        Vixie (Sun, Mon, Tue, Wed, Thu, Fri) but APScheduler rejects it as
        inverted because its named ordering is ``mon..sun``. Expansion
        produces a list APScheduler accepts regardless."""
        from ctrlrelay.core.scheduler import _normalize_cron

        assert (
            _normalize_cron("0 6 * * sun-fri")
            == "0 6 * * sun,mon,tue,wed,thu,fri"
        )

    def test_singleton_named_token_unchanged(self) -> None:
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * mon") == "0 6 * * mon"

    def test_wildcard_dow_is_unchanged(self) -> None:
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * *") == "0 6 * * *"

    def test_mixed_named_and_numeric_dow_is_fully_remapped(self) -> None:
        """Regression for codex round-3 [P2]: `sun,1` previously escaped
        normalization because the field contained letters; APScheduler then
        read `1` as Tuesday. Every token must be remapped individually."""
        from ctrlrelay.core.scheduler import _normalize_cron

        assert _normalize_cron("0 6 * * sun,1") == "0 6 * * sun,mon"
        assert _normalize_cron("0 6 * * mon,5") == "0 6 * * mon,fri"

    def test_registered_monday_trigger_fires_on_monday_not_tuesday(self) -> None:
        """End-to-end: feed the raw Vixie expression into the scheduler and
        inspect the underlying CronTrigger. The `day_of_week` field must
        resolve to Monday."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        scheduler = make_scheduler(timezone="UTC")

        async def noop() -> None:
            return None

        scheduler.add_cron_job("weekly", "0 6 * * 1", noop)
        trigger = scheduler._impl.get_job("weekly").trigger

        # Pick a known Sunday (2024-01-07) and ask "when's the next fire?"
        # A correct Monday-trigger must advance to Monday 2024-01-08.
        sunday = datetime(2024, 1, 7, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
        next_fire = trigger.get_next_fire_time(None, sunday)
        assert next_fire is not None
        assert next_fire.weekday() == 0, (
            f"expected Monday (weekday=0), got weekday={next_fire.weekday()}"
        )


class TestVixieDomDowOrSemantics:
    """Regression for codex round-8 [P2]: when BOTH DOM and DOW are set,
    Vixie cron fires on EITHER (union); APScheduler's raw
    CronTrigger.from_crontab fires only on the intersection (AND). The
    scheduler must split such expressions into an OrTrigger."""

    def test_dom_and_dow_produces_or_trigger(self) -> None:
        from apscheduler.triggers.combining import OrTrigger

        from ctrlrelay.core.scheduler import _build_vixie_trigger

        trigger = _build_vixie_trigger("0 6 1 * 1", timezone="UTC")
        assert isinstance(trigger, OrTrigger), (
            "DOM+DOW expression must wrap two CronTriggers in OrTrigger "
            "(codex round-8 [P2] regression)"
        )

    def test_dom_only_stays_single_trigger(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        from ctrlrelay.core.scheduler import _build_vixie_trigger

        trigger = _build_vixie_trigger("0 6 15 * *", timezone="UTC")
        assert isinstance(trigger, CronTrigger)

    def test_dow_only_stays_single_trigger(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        from ctrlrelay.core.scheduler import _build_vixie_trigger

        trigger = _build_vixie_trigger("0 6 * * 1", timezone="UTC")
        assert isinstance(trigger, CronTrigger)

    def test_dom_or_dow_fires_on_dow_match_when_dom_wouldnt(self) -> None:
        """Concrete semantic check: ``0 6 1 * 1`` (1st OR Monday). A
        Monday that is NOT the 1st must still fire; APScheduler's AND
        reading would skip it."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from ctrlrelay.core.scheduler import _build_vixie_trigger

        trigger = _build_vixie_trigger("0 6 1 * 1", timezone="UTC")
        # Sunday 2024-01-07 — next fire under Vixie OR should be
        # Monday 2024-01-08 (DOW match). Under AND it would skip until
        # a Monday that also happens to be the 1st.
        start = datetime(2024, 1, 7, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
        next_fire = trigger.get_next_fire_time(None, start)
        assert next_fire is not None
        assert next_fire.date().isoformat() == "2024-01-08", (
            f"expected 2024-01-08 (Monday fire), got {next_fire}"
        )

    def test_dom_or_dow_also_fires_on_dom_match(self) -> None:
        """Concrete semantic check: same expression, pick a start where
        the next 1st-of-month comes before the next Monday."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from ctrlrelay.core.scheduler import _build_vixie_trigger

        trigger = _build_vixie_trigger("0 6 1 * 1", timezone="UTC")
        # Tuesday 2024-01-30 — next Monday is Feb 5. But Feb 1 (a
        # Thursday) comes first via the DOM branch.
        start = datetime(2024, 1, 30, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
        next_fire = trigger.get_next_fire_time(None, start)
        assert next_fire is not None
        assert next_fire.date().isoformat() == "2024-02-01"


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
