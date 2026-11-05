"""A hold on new work, set from outside the poller (#306).

``ctrlrelay self-update`` has to restart the poller, and a restart cancels
whatever run is going. Checking for a run and then restarting are two
steps, so a poll can start one in between: measured on 4 Nov, a run
started nine seconds before an install restart cancelled it. So the
updater first stops new work, then waits for the work already going.

It does that with a small file beside the state database, the same way
the usage pause (#247) stops new work: runs already going finish, new
issues and scheduled sweeps are left for later. The poller answers with
an acknowledgement file that says whether anything is still in flight
in this process, so the updater knows the hold was seen and when the
last run ended, rather than guessing from a quiet state database.

A hold names the process that placed it and an expiry. One whose
process is gone, or whose expiry has passed, is ignored: an updater
killed outright must not leave the machine taking no work, silently,
for good.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.intake_hold")

ACK_SECONDS = 2.0


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists. One owned by another user
    still exists (``PermissionError``)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class Hold:
    token: str
    pid: int
    reason: str
    placed_at: int
    expires_at: int

    def to_json(self) -> dict[str, Any]:
        return {
            "token": self.token, "pid": self.pid, "reason": self.reason,
            "placed_at": self.placed_at, "expires_at": self.expires_at,
        }

    @classmethod
    def from_json(cls, data: Any) -> Hold | None:
        if not isinstance(data, dict):
            return None
        token, pid, reason = data.get("token"), data.get("pid"), data.get("reason")
        placed, expires = data.get("placed_at"), data.get("expires_at")
        if not (isinstance(token, str) and token and isinstance(reason, str)):
            return None
        if not all(type(v) is int for v in (pid, placed, expires)):
            return None
        return cls(token=token, pid=pid, reason=reason,
                   placed_at=placed, expires_at=expires)


@dataclass(frozen=True)
class Ack:
    token: str
    busy: bool
    at: int


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".intake-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return None


class IntakeHold:
    """Both sides of the hold: ``place``/``release``/``read_ack`` for the
    updater, ``begin``/``end``/``acknowledge`` for the poller.

    The poller side relies on running on one asyncio loop: ``begin``
    reads the hold and counts the work in one step, and ``acknowledge``
    runs between steps, so an acknowledgement that says "not busy" can
    never be written while a piece of work has passed the check and not
    yet been counted.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        alive: Callable[[int], bool] = pid_alive,
    ) -> None:
        self.path = Path(path)
        self.ack_path = self.path.with_name(self.path.stem + ".ack.json")
        self._clock = clock
        self._alive = alive
        self.in_flight = 0

    @classmethod
    def beside(cls, state_db: Path, **kwargs: Any) -> IntakeHold:
        return cls(Path(state_db).parent / "intake-hold.json", **kwargs)

    # --- the updater's side ------------------------------------------------

    def place(self, *, token: str, reason: str, seconds: float) -> Hold:
        now = int(self._clock())
        hold = Hold(token=token, pid=os.getpid(), reason=reason,
                    placed_at=now, expires_at=now + int(seconds))
        self._drop(self.ack_path)
        _write_json(self.path, hold.to_json())
        return hold

    def release(self, token: str | None = None) -> None:
        """Remove the hold, and its acknowledgement. With ``token``, only
        a hold that is still this one: a second updater's is left alone."""
        if token is not None:
            current = Hold.from_json(_read_json(self.path))
            if current is not None and current.token != token:
                return
        self._drop(self.path)
        self._drop(self.ack_path)

    def read_ack(self, token: str) -> Ack | None:
        data = _read_json(self.ack_path)
        if not isinstance(data, dict) or data.get("token") != token:
            return None
        busy, at = data.get("busy"), data.get("at")
        if not isinstance(busy, bool) or type(at) is not int:
            return None
        return Ack(token=token, busy=busy, at=at)

    @staticmethod
    def _drop(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            log_event(_logger, "intake_hold.remove_failed",
                      path=str(path), error=str(e)[:200])

    # --- the poller's side -------------------------------------------------

    def active(self) -> Hold | None:
        """The hold in force, or ``None``. A hold past its expiry or
        whose updater is gone is ignored; an unreadable file is no hold,
        since a damaged file must not stop the machine for good either."""
        hold = Hold.from_json(_read_json(self.path))
        if hold is None:
            return None
        if self._clock() >= hold.expires_at:
            return None
        if not self._alive(hold.pid):
            return None
        return hold

    def begin(self) -> Hold | None:
        """Ask before taking a piece of new work. ``None`` means go ahead,
        and the work is counted until ``end``; otherwise the hold that
        refuses it, and nothing is counted."""
        hold = self.active()
        if hold is not None:
            return hold
        self.in_flight += 1
        return None

    def end(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)

    def acknowledge(self) -> None:
        """Tell the updater the hold was seen and whether work is still
        in flight. Best-effort: a write that fails is logged and the
        updater keeps waiting, within its own bound."""
        hold = self.active()
        if hold is None:
            return
        try:
            _write_json(self.ack_path, {
                "token": hold.token, "busy": self.in_flight > 0,
                "at": int(self._clock()),
            })
        except Exception as e:
            log_event(_logger, "intake_hold.ack_failed", error=str(e)[:200])

    async def run_acks(self, interval: float = ACK_SECONDS) -> None:
        """Acknowledge until cancelled."""
        seen: str | None = None
        while True:
            hold = self.active()
            token = hold.token if hold is not None else None
            if token != seen:
                if hold is not None:
                    log_event(_logger, "intake_hold.held", reason=hold.reason)
                else:
                    log_event(_logger, "intake_hold.released")
                seen = token
            self.acknowledge()
            await asyncio.sleep(interval)
