"""Tests for `ctrlrelay ci wait` — the approved CI-wait helper.

The dev pipeline used to ask Claude to improvise a bash `until gh pr checks`
loop, which it kept getting wrong (inverted semantics, pipe swallowing exit
codes — see issue #85). This command replaces that improvisation with a
first-class helper that has sane exit codes and a hard timeout.
"""

import re
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

runner = CliRunner()

# Typer-with-Rich colorises `--help` on some hosts (CI runners in particular),
# which splits literal substrings like `--pr` across escape codes. Strip codes
# before substring assertions so the tests work identically on a plain-TTY
# dev laptop and the GitHub Actions runner.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return _ANSI.sub("", output)


class TestCIWaitCommand:
    def test_ci_wait_help(self) -> None:
        """`ctrlrelay ci wait --help` should document --pr, --repo, --timeout."""
        from ctrlrelay.cli import app

        result = runner.invoke(app, ["ci", "wait", "--help"])

        assert result.exit_code == 0
        out = _plain(result.output)
        assert "--pr" in out
        assert "--repo" in out
        assert "--timeout" in out

    def test_ci_wait_requires_pr_and_repo(self) -> None:
        """Both --pr and --repo are required."""
        from ctrlrelay.cli import app

        result = runner.invoke(app, ["ci", "wait"])

        assert result.exit_code != 0

    def test_ci_wait_exits_0_when_all_checks_pass(self) -> None:
        """Exit 0 when every check is in a passing bucket."""
        from ctrlrelay.cli import app

        fake_github = AsyncMock()
        fake_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
            {"name": "lint", "state": "SUCCESS", "bucket": "pass"},
        ]

        with patch("ctrlrelay.cli.GitHubCLI", return_value=fake_github):
            result = runner.invoke(
                app,
                ["ci", "wait", "--pr", "42", "--repo", "owner/repo",
                 "--timeout", "1", "--interval", "0"],
            )

        assert result.exit_code == 0, result.output

    def test_ci_wait_exits_0_when_no_ci_configured(self) -> None:
        """Repos with no CI (empty checks after confirmation) should pass."""
        from ctrlrelay.cli import app

        fake_github = AsyncMock()
        fake_github.get_pr_checks.return_value = []

        with patch("ctrlrelay.cli.GitHubCLI", return_value=fake_github):
            result = runner.invoke(
                app,
                ["ci", "wait", "--pr", "42", "--repo", "owner/repo",
                 "--timeout", "1", "--interval", "0"],
            )

        assert result.exit_code == 0, result.output

    def test_ci_wait_exits_1_when_check_fails(self) -> None:
        """Any failing check → exit 1."""
        from ctrlrelay.cli import app

        fake_github = AsyncMock()
        fake_github.get_pr_checks.return_value = [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
            {"name": "lint", "state": "FAILURE", "bucket": "fail"},
        ]

        with patch("ctrlrelay.cli.GitHubCLI", return_value=fake_github):
            result = runner.invoke(
                app,
                ["ci", "wait", "--pr", "42", "--repo", "owner/repo",
                 "--timeout", "1", "--interval", "0"],
            )

        assert result.exit_code == 1, result.output
        assert "lint" in _plain(result.output)

    def test_ci_wait_exits_2_when_timeout_with_pending(self) -> None:
        """Hard timeout while checks are still pending → exit 2 (distinct from fail)."""
        from ctrlrelay.cli import app

        fake_github = AsyncMock()
        fake_github.get_pr_checks.return_value = [
            {"name": "long-ci", "state": "IN_PROGRESS", "bucket": "pending"},
        ]

        with patch("ctrlrelay.cli.GitHubCLI", return_value=fake_github):
            result = runner.invoke(
                app,
                ["ci", "wait", "--pr", "42", "--repo", "owner/repo",
                 "--timeout", "0", "--interval", "0"],
            )

        assert result.exit_code == 2, result.output

    def test_ci_wait_polls_until_pending_resolves(self) -> None:
        """Should keep polling while pending, return 0 when everything goes green."""
        from ctrlrelay.cli import app

        fake_github = AsyncMock()
        fake_github.get_pr_checks.side_effect = [
            [{"name": "ci", "state": "IN_PROGRESS", "bucket": "pending"}],
            [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}],
        ]

        with patch("ctrlrelay.cli.GitHubCLI", return_value=fake_github):
            result = runner.invoke(
                app,
                ["ci", "wait", "--pr", "42", "--repo", "owner/repo",
                 "--timeout", "5", "--interval", "0"],
            )

        assert result.exit_code == 0, result.output
        assert fake_github.get_pr_checks.call_count == 2
