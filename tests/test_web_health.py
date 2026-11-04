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
    """Health as the **Linux** reader sees it, from the /proc fixture.

    ``platform`` is passed explicitly because memory and uptime now come
    from whichever source the platform has (#321), and these cases are
    about the ``/proc`` one. Before that they relied on ``proc=`` being
    read on every host, which is how a suite full of Linux assumptions ran
    green on two Macs for weeks.
    """
    defaults = {
        "disks": [("home", tmp_path)],
        "proc": kw.pop("proc", None) or _proc(tmp_path),
        "loadavg": lambda: (0.5, 0.25, 0.125),
        "cpu_count": lambda: 12,
        "disk_usage": lambda p: Usage(1000, 600, 400),
        "platform": "linux",
    }
    defaults.update(kw)
    return read_server_health(**defaults)


# ---- the macOS reader (#321) ------------------------------------------------
# Real `vm_stat` output, trimmed, from alkamax (macOS 14.2, arm64). The trailing
# full stop on every count is vm_stat's own.
VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              509239.
Pages active:                           1665080.
Pages inactive:                         1093434.
Pages speculative:                       732215.
Pages throttled:                              0.
Pages wired down:                        121301.
Pages purgeable:                          11246.
"Translation faults":                2549620058.
File-backed pages:                      2853802.
"""
MEMSIZE = "68719476736"
BOOTTIME = "{ sec = 1790576331, usec = 490981 } Sun Sep 27 23:18:51 2026"
# 1790576331 + 3257709s -> the uptime the page showed when this was written.
NOW = 1793834040.0


def _sysctl(**overrides):
    values = {"hw.memsize": MEMSIZE, "kern.boottime": BOOTTIME}
    values.update(overrides)

    def read(name: str) -> str:
        value = values[name]
        if isinstance(value, Exception):
            raise value
        return value

    return read


def _mac(tmp_path: Path, **kw) -> dict:
    defaults = {
        "disks": [("home", tmp_path)],
        "loadavg": lambda: (1.0, 1.0, 1.0),
        "cpu_count": lambda: 10,
        "disk_usage": lambda p: Usage(1000, 600, 400),
        "platform": "darwin",
        "sysctl": _sysctl(),
        "vm_stat": lambda: VM_STAT,
        "now": lambda: NOW,
    }
    defaults.update(kw)
    return read_server_health(**defaults)


def test_macos_memory_and_uptime_come_from_sysctl_and_vm_stat(tmp_path: Path) -> None:
    out = _mac(tmp_path)

    # free + inactive + speculative + purgeable, times the page size in the header.
    pages = 509239 + 1093434 + 732215 + 11246
    assert out["memory"]["total_bytes"] == 68719476736
    assert out["memory"]["available_bytes"] == pages * 16384
    assert out["memory"]["available_pct"] == 55.9
    assert out["memory"]["low"] is False
    assert out["uptime_s"] == int(NOW - 1790576331)
    # The point of the ticket: nothing is missing and nothing is explained away.
    assert out["unavailable"] == []


def test_macos_low_memory_is_flagged_below_ten_percent(tmp_path: Path) -> None:
    # 1 free page of a 16 GiB machine: everything reclaimable is gone.
    one_page = VM_STAT.replace("509239.", "1.").replace("1093434.", "0.")
    one_page = one_page.replace("732215.", "0.").replace("11246.", "0.")
    out = _mac(
        tmp_path, sysctl=_sysctl(**{"hw.memsize": str(16 * 2**30)}), vm_stat=lambda: one_page,
    )

    assert out["memory"]["available_bytes"] == 16384
    assert out["memory"]["low"] is True
    assert out["memory"]["available_pct"] == 0.0


@pytest.mark.parametrize(
    ("name", "broken"),
    [
        ("no page size", "Pages free: 1.\n"),
        (
            "no free line",
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages active: 5.\n",
        ),
        ("a count that is not a number", VM_STAT.replace("509239.", "lots.")),
    ],
)
def test_macos_vm_stat_it_cannot_read_is_unknown_not_a_partial_sum(
    tmp_path: Path, name: str, broken: str,
) -> None:
    """A `vm_stat` missing a line it sums must not report the rest.

    Summing what it recognised would under-report available memory, and the
    panel's whole purpose is to say whether memory is low -- so a partial
    read would be a false alarm rather than a gap.
    """
    out = _mac(tmp_path, vm_stat=lambda: broken)

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS), name
    assert any(
        line.startswith("memory (sysctl hw.memsize, vm_stat):") for line in out["unavailable"]
    )


def test_macos_a_failing_sysctl_is_unknown_with_the_source_named(tmp_path: Path) -> None:
    out = _mac(tmp_path, sysctl=_sysctl(**{
        "hw.memsize": OSError("sysctl: unknown oid"),
        "kern.boottime": OSError("sysctl: unknown oid"),
    }))

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS)
    assert out["uptime_s"] is None
    assert [line.split(":")[0] for line in out["unavailable"]] == [
        "memory (sysctl hw.memsize, vm_stat)", "uptime (sysctl kern.boottime)",
    ]
    # The rest of the panel is still measured.
    assert out["load_1m"] == 1.0 and out["cpu_count"] == 10


@pytest.mark.parametrize("boottime", ["", "{ usec = 1 }", "{ sec = tomorrow }"])
def test_macos_an_unreadable_boot_time_is_unknown(tmp_path: Path, boottime: str) -> None:
    out = _mac(tmp_path, sysctl=_sysctl(**{"kern.boottime": boottime}))

    assert out["uptime_s"] is None
    assert any(line.startswith("uptime (sysctl kern.boottime):") for line in out["unavailable"])


def test_macos_a_boot_time_in_the_future_is_unknown_rather_than_negative(tmp_path: Path) -> None:
    out = _mac(tmp_path, now=lambda: 1790576331.0 - 60)

    assert out["uptime_s"] is None
    assert any("boot time is in the future" in line for line in out["unavailable"])


def test_a_platform_with_neither_source_says_which_two_it_looked_for(tmp_path: Path) -> None:
    out = read_server_health(
        disks=[("home", tmp_path)], loadavg=lambda: (0.1, 0.1, 0.1), cpu_count=lambda: 2,
        disk_usage=lambda p: Usage(1000, 600, 400), platform="sunos5",
    )

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS)
    assert out["uptime_s"] is None
    assert out["unavailable"] == [
        "memory and uptime: 'sunos5' has neither /proc nor sysctl support here"
    ]
    # Everything that does not need a platform is still there.
    assert out["load_1m"] == 0.1 and out["cpu_count"] == 2 and out["disks"][0]["free_pct"] == 40.0


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


def test_a_system_without_getloadavg_imports_and_reports_load_unknown(
    tmp_path: Path, monkeypatch,
) -> None:
    """os.getloadavg does not exist everywhere. Its absence must not break
    the import or the read: load is null, with a reason."""
    import importlib

    import ctrlrelay.web.health as health

    monkeypatch.delattr(os, "getloadavg")
    reloaded = importlib.reload(health)
    try:
        out = reloaded.read_server_health(
            disks=[], proc=_proc(tmp_path), cpu_count=lambda: 4, platform="linux",
        )
    finally:
        monkeypatch.undo()
        importlib.reload(health)

    assert (out["load_1m"], out["load_5m"], out["load_15m"]) == (None, None, None)
    assert any(r.startswith("load average") for r in out["unavailable"])
    # Present case: the rest is still read.
    assert out["cpu_count"] == 4 and out["memory"]["total_bytes"] == 16_000_000 * 1024


def _boom(*_a, **_kw):
    raise RuntimeError("boom")


@pytest.mark.parametrize("broken", ["loadavg", "cpu_count", "disk_usage"])
def test_one_failing_reading_nulls_only_itself_and_never_raises(
    tmp_path: Path, broken: str,
) -> None:
    """Any exception from one reading, not just the expected OSError,
    nulls that value with a reason; the other readings are still made."""
    out = _health(tmp_path, **{broken: _boom})

    joined = " | ".join(out["unavailable"])
    assert "RuntimeError: boom" in joined
    assert len(out["unavailable"]) == 1
    if broken == "loadavg":
        assert out["load_1m"] is None and out["cpu_count"] == 12
    elif broken == "cpu_count":
        assert out["cpu_count"] is None and out["load_1m"] == 0.5
    else:
        assert out["disks"][0]["free_bytes"] is None and out["load_1m"] == 0.5
    assert out["memory"]["total_bytes"] == 16_000_000 * 1024
    assert out["uptime_s"] == 350000


def test_unreadable_proc_files_never_raise(tmp_path: Path) -> None:
    proc = _proc(tmp_path, meminfo="\xff not text", uptime="")
    (proc / "meminfo").write_bytes(b"\xff\xfe")  # not UTF-8

    out = _health(tmp_path, proc=proc)

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS)
    assert out["uptime_s"] is None
    assert out["load_1m"] == 0.5


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the real /proc")
def test_reads_this_machines_real_proc(tmp_path: Path) -> None:
    """The real files, with the real readers: nothing unavailable on Linux."""
    out = read_server_health(disks=[("home", tmp_path)])

    assert out["unavailable"] == []
    assert out["memory"]["total_bytes"] > out["memory"]["available_bytes"] > 0
    assert out["uptime_s"] > 0
    assert out["cpu_count"] == os.cpu_count()
    assert out["disks"][0]["total_bytes"] == shutil.disk_usage(tmp_path).total


@pytest.mark.skipif(sys.platform != "darwin", reason="reads this machine's real sysctl and vm_stat")
def test_reads_this_machines_real_sysctl_and_vm_stat(tmp_path: Path) -> None:
    """The real commands, with the real readers: nothing unavailable on macOS.

    The counterpart of the real-/proc test above, and the one that catches a
    format guessed wrong rather than a fixture copied wrong — which is the
    whole reason #321 existed, since `/proc/meminfo` was never going to be
    there to guess at.
    """
    out = read_server_health(disks=[("home", tmp_path)])

    assert out["unavailable"] == []
    memory = out["memory"]
    # Sane rather than exact: these move between runs.
    assert memory["total_bytes"] > 2 * 2**30
    assert 0 < memory["available_bytes"] <= memory["total_bytes"]
    assert 0.0 < memory["available_pct"] <= 100.0
    assert memory["low"] is (memory["available_bytes"] < LOW_FRACTION * memory["total_bytes"])
    assert out["uptime_s"] is not None and out["uptime_s"] > 0
