"""The status page's server health (#232): load, memory, disk and uptime.

Read only, standard library only. Read from ``os.getloadavg``,
``os.cpu_count``, ``/proc/meminfo``, ``/proc/uptime`` and
``shutil.disk_usage``, on every ``/api/status`` request (each is a few
microseconds).

Every number may be ``null``. A number that could not be read is ``null``,
never ``0`` (which would read as "out of memory" or "no load"), and
``unavailable`` says why, one line per thing that could not be read. Off
Linux there is no ``/proc``, so memory and uptime are ``null`` there.

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
import shutil
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
) -> dict[str, Any]:
    """This machine's health, read now. ``disks`` is ``(name, path)`` pairs
    to measure, in the order shown. ``loadavg`` defaults to
    ``os.getloadavg``, looked up here rather than at import because it
    does not exist on every system.

    Never raises for anything a reading does: each reading is guarded on
    its own, so one that fails is ``null`` with a line in ``unavailable``
    and the others are still made."""
    unavailable: list[str] = []
    if loadavg is None:
        loadavg = getattr(os, "getloadavg", None)
    one, five, fifteen = _load(loadavg, unavailable)
    cores = _cores(cpu_count, unavailable)
    return {
        "load_1m": one,
        "load_5m": five,
        "load_15m": fifteen,
        "cpu_count": cores,
        "memory": _memory(proc, unavailable),
        "disks": [_disk(n, Path(p), disk_usage, unavailable) for n, p in disks],
        "uptime_s": _uptime(proc, unavailable),
        "unavailable": unavailable,
    }
