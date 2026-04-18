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
