"""`ctrlrelay agent check` (#381): does a backend write its checkpoint?

Every test here runs a **real** subprocess — a shell script standing in
for a coding CLI, enrolled through `agent.plugins` exactly as Codex or
Copilot would be. No network, no model, and nothing mocked between the
command and the process, because the thing under test is precisely what
happens between them: that `CTRLRELAY_STATE_FILE` reaches the child, that
a checkpoint written there is read back, and that a child which exits
zero without writing one is reported as a failure rather than a success.

A mocked `spawn_session` would have passed every one of these while the
environment variable was misspelled. The sink variables the fakes write
to are deliberately NOT named `CTRLRELAY_*`: every variable with that
prefix is stripped from an agent's environment, so a test that reached
for one would have found an empty value and blamed the command.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

# The fake CLI reads the two variables the relay promises every backend,
# so a test that passes proves the promise was kept.
_WRITES_DONE = """#!/bin/sh
mkdir -p -- "$(dirname "$CTRLRELAY_STATE_FILE")"
printf '{"version":"1","status":"DONE","session_id":"%s",' \\
  "$CTRLRELAY_SESSION_ID" > "$CTRLRELAY_STATE_FILE"
printf '"timestamp":"2026-11-07T00:00:00Z","summary":"fake agent ran"}' \\
  >> "$CTRLRELAY_STATE_FILE"
printf '{"sessionId":"fake-native-7f3a"}\\n'
exit 0
"""

# The failure this command exists to find: a clean exit and no checkpoint.
_SILENT_SUCCESS = """#!/bin/sh
printf 'I did the work.\\n'
exit 0
"""

_WRITES_BLOCKED = """#!/bin/sh
mkdir -p -- "$(dirname "$CTRLRELAY_STATE_FILE")"
printf '{"version":"1","status":"BLOCKED_NEEDS_INPUT","session_id":"%s",' \\
  "$CTRLRELAY_SESSION_ID" > "$CTRLRELAY_STATE_FILE"
printf '"timestamp":"2026-11-07T00:00:00Z","question":"which repo?"}' \\
  >> "$CTRLRELAY_STATE_FILE"
exit 0
"""

_WRITES_RUBBISH = """#!/bin/sh
mkdir -p -- "$(dirname "$CTRLRELAY_STATE_FILE")"
printf '{"status":"FINISHED!"}' > "$CTRLRELAY_STATE_FILE"
exit 0
"""

_FAILS_LOUDLY = """#!/bin/sh
printf 'boom\\n' >&2
exit 17
"""


def _with_prelude(prelude: str) -> str:
    """``_WRITES_DONE`` with extra commands first, keeping its shebang."""
    _, body = _WRITES_DONE.split("\n", 1)
    return f"#!/bin/sh\n{prelude}\n{body}"


def _script(tmp_path: Path, body: str, name: str = "fake-agent") -> Path:
    path = tmp_path / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _config(tmp_path: Path, **plugins):
    """A Config whose default backend is claude and whose plugins are fakes."""
    from ctrlrelay.core.config import Config

    root = tmp_path / "state"
    return Config.model_validate({
        "node_id": "testbox",
        "paths": {
            "state_db": str(root / "state.db"),
            "worktrees": str(root / "worktrees"),
            "bare_repos": str(root / "bare"),
            "contexts": str(root / "contexts"),
            "skills": str(root / "skills"),
            "repo_root": str(root / "projects"),
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {
                "inbox": str(root / "inbox.txt"),
                "outbox": str(root / "outbox.txt"),
            },
        },
        "agent": {"type": "claude", "plugins": plugins},
        "repos": [{"name": "AInvirion/ctrlrelay"}],
    })


def _plugin(script: Path, **extra) -> dict:
    manifest = {"binary": str(script), "session_id_path": "sessionId"}
    manifest.update(extra)
    return manifest


def _run(config, agent_type: str, *, timeout: int = 60):
    from ctrlrelay.core.agent_check import run_agent_check

    return asyncio.run(run_agent_check(config, agent_type, timeout=timeout))


def test_git_is_available_for_the_throwaway_repo() -> None:
    """A negative control for every test below: they all need a real git,
    and `git` missing would otherwise read as a backend failure."""
    assert shutil.which("git"), "these tests cannot run without git"


class TestABackendThatWritesTheCheckpoint:
    def test_a_done_checkpoint_and_a_native_session_id_are_both_reported(
        self, tmp_path: Path
    ) -> None:
        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_DONE)),
        )

        report = _run(config, "fake")

        assert report.ok is True
        assert report.exit_code == 0
        assert report.checkpoint == "DONE"
        assert report.checkpoint_written is True
        assert report.agent_session_id == "fake-native-7f3a"
        assert report.agent_type == "fake"
        assert report.plugin_id == "generic:fake"
        assert report.fingerprint
        assert report.fingerprint_prefix == report.fingerprint[:12]
        assert report.elapsed_seconds >= 0
        assert report.timed_out is False

    def test_the_report_names_each_observation_on_its_own_line(
        self, tmp_path: Path
    ) -> None:
        """Separate lines because they fail separately. A CLI that writes
        the checkpoint but reports no session id looks resumable and is
        not; collapsing the two into one verdict hides which you have."""
        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_DONE)),
        )

        lines = _run(config, "fake").lines()

        assert len(lines) == 5
        assert lines[0] == "exit code: 0"
        assert lines[1] == "checkpoint: written, status DONE"
        assert lines[2] == "native session id: fake-native-7f3a"
        assert "type=fake" in lines[3] and "plugin=generic:fake" in lines[3]
        assert lines[4].startswith("elapsed: ")

    def test_the_json_report_carries_the_same_facts(self, tmp_path: Path) -> None:
        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_DONE)),
        )

        payload = json.loads(json.dumps(_run(config, "fake").as_dict()))

        assert payload["ok"] is True
        assert payload["exit_code"] == 0
        assert payload["checkpoint"] == "DONE"
        assert payload["checkpoint_written"] is True
        assert payload["agent_session_id"] == "fake-native-7f3a"
        assert payload["agent_type"] == "fake"
        assert payload["plugin_id"] == "generic:fake"
        assert payload["timed_out"] is False

    def test_a_backend_with_no_session_id_mapping_says_none(
        self, tmp_path: Path
    ) -> None:
        """No mapping is not a failure, and it is not a captured id either.

        A generic manifest may legitimately have no resume support. What
        must not happen is the report implying one was captured."""
        script = _script(tmp_path, _WRITES_DONE)
        config = _config(tmp_path, fake={"binary": str(script)})

        report = _run(config, "fake")

        assert report.ok is True
        assert report.agent_session_id is None
        assert "native session id: none" in report.lines()


class TestABackendThatDoesNot:
    def test_exiting_zero_without_a_checkpoint_is_not_success(
        self, tmp_path: Path
    ) -> None:
        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _SILENT_SUCCESS)),
        )

        report = _run(config, "fake")

        assert report.exit_code == 0
        assert report.ok is False
        assert report.checkpoint_written is False
        assert report.checkpoint == "none"
        assert "no checkpoint" in report.checkpoint_line()
        assert report.as_dict()["ok"] is False

    def test_a_blocked_checkpoint_is_written_but_still_not_ok(
        self, tmp_path: Path
    ) -> None:
        """The command answers "does it write the checkpoint", and here the
        answer is yes — but a check that asked for DONE and got BLOCKED
        did not get what it asked for, so it must not exit 0."""
        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_BLOCKED)),
        )

        report = _run(config, "fake")

        assert report.checkpoint == "BLOCKED_NEEDS_INPUT"
        assert report.checkpoint_written is True
        assert report.ok is False

    def test_an_unparseable_checkpoint_is_distinguished_from_none(
        self, tmp_path: Path
    ) -> None:
        """Two different defects: nothing written, and something written
        that is not a checkpoint. The dispatcher returns `state=None` for
        both, so the report has to look at the file to tell them apart."""
        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_RUBBISH)),
        )

        report = _run(config, "fake")

        assert report.checkpoint == "unreadable"
        assert report.checkpoint_written is True
        assert report.ok is False
        assert "could not be parsed" in report.checkpoint_line()

    def test_a_nonzero_exit_is_reported_with_its_code(self, tmp_path: Path) -> None:
        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _FAILS_LOUDLY)),
        )

        report = _run(config, "fake")

        assert report.exit_code == 17
        assert report.ok is False
        assert "boom" in report.stderr_tail


class TestAnUnknownType:
    def test_it_fails_naming_every_enrolled_type(self, tmp_path: Path) -> None:
        from ctrlrelay.core.agent_check import AgentCheckError

        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_DONE)),
        )

        with pytest.raises(AgentCheckError) as excinfo:
            _run(config, "nosuchagent")

        message = str(excinfo.value)
        assert "nosuchagent" in message
        # Every type this node could build, not only the ones built so far:
        # `opencode` and `fake` are lazy, and naming only `claude` would
        # tell an operator their own plugin is not enrolled.
        for enrolled in ("claude", "opencode", "fake"):
            assert enrolled in message

    def test_a_plugin_whose_binary_is_missing_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        """Enrolled and unbuildable is a refusal for this run, not a crash.

        The manifest is valid config; the executable is gone. The refusal
        has to name the type, because the operator's next question is
        which of several plugins they must go and install."""
        from ctrlrelay.core.agent_check import AgentCheckError

        config = _config(tmp_path, ghost={"binary": "ctrlrelay-no-such-binary"})

        with pytest.raises(AgentCheckError) as excinfo:
            _run(config, "ghost")

        assert "ghost" in str(excinfo.value)


class TestTheThrowawayRepository:
    def test_it_is_a_git_repository_with_one_commit_and_a_readme(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.core.agent_check import make_throwaway_repo

        repo = make_throwaway_repo(tmp_path)

        assert (repo / ".git").exists()
        assert (repo / "README.md").exists()
        count = subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        assert count.stdout.strip() == "1"

    def test_the_agent_runs_inside_it_and_the_relay_removes_it_afterwards(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No real repository is touched, and nothing is left behind.

        The fake records its own working directory, so this asserts where
        the run actually happened rather than what was configured."""
        sink = tmp_path / "cwd.txt"
        monkeypatch.setenv("FAKE_AGENT_CWD_SINK", str(sink))
        config = _config(
            tmp_path,
            fake=_plugin(_script(
                tmp_path, _with_prelude('pwd > "$FAKE_AGENT_CWD_SINK"'),
            )),
        )

        report = _run(config, "fake")

        assert report.ok is True
        where = Path(sink.read_text().strip())
        assert where.name == "repo"
        assert where != tmp_path and tmp_path not in where.parents
        assert not where.exists(), "the throwaway repo must be removed"
        assert not where.parent.exists(), "the temp root must be removed too"

    def test_the_temp_root_goes_even_when_the_repo_cannot_be_prepared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """"Clean up whatever happens" includes the paths that raise.

        The spy on `mkdtemp` is how this knows WHICH directory to look
        for. Globbing the temp directory would pass while another test's
        leftover sat beside the one this run made."""
        import ctrlrelay.core.agent_check as agent_check

        created: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def spy(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        def explode(root: Path) -> Path:
            raise agent_check.AgentCheckError("git exited 128")

        monkeypatch.setattr(tempfile, "mkdtemp", spy)
        monkeypatch.setattr(agent_check, "make_throwaway_repo", explode)
        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_DONE)),
        )

        with pytest.raises(agent_check.AgentCheckError):
            _run(config, "fake")

        assert created, "the check never made a temp root"
        assert not created[0].exists()


class TestThePrompt:
    def test_it_hands_the_agent_the_done_snippet_and_the_state_file(self) -> None:
        """The snippet IS the task. If the prompt stopped carrying it the
        check would still pass against a cooperative fake, and tell an
        operator nothing about a real CLI."""
        from ctrlrelay.core.agent_check import check_prompt

        prompt = check_prompt("/tmp/w/.ctrlrelay/state.json", "check-fake-abcd")

        assert prompt.startswith("Run this exact shell command, then stop.")
        assert "STATE_FILE: /tmp/w/.ctrlrelay/state.json" in prompt
        assert "SESSION_ID: check-fake-abcd" in prompt
        assert "mkdir -p -- /tmp/w/.ctrlrelay" in prompt
        assert "python3 - /tmp/w/.ctrlrelay/state.json <<'CTRLRELAY_DONE'" in prompt
        assert '"status": "DONE"' in prompt
        assert '"session_id": "check-fake-abcd"' in prompt

    def test_a_path_with_an_apostrophe_is_quoted_not_concatenated(self) -> None:
        """#191: hand-quoting wrote /tmp/a'b'c/state.json to /tmp/abc/."""
        from ctrlrelay.core.agent_check import check_prompt

        prompt = check_prompt("/tmp/a'b'c/.ctrlrelay/state.json", "s1")

        assert "/tmp/abc" not in prompt
        assert "'/tmp/a'\"'\"'b'\"'\"'c/.ctrlrelay/state.json'" in prompt

    def test_the_prompt_the_agent_actually_receives_carries_the_snippet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserted on the argv the child saw, not on the string the
        builder returned: a run could be wired to send a different one."""
        sink = tmp_path / "prompt.txt"
        monkeypatch.setenv("FAKE_AGENT_PROMPT_SINK", str(sink))
        config = _config(
            tmp_path,
            fake=_plugin(
                _script(
                    tmp_path,
                    _with_prelude('printf %s "$1" > "$FAKE_AGENT_PROMPT_SINK"'),
                ),
                args=["{prompt}"],
            ),
        )

        report = _run(config, "fake")

        assert report.ok is True
        seen = sink.read_text()
        assert seen.startswith("Run this exact shell command, then stop.")
        assert "CTRLRELAY_DONE" in seen
        assert ".ctrlrelay/state.json" in seen


class TestTheAllowlistIsNotConsulted:
    def test_an_enrolled_type_runs_without_being_assignable(
        self, tmp_path: Path
    ) -> None:
        """`agent.assignable_types` governs assignments arriving from
        outside the machine. An operator holding the config is already
        past that boundary, and making the proof need an entry would push
        them to widen the one list that exists to stay narrow."""
        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_DONE)),
        )
        assert config.agent.assignable_types == []

        assert _run(config, "fake").ok is True

    def test_the_assignment_seam_still_refuses_the_same_type(
        self, tmp_path: Path
    ) -> None:
        """The paired half of the test above. If `select_agent` ever let an
        un-allowlisted type through, the test above would keep passing and
        the allowlist would be gone."""
        from ctrlrelay.core.dispatcher import (
            AgentSelectionError,
            make_repo_agent_dispatcher,
            select_agent,
        )

        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_DONE)),
        )
        dispatcher = make_repo_agent_dispatcher(
            config, outcome_hook=None, usage_store=None,
        )

        with pytest.raises(AgentSelectionError):
            select_agent(dispatcher, repo=None, requested_type="fake")

    def test_the_default_backend_is_selectable_by_its_own_name(
        self, tmp_path: Path
    ) -> None:
        """A single-backend deployment gets back a bare adapter with no
        `by_type` mapping, so selecting `claude` there has to come from the
        adapter's own type or it fails as unenrolled."""
        from ctrlrelay.core.dispatcher import (
            make_repo_agent_dispatcher,
            select_enrolled_agent,
        )

        config = _config(tmp_path)
        dispatcher = make_repo_agent_dispatcher(
            config, outcome_hook=None, usage_store=None,
        )
        assert not hasattr(dispatcher, "by_type"), "expected the bare adapter"

        selection = select_enrolled_agent(dispatcher, "claude")

        assert selection.identity.agent_type == "claude"
        assert selection.dispatcher is dispatcher


class TestTheCliCommand:
    def _invoke(self, tmp_path: Path, args: list[str], plugins: dict):
        import yaml
        from typer.testing import CliRunner

        from ctrlrelay.cli import app

        config = _config(tmp_path, **plugins)
        config_path = tmp_path / "orchestrator.yaml"
        config_path.write_text(yaml.safe_dump(json.loads(config.model_dump_json())))
        return CliRunner().invoke(
            app, ["agent", "check", *args, "--config", str(config_path)],
        )

    def test_a_done_checkpoint_exits_zero_and_prints_the_report(
        self, tmp_path: Path
    ) -> None:
        result = self._invoke(
            tmp_path, ["fake"],
            {"fake": _plugin(_script(tmp_path, _WRITES_DONE))},
        )

        assert result.exit_code == 0, result.output
        assert "exit code: 0" in result.output
        assert "status DONE" in result.output
        assert "fake-native-7f3a" in result.output

    def test_no_checkpoint_exits_non_zero_and_says_so(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path, ["fake"],
            {"fake": _plugin(_script(tmp_path, _SILENT_SUCCESS))},
        )

        assert result.exit_code == 1, result.output
        assert "no checkpoint" in result.output

    def test_a_failing_backend_shows_what_it_said_on_stderr(
        self, tmp_path: Path
    ) -> None:
        """The next question after "it did not write the checkpoint" is
        always "what did it say instead". Without this the operator has to
        go and find the log to read it."""
        result = self._invoke(
            tmp_path, ["fake"],
            {"fake": _plugin(_script(tmp_path, _FAILS_LOUDLY))},
        )

        assert result.exit_code == 1, result.output
        assert "exit code: 17" in result.output
        assert "boom" in result.output

    def test_an_unknown_type_exits_non_zero_listing_the_enrolled_types(
        self, tmp_path: Path
    ) -> None:
        result = self._invoke(
            tmp_path, ["nosuchagent"],
            {"fake": _plugin(_script(tmp_path, _WRITES_DONE))},
        )

        assert result.exit_code == 1, result.output
        assert "nosuchagent" in result.output
        for enrolled in ("claude", "opencode", "fake"):
            assert enrolled in result.output

    def test_json_prints_one_object_and_nothing_else(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path, ["fake", "--json"],
            {"fake": _plugin(_script(tmp_path, _WRITES_DONE))},
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        assert payload["checkpoint"] == "DONE"
        assert payload["agent_session_id"] == "fake-native-7f3a"

    def test_json_reports_an_unknown_type_as_an_object_too(
        self, tmp_path: Path
    ) -> None:
        """A caller that asked for JSON gets JSON on the failure path too,
        or it has to parse prose to find out what broke."""
        result = self._invoke(
            tmp_path, ["nosuchagent", "--json"],
            {"fake": _plugin(_script(tmp_path, _WRITES_DONE))},
        )

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is False
        assert "nosuchagent" in payload["error"]

    def test_json_stdout_is_one_object_when_the_real_process_runs(
        self, tmp_path: Path
    ) -> None:
        """Run as a real subprocess, because `CliRunner` cannot see this.

        The structured log handler binds `sys.stdout` when the module is
        imported, long before `CliRunner` swaps the stream — so under the
        runner the log lines go somewhere else and `--json` looks clean
        whether or not it is. The INFO lines that broke `jq` for an
        operator are only visible from outside the process.
        """
        import sys

        import yaml

        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_DONE)),
        )
        config_path = tmp_path / "orchestrator.yaml"
        config_path.write_text(yaml.safe_dump(json.loads(config.model_dump_json())))

        proc = subprocess.run(
            [
                sys.executable, "-m", "ctrlrelay.cli",
                "agent", "check", "fake", "--json",
                "--config", str(config_path),
            ],
            capture_output=True, text=True, check=False,
        )

        assert proc.returncode == 0, proc.stderr
        assert len(proc.stdout.strip().splitlines()) == 1, proc.stdout
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert payload["checkpoint"] == "DONE"
        # The log was not silenced, only moved: the events are still there.
        assert "agent_check.finished" in proc.stderr

    def test_the_human_report_still_goes_to_stdout_with_the_log(
        self, tmp_path: Path
    ) -> None:
        """The paired half: without `--json` nothing moves, so an operator
        reading one stream sees the run and the report together, as every
        other command in this CLI behaves."""
        import sys

        import yaml

        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_DONE)),
        )
        config_path = tmp_path / "orchestrator.yaml"
        config_path.write_text(yaml.safe_dump(json.loads(config.model_dump_json())))

        proc = subprocess.run(
            [
                sys.executable, "-m", "ctrlrelay.cli",
                "agent", "check", "fake", "--config", str(config_path),
            ],
            capture_output=True, text=True, check=False,
        )

        assert proc.returncode == 0, proc.stderr
        assert "checkpoint: written, status DONE" in proc.stdout
        assert "agent_check.finished" in proc.stdout

    def test_the_timeout_option_defaults_to_three_hundred_seconds(self) -> None:
        """Named here because the help text is the only place an operator
        sees it, and a default that drifts from the documented one is a
        run that dies earlier or later than the docs promise."""
        from ctrlrelay.core.agent_check import DEFAULT_TIMEOUT_SECONDS

        assert DEFAULT_TIMEOUT_SECONDS == 300


# Writes a valid DONE checkpoint, then never exits.
_WRITES_DONE_THEN_HANGS = _with_prelude("").replace(
    "exit 0\n", "exec sleep 600\n",
)


class TestABackendThatWritesThenHangs:
    def test_it_is_reported_as_timed_out_with_the_checkpoint_it_wrote(
        self, tmp_path: Path
    ) -> None:
        """The dispatcher returns before reading the checkpoint on its
        timeout path, so the file is still on disk and valid. Calling it
        unparseable sends the operator after the wrong defect: the
        checkpoint is fine, and the CLI's failure is that it never exits."""
        from ctrlrelay.core.agent_check import UNREADABLE_CHECKPOINT

        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _WRITES_DONE_THEN_HANGS)),
        )
        report = _run(config, "fake", timeout=2)

        assert report.timed_out
        assert report.checkpoint != UNREADABLE_CHECKPOINT
        assert report.checkpoint == "DONE"
        assert not report.ok
        assert "stopped it" in report.lines()[0]


class TestTheCliRefusesWhatItCannotRun:
    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_a_timeout_that_is_not_positive_is_refused(
        self, tmp_path: Path, value: str
    ) -> None:
        """Zero would not mean "no time": the adapters read `timeout or
        default`, so `--timeout 0` quietly became the configured session
        timeout, which can be half an hour."""
        result = TestTheCliCommand()._invoke(
            tmp_path, ["fake", "--timeout", value],
            {"fake": _plugin(_script(tmp_path, _WRITES_DONE))},
        )

        assert result.exit_code != 0
        assert "status DONE" not in result.output

    def test_json_reports_an_undiscoverable_config_as_an_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auto-discovery fails before the config is loaded, on a path that
        printed prose whatever the caller had asked for."""
        from typer.testing import CliRunner

        import ctrlrelay.core.config as config_module
        from ctrlrelay.cli import app

        monkeypatch.setattr(
            config_module, "default_config_search_paths",
            lambda: [tmp_path / "nowhere.yaml"],
        )
        result = CliRunner().invoke(app, ["agent", "check", "fake", "--json"])

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is False
        assert payload["error"]

    def test_json_reports_a_temp_dir_failure_as_an_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OSError preparing the run is not an agent verdict, but a
        caller that asked for JSON still has to be able to read it."""
        import ctrlrelay.core.agent_check as agent_check

        def no_space(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(agent_check.tempfile, "mkdtemp", no_space)
        result = TestTheCliCommand()._invoke(
            tmp_path, ["fake", "--json"],
            {"fake": _plugin(_script(tmp_path, _WRITES_DONE))},
        )

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is False
        assert "No space left" in payload["error"]


class TestALeftoverStateFileIsReadSafely:
    def test_a_fifo_left_at_the_state_path_does_not_hang_the_report(
        self, tmp_path: Path
    ) -> None:
        """The agent controls that path. Opening a FIFO there would block
        forever — past the timeout and past the cleanup of the repository."""
        from ctrlrelay.core.agent_check import UNREADABLE_CHECKPOINT

        script = _script(tmp_path, """#!/bin/sh
mkdir -p -- "$(dirname "$CTRLRELAY_STATE_FILE")"
mkfifo "$CTRLRELAY_STATE_FILE"
exec sleep 600
""")
        report = _run(_config(tmp_path, fake=_plugin(script)), "fake", timeout=2)

        assert report.timed_out
        assert report.checkpoint == UNREADABLE_CHECKPOINT
        assert not report.ok


class TestALeftoverCheckpointThatBreaksTheParser:
    @pytest.mark.parametrize(
        "payload",
        [
            # Over CPython's int-string limit: a bare ValueError, not a
            # JSONDecodeError, out of json.loads.
            '{"version":"1","status":"DONE","n":' + "9" * 5000 + "}",
            # Deep enough to raise RecursionError in the decoder, and under the
            # size cap so it reaches the parser.
            "[" * 20000 + "]" * 20000,
        ],
        ids=["huge-int", "deep-nesting"],
    )
    def test_it_is_reported_as_unreadable_not_raised(
        self, tmp_path: Path, payload: str
    ) -> None:
        from ctrlrelay.core.agent_check import UNREADABLE_CHECKPOINT

        data = tmp_path / "payload.json"
        data.write_text(payload)
        script = _script(tmp_path, f"""#!/bin/sh
mkdir -p -- "$(dirname "$CTRLRELAY_STATE_FILE")"
cp {data} "$CTRLRELAY_STATE_FILE"
exec sleep 600
""")
        report = _run(_config(tmp_path, fake=_plugin(script)), "fake", timeout=2)

        assert report.checkpoint == UNREADABLE_CHECKPOINT
        assert not report.ok


def test_json_reports_a_non_positive_timeout_as_an_object(tmp_path: Path) -> None:
    result = TestTheCliCommand()._invoke(
        tmp_path, ["fake", "--json", "--timeout", "0"],
        {"fake": _plugin(_script(tmp_path, _WRITES_DONE))},
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is False
    assert "timeout" in payload["error"]


# --- the resume round (#382) ------------------------------------------------
#
# The fake below is the whole point of these tests: it has no memory of its
# own, so it stands in for a CLI that really does carry context across a
# resume. Round 1 reads the token out of its prompt and files it under the
# native session id it prints; round 2 is handed that id on the command line
# and can only answer by looking the token up. A fake that echoed the token
# straight back would pass while `--resume <id>` went nowhere.

_REMEMBERS_THE_TOKEN = """#!/bin/sh
mkdir -p -- "$FAKE_AGENT_MEMORY"
printf 'ran\\n' >> "$FAKE_AGENT_MEMORY/calls"
mkdir -p -- "$(dirname "$CTRLRELAY_STATE_FILE")"
if [ "$1" = "--resume" ]; then
  native="$2"
  printf '%s' "$3" > "$FAKE_AGENT_MEMORY/round2-prompt"
  token=$(cat "$FAKE_AGENT_MEMORY/$native" 2>/dev/null)
  if [ -n "$FAKE_AGENT_RESUME_LAST" ]; then
    token=$(cat "$FAKE_AGENT_MEMORY/last" 2>/dev/null)
  fi
  if [ -n "$FAKE_AGENT_ROUND2_SILENT" ]; then
    printf '{"sessionId":"%s"}\\n' "$native"
    exit 0
  fi
  printf '{"version":"1","status":"DONE","session_id":"%s",' \\
    "$CTRLRELAY_SESSION_ID" > "$CTRLRELAY_STATE_FILE"
  printf '"timestamp":"2026-11-07T00:00:00Z","summary":"%s"}' \\
    "${FAKE_AGENT_SUMMARY-$token}" >> "$CTRLRELAY_STATE_FILE"
  printf '{"sessionId":"%s"}\\n' "${FAKE_AGENT_ROUND2_NATIVE-$native}"
  exit 0
fi
native=fake-native-7f3a
if [ -e "$FAKE_AGENT_MEMORY/round1-prompt" ]; then
  native=fake-native-decoy
  printf '%s' "$1" > "$FAKE_AGENT_MEMORY/decoy-prompt"
else
  printf '%s' "$1" > "$FAKE_AGENT_MEMORY/round1-prompt"
fi
token=$(printf '%s' "$1" | sed -n 's/^TOKEN: \\([0-9a-f]*\\)$/\\1/p' | head -1)
printf '%s' "$token" > "$FAKE_AGENT_MEMORY/$native"
printf '%s' "$token" > "$FAKE_AGENT_MEMORY/last"
printf '{"version":"1","status":"%s","session_id":"%s",' \\
  "${FAKE_AGENT_ROUND1_STATUS-BLOCKED_NEEDS_INPUT}" \\
  "$CTRLRELAY_SESSION_ID" > "$CTRLRELAY_STATE_FILE"
printf '"timestamp":"2026-11-07T00:00:00Z","question":"What is the token?",' \\
  >> "$CTRLRELAY_STATE_FILE"
printf '"summary":"round one"}' >> "$CTRLRELAY_STATE_FILE"
if [ -z "$FAKE_AGENT_HIDE_NATIVE_ID" ]; then
  printf '{"sessionId":"%s"}\\n' "$native"
fi
exit 0
"""

_RESUMABLE = {
    "resume_args": ["--resume", "{resume_session_id}"],
    "session_id_path": "sessionId",
}


def _resumable_plugin(script: Path, **extra) -> dict:
    return _plugin(script, **{**_RESUMABLE, **extra})


def _memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the fake keeps the token, its call log and the prompts it saw."""
    where = tmp_path / "fake-memory"
    monkeypatch.setenv("FAKE_AGENT_MEMORY", str(where))
    return where


def _run_resume(config, agent_type: str, *, timeout: int = 60):
    from ctrlrelay.core.agent_check import run_agent_resume_check

    return asyncio.run(
        run_agent_resume_check(config, agent_type, timeout=timeout)
    )


def _calls(memory: Path) -> int:
    log = memory / "calls"
    return len(log.read_text().splitlines()) if log.exists() else 0


class TestABackendThatResumes:
    def test_both_rounds_pass_and_the_token_comes_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory = _memory(tmp_path, monkeypatch)
        config = _config(
            tmp_path,
            fake=_resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN)),
        )

        report = _run_resume(config, "fake")

        assert report.ok is True, report.failures
        assert report.failures == ()
        assert report.warnings == ()
        assert report.rounds_run == 2
        assert report.round1.checkpoint == "BLOCKED_NEEDS_INPUT"
        assert report.round1.agent_session_id == "fake-native-7f3a"
        assert report.round2 is not None
        assert report.round2.checkpoint == "DONE"
        assert report.round2.checkpoint_summary == report.token
        assert report.round2.agent_session_id == "fake-native-7f3a"
        assert len(report.token) == 8
        # Round 1, the decoy, round 2.
        assert _calls(memory) == 3

    def test_round_two_is_handed_the_native_id_round_one_captured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The resume argv is what is actually under test here.

        Without this, a pass would be consistent with round 2 starting a
        fresh session that happened to know the token — which is the one
        thing the token was introduced to rule out."""
        memory = _memory(tmp_path, monkeypatch)
        argv_sink = tmp_path / "argv.txt"
        monkeypatch.setenv("FAKE_AGENT_ARGV_SINK", str(argv_sink))
        body = _REMEMBERS_THE_TOKEN.replace(
            'if [ "$1" = "--resume" ]; then',
            'printf "%s\\n" "$@" > "$FAKE_AGENT_ARGV_SINK"\n'
            'if [ "$1" = "--resume" ]; then',
        )
        config = _config(
            tmp_path, fake=_resumable_plugin(_script(tmp_path, body)),
        )

        report = _run_resume(config, "fake")

        assert report.ok is True, report.failures
        seen = argv_sink.read_text().splitlines()
        assert seen[0] == "--resume"
        assert seen[1] == "fake-native-7f3a"
        # Round 1, the decoy, round 2.
        assert _calls(memory) == 3

    def test_the_token_is_only_in_the_first_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserted on the prompts the child actually received.

        If the token reached round 2 in its prompt, round 2 could answer
        without remembering anything and the check would prove nothing."""
        memory = _memory(tmp_path, monkeypatch)
        config = _config(
            tmp_path,
            fake=_resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN)),
        )

        report = _run_resume(config, "fake")

        assert report.ok is True, report.failures
        first = (memory / "round1-prompt").read_text()
        second = (memory / "round2-prompt").read_text()
        assert f"TOKEN: {report.token}" in first
        assert report.token not in second

    def test_the_report_has_a_line_per_round(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _memory(tmp_path, monkeypatch)
        config = _config(
            tmp_path,
            fake=_resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN)),
        )

        report = _run_resume(config, "fake")
        lines = report.lines()

        assert lines[0].startswith("round 1 (fresh):")
        assert "checkpoint BLOCKED_NEEDS_INPUT" in lines[0]
        assert "fake-native-7f3a" in lines[0]
        assert lines[1].startswith("decoy (fresh):")
        assert "fake-native-decoy" in lines[1]
        assert lines[2].startswith("round 2 (resume):")
        assert "checkpoint DONE" in lines[2]
        assert report.token in lines[3]
        assert "type=fake" in lines[4]
        assert lines[5].startswith("elapsed: ")

    def test_the_json_report_carries_both_rounds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _memory(tmp_path, monkeypatch)
        config = _config(
            tmp_path,
            fake=_resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN)),
        )

        payload = json.loads(json.dumps(_run_resume(config, "fake").as_dict()))

        assert payload["ok"] is True
        assert payload["resume"] is True
        assert payload["token"]
        assert payload["rounds_run"] == 2
        assert payload["round1"]["checkpoint"] == "BLOCKED_NEEDS_INPUT"
        assert payload["round1"]["agent_session_id"] == "fake-native-7f3a"
        assert payload["round2"]["checkpoint"] == "DONE"
        assert payload["round2"]["checkpoint_summary"] == payload["token"]
        assert payload["failures"] == []
        assert payload["warnings"] == []

    def test_a_new_native_id_on_resume_is_a_warning_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Some CLIs mint a new id per turn. The context came back, which
        is what was being proved; the id changing is worth saying and is
        not a failure."""
        _memory(tmp_path, monkeypatch)
        monkeypatch.setenv("FAKE_AGENT_ROUND2_NATIVE", "fake-native-99zz")
        config = _config(
            tmp_path,
            fake=_resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN)),
        )

        report = _run_resume(config, "fake")

        assert report.ok is True, report.failures
        assert report.failures == ()
        assert any("fake-native-99zz" in w for w in report.warnings)
        assert any("new native session id" in w for w in report.warnings)


class TestABackendThatDoesNotResume:
    def test_no_native_id_in_round_one_stops_before_round_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing to resume from is a failure of round 1, and the relay
        must not paper over it by starting a second fresh session — which
        is exactly what a pipeline resume without a stored id used to do."""
        memory = _memory(tmp_path, monkeypatch)
        monkeypatch.setenv("FAKE_AGENT_HIDE_NATIVE_ID", "1")
        config = _config(
            tmp_path,
            fake=_resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN)),
        )

        report = _run_resume(config, "fake")

        assert report.ok is False
        assert report.rounds_run == 1
        assert report.round2 is None
        assert report.round1.checkpoint == "BLOCKED_NEEDS_INPUT"
        assert any("native session id" in f for f in report.failures)
        assert _calls(memory) == 1, "round 2 must not have been spawned"

    def test_round_one_writing_done_instead_of_blocked_says_which_part(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory = _memory(tmp_path, monkeypatch)
        monkeypatch.setenv("FAKE_AGENT_ROUND1_STATUS", "DONE")
        config = _config(
            tmp_path,
            fake=_resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN)),
        )

        report = _run_resume(config, "fake")

        assert report.ok is False
        assert report.rounds_run == 1
        assert report.round1.checkpoint == "DONE"
        assert any(
            "BLOCKED_NEEDS_INPUT" in f and "DONE" in f for f in report.failures
        )
        assert _calls(memory) == 1

    def test_round_two_without_the_token_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DONE checkpoint is not the answer. The run that this check
        exists to predict is one where the agent is asked a question and
        comes back knowing what it was doing."""
        memory = _memory(tmp_path, monkeypatch)
        monkeypatch.setenv("FAKE_AGENT_SUMMARY", "I forgot the token")
        config = _config(
            tmp_path,
            fake=_resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN)),
        )

        report = _run_resume(config, "fake")

        assert report.ok is False
        assert report.rounds_run == 2
        assert report.round2 is not None
        assert report.round2.checkpoint == "DONE"
        assert report.round2.checkpoint_summary == "I forgot the token"
        assert any("token" in f for f in report.failures)
        # Round 1, the decoy, round 2.
        assert _calls(memory) == 3

    def test_round_two_writing_nothing_fails_naming_the_round(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exiting zero with no checkpoint is the original #381 failure,
        and it has to be reported against the round it happened in."""
        _memory(tmp_path, monkeypatch)
        monkeypatch.setenv("FAKE_AGENT_ROUND2_SILENT", "1")
        config = _config(
            tmp_path,
            fake=_resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN)),
        )

        report = _run_resume(config, "fake")

        assert report.ok is False
        assert report.round2 is not None
        assert report.round2.checkpoint == "none"
        assert any("round 2" in f for f in report.failures)


class TestABackendWithNoResumeConfigured:
    def test_it_fails_fast_without_spawning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `resume_args` means the first round could never be resumed,
        so running it would burn a session to learn what the manifest
        already says."""
        from ctrlrelay.core.agent_check import AgentCheckError

        memory = _memory(tmp_path, monkeypatch)
        config = _config(
            tmp_path, fake=_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN)),
        )

        with pytest.raises(AgentCheckError) as excinfo:
            _run_resume(config, "fake")

        assert "resume not configured for fake" in str(excinfo.value)
        assert _calls(memory) == 0, "nothing may be spawned"

    def test_a_manifest_with_resume_args_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The paired half. If the capability flag were hard-coded false
        the test above would pass and `--resume` would be unusable."""
        _memory(tmp_path, monkeypatch)
        config = _config(
            tmp_path,
            fake=_resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN)),
        )

        assert _run_resume(config, "fake").rounds_run == 2


class TestTheResumePrompts:
    def test_round_one_carries_the_token_and_the_blocked_snippet(self) -> None:
        from ctrlrelay.core.agent_check import resume_round1_prompt

        prompt = resume_round1_prompt(
            "/tmp/w/.ctrlrelay/state.json", "check-fake-abcd", "1a2b3c4d",
        )

        assert "TOKEN: 1a2b3c4d" in prompt
        assert "STATE_FILE: /tmp/w/.ctrlrelay/state.json" in prompt
        assert "SESSION_ID: check-fake-abcd" in prompt
        assert "mkdir -p -- /tmp/w/.ctrlrelay" in prompt
        assert '"status":"BLOCKED_NEEDS_INPUT"' in prompt
        assert '"question":"What is the token?"' in prompt
        assert "> /tmp/w/.ctrlrelay/state.json" in prompt

    def test_round_two_asks_for_the_token_without_containing_it(self) -> None:
        """The prompt cannot carry the answer it is checking for."""
        from ctrlrelay.core.agent_check import resume_round2_prompt

        prompt = resume_round2_prompt(
            "/tmp/w/.ctrlrelay/state.json", "check-fake-abcd",
        )

        # The phrase as the prompt wraps it: asserting across the line
        # break would fail on a reflow that changed nothing.
        assert "summary is the token you were asked to" in prompt
        assert '"status": "DONE"' in prompt
        assert '"summary": "<TOKEN>"' in prompt
        assert "python3 - /tmp/w/.ctrlrelay/state.json <<'CTRLRELAY_DONE'" in prompt

    def test_both_prompts_quote_a_path_with_an_apostrophe(self) -> None:
        """#191 again: the resume rounds build their own snippets, so the
        quoting that fixed it has to be in each of them."""
        from ctrlrelay.core.agent_check import (
            resume_round1_prompt,
            resume_round2_prompt,
        )

        path = "/tmp/a'b'c/.ctrlrelay/state.json"
        quoted = "'/tmp/a'\"'\"'b'\"'\"'c/.ctrlrelay/state.json'"

        assert quoted in resume_round1_prompt(path, "s1", "1a2b3c4d")
        assert quoted in resume_round2_prompt(path, "s1")
        assert "/tmp/abc" not in resume_round1_prompt(path, "s1", "1a2b3c4d")
        assert "/tmp/abc" not in resume_round2_prompt(path, "s1")


class TestTheResumeCliCommand:
    def test_it_exits_zero_and_prints_both_rounds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _memory(tmp_path, monkeypatch)
        result = TestTheCliCommand()._invoke(
            tmp_path, ["fake", "--resume"],
            {"fake": _resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN))},
        )

        assert result.exit_code == 0, result.output
        assert "round 1 (fresh):" in result.output
        assert "round 2 (resume):" in result.output
        assert "checkpoint BLOCKED_NEEDS_INPUT" in result.output
        assert "checkpoint DONE" in result.output
        assert "fake resumed" in result.output

    def test_a_round_two_that_forgot_exits_non_zero_saying_why(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _memory(tmp_path, monkeypatch)
        monkeypatch.setenv("FAKE_AGENT_SUMMARY", "no idea")
        result = TestTheCliCommand()._invoke(
            tmp_path, ["fake", "--resume"],
            {"fake": _resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN))},
        )

        assert result.exit_code == 1, result.output
        assert "round 2 (resume):" in result.output
        assert "token" in result.output

    def test_no_resume_configured_exits_non_zero_with_the_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory = _memory(tmp_path, monkeypatch)
        result = TestTheCliCommand()._invoke(
            tmp_path, ["fake", "--resume"],
            {"fake": _plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN))},
        )

        assert result.exit_code == 1, result.output
        assert "resume not configured for fake" in result.output
        assert _calls(memory) == 0

    def test_json_prints_one_object_with_both_rounds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _memory(tmp_path, monkeypatch)
        result = TestTheCliCommand()._invoke(
            tmp_path, ["fake", "--resume", "--json"],
            {"fake": _resumable_plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN))},
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        assert payload["resume"] is True
        assert payload["round1"]["checkpoint"] == "BLOCKED_NEEDS_INPUT"
        assert payload["round2"]["checkpoint"] == "DONE"

    def test_json_reports_no_resume_configured_as_an_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _memory(tmp_path, monkeypatch)
        result = TestTheCliCommand()._invoke(
            tmp_path, ["fake", "--resume", "--json"],
            {"fake": _plugin(_script(tmp_path, _REMEMBERS_THE_TOKEN))},
        )

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is False
        assert "resume not configured for fake" in payload["error"]

    def test_without_the_flag_one_round_runs_and_resume_is_not_claimed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The single-round check is unchanged by this option existing."""
        _memory(tmp_path, monkeypatch)
        result = TestTheCliCommand()._invoke(
            tmp_path, ["fake", "--json"],
            {"fake": _resumable_plugin(_script(tmp_path, _WRITES_DONE))},
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["checkpoint"] == "DONE"
        assert "round1" not in payload
        assert "resume" not in payload


class TestResumeIsByIdNotByRecency:
    def test_a_backend_that_resumes_the_latest_session_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A manifest built on `--continue` or `resume --last` carries
        context, just not the context it was asked for. On a node where
        runs interleave it resumes somebody else's session, so a check that
        only ever ran one session would pass it."""
        memory = _memory(tmp_path, monkeypatch)
        monkeypatch.setenv("FAKE_AGENT_RESUME_LAST", "1")
        script = _script(tmp_path, _REMEMBERS_THE_TOKEN)

        report = _run_resume(
            _config(tmp_path, fake=_resumable_plugin(script)), "fake",
        )

        assert not report.ok
        assert any("most recent" in f for f in report.failures), report.failures
        assert (memory / "decoy-prompt").exists()

    def test_the_decoy_is_a_separate_fresh_session_with_its_own_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory = _memory(tmp_path, monkeypatch)
        script = _script(tmp_path, _REMEMBERS_THE_TOKEN)

        report = _run_resume(
            _config(tmp_path, fake=_resumable_plugin(script)), "fake",
        )

        assert report.ok, report.failures
        decoy = (memory / "decoy-prompt").read_text()
        assert report.decoy_token and report.decoy_token != report.token
        assert report.decoy_token in decoy
        assert report.token not in decoy
        assert report.token not in (memory / "round2-prompt").read_text()
        assert report.decoy_token not in (memory / "round2-prompt").read_text()
