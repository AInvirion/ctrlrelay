"""Tests for configuration loading and validation."""

from pathlib import Path

import pytest
import yaml

from ctrlrelay.core.config import AutomationConfig, Config, ConfigError, load_config


class TestConfigLoading:
    def test_load_valid_config(self, sample_config_file: Path) -> None:
        """Loading a valid config file should return a Config object."""
        config = load_config(sample_config_file)
        assert isinstance(config, Config)
        assert config.node_id == "test-node"
        assert config.timezone == "UTC"

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        """Loading a non-existent file should raise ConfigError."""
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nonexistent.yaml")

    def test_load_invalid_yaml_raises(self, tmp_path: Path) -> None:
        """Loading invalid YAML should raise ConfigError."""
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("{{{{invalid yaml")
        with pytest.raises(ConfigError, match="parse"):
            load_config(bad_file)

    def test_config_validates_required_fields(self, tmp_path: Path) -> None:
        """Config missing required fields should raise validation error."""
        incomplete = tmp_path / "incomplete.yaml"
        incomplete.write_text("version: '1'\n")
        with pytest.raises(ConfigError, match="validation"):
            load_config(incomplete)


class TestConfigPaths:
    def test_paths_expand_tilde(self, sample_config_file: Path) -> None:
        """Path fields should expand ~ to home directory."""
        config = load_config(sample_config_file)
        assert "~" not in str(config.paths.state_db)
        assert str(config.paths.state_db).startswith("/")


class TestSchedulesConfig:
    def test_default_secops_cron_is_six_am_daily(
        self, sample_config_file: Path
    ) -> None:
        """When the config omits schedules, secops_cron must default to 6am
        daily — the target from the original design doc."""
        config = load_config(sample_config_file)
        assert config.schedules.secops_cron == "0 6 * * *"

    def test_secops_cron_override_accepted(
        self, sample_config_dict: dict, tmp_path: Path
    ) -> None:
        """Valid cron expressions should pass through untouched."""
        import yaml

        sample_config_dict["schedules"] = {"secops_cron": "0 6 * * 1"}
        cfg_path = tmp_path / "orchestrator.yaml"
        cfg_path.write_text(yaml.dump(sample_config_dict))
        config = load_config(cfg_path)
        assert config.schedules.secops_cron == "0 6 * * 1"

    def test_invalid_cron_raises_config_error(
        self, sample_config_dict: dict, tmp_path: Path
    ) -> None:
        """A malformed cron expression must fail fast at config load time,
        not silently disable the scheduled job at runtime."""
        import yaml

        sample_config_dict["schedules"] = {"secops_cron": "not a cron expression"}
        cfg_path = tmp_path / "orchestrator.yaml"
        cfg_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="invalid cron"):
            load_config(cfg_path)


class TestAgentConfig:
    """The `agent:` section replaces the legacy `claude:` section.
    Schema must accept the new key, keep the old one as a deprecated
    alias, and still expose `config.claude` as a property so existing
    callers keep working."""

    def test_agent_section_is_accepted(
        self, sample_config_dict: dict, tmp_path: Path
    ) -> None:
        import yaml

        sample_config_dict.pop("claude", None)
        sample_config_dict["agent"] = {
            "type": "claude",
            "binary": "/opt/homebrew/bin/claude",
            "default_timeout_seconds": 600,
        }
        cfg_path = tmp_path / "orchestrator.yaml"
        cfg_path.write_text(yaml.dump(sample_config_dict))
        config = load_config(cfg_path)
        assert config.agent.type == "claude"
        assert str(config.agent.binary) == "/opt/homebrew/bin/claude"
        assert config.agent.default_timeout_seconds == 600

    def test_legacy_claude_key_is_aliased_to_agent(
        self, sample_config_dict: dict, tmp_path: Path
    ) -> None:
        """The YAML `claude:` key is migrated to `agent:` at load time
        with a DeprecationWarning, so pre-migration configs keep
        working until the operator renames."""
        import warnings

        import yaml

        sample_config_dict["claude"] = {
            "binary": "/usr/local/bin/claude",
            "default_timeout_seconds": 900,
        }
        cfg_path = tmp_path / "orchestrator.yaml"
        cfg_path.write_text(yaml.dump(sample_config_dict))

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            config = load_config(cfg_path)

        assert config.agent.binary == "/usr/local/bin/claude"
        assert config.agent.default_timeout_seconds == 900
        # DeprecationWarning must fire so the operator sees the hint.
        msgs = [str(w.message) for w in captured if issubclass(w.category, DeprecationWarning)]
        assert any("'claude:' is deprecated" in m for m in msgs), msgs

    def test_claude_property_mirrors_agent(
        self, sample_config_file: Path
    ) -> None:
        """`config.claude` must still work at the Python attribute
        level — existing callers that haven't migrated stay green."""
        config = load_config(sample_config_file)
        assert config.claude is config.agent


class TestMakeAgentDispatcher:
    """The factory is the seam where future agent backends plug in.
    Today only `claude` is wired up; other types must fail loudly so
    a config typo doesn't silently fall back to Claude."""

    def test_claude_type_returns_claude_dispatcher(self) -> None:
        from ctrlrelay.core.config import AgentConfig
        from ctrlrelay.core.dispatcher import ClaudeDispatcher, make_agent_dispatcher

        cfg = AgentConfig(type="claude", binary="claude", default_timeout_seconds=60)
        adapter = make_agent_dispatcher(cfg)
        assert isinstance(adapter, ClaudeDispatcher)
        assert adapter.default_timeout == 60

    def test_unknown_type_raises_not_implemented_with_hint(self) -> None:
        from ctrlrelay.core.config import AgentConfig
        from ctrlrelay.core.dispatcher import make_agent_dispatcher

        cfg = AgentConfig(type="codex")
        with pytest.raises(NotImplementedError) as excinfo:
            make_agent_dispatcher(cfg)
        msg = str(excinfo.value).lower()
        assert "codex" in msg
        assert "agentadapter" in msg or "adapter" in msg


class TestTimezoneValidation:
    """Regression for codex [P2]: invalid IANA zones must fail at load,
    not at poller startup. Since the scheduler feeds timezone directly
    into APScheduler, a typo would otherwise crash the daemon with
    ZoneInfoNotFoundError."""

    def test_invalid_timezone_raises_config_error(
        self, sample_config_dict: dict, tmp_path: Path
    ) -> None:
        import yaml

        sample_config_dict["timezone"] = "America/Santiagoo"  # typo
        cfg_path = tmp_path / "orchestrator.yaml"
        cfg_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="unknown timezone"):
            load_config(cfg_path)

    def test_valid_iana_timezone_accepted(
        self, sample_config_dict: dict, tmp_path: Path
    ) -> None:
        import yaml

        sample_config_dict["timezone"] = "America/Santiago"
        cfg_path = tmp_path / "orchestrator.yaml"
        cfg_path.write_text(yaml.dump(sample_config_dict))
        config = load_config(cfg_path)
        assert config.timezone == "America/Santiago"


class TestAutomationExcludeLabels:
    """exclude_labels surfaces per-repo so the poller can skip operator-only issues."""

    def test_default_exclude_labels(self) -> None:
        """Default covers the common 'not for the agent' keywords from #91."""
        auto = AutomationConfig()
        assert auto.exclude_labels == ["manual", "operator", "instruction"]

    def test_exclude_labels_empty_list_is_valid(self) -> None:
        """Operators can opt out of any default exclusions."""
        auto = AutomationConfig(exclude_labels=[])
        assert auto.exclude_labels == []

    def test_config_without_exclude_labels_key_gets_default(
        self, sample_config_dict: dict, tmp_path: Path
    ) -> None:
        """Legacy configs without exclude_labels load fine and get the default."""
        sample_config_dict["repos"] = [
            {
                "name": "owner/repo",
                "local_path": "~/Projects/repo",
                "automation": {"dependabot_patch": "auto"},
            }
        ]
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(yaml.dump(sample_config_dict))

        config = load_config(cfg_path)

        assert config.repos[0].automation.exclude_labels == [
            "manual",
            "operator",
            "instruction",
        ]

    def test_exclude_labels_override_from_yaml(
        self, sample_config_dict: dict, tmp_path: Path
    ) -> None:
        """YAML override wins over the built-in default."""
        sample_config_dict["repos"] = [
            {
                "name": "owner/repo",
                "local_path": "~/Projects/repo",
                "automation": {"exclude_labels": ["no-agent", "wontfix"]},
            }
        ]
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(yaml.dump(sample_config_dict))

        config = load_config(cfg_path)

        assert config.repos[0].automation.exclude_labels == ["no-agent", "wontfix"]
