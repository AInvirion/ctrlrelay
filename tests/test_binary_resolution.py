"""Finding a tool the operator installed for themselves (#197).

The defect: a systemd user unit gets a minimal PATH and does not read
`~/.bashrc`, so the configured reviewer under `~/.local/bin` was
invisible to the poller. Every `code_review` spawn died with
`FileNotFoundError: [Errno 2] No such file or directory` on that
program's name, while `command -v` on the same name in the operator's
terminal answered a path under `~/.local/bin`.

Measured on alkaturbo 2026-10-11: the poller's own environment, read
from `/proc/2328/environ`, carried
`PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:
/usr/games:/usr/local/games:/snap/bin` and no `~/.local/bin`. The cost
was `AInvirion/zzsites#842`, opened with zero reviews on a repo whose
rule is reviewed-plus-green — and on the forge zero reviews is
indistinguishable from a review that has not run yet.

The dispatcher already carried this fix for the agent binary and the
review path had no copy, which is why the search now lives in one
module rather than two.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


def _executable(directory: Path, name: str, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


class _Recorder:
    """A logger that records instead of emitting."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def warning(self, event, extra=None, **_kw) -> None:
        self.calls.append((event, dict(extra or {})))

    def events(self) -> list[str]:
        return [e for e, _ in self.calls]


class TestProbe:
    def test_a_tool_only_in_a_user_dir_is_found(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The exact shape of the outage: installed, and not on PATH."""
        from ctrlrelay.core import binaries

        userbin = tmp_path / "userbin"
        _executable(userbin, "fakereviewer")
        # A PATH that does NOT contain it, which is what the unit gets.
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(userbin),))

        assert binaries.probe_binary("fakereviewer") == str(
            userbin / "fakereviewer"
        )

    def test_a_tool_nowhere_is_not_found(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from ctrlrelay.core import binaries

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(tmp_path),))
        assert binaries.probe_binary("definitely-not-a-tool") is None

    def test_a_file_that_is_not_executable_is_not_found(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Present is not runnable.

        A non-executable file of the right name would otherwise be
        returned and then fail at exec — trading a clear "not found"
        for a confusing "permission denied".
        """
        from ctrlrelay.core import binaries

        userbin = tmp_path / "userbin"
        userbin.mkdir()
        (userbin / "fakereviewer").write_text("not executable")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(userbin),))
        assert binaries.probe_binary("fakereviewer") is None

    def test_an_absolute_path_the_operator_gave_is_not_searched_for(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A path names a file. Searching elsewhere would override them.

        Without this, configuring `/opt/vendor/bin/review` on a box
        where that file is missing would silently run a DIFFERENT
        `review` from a user directory.
        """
        from ctrlrelay.core import binaries

        decoy = tmp_path / "userbin"
        _executable(decoy, "review")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(decoy),))

        asked = tmp_path / "opt" / "review"
        assert binaries.probe_binary(str(asked)) is None

        _executable(asked.parent, "review")
        assert binaries.probe_binary(str(asked)) == str(asked)

    def test_a_relative_path_is_not_searched_for_either(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The case the absolute one above cannot see.

        `os.path.join(user_dir, "/abs/path")` discards the user dir, so
        an absolute path is immune to the fallback search whether or
        not anything guards it — and a test using one passes with the
        guard deleted. A RELATIVE path with a separator is not immune:
        `join(user_dir, "bin/review")` is a real place to look, so
        without the guard an operator who configured `bin/review` would
        silently run a different `bin/review` out of a user directory.

        Found by mutation: deleting the guard left every other case in
        this file green.
        """
        from ctrlrelay.core import binaries

        userbin = tmp_path / "userbin"
        _executable(userbin / "bin", "review")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(userbin),))
        monkeypatch.chdir(tmp_path)

        # Nothing at ./bin/review relative to the cwd, and the decoy at
        # <userbin>/bin/review must not stand in for it.
        assert binaries.probe_binary(os.path.join("bin", "review")) is None


class TestResolveAndSaySo:
    def test_a_fallback_is_logged_with_the_path_it_used(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from ctrlrelay.core import binaries

        userbin = tmp_path / "userbin"
        target = _executable(userbin, "fakereviewer")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(userbin),))

        log = _Recorder()
        got = binaries.resolve_binary(
            "fakereviewer", logger=log, event_prefix="code_review.binary"
        )
        assert got == str(target)
        assert log.events() == ["code_review.binary.fallback"]
        assert log.calls[0][1]["binary"] == str(target)

    def test_unresolved_returns_the_bare_name_and_says_so(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The bare name is deliberate.

        The caller then fails exactly as before — FileNotFoundError out
        of exec, naming the tool — rather than with a new error about
        resolution. What changes is that the log says it first.
        """
        from ctrlrelay.core import binaries

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(tmp_path),))

        log = _Recorder()
        got = binaries.resolve_binary(
            "definitely-not-a-tool",
            logger=log,
            event_prefix="code_review.binary",
        )
        assert got == "definitely-not-a-tool"
        assert log.events() == ["code_review.binary.unresolved"]

    def test_on_path_says_nothing(self, monkeypatch) -> None:
        """The ordinary case must not add a line per spawn."""
        from ctrlrelay.core import binaries

        log = _Recorder()
        got = binaries.resolve_binary(
            "sh", logger=log, event_prefix="code_review.binary"
        )
        assert got and os.path.isabs(got)
        assert log.events() == []


class TestTheReviewActuallySpawns:
    """The regression, driven through `run_code_review` and a real exec.

    Asserting the resolver returns a path does not prove the review
    path uses it. This runs a real executable that is not on PATH.
    """

    @pytest.mark.asyncio
    async def test_a_reviewer_only_in_a_user_dir_runs(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from ctrlrelay.core import binaries
        from ctrlrelay.core.code_review import ReviewStatus, run_code_review

        userbin = tmp_path / "userbin"
        # A transcript carrying a command that read the change, which is
        # what `find_inspection_evidence` looks for. Deliberately no
        # verdict-marker line: the evidence scan falls back to the whole
        # output without one, so this test does not depend on the
        # reviewer's own output format to prove the program ran.
        _executable(
            userbin,
            "fakereviewer",
            "#!/bin/sh\n"
            'echo "$ git diff origin/main...HEAD"\n'
            'echo "No findings."\n'
            "exit 0\n",
        )
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(userbin),))

        outcome = await run_code_review(
            repo="o/r",
            worktree_path=tmp_path,
            base_branch="main",
            cli_command="fakereviewer review",
        )
        assert outcome.status is ReviewStatus.REVIEWED, outcome.error
        assert outcome.evidence, "the transcript's git diff line was not seen"

    @pytest.mark.asyncio
    async def test_without_the_resolver_the_same_call_fails_to_spawn(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The negative control, in the same file as the claim.

        This is the state the daemon was in: the tool installed, the
        call correct, and `exec` unable to find it.
        """
        import ctrlrelay.core.code_review as cr
        from ctrlrelay.core import binaries
        from ctrlrelay.core.code_review import ReviewStatus, run_code_review

        userbin = tmp_path / "userbin"
        _executable(userbin, "fakereviewer")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(userbin),))
        # Reinstate the fault: pass the name through unresolved.
        monkeypatch.setattr(cr, "resolve_binary", lambda name, **_kw: name)

        outcome = await run_code_review(
            repo="o/r",
            worktree_path=tmp_path,
            base_branch="main",
            cli_command="fakereviewer review",
        )
        assert outcome.status is ReviewStatus.FAILED
        assert "FileNotFoundError" in outcome.error, outcome.error

    @pytest.mark.asyncio
    async def test_an_empty_cli_command_does_not_index_into_nothing(
        self, tmp_path: Path
    ) -> None:
        """`shlex.split("")` is `[]`, and `argv[0]` on that is IndexError.

        Resolving argv[0] introduced that reach; a blank or
        whitespace-only `cli_command` is a plausible config.
        """
        from ctrlrelay.core.code_review import ReviewStatus, run_code_review

        for command in ("", "   "):
            outcome = await run_code_review(
                repo="o/r",
                worktree_path=tmp_path,
                base_branch="main",
                cli_command=command,
            )
            assert outcome.status is ReviewStatus.FAILED, command
            assert "empty" in outcome.error, outcome.error


class TestTheStartupCheck:
    """Said once at boot, not once per pull request."""

    @staticmethod
    def _config(tmp_path: Path, repos: list[dict]):
        import yaml

        from ctrlrelay.core.config import load_config

        data = {
            "version": "1",
            "node_id": "n",
            "timezone": "UTC",
            "paths": {
                "repo_root": str(tmp_path / "src"),
                "state_db": str(tmp_path / "s.db"),
                "worktrees": str(tmp_path / "w"),
                "bare_repos": str(tmp_path / "r"),
                "contexts": str(tmp_path / "c"),
                "skills": str(tmp_path / "k"),
            },
            "agent": {"binary": "a", "default_timeout_seconds": 1800},
            "transport": {
                "type": "file_mock",
                "file_mock": {
                    "inbox": str(tmp_path / "in"),
                    "outbox": str(tmp_path / "out"),
                },
            },
            "dashboard": {"enabled": False},
            # repo_root so the entries need no local_path each.
            "repos": repos,
        }
        p = tmp_path / "orchestrator.yaml"
        p.write_text(yaml.dump(data))
        return load_config(p)

    def test_a_missing_reviewer_names_every_repo_that_wanted_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from ctrlrelay.cli import unresolved_review_programs
        from ctrlrelay.core import binaries

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(tmp_path),))

        config = self._config(
            tmp_path,
            [
                {"name": "o/a", "code_review": {"cli_command": "ghostly a"}},
                {"name": "o/b", "code_review": {"cli_command": "ghostly b"}},
            ],
        )
        assert unresolved_review_programs(config) == {
            "ghostly": ["o/a", "o/b"]
        }

    def test_a_reviewer_that_resolves_is_not_reported(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Both directions. A check that can only say yes is half a check."""
        from ctrlrelay.cli import unresolved_review_programs
        from ctrlrelay.core import binaries

        userbin = tmp_path / "userbin"
        _executable(userbin, "ghostly")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(userbin),))

        config = self._config(
            tmp_path,
            [{"name": "o/a", "code_review": {"cli_command": "ghostly a"}}],
        )
        assert unresolved_review_programs(config) == {}

    def test_review_switched_off_is_not_a_fault(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A reviewer nobody asked for being absent is not news.

        Without this the operator gets a warning per repo for a setting
        they chose, which is how a warning teaches people to ignore
        warnings.
        """
        from ctrlrelay.cli import unresolved_review_programs
        from ctrlrelay.core import binaries

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(tmp_path),))

        config = self._config(
            tmp_path,
            [
                {
                    "name": "o/a",
                    "code_review": {
                        "method": "off",
                        "cli_command": "ghostly a",
                    },
                }
            ],
        )
        assert unresolved_review_programs(config) == {}

    def test_an_unbalanced_quote_is_not_reported_as_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A different fault, already reported per run with the repo named.

        Calling it a missing program would send somebody to install
        something that is already there.
        """
        from ctrlrelay.cli import unresolved_review_programs
        from ctrlrelay.core import binaries

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(tmp_path),))

        config = self._config(
            tmp_path,
            [{"name": "o/a", "code_review": {"cli_command": 'ghostly "'}}],
        )
        assert unresolved_review_programs(config) == {}


class TestTheUnitsCarryTheUserBinDir:
    """A fresh install should not inherit the fault.

    The fallback search above covers a unit already on disk. These
    assert the rendered units, which is what a new machine gets.
    """

    def test_systemd_units_put_the_user_bin_dir_first(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.install import render_systemd

        expected = str(Path.home() / ".local" / "bin")
        rendered = render_systemd(workdir=tmp_path)
        assert rendered, "render_systemd produced no units"
        for unit in rendered:
            lines = [
                ln
                for ln in unit.content.splitlines()
                if ln.startswith("Environment=PATH=")
            ]
            assert len(lines) == 1, (unit.service, lines)
            value = lines[0].split("=", 2)[2]
            assert value.split(":")[0] == expected, (unit.service, value)

    def test_launchd_jobs_put_the_user_bin_dir_first(
        self, tmp_path: Path
    ) -> None:
        import plistlib

        from ctrlrelay.install import render_launchd

        expected = str(Path.home() / ".local" / "bin")
        rendered = render_launchd(workdir=tmp_path)
        assert rendered, "render_launchd produced no jobs"
        for unit in rendered:
            # Parsed, not grepped: a PATH inside a comment would
            # satisfy a substring match and reach no process.
            plist = plistlib.loads(unit.content.encode())
            value = plist["EnvironmentVariables"]["PATH"]
            assert value.split(":")[0] == expected, (unit.service, value)
