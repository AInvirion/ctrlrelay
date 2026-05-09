"""Tests for the `ctrlrelay setup` first-run flow."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ctrlrelay.cli import app
from ctrlrelay.setup import (
    GhAuthError,
    SetupOptions,
    assert_gh_auth,
    build_orchestrator_yaml,
    detect_owners,
    list_repos,
    run_setup,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# gh helpers


class TestAssertGhAuth:
    def test_returns_silently_when_authenticated(self) -> None:
        """gh auth status exits 0 -> no error."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "logged in", "")
            assert_gh_auth()  # would raise if it errored

    def test_raises_when_not_authenticated(self) -> None:
        """gh auth status exits non-zero -> GhAuthError with the message."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 1, "", "You are not logged into any GitHub hosts."
            )
            with pytest.raises(GhAuthError, match="not authenticated"):
                assert_gh_auth()


class TestDetectOwners:
    def test_returns_user_then_orgs(self) -> None:
        """The authenticated user's login comes first; orgs follow in API order."""

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[:3] == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(cmd, 0, "alice\n", "")
            if cmd[:3] == ["gh", "api", "user/orgs"]:
                return subprocess.CompletedProcess(
                    cmd, 0, "AInvirion\nSemClone\n", ""
                )
            raise AssertionError(f"unexpected command: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            owners = detect_owners()

        assert owners == ["alice", "AInvirion", "SemClone"]

    def test_handles_no_orgs(self) -> None:
        """User with no orgs -> single-element list (just the user)."""

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[:3] == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(cmd, 0, "alice\n", "")
            if cmd[:3] == ["gh", "api", "user/orgs"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            assert detect_owners() == ["alice"]


class TestListRepos:
    def test_filters_forks_and_empty_and_archived(self) -> None:
        payload = json.dumps([
            {"nameWithOwner": "alice/keep", "isFork": False, "isEmpty": False,
             "defaultBranchRef": {"name": "main"}},
            {"nameWithOwner": "alice/fork", "isFork": True, "isEmpty": False,
             "defaultBranchRef": {"name": "main"}},
            {"nameWithOwner": "alice/empty", "isFork": False, "isEmpty": True,
             "defaultBranchRef": None},
        ])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, payload, "")
            repos = list_repos("alice")

        # Only "alice/keep" survives the filters. Fork is dropped (skip_forks=True),
        # empty is dropped unconditionally (no default branch -> can't clone).
        assert [r["nameWithOwner"] for r in repos] == ["alice/keep"]

    def test_passes_no_archived_flag_when_skip_archived(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "[]", "")
            list_repos("alice", skip_archived=True)
        cmd = mock_run.call_args.args[0]
        assert "--no-archived" in cmd

    def test_omits_no_archived_flag_when_include(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "[]", "")
            list_repos("alice", skip_archived=False)
        cmd = mock_run.call_args.args[0]
        assert "--no-archived" not in cmd

    def test_results_sorted_case_insensitively(self) -> None:
        payload = json.dumps([
            {"nameWithOwner": "alice/Bravo", "isFork": False, "isEmpty": False,
             "defaultBranchRef": {"name": "main"}},
            {"nameWithOwner": "alice/alpha", "isFork": False, "isEmpty": False,
             "defaultBranchRef": {"name": "main"}},
        ])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, payload, "")
            repos = list_repos("alice")
        # Sort is case-insensitive so 'alpha' < 'Bravo' in display order.
        assert [r["nameWithOwner"] for r in repos] == ["alice/alpha", "alice/Bravo"]


# ---------------------------------------------------------------------------
# yaml generation


class TestBuildOrchestratorYaml:
    def _options(self, **overrides) -> SetupOptions:  # type: ignore[no-untyped-def]
        return SetupOptions(
            owners=["alice", "AInvirion"],
            repo_root=Path("/srv/code"),
            config_out=Path("/tmp/cfg.yaml"),
            timezone="America/Santiago",
            **overrides,
        )

    def test_minimal_file_mock_config(self, tmp_path: Path) -> None:
        repos = {
            "alice": [
                {"nameWithOwner": "alice/foo", "isFork": False, "isEmpty": False,
                 "defaultBranchRef": {"name": "main"}}
            ],
            "AInvirion": [
                {"nameWithOwner": "AInvirion/bar", "isFork": False, "isEmpty": False,
                 "defaultBranchRef": {"name": "main"}}
            ],
        }
        text = build_orchestrator_yaml(self._options(), repos)

        # Round-trips through the loader, which is the real correctness check.
        from ctrlrelay.core.config import load_config

        cfg_path = tmp_path / "out.yaml"
        cfg_path.write_text(text)
        config = load_config(cfg_path)
        assert {r.name for r in config.repos} == {"alice/foo", "AInvirion/bar"}
        assert config.timezone == "America/Santiago"
        # Lowercase owner derivation (#128).
        ainvirion_repo = next(r for r in config.repos if r.name == "AInvirion/bar")
        assert ainvirion_repo.local_path == Path("/srv/code/ainvirion/bar")

    def test_telegram_block_when_transport_telegram(self) -> None:
        text = build_orchestrator_yaml(
            self._options(transport="telegram", telegram_chat_id=12345),
            repos_by_owner={"alice": [], "AInvirion": []},
        )
        assert 'type: "telegram"' in text
        assert "chat_id: 12345" in text
        # file_mock block is NOT emitted when telegram is selected.
        assert 'type: "file_mock"' not in text

    def test_personalization_block_emitted_when_repo_set(self) -> None:
        text = build_orchestrator_yaml(
            self._options(personalization_repo="alice/dotclaude"),
            repos_by_owner={"alice": [], "AInvirion": []},
        )
        assert "personalization:" in text
        assert 'repo: "alice/dotclaude"' in text

    def test_personalization_block_omitted_when_repo_unset(self) -> None:
        text = build_orchestrator_yaml(
            self._options(),
            repos_by_owner={"alice": [], "AInvirion": []},
        )
        assert "personalization:" not in text


# ---------------------------------------------------------------------------
# end-to-end run_setup


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Mock all gh subprocess invocations + git clone.

    Returns a dict the tests can mutate to control responses. Yields a
    dict with 'repos_by_owner', 'auth_ok' and a list 'git_clone_calls'.
    """
    state = {
        "auth_ok": True,
        "repos_by_owner": {"alice": [], "AInvirion": []},
        "git_clone_calls": [],
    }

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if cmd[:3] == ["gh", "auth", "status"]:
            rc = 0 if state["auth_ok"] else 1
            return subprocess.CompletedProcess(cmd, rc, "ok", "")
        if cmd[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(cmd, 0, "alice\n", "")
        if cmd[:3] == ["gh", "api", "user/orgs"]:
            return subprocess.CompletedProcess(
                cmd, 0, "\n".join(state["repos_by_owner"].keys() - {"alice"}) + "\n", ""
            )
        if cmd[:3] == ["gh", "repo", "list"]:
            owner = cmd[3]
            data = []
            for full_name in state["repos_by_owner"].get(owner, []):
                data.append({
                    "nameWithOwner": full_name,
                    "isFork": False,
                    "isEmpty": False,
                    "defaultBranchRef": {"name": "main"},
                })
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")
        if cmd[:2] == ["git", "clone"]:
            state["git_clone_calls"].append(cmd)
            target = Path(cmd[-1])
            (target / ".git").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("subprocess.run", fake_run)
    return state


class TestRunSetup:
    def test_writes_config_and_clones_repos(
        self, fake_gh: dict, tmp_path: Path
    ) -> None:
        fake_gh["repos_by_owner"] = {
            "alice": ["alice/foo", "alice/bar"],
            "AInvirion": ["AInvirion/baz"],
        }
        opts = SetupOptions(
            owners=["alice", "AInvirion"],
            repo_root=tmp_path / "Projects",
            config_out=tmp_path / ".config" / "orchestrator.yaml",
            transport="file_mock",
        )
        result = run_setup(opts)

        # Config file written and validated.
        assert opts.config_out.is_file()
        assert result.n_repos == 3
        assert result.cloned == 3
        assert result.failed == 0

        # All clones land at owner.lower()/repo (#128).
        assert (tmp_path / "Projects" / "alice" / "foo" / ".git").is_dir()
        assert (tmp_path / "Projects" / "alice" / "bar" / ".git").is_dir()
        assert (tmp_path / "Projects" / "ainvirion" / "baz" / ".git").is_dir()

    def test_refuses_to_overwrite_existing_config_without_force(
        self, fake_gh: dict, tmp_path: Path
    ) -> None:
        config_out = tmp_path / "cfg.yaml"
        config_out.write_text("# pre-existing operator file\n")
        opts = SetupOptions(
            owners=["alice"],
            repo_root=tmp_path / "Projects",
            config_out=config_out,
        )
        with pytest.raises(FileExistsError, match="already exists"):
            run_setup(opts)

        # The pre-existing file is untouched.
        assert config_out.read_text().startswith("# pre-existing")

    def test_overwrites_with_force(self, fake_gh: dict, tmp_path: Path) -> None:
        config_out = tmp_path / "cfg.yaml"
        config_out.write_text("# stale\n")
        fake_gh["repos_by_owner"] = {"alice": ["alice/foo"]}
        opts = SetupOptions(
            owners=["alice"],
            repo_root=tmp_path / "Projects",
            config_out=config_out,
            force=True,
        )
        run_setup(opts)
        # Replaced with generated content; the stale comment is gone.
        assert "# stale" not in config_out.read_text()
        assert "alice/foo" in config_out.read_text()

    def test_blocks_when_gh_not_authed(
        self, fake_gh: dict, tmp_path: Path
    ) -> None:
        fake_gh["auth_ok"] = False
        opts = SetupOptions(
            owners=["alice"],
            repo_root=tmp_path / "Projects",
            config_out=tmp_path / "cfg.yaml",
        )
        with pytest.raises(GhAuthError):
            run_setup(opts)
        # Config NOT written because auth fails before any disk write.
        assert not opts.config_out.exists()

    def test_unknown_transport_value_rejected(
        self, fake_gh: dict, tmp_path: Path
    ) -> None:
        """Mistyped transport (e.g. 'telegrm') must fail fast rather
        than silently producing a file_mock config. Codex review pass
        2 caught this — keep the regression test next to the guard."""
        opts = SetupOptions(
            owners=["alice"],
            repo_root=tmp_path / "Projects",
            config_out=tmp_path / "cfg.yaml",
            transport="telegrm",  # typo
        )
        with pytest.raises(ValueError, match="unknown transport"):
            run_setup(opts)
        assert not opts.config_out.exists()

    def test_install_daemons_with_custom_config_out_rejected(
        self, fake_gh: dict, tmp_path: Path
    ) -> None:
        """The launchd/systemd templates don't carry CTRLRELAY_CONFIG,
        so a daemon rendered for a non-default --config-out path would
        fail to find the file at runtime. Refuse the combination at
        setup time instead of producing orphan daemons. Codex review
        pass 2 caught this."""
        opts = SetupOptions(
            owners=["alice"],
            repo_root=tmp_path / "Projects",
            config_out=tmp_path / "weird-place" / "cfg.yaml",
            install_daemons=True,
        )
        with pytest.raises(ValueError, match="install-daemons requires the default"):
            run_setup(opts)

    def test_skip_clone_writes_config_but_no_clones(
        self, fake_gh: dict, tmp_path: Path
    ) -> None:
        fake_gh["repos_by_owner"] = {"alice": ["alice/foo"]}
        opts = SetupOptions(
            owners=["alice"],
            repo_root=tmp_path / "Projects",
            config_out=tmp_path / "cfg.yaml",
            skip_clone=True,
        )
        result = run_setup(opts)
        assert result.cloned == 0
        # No git clones were issued.
        assert fake_gh["git_clone_calls"] == []
        # Config is still on disk and parses.
        assert opts.config_out.is_file()


# ---------------------------------------------------------------------------
# CLI surface


class TestSetupCli:
    def test_help_runs(self) -> None:
        result = runner.invoke(app, ["setup", "--help"])
        assert result.exit_code == 0
        assert "First-run setup" in result.output

    def test_telegram_without_chat_id_fails_in_yes_mode(
        self, fake_gh: dict, tmp_path: Path
    ) -> None:
        """Non-interactive setup with --transport telegram but no
        --telegram-chat-id must fail fast rather than silently write
        chat_id: 0 (which the bridge would try to send to). Codex
        review pass 1 caught this regression — keep the test next to
        the fix."""
        fake_gh["repos_by_owner"] = {"alice": [], "AInvirion": []}
        result = runner.invoke(
            app,
            [
                "setup",
                "--yes",
                "--repo-root", str(tmp_path / "Projects"),
                "--config-out", str(tmp_path / "cfg.yaml"),
                "--no-personalization",
                "--transport", "telegram",
                # NOTE: no --telegram-chat-id
            ],
        )
        assert result.exit_code == 2, result.output
        assert "telegram-chat-id" in result.output
        # Config NOT written — failure is before any disk write.
        assert not (tmp_path / "cfg.yaml").exists()

    def test_telegram_without_token_keeps_telegram_transport(
        self, fake_gh: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing CTRLRELAY_TELEGRAM_TOKEN must NOT silently flip the
        config to file_mock. The config stores bot_token_env so the
        token can be supplied later. Codex review pass 1 caught this —
        regression test guards the transport-preservation contract."""
        monkeypatch.delenv("CTRLRELAY_TELEGRAM_TOKEN", raising=False)
        fake_gh["repos_by_owner"] = {"alice": [], "AInvirion": []}
        result = runner.invoke(
            app,
            [
                "setup",
                "--yes",
                "--repo-root", str(tmp_path / "Projects"),
                "--config-out", str(tmp_path / "cfg.yaml"),
                "--no-personalization",
                "--transport", "telegram",
                "--telegram-chat-id", "12345",
                # No --install-daemons, so token isn't needed for plist render.
            ],
        )
        assert result.exit_code == 0, result.output
        text = (tmp_path / "cfg.yaml").read_text()
        assert 'type: "telegram"' in text
        assert 'type: "file_mock"' not in text
        assert "chat_id: 12345" in text

    def test_include_forks_omits_source_flag_to_gh(
        self, fake_gh: dict, tmp_path: Path
    ) -> None:
        """--include-forks must let forks through. Pre-fix, list_repos
        passed --source unconditionally, so gh excluded forks at the
        API level before our skip_forks check could let them in.
        Codex review pass 1 caught this."""
        # Spy on the gh repo list args to confirm --source is dropped.
        captured: list[list[str]] = []

        original_run = subprocess.run

        def spy_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[:3] == ["gh", "repo", "list"]:
                captured.append(list(cmd))
            return original_run(cmd, **kwargs)  # falls through to fake_gh

        # The fake_gh fixture already monkeypatched subprocess.run; spy
        # by intercepting and forwarding to whatever it set.
        with patch("subprocess.run", side_effect=spy_run):
            list_repos("alice", skip_forks=False)

        assert any(c[:3] == ["gh", "repo", "list"] for c in captured)
        repo_list_cmd = next(c for c in captured if c[:3] == ["gh", "repo", "list"])
        assert "--source" not in repo_list_cmd

    def test_clone_failure_surfaces_nonzero_exit_even_with_daemons(
        self, fake_gh: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When --install-daemons succeeds in rendering plists but at
        least one git clone failed, exit code must be non-zero so
        automation doesn't mistake a partial setup for success. Codex
        review pass 3 caught the prior `elif` that masked this."""
        # Override DEFAULT_CONFIG_OUT for this test so the
        # install-daemons-requires-default-config-out guard allows the
        # tmp config_out we're using. Critically, this prevents the test
        # from clobbering the operator's real ~/.config/ctrlrelay/...
        from ctrlrelay import setup as setup_mod

        tmp_config_out = tmp_path / "cfg.yaml"
        monkeypatch.setattr(setup_mod, "DEFAULT_CONFIG_OUT", tmp_config_out)

        # Route plist writes to tmp so we don't touch ~/Library/LaunchAgents.
        target_dir = tmp_path / "LaunchAgents"
        from ctrlrelay import install as install_mod

        original_render = install_mod.render_launchd

        def render_to_tmp(**kwargs):  # type: ignore[no-untyped-def]
            kwargs["target_dir"] = target_dir
            return original_render(**kwargs)

        monkeypatch.setattr(install_mod, "render_launchd", render_to_tmp)

        # Wrap the existing fake_gh subprocess.run so git clone fails.
        original_run = subprocess.run

        def fail_clones(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[:2] == ["git", "clone"]:
                return subprocess.CompletedProcess(
                    cmd, 1, "", "fatal: repository not found"
                )
            return original_run(cmd, **kwargs)

        fake_gh["repos_by_owner"] = {"alice": ["alice/foo"]}
        with patch("subprocess.run", side_effect=fail_clones):
            result = runner.invoke(
                app,
                [
                    "setup",
                    "--yes",
                    "--repo-root", str(tmp_path / "Projects"),
                    "--config-out", str(tmp_config_out),
                    "--no-personalization",
                    "--transport", "telegram",
                    "--telegram-chat-id", "12345",
                    "--install-daemons",
                ],
            )
        assert result.exit_code != 0, (
            f"clone failures must yield non-zero exit; got {result.exit_code} "
            f"with output:\n{result.output}"
        )

    def test_yes_takes_all_owners_non_interactively(
        self, fake_gh: dict, tmp_path: Path
    ) -> None:
        fake_gh["repos_by_owner"] = {
            "alice": ["alice/foo"],
            "AInvirion": [],
        }
        result = runner.invoke(
            app,
            [
                "setup",
                "--yes",
                "--repo-root", str(tmp_path / "Projects"),
                "--config-out", str(tmp_path / "cfg.yaml"),
                "--no-personalization",
                "--transport", "file_mock",
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "cfg.yaml").is_file()
        assert (tmp_path / "Projects" / "alice" / "foo" / ".git").is_dir()
