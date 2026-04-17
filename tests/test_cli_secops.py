"""Tests for secops CLI commands."""


from typer.testing import CliRunner

runner = CliRunner()


class TestSecopsCLI:
    def test_run_secops_requires_config(self) -> None:
        """Should fail without valid config."""
        from dev_sync.cli import app

        result = runner.invoke(app, ["run", "secops", "--config", "/nonexistent.yaml"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "error" in result.output.lower()
