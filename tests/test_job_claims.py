"""Scheduled jobs run once across machines (#246).

``Refs`` is one repository's refs and blobs as the GitHub API behaves:
creating a ref that exists is refused, all or nothing. It was checked
against the real API before these tests were written: a second create
answers 422 "Reference already exists".
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime

import pytest

from ctrlrelay.core.job_claims import (
    KEEP_DAYS,
    REF_PREFIX,
    JobClaimer,
    ref_name,
    window_id,
)


class Refs:
    def __init__(self) -> None:
        self.refs: dict[str, str] = {}
        self.blobs: dict[str, str] = {}
        self.fail: Exception | None = None

    async def create_blob(self, repo: str, content: str) -> str:
        if self.fail:
            raise self.fail
        sha = f"blob{len(self.blobs)}"
        self.blobs[sha] = content
        await asyncio.sleep(0)  # let another machine get in between
        return sha

    # Refs belong to a repository, as on GitHub; keyed "repo:ref".
    async def create_ref(self, repo: str, ref: str, sha: str) -> bool:
        if f"{repo}:{ref}" in self.refs:
            return False
        self.refs[f"{repo}:{ref}"] = sha
        return True

    async def get_ref_sha(self, repo: str, ref: str) -> str:
        return self.refs[f"{repo}:{ref}"]

    async def get_blob(self, repo: str, sha: str) -> str:
        return base64.b64encode(self.blobs[sha].encode()).decode()

    async def list_refs(self, repo: str, prefix: str) -> list[str]:
        mine = [k.split(":", 1)[1] for k in self.refs if k.startswith(f"{repo}:")]
        return [r for r in mine if r.startswith(prefix)]

    async def delete_ref(self, repo: str, ref: str) -> None:
        del self.refs[f"{repo}:{ref}"]


def _ts(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()


class TestWindow:
    def test_the_window_is_the_minute_it_was_due(self) -> None:
        assert window_id(_ts("2026-11-01 06:00:00")) == "20261101T0600Z"

    def test_two_fires_a_minute_apart_are_two_windows(self) -> None:
        assert window_id(_ts("2026-11-01 06:00:00")) != window_id(_ts("2026-11-01 06:01:00"))

    def test_late_and_early_starts_name_the_fire_they_belong_to(self) -> None:
        from ctrlrelay.core.scheduler import last_fire_time

        def due(cron: str, at: str) -> str:
            now = datetime.strptime(at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            return last_fire_time(cron, UTC, now).strftime("%m-%d %H:%M")

        # Fired on the minute, or seconds after it: that minute.
        assert due("0 6 * * *", "2026-11-01 06:00:00") == "11-01 06:00"
        assert due("* * * * *", "2026-11-01 06:00:35") == "11-01 06:00"
        # Hourly, asleep through 06:00 and 07:00: the scheduler coalesces
        # them into one run for 07:00, and that is the window named.
        assert due("0 * * * *", "2026-11-01 07:20:00") == "11-01 07:00"
        # Every minute: each fire its own.
        assert due("* * * * *", "2026-11-01 06:01:10") == "11-01 06:01"
        # Weekly: found days back.
        assert due("0 6 * * 1", "2026-11-01 07:00:00") == "10-26 06:00"


class TestClaim:
    @pytest.mark.asyncio
    async def test_two_machines_at_the_same_moment_one_wins(self) -> None:
        refs = Refs()
        a, b = await asyncio.gather(
            JobClaimer(refs, "node-a", window="20261101T0600Z").claim("o/r", "secops"),
            JobClaimer(refs, "node-b", window="20261101T0600Z").claim("o/r", "secops"),
        )
        assert sorted([a.won, b.won]) == [False, True]
        loser = b if a.won else a
        assert loser.owner == ("node-a" if a.won else "node-b")
        assert list(refs.refs) == ["o/r:" + ref_name("secops", "20261101T0600Z")]

    @pytest.mark.asyncio
    async def test_the_next_window_is_free_again(self) -> None:
        refs = Refs()
        a = JobClaimer(refs, "node-a", window="20261101T0600Z")
        assert (await a.claim("o/r", "secops")).won
        # node-a died; tomorrow node-b runs it.
        b = JobClaimer(refs, "node-b", window="20261102T0600Z")
        assert (await b.claim("o/r", "secops")).won

    @pytest.mark.asyncio
    async def test_a_claim_that_cannot_be_made_is_an_error_not_a_win(self) -> None:
        refs = Refs()
        refs.fail = RuntimeError("HTTP 403: Resource not accessible")
        out = await JobClaimer(refs, "node-a", window="20261101T0600Z").claim("o/r", "secops")
        assert not out.won and out.error and "403" in out.error

    @pytest.mark.asyncio
    async def test_old_windows_are_pruned_recent_ones_kept(self) -> None:
        refs = Refs()
        for name in ("secops/20261001T0600Z", "secops/20261030T0600Z",
                     "secops/not-a-window", "other/20261001T0600Z",
                     "secops/nested/20261001T0600Z"):
            refs.refs[f"o/r:{REF_PREFIX}/{name}"] = "x"
        await JobClaimer(refs, "node-a", window="20261101T0600Z").claim("o/r", "secops")
        left = sorted(k.split(":", 1)[1] for k in refs.refs)
        assert f"{REF_PREFIX}/secops/20261001T0600Z" not in left  # 31 days
        assert f"{REF_PREFIX}/secops/20261030T0600Z" in left  # 2 days
        assert f"{REF_PREFIX}/secops/not-a-window" in left  # not ours to judge
        assert f"{REF_PREFIX}/other/20261001T0600Z" in left  # another job
        assert f"{REF_PREFIX}/secops/nested/20261001T0600Z" in left  # not a child
        assert KEEP_DAYS == 30


class TestTheSweep:
    """Two machines, one account, the same schedule, through the real
    ``run_secops_all``: each repo is swept once in the window."""

    @pytest.mark.asyncio
    async def test_each_repo_is_swept_once_across_two_machines(self, tmp_path, monkeypatch) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        import ctrlrelay.pipelines.secops as secops_mod
        from ctrlrelay.core.state import StateDB

        refs = Refs()
        swept: list[tuple[str, str]] = []

        def machine(node: str):
            async def run(self, ctx):
                swept.append((node, ctx.repo))
                return secops_mod.PipelineResult(success=True, session_id=ctx.session_id,
                                                 summary="ok")

            return run

        repos = [SimpleNamespace(name="o/a", automation=None),
                 SimpleNamespace(name="o/b", automation=None)]

        async def sweep(node: str):
            worktree = MagicMock()
            worktree.ensure_bare_repo = AsyncMock()
            worktree.create_worktree = AsyncMock(return_value=tmp_path / node)
            worktree.remove_worktree = AsyncMock()
            (tmp_path / node).mkdir(exist_ok=True)
            db = StateDB(tmp_path / f"{node}.db")
            pipeline_cls = type(f"P{node}", (secops_mod.SecopsPipeline,),
                                {"run": machine(node)})
            monkeypatch.setattr(secops_mod, "SecopsPipeline", pipeline_cls)
            try:
                return await secops_mod.run_secops_all(
                    repos=repos, dispatcher=MagicMock(), github=MagicMock(),
                    worktree=worktree, dashboard=None, state_db=db, transport=None,
                    contexts_dir=tmp_path / "ctx",
                    job_claims=JobClaimer(refs, node, window="20261101T0600Z"),
                )
            finally:
                db.close()

        ra = await sweep("node-a")
        rb = await sweep("node-b")
        assert sorted(r for _, r in swept) == ["o/a", "o/b"]
        assert {n for n, _ in swept} == {"node-a"}
        assert all("swept by node-a" in r.summary for r in rb)
        assert all(r.success for r in ra + rb)

    @pytest.mark.asyncio
    async def test_a_sweep_that_cannot_claim_skips_and_says_why(self, tmp_path) -> None:
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import ctrlrelay.pipelines.secops as secops_mod
        from ctrlrelay.core.state import StateDB

        refs = Refs()
        refs.fail = RuntimeError("HTTP 502")
        db = StateDB(tmp_path / "s.db")
        worktree = MagicMock()
        try:
            [r] = await secops_mod.run_secops_all(
                repos=[SimpleNamespace(name="o/a", automation=None)],
                dispatcher=MagicMock(), github=MagicMock(), worktree=worktree,
                dashboard=None, state_db=db, transport=None, contexts_dir=tmp_path,
                job_claims=JobClaimer(refs, "node-a", window="20261101T0600Z"),
            )
        finally:
            db.close()
        assert not r.success and "claim" in r.error
        worktree.create_worktree.assert_not_called()



class TestALateRunKeepsItsWindow:
    def test_outside_a_scheduled_run_the_window_is_the_last_due_time(
        self, monkeypatch,
    ) -> None:
        from datetime import datetime

        import ctrlrelay.cli as cli_mod

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 11, 1, 6, 55, tzinfo=tz)

        monkeypatch.setattr("datetime.datetime", Frozen)
        assert cli_mod.sweep_window("0 6 * * *", "UTC") == "20261101T0600Z"



class TestRoundOneFixes:
    @pytest.mark.asyncio
    async def test_a_busy_local_lock_claims_nothing(self, tmp_path) -> None:
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import ctrlrelay.pipelines.secops as secops_mod
        from ctrlrelay.core.state import StateDB

        refs = Refs()
        db = StateDB(tmp_path / "s.db")
        db.acquire_lock("o/a", "someone-else")
        try:
            [r] = await secops_mod.run_secops_all(
                repos=[SimpleNamespace(name="o/a", automation=None)],
                dispatcher=MagicMock(), github=MagicMock(), worktree=MagicMock(),
                dashboard=None, state_db=db, transport=None, contexts_dir=tmp_path,
                job_claims=JobClaimer(refs, "node-a", window="20261101T0600Z"),
            )
        finally:
            db.close()
        assert not r.success and "lock" in r.summary.lower()
        assert refs.refs == {}  # the window stays free for another machine

    @pytest.mark.asyncio
    async def test_a_lost_claim_releases_the_local_lock(self, tmp_path) -> None:
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import ctrlrelay.pipelines.secops as secops_mod
        from ctrlrelay.core.state import StateDB

        refs = Refs()
        await JobClaimer(refs, "node-b", window="20261101T0600Z").claim("o/a", "secops")
        db = StateDB(tmp_path / "s.db")
        try:
            await secops_mod.run_secops_all(
                repos=[SimpleNamespace(name="o/a", automation=None)],
                dispatcher=MagicMock(), github=MagicMock(), worktree=MagicMock(),
                dashboard=None, state_db=db, transport=None, contexts_dir=tmp_path,
                job_claims=JobClaimer(refs, "node-a", window="20261101T0600Z"),
            )
            assert db.acquire_lock("o/a", "next")
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_is_an_error_result(self, monkeypatch) -> None:
        import ctrlrelay.core.job_claims as mod

        def boom(*a, **k):
            raise KeyError("surprise")

        # Outside the API calls' own handler: the logging after a win.
        monkeypatch.setattr(mod, "log_event", boom)
        out = await JobClaimer(Refs(), "node-a", window="20261101T0600Z").claim("o/r", "secops")
        assert not out.won and "KeyError" in out.error



class TestTheSchedulerSaysWhichRun:
    @pytest.mark.asyncio
    async def test_the_window_is_the_fire_before_the_planned_next_one_not_the_clock(
        self,
    ) -> None:
        from datetime import timedelta

        from ctrlrelay.core.scheduler import current_due_time, make_scheduler

        seen = []

        async def job() -> None:
            seen.append(current_due_time())

        sched = make_scheduler("UTC")
        sched.add_cron_job(name="every-minute", cron_expr="* * * * *", func=job)
        sched.start()
        try:
            planned = sched._impl.get_job("every-minute")
            this_fire = planned.next_run_time
            # The scheduler submits this fire and moves on to the next ...
            planned.modify(next_run_time=this_fire + timedelta(minutes=1))
            # ... and the run itself starts whenever it starts: the clock is
            # not read at all, so how late does not matter.
            await planned.func()
        finally:
            await sched.shutdown()
        assert seen == [this_fire]
        assert current_due_time() is None  # nothing leaks out of the run


    @pytest.mark.asyncio
    async def test_a_repeated_daylight_saving_hour_names_the_right_fire(self) -> None:
        from zoneinfo import ZoneInfo

        from ctrlrelay.core.scheduler import current_due_time, make_scheduler

        tz = ZoneInfo("America/New_York")
        seen = []

        async def job() -> None:
            seen.append(current_due_time())

        sched = make_scheduler("America/New_York")
        sched.add_cron_job(name="every-minute", cron_expr="* * * * *", func=job)
        sched.start()
        try:
            planned = sched._impl.get_job("every-minute")
            # 01:00 happens twice there on 2026-11-01. The next fire is the
            # second 01:00 (EST); the run before it is the first 01:59 (EDT).
            planned.modify(next_run_time=datetime(2026, 11, 1, 1, 0, tzinfo=tz, fold=1))
            await planned.func()
        finally:
            await sched.shutdown()
        assert seen[0].astimezone(UTC) == datetime(2026, 11, 1, 5, 59, tzinfo=UTC)
