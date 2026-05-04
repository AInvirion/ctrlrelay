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
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctrlrelay.cli import app
from ctrlrelay.install import (
    render_launchd,
    render_systemd,
    write_units,
)


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
        assert names == ["ctrlrelay-bridge.service", "ctrlrelay-poller.service"]

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
