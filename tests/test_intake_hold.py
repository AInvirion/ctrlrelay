"""The hold on new work that ``self-update`` sets and the poller obeys (#306)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ctrlrelay.core.intake_hold import IntakeHold, pid_alive


def _pair(tmp_path: Path, now: list[float], alive=lambda _pid: True):
    clock = lambda: now[0]  # noqa: E731
    updater = IntakeHold.beside(tmp_path / "state.db", clock=clock, alive=alive)
    poller = IntakeHold.beside(tmp_path / "state.db", clock=clock, alive=alive)
    return updater, poller


def test_new_work_is_refused_while_held_and_counted_otherwise(tmp_path) -> None:
    now = [1000.0]
    updater, poller = _pair(tmp_path, now)

    assert poller.begin() is None
    assert poller.in_flight == 1
    poller.end()

    updater.place(token="t1", reason="self-update to 9.9", seconds=60)
    refused = poller.begin()
    assert refused is not None and refused.reason == "self-update to 9.9"
    assert poller.in_flight == 0   # a refusal is not counted

    updater.release("t1")
    assert poller.begin() is None


def test_the_ack_says_whether_work_is_in_flight(tmp_path) -> None:
    now = [1000.0]
    updater, poller = _pair(tmp_path, now)
    poller.acknowledge()
    assert not poller.ack_path.exists()   # nothing to acknowledge

    assert poller.begin() is None          # a run already going
    updater.place(token="t1", reason="r", seconds=60)
    poller.acknowledge()
    assert updater.read_ack("t1").busy is True
    poller.end()
    poller.acknowledge()
    assert updater.read_ack("t1").busy is False
    assert updater.read_ack("someone-else") is None


def test_an_expired_hold_is_ignored(tmp_path) -> None:
    now = [1000.0]
    updater, poller = _pair(tmp_path, now)
    updater.place(token="t1", reason="r", seconds=60)
    now[0] += 61
    assert poller.active() is None and poller.begin() is None


def test_a_hold_whose_updater_is_gone_is_ignored(tmp_path) -> None:
    now = [1000.0]
    updater, _ = _pair(tmp_path, now)
    updater.place(token="t1", reason="r", seconds=600)
    poller = IntakeHold.beside(tmp_path / "state.db", clock=lambda: now[0],
                               alive=lambda _pid: False)
    assert poller.active() is None


def test_a_damaged_hold_file_is_no_hold(tmp_path) -> None:
    now = [1000.0]
    updater, poller = _pair(tmp_path, now)
    updater.path.write_text("{not json")
    assert poller.active() is None
    updater.path.write_text(json.dumps({"token": "t", "pid": "1"}))
    assert poller.active() is None


def test_release_leaves_another_updaters_hold(tmp_path) -> None:
    now = [1000.0]
    updater, poller = _pair(tmp_path, now)
    updater.place(token="mine", reason="r", seconds=60)
    updater.release("not-mine")
    assert poller.active() is not None
    updater.release("mine")
    assert not updater.path.exists()


def test_the_real_hold_names_this_process(tmp_path) -> None:
    hold = IntakeHold.beside(tmp_path / "state.db")
    placed = hold.place(token="t", reason="r", seconds=60)
    assert placed.pid == os.getpid() and hold.active() == placed
    assert pid_alive(os.getpid())
    assert not pid_alive(0)


def test_run_acks_acknowledges_on_its_own(tmp_path) -> None:
    now = [1000.0]
    updater, poller = _pair(tmp_path, now)
    updater.place(token="t1", reason="r", seconds=60)

    async def go() -> None:
        task = asyncio.create_task(poller.run_acks(interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(go())
    assert updater.read_ack("t1") is not None
