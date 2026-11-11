"""Each agent backend gets only its own provider credentials (#390).

Every test builds the adapter the way the daemon does — from a real
``AgentConfig`` through ``make_agent_dispatcher`` — and reads the
environment handed to ``create_subprocess_exec``, which is the thing the
child actually receives. Each "does not see" is paired with a variable the
same run DOES see, so an adapter that passed an empty environment, or
never ran, cannot pass.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from ctrlrelay.core.config import AgentConfig
from ctrlrelay.core.dispatcher import make_agent_dispatcher

_PROVIDER_KEYS = {
    "ANTHROPIC_API_KEY": "anthropic-value-390",
    "CLAUDE_CODE_OAUTH_TOKEN": "claude-oauth-value-390",
    "OPENAI_API_KEY": "openai-value-390",
    "DEEPSEEK_API_KEY": "deepseek-value-390",
    "COPILOT_GITHUB_TOKEN": "copilot-value-390",
}
_GITHUB_AND_RELAY = {
    "GH_TOKEN": "gh-value-390",
    "CTRLRELAY_MATTERMOST_TOKEN": "bot-value-390",
}
# Not a provider key: must pass through untouched, or the rule is an
# allowlist of the whole environment and a build loses its toolchain.
_ORDINARY = {"JAVA_HOME": "/opt/jdk-390"}

_PLUGIN = {
    "binary": "/bin/true",
    "args": ["{prompt}"],
}


def _seed(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {**_PROVIDER_KEYS, **_GITHUB_AND_RELAY, **_ORDINARY}.items():
        monkeypatch.setenv(k, v)


def _adapter(agent: dict):
    return make_agent_dispatcher(
        AgentConfig(**agent), outcome_hook=None, usage_store=None,
    )


async def _spawned_env(
    adapter, tmp_path: Path, *, github_access: bool, log_target: str,
) -> tuple[dict[str, str], dict]:
    """``(env at exec, fields of the credentials event)``."""
    proc = AsyncMock()
    proc.communicate.return_value = (b"", b"")
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=proc) as ex, \
            patch(log_target) as log:
        await adapter.spawn_session(
            session_id="s-390",
            prompt="p",
            working_dir=tmp_path,
            state_file=tmp_path / "state.json",
            github_access=github_access,
        )
    events = [
        c.kwargs for c in log.call_args_list
        if c.args[1] == "dispatcher.session.credentials"
    ]
    assert len(events) == 1
    return dict(ex.call_args.kwargs["env"]), events[0]


_CLAUDE_LOG = "ctrlrelay.core.dispatcher.log_event"
_GENERIC_LOG = "ctrlrelay.core.generic_cli.log_event"


class TestClaudeGetsOnlyAnthropicKeys:
    @pytest.mark.asyncio
    async def test_a_claude_run_does_not_see_another_providers_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(monkeypatch)
        adapter = _adapter({"type": "claude", "binary": "/bin/true"})

        env, event = await _spawned_env(
            adapter, tmp_path, github_access=False, log_target=_CLAUDE_LOG,
        )

        assert "OPENAI_API_KEY" not in env
        assert "DEEPSEEK_API_KEY" not in env
        assert "COPILOT_GITHUB_TOKEN" not in env
        assert env["ANTHROPIC_API_KEY"] == "anthropic-value-390"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth-value-390"
        assert env["JAVA_HOME"] == "/opt/jdk-390"
        # Names, never values.
        for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "COPILOT_GITHUB_TOKEN"):
            assert name in event["stripped_env"]
        assert "ANTHROPIC_API_KEY" not in event["stripped_env"]
        assert not any("value-390" in n for n in event["stripped_env"])


class TestGenericPluginGetsOnlyWhatItsManifestAllows:
    @pytest.mark.asyncio
    async def test_no_provider_key_reaches_a_plugin_that_declares_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(monkeypatch)
        adapter = _adapter({"type": "example", "plugins": {"example": _PLUGIN}})

        env, event = await _spawned_env(
            adapter, tmp_path, github_access=False, log_target=_GENERIC_LOG,
        )

        assert not set(_PROVIDER_KEYS) & set(env)
        assert set(_PROVIDER_KEYS) <= set(event["stripped_env"])
        assert env["JAVA_HOME"] == "/opt/jdk-390"

    @pytest.mark.asyncio
    async def test_env_allow_lets_exactly_the_named_keys_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(monkeypatch)
        manifest = {**_PLUGIN, "env_allow": ["ANTHROPIC_API_KEY", "OPENAI_*"]}
        adapter = _adapter({"type": "example", "plugins": {"example": manifest}})

        env, event = await _spawned_env(
            adapter, tmp_path, github_access=False, log_target=_GENERIC_LOG,
        )

        assert env["ANTHROPIC_API_KEY"] == "anthropic-value-390"
        assert env["OPENAI_API_KEY"] == "openai-value-390"
        assert "DEEPSEEK_API_KEY" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert "DEEPSEEK_API_KEY" in event["stripped_env"]
        assert "ANTHROPIC_API_KEY" not in event["stripped_env"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("github_access", [False, True])
    async def test_a_key_set_through_extra_env_gets_no_exemption(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        github_access: bool,
    ) -> None:
        """The only way a provider key reaches an agent is its backend's
        allowance: an adapter's ``extra_env`` cannot route around it."""
        from ctrlrelay.core.generic_cli import GenericCliDispatcher

        _seed(monkeypatch)
        adapter = GenericCliDispatcher(
            agent_type="example", binary="/bin/true", usage_store=None,
            extra_env={"OPENAI_API_KEY": "set-by-adapter", "EXAMPLE": "kept"},
            env_allow=("DEEPSEEK_API_KEY",),
        )

        env, event = await _spawned_env(
            adapter, tmp_path, github_access=github_access,
            log_target=_GENERIC_LOG,
        )

        assert "OPENAI_API_KEY" not in env
        assert "OPENAI_API_KEY" in event["stripped_env"]
        # The filter ran and the allowance works on this same adapter.
        assert env["DEEPSEEK_API_KEY"] == "deepseek-value-390"
        assert env["EXAMPLE"] == "kept"


class TestOpencodeGetsWhatItsBlockAllows:
    @pytest.mark.asyncio
    async def test_opencode_env_allow_is_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(monkeypatch)
        adapter = _adapter({
            "type": "opencode",
            "opencode": {"binary": "/bin/true", "env_allow": ["DEEPSEEK_API_KEY"]},
        })

        env, event = await _spawned_env(
            adapter, tmp_path, github_access=False, log_target=_CLAUDE_LOG,
        )

        assert env["DEEPSEEK_API_KEY"] == "deepseek-value-390"
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" in event["stripped_env"]


class TestSecopsKeepsGitHubButNotRelaySecrets:
    """``github_access=True``: the operator's GitHub login stays, on
    purpose; the relay's own secrets and other providers' keys do not."""

    @pytest.mark.asyncio
    async def test_claude_secops_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(monkeypatch)
        adapter = _adapter({"type": "claude", "binary": "/bin/true"})

        env, event = await _spawned_env(
            adapter, tmp_path, github_access=True, log_target=_CLAUDE_LOG,
        )

        assert env["GH_TOKEN"] == "gh-value-390"
        assert "CTRLRELAY_MATTERMOST_TOKEN" not in env
        assert "OPENAI_API_KEY" not in env
        assert env["ANTHROPIC_API_KEY"] == "anthropic-value-390"
        assert sorted(k for k in env if k.startswith("CTRLRELAY_")) == [
            "CTRLRELAY_SESSION_ID", "CTRLRELAY_STATE_FILE",
        ]
        assert event["github_access"] is True
        assert "CTRLRELAY_MATTERMOST_TOKEN" in event["stripped_env"]
        assert "OPENAI_API_KEY" in event["stripped_env"]
        assert "GH_TOKEN" not in event["stripped_env"]

    @pytest.mark.asyncio
    async def test_generic_secops_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(monkeypatch)
        adapter = _adapter({"type": "example", "plugins": {"example": _PLUGIN}})

        env, event = await _spawned_env(
            adapter, tmp_path, github_access=True, log_target=_GENERIC_LOG,
        )

        assert env["GH_TOKEN"] == "gh-value-390"
        assert "CTRLRELAY_MATTERMOST_TOKEN" not in env
        assert not set(_PROVIDER_KEYS) & set(env)
        assert env["CTRLRELAY_SESSION_ID"] == "s-390"
        assert "CTRLRELAY_MATTERMOST_TOKEN" in event["stripped_env"]


class TestEnvAllowIsValidatedAtLoad:
    @pytest.mark.parametrize("entry", [
        "*", "openai_api_key", "OPENAI*KEY", "GH_TOKEN", "GITHUB_*",
        "CTRLRELAY_MATTERMOST_TOKEN", "CTRLRELAY_*", "SSH_AUTH_SOCK", "",
    ])
    def test_refused_on_a_plugin_and_on_opencode(self, entry: str) -> None:
        with pytest.raises(ValidationError, match="env_allow"):
            AgentConfig(plugins={"example": {**_PLUGIN, "env_allow": [entry]}})
        with pytest.raises(ValidationError, match="env_allow"):
            AgentConfig(opencode={"env_allow": [entry]})

    def test_names_and_prefixes_are_accepted(self) -> None:
        cfg = AgentConfig(plugins={
            "example": {**_PLUGIN, "env_allow": ["OPENAI_API_KEY", "OPENAI_*"]},
        })
        assert cfg.plugins["example"].env_allow == ["OPENAI_API_KEY", "OPENAI_*"]
