"""Tests for configuration loading and validation."""

from pathlib import Path

import pytest

from ctrlrelay.core.config import Config, ConfigError, load_config


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
