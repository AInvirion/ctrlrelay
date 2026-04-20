"""Tests for the `bridge start` / `poller start` daemonize-by-default behavior."""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from ctrlrelay.cli import app

runner = CliRunner()


def _make_live_proc(pid: int) -> MagicMock:
    """Build a subprocess.Popen mock that behaves like a still-running child.

    `proc.wait(timeout=...)` must raise TimeoutExpired so the daemon-start
    path interprets the child as healthy.
    """
    proc = MagicMock()
    proc.pid = pid
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="<test>", timeout=1.0)
    return proc

# Typer/Rich injects ANSI escapes around flag names (e.g. "--foreground" becomes
# "-" + ESC[...m + "-foreground" + ESC[0m), so substring matches on the raw
# output fail under a TTY-emulating runner. Strip escapes before asserting.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


@pytest.fixture
def telegram_config(tmp_path: Path) -> Path:
    socket_path = tmp_path / "ctrlrelay.sock"
    config = {
        "version": "1",
        "node_id": "test-node",
        "timezone": "UTC",
        "paths": {
            "state_db": str(tmp_path / "state.db"),
            "worktrees": str(tmp_path / "worktrees"),
            "bare_repos": str(tmp_path / "repos"),
            "contexts": str(tmp_path / "contexts"),
            "skills": str(tmp_path / "skills"),
        },
        "claude": {
            "binary": "claude",
            "default_timeout_seconds": 1800,
            "output_format": "json",
        },
        "transport": {
            "type": "telegram",
            "telegram": {
                "socket_path": str(socket_path),
                "bot_token_env": "CTRLRELAY_TEST_TOKEN",
                "chat_id": 12345,
            },
        },
        "dashboard": {"enabled": False},
        "repos": [],
    }
    config_path = tmp_path / "orchestrator.yaml"
    config_path.write_text(yaml.dump(config))
    return config_path


@pytest.fixture
def bot_token_env():
    with patch.dict(os.environ, {"CTRLRELAY_TEST_TOKEN": "dummy-token"}):
        yield


class TestBridgeStartDefault:
    """`ctrlrelay bridge start` (no flag) must daemonize, not block."""

    def test_default_spawns_subprocess_and_returns(
        self, telegram_config: Path, bot_token_env: None, tmp_path: Path
    ) -> None:
        fake_proc = _make_live_proc(99999)
        with patch("subprocess.Popen", return_value=fake_proc) as popen:
            result = runner.invoke(
                app, ["bridge", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code == 0, result.output
        assert popen.called, "default invocation must spawn a detached subprocess"
        _args, kwargs = popen.call_args
        assert kwargs.get("start_new_session") is True
        assert f"PID {fake_proc.pid}" in result.output

        pid_file = Path(str(telegram_config.parent / "ctrlrelay.pid"))
        assert pid_file.exists()
        assert pid_file.read_text().strip() == str(fake_proc.pid)

    def test_default_does_not_run_server_inline(
        self, telegram_config: Path, bot_token_env: None
    ) -> None:
        """Default path must NOT import/run BridgeServer in-process."""
        fake_proc = _make_live_proc(42)
        with (
            patch("subprocess.Popen", return_value=fake_proc),
            patch("ctrlrelay.bridge.BridgeServer") as server_cls,
        ):
            result = runner.invoke(
                app, ["bridge", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code == 0, result.output
        assert not server_cls.called, (
            "default path must NOT instantiate BridgeServer inline"
        )


class TestBridgeStartForeground:
    """`--foreground` runs the server inline and writes our own PID to the file."""

    def test_foreground_runs_server_inline(
        self, telegram_config: Path, bot_token_env: None
    ) -> None:
        server_instance = MagicMock()

        async def _fake_start():
            return None

        async def _fake_stop():
            return None

        server_instance.start = _fake_start
        server_instance.stop = _fake_stop
        with (
            patch("subprocess.Popen") as popen,
            patch(
                "ctrlrelay.bridge.BridgeServer", return_value=server_instance
            ) as server_cls,
        ):
            result = runner.invoke(
                app,
                [
                    "bridge",
                    "start",
                    "--foreground",
                    "--config",
                    str(telegram_config),
                ],
            )

        assert result.exit_code == 0, result.output
        assert not popen.called, "--foreground must NOT spawn a subprocess"
        assert server_cls.called, "--foreground must run BridgeServer inline"

    def test_foreground_cleans_up_pid_file_on_exit(
        self, telegram_config: Path, bot_token_env: None
    ) -> None:
        server_instance = MagicMock()

        async def _fake_start():
            return None

        async def _fake_stop():
            return None

        server_instance.start = _fake_start
        server_instance.stop = _fake_stop
        with patch("ctrlrelay.bridge.BridgeServer", return_value=server_instance):
            result = runner.invoke(
                app,
                [
                    "bridge",
                    "start",
                    "--foreground",
                    "--config",
                    str(telegram_config),
                ],
            )

        assert result.exit_code == 0, result.output
        pid_file = telegram_config.parent / "ctrlrelay.pid"
        assert not pid_file.exists(), (
            "foreground mode must clean up the PID file on graceful exit"
        )


class TestBridgeForegroundShutdownOrdering:
    """Regression for codex [P2]: when the foreground bridge is cancelled
    (SIGTERM / SIGINT), server.stop() must actually run and complete before
    the event loop closes. Fire-and-forget `create_task(server.stop())` from
    the signal handler does NOT guarantee that ordering."""

    def test_stop_completes_when_start_is_cancelled(
        self, telegram_config: Path, bot_token_env: None
    ) -> None:
        import asyncio

        calls: list[str] = []

        async def fake_start():
            calls.append("start")
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                calls.append("start_cancelled")
                raise

        async def fake_stop():
            await asyncio.sleep(0)
            calls.append("stop_done")

        server_instance = MagicMock()
        server_instance.start = fake_start
        server_instance.stop = fake_stop

        # Monkeypatch asyncio.new_event_loop to inject a cancellation just
        # after run_until_complete starts, simulating a SIGTERM arrival.
        orig_new_event_loop = asyncio.new_event_loop

        def tracked_new_loop():
            loop = orig_new_event_loop()
            orig_rc = loop.run_until_complete

            def rc_with_cancel(task):
                loop.call_soon(task.cancel)
                return orig_rc(task)

            loop.run_until_complete = rc_with_cancel
            return loop

        with (
            patch("ctrlrelay.bridge.BridgeServer", return_value=server_instance),
            patch("asyncio.new_event_loop", side_effect=tracked_new_loop),
        ):
            result = runner.invoke(
                app,
                [
                    "bridge",
                    "start",
                    "--foreground",
                    "--config",
                    str(telegram_config),
                ],
            )

        assert result.exit_code == 0, result.output
        assert "start_cancelled" in calls, "start() must observe the cancel"
        assert "stop_done" in calls, (
            "server.stop() must run to completion before the loop closes "
            "(codex [P2] regression — stale socket would otherwise remain)"
        )


class TestBridgeStartDaemonSecrets:
    """Regression for codex [P1]: the Telegram bot token must NEVER appear
    in the daemon child's argv (readable via `ps` / /proc/*/cmdline)."""

    def test_bot_token_not_in_argv(
        self, telegram_config: Path, bot_token_env: None
    ) -> None:
        fake_proc = _make_live_proc(31415)
        with patch("subprocess.Popen", return_value=fake_proc) as popen:
            result = runner.invoke(
                app, ["bridge", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code == 0, result.output
        cmd = popen.call_args[0][0]
        assert "dummy-token" not in cmd, (
            "bot token leaked into daemon argv — would be visible to anyone "
            "reading `ps`. Pass the env-var name and inherit the process env "
            "instead."
        )
        assert "--bot-token-env" in cmd, (
            "daemon should tell the child which env var holds the token"
        )


class TestBridgeStartDaemonFailFast:
    """Regression for codex [P2]: if the spawned child exits immediately
    (bad env, crash-on-import, missing dep), the parent must report failure
    rather than printing 'Bridge started (PID N)' and dropping the user."""

    def test_reports_failure_when_child_exits_nonzero(
        self, telegram_config: Path, bot_token_env: None
    ) -> None:
        fake_proc = MagicMock()
        fake_proc.pid = 2718
        fake_proc.wait.return_value = 1  # child exited with code 1
        fake_proc.returncode = 1
        with patch("subprocess.Popen", return_value=fake_proc):
            result = runner.invoke(
                app, ["bridge", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code != 0
        assert "failed to start" in result.output.lower()

    def test_clean_child_exit_zero_is_not_a_failure(
        self, telegram_config: Path, bot_token_env: None
    ) -> None:
        """Regression for codex [P3]: exit 0 must not be classified as a
        crash (symmetry with the poller fix)."""
        fake_proc = MagicMock()
        fake_proc.pid = 2719
        fake_proc.wait.return_value = 0
        fake_proc.returncode = 0
        with patch("subprocess.Popen", return_value=fake_proc):
            result = runner.invoke(
                app, ["bridge", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code == 0, result.output
        assert "failed to start" not in result.output.lower()


class TestBridgeStartDaemonNoStartupRace:
    """Regression for codex [P2]: PID file must be claimed BEFORE the 1-second
    liveness probe, so a second concurrent `start` can't spawn a duplicate."""

    def test_pid_file_claimed_before_liveness_probe(
        self, telegram_config: Path, bot_token_env: None
    ) -> None:
        probe_seen_pid = {"value": None}
        pid_file = telegram_config.parent / "ctrlrelay.pid"

        def wait_with_assert(timeout: float) -> int:
            probe_seen_pid["value"] = (
                pid_file.read_text().strip() if pid_file.exists() else None
            )
            raise subprocess.TimeoutExpired(cmd="<test>", timeout=timeout)

        fake_proc = MagicMock()
        fake_proc.pid = 99001
        fake_proc.wait.side_effect = wait_with_assert
        with patch("subprocess.Popen", return_value=fake_proc):
            result = runner.invoke(
                app, ["bridge", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code == 0, result.output
        assert probe_seen_pid["value"] == str(fake_proc.pid), (
            "PID file must be written before proc.wait() so a concurrent "
            "`start` in the probe window sees the claim"
        )

    def test_pid_file_removed_if_child_crashes_on_start(
        self, telegram_config: Path, bot_token_env: None
    ) -> None:
        pid_file = telegram_config.parent / "ctrlrelay.pid"
        fake_proc = MagicMock()
        fake_proc.pid = 99002
        fake_proc.wait.return_value = 5
        fake_proc.returncode = 5
        with patch("subprocess.Popen", return_value=fake_proc):
            result = runner.invoke(
                app, ["bridge", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code != 0
        assert not pid_file.exists(), (
            "PID file must be removed when the child crashed on start, so "
            "subsequent `start` is not blocked by a stale claim"
        )


class TestBridgeStartAlreadyRunning:
    def test_refuses_when_live_pid_in_file(
        self, telegram_config: Path, bot_token_env: None, tmp_path: Path
    ) -> None:
        pid_file = tmp_path / "ctrlrelay.pid"
        pid_file.write_text(str(os.getpid()))

        with patch("subprocess.Popen") as popen:
            result = runner.invoke(
                app, ["bridge", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code != 0
        assert "already running" in result.output.lower()
        assert not popen.called


class TestPollerStartDefault:
    """`ctrlrelay poller start` (no flag) must daemonize and pass --foreground to its child."""

    def test_default_spawns_child_with_foreground_flag(
        self, telegram_config: Path, tmp_path: Path
    ) -> None:
        fake_proc = _make_live_proc(77777)
        with patch("subprocess.Popen", return_value=fake_proc) as popen:
            result = runner.invoke(
                app, ["poller", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code == 0, result.output
        assert popen.called
        cmd = popen.call_args[0][0]
        assert "--foreground" in cmd, (
            "parent must pass --foreground to the child so the child runs inline "
            "instead of recursively daemonizing"
        )
        assert f"PID {fake_proc.pid}" in result.output


class TestPollerStartDaemonFailFast:
    """Regression for codex [P2]: if the spawned child exits immediately
    (missing gh, bad config, etc), the parent must NOT claim success."""

    def test_reports_failure_when_child_exits_nonzero(
        self, telegram_config: Path
    ) -> None:
        fake_proc = MagicMock()
        fake_proc.pid = 4242
        fake_proc.wait.return_value = 2  # e.g. gh not found
        fake_proc.returncode = 2
        with patch("subprocess.Popen", return_value=fake_proc):
            result = runner.invoke(
                app, ["poller", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code != 0
        assert "failed to start" in result.output.lower()

    def test_clean_child_exit_zero_is_not_a_failure(
        self, telegram_config: Path
    ) -> None:
        """Regression for codex [P3]: if the child exits 0 within 1s (e.g.
        `repos: []` no-op), the parent must NOT report 'failed to start'."""
        fake_proc = MagicMock()
        fake_proc.pid = 4343
        fake_proc.wait.return_value = 0
        fake_proc.returncode = 0
        with patch("subprocess.Popen", return_value=fake_proc):
            result = runner.invoke(
                app, ["poller", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code == 0, result.output
        assert "failed to start" not in result.output.lower()


class TestPollerStartDaemonNoStartupRace:
    """Regression for codex [P1]: PID file must be claimed BEFORE the 1-second
    liveness probe, so a second concurrent `start` can't spawn a duplicate
    poller (which would process the same issue twice)."""

    def test_pid_file_claimed_before_liveness_probe(
        self, telegram_config: Path, tmp_path: Path
    ) -> None:
        pid_file = tmp_path / "poller.pid"
        probe_seen_pid = {"value": None}

        def wait_with_assert(timeout: float) -> int:
            probe_seen_pid["value"] = (
                pid_file.read_text().strip() if pid_file.exists() else None
            )
            raise subprocess.TimeoutExpired(cmd="<test>", timeout=timeout)

        fake_proc = MagicMock()
        fake_proc.pid = 55001
        fake_proc.wait.side_effect = wait_with_assert
        with patch("subprocess.Popen", return_value=fake_proc):
            result = runner.invoke(
                app, ["poller", "start", "--config", str(telegram_config)]
            )

        assert result.exit_code == 0, result.output
        assert probe_seen_pid["value"] == str(fake_proc.pid), (
            "PID file must be written before proc.wait() so a concurrent "
            "`start` in the probe window sees the claim"
        )


class TestPollerStartForegroundSigtermEarly:
    """Regression for codex [P2]: SIGTERM handlers must be installed BEFORE
    `_find_gh`/`gh api user`/`seed_current()`, otherwise a supervisor stop
    during startup bypasses the `finally` that unlinks poller.pid."""

    def test_sigterm_installed_before_startup_work(
        self, telegram_config: Path
    ) -> None:
        call_order: list[str] = []

        def record_signal(sig: int, handler: object) -> None:
            if sig == signal.SIGTERM:
                call_order.append("signal.signal(SIGTERM)")

        def record_find_gh() -> None:
            call_order.append("_find_gh")
            raise RuntimeError("short-circuit")

        with (
            patch("signal.signal", side_effect=record_signal),
            patch("ctrlrelay.core.github._find_gh", side_effect=record_find_gh),
        ):
            runner.invoke(
                app,
                [
                    "poller",
                    "start",
                    "--foreground",
                    "--config",
                    str(telegram_config),
                ],
            )

        assert "signal.signal(SIGTERM)" in call_order, (
            "poller foreground must install a SIGTERM handler"
        )
        assert "_find_gh" in call_order, (
            "test invariant: startup must reach _find_gh"
        )
        assert call_order.index("signal.signal(SIGTERM)") < call_order.index(
            "_find_gh"
        ), (
            "SIGTERM handler must be installed BEFORE _find_gh() / the rest "
            "of the startup work, so a supervisor stop during startup still "
            "runs the PID-file cleanup finally"
        )


class TestPollerStartForeground:
    def test_foreground_flag_accepted(self) -> None:
        result = runner.invoke(app, ["poller", "start", "--help"])
        assert result.exit_code == 0
        assert "--foreground" in _plain(result.output)

    def test_foreground_tolerates_its_own_pid_in_pidfile(
        self, telegram_config: Path, tmp_path: Path
    ) -> None:
        """Regression for codex P1: when the daemon parent writes the child's
        PID to the file before spawning `--foreground`, the child must NOT
        treat its own PID as a conflict and exit."""
        pid_file = tmp_path / "poller.pid"
        pid_file.write_text(str(os.getpid()))

        # Force the body of the foreground branch to short-circuit before it
        # tries to talk to GitHub or start polling; we only care that the
        # self-PID guard doesn't fire.
        with patch(
            "ctrlrelay.core.github._find_gh",
            side_effect=RuntimeError("short-circuit"),
        ) as find_gh:
            result = runner.invoke(
                app,
                [
                    "poller",
                    "start",
                    "--foreground",
                    "--config",
                    str(telegram_config),
                ],
            )

        assert "already running" not in result.output.lower(), (
            "foreground child must not treat its own PID in the file as a "
            "conflict (codex [P1] regression)"
        )
        assert find_gh.called, (
            "execution must proceed past the PID-file guard into the poll "
            "setup path"
        )
