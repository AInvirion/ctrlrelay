"""The status page's server health (#232): load, memory, disk and uptime.

Read only, standard library only. Read from ``os.getloadavg``,
``os.cpu_count``, ``shutil.disk_usage``, and -- for memory and uptime --
whichever source this platform has: ``/proc/meminfo`` and ``/proc/uptime``
on Linux, ``sysctl`` and ``vm_stat`` on macOS.

On every ``/api/status`` request. **The two paths do not cost the same**:
reading ``/proc`` is a few microseconds, while the macOS path starts
**three** processes -- ``sysctl hw.memsize``, ``vm_stat`` and
``sysctl kern.boottime`` -- which is milliseconds at best. Each is bounded
by ``_RUN_TIMEOUT_S``, so a pathological host holds a status request for
three times that and no longer; the bound is small for that reason.

Every number may be ``null``. A number that could not be read is ``null``,
never ``0`` (which would read as "out of memory" or "no load"), and
``unavailable`` says why, one line per thing that could not be read, naming
the source it tried.

**Two of the four machines are Macs**, where there is no ``/proc`` at all,
so a panel that reads only ``/proc`` is empty for half the fleet -- and it
does not fail quietly: the page prints
``memory (/proc/meminfo): FileNotFoundError`` to whoever is reading it,
which looks like a machine in trouble rather than a reader that cannot
look (#321).

**What ``available`` means on macOS, and what it does not.** There is no
``MemAvailable`` to read, so it is ``free + inactive`` pages -- the
definition ``psutil`` uses for macOS, chosen because other tools already
agree on it rather than because it was invented here.

It is an **estimate and an upper bound**: inactive pages can hold dirty
anonymous data that must be compressed or swapped before the memory is
handed out, so some of what is counted is not free for the asking.
``speculative`` and ``purgeable`` are deliberately **not** added -- purgeable
pages are counted inside the other queues, so adding them double-counts.

**The two platforms measure different estimates and this does not claim
otherwise.** ``LOW_FRACTION`` is applied to each platform's own estimate:
"less than 10% of what this kernel would call available". Useful on both,
the same measurement on neither -- which matters to anyone comparing two
machines' percentages side by side.

Shape::

    {"load_1m", "load_5m", "load_15m": float | null,
     "cpu_count": int | null,
     "memory": {"total_bytes", "available_bytes": int | null,
                "available_pct": float | null, "low": bool | null},
     "disks": [{"name", "path", "total_bytes", "free_bytes", "free_pct",
                "low"}],
     "uptime_s": int | null,
     "unavailable": [str]}

``low`` is true when less than ``LOW_FRACTION`` (10%) of the memory or the
disk is available, ``null`` when that could not be measured.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

LOW_FRACTION = 0.10

_MEMORY_KEYS = ("total_bytes", "available_bytes", "available_pct", "low")


def _why(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


def _meminfo_kb(text: str, name: str) -> int:
    """The value of ``name`` in ``/proc/meminfo``, in kB. Raises on a line
    that is missing or not ``<name>: <int> kB``."""
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if sep and key.strip() == name:
            parts = rest.split()
            if len(parts) != 2 or parts[1] != "kB" or not parts[0].isdigit():
                raise ValueError(f"{name} is {rest.strip()!r}, not '<n> kB'")
            return int(parts[0])
    raise ValueError(f"no {name} line")


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1)


def _memory(proc: Path, unavailable: list[str]) -> dict[str, Any]:
    try:
        text = (proc / "meminfo").read_text()
        total = _meminfo_kb(text, "MemTotal") * 1024
        available = _meminfo_kb(text, "MemAvailable") * 1024
        if total <= 0:
            raise ValueError("MemTotal is 0")
    except Exception as exc:
        unavailable.append(f"memory ({proc / 'meminfo'}): {_why(exc)}")
        return dict.fromkeys(_MEMORY_KEYS)
    return {
        "total_bytes": total,
        "available_bytes": available,
        "available_pct": _pct(available, total),
        "low": available < LOW_FRACTION * total,
    }


# The default macOS readers. Separate callables so a test can hand in the
# text of a real `vm_stat` or `sysctl` run instead of needing a Mac.
# Absolute paths, not names on PATH: these run on every status request and
# their whole job is to report what THIS machine is doing, so a `sysctl`
# earlier on somebody's PATH would be answering a different question,
# plausibly. A path that is absent raises, which the callers already turn
# into a gap with a reason.
_SYSCTL = "/usr/sbin/sysctl"
_VM_STAT = "/usr/bin/vm_stat"
# Three commands run per request, so this is a third of the worst case a
# status request can spend in here.
_RUN_TIMEOUT_S = 2


def _run(argv: Sequence[str]) -> str:
    """The output of ``argv``, or raise. Bounded, because a status-page
    request waits on it.

    ``subprocess.run`` kills and reaps the command it started; a command
    that leaves descendants behind is not something this contains, which is
    part of why the two it runs are named absolutely rather than found."""
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=_RUN_TIMEOUT_S, check=True,
    ).stdout


def default_sysctl(name: str) -> str:
    return _run([_SYSCTL, "-n", name]).strip()


def default_vm_stat() -> str:
    return _run([_VM_STAT])


_VM_STAT_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
# "Pages free:   509239." -- the trailing full stop is vm_stat's, not a typo.
_VM_STAT_LINE = re.compile(r"^(.+?):\s+(\d+)\.?\s*$")

# `psutil`'s definition for macOS. See the module docstring for why not more.
_AVAILABLE_PAGES = ("Pages free", "Pages inactive")
# The page sizes a Darwin kernel actually uses. Accepting any number would
# let a header reading "page size of 0 bytes" turn every count into zero
# bytes available -- a gap reported as "out of memory", which is the one
# answer this module must never give. A wrong-but-plausible size would
# scale every number silently.
_PAGE_SIZES = (4096, 16384)
# Larger than any machine this runs on, and small enough that the arithmetic
# below cannot overflow a float. A 400-digit `hw.memsize` parses as an int
# and would otherwise raise OverflowError out of this module, taking the
# whole status request with it rather than one row of one panel.
_MAX_TOTAL_BYTES = 2**60


def _vm_stat_pages(text: str) -> tuple[int, dict[str, int]]:
    """``(page_size, {name: pages})`` from ``vm_stat`` output. Raises unless
    the **first** line carries a page size this understands: every count is
    multiplied by it, and a page size further down the output belongs to
    something this does not understand."""
    lines = text.splitlines()
    head = _VM_STAT_PAGE_SIZE.search(lines[0]) if lines else None
    if not head:
        raise ValueError("no 'page size of <n> bytes' in the first line")
    page_size = int(head.group(1))
    if page_size not in _PAGE_SIZES:
        raise ValueError(f"page size is {page_size}, not one of {_PAGE_SIZES}")
    counts = {}
    for line in lines[1:]:
        found = _VM_STAT_LINE.match(line.strip())
        if found:
            counts[found.group(1).strip('"')] = int(found.group(2))
    return page_size, counts


def _memory_darwin(
    sysctl: Callable[[str], str], vm_stat: Callable[[], str], unavailable: list[str],
) -> dict[str, Any]:
    # Everything, the arithmetic included, inside the guard: a reading that
    # parses and then cannot be divided must still be a gap, not an
    # exception leaving this module and taking the request with it.
    try:
        total = int(sysctl("hw.memsize"))
        if not 0 < total <= _MAX_TOTAL_BYTES:
            # The number itself, only while it is short enough to be worth
            # reading: `_why` truncates at 200 characters, and a 400-digit
            # `hw.memsize` pushed the explanation off the end of its own
            # message, which a test caught by looking for the explanation.
            shown = str(total) if abs(total) <= _MAX_TOTAL_BYTES else f"{len(str(total))} digits"
            raise ValueError(f"hw.memsize is {shown}, not a plausible size")
        page_size, counts = _vm_stat_pages(vm_stat())
        missing = [name for name in _AVAILABLE_PAGES if name not in counts]
        if missing:
            # Not guessed at: a vm_stat without these lines is one this does
            # not understand, and a partial sum would read as low memory.
            raise ValueError(f"vm_stat has no {', '.join(missing)} line")
        available = sum(counts[name] for name in _AVAILABLE_PAGES) * page_size
        if available > total:
            # The two commands disagree about one machine, so neither number
            # is worth putting on a page.
            raise ValueError(
                f"vm_stat reports {available} bytes available of {total}, which cannot be"
            )
        return {
            "total_bytes": total,
            "available_bytes": available,
            "available_pct": _pct(available, total),
            "low": available < LOW_FRACTION * total,
        }
    except Exception as exc:
        unavailable.append(f"memory (sysctl hw.memsize, vm_stat): {_why(exc)}")
        return dict.fromkeys(_MEMORY_KEYS)


# `kern.boottime` prints `{ sec = 1790576331, usec = 490981 } Sun Sep 27 ...`.
# The word boundary matters -- without it a reading with no `sec` field takes
# the microseconds as seconds -- and so does the terminator: `sec = 100oops`
# would otherwise read as 100. A test plants both.
_BOOTTIME_SEC = re.compile(r"\bsec\s*=\s*(\d+)\s*(?=[,}]|$)")


def _uptime_darwin(
    sysctl: Callable[[str], str], now: Callable[[], float], unavailable: list[str],
) -> int | None:
    try:
        text = sysctl("kern.boottime")
        found = _BOOTTIME_SEC.findall(text)
        if not found:
            raise ValueError(f"no 'sec = <n>' in {text!r}")
        if len(found) > 1:
            # Two boot times is not a reading to pick the first of.
            raise ValueError(f"{len(found)} 'sec =' fields in {text!r}")
        # Compared as a float before truncating: `int()` of -0.5 is 0, so
        # truncating first turns a boot time half a second in the future into
        # a plausible zero-second uptime.
        elapsed = now() - int(found[0])
        if elapsed < 0:
            raise ValueError("boot time is in the future")
        return int(elapsed)
    except Exception as exc:
        unavailable.append(f"uptime (sysctl kern.boottime): {_why(exc)}")
        return None


def _uptime(proc: Path, unavailable: list[str]) -> int | None:
    try:
        first = (proc / "uptime").read_text().split()[0]
        return int(float(first))
    except Exception as exc:
        unavailable.append(f"uptime ({proc / 'uptime'}): {_why(exc)}")
        return None


def _disk(
    name: str, path: Path, disk_usage: Callable[[Path], Any], unavailable: list[str],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": name, "path": str(path), "total_bytes": None, "free_bytes": None,
        "free_pct": None, "low": None,
    }
    try:
        usage = disk_usage(path)
        total, free = int(usage.total), int(usage.free)
        if total <= 0:
            raise ValueError("total size is 0")
    except Exception as exc:
        unavailable.append(f"disk {name} ({path}): {_why(exc)}")
        return out
    out.update(
        total_bytes=total, free_bytes=free, free_pct=_pct(free, total),
        low=free < LOW_FRACTION * total,
    )
    return out


def _load(
    loadavg: Callable[[], Any] | None, unavailable: list[str],
) -> tuple[float | None, float | None, float | None]:
    if loadavg is None:
        unavailable.append("load average: os.getloadavg is not available here")
        return None, None, None
    try:
        one, five, fifteen = (float(x) for x in loadavg())
    except Exception as exc:
        unavailable.append(f"load average: {_why(exc)}")
        return None, None, None
    return one, five, fifteen


def _cores(cpu_count: Callable[[], Any], unavailable: list[str]) -> int | None:
    try:
        cores = cpu_count()
    except Exception as exc:
        unavailable.append(f"cpu count: {_why(exc)}")
        return None
    if type(cores) is not int or cores < 1:
        unavailable.append("cpu count: not reported by this system")
        return None
    return cores


def read_server_health(
    *,
    disks: Sequence[tuple[str, Path]],
    proc: Path = Path("/proc"),
    loadavg: Callable[[], Any] | None = None,
    cpu_count: Callable[[], Any] = os.cpu_count,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    platform: str = sys.platform,
    sysctl: Callable[[str], str] = default_sysctl,
    vm_stat: Callable[[], str] = default_vm_stat,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """This machine's health, read now. ``disks`` is ``(name, path)`` pairs
    to measure, in the order shown. ``loadavg`` defaults to
    ``os.getloadavg``, looked up here rather than at import because it
    does not exist on every system.

    ``platform`` decides where memory and uptime are read from -- ``/proc``
    under ``proc`` on Linux, ``sysctl`` and ``vm_stat`` on macOS -- and is a
    parameter so that both paths are tested from fixture text on either
    kind of host. A platform with neither source is not an error: its
    numbers are ``null`` and ``unavailable`` says what was looked for.

    Never raises for anything a reading does: each reading is guarded on
    its own, so one that fails is ``null`` with a line in ``unavailable``
    and the others are still made."""
    unavailable: list[str] = []
    if loadavg is None:
        loadavg = getattr(os, "getloadavg", None)
    one, five, fifteen = _load(loadavg, unavailable)
    cores = _cores(cpu_count, unavailable)
    if platform.startswith("linux"):
        memory = _memory(proc, unavailable)
        uptime_s = _uptime(proc, unavailable)
    elif platform == "darwin":
        memory = _memory_darwin(sysctl, vm_stat, unavailable)
        uptime_s = _uptime_darwin(sysctl, now, unavailable)
    else:
        # One line per reading, as every other gap here is, and worded as
        # what it is: nothing was probed on this platform, so neither line
        # claims to have looked and failed.
        unavailable.append(f"memory: no reader for platform {platform!r}")
        unavailable.append(f"uptime: no reader for platform {platform!r}")
        memory, uptime_s = dict.fromkeys(_MEMORY_KEYS), None
    return {
        "load_1m": one,
        "load_5m": five,
        "load_15m": fifteen,
        "cpu_count": cores,
        "memory": memory,
        "disks": [_disk(n, Path(p), disk_usage, unavailable) for n, p in disks],
        "uptime_s": uptime_s,
        "unavailable": unavailable,
    }
