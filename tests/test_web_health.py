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

    # free + inactive, times the page size in the header (see the module docstring
    # for why speculative and purgeable are not added).
    pages = 509239 + 1093434
    assert out["memory"]["total_bytes"] == 68719476736
    assert out["memory"]["available_bytes"] == pages * 16384
    assert out["memory"]["available_pct"] == round(100.0 * pages * 16384 / 68719476736, 1)
    assert out["memory"]["low"] is False
    # The boot instant is sec AND usec, so this is a second less than the
    # seconds field alone would give -- which is the point of counting usec.
    assert out["uptime_s"] == int(NOW - (1790576331 + 490981 / 1_000_000))
    assert out["uptime_s"] == 3257708
    # The point of the ticket: nothing is missing and nothing is explained away.
    assert out["unavailable"] == []


def _vm_stat_for(total_bytes: int, available_bytes: int) -> str:
    """A physically consistent `vm_stat` for a machine of ``total_bytes``
    with ``available_bytes`` free+inactive, the rest active.

    Built rather than edited by hand because the first version of the
    low-memory fixture kept 1,665,080 active pages while declaring a 16 GiB
    total -- more active memory than the machine had, which is not a state
    any kernel reports and so not a case worth passing.
    """
    page = 16384
    assert total_bytes % page == 0, "a machine is a whole number of pages"
    assert available_bytes % page == 0, "use _pages() to round to a page"
    free = available_bytes // page
    active = (total_bytes - available_bytes) // page
    return (
        f"Mach Virtual Memory Statistics: (page size of {page} bytes)\n"
        f"Pages free:                              {free}.\n"
        f"Pages active:                           {active}.\n"
        "Pages inactive:                                0.\n"
        "Pages speculative:                             0.\n"
        "Pages wired down:                              0.\n"
    )


# A machine of exactly 10,000 pages, so that 10% of it is exactly 1,000 pages
# and the boundary can be landed on rather than rounded to. Choosing a 16 GiB
# total instead put "exactly 10%" a fraction of a page under the threshold and
# the case quietly tested 9.99% — found by it failing.
PAGE = 16384
TOTAL_PAGES = 10_000
TOTAL = TOTAL_PAGES * PAGE


@pytest.mark.parametrize(
    ("name", "available_pages", "low"),
    [
        ("just under 10%", 999, True),
        ("exactly 10%", 1_000, False),   # `low` is a strict <, so the boundary is not low
        ("just over 10%", 1_001, False),
        ("one page", 1, True),
        ("half", 5_000, False),
    ],
)
def test_macos_low_is_the_same_ten_percent_as_everywhere_else(
    tmp_path: Path, name: str, available_pages: int, low: bool,
) -> None:
    available = available_pages * PAGE
    out = _mac(
        tmp_path,
        sysctl=_sysctl(**{"hw.memsize": str(TOTAL)}),
        vm_stat=lambda: _vm_stat_for(TOTAL, available),
    )

    assert out["memory"]["available_bytes"] == available, name
    assert out["memory"]["low"] is low, name
    # The expected percentage is computed here, not taken from the code.
    assert out["memory"]["available_pct"] == round(100.0 * available / TOTAL, 1), name


@pytest.mark.parametrize("missing", ["Pages free", "Pages inactive"])
def test_macos_one_missing_counter_is_a_gap_not_a_partial_sum(
    tmp_path: Path, missing: str,
) -> None:
    """Each summed counter, removed on its own.

    The first version of this test removed every counter at once, so it
    could not tell "rejects a vm_stat it does not fully understand" from
    "rejects an empty one".
    """
    lines = [line for line in VM_STAT.splitlines(True) if not line.startswith(f"{missing}:")]
    out = _mac(tmp_path, vm_stat=lambda: "".join(lines))

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS), missing
    assert any(f"vm_stat has no {missing} line" in line for line in out["unavailable"])


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (
            "a page size of zero",
            VM_STAT.replace("page size of 16384 bytes", "page size of 0 bytes"),
        ),
        # Not "a plausible wrong size": a size this reader supports but that
        # is wrong for the machine (4096 where the kernel said 16384) is
        # indistinguishable here and is not claimed to be covered.
        ("an unsupported size", VM_STAT.replace("of 16384 bytes", "of 3 bytes")),
        (
            "the header further down instead of first",
            "something else entirely\n" + VM_STAT,
        ),
    ],
)
def test_macos_a_page_size_it_cannot_trust_is_a_gap(tmp_path: Path, name: str, text: str) -> None:
    """A page size of 0 would make every count zero bytes available — a gap
    reported as "out of memory", which is the one answer this must not give.
    """
    out = _mac(tmp_path, vm_stat=lambda: text)

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS), name


def test_macos_an_implausible_total_is_a_gap_and_does_not_escape(tmp_path: Path) -> None:
    """A 400-digit `hw.memsize` parses as an int, and `0.10 * that` raises
    OverflowError. That used to happen outside the guard, so one unreadable
    number took the whole status request with it."""
    out = _mac(tmp_path, sysctl=_sysctl(**{"hw.memsize": "9" * 400}))

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS)
    assert any("not a plausible size" in line for line in out["unavailable"])
    # The rest of the panel still arrived, which is the invariant at stake.
    assert out["uptime_s"] is not None and out["disks"][0]["free_pct"] == 40.0


def test_macos_more_available_than_installed_is_a_gap(tmp_path: Path) -> None:
    """The two commands disagreeing about one machine is not a measurement."""
    out = _mac(
        tmp_path,
        sysctl=_sysctl(**{"hw.memsize": str(2 * 2**30)}),
        vm_stat=lambda: _vm_stat_for(64 * 2**30, 32 * 2**30),
    )

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS)
    assert any("which cannot be" in line for line in out["unavailable"])


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


@pytest.mark.parametrize("ahead_s", [60.0, 0.5])
def test_macos_a_boot_time_in_the_future_is_unknown_rather_than_zero(
    tmp_path: Path, ahead_s: float,
) -> None:
    """Half a second ahead matters as much as a minute: `int(-0.5)` is 0, so
    truncating before the comparison turned a future boot time into a
    plausible zero-second uptime."""
    out = _mac(tmp_path, now=lambda: 1790576331.0 - ahead_s)

    assert out["uptime_s"] is None, ahead_s
    assert any("boot time is in the future" in line for line in out["unavailable"])


@pytest.mark.parametrize(
    ("name", "boottime"),
    [
        ("a number with a tail", "{ sec = 100oops, usec = 1 }"),
        ("two records", "{ sec = 1 }\n{ sec = 100 }"),
    ],
)
def test_macos_an_ambiguous_boot_time_is_unknown(tmp_path: Path, name: str, boottime: str) -> None:
    out = _mac(tmp_path, sysctl=_sysctl(**{"kern.boottime": boottime}), now=lambda: 200.0)

    assert out["uptime_s"] is None, name
    assert any(line.startswith("uptime (sysctl kern.boottime):") for line in out["unavailable"])


def test_a_platform_with_no_reader_says_so_once_per_reading(tmp_path: Path) -> None:
    out = read_server_health(
        disks=[("home", tmp_path)], loadavg=lambda: (0.1, 0.1, 0.1), cpu_count=lambda: 2,
        disk_usage=lambda p: Usage(1000, 600, 400), platform="sunos5",
    )

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS)
    assert out["uptime_s"] is None
    assert out["unavailable"] == [
        "memory: no reader for platform 'sunos5'",
        "uptime: no reader for platform 'sunos5'",
    ]
    # Worded as what happened -- nothing was probed -- rather than as two
    # sources having been tried and failed. Everything that does not need a
    # platform is still there.
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
    # `>= 0`, not `> 0`: a machine with one page available is a reading, and
    # 0.0% is what it rounds to. Unknown is `None`, which the assertion above
    # has already excluded -- that is the distinction this panel is built on.
    assert 0 <= memory["available_bytes"] <= memory["total_bytes"]
    assert 0.0 <= memory["available_pct"] <= 100.0
    assert memory["low"] is (memory["available_bytes"] < LOW_FRACTION * memory["total_bytes"])
    assert out["uptime_s"] is not None and out["uptime_s"] > 0


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("two values for one counter", VM_STAT.replace(
            "Pages active:                           1665080.",
            "Pages free:                                   1.",
        )),
        ("a duplicate further down", VM_STAT + "Pages free:                            1.\n"),
    ],
)
def test_macos_a_duplicated_counter_is_a_gap_not_the_last_value(
    tmp_path: Path, name: str, text: str,
) -> None:
    """`Pages free: 509239.` and then `Pages free: 1.` would have reported a
    machine out of memory, with nothing saying the source was ambiguous."""
    out = _mac(tmp_path, vm_stat=lambda: text)

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS), name
    assert any("two Pages free lines" in line for line in out["unavailable"]), name


def test_macos_a_total_smaller_than_a_page_is_a_gap(tmp_path: Path) -> None:
    """hw.memsize=1 passed the positive check and gave total=1, available=0,
    low=True: an impossible machine presented as one out of memory."""
    out = _mac(tmp_path, sysctl=_sysctl(**{"hw.memsize": "1"}))

    assert out["memory"] == dict.fromkeys(MEMORY_KEYS)
    assert any("smaller than one 16384-byte page" in line for line in out["unavailable"])


@pytest.mark.parametrize(
    ("name", "boottime"),
    [
        ("a field that merely ends in sec", "{ not-sec = 100, usec = 0 }"),
        ("no usec field", "{ sec = 100 }"),
        ("a malformed second record", "{ sec = 1, usec = 0 }\n{ sec = tomorrow }"),
        ("a prefix before the record", "boottime: { sec = 100, usec = 0 }"),
    ],
)
def test_macos_a_boot_time_that_is_not_one_whole_record_is_a_gap(
    tmp_path: Path, name: str, boottime: str,
) -> None:
    """Searching for `sec = <digits>` anywhere accepted all of these: a word
    boundary is not a field boundary, and a malformed second record is
    invisible to a search that already matched the first."""
    out = _mac(tmp_path, sysctl=_sysctl(**{"kern.boottime": boottime}), now=lambda: 200.5)

    assert out["uptime_s"] is None, name
    assert any(
        line.startswith("uptime (sysctl kern.boottime):") for line in out["unavailable"]
    ), name


def test_macos_uptime_counts_the_microseconds_too(tmp_path: Path) -> None:
    """`usec` is part of the boot instant, so a boot 0.9s in the future is in
    the future even when its `sec` field alone is in the past."""
    out = _mac(
        tmp_path,
        sysctl=_sysctl(**{"kern.boottime": "{ sec = 100, usec = 900000 }"}),
        now=lambda: 100.5,
    )

    assert out["uptime_s"] is None
    assert any("boot time is in the future" in line for line in out["unavailable"])

    # And the same record half a second later is a reading of zero seconds.
    ok = _mac(
        tmp_path,
        sysctl=_sysctl(**{"kern.boottime": "{ sec = 100, usec = 900000 }"}),
        now=lambda: 101.0,
    )
    assert ok["uptime_s"] == 0
