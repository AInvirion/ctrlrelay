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

    def test_an_absolute_path_is_answered_about_itself(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A path names a file: the answer is about that file.

        This does NOT establish the separator guard, and it used to
        claim it did. An absolute name is immune to the fallback search
        either way, because `os.path.join(user_dir, "/abs")` discards
        `user_dir` — so this passes with the guard deleted.
        `test_a_relative_path_is_not_searched_for_either` is the one
        that detects that.
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

        This is the probe's own defensive rule. Both callers reject a
        relative separator-containing name before they get here, so
        deleting this guard alone cannot make the daemon run the decoy
        — the operator-visible claims are
        `test_a_relative_operator_path_is_not_claimed_about` and
        `test_a_relative_program_is_not_answered_for`. Kept because the
        probe is public and the next caller will not know the rule.
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


class TestTheSearchOrderIsTheDispatchersOwn:
    """This helper replaced a private copy in the dispatcher.

    A different candidate order is a different program on a box that
    has two, so the order is part of the behaviour rather than an
    implementation detail. An earlier version of this change inserted
    `~/.local/share/uv/tools` ahead of `/usr/local/bin` — which also
    could never match, because that directory holds one subdirectory
    per tool and a candidate built from it is never a file.
    """

    def test_the_order_is_user_bin_then_usr_local_then_homebrew(self) -> None:
        from ctrlrelay.core.binaries import USER_INSTALL_DIRS

        assert USER_INSTALL_DIRS == (
            "~/.local/bin",
            "/usr/local/bin",
            "/opt/homebrew/bin",
        )

    def test_the_earlier_directory_wins(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Not just the list — that the search honours it."""
        from ctrlrelay.core import binaries

        first, second = tmp_path / "first", tmp_path / "second"
        _executable(first, "dup")
        _executable(second, "dup")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(
            binaries, "USER_INSTALL_DIRS", (str(first), str(second))
        )
        assert binaries.probe_binary("dup") == str(first / "dup")


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

    def test_an_operator_given_path_that_is_missing_still_reports(
        self, tmp_path: Path
    ) -> None:
        """Not searched for elsewhere, but a miss is still a miss.

        The separator guard returned before any logging, so the one
        configuration an operator is most likely to get wrong — a full
        path with a typo in it — was the one that said nothing.
        """
        from ctrlrelay.core import binaries

        log = _Recorder()
        asked = str(tmp_path / "nope" / "review")
        got = binaries.resolve_binary(
            asked, logger=log, event_prefix="code_review.binary"
        )
        assert got == asked
        assert log.events() == ["code_review.binary.unresolved"]
        assert log.calls[0][1]["reason"] == "not_a_usable_file"

    def test_an_operator_given_path_that_works_says_nothing(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core import binaries

        target = _executable(tmp_path / "opt", "review")
        log = _Recorder()
        got = binaries.resolve_binary(
            str(target), logger=log, event_prefix="code_review.binary"
        )
        assert got == str(target)
        assert log.events() == []

    def test_a_relative_path_entry_still_yields_an_absolute_answer(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The caller execs with its own cwd, so a relative answer is a
        different file.

        With a relative PATH entry, `shutil.which` inspects a file
        under the DAEMON's directory and hands back a relative name.
        `create_subprocess_exec(cwd=worktree_path)` would then resolve
        that name under the worktree — running a different program than
        the one just checked, or none.
        """
        from ctrlrelay.core import binaries

        _executable(tmp_path / "relbin", "review")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PATH", "relbin")

        log = _Recorder()
        got = binaries.resolve_binary(
            "review", logger=log, event_prefix="code_review.binary"
        )
        assert os.path.isabs(got), got
        assert got == str(tmp_path / "relbin" / "review")
        assert log.events() == []

    def test_a_relative_operator_path_is_not_claimed_about(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Only an absolute operator-given path is answerable here.

        A relative one is resolved by the child against the worktree,
        so reporting a miss from the daemon's directory would be a
        claim about the wrong tree — and the first version of this
        reported it.
        """
        from ctrlrelay.core import binaries

        monkeypatch.chdir(tmp_path)
        log = _Recorder()
        got = binaries.resolve_binary(
            os.path.join("tools", "review"),
            logger=log,
            event_prefix="code_review.binary",
        )
        assert got == os.path.join("tools", "review")
        assert log.events() == []

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

    def test_a_relative_program_is_not_answered_for(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`exec` resolves it against the CHILD's cwd, not the daemon's.

        The child's cwd is the repo's worktree. Probing `./tools/review`
        from the daemon's own directory answers a different question: it
        would report a script that is present in the repo as missing,
        and stay quiet when a same-named file happens to sit beside the
        daemon.
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
                    "code_review": {"cli_command": "./tools/review --x"},
                }
            ],
        )
        assert unresolved_review_programs(config) == {}

    def test_an_absolute_program_is_answered_for(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The other half, and the one the first version got wrong.

        An absolute path does not depend on anybody's working
        directory, so startup can answer it — and skipping it meant the
        configuration an operator is most likely to typo stayed silent
        until the first issue of the day.
        """
        from ctrlrelay.cli import unresolved_review_programs
        from ctrlrelay.core import binaries

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(tmp_path),))

        missing = str(tmp_path / "nope" / "review")
        present = _executable(tmp_path / "opt", "review")
        config = self._config(
            tmp_path,
            [
                {"name": "o/a", "code_review": {"cli_command": missing}},
                {
                    "name": "o/b",
                    "code_review": {"cli_command": f"{present} --x"},
                },
            ],
        )
        # Both directions in one call: the missing one is reported and
        # the usable one is not.
        assert unresolved_review_programs(config) == {missing: ["o/a"]}

    def test_a_blank_command_is_reported_at_startup(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """It fails every review deterministically, so it is boot news.

        Skipping it left exactly the silence this change removes: the
        runtime guard returns FAILED for every review and nothing at
        startup said why.
        """
        from ctrlrelay.cli import unresolved_review_programs
        from ctrlrelay.core import binaries

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(binaries, "USER_INSTALL_DIRS", (str(tmp_path),))

        config = self._config(
            tmp_path,
            [{"name": "o/a", "code_review": {"cli_command": "   "}}],
        )
        assert unresolved_review_programs(config) == {"": ["o/a"]}

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

    def test_no_rendered_path_carries_an_unsubstituted_variable(
        self, tmp_path: Path
    ) -> None:
        """Neither systemd nor launchd expands a shell-style reference.

        `safe_substitute` leaves an unknown `${VAR}` intact by design,
        so a `HOME` missing from the mapping would render a PATH whose
        first entry is the literal text `${HOME}/.local/bin` — a
        directory that does not exist, silently undoing the fix on a
        fresh install while every substring check still passes.
        """
        import plistlib

        from ctrlrelay.install import render_launchd, render_systemd

        examined = 0
        for unit in render_systemd(workdir=tmp_path):
            for line in unit.content.splitlines():
                if line.startswith("Environment=PATH="):
                    assert "${" not in line, (unit.service, line)
                    examined += 1
        for unit in render_launchd(workdir=tmp_path):
            value = plistlib.loads(unit.content.encode())[
                "EnvironmentVariables"
            ]["PATH"]
            assert "${" not in value, (unit.service, value)
            examined += 1
        # Four units, four PATHs. Without this the whole test passes if
        # a renderer returns nothing, or stops emitting a PATH at all —
        # an absence assertion over an empty set.
        assert examined == 4, examined

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


class TestWhatTheOperatorIsTold:
    """The log event and the console line, which had disagreed.

    For a blank `cli_command` the event said there was nothing to
    install while the console said to install it. One function builds
    both now, so they cannot drift apart without a test noticing.
    """

    def test_a_blank_command_does_not_tell_anybody_to_install_something(
        self,
    ) -> None:
        from ctrlrelay.cli import describe_unresolved_reviewer

        detail, line = describe_unresolved_reviewer("", ["o/a"])
        assert "nothing to install" in detail
        assert "install" not in line.lower(), line
        assert "cli_command" in line

    def test_a_missing_tool_says_install_it_in_both(self) -> None:
        from ctrlrelay.cli import describe_unresolved_reviewer

        detail, line = describe_unresolved_reviewer("ghostly", ["o/a"])
        assert "installed" in detail
        assert "installed" in line
        assert "ghostly" in detail and "ghostly" in line

    def test_neither_predicts_the_future(self) -> None:
        """Resolution is re-done per spawn, so "will fail" is unkeepable.

        An earlier version said "every review on these repos will fail
        to spawn", which stops being true the moment somebody installs
        the tool — and the daemon needs no restart for that.
        """
        from ctrlrelay.cli import describe_unresolved_reviewer

        for program in ("", "ghostly"):
            detail, line = describe_unresolved_reviewer(program, ["o/a"])
            for text in (detail, line):
                assert "will fail" not in text, text
                assert "will not run" not in text, text

    def test_a_program_name_cannot_break_the_console(self) -> None:
        """The name comes from config and is interpolated into markup.

        `[bold]reviewer` opens a tag that is never closed, and
        `console.print` raises `MarkupError` — turning a missing
        reviewer into a daemon that will not start, which is the one
        outcome worse than the bug being fixed.
        """
        from rich.console import Console

        from ctrlrelay.cli import describe_unresolved_reviewer

        console = Console(file=open(os.devnull, "w"), force_terminal=False)
        try:
            for program in ("[bold]reviewer", "[/]", "rev[iewer", "[red"):
                _detail, line = describe_unresolved_reviewer(
                    program, ["o/a"]
                )
                # Raises MarkupError if the name was not escaped.
                console.print(f"[yellow]{line}[/yellow]")
        finally:
            console.file.close()

    def test_the_repo_list_is_capped_and_says_how_many_it_hid(self) -> None:
        from ctrlrelay.cli import describe_unresolved_reviewer

        _detail, line = describe_unresolved_reviewer(
            "ghostly", [f"o/r{i}" for i in range(7)]
        )
        assert "and 4 more" in line, line
