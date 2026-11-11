"""Built-in agent presets (#384): Copilot without a hand-written manifest."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ctrlrelay.core.agent_presets import PRESET_NAMES, preset_manifest
from ctrlrelay.core.config import AgentConfig, Config, PresetOverrides
from ctrlrelay.core.dispatcher import (
    RepoAgentRouter,
    make_agent_dispatcher,
    make_repo_agent_dispatcher,
    select_agent,
    select_enrolled_agent,
)
from ctrlrelay.core.generic_cli import GenericCliDispatcher

SESSION = "8e34dff3-65de-4e73-bd7b-5b56f3b8c8c7"


def _config(agent: dict, repos: list[dict]) -> Config:
    return Config.model_validate({
        "node_id": "testbox",
        "paths": {
            "state_db": "/tmp/ctrlrelay-test/state.db",
            "worktrees": "/tmp/ctrlrelay-test/worktrees",
            "bare_repos": "/tmp/ctrlrelay-test/bare",
            "contexts": "/tmp/ctrlrelay-test/contexts",
            "skills": "/tmp/ctrlrelay-test/skills",
            "repo_root": "/tmp/ctrlrelay-test/projects",
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {
                "inbox": "/tmp/ctrlrelay-test/inbox.txt",
                "outbox": "/tmp/ctrlrelay-test/outbox.txt",
            },
        },
        "agent": agent,
        "repos": repos,
    })


def _copilot(**overrides) -> GenericCliDispatcher:
    # An absolute binary, so the test does not depend on Copilot being
    # installed where the suite runs.
    overrides.setdefault("binary", "/opt/copilot")
    cfg = AgentConfig(type="copilot", presets={"copilot": overrides})
    adapter = make_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)
    assert isinstance(adapter, GenericCliDispatcher)
    return adapter


def _argvs(adapter: GenericCliDispatcher, tmp_path: Path) -> tuple[list, list]:
    fresh = adapter._build_argv(
        prompt="do it", session_id="s1", state_file=tmp_path / "s.json",
        resume_session_id=None,
    )
    resumed = adapter._build_argv(
        prompt="answer", session_id="s1", state_file=tmp_path / "s.json",
        resume_session_id=SESSION,
    )
    return fresh, resumed


class TestARepoSelectsThePresetByName:
    def test_repo_agent_type_copilot_runs_copilot_with_no_plugin_entry(self) -> None:
        """The way an operator arrives at it: one line on a repo."""
        cfg = _config(
            {"type": "claude", "presets": {"copilot": {"binary": "/opt/copilot"}}},
            [{"name": "owner/plain"}, {"name": "owner/pilot", "agent_type": "copilot"}],
        )
        assert cfg.agent.plugins == {}
        router = make_repo_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)
        assert isinstance(router, RepoAgentRouter)

        selected = select_agent(router, repo="owner/pilot", requested_type=None)

        assert isinstance(selected.dispatcher, GenericCliDispatcher)
        assert selected.identity.plugin_id == "generic:copilot"
        assert selected.dispatcher.binary == "/opt/copilot"
        assert select_agent(
            router, repo="owner/plain", requested_type=None,
        ).identity.plugin_id == "claude"

    def test_agent_type_copilot_works_as_the_node_default(self) -> None:
        assert _copilot().agent_type == "copilot"

    def test_a_preset_is_assignable_without_a_plugin_entry(self) -> None:
        cfg = _config(
            {
                "type": "claude",
                "presets": {"copilot": {"binary": "/opt/copilot"}},
                "assignable_types": ["copilot"],
            },
            [{"name": "owner/repo"}],
        )
        router = make_repo_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)

        selected = select_agent(router, repo="owner/repo", requested_type="copilot")

        assert selected.identity.plugin_id == "generic:copilot"

    def test_an_unselected_preset_is_still_enrolled_for_a_pinned_resume(self) -> None:
        """Built lazily, like an unrouted plugin (#380): a run pinned to
        copilot must be able to come back after the repo stops naming it."""
        cfg = _config(
            {"type": "claude", "presets": {"copilot": {"binary": "/opt/copilot"}}},
            [{"name": "owner/repo"}],
        )
        dispatcher = make_repo_agent_dispatcher(
            cfg, outcome_hook=None, usage_store=None,
        )

        selection = select_enrolled_agent(dispatcher, "copilot")

        assert selection.identity.plugin_id == "generic:copilot"


class TestTheSafeFlagsCannotBeDropped:
    @pytest.mark.parametrize("overrides", [{}, {"model": "gpt-5.1"}])
    def test_fresh_and_resume_argv_keep_the_github_mcp_off(
        self, tmp_path, overrides,
    ) -> None:
        fresh, resumed = _argvs(_copilot(**overrides), tmp_path)

        assert fresh[:3] == ["/opt/copilot", "-p", "do it"]
        assert "--resume" not in fresh
        assert resumed[resumed.index("--resume") + 1] == SESSION
        for argv in (fresh, resumed):
            assert "--disable-builtin-mcps" in argv
            assert argv[argv.index("--output-format") + 1] == "json"

    def test_a_model_override_appends_model_to_both_argument_lists(
        self, tmp_path,
    ) -> None:
        fresh, resumed = _argvs(_copilot(model="gpt-5.1"), tmp_path)

        assert fresh[-2:] == ["--model", "gpt-5.1"]
        assert resumed[-2:] == ["--model", "gpt-5.1"]

    def test_no_model_means_no_model_flag(self, tmp_path) -> None:
        fresh, resumed = _argvs(_copilot(), tmp_path)

        assert "--model" not in fresh
        assert "--model" not in resumed

    @pytest.mark.parametrize("key", ["args", "resume_args", "session_id_path", "extra"])
    def test_overrides_cannot_reach_the_arguments(self, key) -> None:
        with pytest.raises(ValidationError, match=key):
            AgentConfig(presets={"copilot": {key: ["--yolo"]}})

    def test_overrides_cannot_widen_the_provider_allowance(self) -> None:
        """A well-formed entry, so the refusal is the override rule and not
        the env_allow pattern check (#390)."""
        with pytest.raises(ValidationError, match="env_allow"):
            AgentConfig(presets={"copilot": {"env_allow": ["OPENAI_API_KEY"]}})

    def test_the_timeout_override_reaches_the_adapter(self) -> None:
        assert _copilot(default_timeout_seconds=99).default_timeout == 99

    def test_without_a_timeout_override_the_node_default_applies(self) -> None:
        cfg = AgentConfig(
            type="copilot", default_timeout_seconds=1234,
            presets={"copilot": {"binary": "/opt/copilot"}},
        )
        adapter = make_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)

        assert adapter.default_timeout == 1234


class TestConfigRefusals:
    def test_a_hand_written_copilot_plugin_is_refused(self) -> None:
        with pytest.raises(
            ValidationError,
            match=(
                "copilot is a built-in preset; set agent.presets.copilot "
                "to change binary, model or timeout"
            ),
        ):
            AgentConfig(plugins={"copilot": {"binary": "/opt/copilot"}})

    def test_an_unknown_preset_is_refused(self) -> None:
        with pytest.raises(ValidationError, match=r"agent.presets\['nope'\]"):
            AgentConfig(presets={"nope": {}})

    @pytest.mark.parametrize(
        "model", ["{prompt}", "{resume_session_id}", "x}", "--allow-all-paths"],
    )
    def test_a_model_cannot_carry_a_placeholder_or_a_flag(self, model) -> None:
        with pytest.raises(ValidationError, match="must be a model name"):
            AgentConfig(presets={"copilot": {"model": model}})

    def test_an_ordinary_model_name_is_accepted(self) -> None:
        cfg = AgentConfig(presets={"copilot": {"model": "claude-sonnet-4.5"}})
        assert cfg.presets["copilot"].model == "claude-sonnet-4.5"

    @pytest.mark.parametrize("field", ["binary", "model"])
    def test_a_blank_override_is_refused(self, field) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            PresetOverrides(**{field: "  "})

    def test_other_plugin_names_are_still_accepted(self) -> None:
        cfg = AgentConfig(plugins={"example": {"binary": "/opt/example"}})
        assert set(cfg.plugins) == {"example"}


class TestIdentity:
    def test_preset_identity_is_generic_copilot_at_the_preset_version(self) -> None:
        identity = _copilot().agent_identity

        assert identity.plugin_id == "generic:copilot"
        assert identity.plugin_version == "1"
        assert identity.plugin_version == preset_manifest("copilot").plugin_version

    def test_the_arguments_are_pinned_to_the_version(self) -> None:
        """A tripwire: changing the preset's arguments fails here. When it
        does, bump the preset's version and update both expected values
        together, so a run pinned to the old arguments is refused rather
        than resumed with new ones."""
        m = preset_manifest("copilot")
        assert (m.plugin_version, m.args, m.resume_args) == (
            "1",
            ["-p", "{prompt}", "--allow-all-tools", "--disable-builtin-mcps",
             "--output-format", "json"],
            ["-p", "{prompt}", "--allow-all-tools", "--disable-builtin-mcps",
             "--output-format", "json", "--resume", "{resume_session_id}"],
        )

    def test_the_factory_carries_the_preset_version_into_the_identity(
        self, monkeypatch,
    ) -> None:
        """"1" is also the adapter's default version, so read a version
        no default could supply."""
        import dataclasses

        from ctrlrelay.core import agent_presets

        bumped = dataclasses.replace(agent_presets._PRESETS["copilot"], version="7")
        monkeypatch.setitem(agent_presets._PRESETS, "copilot", bumped)

        assert _copilot().agent_identity.plugin_version == "7"

    def test_a_version_bump_changes_the_fingerprint(self) -> None:
        """So a run pinned to old arguments is refused, not resumed."""
        base = preset_manifest("copilot", PresetOverrides(binary="/opt/copilot"))
        bumped = base.model_copy(update={"plugin_version": "2"})

        def fingerprint(m) -> str:
            return GenericCliDispatcher(
                agent_type="copilot", binary=m.binary, args=tuple(m.args),
                resume_args=tuple(m.resume_args),
                session_id_path=m.session_id_path,
                plugin_version=m.plugin_version, usage_store=None,
            ).agent_identity.fingerprint

        assert fingerprint(base) != fingerprint(bumped)

    def test_the_session_id_is_read_from_copilot_output(self) -> None:
        stdout = "\n".join([
            '{"type":"assistant.message","data":{"content":"OK"},'
            '"id":"0eddbbb8-b1f6-45bd-a578-7ab4fa608fd1"}',
            '{"type":"result","timestamp":"2026-11-07T18:44:59.436Z",'
            f'"sessionId":"{SESSION}","exitCode":0,'
            '"usage":{"premiumRequests":1}}',
        ])
        assert _copilot()._parse_agent_session_id(stdout) == SESSION


class TestTheCopilotPresetGetsOnlyItsOwnProviderKey:
    """#390: through a real spawn, so the env checked is the one
    ``build_agent_env`` hands ``create_subprocess_exec``."""

    @pytest.mark.asyncio
    async def test_keeps_its_token_and_drops_other_providers_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import AsyncMock, patch

        for name, value in {
            "COPILOT_GITHUB_TOKEN": "copilot-value",
            "OPENAI_API_KEY": "openai-value",
            "ANTHROPIC_API_KEY": "anthropic-value",
            "GH_TOKEN": "gh-value",
        }.items():
            monkeypatch.setenv(name, value)
        proc = AsyncMock()
        proc.communicate.return_value = (b"", b"")
        proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=proc) as ex:
            await _copilot().spawn_session(
                session_id="s1", prompt="p", working_dir=tmp_path,
                state_file=tmp_path / "state.json", github_access=False,
            )

        env = ex.call_args.kwargs["env"]
        assert env["COPILOT_GITHUB_TOKEN"] == "copilot-value"
        assert "OPENAI_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "GH_TOKEN" not in env

    def test_the_allowance_is_fixed_in_the_preset(self) -> None:
        assert preset_manifest("copilot").env_allow == ["COPILOT_GITHUB_TOKEN"]


def test_unknown_names_have_no_preset() -> None:
    assert {"copilot", "codex"} <= PRESET_NAMES
    assert preset_manifest("copilot") is not None
    assert preset_manifest("nope") is None


CODEX_THREAD = "01a1c827-b87a-7f20-b0cb-eeb5318361b3"


def _codex(**overrides) -> GenericCliDispatcher:
    overrides.setdefault("binary", "/opt/codex")
    cfg = AgentConfig(type="codex", presets={"codex": overrides})
    adapter = make_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)
    assert isinstance(adapter, GenericCliDispatcher)
    return adapter


class TestCodexPreset:
    """The `codex` preset (#385), from the runs recorded on #383."""

    def test_repo_agent_type_codex_runs_codex_with_no_plugin_entry(self) -> None:
        cfg = _config(
            {"type": "claude", "presets": {"codex": {"binary": "/opt/codex"}}},
            [{"name": "owner/plain"}, {"name": "owner/cx", "agent_type": "codex"}],
        )
        assert cfg.agent.plugins == {}
        router = make_repo_agent_dispatcher(cfg, outcome_hook=None, usage_store=None)

        selected = select_agent(router, repo="owner/cx", requested_type=None)

        assert selected.identity.plugin_id == "generic:codex"
        assert selected.dispatcher.binary == "/opt/codex"

    def test_fresh_and_resume_argv(self, tmp_path) -> None:
        fresh, resumed = _argvs(_codex(), tmp_path)

        assert fresh == [
            "/opt/codex", "exec", "--json",
            "--dangerously-bypass-approvals-and-sandbox", "do it",
        ]
        assert resumed == [
            "/opt/codex", "exec", "resume", "--json",
            "--dangerously-bypass-approvals-and-sandbox", SESSION, "answer",
        ]

    def test_a_model_goes_right_after_the_subcommand(self, tmp_path) -> None:
        """Not at the end, where Codex would read it after the positionals."""
        fresh, resumed = _argvs(_codex(model="gpt-5.1-codex"), tmp_path)

        assert fresh[:4] == ["/opt/codex", "exec", "-m", "gpt-5.1-codex"]
        assert resumed[:5] == [
            "/opt/codex", "exec", "resume", "-m", "gpt-5.1-codex",
        ]
        assert fresh[-1] == "do it"
        assert resumed[-2:] == [SESSION, "answer"]

    def test_the_session_id_is_the_thread_id_from_codex_output(self) -> None:
        # Event shapes recorded from Codex CLI 0.167.0 on #383; the token
        # counts are illustrative.
        stdout = "\n".join([
            f'{{"type":"thread.started","thread_id":"{CODEX_THREAD}"}}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed","usage":{"input_tokens":5120,'
            '"cached_input_tokens":4096,"cache_write_input_tokens":0,'
            '"output_tokens":210,"reasoning_output_tokens":64}}',
        ])
        assert _codex()._parse_agent_session_id(stdout) == CODEX_THREAD

    def test_a_hand_written_codex_plugin_is_refused(self) -> None:
        with pytest.raises(
            ValidationError,
            match=(
                "codex is a built-in preset; set agent.presets.codex "
                "to change binary, model or timeout"
            ),
        ):
            AgentConfig(plugins={"codex": {"binary": "/opt/codex"}})

    def test_the_arguments_are_pinned_to_the_version(self) -> None:
        """A tripwire, as for copilot: bump the version with the arguments."""
        m = preset_manifest("codex")
        assert (m.plugin_version, m.args, m.resume_args, m.session_id_path) == (
            "1",
            ["exec", "--json", "--dangerously-bypass-approvals-and-sandbox",
             "{prompt}"],
            ["exec", "resume", "--json",
             "--dangerously-bypass-approvals-and-sandbox",
             "{resume_session_id}", "{prompt}"],
            "thread_id",
        )
        assert m.instructions_files == ["AGENTS.md"]

    def test_identity(self) -> None:
        identity = _codex().agent_identity
        assert identity.plugin_id == "generic:codex"
        assert identity.plugin_version == "1"
