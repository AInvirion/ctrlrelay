"""Tests for the status page's server health (#232).

The /proc files here are copies of the real files' format (``/proc/meminfo``
lines are ``Name:<spaces>value kB``; ``/proc/uptime`` is two floats). One
test also reads this machine's real /proc, so a format we guessed wrong
fails here rather than on the page.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from ctrlrelay.web.health import LOW_FRACTION, read_server_health

KEYS = {"load_1m", "load_5m", "load_15m", "cpu_count", "memory", "disks",
        "uptime_s", "unavailable"}
MEMORY_KEYS = {"total_bytes", "available_bytes", "available_pct", "low"}
DISK_KEYS = {"name", "path", "total_bytes", "free_bytes", "free_pct", "low"}

Usage = namedtuple("Usage", "total used free")

MEMINFO = """\
MemTotal:       16000000 kB
MemFree:         3000000 kB
MemAvailable:    8000000 kB
Buffers:          100000 kB
"""


def _proc(tmp_path: Path, *, meminfo: str | None = MEMINFO,
          uptime: str | None = "350000.25 4000000.00\n") -> Path:
    proc = tmp_path / "proc"
    proc.mkdir()
    if meminfo is not None:
        (proc / "meminfo").write_text(meminfo)
    if uptime is not None:
        (proc / "uptime").write_text(uptime)
    return proc


def _health(tmp_path: Path, **kw) -> dict:
    defaults = {
        "disks": [("home", tmp_path)],
        "proc": kw.pop("proc", None) or _proc(tmp_path),
        "loadavg": lambda: (0.5, 0.25, 0.125),
        "cpu_count": lambda: 12,
        "disk_usage": lambda p: Usage(1000, 600, 400),
    }
    defaults.update(kw)
    return read_server_health(**defaults)


def test_every_number_from_its_source(tmp_path: Path) -> None:
    out = _health(tmp_path)

    assert set(out) == KEYS
    assert (out["load_1m"], out["load_5m"], out["load_15m"]) == (0.5, 0.25, 0.125)
    assert out["cpu_count"] == 12
    assert out["memory"] == {
        "total_bytes": 16_000_000 * 1024,
        "available_bytes": 8_000_000 * 1024,
        "available_pct": 50.0,
        "low": False,
    }
    assert out["disks"] == [{
        "name": "home", "path": str(tmp_path), "total_bytes": 1000,
        "free_bytes": 400, "free_pct": 40.0, "low": False,
    }]
    assert out["uptime_s"] == 350000
    assert out["unavailable"] == []


def test_low_memory_and_low_disk_are_flagged_below_ten_percent(tmp_path: Path) -> None:
    assert LOW_FRACTION == 0.10
    low_mem = MEMINFO.replace("MemAvailable:    8000000", "MemAvailable:    1599999")
    edge_mem = MEMINFO.replace("MemAvailable:    8000000", "MemAvailable:    1600000")

    low = _health(tmp_path / "a", proc=_proc(_mk(tmp_path / "a"), meminfo=low_mem),
                  disk_usage=lambda p: Usage(1000, 901, 99))
    edge = _health(tmp_path / "b", proc=_proc(_mk(tmp_path / "b"), meminfo=edge_mem),
                   disk_usage=lambda p: Usage(1000, 900, 100))

    assert low["memory"]["low"] is True
    assert low["disks"][0]["low"] is True
    # Exactly 10% is not below 10%.
    assert edge["memory"]["low"] is False
    assert edge["disks"][0]["low"] is False


def _mk(path: Path) -> Path:
    path.mkdir()
    return path


def test_no_meminfo_is_null_with_a_reason_not_zero(tmp_path: Path) -> None:
    """Off Linux there is no /proc/meminfo: memory is unknown, never 0
    (which would read as "out of memory") and never omitted."""
    out = _health(tmp_path, proc=_proc(tmp_path, meminfo=None, uptime=None))

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS)
    assert out["uptime_s"] is None
    joined = " | ".join(out["unavailable"])
    assert "memory" in joined and "uptime" in joined
    # Present case: the rest is still measured.
    assert out["load_1m"] == 0.5 and out["disks"][0]["free_bytes"] == 400


@pytest.mark.parametrize("meminfo", [
    "MemTotal:       16000000 kB\n",                           # no MemAvailable
    "MemTotal:       16000000 MB\nMemAvailable: 1 MB\n",       # a unit we do not know
    "MemTotal:       lots kB\nMemAvailable:    8000000 kB\n",  # not a number
    "MemTotal:       0 kB\nMemAvailable:    0 kB\n",           # no total to divide by
])
def test_meminfo_that_cannot_be_read_exactly_is_unknown(tmp_path: Path, meminfo: str) -> None:
    out = _health(tmp_path, proc=_proc(tmp_path, meminfo=meminfo))

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS)
    assert any(r.startswith("memory") for r in out["unavailable"])


def test_a_missing_disk_path_is_unknown_and_the_others_still_measured(tmp_path: Path) -> None:
    gone = tmp_path / "not-there"
    out = read_server_health(
        disks=[("home", tmp_path), ("worktrees", gone)],
        proc=_proc(tmp_path),
        loadavg=lambda: (1.0, 1.0, 1.0),
        cpu_count=lambda: 4,
        disk_usage=shutil.disk_usage,
    )

    home, worktrees = out["disks"]
    assert home["name"] == "home" and isinstance(home["total_bytes"], int)
    assert worktrees == {
        "name": "worktrees", "path": str(gone), "total_bytes": None,
        "free_bytes": None, "free_pct": None, "low": None,
    }
    assert any(r.startswith("disk worktrees") for r in out["unavailable"])


def test_no_load_average_and_no_cpu_count_are_unknown(tmp_path: Path) -> None:
    def no_load():
        raise OSError("Load averages are unobtainable")

    out = _health(tmp_path, loadavg=no_load, cpu_count=lambda: None)

    assert (out["load_1m"], out["load_5m"], out["load_15m"]) == (None, None, None)
    assert out["cpu_count"] is None
    joined = " | ".join(out["unavailable"])
    assert "load" in joined and "cpu count" in joined


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the real /proc")
def test_reads_this_machines_real_proc(tmp_path: Path) -> None:
    """The real files, with the real readers: nothing unavailable on Linux."""
    out = read_server_health(disks=[("home", tmp_path)])

    assert out["unavailable"] == []
    assert out["memory"]["total_bytes"] > out["memory"]["available_bytes"] > 0
    assert out["uptime_s"] > 0
    assert out["cpu_count"] == os.cpu_count()
    assert out["disks"][0]["total_bytes"] == shutil.disk_usage(tmp_path).total
