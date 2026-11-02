"""The parts of the daemon and the CLI that must know about #286.

A correct adapter nobody builds is a correct adapter that never runs, so
these tests are about the wiring rather than the adapter: the daemon and
the one-shot commands must go through `make_repo_agent_dispatcher`, and
`ctrlrelay node check` must stop probing a subscription that a non-claude
deployment does not have.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "ctrlrelay"


class TestEveryConstructionSiteRoutes:
    def test_the_cli_never_calls_the_single_backend_factory(self) -> None:
        """`make_agent_dispatcher` builds ONE backend from
        `agent.type` and knows nothing about `repos[].agent_type`. A
        call site still using it is a repo running on a backend nobody
        chose — and since the default is claude, that failure is
        invisible until a repo meant for the local model quietly spends
        the subscription (or the reverse, which opens bad pull
        requests).

        Read from the source rather than from a mock: this asserts that
        no site was MISSED, and a mock can only tell you about the sites
        you remembered to write a test for.
        """
        tree = ast.parse((SRC / "cli.py").read_text())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "make_repo_agent_dispatcher" in called
        assert "make_agent_dispatcher" not in called

    def test_the_only_importer_of_the_single_factory_is_the_routing_one(
        self,
    ) -> None:
        names: list[str] = []
        for path in SRC.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == "make_agent_dispatcher":
                            names.append(str(path.relative_to(SRC)))
        # `core/__init__.py` re-exports it for downstream callers; the
        # routing factory in `core/dispatcher.py` calls it directly.
        # Nothing else should.
        assert sorted(set(names)) == ["core/__init__.py"]


class TestNodeCheck:
    def _config(self, agent_type: str):
        return SimpleNamespace(
            node_id="node-a",
            fleet=SimpleNamespace(peers=[], timeout_seconds=1),
            agent=SimpleNamespace(type=agent_type, binary="claude"),
        )

    @pytest.mark.asyncio
    async def test_a_skipped_agent_check_can_say_why(self) -> None:
        from ctrlrelay.core.node_check import run_checks

        async def login() -> str:
            return "a"

        checks = await run_checks(
            self._config("opencode"),
            github_login=login,
            agent_probe=None,
            fetch=lambda *a, **k: {},
            agent_skip_reason="skipped: agent.type is 'opencode'",
        )
        [row] = [c for c in checks if c.name == "agent login"]
        assert row.ok is None
        assert row.detail == "skipped: agent.type is 'opencode'"

    @pytest.mark.asyncio
    async def test_the_default_reason_is_unchanged(self) -> None:
        """`--no-agent` was the only reason to skip before this. It must
        still read as itself, not as the new one."""
        from ctrlrelay.core.node_check import run_checks

        async def login() -> str:
            return "a"

        checks = await run_checks(
            self._config("claude"),
            github_login=login,
            agent_probe=None,
            fetch=lambda *a, **k: {},
        )
        [row] = [c for c in checks if c.name == "agent login"]
        assert row.detail == "skipped (--no-agent)"

    def _config_file(self, tmp_path: Path, agent_type: str) -> Path:
        cfg = tmp_path / "orchestrator.yaml"
        cfg.write_text(
            'version: "1"\nnode_id: "node-a"\n'
            f"agent: {{type: {agent_type}}}\n"
            f'paths: {{state_db: "{tmp_path}/s.db", worktrees: "{tmp_path}/w", '
            f'bare_repos: "{tmp_path}/r", contexts: "{tmp_path}/c", '
            f'skills: "{tmp_path}/k"}}\n'
            f"transport: {{type: file_mock, file_mock: "
            f'{{inbox: "{tmp_path}/i", outbox: "{tmp_path}/o"}}}}\n'
        )
        return cfg

    def test_the_command_skips_the_subscription_probe_off_claude(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without this the check runs `claude -p` on a deployment that
        has no claude, and reports a FAILED agent login telling the
        operator to log in to a CLI they do not use."""
        from typer.testing import CliRunner

        import ctrlrelay.core.node_check as nc
        from ctrlrelay.cli import app

        seen: dict = {}

        async def fake_run_checks(config, **kw):
            seen.update(kw)
            return [nc.Check("node_id", True, "node-a")]

        monkeypatch.setattr(nc, "run_checks", fake_run_checks)
        result = CliRunner().invoke(
            app,
            ["node", "check", "-c", str(self._config_file(tmp_path, "opencode"))],
        )
        assert result.exit_code == 0, result.output
        assert seen["agent_probe"] is None
        assert "opencode" in seen["agent_skip_reason"]
        assert "claude subscription" in seen["agent_skip_reason"]

    def test_the_command_still_probes_on_claude(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        import ctrlrelay.core.node_check as nc
        from ctrlrelay.cli import app

        seen: dict = {}

        async def fake_run_checks(config, **kw):
            seen.update(kw)
            return [nc.Check("node_id", True, "node-a")]

        monkeypatch.setattr(nc, "run_checks", fake_run_checks)
        result = CliRunner().invoke(
            app,
            ["node", "check", "-c", str(self._config_file(tmp_path, "claude"))],
        )
        assert result.exit_code == 0, result.output
        assert seen["agent_probe"] is not None
        assert seen["agent_skip_reason"] is None


class TestTheExampleConfigIsLoadable:
    def test_the_shipped_example_still_validates(self) -> None:
        """The example is what an operator copies. A commented block
        that does not load is a bug they find at startup, and the
        opencode block is new text in it."""
        import yaml

        from ctrlrelay.core.config import Config

        path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "orchestrator.yaml.example"
        )
        Config.model_validate(yaml.safe_load(path.read_text()))

    def test_the_example_documents_the_opencode_block(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "orchestrator.yaml.example"
        )
        text = path.read_text()
        assert "opencode" in text
        assert "agent_type" in text
