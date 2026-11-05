"""The daemon's heartbeat, for the status page (#249).

The web server is its own process. It can read the state database, but
nothing there says whether the poller is still alive: a daemon that hung
or stopped leaves the last rows exactly as a quiet one does. So the
daemon writes a small file beside the state database:

* ``heartbeat_at``, every ``HEARTBEAT_SECONDS``, from its own task, so it
  moves while a run holds the poll loop as well as while idle;
* ``last_poll_at``, after each poll of GitHub.

A page that finds the heartbeat old says the machine is **silent**: the
web server answers but nothing confirms the poller is working.

It also carries the writer's ``pid``, ``version`` and ``started_at``, so
``ctrlrelay self-update`` (#306) can tell that a new poller process came up
on the new version and stayed up, rather than trusting the web server,
which reports only its own version, or a restart command that succeeds
even when the unit then crash-loops.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ctrlrelay import __version__
from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.heartbeat")

HEARTBEAT_SECONDS = 30.0


def _is_time(value: object) -> bool:
    """An epoch-seconds int between 2001 and 2286; anything else in the
    file is damage, and a damaged heartbeat is no heartbeat."""
    return type(value) is int and 1_000_000_000 <= value < 10_000_000_000


class Heartbeat:
    def __init__(self, path: Path, *, version: str = __version__) -> None:
        self.path = Path(path)
        self._last_poll_at: int | None = None
        self._version = version
        self._started_at = int(time.time())

    @classmethod
    def beside(cls, state_db: Path) -> Heartbeat:
        return cls(Path(state_db).parent / "heartbeat.json")

    def beat(self, now: float | None = None) -> None:
        self._write(int(time.time() if now is None else now))

    def polled(self, now: float | None = None) -> None:
        self._last_poll_at = int(time.time() if now is None else now)
        self._write(self._last_poll_at)

    def _write(self, now: int) -> None:
        """Best-effort: a heartbeat that cannot be written is logged, never
        allowed to stop the daemon it reports on."""
        data = {
            "heartbeat_at": now, "last_poll_at": self._last_poll_at,
            "pid": os.getpid(), "version": self._version,
            "started_at": self._started_at,
        }
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".heartbeat-")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            # mkstemp makes it 0600; the web server may run as another user.
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
            tmp = None
        except Exception as e:
            log_event(_logger, "heartbeat.write_failed", error=str(e)[:200])
            if tmp is not None:  # every 30 s: never leave one behind per failure
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def read(self) -> dict[str, Any] | None:
        """``{heartbeat_at, last_poll_at}`` as epoch ints (``last_poll_at``
        may be ``None``), or ``None`` when there is no readable heartbeat."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(data, dict):
            return None
        beat = data.get("heartbeat_at")
        poll = data.get("last_poll_at")
        if not _is_time(beat):
            return None
        return {
            "heartbeat_at": beat,
            "last_poll_at": poll if _is_time(poll) else None,
        }

    def read_process(self) -> dict[str, Any] | None:
        """``{heartbeat_at, pid, version, started_at}`` of the process that
        wrote the heartbeat, or ``None`` when it is unreadable. ``version``
        and ``started_at`` are ``None`` from a writer before #306."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(data, dict):
            return None
        beat, pid = data.get("heartbeat_at"), data.get("pid")
        if not _is_time(beat) or type(pid) is not int:
            return None
        version, started = data.get("version"), data.get("started_at")
        return {
            "heartbeat_at": beat,
            "pid": pid,
            "version": version if isinstance(version, str) and version else None,
            "started_at": started if _is_time(started) else None,
        }

    async def run(self, interval: float = HEARTBEAT_SECONDS) -> None:
        """Beat until cancelled."""
        while True:
            self.beat()
            await asyncio.sleep(interval)
