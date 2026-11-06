"""Several agent runs at once on one node (#210).

``agent.max_parallel`` is one cap over every track that starts an agent:
the poll loop's new issues, the resume sweeper and the security sweep.
With it set, a poll's issues start side by side up to the free slots,
never two of one repo at once. Unset, the poll loop takes one issue at a
time, as before.

The poll loop is driven with a fake poller (its batches are given) and a
handler that records when each issue starts and waits until the test lets
it finish, so "started in the same round" is observed, not inferred.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from ctrlrelay.core.config import AgentConfig
from ctrlrelay.core.poller import run_poll_loop
from ctrlrelay.core.slots import AgentSlots


def _item(repo: str, n: int) -> dict[str, Any]:
    return {"repo": repo, "issue": {"number": n, "title": f"#{n}"}}


class _Poller:
    """Gives ``batches`` in order, then empty polls."""

    def __init__(self, *batches: list[dict[str, Any]]) -> None:
        self.batches = list(batches)
        self.unmarked: list[tuple[str, int]] = []

    def unmark_seen(self, repo: str, number: int) -> None:
        self.unmarked.append((repo, number))

    async def poll(self) -> list[dict[str, Any]]:
        return self.batches.pop(0) if self.batches else []

    async def recheck_before_dispatch(self, repo, issue):
        return issue


class _Handler:
    """Records starts and the most runs going at once; each run waits for
    its own release event."""

    def __init__(self) -> None:
        self.started: list[tuple[str, int]] = []
        self.going = 0
        self.most = 0
        self.release: dict[int, asyncio.Event] = {}

    async def __call__(self, repo: str, issue: dict) -> None:
        n = issue["number"]
        self.started.append((repo, n))
        self.going += 1
        self.most = max(self.most, self.going)
        try:
            await self.release.setdefault(n, asyncio.Event()).wait()
        finally:
            self.going -= 1

    def finish(self, *ns: int) -> None:
        for n in ns:
            self.release.setdefault(n, asyncio.Event()).set()

    def finish_all(self) -> None:
        self.finish(*{n for _, n in self.started} | set(self.release))


async def _until(cond, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


_LOOPS: list[asyncio.Task] = []


@pytest_asyncio.fixture(autouse=True)
async def _cancel_leftover_loops():
    """A test that fails part-way leaves its loop and runs waiting; cancel
    them so the failure is reported instead of the suite hanging."""
    yield
    for t in _LOOPS:
        t.cancel()
    if _LOOPS:
        await asyncio.wait(_LOOPS, timeout=5)
    _LOOPS.clear()


def _loop(poller, handler, slots, iterations=1):
    task = asyncio.create_task(run_poll_loop(
        poller, handler, interval=0, max_iterations=iterations, slots=slots,
    ))
    _LOOPS.append(task)
    return task


@pytest.mark.asyncio
async def test_three_issues_in_three_repos_start_in_the_same_round() -> None:
    """The ticket's done-when, with `max_parallel: 3`."""
    handler = _Handler()
    slots = AgentSlots(3)
    poller = _Poller([_item("o/a", 1), _item("o/b", 2), _item("o/c", 3)])
    loop = _loop(poller, handler, slots)

    await _until(lambda: len(handler.started) == 3)
    assert handler.going == 3  # all three running, none finished
    handler.finish_all()
    await asyncio.wait_for(loop, 5)
    assert slots.busy == 0


@pytest.mark.asyncio
async def test_never_more_than_the_cap() -> None:
    handler = _Handler()
    slots = AgentSlots(2)
    poller = _Poller([_item(f"o/r{i}", i) for i in range(1, 6)])
    loop = _loop(poller, handler, slots)

    await _until(lambda: len(handler.started) == 2)
    await asyncio.sleep(0.05)
    assert len(handler.started) == 2
    # Each finished run lets exactly one more start.
    handler.finish(1)
    await _until(lambda: len(handler.started) == 3)
    handler.finish_all()
    while len(handler.started) < 5:
        await asyncio.sleep(0.01)
        handler.finish_all()
    await asyncio.wait_for(loop, 5)
    assert handler.most == 2
    assert slots.busy == 0


@pytest.mark.asyncio
async def test_two_issues_of_one_repo_never_run_together() -> None:
    """The second issue of a repo waits for the first, and an issue of
    another repo behind it in the batch goes first."""
    handler = _Handler()
    slots = AgentSlots(3)
    poller = _Poller([_item("o/a", 1), _item("o/a", 2), _item("o/b", 3)])
    loop = _loop(poller, handler, slots)

    await _until(lambda: len(handler.started) == 2)
    await asyncio.sleep(0.05)
    assert handler.started == [("o/a", 1), ("o/b", 3)]
    handler.finish(1)
    await _until(lambda: len(handler.started) == 3)
    assert handler.started[-1] == ("o/a", 2)
    handler.finish_all()
    await asyncio.wait_for(loop, 5)


@pytest.mark.asyncio
async def test_the_cap_is_shared_with_the_other_tracks() -> None:
    """A slot held elsewhere (a resumed run, the security sweep) is one the
    poll loop cannot use."""
    handler = _Handler()
    slots = AgentSlots(2)
    await slots.acquire()  # e.g. the resume sweeper's run
    poller = _Poller([_item("o/a", 1), _item("o/b", 2)])
    loop = _loop(poller, handler, slots)

    await _until(lambda: len(handler.started) == 1)
    await asyncio.sleep(0.05)
    assert len(handler.started) == 1
    slots.release()
    await _until(lambda: len(handler.started) == 2)
    handler.finish_all()
    await asyncio.wait_for(loop, 5)
    assert slots.busy == 0


@pytest.mark.asyncio
async def test_the_loop_polls_again_while_runs_go_on() -> None:
    """A slow issue does not hold up the next poll: its batch starts while
    the slow one is still running."""
    handler = _Handler()
    slots = AgentSlots(2)
    poller = _Poller([_item("o/a", 1)], [_item("o/b", 2)])
    loop = _loop(poller, handler, slots, iterations=2)

    await _until(lambda: len(handler.started) == 2)
    assert handler.going == 2
    handler.finish_all()
    await asyncio.wait_for(loop, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("slots", [None, AgentSlots(None)])
async def test_without_a_cap_issues_run_one_at_a_time(slots) -> None:
    handler = _Handler()
    poller = _Poller([_item("o/a", 1), _item("o/b", 2)])
    loop = _loop(poller, handler, slots)

    await _until(lambda: len(handler.started) == 1)
    await asyncio.sleep(0.05)
    assert len(handler.started) == 1
    handler.finish(1)
    await _until(lambda: len(handler.started) == 2)
    handler.finish(2)
    await asyncio.wait_for(loop, 5)
    assert handler.most == 1


@pytest.mark.asyncio
async def test_a_failing_handler_gives_its_slot_back() -> None:
    slots = AgentSlots(1)
    calls: list[int] = []

    async def handler(repo, issue):
        calls.append(issue["number"])
        if issue["number"] == 1:
            raise RuntimeError("boom")

    poller = _Poller([_item("o/a", 1), _item("o/b", 2)])
    await asyncio.wait_for(
        run_poll_loop(poller, handler, interval=0, max_iterations=1, slots=slots),
        5,
    )
    assert calls == [1, 2]
    assert slots.busy == 0


@pytest.mark.asyncio
async def test_cancelling_the_loop_cancels_its_runs() -> None:
    handler = _Handler()
    slots = AgentSlots(2)
    poller = _Poller([_item("o/a", 1), _item("o/b", 2)])
    loop = _loop(poller, handler, slots, iterations=None)

    await _until(lambda: handler.going == 2)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(loop, 5)
    assert handler.going == 0
    assert slots.busy == 0


@pytest.mark.asyncio
async def test_a_repo_blocked_issue_does_not_stop_the_next_poll() -> None:
    """A#2 waits behind A#1; the next poll still happens, and C#3 from it
    starts while both A issues are still in play."""
    handler = _Handler()
    slots = AgentSlots(3)
    poller = _Poller([_item("o/a", 1), _item("o/a", 2)], [_item("o/c", 3)])
    loop = _loop(poller, handler, slots, iterations=2)

    await _until(lambda: ("o/c", 3) in handler.started)
    assert handler.started == [("o/a", 1), ("o/c", 3)]
    handler.finish(1)
    await _until(lambda: ("o/a", 2) in handler.started)
    handler.finish_all()
    await asyncio.wait_for(loop, 5)


@pytest.mark.asyncio
async def test_a_full_node_keeps_polling() -> None:
    """Every slot busy: the loop still polls, and queues what it finds."""
    handler = _Handler()
    slots = AgentSlots(1)
    poller = _Poller([_item("o/a", 1)], [_item("o/b", 2)], [_item("o/c", 3)])
    loop = _loop(poller, handler, slots, iterations=3)

    await _until(lambda: not poller.batches)  # all three polls happened
    assert handler.started == [("o/a", 1)]
    handler.finish(1)
    await _until(lambda: len(handler.started) == 2)
    handler.finish_all()
    while len(handler.started) < 3:
        await asyncio.sleep(0.01)
        handler.finish_all()
    await asyncio.wait_for(loop, 5)
    assert handler.started == [("o/a", 1), ("o/b", 2), ("o/c", 3)]


@pytest.mark.asyncio
async def test_stopping_hands_unstarted_issues_back() -> None:
    """Polled issues are marked seen; one that never started must be
    offered again after a restart."""
    handler = _Handler()
    slots = AgentSlots(1)
    poller = _Poller([_item("o/a", 1), _item("o/b", 2), _item("o/c", 3)])
    loop = _loop(poller, handler, slots, iterations=None)

    await _until(lambda: handler.going == 1)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(loop, 5)
    assert sorted(poller.unmarked) == [("o/b", 2), ("o/c", 3)]


@pytest.mark.asyncio
async def test_a_shutdown_starts_nothing_in_the_freed_slots() -> None:
    """Another track waiting for a slot when the loop stops gets
    cancelled, not the slot the loop's cancelled run gives back."""
    handler = _Handler()
    slots = AgentSlots(1)
    poller = _Poller([_item("o/a", 1)])
    loop = _loop(poller, handler, slots, iterations=None)
    await _until(lambda: handler.going == 1)

    started_resume = False

    async def resume_sweeper() -> None:
        nonlocal started_resume
        async with slots.hold():
            started_resume = True

    waiter = asyncio.create_task(resume_sweeper())
    await asyncio.sleep(0.05)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(loop, 5)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, 5)
    assert not started_resume
    with pytest.raises(asyncio.CancelledError):
        await slots.acquire()


def test_try_acquire_does_not_jump_the_queue() -> None:
    async def main() -> None:
        slots = AgentSlots(1)
        assert slots.try_acquire()
        assert not slots.try_acquire()
        waiter = asyncio.create_task(slots.acquire())
        await asyncio.sleep(0)
        slots.release()
        # The slot belongs to the queued waiter, not to a later try.
        assert not slots.try_acquire()
        await asyncio.wait_for(waiter, 5)
        assert slots.busy == 1

    asyncio.run(main())


# --- the security sweep --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_security_sweep_waits_for_a_slot_and_gives_it_back() -> None:
    """Each repo's sweep takes a slot before its lock; with none free it
    waits, and every repo's slot is given back, also on a refused lock."""
    from ctrlrelay.pipelines.secops import run_secops_all

    slots = AgentSlots(1)
    await slots.acquire()  # a dev run holds the only slot
    state_db = MagicMock()
    state_db.acquire_lock.return_value = False  # every repo locked

    repos = [MagicMock(name=f"r{i}") for i in range(3)]
    for i, r in enumerate(repos):
        r.name = f"o/r{i}"
    sweep = asyncio.create_task(run_secops_all(
        repos=repos, dispatcher=AsyncMock(), github=AsyncMock(),
        worktree=AsyncMock(), dashboard=None, state_db=state_db,
        transport=None, contexts_dir=MagicMock(), job_claims=None,
        slots=slots,
    ))
    await asyncio.sleep(0.05)
    assert not sweep.done()
    state_db.acquire_lock.assert_not_called()  # never waits holding a lock

    slots.release()
    results = await asyncio.wait_for(sweep, 5)
    assert len(results) == 3
    assert state_db.acquire_lock.call_count == 3
    assert slots.busy == 0


# --- the setting ---------------------------------------------------------------


def test_max_parallel_is_unset_by_default_and_at_least_one() -> None:
    assert AgentConfig().max_parallel is None
    assert AgentConfig(max_parallel=3).max_parallel == 3
    with pytest.raises(ValueError):
        AgentConfig(max_parallel=0)
    with pytest.raises(ValueError):
        AgentSlots(0)


@pytest.mark.asyncio
async def test_the_security_sweep_skips_repos_once_new_work_is_held() -> None:
    """A hold placed while the sweep waited for a slot is asked again per
    repo: those repos are skipped, one result each, and no lock taken."""
    from ctrlrelay.pipelines.secops import run_secops_all

    slots = AgentSlots(1)
    state_db = MagicMock()
    repos = [MagicMock() for _ in range(2)]
    for i, r in enumerate(repos):
        r.name = f"o/r{i}"
    results = await asyncio.wait_for(run_secops_all(
        repos=repos, dispatcher=AsyncMock(), github=AsyncMock(),
        worktree=AsyncMock(), dashboard=None, state_db=state_db,
        transport=None, contexts_dir=MagicMock(), job_claims=None,
        slots=slots, held=lambda: "self-update to 0.25.0",
    ), 5)
    assert [r.summary for r in results] == [
        "Skipped o/r0: new work is held (self-update to 0.25.0)",
        "Skipped o/r1: new work is held (self-update to 0.25.0)",
    ]
    state_db.acquire_lock.assert_not_called()
    assert slots.busy == 0
