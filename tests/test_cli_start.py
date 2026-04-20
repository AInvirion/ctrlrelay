"""Tests for the `bridge start` / `poller start` daemonize-by-default behavior."""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from ctrlrelay.cli import app

runner = CliRunner()

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
        fake_proc = MagicMock()
        fake_proc.pid = 99999
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
        fake_proc = MagicMock()
        fake_proc.pid = 42
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

        server_instance.start = _fake_start
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

        server_instance.start = _fake_start
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
        fake_proc = MagicMock()
        fake_proc.pid = 77777
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


class TestPollerStartForeground:
    def test_foreground_flag_accepted(self) -> None:
        result = runner.invoke(app, ["poller", "start", "--help"])
        assert result.exit_code == 0
        assert "--foreground" in _plain(result.output)
