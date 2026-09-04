"""Tests for gh-configured git protocol detection (#137).

``gh config get git_protocol`` tells us whether the operator has
GitHub set up for SSH or HTTPS. ctrlrelay's own clones (personalization
repo, pre-scan clone) must respect this instead of hardcoding HTTPS —
an SSH-only operator otherwise hits "could not read Username" with no
clear signal of what to do about it.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from ctrlrelay.gh_protocol import detect_git_protocol, github_clone_url


class TestDetectGitProtocol:
    def test_returns_ssh_when_gh_reports_ssh(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "ssh\n", "")
            assert detect_git_protocol() == "ssh"

    def test_returns_https_when_gh_reports_https(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "https\n", "")
            assert detect_git_protocol() == "https"

    def test_falls_back_to_default_when_gh_missing(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("no gh")):
            assert detect_git_protocol() == "https"
            assert detect_git_protocol(default="ssh") == "ssh"

    def test_falls_back_to_default_when_gh_exits_nonzero(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 1, "", "unknown config key git_protocol"
            )
            assert detect_git_protocol() == "https"

    def test_falls_back_to_default_on_unexpected_output(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "carrier-pigeon\n", "")
            assert detect_git_protocol() == "https"

    def test_falls_back_to_default_on_timeout(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=5),
        ):
            assert detect_git_protocol() == "https"


class TestGithubCloneUrl:
    def test_ssh_protocol_builds_ssh_url(self) -> None:
        assert (
            github_clone_url("alice/dotclaude", protocol="ssh")
            == "git@github.com:alice/dotclaude.git"
        )

    def test_https_protocol_builds_https_url(self) -> None:
        assert (
            github_clone_url("alice/dotclaude", protocol="https")
            == "https://github.com/alice/dotclaude.git"
        )
