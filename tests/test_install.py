"""Tests for the launchd / systemd template installer.

The CLI ``ctrlrelay install`` produces unit files from in-package
templates so operators don't have to copy/paste XML or INI fragments
out of docs and hand-edit /Users/$ME/... paths. Coverage focuses on:

* The four shipped templates render with no syntax surprises.
* Filenames are platform-correct (label-prefix for launchd, fixed for
  systemd) and end up under the expected target dirs.
* Unresolved variables are surfaced (CTRLRELAY_TELEGRAM_TOKEN is the
  load-bearing one — silently writing a plist with a literal
  ``${TOKEN}`` would brick the bridge at next boot).
* ``write_units`` refuses to clobber existing files unless ``overwrite``.
* ``write_units`` creates ``~/.ctrlrelay/logs/`` (missing it crash-loops
  the generated systemd units, see #137) and chmods unit files 0600
  (they embed CTRLRELAY_TELEGRAM_TOKEN in plaintext).
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctrlrelay.cli import app
from ctrlrelay.install import (
    render_launchd,
    render_systemd,
    write_units,
)


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at a scratch dir for every test in this
    module. ``write_units`` now creates ``~/.ctrlrelay/logs/`` as a
    side effect (#137) — without this, running the suite would create
    that directory (and touch unit-file permissions) under the real
    developer/CI home.

    It also cuts this module off from config discovery. ``ctrlrelay
    install`` reads the active transport to learn which token variable
    to render (#267), and ``default_config_search_paths`` walks $HOME,
    $XDG_CONFIG_HOME, $CTRLRELAY_CONFIG and every ``config/`` directory
    from cwd upwards. A developer box with a real orchestrator.yaml in
    any of those would otherwise decide what these tests assert — and
    on a Mattermost box the Telegram cases would fail for a reason that
    has nothing to do with the code.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("CTRLRELAY_CONFIG", raising=False)
    scratch_cwd = tmp_path / "cwd"
    scratch_cwd.mkdir()
    monkeypatch.chdir(scratch_cwd)
    return home


def _write_config(path: Path, transport: dict, *, tmp_path: Path) -> Path:
    """A minimal valid orchestrator.yaml carrying ``transport``."""
    import yaml

    config = {
        "version": "1",
        "node_id": "test-node",
        "timezone": "UTC",
        "paths": {
            "state_db": str(tmp_path / "state.db"),
            "worktrees": str(tmp_path / "worktrees"),
            "bare_repos": str(tmp_path / "repos"),
            "contexts": str(tmp_path / "contexts"),
            "skills": str(tmp_path / "skills"),
        },
        "agent": {"binary": "claude"},
        "transport": transport,
        "dashboard": {"enabled": False},
        "repos": [],
    }
    path.write_text(yaml.dump(config))
    return path


MATTERMOST_TRANSPORT = {
    "type": "mattermost",
    "mattermost": {
        "url": "https://chat.example.com",
        "bot_token_env": "CTRLRELAY_MATTERMOST_TOKEN",
        "channel_id": "abcdefghijklmnopqrstuvwxyz",
    },
}


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "workdir"
    d.mkdir()
    return d


@pytest.fixture
def target_dir(tmp_path: Path) -> Path:
    return tmp_path / "units"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestRenderLaunchd:
    def test_renders_two_units(self, workdir: Path, target_dir: Path) -> None:
        units = render_launchd(workdir=workdir, target_dir=target_dir)
        assert {u.service for u in units} == {"bridge", "poller"}
        assert all(u.platform == "launchd" for u in units)

    def test_target_filename_includes_label_prefix(
        self, workdir: Path, target_dir: Path
    ) -> None:
        # macOS refuses to load a plist whose <Label> doesn't match the
        # file basename. The prefix from --label-prefix has to flow into
        # both, otherwise launchctl bootstrap silently no-ops.
        units = render_launchd(
            workdir=workdir, target_dir=target_dir, label_prefix="com.acme"
        )
        names = sorted(u.target_path.name for u in units)
        assert names == [
            "com.acme.ctrlrelay-bridge.plist",
            "com.acme.ctrlrelay-poller.plist",
        ]
        for u in units:
            assert f"com.acme.ctrlrelay-{u.service}" in u.content

    def test_workdir_is_substituted(self, workdir: Path, target_dir: Path) -> None:
        units = render_launchd(workdir=workdir, target_dir=target_dir)
        for u in units:
            assert str(workdir) in u.content
            assert "${WORKDIR}" not in u.content

    def test_ctrlrelay_bin_override(self, workdir: Path, target_dir: Path) -> None:
        units = render_launchd(
            workdir=workdir,
            target_dir=target_dir,
            ctrlrelay_bin="/opt/custom/bin/ctrlrelay",
        )
        for u in units:
            assert "/opt/custom/bin/ctrlrelay" in u.content

    def test_token_from_env_is_substituted(
        self, workdir: Path, target_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CTRLRELAY_TELEGRAM_TOKEN", "abc:test-token-xyz")
        units = render_launchd(workdir=workdir, target_dir=target_dir)
        for u in units:
            assert "abc:test-token-xyz" in u.content
            assert u.unresolved == []

    def test_token_unset_leaves_placeholder_and_flags_unresolved(
        self, workdir: Path, target_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CTRLRELAY_TELEGRAM_TOKEN", raising=False)
        units = render_launchd(workdir=workdir, target_dir=target_dir)
        for u in units:
            assert "${CTRLRELAY_TELEGRAM_TOKEN}" in u.content
            assert "CTRLRELAY_TELEGRAM_TOKEN" in u.unresolved

    def test_pythonunbuffered_set_in_both_units(
        self, workdir: Path, target_dir: Path
    ) -> None:
        # PYTHONUNBUFFERED=1 is load-bearing for live log tailing under
        # launchd — without it, Python buffers stdout and `tail -f` lags
        # by minutes. Both bridge and poller need it; assert so the
        # template can't quietly drop it again.
        units = render_launchd(workdir=workdir, target_dir=target_dir)
        for u in units:
            assert "<key>PYTHONUNBUFFERED</key>" in u.content
            assert "<string>1</string>" in u.content

    def test_poller_interval_is_substituted(
        self, workdir: Path, target_dir: Path
    ) -> None:
        units = render_launchd(
            workdir=workdir, target_dir=target_dir, poller_interval=120
        )
        poller = next(u for u in units if u.service == "poller")
        # The poller plist has --interval as separate string args; check
        # that 120 appears as its own <string> element rather than
        # smushed into something else.
        assert "<string>120</string>" in poller.content


class TestRenderSystemd:
    def test_target_filenames_are_fixed(self, workdir: Path, target_dir: Path) -> None:
        units = render_systemd(workdir=workdir, target_dir=target_dir)
        names = sorted(u.target_path.name for u in units)
        assert names == [
            "ctrlrelay-bridge.service",
            "ctrlrelay-poller.service",
            "ctrlrelay-web.service",
        ]

    def test_web_unit_runs_the_status_page(
        self, workdir: Path, target_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The status page (#209) is its own unit so a slow page can never
        # stall the poller or the bridge. It needs no chat token.
        monkeypatch.setenv("CTRLRELAY_TELEGRAM_TOKEN", "abc:test-token-xyz")
        units = render_systemd(
            workdir=workdir, target_dir=target_dir, ctrlrelay_bin="/opt/bin/ctrlrelay"
        )
        [web] = [u for u in units if u.service == "web"]
        lines = web.content.splitlines()
        assert "ExecStart=/opt/bin/ctrlrelay web start --foreground" in lines
        assert "StandardOutput=append:%h/.ctrlrelay/logs/web.log" in lines
        assert "StandardError=append:%h/.ctrlrelay/logs/web.error.log" in lines
        assert "abc:test-token-xyz" not in web.content
        assert web.unresolved == []

    def test_pythonunbuffered_set_in_both_units(
        self, workdir: Path, target_dir: Path
    ) -> None:
        # Same buffering concern as the launchd plists — journalctl -f
        # also waits on stdio buffers if PYTHONUNBUFFERED is unset.
        units = render_systemd(workdir=workdir, target_dir=target_dir)
        for u in units:
            assert "Environment=PYTHONUNBUFFERED=1" in u.content

    def test_systemd_units_have_install_section(
        self, workdir: Path, target_dir: Path
    ) -> None:
        # Without [Install] WantedBy=default.target, ``systemctl --user
        # enable`` errors out. Cheap check that we didn't accidentally
        # ship a half-template.
        units = render_systemd(workdir=workdir, target_dir=target_dir)
        for u in units:
            assert "[Install]" in u.content
            assert "WantedBy=default.target" in u.content

    def test_workdir_and_bin_substituted(
        self, workdir: Path, target_dir: Path
    ) -> None:
        units = render_systemd(
            workdir=workdir,
            target_dir=target_dir,
            ctrlrelay_bin="/opt/bin/ctrlrelay",
        )
        for u in units:
            assert f"WorkingDirectory={workdir}" in u.content
            assert "/opt/bin/ctrlrelay" in u.content


class TestWriteUnits:
    def test_creates_logs_dir(
        self, workdir: Path, target_dir: Path, fake_home: Path
    ) -> None:
        # Both the launchd and systemd templates set
        # StandardOutput/StandardError under ~/.ctrlrelay/logs/, but
        # nothing else in setup ever creates it. systemd refuses to
        # start a unit whose log dir is missing (EXIT_STDOUT, 209),
        # and with Restart=always that's an immediate crash-loop.
        units = render_systemd(workdir=workdir, target_dir=target_dir)
        write_units(units)
        assert (fake_home / ".ctrlrelay" / "logs").is_dir()

    def test_written_units_are_chmod_600(
        self, workdir: Path, target_dir: Path
    ) -> None:
        # Unit files embed CTRLRELAY_TELEGRAM_TOKEN as plaintext —
        # refuse to leave them group/world-readable.
        units = render_systemd(workdir=workdir, target_dir=target_dir)
        written = write_units(units)
        for path in written:
            assert (path.stat().st_mode & 0o777) == 0o600

    def test_writes_files_to_disk(
        self, workdir: Path, target_dir: Path
    ) -> None:
        units = render_launchd(workdir=workdir, target_dir=target_dir)
        written = write_units(units)
        assert len(written) == 2
        for path in written:
            assert path.exists()
            assert path.read_text().startswith("<?xml")

    def test_creates_target_dir_if_missing(
        self, workdir: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "deep" / "nested" / "dir"
        units = render_launchd(workdir=workdir, target_dir=target)
        write_units(units)
        assert target.is_dir()

    def test_refuses_to_overwrite_by_default(
        self, workdir: Path, target_dir: Path
    ) -> None:
        units = render_launchd(workdir=workdir, target_dir=target_dir)
        write_units(units)
        # Re-render and try again — should refuse.
        units2 = render_launchd(workdir=workdir, target_dir=target_dir)
        with pytest.raises(FileExistsError, match="--force"):
            write_units(units2)

    def test_force_overwrites(
        self, workdir: Path, target_dir: Path
    ) -> None:
        units = render_launchd(workdir=workdir, target_dir=target_dir)
        write_units(units)
        for u in units:
            u.target_path.write_text("HAND EDITED")
        units2 = render_launchd(workdir=workdir, target_dir=target_dir)
        write_units(units2, overwrite=True)
        for u in units2:
            assert "HAND EDITED" not in u.target_path.read_text()
            assert u.target_path.read_text().startswith("<?xml")


class TestInstallCli:
    def test_dry_run_does_not_touch_filesystem(
        self,
        runner: CliRunner,
        workdir: Path,
        target_dir: Path,
    ) -> None:
        result = runner.invoke(
            app,
            [
                "install", "launchd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "would write" in result.output
        assert not target_dir.exists() or not any(target_dir.iterdir())

    def test_writes_when_not_dry_run(
        self,
        runner: CliRunner,
        workdir: Path,
        target_dir: Path,
    ) -> None:
        result = runner.invoke(
            app,
            [
                "install", "launchd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
                "--label-prefix", "com.test",
            ],
        )
        assert result.exit_code == 0, result.output
        assert (target_dir / "com.test.ctrlrelay-bridge.plist").exists()
        assert (target_dir / "com.test.ctrlrelay-poller.plist").exists()

    def test_refuse_overwrite_exits_nonzero(
        self,
        runner: CliRunner,
        workdir: Path,
        target_dir: Path,
    ) -> None:
        # First run succeeds.
        runner.invoke(
            app,
            [
                "install", "launchd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
            ],
        )
        # Second run without --force must fail loudly so the operator
        # notices they're about to clobber a customised plist.
        result = runner.invoke(
            app,
            [
                "install", "launchd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
            ],
        )
        assert result.exit_code == 1
        assert "refusing to overwrite" in result.output

    def test_systemd_subcommand_writes_units(
        self,
        runner: CliRunner,
        workdir: Path,
        target_dir: Path,
    ) -> None:
        result = runner.invoke(
            app,
            [
                "install", "systemd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (target_dir / "ctrlrelay-bridge.service").exists()
        assert (target_dir / "ctrlrelay-poller.service").exists()


def test_the_bridge_unit_names_the_chat_bridge_not_one_provider(tmp_path) -> None:
    """The bridge serves Telegram or Mattermost; its unit says so."""
    from ctrlrelay.install import render_systemd

    [bridge] = [
        u for u in render_systemd(workdir=tmp_path, target_dir=tmp_path)
        if u.service == "bridge"
    ]
    assert "Description=ctrlrelay chat bridge (Telegram or Mattermost)" in bridge.content


def _systemd_env(content: str) -> dict[str, str]:
    """The ``Environment=`` assignments systemd would read, as a dict.

    Parsed rather than matched as text so a variable named in a ``#``
    comment — which systemd ignores — cannot satisfy an assertion about
    what the unit sets, in either direction.
    """
    env: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith("Environment="):
            continue
        name, _, value = stripped[len("Environment="):].partition("=")
        env[name] = value
    return env


def _plist_env(content: str) -> dict[str, str]:
    """``EnvironmentVariables`` as launchd would read it."""
    parsed = plistlib.loads(content.encode("utf-8"))
    return {k: str(v) for k, v in parsed["EnvironmentVariables"].items()}


def _plist_env_value(content: str, key: str) -> str:
    """The value launchd would read for ``key``, by parsing the plist.

    Parsed rather than grepped for two reasons. ``<string>1</string>``
    already appears for PYTHONUNBUFFERED, so an ``in`` check would pass
    for a plist that named the token key and gave it somebody else's
    value — and a key sitting inside an XML comment would satisfy a text
    match while launchd never sees it. Raises when the key is absent, so
    a missing entry cannot read as an empty value.
    """
    env = _plist_env(content)
    if key not in env:
        raise AssertionError(
            f"{key} is not in EnvironmentVariables; got {sorted(env)}"
        )
    return str(env[key])


class TestTokenVariableFollowsTheTransport:
    """#267: the unit carries the variable the transport names.

    The installer used to hard-code ``CTRLRELAY_TELEGRAM_TOKEN`` in both
    templates, so a Mattermost box got a unit with no token in it at all
    and the bridge could not log in once systemd or launchd started it.
    """

    def test_systemd_units_name_the_mattermost_variable(
        self, workdir: Path, target_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CTRLRELAY_MATTERMOST_TOKEN", "mm-token-xyz")
        units = render_systemd(
            workdir=workdir,
            target_dir=target_dir,
            token_env_var="CTRLRELAY_MATTERMOST_TOKEN",
        )
        for service in ("bridge", "poller"):
            [unit] = [u for u in units if u.service == service]
            env = _systemd_env(unit.content)
            assert env.get("CTRLRELAY_MATTERMOST_TOKEN") == "mm-token-xyz", (
                f"{service} unit sets {sorted(env)}"
            )
            # Not merely accompanied: the unit must no longer set a
            # Telegram variable this operator never exports.
            assert "CTRLRELAY_TELEGRAM_TOKEN" not in env
            assert unit.unresolved == []

    def test_launchd_plists_name_the_mattermost_variable(
        self, workdir: Path, target_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CTRLRELAY_MATTERMOST_TOKEN", "mm-token-xyz")
        units = render_launchd(
            workdir=workdir,
            target_dir=target_dir,
            token_env_var="CTRLRELAY_MATTERMOST_TOKEN",
        )
        for unit in units:
            value = _plist_env_value(unit.content, "CTRLRELAY_MATTERMOST_TOKEN")
            assert value == "mm-token-xyz"
            assert "CTRLRELAY_TELEGRAM_TOKEN" not in _plist_env(unit.content)
            assert unit.unresolved == []

    def test_unset_mattermost_token_is_flagged_under_its_own_name(
        self, workdir: Path, target_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The operator has to be told which variable to export. Flagging
        # CTRLRELAY_TELEGRAM_TOKEN on a Mattermost box sends them to set a
        # variable nothing reads.
        monkeypatch.delenv("CTRLRELAY_MATTERMOST_TOKEN", raising=False)
        units = render_systemd(
            workdir=workdir,
            target_dir=target_dir,
            token_env_var="CTRLRELAY_MATTERMOST_TOKEN",
        )
        for service in ("bridge", "poller"):
            [unit] = [u for u in units if u.service == service]
            env = _systemd_env(unit.content)
            assert (
                env.get("CTRLRELAY_MATTERMOST_TOKEN")
                == "${CTRLRELAY_MATTERMOST_TOKEN}"
            )
            assert unit.unresolved == ["CTRLRELAY_MATTERMOST_TOKEN"]
            assert unit.token_env_var == "CTRLRELAY_MATTERMOST_TOKEN"

    def test_an_arbitrary_variable_name_is_honoured(
        self, workdir: Path, target_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # bot_token_env is free text, not one of two known names: an
        # operator may point either transport at any variable.
        monkeypatch.setenv("MY_OWN_BOT_TOKEN", "house-token")
        units = render_systemd(
            workdir=workdir, target_dir=target_dir, token_env_var="MY_OWN_BOT_TOKEN"
        )
        [bridge] = [u for u in units if u.service == "bridge"]
        assert _systemd_env(bridge.content).get("MY_OWN_BOT_TOKEN") == "house-token"

    def test_telegram_is_still_the_default_and_renders_as_before(
        self, workdir: Path, target_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CTRLRELAY_TELEGRAM_TOKEN", "abc:test-token-xyz")
        units = render_systemd(workdir=workdir, target_dir=target_dir)
        [bridge] = [u for u in units if u.service == "bridge"]
        assert (
            _systemd_env(bridge.content).get("CTRLRELAY_TELEGRAM_TOKEN")
            == "abc:test-token-xyz"
        )

    def test_the_status_page_unit_gets_no_token_whichever_transport(
        self, workdir: Path, target_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The web unit sends nothing, so it has no business holding a
        # token — renaming the variable must not sneak one in.
        monkeypatch.setenv("CTRLRELAY_MATTERMOST_TOKEN", "mm-token-xyz")
        units = render_systemd(
            workdir=workdir,
            target_dir=target_dir,
            token_env_var="CTRLRELAY_MATTERMOST_TOKEN",
        )
        [web] = [u for u in units if u.service == "web"]
        assert "mm-token-xyz" not in web.content
        assert "CTRLRELAY_MATTERMOST_TOKEN" not in _systemd_env(web.content)
        assert web.unresolved == []


class TestInstallCliReadsTheTransportFromConfig:
    """The operator's route: ``ctrlrelay install`` with a config."""

    def test_systemd_install_writes_the_configured_mattermost_variable(
        self,
        runner: CliRunner,
        workdir: Path,
        target_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CTRLRELAY_MATTERMOST_TOKEN", "mm-token-xyz")
        config = _write_config(
            tmp_path / "orchestrator.yaml", MATTERMOST_TRANSPORT, tmp_path=tmp_path
        )
        result = runner.invoke(
            app,
            [
                "install", "systemd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
                "--config", str(config),
            ],
        )
        assert result.exit_code == 0, result.output
        for name in ("ctrlrelay-bridge.service", "ctrlrelay-poller.service"):
            env = _systemd_env((target_dir / name).read_text())
            assert env.get("CTRLRELAY_MATTERMOST_TOKEN") == "mm-token-xyz", (
                f"{name} sets {sorted(env)}"
            )
            assert "CTRLRELAY_TELEGRAM_TOKEN" not in env

    def test_launchd_install_writes_the_configured_mattermost_variable(
        self,
        runner: CliRunner,
        workdir: Path,
        target_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CTRLRELAY_MATTERMOST_TOKEN", "mm-token-xyz")
        config = _write_config(
            tmp_path / "orchestrator.yaml", MATTERMOST_TRANSPORT, tmp_path=tmp_path
        )
        result = runner.invoke(
            app,
            [
                "install", "launchd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
                "--label-prefix", "com.test",
                "--config", str(config),
            ],
        )
        assert result.exit_code == 0, result.output
        for service in ("bridge", "poller"):
            written = (
                target_dir / f"com.test.ctrlrelay-{service}.plist"
            ).read_text()
            assert (
                _plist_env_value(written, "CTRLRELAY_MATTERMOST_TOKEN")
                == "mm-token-xyz"
            )
            assert "CTRLRELAY_TELEGRAM_TOKEN" not in _plist_env(written)

    def test_the_unresolved_hint_names_the_configured_variable(
        self,
        runner: CliRunner,
        workdir: Path,
        target_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CTRLRELAY_MATTERMOST_TOKEN", raising=False)
        config = _write_config(
            tmp_path / "orchestrator.yaml", MATTERMOST_TRANSPORT, tmp_path=tmp_path
        )
        result = runner.invoke(
            app,
            [
                "install", "systemd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
                "--config", str(config),
            ],
        )
        assert result.exit_code == 0, result.output
        plain = " ".join(result.output.split())
        assert "export CTRLRELAY_MATTERMOST_TOKEN" in plain
        assert "CTRLRELAY_TELEGRAM_TOKEN" not in plain

    def test_a_telegram_config_with_its_own_variable_name_is_followed(
        self,
        runner: CliRunner,
        workdir: Path,
        target_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Reading bot_token_env, not the transport type: Telegram pointed
        # at a renamed variable has to render that name too.
        monkeypatch.setenv("MY_TELEGRAM_TOKEN", "abc:renamed")
        config = _write_config(
            tmp_path / "orchestrator.yaml",
            {
                "type": "telegram",
                "telegram": {
                    "bot_token_env": "MY_TELEGRAM_TOKEN",
                    "chat_id": 12345,
                },
            },
            tmp_path=tmp_path,
        )
        result = runner.invoke(
            app,
            [
                "install", "systemd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
                "--config", str(config),
            ],
        )
        assert result.exit_code == 0, result.output
        written = (target_dir / "ctrlrelay-bridge.service").read_text()
        assert _systemd_env(written).get("MY_TELEGRAM_TOKEN") == "abc:renamed"

    def test_no_config_anywhere_keeps_the_telegram_default(
        self,
        runner: CliRunner,
        workdir: Path,
        target_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Nothing names a transport, so the historical placeholder stands
        # and the warning tells the operator to export it.
        monkeypatch.delenv("CTRLRELAY_TELEGRAM_TOKEN", raising=False)
        result = runner.invoke(
            app,
            [
                "install", "systemd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        written = (target_dir / "ctrlrelay-bridge.service").read_text()
        assert (
            _systemd_env(written).get("CTRLRELAY_TELEGRAM_TOKEN")
            == "${CTRLRELAY_TELEGRAM_TOKEN}"
        )

    def test_a_config_path_that_does_not_exist_exits_nonzero(
        self,
        runner: CliRunner,
        workdir: Path,
        target_dir: Path,
        tmp_path: Path,
    ) -> None:
        # Silently falling back to the Telegram default here would write a
        # unit for a transport the operator did not ask for, on the
        # strength of a path they got wrong.
        result = runner.invoke(
            app,
            [
                "install", "systemd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
                "--config", str(tmp_path / "nope.yaml"),
            ],
        )
        assert result.exit_code == 1
        assert "config" in result.output.lower()
        assert not (target_dir / "ctrlrelay-bridge.service").exists()

    def test_a_file_mock_config_keeps_the_telegram_placeholder(
        self,
        runner: CliRunner,
        workdir: Path,
        target_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # file_mock has no chat token and no bridge; there is no variable
        # to name, so the unit keeps the placeholder it has always had and
        # the warning stands. Pinned so the choice is deliberate.
        monkeypatch.delenv("CTRLRELAY_TELEGRAM_TOKEN", raising=False)
        config = _write_config(
            tmp_path / "orchestrator.yaml",
            {
                "type": "file_mock",
                "file_mock": {
                    "inbox": str(tmp_path / "inbox"),
                    "outbox": str(tmp_path / "outbox"),
                },
            },
            tmp_path=tmp_path,
        )
        result = runner.invoke(
            app,
            [
                "install", "systemd",
                "--workdir", str(workdir),
                "--target-dir", str(target_dir),
                "--config", str(config),
            ],
        )
        assert result.exit_code == 0, result.output
        written = (target_dir / "ctrlrelay-bridge.service").read_text()
        assert (
            _systemd_env(written).get("CTRLRELAY_TELEGRAM_TOKEN")
            == "${CTRLRELAY_TELEGRAM_TOKEN}"
        )
