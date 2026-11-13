"""``ctrlrelay poller forget`` (#417).

The poller's seen-record is deliberately permanent, and before this there was
no way to undo one: an issue recorded for a reason that no longer applied was
invisible for ever, and the only cure was editing ``poller_state.json`` by
hand -- which two of the tool's own error messages told operators to do.

Every test here points ``paths.state_db`` at ``tmp_path``. The sample config
fixture points it at ``~/.ctrlrelay/state.db``, and a test that used it would
write a real node's poller state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ctrlrelay.cli import app

runner = CliRunner()


@pytest.fixture
def config_and_state(
    sample_config_dict: dict, tmp_path: Path, monkeypatch
) -> tuple[Path, Path]:
    """A config whose state lives under ``tmp_path``, and its state file.

    ``_live_poller_pid`` is stubbed to "no poller" by default. Without it these
    tests would pass or fail on whether the machine running them happens to
    have a poller pid file, which is not what any of them is about; the two
    that ARE about liveness override it.
    """
    monkeypatch.setattr("ctrlrelay.cli._live_poller_pid", lambda _c: None)
    sample_config_dict["paths"] = dict(sample_config_dict.get("paths") or {})
    sample_config_dict["paths"]["state_db"] = str(tmp_path / "state.db")
    config_path = tmp_path / "orchestrator.yaml"
    config_path.write_text(yaml.dump(sample_config_dict))
    state_file = tmp_path / "poller_state.json"
    return config_path, state_file


def _write_state(state_file: Path, seen: dict) -> None:
    """The poller's own format. Note there is no ``recheck_after``: it is
    in-memory only and never persisted, which is why a parked refusal cannot
    outlive a restart -- and why this command has only ``seen_issues`` to
    undo."""
    state_file.write_text(json.dumps({
        "seen_issues": seen,
        "last_poll": "2026-11-12T22:28:10+00:00",
    }))


def test_forget_removes_the_seen_record(config_and_state) -> None:
    config_path, state_file = config_and_state
    _write_state(state_file, {"AInvirion/task-orchestrator": [523, 547]})

    result = runner.invoke(
        app, ["poller", "forget", "AInvirion/task-orchestrator#523",
              "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    assert "forgotten" in result.output
    saved = json.loads(state_file.read_text())
    # Only the named one, and the state file is still the poller's own format.
    assert saved["seen_issues"]["AInvirion/task-orchestrator"] == [547]


def test_forget_refuses_while_a_poller_is_live_and_changes_nothing(
    config_and_state, monkeypatch
) -> None:
    """Refused BEFORE any read or write, and the state file is untouched.

    Not caution: this command reads the file, edits its own copy and writes it
    back, so a record the daemon accepted between that read and that write is
    lost -- and a lost acceptance means the issue is handled twice. There is no
    IPC and no lock, so the contract is that the daemon is stopped.

    Liveness is the real mechanism, not the pid file's existence: this uses a
    pid that IS alive (our own), so the test cannot pass merely because a file
    was created.
    """
    import os

    config_path, state_file = config_and_state
    before = json.dumps({"seen_issues": {"owner/repo": [1]},
                         "last_poll": "2026-11-12T22:28:10+00:00"})
    state_file.write_text(before)
    monkeypatch.setattr(
        "ctrlrelay.cli._live_poller_pid", lambda _c: os.getpid(),
    )

    result = runner.invoke(
        app, ["poller", "forget", "owner/repo#1", "--config", str(config_path)],
    )

    assert result.exit_code == 1, result.output
    assert "Stop it first" in result.output
    assert "poller stop" in result.output
    # The state file is byte-identical: nothing was read-modify-written.
    assert state_file.read_text() == before


def test_liveness_is_signal_zero_not_the_pid_files_existence(
    tmp_path: Path, monkeypatch
) -> None:
    """A stale pid file must not read as a running poller.

    Both directions, because only one of them is safe to get wrong: a stale
    file refusing a safe edit is an annoyance, a running daemon read as absent
    is the lost-update this command exists to avoid.
    """
    import os

    from ctrlrelay.cli import _live_poller_pid

    pid_file = tmp_path / "poller.pid"
    monkeypatch.setattr("ctrlrelay.cli._get_poller_pid_file", lambda _c: pid_file)

    # Alive: our own process.
    pid_file.write_text(str(os.getpid()))
    assert _live_poller_pid(None) == os.getpid()

    # Stale: a pid that cannot be running. PID 0 and negatives are refused
    # outright because os.kill(0, 0) signals the whole process group.
    pid_file.write_text("0")
    assert _live_poller_pid(None) is None
    pid_file.write_text("-1")
    assert _live_poller_pid(None) is None

    # Unreadable or absent tells us nothing, which is not a poller.
    pid_file.write_text("not a pid")
    assert _live_poller_pid(None) is None
    pid_file.unlink()
    assert _live_poller_pid(None) is None


def test_forget_says_so_when_there_was_nothing_to_forget(config_and_state) -> None:
    """It must not report success for a no-op: "forgotten" on an issue the
    poller already considers would send an operator looking in the wrong place
    for why pickup is not happening."""
    config_path, state_file = config_and_state
    _write_state(state_file, {"owner/repo": [1]})

    result = runner.invoke(
        app, ["poller", "forget", "owner/repo#2", "--config", str(config_path)],
    )

    assert result.exit_code == 1
    assert "not recorded" in result.output
    assert "gate" in result.output  # points at the real reason instead


@pytest.mark.parametrize("bad", ["owner/repo", "ownerrepo#1", "owner/repo#x",
                                 "owner/sub/repo#1", "#5"])
def test_forget_refuses_a_reference_it_cannot_parse(config_and_state, bad) -> None:
    config_path, _ = config_and_state
    result = runner.invoke(
        app, ["poller", "forget", bad, "--config", str(config_path)],
    )
    assert result.exit_code == 2, f"{bad!r} was accepted: {result.output}"
    assert "Not an issue reference" in result.output


def test_forget_reports_a_missing_state_file(config_and_state) -> None:
    config_path, state_file = config_and_state
    assert not state_file.exists()
    result = runner.invoke(
        app, ["poller", "forget", "owner/repo#1", "--config", str(config_path)],
    )
    assert result.exit_code == 1
    assert "No poller state" in result.output
