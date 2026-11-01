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
    def test_a_few_seconds_either_side_of_the_hour_is_one_window(self) -> None:
        assert window_id(_ts("2026-11-01 05:59:58")) == window_id(_ts("2026-11-01 06:00:03"))
        assert window_id(_ts("2026-11-01 06:00:00")) == "20261101T0600Z"

    def test_the_next_day_is_a_new_window(self) -> None:
        assert window_id(_ts("2026-11-01 06:00:00")) != window_id(_ts("2026-11-02 06:00:00"))


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
        for name in ("secops/20261020T0600Z", "secops/20261030T0600Z",
                     "secops/not-a-window", "other/20261001T0600Z"):
            refs.refs[f"o/r:{REF_PREFIX}/{name}"] = "x"
        await JobClaimer(refs, "node-a", window="20261101T0600Z").claim("o/r", "secops")
        left = sorted(k.split(":", 1)[1] for k in refs.refs)
        assert f"{REF_PREFIX}/secops/20261020T0600Z" not in left  # 12 days
        assert f"{REF_PREFIX}/secops/20261030T0600Z" in left  # 2 days
        assert f"{REF_PREFIX}/secops/not-a-window" in left  # not ours to judge
        assert f"{REF_PREFIX}/other/20261001T0600Z" in left  # another job
        assert KEEP_DAYS == 7


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
    def test_a_run_an_hour_late_names_the_window_it_was_due_in(self, monkeypatch) -> None:
        from datetime import datetime

        import ctrlrelay.cli as cli_mod

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 11, 1, 6, 55, tzinfo=tz)

        monkeypatch.setattr("datetime.datetime", Frozen)
        assert cli_mod.sweep_window("0 6 * * *", "UTC") == "20261101T0600Z"
