"""The agent subscription's usage, as the agent CLI reports it (#247).

Each machine runs on a subscription with a 5-hour and a 7-day window.
Run with ``--output-format stream-json --verbose``, the agent CLI emits
a ``rate_limit_event`` line::

    {"type": "rate_limit_event",
     "rate_limit_info": {"status": "allowed", "resetsAt": 1793503200,
       "unifiedWindows": {
         "five_hour": {"utilization": 0.28, "resetsAt": 1793503200, ...},
         "seven_day": {"utilization": 0.35, "resetsAt": 1793955600, ...}}}}

The field is not documented and can change, so every reader here has a
third answer besides a number: ``None``, published as ``"unknown"``.
A reading that cannot be parsed is never 0% used.

The last reading is kept in a small JSON file beside the state database,
so the web page (which may run in another process) can show it.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.usage")

WINDOWS = ("five_hour", "seven_day")
ALLOWED = "allowed"


@dataclass(frozen=True)
class Window:
    used: float
    resets_at: int | None


@dataclass(frozen=True)
class UsageReading:
    status: str
    windows: dict[str, Window]
    read_at: int
    # When the status itself resets (the event's top-level ``resetsAt``):
    # a refusal on the 5-hour window ends at that window's reset, not at
    # the 7-day one.
    resets_at: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "read_at": self.read_at,
            "resets_at": self.resets_at,
            **{
                name: {"used": w.used, "resets_at": w.resets_at}
                for name, w in self.windows.items()
            },
        }

    @classmethod
    def from_json(cls, data: Any) -> UsageReading | None:
        if not isinstance(data, dict):
            return None
        status = data.get("status")
        read_at = data.get("read_at")
        if not isinstance(status, str) or type(read_at) is not int:
            return None
        windows = {}
        for name in WINDOWS:
            w = _window(data.get(name), used_key="used", reset_key="resets_at")
            if w is not None:
                windows[name] = w
        return cls(
            status=status, windows=windows, read_at=read_at,
            resets_at=_timestamp(data.get("resets_at")),
        )


def _number(value: Any) -> float | None:
    # bool is an int in Python; True is not a utilization. NaN and the
    # infinities are numbers to `json.loads` and nothing else is.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


# Epoch seconds between 2001 and 2286: anything outside is not a reset
# time, and would not survive `datetime.fromtimestamp` either.
_MIN_TS, _MAX_TS = 1_000_000_000, 10_000_000_000


def _timestamp(value: Any) -> int | None:
    number = _number(value)
    if number is None or not _MIN_TS <= number < _MAX_TS:
        return None
    return int(number)


def _window(raw: Any, *, used_key: str, reset_key: str) -> Window | None:
    if not isinstance(raw, dict):
        return None
    used = _number(raw.get(used_key))
    if used is None or not 0 <= used <= 1.5:
        return None
    return Window(used=used, resets_at=_timestamp(raw.get(reset_key)))


def parse_rate_limit_event(
    stdout_text: str, *, now: float | None = None,
) -> UsageReading | None:
    """The last ``rate_limit_event`` in a stream-json run, or ``None``.

    Only the last one is read: when it does not carry a status and at
    least one window with a utilization, the answer is ``None``, not an
    earlier event's numbers. A half-understood line is not a reading.
    """
    last = None
    for line in stdout_text.splitlines():
        if '"rate_limit_event"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            last = None  # a broken event line still replaces the earlier one
            continue
        if isinstance(event, dict) and event.get("type") == "rate_limit_event":
            last = event
    if last is None:
        return None
    info = last.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    status = info.get("status")
    unified = info.get("unifiedWindows")
    if not isinstance(status, str) or not isinstance(unified, dict):
        return None
    windows = {}
    for name in WINDOWS:
        w = _window(unified.get(name), used_key="utilization", reset_key="resetsAt")
        if w is not None:
            windows[name] = w
    if not windows:
        return None
    return UsageReading(
        status=status,
        windows=windows,
        read_at=int(time.time() if now is None else now),
        resets_at=_timestamp(info.get("resetsAt")),
    )


def parse_agent_session_id(stdout_text: str) -> str | None:
    """The agent's own session id, needed by ``--resume``.

    From the stream-json ``result`` line, falling back to any line that
    carries one (a run cut short has no result line), and to the single
    object ``--output-format json`` prints, so a recorded run from
    before the switch still parses.
    """
    text = stdout_text.strip()
    if not text:
        return None
    found: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        value = payload.get("session_id")
        if not isinstance(value, str) or not value:
            continue
        if payload.get("type") == "result":
            return value
        found = found or value
    if found is not None:
        return found
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        value = payload.get("session_id")
        if isinstance(value, str) and value:
            return value
    return None


@dataclass(frozen=True)
class Pause:
    """Why new work is paused, and until when (``None``: no reset time
    known, so look again after the next reading)."""

    reason: str
    until: int | None


def pause_for(
    reading: UsageReading | None, *, threshold: float, now: float,
) -> Pause | None:
    """Whether to stop taking new work.

    Paused when the status is not ``allowed`` or a window is at or above
    ``threshold``, until that window's reset. A reading whose reset time
    has passed says nothing about now, so it does not pause.

    An unknown reading does not pause: there is nothing to wait for, and
    pausing on it would stop a machine that has never read its usage
    from ever starting a run. The page shows it as unknown instead.
    """
    if reading is None:
        return None
    hits: list[tuple[str, int | None]] = []
    if reading.status != ALLOWED:
        until = reading.resets_at
        if until is None:
            resets = [w.resets_at for w in reading.windows.values() if w.resets_at]
            until = max(resets) if resets else None
        hits.append((f"usage status is {reading.status}", until))
    for name, w in reading.windows.items():
        if w.used >= threshold:
            hits.append((f"{_label(name)} usage at {round(w.used * 100)}%", w.resets_at))
    live = [(why, until) for why, until in hits if until is None or until > now]
    if not live:
        return None
    known = [u for _, u in live if u is not None]
    until = max(known) if known else None
    return Pause(reason="; ".join(why for why, _ in live), until=until)


def _label(name: str) -> str:
    return {"five_hour": "5-hour", "seven_day": "7-day"}.get(name, name)


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def usage_json(reading: UsageReading | None) -> dict[str, Any] | str:
    """The ``usage`` value of ``/api/status``: numbers, or ``"unknown"``."""
    if reading is None:
        return "unknown"
    out: dict[str, Any] = {"status": reading.status, "read_at": _iso(reading.read_at)}
    for name in WINDOWS:
        w = reading.windows.get(name)
        out[name] = (
            "unknown" if w is None
            else {"used": w.used, "resets_at": _iso(w.resets_at)}
        )
    return out


class UsageStore:
    """The last reading, in a JSON file written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def beside(cls, state_db: Path) -> UsageStore:
        return cls(Path(state_db).parent / "usage.json")

    def write(self, reading: UsageReading) -> None:
        """Best-effort: a reading that cannot be saved is logged and lost,
        never allowed to fail the run that produced it."""
        self._write_json(reading.to_json())

    def mark_unknown(self, now: float | None = None) -> None:
        """The latest run carried no readable usage: record that, so the
        page says unknown and an older number stops pausing the machine."""
        self._write_json({
            "unknown": True, "read_at": int(time.time() if now is None else now),
        })

    def _write_json(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".usage-")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except Exception as e:
            log_event(_logger, "usage.write_failed", path=str(self.path), error=str(e)[:200])

    def read(self) -> UsageReading | None:
        try:
            return UsageReading.from_json(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            return None


class UsageGate:
    """Decides whether this machine takes new work, and says so once.

    ``notify`` gets one line when the machine pauses and one when it
    resumes, not one per issue skipped.
    """

    def __init__(
        self,
        store: UsageStore,
        *,
        threshold: float,
        notify: Callable[[str], Awaitable[None]] | None = None,
        on_resume: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.threshold = threshold
        self.notify = notify
        # Called when a pause ends, so issues left until the reset are
        # offered again now rather than at the reset time they were
        # given (usage can drop early, e.g. when a probe reads it again).
        self.on_resume = on_resume
        self._clock = clock
        self.paused: Pause | None = None

    async def check(self) -> Pause | None:
        now = self._clock()
        pause = pause_for(self.store.read(), threshold=self.threshold, now=now)
        was = self.paused
        self.paused = pause
        if pause is not None and was is None:
            until = _iso(pause.until) or "the next usage reading"
            log_event(_logger, "usage.paused", reason=pause.reason, until=until)
            await self._say(
                f"⏸️ Pausing new work: {pause.reason}. Runs already "
                f"going will finish. Resuming after {until}."
            )
        elif pause is None and was is not None:
            log_event(_logger, "usage.resumed")
            if self.on_resume is not None:
                try:
                    self.on_resume()
                except Exception as e:
                    log_event(_logger, "usage.resume_hook_failed", error=str(e)[:200])
            await self._say("▶️ Usage is below the limit again; taking new work.")
        return pause

    async def _say(self, text: str) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(text)
        except Exception as e:
            log_event(_logger, "usage.notify_failed", error=str(e)[:200])


PROBE_PROMPT = "Reply with the single word ok."


async def probe_usage(
    binary: str,
    store: UsageStore,
    *,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
) -> UsageReading | None:
    """One tiny agent call, for its usage reading only.

    Run with no tools (so it cannot read a file or run a command), no MCP
    servers, no user or project settings (so no hooks), one turn, in an
    empty temporary directory. Returns the reading and stores it; a call
    that fails or carries no reading returns ``None`` and leaves the
    store alone. Never raises: it runs on a schedule, and its failure is
    only a missed refresh.
    """
    proc: asyncio.subprocess.Process | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="ctrlrelay-usage-") as cwd:
            proc = await asyncio.create_subprocess_exec(
                binary, "-p", PROBE_PROMPT,
                "--output-format", "stream-json", "--verbose",
                "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                "--max-turns", "1",
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            log_event(_logger, "usage.probe_failed", exit_code=proc.returncode)
            return None
        reading = parse_rate_limit_event(stdout.decode(errors="replace"))
        if reading is None:
            log_event(_logger, "usage.probe_failed", error="no usage in the output")
            return None
        store.write(reading)
        return reading
    except asyncio.CancelledError:
        await _reap(proc)
        raise
    except Exception as e:
        await _reap(proc)
        log_event(_logger, "usage.probe_failed", error=f"{type(e).__name__}: {e}"[:200])
        return None


async def _reap(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception:
        pass
