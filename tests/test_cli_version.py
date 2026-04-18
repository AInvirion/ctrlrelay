"""Tests for the version command."""

from typer.testing import CliRunner

from ctrlrelay import __version__
from ctrlrelay.cli import app

runner = CliRunner()


class TestVersionCommand:
    def test_version_command_succeeds(self) -> None:
        """ctrlrelay version should exit with code 0."""
        result = runner.invoke(app, ["version"])

        assert result.exit_code == 0

    def test_version_command_prints_version(self) -> None:
        """ctrlrelay version should print the package version."""
        result = runner.invoke(app, ["version"])

        assert __version__ in result.output
