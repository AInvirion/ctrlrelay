"""Tests for `ctrlrelay repos` bulk-clone/pull/status commands."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from ctrlrelay.cli import app

runner = CliRunner()


@pytest.fixture
def repos_config(sample_config_dict: dict, tmp_path: Path) -> Path:
    sample_config_dict["repos"] = [
        {"name": "AInvirion/aiproxyguard", "local_path": "~/Projects/AINVIRION/aiproxyguard"},
        {"name": "AInvirion/aiproxyguard-cloud", "local_path": "~/Projects/AINVIRION/cloud"},
        {"name": "SemClone/binarysniffer", "local_path": "~/Projects/SEMCL.ONE/binarysniffer"},
    ]
    path = tmp_path / "orchestrator.yaml"
    path.write_text(yaml.dump(sample_config_dict))
    return path


class TestCloneAllDryRun:
    def test_emits_one_line_per_repo(self, repos_config: Path, tmp_path: Path) -> None:
        dest = tmp_path / "workspace"
        result = runner.invoke(
            app,
            ["repos", "clone-all", str(dest), "--config", str(repos_config), "--dry-run"],
        )
        assert result.exit_code == 0
        assert "AInvirion/aiproxyguard" in result.output
        assert "AInvirion/aiproxyguard-cloud" in result.output
        assert "SemClone/binarysniffer" in result.output

    def test_path_is_dest_slash_org_slash_repo(self, repos_config: Path, tmp_path: Path) -> None:
        dest = tmp_path / "workspace"
        result = runner.invoke(
            app,
            ["repos", "clone-all", str(dest), "--config", str(repos_config), "--dry-run"],
        )
        # Rich line-wraps long paths — collapse whitespace before substring check.
        flat = "".join(result.output.split())
        assert "workspace/AInvirion/aiproxyguard" in flat
        assert "workspace/SemClone/binarysniffer" in flat

    def test_remote_uses_ssh_github_convention(self, repos_config: Path, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "repos", "clone-all", str(tmp_path / "ws"),
                "--config", str(repos_config),
                "--dry-run",
            ],
        )
        assert "git@github.com:AInvirion/aiproxyguard.git" in result.output
        assert "git@github.com:SemClone/binarysniffer.git" in result.output

    def test_filter_narrows_to_substring(self, repos_config: Path, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "repos", "clone-all", str(tmp_path / "ws"),
                "--config", str(repos_config),
                "--filter", "SemClone",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        assert "SemClone/binarysniffer" in result.output
        assert "AInvirion" not in result.output


class TestCloneAllExecution:
    def test_invokes_git_clone_with_exact_argv(
        self, repos_config: Path, tmp_path: Path
    ) -> None:
        """clone-all must invoke `git clone --quiet REMOTE TARGET` exactly."""
        dest = tmp_path / "workspace"

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = runner.invoke(
                app,
                ["repos", "clone-all", str(dest), "--config", str(repos_config)],
            )

        assert result.exit_code == 0
        clone_cmds = [c for c in calls if c[:2] == ["git", "clone"]]
        assert len(clone_cmds) == 3

        target = (dest / "AInvirion" / "aiproxyguard").resolve()
        expected = [
            "git",
            "clone",
            "--quiet",
            "git@github.com:AInvirion/aiproxyguard.git",
            str(target),
        ]
        assert expected in clone_cmds, (
            f"expected exact argv {expected} in {clone_cmds}"
        )

    def test_skips_already_cloned_repos(
        self, repos_config: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "workspace"
        # Pre-create one repo as if already cloned.
        already = dest / "AInvirion" / "aiproxyguard" / ".git"
        already.mkdir(parents=True)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            result = runner.invoke(
                app,
                ["repos", "clone-all", str(dest), "--config", str(repos_config)],
            )

        assert result.exit_code == 0
        clone_cmds = [
            c for c in [args[0] for args, _ in mock_run.call_args_list]
            if c[:2] == ["git", "clone"]
        ]
        # Only 2 clones — the third was skipped.
        assert len(clone_cmds) == 2


class TestPullAll:
    def test_skips_repos_that_are_not_cloned(
        self, repos_config: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "workspace"
        with patch("subprocess.run") as mock_run:
            result = runner.invoke(
                app,
                ["repos", "pull-all", str(dest), "--config", str(repos_config)],
            )
        assert result.exit_code == 0
        assert "not cloned" in result.output
        # No git was invoked because nothing was cloned.
        assert mock_run.call_count == 0

    def test_pull_uses_correct_flags_and_cwd(
        self, repos_config: Path, tmp_path: Path
    ) -> None:
        """pull-all on a clean tree must call `git -C TARGET pull --ff-only --quiet`."""
        dest = tmp_path / "workspace"
        target = dest / "AInvirion" / "aiproxyguard"
        (target / ".git").mkdir(parents=True)

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = runner.invoke(
                app,
                [
                    "repos", "pull-all", str(dest),
                    "--config", str(repos_config),
                    "--filter", "aiproxyguard",
                ],
            )

        assert result.exit_code == 0
        target_resolved = str(target.resolve())
        # Clean tree: status check, then pull (no fetch path).
        expected_status = ["git", "-C", target_resolved, "status", "--porcelain"]
        expected_pull = ["git", "-C", target_resolved, "pull", "--ff-only", "--quiet"]
        assert expected_status in calls
        assert expected_pull in calls

    def test_dirty_tree_fetch_failure_is_reported_as_failed(
        self, repos_config: Path, tmp_path: Path
    ) -> None:
        """If `git fetch` returns non-zero on a dirty tree, count it as failed (not dirty)."""
        dest = tmp_path / "workspace"
        target = dest / "AInvirion" / "aiproxyguard"
        (target / ".git").mkdir(parents=True)

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if "status" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=" M file.txt\n", stderr="")
            if "fetch" in cmd:
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="auth failed")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = runner.invoke(
                app,
                [
                    "repos", "pull-all", str(dest),
                    "--config", str(repos_config),
                    "--filter", "aiproxyguard",
                ],
            )

        # Failed because fetch errored out.
        assert result.exit_code == 1
        assert "failed" in result.output
        assert "auth failed" in result.output
        # Crucially, it must NOT have been counted as a successful "dirty — fetched only".
        assert "fetched only" not in result.output


class TestStatus:
    def test_shows_not_cloned_for_missing_directories(
        self, repos_config: Path, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            app,
            ["repos", "status", str(tmp_path / "workspace"), "--config", str(repos_config)],
        )
        assert result.exit_code == 0
        assert "not cloned" in result.output


class TestRepoConfigNameValidation:
    """Belt-and-suspenders: malicious manifest names must be rejected at config load."""

    def test_traversal_name_is_rejected(self, sample_config_dict: dict, tmp_path: Path) -> None:
        from ctrlrelay.core.config import ConfigError, load_config

        sample_config_dict["repos"] = [
            {"name": "foo/../../tmp/pwn", "local_path": "~/pwn"},
        ]
        path = tmp_path / "orchestrator.yaml"
        path.write_text(yaml.dump(sample_config_dict))

        with pytest.raises(ConfigError):
            load_config(path)

    def test_extra_slash_name_is_rejected(self, sample_config_dict: dict, tmp_path: Path) -> None:
        from ctrlrelay.core.config import ConfigError, load_config

        sample_config_dict["repos"] = [
            {"name": "owner/sub/repo", "local_path": "~/x"},
        ]
        path = tmp_path / "orchestrator.yaml"
        path.write_text(yaml.dump(sample_config_dict))

        with pytest.raises(ConfigError):
            load_config(path)

    def test_well_formed_name_passes(self, sample_config_dict: dict, tmp_path: Path) -> None:
        from ctrlrelay.core.config import load_config

        sample_config_dict["repos"] = [
            {"name": "AInvirion/aiproxyguard-cloud.v2", "local_path": "~/x"},
        ]
        path = tmp_path / "orchestrator.yaml"
        path.write_text(yaml.dump(sample_config_dict))

        cfg = load_config(path)
        assert cfg.repos[0].name == "AInvirion/aiproxyguard-cloud.v2"
