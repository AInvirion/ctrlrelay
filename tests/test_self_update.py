"""``ctrlrelay self-update`` (#306): hold new work, wait for the run in
flight, install a verified wheel, restart, check, and put the old version
back on any failure after the install.

Every external effect is a fake behind ``Effects``; the hold is the real
``IntakeHold`` on files in ``tmp_path``, and the poller's side of it is a
second real ``IntakeHold`` on the same file, so the acknowledgement goes
through the same code the daemon runs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ctrlrelay.core.intake_hold import IntakeHold
from ctrlrelay.core.self_update import (
    Effects,
    Limits,
    SelfUpdate,
    SelfUpdateError,
    Unit,
    download_verified,
    find_units,
    resolve_release,
    restart_command,
    wait_until,
)

OLD, NEW = "0.22.0", "0.23.0"
WHEEL = {v: f"wheel bytes of {v}".encode() for v in (OLD, NEW)}
UNITS = [
    Unit("systemd", "bridge", "ctrlrelay-bridge.service"),
    Unit("systemd", "poller", "ctrlrelay-poller.service"),
    Unit("systemd", "web", "ctrlrelay-web.service"),
]


def _wheel_url(version: str) -> str:
    return f"https://files.pythonhosted.org/x/ctrlrelay-{version}-py3-none-any.whl"


def _pypi(version: str, *, sha: str | None = None) -> dict:
    return {
        "info": {"version": version},
        "urls": [
            {"packagetype": "sdist", "filename": f"ctrlrelay-{version}.tar.gz",
             "url": "https://files.pythonhosted.org/x/sdist.tar.gz",
             "digests": {"sha256": "0" * 64}},
            {"packagetype": "bdist_wheel",
             "filename": f"ctrlrelay-{version}-py3-none-any.whl",
             "url": _wheel_url(version),
             "digests": {"sha256": sha or hashlib.sha256(WHEEL[version]).hexdigest()}},
        ],
    }


class FakeNode:
    """A node: PyPI, the network, the service manager and the state
    database, as far as self-update can see them."""

    def __init__(self, tmp_path: Path) -> None:
        self.now = 1000.0
        self.pypi = {None: _pypi(NEW), NEW: _pypi(NEW), OLD: _pypi(OLD)}
        self.served = dict(WHEEL)            # what a download actually returns
        self.installed = OLD
        self.running_version = OLD           # what the restarted units run
        self.commands: list[list[str]] = []
        self.fail_command: str | None = None  # a substring of the argv to fail
        self.sessions: list[int | None] = [0]  # popped per read; last one sticks
        self.sessions_at_install: int | None = -1
        self.poller_alive = True
        self.poller_answers = True
        self.chat: list[str] = []
        self.sleeps = 0
        self.hold = IntakeHold.beside(tmp_path / "state.db", clock=self.clock)
        # The poller's own instance, on the same file.
        self.poller = IntakeHold.beside(tmp_path / "state.db", clock=self.clock)
        self.on_sleep: list = []
        # The poller process: a pre-#306 poller writes no version.
        self.poller_pid = 4000
        self.poller_version: str | None = None
        self.poller_up = True
        # What a poller started on NEW does: "ok", "dead" (never writes a
        # heartbeat) or "crashloop" (a new pid at every look).
        self.new_poller = "ok"

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds
        if self.poller_answers:
            self.poller.acknowledge()
        for hook in self.on_sleep:
            hook()

    def fetch_json(self, url: str):
        for version, data in self.pypi.items():
            want = ("https://pypi.org/pypi/ctrlrelay/json" if version is None
                    else f"https://pypi.org/pypi/ctrlrelay/{version}/json")
            if url == want:
                return data
        raise OSError(f"404 {url}")

    def download(self, url: str, dest: Path) -> None:
        for version, data in self.served.items():
            if url == _wheel_url(version):
                dest.write_bytes(data)
                return
        raise OSError(f"404 {url}")

    def run(self, argv: list[str], timeout: float) -> tuple[int, str]:
        self.commands.append(argv)
        if self.fail_command and self.fail_command in " ".join(argv):
            return 1, "it went wrong"
        if argv[1:3] == ["tool", "list"]:
            return 0, f"ctrlrelay v{self.installed}\n- ctrlrelay\n"
        if argv[1:4] == ["tool", "install", "--force"]:
            self.sessions_at_install = self.sessions[0]
            path = Path(argv[4])
            for version, data in WHEEL.items():
                if path.read_bytes() == data:
                    self.installed = version
            return 0, ""
        if "restart" in argv or "kickstart" in argv:
            if "poller" in argv[-1]:
                self._start_poller()
            else:
                self.running_version = self.installed
            return 0, ""
        return 0, ""

    def _start_poller(self) -> None:
        self.poller_pid += 1
        self.poller_version = NEW if self.installed == NEW else None
        self.poller_up = not (self.installed == NEW and self.new_poller == "dead")

    def poller_heartbeat(self) -> dict | None:
        if self.installed == NEW and self.new_poller == "crashloop":
            self.poller_pid += 1
        if not self.poller_up:
            # Only the last heartbeat of the process that was stopped.
            return {"heartbeat_at": 900, "pid": 4000, "version": None,
                    "started_at": 800}
        return {"heartbeat_at": int(self.now), "pid": self.poller_pid,
                "version": self.poller_version, "started_at": int(self.now)}

    def status(self) -> dict:
        return {"host": "alka", "version": self.running_version}

    def running_sessions(self) -> int | None:
        if len(self.sessions) > 1:
            return self.sessions.pop(0)
        return self.sessions[0]

    def effects(self) -> Effects:
        return Effects(
            fetch_json=self.fetch_json, download=self.download, run=self.run,
            status=self.status, running_sessions=self.running_sessions,
            poller_alive=lambda: self.poller_alive, notify=self.chat.append,
            poller_heartbeat=self.poller_heartbeat,
            clock=self.clock, sleep=self.sleep, now=self.clock,
        )

    def installs(self) -> list[list[str]]:
        return [c for c in self.commands if c[1:3] == ["tool", "install"]]

    def restarts(self) -> list[list[str]]:
        return [c for c in self.commands if c[0] == "systemctl"]


def _updater(node: FakeNode, tmp_path: Path, **kw) -> SelfUpdate:
    kw.setdefault("limits", Limits(idle_seconds=600, idle_interval=10,
                                   ack_seconds=20, ack_interval=2,
                                   status_seconds=30, status_interval=3))
    return SelfUpdate(
        hold=node.hold, units=UNITS, wheel_dir=tmp_path / "wheels", uv="uv",
        uid=1000, node="alka", effects=node.effects(), **kw,
    )


@pytest.fixture
def node(tmp_path: Path) -> FakeNode:
    return FakeNode(tmp_path)


# --- the whole update --------------------------------------------------------


def test_an_idle_node_is_updated_restarted_and_checked(node, tmp_path) -> None:
    outcome = _updater(node, tmp_path).run(None)

    assert (outcome.old_version, outcome.new_version, outcome.changed) == (OLD, NEW, True)
    wheel = tmp_path / "wheels" / f"ctrlrelay-{NEW}-py3-none-any.whl"
    assert node.installs() == [["uv", "tool", "install", "--force", str(wheel)]]
    assert node.restarts() == [
        ["systemctl", "--user", "restart", u.name] for u in UNITS
    ]
    assert node.running_version == NEW
    assert node.poller_version == NEW and node.poller_pid == 4001
    assert not node.hold.path.exists() and not node.hold.ack_path.exists()
    assert node.chat == [f"✅ alka updated from ctrlrelay {OLD} to {NEW}."]


def test_an_explicit_version_is_read_from_its_own_json(node, tmp_path) -> None:
    node.pypi[None] = _pypi(OLD)   # "latest" would be a no-op
    assert _updater(node, tmp_path).run(NEW).new_version == NEW


def test_a_node_already_on_the_version_does_nothing(node, tmp_path) -> None:
    node.running_version = NEW
    outcome = _updater(node, tmp_path).run(NEW)
    assert outcome.changed is False
    assert node.commands == [] and node.chat == []
    assert not node.hold.path.exists()


# --- the sha256 ---------------------------------------------------------------


def test_a_sha256_mismatch_installs_nothing(node, tmp_path) -> None:
    node.served[NEW] = b"something else entirely"

    with pytest.raises(SelfUpdateError) as e:
        _updater(node, tmp_path).run(NEW)

    assert e.value.step == "download"
    assert node.installs() == [] and node.restarts() == []
    assert node.installed == OLD and node.running_version == OLD
    # Neither the bad file nor a hold is left behind.
    assert list((tmp_path / "wheels").iterdir()) == []
    assert not node.hold.path.exists()
    assert len(node.chat) == 1
    assert "sha256" in node.chat[0] and f"still runs {OLD}" in node.chat[0]


def test_download_verified_keeps_only_a_matching_wheel(tmp_path) -> None:
    release = resolve_release(NEW, fetch_json=lambda _url: _pypi(NEW))
    path = download_verified(
        release, tmp_path, download=lambda _u, dest: dest.write_bytes(WHEEL[NEW]),
    )
    assert path.read_bytes() == WHEEL[NEW]
    assert path.name == release.filename


# --- waiting for the run in flight ------------------------------------------


def test_a_busy_node_waits_and_installs_only_once_idle(node, tmp_path) -> None:
    node.sessions = [1, 1, 1, 0]

    _updater(node, tmp_path).run(NEW)

    assert node.sessions_at_install == 0
    assert node.installed == NEW


def test_the_wait_for_a_busy_node_is_bounded(node, tmp_path) -> None:
    node.sessions = [1]
    start = node.now

    with pytest.raises(SelfUpdateError) as e:
        _updater(node, tmp_path).run(NEW)

    assert e.value.step == "wait"
    # Gave up at the deadline, after a bounded number of looks.
    assert node.now - start == pytest.approx(600 + 4, abs=10)
    assert node.sleeps <= 600 // 10 + 20
    assert node.installs() == [] and node.running_version == OLD
    assert not node.hold.path.exists()
    assert f"still runs {OLD}" in node.chat[0]


def test_wait_until_is_bounded_even_when_the_clock_does_not_move() -> None:
    calls = []
    got = wait_until(lambda: calls.append(1) and False, seconds=100, interval=10,
                     clock=lambda: 0.0, sleep=lambda _s: None)
    assert got is False
    assert len(calls) == 12   # ceil(100/10) + 2


def test_a_run_the_poller_is_still_counting_holds_the_install(node, tmp_path) -> None:
    """The state database can look idle between an issue passing the
    check and its session row being written (#306's first comment). The
    poller's own count is what closes that gap."""
    node.poller.in_flight = 1
    node.sessions = [0]
    ticks = []

    def finish_after_three() -> None:
        ticks.append(1)
        if len(ticks) == 3:
            node.poller.end()

    node.on_sleep.append(finish_after_three)

    _updater(node, tmp_path).run(NEW)

    assert len(ticks) >= 3
    assert node.installed == NEW


def test_a_poller_that_does_not_acknowledge_stops_the_update(node, tmp_path) -> None:
    node.poller_answers = False

    with pytest.raises(SelfUpdateError) as e:
        _updater(node, tmp_path).run(NEW)

    assert e.value.step == "wait" and "--no-ack" in e.value.message
    assert node.installs() == []
    assert not node.hold.path.exists()


def test_no_ack_goes_on_the_state_database_alone(node, tmp_path) -> None:
    node.poller_answers = False
    _updater(node, tmp_path, require_ack=False).run(NEW)
    assert node.installed == NEW


def test_a_stopped_poller_is_not_waited_on_for_an_ack(node, tmp_path) -> None:
    node.poller_alive = False
    node.poller_answers = False
    _updater(node, tmp_path).run(NEW)
    assert node.installed == NEW


def test_unreadable_sessions_are_not_idle(node, tmp_path) -> None:
    node.sessions = [None]
    with pytest.raises(SelfUpdateError) as e:
        _updater(node, tmp_path).run(NEW)
    assert e.value.step == "wait"
    assert node.installs() == []


# --- failures after the install, and the hold on every exit path ------------


def test_a_failed_restart_puts_the_old_version_back(node, tmp_path) -> None:
    calls = {"n": 0}
    real_run = node.run

    def run(argv, timeout):
        # The first restart of the poller fails; the roll-back's succeeds.
        if argv[-1] == "ctrlrelay-poller.service":
            calls["n"] += 1
            if calls["n"] == 1:
                node.commands.append(argv)
                return 1, "Job failed"
        return real_run(argv, timeout)

    node.run = run

    with pytest.raises(SelfUpdateError) as e:
        _updater(node, tmp_path).run(NEW)

    assert e.value.step == "restart"
    assert [c[-1] for c in node.installs()] == [
        str(tmp_path / "wheels" / f"ctrlrelay-{v}-py3-none-any.whl") for v in (NEW, OLD)
    ]
    assert node.installed == OLD and node.running_version == OLD
    assert not node.hold.path.exists()
    assert len(node.chat) == 1
    assert "failed at restart" in node.chat[0]
    assert f"{OLD} was put back and is running" in node.chat[0]


def test_a_version_the_status_page_never_reports_is_rolled_back(node, tmp_path) -> None:
    node.status = lambda: {"host": "alka", "version": OLD if node.installed == OLD
                           else "0.0.0-stale"}
    with pytest.raises(SelfUpdateError) as e:
        _updater(node, tmp_path).run(NEW)
    assert e.value.step == "verify"
    assert node.installed == OLD
    assert "was put back" in node.chat[0]
    assert not node.hold.path.exists()


def test_a_failed_roll_back_says_the_node_needs_a_hand(node, tmp_path) -> None:
    node.fail_command = "restart ctrlrelay-web"
    with pytest.raises(SelfUpdateError):
        _updater(node, tmp_path).run(NEW)
    assert "also failed" in node.chat[0] and "needs a hand" in node.chat[0]
    assert not node.hold.path.exists()


def test_the_hold_is_cleared_when_the_update_is_interrupted(node, tmp_path) -> None:
    """Ctrl-C or SIGTERM (the CLI turns it into SystemExit) mid-wait."""
    node.sessions = [1]
    seen = []

    def interrupt() -> None:
        seen.append(node.poller.active() is not None)
        raise KeyboardInterrupt

    node.on_sleep.append(interrupt)
    with pytest.raises(KeyboardInterrupt):
        _updater(node, tmp_path).run(NEW)
    assert seen == [True]            # it was held while waiting
    assert not node.hold.path.exists()
    assert node.poller.active() is None


def test_the_hold_is_held_during_the_install(node, tmp_path) -> None:
    held = []
    real_run = node.run

    def run(argv, timeout):
        if argv[1:3] == ["tool", "install"]:
            held.append(node.poller.active() is not None)
        return real_run(argv, timeout)

    node.run = run
    _updater(node, tmp_path).run(NEW)
    assert held == [True]


# --- preflight ---------------------------------------------------------------


def test_no_status_page_refuses_before_anything_changes(node, tmp_path) -> None:
    def down() -> dict:
        raise ConnectionRefusedError("refused")

    node.status = down
    with pytest.raises(SelfUpdateError) as e:
        _updater(node, tmp_path).run(NEW)
    assert e.value.step == "preflight"
    assert node.commands == [] and not node.hold.path.exists()


def test_a_node_not_installed_with_uv_tool_is_refused(node, tmp_path) -> None:
    node.run = lambda argv, timeout: (node.commands.append(argv) or (0, "ruff v0.1\n"))
    with pytest.raises(SelfUpdateError) as e:
        _updater(node, tmp_path).run(NEW)
    assert e.value.step == "preflight"
    assert node.installs() == []


def test_without_a_web_unit_the_update_is_refused(node, tmp_path) -> None:
    updater = _updater(node, tmp_path)
    updater.units = [u for u in UNITS if u.service != "web"]
    with pytest.raises(SelfUpdateError) as e:
        updater.run(NEW)
    assert e.value.step == "preflight"
    assert node.installs() == []


# --- PyPI's answer -----------------------------------------------------------


def test_resolve_picks_the_wheel_and_its_sha() -> None:
    r = resolve_release(NEW, fetch_json=lambda _url: _pypi(NEW))
    assert r.filename == f"ctrlrelay-{NEW}-py3-none-any.whl"
    assert r.sha256 == hashlib.sha256(WHEEL[NEW]).hexdigest()


@pytest.mark.parametrize("data", [
    {"info": {"version": "9.9.9"}, "urls": _pypi(NEW)["urls"]},   # another version
    {"info": {"version": NEW}, "urls": []},                        # no wheel
    {"info": {"version": NEW}, "urls": [
        {**_pypi(NEW)["urls"][1], "digests": {}}]},                # no sha256
    {"info": {"version": NEW}, "urls": [
        {**_pypi(NEW)["urls"][1], "url": "http://plain/x.whl"}]},   # not https
    "not json at all",
])
def test_resolve_refuses_an_answer_it_cannot_trust(data) -> None:
    with pytest.raises(SelfUpdateError):
        resolve_release(NEW, fetch_json=lambda _url: data)


def test_resolve_refuses_a_version_that_is_not_one() -> None:
    with pytest.raises(SelfUpdateError):
        resolve_release("../../etc", fetch_json=lambda _url: pytest.fail("fetched"))


# --- the units ---------------------------------------------------------------


def test_find_units_on_linux_lists_the_installed_systemd_units(tmp_path) -> None:
    base = tmp_path / ".config" / "systemd" / "user"
    base.mkdir(parents=True)
    for service in ("poller", "web"):
        (base / f"ctrlrelay-{service}.service").write_text("")
    assert find_units("linux", home=tmp_path) == [
        Unit("systemd", "poller", "ctrlrelay-poller.service"),
        Unit("systemd", "web", "ctrlrelay-web.service"),
    ]


def test_find_units_on_macos_uses_the_installed_label(tmp_path) -> None:
    base = tmp_path / "Library" / "LaunchAgents"
    base.mkdir(parents=True)
    for service in ("bridge", "poller", "web"):
        (base / f"com.ainvirion.ctrlrelay-{service}.plist").write_text("")
    (base / "com.other.thing.plist").write_text("")
    units = find_units("darwin", home=tmp_path)
    assert [u.name for u in units] == [
        f"com.ainvirion.ctrlrelay-{s}" for s in ("bridge", "poller", "web")
    ]
    assert restart_command(units[1], 501) == [
        "launchctl", "kickstart", "-k", "gui/501/com.ainvirion.ctrlrelay-poller",
    ]


def test_restart_command_on_systemd() -> None:
    assert restart_command(UNITS[1], 1000) == [
        "systemctl", "--user", "restart", "ctrlrelay-poller.service",
    ]


# --- the poller, not just the web server, has to come up on the new version --


@pytest.mark.parametrize("mode", ["dead", "crashloop"])
def test_a_new_poller_that_does_not_stay_up_is_rolled_back(node, tmp_path, mode) -> None:
    """The web server reports the new version and every restart command
    returned 0, yet no new poller process kept beating."""
    node.new_poller = mode

    with pytest.raises(SelfUpdateError) as e:
        _updater(node, tmp_path).run(NEW)

    assert node.running_version == OLD   # the web was rolled back too
    assert e.value.step == "verify" and "poller" in e.value.message
    assert node.installed == OLD
    assert len(node.chat) == 1
    assert "failed at verify" in node.chat[0] and "was put back" in node.chat[0]
    assert not node.hold.path.exists()


def test_a_poller_still_on_the_old_version_is_not_enough(node, tmp_path) -> None:
    def start_stale() -> None:
        node.poller_pid += 1
        node.poller_version = OLD
        node.poller_up = True

    node._start_poller = start_stale
    with pytest.raises(SelfUpdateError) as e:
        _updater(node, tmp_path).run(NEW)
    assert e.value.step == "verify"
    assert "was put back" in node.chat[0]


# --- interrupted after the install ------------------------------------------


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_an_interruption_after_the_install_rolls_back_and_says_so(
    node, tmp_path, exc,
) -> None:
    real_run = node.run
    interrupted = []

    def run(argv, timeout):
        out = real_run(argv, timeout)
        if argv[1:3] == ["tool", "install"] and not interrupted:
            interrupted.append(True)
            raise exc(143) if exc is SystemExit else exc()
        return out

    node.run = run
    with pytest.raises(exc):
        _updater(node, tmp_path).run(NEW)

    assert node.installed == OLD and node.running_version == OLD
    assert len(node.chat) == 1
    assert f"interrupted ({exc.__name__})" in node.chat[0]
    assert f"{OLD} was put back and is running" in node.chat[0]
    assert not node.hold.path.exists()


def test_an_interruption_before_the_install_changes_nothing(node, tmp_path) -> None:
    node.sessions = [1]

    def interrupt() -> None:
        raise KeyboardInterrupt

    node.on_sleep.append(interrupt)
    with pytest.raises(KeyboardInterrupt):
        _updater(node, tmp_path).run(NEW)
    assert node.installs() == [] and node.chat == []


# --- the heartbeat names its process and version ----------------------------


def test_the_heartbeat_names_its_process_and_version(tmp_path) -> None:
    import json
    import os

    from ctrlrelay.core.heartbeat import Heartbeat

    path = tmp_path / "heartbeat.json"
    Heartbeat(path, version="9.9.9").beat(now=1_800_000_000)
    got = Heartbeat(path).read_process()
    assert got["pid"] == os.getpid() and got["version"] == "9.9.9"
    assert got["heartbeat_at"] == 1_800_000_000
    assert isinstance(got["started_at"], int)

    # One written before #306: no version, no start time.
    path.write_text(json.dumps({"heartbeat_at": 1_800_000_000, "last_poll_at": None,
                                "pid": 12}))
    assert Heartbeat(path).read_process() == {
        "heartbeat_at": 1_800_000_000, "pid": 12, "version": None, "started_at": None,
    }
