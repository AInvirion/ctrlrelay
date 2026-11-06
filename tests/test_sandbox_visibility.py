"""Each machine's agent sandbox mode is visible (#305): in the node block of
``/api/status``, on a peer's card, on the page, and as one warning at poller
start when the agent runs unconfined."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from ctrlrelay.core.config import SANDBOX_MODES, Config


def _config_dict(tmp_path: Path, sandbox: str) -> dict:
    return {
        "version": "1",
        "node_id": "node-a",
        "timezone": "UTC",
        "paths": {
            "state_db": str(tmp_path / "state.db"),
            "worktrees": str(tmp_path / "worktrees"),
            "bare_repos": str(tmp_path / "repos"),
            "contexts": str(tmp_path / "contexts"),
            "skills": str(tmp_path / "skills"),
        },
        "agent": {"binary": "claude", "sandbox": sandbox},
        "transport": {"type": "file_mock", "file_mock": {
            "inbox": str(tmp_path / "in"), "outbox": str(tmp_path / "out"),
        }},
        "dashboard": {"enabled": False},
        "schedules": {"weekly_summary": {"enabled": False}},
        "repos": [{"name": "o/r", "local_path": str(tmp_path / "o-r")}],
    }


def test_the_modes_are_the_ones_the_config_accepts() -> None:
    assert SANDBOX_MODES == ("none", "bwrap")


@pytest.mark.parametrize("mode", ["none", "bwrap"])
def test_the_node_block_carries_this_machines_mode(tmp_path: Path, mode: str) -> None:
    from ctrlrelay.web.server import StatusServer

    server = StatusServer.__new__(StatusServer)
    server.config = Config.model_validate(_config_dict(tmp_path, mode))
    assert server.node()["sandbox"] == mode


@pytest.mark.parametrize(("sent", "kept"), [
    ("none", "none"),
    ("bwrap", "bwrap"),
    # An older peer sends nothing: unknown, never "off".
    (None, None),
    ("BWRAP", None),
    ("docker", None),
    (1, None),
    (["bwrap"], None),
])
def test_a_peers_mode_is_kept_only_when_it_is_one_there_is(sent, kept) -> None:
    from ctrlrelay.web.fleet import _clean_node

    raw = {"name": "b", "state": "idle"}
    if sent is not None:
        raw["sandbox"] = sent
    assert _clean_node(raw)["sandbox"] == kept


@pytest.mark.parametrize(("mode", "line"), [
    ("bwrap", ["muted", "sandbox bwrap"]),
    ("none", ["drift",
              "sandbox off: the agent has this user's full access (agent.sandbox: none)"]),
    (None, ["muted", "sandbox unknown"]),
    ("weird", ["muted", "sandbox unknown"]),
])
def test_the_page_says_off_in_the_warning_colour_and_unknown_as_unknown(
    mode, line,
) -> None:
    from tests.test_web_server import _run_js

    got = _run_js(
        ["sandboxLine"],
        "(function () { el = function (tag, cls, text) { return [cls, text]; };"
        f" return sandboxLine({json.dumps(mode)}); }})()",
    )
    assert got == line


def test_every_machine_card_has_the_sandbox_line() -> None:
    from tests.test_web_server import _js_function, _script

    body = _js_function(_script(), "machineCard")
    assert "box.appendChild(sandboxLine(node.sandbox));" in body


@pytest.mark.parametrize(("mode", "warned"), [("none", True), ("bwrap", False)])
def test_the_warning_text_only_for_none(tmp_path: Path, mode: str, warned: bool) -> None:
    from ctrlrelay.cli import sandbox_off_warning

    text = sandbox_off_warning(Config.model_validate(_config_dict(tmp_path, mode)))
    assert (text is not None) is warned
    if warned:
        # Says what to do, not only that something is off.
        assert "Set agent.sandbox: bwrap" in text


@pytest.mark.parametrize("mode", ["none", "bwrap"])
def test_poller_start_logs_it_once_only_when_off(tmp_path: Path, mode: str) -> None:
    """Through the real ``poller start --foreground`` wiring."""
    import ctrlrelay.cli as cli_mod

    if mode == "bwrap":
        from ctrlrelay.core.agent_env import resolve_bwrap

        try:
            resolve_bwrap()
        except Exception:
            pytest.skip("bwrap is not installed, so a bwrap poller cannot start here")

    path = tmp_path / "orchestrator.yaml"
    path.write_text(yaml.dump(_config_dict(tmp_path, mode)))
    events: list[str] = []
    real_log_event = cli_mod.log_event

    def recording(logger, event, **fields):
        events.append(event)
        return real_log_event(logger, event, **fields)

    scheduler = MagicMock()
    scheduler.shutdown = AsyncMock()
    reached: list[bool] = []

    async def fake_poll_loop(**_kw):
        # The poll loop starts after the start-up lines.
        reached.append(True)

    gh_user = subprocess.CompletedProcess(args=["gh"], returncode=0,
                                          stdout="me\n", stderr="")
    with (
        patch("ctrlrelay.core.github._find_gh", return_value="gh"),
        patch("subprocess.run", return_value=gh_user),
        patch("ctrlrelay.core.scheduler.make_scheduler", return_value=scheduler),
        patch("ctrlrelay.core.poller.run_poll_loop", new=fake_poll_loop),
        patch("ctrlrelay.core.poller.IssuePoller.seed_current", new=AsyncMock()),
        patch("ctrlrelay.cli.log_event", new=recording),
    ):
        result = CliRunner().invoke(
            cli_mod.app, ["poller", "start", "--foreground", "--config", str(path)],
        )
    assert reached, result.output
    assert events.count("poller.sandbox_off") == (1 if mode == "none" else 0)
