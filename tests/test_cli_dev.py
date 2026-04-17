"""Tests for dev pipeline CLI commands."""

from typer.testing import CliRunner

runner = CliRunner()


class TestRunDevCommand:
    def test_run_dev_requires_issue(self) -> None:
        """Should require issue number."""
        from dev_sync.cli import app

        result = runner.invoke(app, ["run", "dev"])

        assert result.exit_code != 0
        assert "Missing option" in result.output or "required" in result.output.lower()

    def test_run_dev_help(self) -> None:
        """Should show help for run dev command."""
        from dev_sync.cli import app

        result = runner.invoke(app, ["run", "dev", "--help"])

        assert result.exit_code == 0
        assert "--issue" in result.output
        assert "--repo" in result.output


class TestPollerCommands:
    def test_poller_start_help(self) -> None:
        """Should show help for poller start."""
        from dev_sync.cli import app

        result = runner.invoke(app, ["poller", "start", "--help"])

        assert result.exit_code == 0
        assert "--daemon" in result.output

    def test_poller_status(self) -> None:
        """Should show poller status."""
        from dev_sync.cli import app

        result = runner.invoke(app, ["poller", "status"])

        # Should not crash, status depends on state
        assert result.exit_code in [0, 1]
