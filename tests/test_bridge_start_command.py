"""``ctrlrelay bridge start --foreground`` builds the bridge it is meant to.

0.16.0 shipped with this command constructing ``BridgeServer`` without the
``node`` argument #248 made required, so the bridge exited at startup on
every machine. The ``python -m ctrlrelay.bridge`` path had it; this one did
not, and nothing ran the command.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.mark.parametrize("transport,shared", [
    ("telegram", False),
    ("mattermost", True),
])
def test_bridge_start_names_this_machine(
    tmp_path: Path, monkeypatch, transport: str, shared: bool,
) -> None:
    import ctrlrelay.bridge as bridge
    from ctrlrelay.cli import app

    built: dict = {}

    class FakeServer:
        def __init__(self, socket_path, handler, state_db=None, *, node, shared=False):
            built.update(node=node, shared=shared)

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(bridge, "BridgeServer", FakeServer)
    monkeypatch.setattr(bridge, "make_handler", lambda _t: object())
    if transport == "telegram":
        block = (
            f'transport: {{type: telegram, telegram: {{bot_token_env: "T", '
            f'chat_id: 1, socket_path: "{tmp_path}/b.sock"}}}}\n'
        )
    else:
        block = (
            f'transport: {{type: mattermost, mattermost: {{url: "https://chat.example.com", '
            f'bot_token_env: "T", channel_id: "abcdefghijklmnopqrstuvwxyz", '
            f'shared: true, socket_path: "{tmp_path}/b.sock"}}}}\n'
        )
    cfg = tmp_path / "orchestrator.yaml"
    cfg.write_text(
        'version: "1"\nnode_id: "node-a"\n'
        f'paths: {{state_db: "{tmp_path}/s.db", worktrees: "{tmp_path}/w", '
        f'bare_repos: "{tmp_path}/r", contexts: "{tmp_path}/c", skills: "{tmp_path}/k"}}\n'
        + block
    )
    result = CliRunner().invoke(
        app, ["bridge", "start", "--foreground", "-c", str(cfg)],
    )
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert built == {"node": "node-a", "shared": shared}
