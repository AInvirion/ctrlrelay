"""Tests for the version command."""

import re
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from ctrlrelay import __version__
from ctrlrelay.cli import app

runner = CliRunner()

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([.\-+].+)?$")


class TestVersionCommand:
    def test_version_command_succeeds(self) -> None:
        """ctrlrelay version should exit with code 0."""
        result = runner.invoke(app, ["version"])

        assert result.exit_code == 0

    def test_version_command_prints_version(self) -> None:
        """ctrlrelay version should print the package version."""
        result = runner.invoke(app, ["version"])

        assert __version__ in result.output

    def test_version_matches_semver_shape(self) -> None:
        """__version__ should look like a real version, not a literal placeholder."""
        assert SEMVER_RE.match(__version__), (
            f"__version__={__version__!r} does not match semver-like shape"
        )

    def test_version_matches_pyproject(self) -> None:
        """__version__ must match pyproject.toml so published wheels don't lie.

        Catches the drift that caused issue #94: bumping pyproject.toml on each
        release while forgetting to bump a hardcoded string in __init__.py.
        """
        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        pyproject = tomllib.loads(pyproject_path.read_text())
        expected = pyproject["project"]["version"]

        assert __version__ == expected, (
            f"__version__={__version__!r} diverges from pyproject.toml version "
            f"{expected!r} — did a release bump one but not the other?"
        )
