"""An agent's checkpoint and its leftover processes (#394).

Two ways a session outlived what the relay thought it controlled:

* the checkpoint read trusted whatever the agent left at the state path,
  so a FIFO there blocked the read forever — after the session timeout had
  already stopped applying — and a huge file was read whole;
* the agent shared the relay's process group, and only the direct child was
  ever signalled, so anything it started in the background kept running
  after the run was reported done.

Every test that exercises signalling runs the dispatcher in a **child
Python process started in its own session**. If the guard that keeps the
relay from signalling its own process group ever regresses, the most it
can take down is that child — not pytest, and not the shell that started
it. That is the failure that killed a relay seven times while this was
being written, so it is not hypothetical.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ctrlrelay.core.checkpoint import (
    MAX_CHECKPOINT_BYTES,
    CheckpointError,
    read_checkpoint,
)

_VALID = (
    '{"version":"1","status":"DONE","session_id":"s",'
    '"timestamp":"2026-11-07T00:00:00Z","summary":"ok"}'
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0. It is not running anything, which
    # is all these tests care about.
    probe = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True, text=True, check=False,
    )
    return bool(probe.stdout.strip()) and not probe.stdout.strip().startswith("Z")


def _wait_dead(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _read_isolated(path: Path, timeout: float = 15.0) -> dict:
    """`read_checkpoint(path)` in a child process, so a hang is a failure
    with a timeout rather than a test run that never ends."""
    code = textwrap.dedent(f"""
        import json, sys
        from pathlib import Path
        from ctrlrelay.core.checkpoint import CheckpointError, read_checkpoint
        try:
            s = read_checkpoint(Path({str(path)!r}))
            print(json.dumps({{"ok": True, "status": s.status.value}}))
        except CheckpointError as e:
            print(json.dumps({{"ok": False, "error": str(e)}}))
    """)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout,
            start_new_session=True, check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"read_checkpoint blocked for more than {timeout}s")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestTheCheckpointReadIsBounded:
    def test_a_regular_checkpoint_still_reads(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text(_VALID)
        assert read_checkpoint(path).status.value == "DONE"

    def test_a_fifo_is_refused_without_blocking(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        os.mkfifo(path)

        result = _read_isolated(path)

        assert result["ok"] is False
        assert "regular file" in result["error"]

    def test_a_symlink_is_not_followed(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere.json"
        target.write_text(_VALID)
        path = tmp_path / "state.json"
        path.symlink_to(target)

        with pytest.raises(CheckpointError):
            read_checkpoint(path)

    def test_a_symlink_to_an_endless_device_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.symlink_to("/dev/zero")

        result = _read_isolated(path)

        assert result["ok"] is False

    def test_a_file_over_the_cap_is_refused_unread(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        with open(path, "wb") as f:
            f.truncate(MAX_CHECKPOINT_BYTES + 1)

        with pytest.raises(CheckpointError, match="larger than"):
            read_checkpoint(path)

    @pytest.mark.parametrize(
        "payload",
        [
            '{"version":"1","status":"DONE","n":' + "9" * 5000 + "}",
            "[" * 20000 + "]" * 20000,
            b"\xff\xfe not utf-8",
        ],
        ids=["huge-int", "deep-nesting", "bad-encoding"],
    )
    def test_what_breaks_the_parser_is_a_checkpoint_error(
        self, tmp_path: Path, payload: str | bytes
    ) -> None:
        path = tmp_path / "state.json"
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(payload)

        with pytest.raises(CheckpointError):
            read_checkpoint(path)

    def test_a_refused_file_is_not_deleted(self, tmp_path: Path) -> None:
        """`delete_after` is for a checkpoint that was read. A FIFO left in
        place is evidence, and deleting it is not this function's job."""
        path = tmp_path / "state.json"
        os.mkfifo(path)

        with pytest.raises(CheckpointError):
            read_checkpoint(path, delete_after=True)

        assert stat.S_ISFIFO(os.lstat(path).st_mode)


# --- leftover processes -----------------------------------------------------

_AGENT = """#!/bin/sh
# Start a background job that would outlive the session, and say where it is.
sleep 1000 </dev/null >/dev/null 2>&1 &
printf '%s' "$!" > "$AGENT_PROBE/background.pid"
printf '%s' "$$" > "$AGENT_PROBE/agent.pid"
ps -o pgid= -p "$$" | tr -d ' ' > "$AGENT_PROBE/agent.pgid"
if [ -n "$AGENT_HANGS" ]; then
  exec sleep 1000
fi
mkdir -p -- "$(dirname "$CTRLRELAY_STATE_FILE")"
printf '{"version":"1","status":"DONE","session_id":"%s",' \\
  "$CTRLRELAY_SESSION_ID" > "$CTRLRELAY_STATE_FILE"
printf '"timestamp":"2026-11-07T00:00:00Z","summary":"done"}' \\
  >> "$CTRLRELAY_STATE_FILE"
exit 0
"""

_DRIVER = """
import asyncio, json, os, sys
from pathlib import Path
from ctrlrelay.core.config import AgentConfig
from ctrlrelay.core.dispatcher import make_agent_dispatcher

script, work, timeout = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
cfg = AgentConfig.model_validate({
    "type": "fake", "sandbox": "none",
    "plugins": {"fake": {"binary": script}},
})
d = make_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)

async def main():
    r = await d.spawn_session(
        "pg-test", "go", work, work / ".ctrlrelay" / "state.json",
        timeout=timeout, github_access=False,
    )
    return {
        "exit_code": r.exit_code,
        "timed_out": r.timed_out,
        "status": r.state.status.value if r.state else None,
        "driver_pgid": os.getpgrp(),
    }

print(json.dumps(asyncio.run(main())))
"""


def _spawn_isolated(
    tmp_path: Path, request: pytest.FixtureRequest, *, timeout: int = 30,
    hangs: bool = False, agent: str = _AGENT,
) -> tuple[dict, Path]:
    probe = tmp_path / "probe"
    probe.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    script = tmp_path / "agent.sh"
    script.write_text(agent)
    script.chmod(0o755)
    env = {**os.environ, "AGENT_PROBE": str(probe)}
    if hangs:
        env["AGENT_HANGS"] = "1"
    else:
        env.pop("AGENT_HANGS", None)
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)

    def _cleanup() -> None:
        # After the test has looked: whatever happened, nothing this test
        # started is left running.
        for name in ("background.pid", "agent.pid"):
            pid_file = probe / name
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), 9)
                except (ProcessLookupError, ValueError):
                    pass

    request.addfinalizer(_cleanup)
    proc = subprocess.run(
        [sys.executable, str(driver), str(script), str(work), str(timeout)],
        capture_output=True, text=True, timeout=timeout + 30, env=env,
        start_new_session=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1]), probe


pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process groups"
)


class TestLeftoverProcessesAreReaped:
    def test_the_agent_leads_its_own_process_group(
        self, tmp_path: Path, request: pytest.FixtureRequest
    ) -> None:
        result, probe = _spawn_isolated(tmp_path, request)

        agent_pid = int((probe / "agent.pid").read_text())
        agent_pgid = int((probe / "agent.pgid").read_text())
        assert agent_pgid == agent_pid
        assert agent_pgid != result["driver_pgid"]

    def test_a_background_job_does_not_survive_a_clean_exit(
        self, tmp_path: Path, request: pytest.FixtureRequest
    ) -> None:
        result, probe = _spawn_isolated(tmp_path, request)

        assert result["status"] == "DONE"
        background = int((probe / "background.pid").read_text())
        assert _wait_dead(background), "the background job outlived the session"

    def test_nothing_survives_a_timeout(
        self, tmp_path: Path, request: pytest.FixtureRequest
    ) -> None:
        result, probe = _spawn_isolated(tmp_path, request, timeout=3, hangs=True)

        assert result["timed_out"] is True
        for name in ("agent.pid", "background.pid"):
            pid = int((probe / name).read_text())
            assert _wait_dead(pid), f"{name} outlived the timeout"

    def test_the_relay_never_signals_its_own_group(self) -> None:
        """The guard, directly. A real child spawned WITHOUT its own
        session is in this process's group; treating that as the agent's
        group would signal pytest itself, which is the relay-killing
        failure in miniature. It must fall back to the child alone."""
        import asyncio

        from ctrlrelay.core.dispatcher import _agent_group

        async def check() -> tuple[int | None, int | None]:
            shared = await asyncio.create_subprocess_exec("sleep", "30")
            own = await asyncio.create_subprocess_exec(
                "sleep", "30", start_new_session=True,
            )
            try:
                return _agent_group(shared), _agent_group(own)
            finally:
                for p in (shared, own):
                    p.kill()
                    await p.wait()

        shared_group, own_group = asyncio.run(check())

        assert shared_group is None
        assert own_group is not None and own_group != os.getpgrp()


class TestTheStateDirectoryIsNotFollowed:
    def test_a_symlinked_state_directory_is_refused_and_nothing_is_deleted(
        self, tmp_path: Path
    ) -> None:
        """The agent controls its `.ctrlrelay` directory. Replaced with a
        symlink to another run's, the relay used to read that run's
        checkpoint as this one's result — and delete it."""
        other = tmp_path / "other-run" / ".ctrlrelay"
        other.mkdir(parents=True)
        (other / "state.json").write_text(_VALID)
        mine = tmp_path / "this-run"
        mine.mkdir()
        (mine / ".ctrlrelay").symlink_to(other)

        with pytest.raises(CheckpointError):
            read_checkpoint(mine / ".ctrlrelay" / "state.json", delete_after=True)

        assert (other / "state.json").read_text() == _VALID


_IGNORES_TERM = """#!/bin/sh
# A background job that ignores SIGTERM: only the KILL escalation stops it.
sh -c 'trap "" TERM; while :; do sleep 1; done' </dev/null >/dev/null 2>&1 &
printf '%s' "$!" > "$AGENT_PROBE/background.pid"
printf '%s' "$$" > "$AGENT_PROBE/agent.pid"
mkdir -p -- "$(dirname "$CTRLRELAY_STATE_FILE")"
printf '{"version":"1","status":"DONE","session_id":"%s",' \\
  "$CTRLRELAY_SESSION_ID" > "$CTRLRELAY_STATE_FILE"
printf '"timestamp":"2026-11-07T00:00:00Z","summary":"done"}' \\
  >> "$CTRLRELAY_STATE_FILE"
exit 0
"""


class TestTheGraceEndsInAKill:
    def test_a_member_that_ignores_sigterm_is_killed(
        self, tmp_path: Path, request: pytest.FixtureRequest
    ) -> None:
        result, probe = _spawn_isolated(tmp_path, request, agent=_IGNORES_TERM)

        assert result["status"] == "DONE"
        background = int((probe / "background.pid").read_text())
        assert _wait_dead(background), "a TERM-ignoring job outlived the grace"


class TestAReusedGroupIdIsNotSignalled:
    def test_after_reaping_a_live_process_at_that_pid_is_not_ours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once the relay has reaped its child, the group the child led has
        no leader. A process that answers at that pid is somebody else's,
        which took the number after it was freed — possibly half an hour
        later, when a descendant held the pipes open until the timeout. It
        must not be signalled."""
        import asyncio

        from ctrlrelay.core import dispatcher

        async def reaped() -> asyncio.subprocess.Process:
            p = await asyncio.create_subprocess_exec(
                "true", start_new_session=True,
            )
            await p.wait()
            return p

        proc = asyncio.run(reaped())
        assert dispatcher._agent_group(proc) == proc.pid  # leaderless: ours

        # Now pretend the number was reused by a new group leader.
        real_getpgid = os.getpgid
        monkeypatch.setattr(
            dispatcher.os, "getpgid",
            lambda pid: pid if pid == proc.pid else real_getpgid(pid),
        )
        assert dispatcher._agent_group(proc) is None
