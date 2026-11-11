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

    def test_the_session_id_comes_from_the_result_event(self) -> None:
        stdout = "\n".join([
            '{"type":"assistant.message","data":{"content":"OK"},'
            '"id":"0eddbbb8-b1f6-45bd-a578-7ab4fa608fd1"}',
            '{"type":"result","timestamp":"2026-11-07T18:44:59.436Z",'
            f'"sessionId":"{SESSION}","exitCode":0,'
            '"usage":{"premiumRequests":1}}',
        ])
        assert _copilot()._parse_agent_session_id(stdout) == SESSION


def test_unknown_names_have_no_preset() -> None:
    assert "copilot" in PRESET_NAMES
    assert preset_manifest("copilot") is not None
    assert preset_manifest("nope") is None
