"""``ctrlrelay self-update``: install a published version on this node (#306).

Until now every release was installed by hand on each node: install,
check that no run is going, restart the units, check the version on the
status page. This does the same steps, in an order that cannot cancel a
run, and puts the old version back if any step after the install fails.

1. Read the release from ``pypi.org/pypi/ctrlrelay/<v>/json`` and check
   the status page answers, so the update can be verified at the end.
2. Download the wheel and check its sha256 against that JSON. The wheel
   is installed by path, not by name: a node's package index served a
   stale list for more than ten minutes after a release, and
   ``uv tool install ctrlrelay==X`` failed there.
3. Hold new work (see ``intake_hold``), then wait, bounded, until the
   poller says nothing is in flight and the state database has no
   running session.
4. ``uv tool install --force <wheel>``, restart this node's units, and
   check ``/api/status`` reports the new version.

Publishing stays a human decision: only a version already on PyPI is
installed. Every external effect goes through ``Effects`` so the order
and the failure paths can be tested without a network or a service
manager.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctrlrelay.core.intake_hold import IntakeHold
from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.self_update")

PYPI_JSON = "https://pypi.org/pypi/ctrlrelay/json"
PYPI_VERSION_JSON = "https://pypi.org/pypi/ctrlrelay/{version}/json"
SERVICES = ("bridge", "poller", "web")
# A version as PyPI spells one; anything else is not put into a URL.
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+!-]{0,63}$")
_WHEEL_RE = re.compile(r"^ctrlrelay-[0-9A-Za-z.+!_-]+\.whl$")


class SelfUpdateError(Exception):
    """A step failed. ``step`` names it for the chat line."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message


@dataclass(frozen=True)
class Release:
    version: str
    filename: str
    url: str
    sha256: str


@dataclass(frozen=True)
class Unit:
    """One of this node's services, as its service manager knows it."""

    manager: str  # "systemd" or "launchd"
    service: str  # "bridge", "poller" or "web"
    name: str     # the systemd unit or the launchd label


@dataclass(frozen=True)
class Limits:
    """Every wait has a deadline; each loop also has an iteration cap
    worked out from it, so a clock that does not move cannot hold one."""

    idle_seconds: float = 3600.0
    idle_interval: float = 10.0
    ack_seconds: float = 60.0
    ack_interval: float = 2.0
    status_seconds: float = 120.0
    status_interval: float = 3.0
    # The new poller has to beat twice from one pid: it writes every 30 s,
    # so two beats show it stayed up rather than crash-looping.
    poller_seconds: float = 150.0
    poller_interval: float = 5.0
    command_seconds: float = 300.0
    # How long past the idle wait the hold may outlive a dead updater.
    hold_margin_seconds: float = 1800.0


@dataclass
class Effects:
    fetch_json: Callable[[str], Any]
    download: Callable[[str, Path], None]
    # (argv, timeout) -> (exit code, combined output)
    run: Callable[[list[str], float], tuple[int, str]]
    status: Callable[[], dict[str, Any]]
    running_sessions: Callable[[], int | None]
    poller_alive: Callable[[], bool]
    notify: Callable[[str], None]
    # The poller's heartbeat: {heartbeat_at, pid, version, started_at}.
    poller_heartbeat: Callable[[], dict[str, Any] | None]
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    # Wall time, to compare with the heartbeat's epoch seconds; ``clock``
    # is for deadlines and may be monotonic.
    now: Callable[[], float]
    say: Callable[[str], None] = field(default=lambda _text: None)


@dataclass(frozen=True)
class Outcome:
    old_version: str
    new_version: str
    changed: bool


def wait_until(
    check: Callable[[], bool], *, seconds: float, interval: float,
    clock: Callable[[], float], sleep: Callable[[float], None],
) -> bool:
    """``check()`` until it is true, the deadline passes, or the
    iteration cap is reached. Returns whether it became true."""
    deadline = clock() + seconds
    cap = int(math.ceil(seconds / interval)) + 2 if interval > 0 else 2
    for _ in range(cap):
        if check():
            return True
        left = deadline - clock()
        if left <= 0:
            return False
        sleep(min(interval, left))
    return False


# --- the release ---------------------------------------------------------


def resolve_release(version: str | None, *, fetch_json: Callable[[str], Any]) -> Release:
    if version is not None and not _VERSION_RE.match(version):
        raise SelfUpdateError("resolve", f"{version!r} is not a version")
    url = PYPI_JSON if version is None else PYPI_VERSION_JSON.format(version=version)
    try:
        data = fetch_json(url)
    except Exception as e:
        raise SelfUpdateError("resolve", f"could not read {url}: {e}") from e
    info = data.get("info") if isinstance(data, dict) else None
    got = info.get("version") if isinstance(info, dict) else None
    if not isinstance(got, str) or not _VERSION_RE.match(got):
        raise SelfUpdateError("resolve", f"{url} did not name a version")
    if version is not None and got != version:
        raise SelfUpdateError("resolve", f"asked for {version}, PyPI answered {got}")
    files = data.get("urls")
    wheels = [
        f for f in (files if isinstance(files, list) else [])
        if isinstance(f, dict) and f.get("packagetype") == "bdist_wheel"
        and isinstance(f.get("filename"), str) and _WHEEL_RE.match(f["filename"])
    ]
    # ctrlrelay is pure Python, so there is one wheel; prefer the
    # universal one if a release ever carries more.
    wheels.sort(key=lambda f: "py3-none-any" not in f["filename"])
    if not wheels:
        raise SelfUpdateError("resolve", f"ctrlrelay {got} has no wheel on PyPI")
    wheel = wheels[0]
    digests = wheel.get("digests")
    sha = digests.get("sha256") if isinstance(digests, dict) else None
    link = wheel.get("url")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise SelfUpdateError("resolve", f"{wheel['filename']} has no sha256 on PyPI")
    if not isinstance(link, str) or not link.startswith("https://"):
        raise SelfUpdateError("resolve", f"{wheel['filename']} has no https URL on PyPI")
    return Release(version=got, filename=wheel["filename"], url=link, sha256=sha)


def download_verified(
    release: Release, wheel_dir: Path, *, download: Callable[[str, Path], None],
) -> Path:
    """The wheel, at ``wheel_dir/<filename>``, only if its sha256 matches.

    Kept after the install: ``uv tool install <path>`` records the path
    in its receipt. A wheel that does not match is deleted and nothing
    is installed.
    """
    wheel_dir.mkdir(parents=True, exist_ok=True)
    target = wheel_dir / release.filename
    partial = wheel_dir / (release.filename + ".part")
    try:
        download(release.url, partial)
        digest = hashlib.sha256()
        with partial.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
    except Exception as e:
        partial.unlink(missing_ok=True)
        raise SelfUpdateError("download", f"could not download {release.url}: {e}") from e
    if digest.hexdigest() != release.sha256:
        partial.unlink(missing_ok=True)
        raise SelfUpdateError(
            "download",
            f"{release.filename} sha256 is {digest.hexdigest()}, PyPI says "
            f"{release.sha256}; nothing was installed",
        )
    partial.replace(target)
    return target


# --- the units -----------------------------------------------------------


def find_units(platform: str, *, home: Path) -> list[Unit]:
    """This node's installed units, by the names ``ctrlrelay install``
    gives them: ``ctrlrelay-<service>.service`` under
    ``~/.config/systemd/user``, ``<prefix>.ctrlrelay-<service>.plist``
    under ``~/Library/LaunchAgents`` (the label is the file's name, which
    launchd requires; the prefix is whatever was installed with)."""
    units: list[Unit] = []
    if platform.startswith("linux"):
        base = home / ".config" / "systemd" / "user"
        for service in SERVICES:
            name = f"ctrlrelay-{service}.service"
            if (base / name).is_file():
                units.append(Unit("systemd", service, name))
    elif platform == "darwin":
        base = home / "Library" / "LaunchAgents"
        for service in SERVICES:
            for plist in sorted(base.glob(f"*.ctrlrelay-{service}.plist")):
                units.append(Unit("launchd", service, plist.stem))
    return units


def restart_command(unit: Unit, uid: int) -> list[str]:
    if unit.manager == "systemd":
        return ["systemctl", "--user", "restart", unit.name]
    return ["launchctl", "kickstart", "-k", f"gui/{uid}/{unit.name}"]


# --- the update ----------------------------------------------------------


class SelfUpdate:
    def __init__(
        self,
        *,
        hold: IntakeHold,
        units: list[Unit],
        wheel_dir: Path,
        uv: str,
        uid: int,
        node: str,
        effects: Effects,
        limits: Limits = Limits(),
        require_ack: bool = True,
    ) -> None:
        self.hold = hold
        self.units = units
        self.wheel_dir = wheel_dir
        self.uv = uv
        self.uid = uid
        self.node = node
        self.fx = effects
        self.limits = limits
        self.require_ack = require_ack

    def run(self, version: str | None) -> Outcome:
        fx = self.fx
        old = "unknown"
        target = version or "latest"
        touched = False
        token: str | None = None
        try:
            release = resolve_release(version, fetch_json=fx.fetch_json)
            target = release.version
            old = self._status_version("preflight")
            if old == release.version:
                fx.say(f"{self.node} already runs ctrlrelay {old}.")
                return Outcome(old, old, changed=False)
            self._preflight()
            fx.say(f"Downloading ctrlrelay {release.version} and checking its sha256.")
            wheel = download_verified(release, self.wheel_dir, download=fx.download)

            token = secrets.token_hex(8)
            self.hold.place(
                token=token, reason=f"self-update to {release.version}",
                seconds=self.limits.idle_seconds + self.limits.hold_margin_seconds,
            )
            fx.say("Holding new work; waiting for the run in flight, if any.")
            self._wait_for_idle(token)

            fx.say(f"Installing {wheel.name}.")
            touched = True
            self._install(wheel)
            self._restart_and_verify(release.version, strict=True)
        except SelfUpdateError as e:
            log_event(_logger, "self_update.failed", step=e.step, error=e.message[:300],
                      target=target, old=old)
            text = (f"⚠️ self-update of {self.node} to {target} failed at "
                    f"{e.step}: {e.message}.")
            if touched:
                text += " " + self._roll_back(old)
            else:
                text += f" Nothing was changed; {self.node} still runs {old}."
            self._notify(text)
            raise SelfUpdateError(e.step, text) from e
        except BaseException as e:
            # Ctrl-C, or SIGTERM (the CLI makes it SystemExit), after the
            # install started would leave the node half updated and nobody
            # told. Every step of the roll-back is bounded, so finishing it
            # before going on with the interruption does not hang.
            if touched:
                log_event(_logger, "self_update.interrupted",
                          reason=type(e).__name__, target=target, old=old)
                text = (f"⚠️ self-update of {self.node} to {target} was "
                        f"interrupted ({type(e).__name__}) after the install "
                        f"began. " + self._roll_back(old))
                self._notify(text)
            raise
        finally:
            if token is not None:
                self.hold.release(token)
        log_event(_logger, "self_update.done", old=old, new=release.version)
        self._notify(f"✅ {self.node} updated from ctrlrelay {old} to {release.version}.")
        return Outcome(old, release.version, changed=True)

    # Each step raises SelfUpdateError and nothing else.

    def _status_version(self, step: str) -> str:
        try:
            got = self.fx.status().get("version")
        except Exception as e:
            raise SelfUpdateError(
                step, f"the status page did not answer ({e}), so the update "
                "could not be checked; start ctrlrelay-web first",
            ) from e
        if not isinstance(got, str) or not got:
            raise SelfUpdateError(step, "the status page did not report a version")
        return got

    def _preflight(self) -> None:
        if not any(u.service == "poller" for u in self.units):
            raise SelfUpdateError(
                "preflight", "no ctrlrelay-poller unit is installed here, so "
                "there is nothing to restart onto the new version",
            )
        if not any(u.service == "web" for u in self.units):
            raise SelfUpdateError(
                "preflight", "no ctrlrelay-web unit is installed here, so the "
                "status page would keep reporting the old version",
            )
        code, out = self._cmd([self.uv, "tool", "list"])
        if code != 0 or not re.search(r"^ctrlrelay\s", out, re.M):
            raise SelfUpdateError(
                "preflight", "ctrlrelay is not installed with `uv tool` here; "
                "self-update only replaces a uv tool install",
            )

    def _wait_for_idle(self, token: str) -> None:
        fx, lim = self.fx, self.limits
        need_ack = self.require_ack and fx.poller_alive()
        if need_ack:
            seen = wait_until(
                lambda: self.hold.read_ack(token) is not None,
                seconds=lim.ack_seconds, interval=lim.ack_interval,
                clock=fx.clock, sleep=fx.sleep,
            )
            if not seen:
                raise SelfUpdateError(
                    "wait", f"the poller did not acknowledge the hold in "
                    f"{int(lim.ack_seconds)}s. A poller older than this "
                    "command does not know it; pass --no-ack to go on the "
                    "state database alone",
                )

        def idle() -> bool:
            if need_ack:
                ack = self.hold.read_ack(token)
                if ack is None or ack.busy:
                    return False
            return fx.running_sessions() == 0

        if not wait_until(idle, seconds=lim.idle_seconds, interval=lim.idle_interval,
                          clock=fx.clock, sleep=fx.sleep):
            raise SelfUpdateError(
                "wait", f"a run was still going after {int(lim.idle_seconds)}s "
                "(or the state database could not be read)",
            )

    def _install(self, wheel: Path) -> None:
        code, out = self._cmd([self.uv, "tool", "install", "--force", str(wheel)])
        if code != 0:
            raise SelfUpdateError("install", f"uv exited {code}: {out.strip()[-300:]}")

    def _restart(self) -> None:
        for unit in self.units:
            code, out = self._cmd(restart_command(unit, self.uid))
            if code != 0:
                raise SelfUpdateError(
                    "restart", f"{unit.name} did not restart (exit {code}): "
                    f"{out.strip()[-200:]}",
                )

    def _restart_and_verify(self, version: str, *, strict: bool) -> None:
        """Restart every unit, then require both the web server and a new
        poller process to report ``version``. A restart command returns 0
        even when the unit then crash-loops (``Restart=always``), and the
        status page's version is the web process's own, so neither proves
        the poller is up. ``strict=False`` is for the roll-back: an old
        poller's heartbeat may carry no version, and a fresh one from a new
        process is then the best evidence there is."""
        before = self._heartbeat()
        old_pid = before["pid"] if before else None
        since = int(self.fx.now())
        self._restart()
        self._verify(version)
        self._verify_poller(version, old_pid=old_pid, since=since, strict=strict)

    def _heartbeat(self) -> dict[str, Any] | None:
        try:
            return self.fx.poller_heartbeat()
        except Exception:
            return None

    def _verify_poller(
        self, version: str, *, old_pid: int | None, since: int, strict: bool,
    ) -> None:
        first_beat: dict[int, int] = {}
        last: list[dict[str, Any] | None] = [None]

        def up() -> bool:
            hb = self._heartbeat()
            last[0] = hb
            if hb is None or hb["pid"] == old_pid or hb["heartbeat_at"] < since:
                return False
            got = hb.get("version")
            if got != version and (strict or got is not None):
                return False
            first = first_beat.setdefault(hb["pid"], hb["heartbeat_at"])
            return hb["heartbeat_at"] > first

        if not wait_until(up, seconds=self.limits.poller_seconds,
                          interval=self.limits.poller_interval,
                          clock=self.fx.clock, sleep=self.fx.sleep):
            hb = last[0]
            seen = ("no heartbeat" if hb is None else
                    f"pid {hb['pid']}, version {hb.get('version') or 'unknown'}")
            raise SelfUpdateError(
                "verify", f"no new poller process stayed up on {version} within "
                f"{int(self.limits.poller_seconds)}s (last heartbeat: {seen})",
            )

    def _verify(self, version: str) -> None:
        seen: list[str] = []

        def reports() -> bool:
            try:
                got = self.fx.status().get("version")
            except Exception:
                return False
            seen.append(str(got))
            return got == version

        if not wait_until(reports, seconds=self.limits.status_seconds,
                          interval=self.limits.status_interval,
                          clock=self.fx.clock, sleep=self.fx.sleep):
            last = seen[-1] if seen else "no answer"
            raise SelfUpdateError(
                "verify", f"/api/status did not report {version} within "
                f"{int(self.limits.status_seconds)}s (last: {last})",
            )

    def _roll_back(self, old: str) -> str:
        """Put ``old`` back the same way, and say how that went."""
        self.fx.say(f"Putting {old} back.")
        try:
            release = resolve_release(old, fetch_json=self.fx.fetch_json)
            wheel = download_verified(release, self.wheel_dir, download=self.fx.download)
            self._install(wheel)
            self._restart_and_verify(old, strict=False)
        except SelfUpdateError as e:
            log_event(_logger, "self_update.rollback_failed", step=e.step,
                      error=e.message[:300], old=old)
            return (f"Putting {old} back also failed at {e.step}: {e.message}. "
                    f"{self.node} needs a hand.")
        log_event(_logger, "self_update.rolled_back", old=old)
        return f"{old} was put back and is running."

    def _cmd(self, argv: list[str]) -> tuple[int, str]:
        try:
            return self.fx.run(argv, self.limits.command_seconds)
        except Exception as e:
            return 127, f"{type(e).__name__}: {e}"

    def _notify(self, text: str) -> None:
        try:
            self.fx.notify(text)
        except Exception as e:
            log_event(_logger, "self_update.notify_failed", error=str(e)[:200])
