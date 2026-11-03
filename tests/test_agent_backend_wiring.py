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

    def _config_file(
        self, tmp_path: Path, agent_type: str,
        repo_agent_types: tuple[str | None, ...] = (),
    ) -> Path:
        cfg = tmp_path / "orchestrator.yaml"
        repos = ""
        if repo_agent_types:
            repos = "repos:\n"
            for i, t in enumerate(repo_agent_types):
                repos += f'  - name: "owner/repo{i}"\n    local_path: "{tmp_path}/r{i}"\n'
                if t is not None:
                    repos += f"    agent_type: {t}\n"
        cfg.write_text(
            'version: "1"\nnode_id: "node-a"\n'
            f"agent: {{type: {agent_type}}}\n"
            + repos +
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

    def _seen_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        agent_type: str, repo_agent_types: tuple[str | None, ...],
    ) -> dict:
        from typer.testing import CliRunner

        import ctrlrelay.core.node_check as nc
        from ctrlrelay.cli import app

        seen: dict = {}

        async def fake_run_checks(config, **kw):
            seen.update(kw)
            seen["repo_count"] = len(config.repos)
            return [nc.Check("node_id", True, "node-a")]

        monkeypatch.setattr(nc, "run_checks", fake_run_checks)
        path = self._config_file(tmp_path, agent_type, repo_agent_types)
        result = CliRunner().invoke(app, ["node", "check", "-c", str(path)])
        assert result.exit_code == 0, result.output
        # The repos reached the command: a config the loader dropped
        # them from would make every assertion below about the global
        # type alone.
        assert seen["repo_count"] == len(repo_agent_types)
        return seen

    def test_global_opencode_with_a_repo_on_claude_still_probes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Global opencode, one repo on claude: that repo dispatches
        through claude, so its login must be checked here. Skipping on
        the global type alone hid an expired login until that repo's
        next job failed."""
        seen = self._seen_for(
            tmp_path, monkeypatch, "opencode", (None, "claude"),
        )
        assert seen["agent_probe"] is not None
        assert seen["agent_skip_reason"] is None

    def test_repos_that_all_stay_off_claude_still_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sibling of the case above, so the pair says which
        behaviour belongs to which: repos present, none naming claude,
        and the skip still happens and says so."""
        seen = self._seen_for(
            tmp_path, monkeypatch, "opencode", (None, "opencode"),
        )
        assert seen["agent_probe"] is None
        assert "no repository routes to claude" in seen["agent_skip_reason"]

    def test_global_claude_with_a_repo_on_opencode_still_probes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reverse mix: every repo but one is on claude, so the
        claude login matters and the probe runs."""
        seen = self._seen_for(
            tmp_path, monkeypatch, "claude", (None, "opencode"),
        )
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
