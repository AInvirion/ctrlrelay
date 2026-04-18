"""Tests for dev pipeline CLI commands."""

import re

from typer.testing import CliRunner

runner = CliRunner()

# Typer's Rich-backed help renderer injects ANSI escapes (bold/underline/color)
# around CLI flag names, which can split "--daemon" into "-" + bold + "-daemon"
# in the raw output. Strip escape sequences before asserting on substrings.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


class TestRunDevCommand:
    def test_run_dev_requires_issue(self) -> None:
        """Should require issue number."""
        from ctrlrelay.cli import app

        result = runner.invoke(app, ["run", "dev"])

        assert result.exit_code != 0
        assert "Missing option" in result.output or "required" in result.output.lower()

    def test_run_dev_help(self) -> None:
        """Should show help for run dev command."""
        from ctrlrelay.cli import app

        result = runner.invoke(app, ["run", "dev", "--help"])

        assert result.exit_code == 0
        plain = _plain(result.output)
        assert "--issue" in plain
        assert "--repo" in plain


class TestPollerCommands:
    def test_poller_start_help(self) -> None:
        """Should show help for poller start."""
        from ctrlrelay.cli import app

        result = runner.invoke(app, ["poller", "start", "--help"])

        assert result.exit_code == 0
        assert "--daemon" in _plain(result.output)

    def test_poller_status(self) -> None:
        """Should show poller status."""
        from ctrlrelay.cli import app

        result = runner.invoke(app, ["poller", "status"])

        # Should not crash, status depends on state
        assert result.exit_code in [0, 1]
