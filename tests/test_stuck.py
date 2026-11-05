"""Stuck-run detection (#224): the transcript loop and the CI sorting."""

from __future__ import annotations

import asyncio
import calendar
import json
import os
import time
from pathlib import Path

import pytest

from ctrlrelay.core.stuck import (
    FailureKind,
    StuckPolicy,
    classify_failure,
    detect_loop,
    failed_tests,
    watch_transcript,
)

TS = "2026-11-04T10:00:00Z"


class _T:
    """Builds transcript rows the way the agent CLI writes them."""

    def __init__(self, ts: str = TS) -> None:
        self.rows: list[str] = []
        self.n = 0
        self.ts = ts

    def call(self, name: str, tool_input: dict, *, error: str | None = None,
             ok: bool = True) -> _T:
        self.n += 1
        tid = f"toolu_{self.n}"
        self.rows.append(json.dumps({
            "type": "assistant", "timestamp": self.ts,
            "message": {"content": [
                {"type": "tool_use", "id": tid, "name": name, "input": tool_input},
            ]},
        }))
        result: dict = {"type": "tool_result", "tool_use_id": tid,
                        "content": error if error is not None else "ok"}
        if error is not None:
            result["is_error"] = True
        if error is not None or ok:
            self.rows.append(json.dumps({
                "type": "user", "timestamp": self.ts,
                "message": {"content": [result]},
            }))
        return self

    def bash(self, command: str, **kw) -> _T:
        return self.call("Bash", {"command": command}, **kw)

    def edit(self, path: str = "src/a.py", new: str = "x") -> _T:
        return self.call("Edit", {"file_path": path, "new_string": new})


FAIL = "Exit code 1\nFAILED tests/test_a.py::test_one - assert 1 == 2\n1 failed in {t}s"


class TestSameCommandInARow:
    def test_five_identical_calls_is_stuck(self):
        t = _T()
        for _ in range(5):
            t.bash("pytest tests/test_a.py")
        reason = detect_loop(t.rows, limit=5)
        assert reason is not None
        assert "same command 5 times in a row" in reason
        assert "pytest" in reason

    def test_four_identical_calls_is_not(self):
        t = _T()
        for _ in range(4):
            t.bash("pytest tests/test_a.py")
        assert detect_loop(t.rows, limit=5) is None

    def test_a_different_call_breaks_the_run(self):
        t = _T()
        for i in range(8):
            t.bash("pytest tests/test_a.py") if i != 5 else t.bash("git status")
        assert detect_loop(t.rows, limit=5) is None

    def test_the_command_text_is_not_quoted_as_typed(self):
        t = _T()
        for _ in range(5):
            t.bash("curl -H 'Authorization: token sekrit123' https://x")
        reason = detect_loop(t.rows, limit=5)
        assert reason is not None
        assert "sekrit123" not in reason


class TestSameErrorInARow:
    def test_edit_rerun_same_error_five_times_is_stuck(self):
        t = _T()
        for i in range(5):
            t.edit(new=f"attempt {i}")
            t.bash("pytest", error=FAIL.format(t=f"0.{i}2"))
        reason = detect_loop(t.rows, limit=5)
        assert reason is not None
        assert "same error 5 times in a row" in reason

    def test_a_changing_error_is_progress(self):
        t = _T()
        for i in range(5):
            t.edit(new=f"attempt {i}")
            t.bash("pytest", error=f"Exit code 1\nFAILED tests/test_a.py::test_{i}")
        assert detect_loop(t.rows, limit=5) is None

    def test_a_success_in_between_resets_it(self):
        t = _T()
        for i in range(6):
            t.edit(new=f"attempt {i}")
            if i == 3:
                t.bash("pytest")
            else:
                t.bash("pytest", error=FAIL.format(t="1.0"))
        assert detect_loop(t.rows, limit=5) is None

    def test_rows_before_the_session_started_are_ignored(self):
        old = _T("2026-11-04T09:00:00Z")
        for i in range(5):
            old.edit(new=f"attempt {i}")
            old.bash("pytest", error=FAIL.format(t="1.0"))
        assert detect_loop(old.rows, limit=5) is not None
        since = calendar.timegm(time.strptime("2026-11-04T09:30:00", "%Y-%m-%dT%H:%M:%S"))
        assert detect_loop(old.rows, since=since, limit=5) is None


class TestWatch:
    def test_returns_the_reason_once_the_transcript_loops(self, tmp_path: Path):
        folder = tmp_path / "proj"
        folder.mkdir()
        since = time.time() - 5
        t = _T(ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        for _ in range(5):
            t.bash("pytest")
        (folder / "s.jsonl").write_text("\n".join(t.rows) + "\n")

        reason = asyncio.run(asyncio.wait_for(
            watch_transcript([folder], since=since,
                             policy=StuckPolicy(repeat_limit=5, poll_seconds=0.01)),
            timeout=5,
        ))
        assert "same command" in reason

    def test_an_old_transcript_is_not_read(self, tmp_path: Path):
        folder = tmp_path / "proj"
        folder.mkdir()
        t = _T()
        for _ in range(5):
            t.bash("pytest")
        f = folder / "old.jsonl"
        f.write_text("\n".join(t.rows) + "\n")
        os.utime(f, (time.time() - 3600, time.time() - 3600))

        async def run():
            return await asyncio.wait_for(
                watch_transcript([folder], since=time.time() - 60,
                                 policy=StuckPolicy(poll_seconds=0.01)),
                timeout=0.2,
            )

        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(run())


class TestClassify:
    def test_pytest_failure_is_code(self):
        log = "build\ttest\t2026-11-04T10:00:00Z FAILED tests/test_a.py::test_one - assert"
        assert classify_failure(log) is FailureKind.CODE
        assert failed_tests(log) == {"tests/test_a.py::test_one"}

    def test_runner_lost_is_infra(self):
        log = (
            "The runner has received a shutdown signal. This can happen when "
            "the runner service is stopped"
        )
        assert classify_failure(log) is FailureKind.INFRA

    def test_network_flake_is_infra(self):
        assert classify_failure(
            "fatal: unable to access 'https://github.com/x/y/': Could not resolve host"
        ) is FailureKind.INFRA

    def test_job_timeout_is_budget(self):
        log = (
            "##[error]The job running on runner GitHub Actions 2 has exceeded "
            "the maximum execution time of 360 minutes."
        )
        assert classify_failure(log) is FailureKind.BUDGET

    def test_failing_tests_win_over_network_words(self):
        log = (
            "E   ConnectionError: connection reset by peer\n"
            "FAILED tests/test_net.py::test_fetch - ConnectionError"
        )
        assert classify_failure(log) is FailureKind.CODE

    def test_unknown_is_code_never_a_blind_rerun(self):
        assert classify_failure("Error: Process completed with exit code 2.") is (
            FailureKind.CODE
        )

    def test_go_and_cargo_names(self):
        assert failed_tests("--- FAIL: TestThing (0.00s)") == {"TestThing"}
        assert failed_tests("test a::b ... FAILED") == {"a::b"}


class TestDispatcherStopsAStuckAgent:
    """Through the real spawn path: a child that would run for a minute,
    a transcript that loops, and the watch that must stop it."""

    def _setup(self, tmp_path: Path):
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        binary = tmp_path / "fake-claude"
        binary.write_text("#!/bin/sh\nexec sleep 60\n")
        binary.chmod(0o755)
        config = tmp_path / "config"
        worktree = tmp_path / "wt"
        worktree.mkdir()
        dispatcher = ClaudeDispatcher(
            usage_store=None, claude_binary=str(binary), default_timeout=30,
            extra_env={"CLAUDE_CONFIG_DIR": str(config)},
        )
        dispatcher.stuck_policy = StuckPolicy(repeat_limit=5, poll_seconds=0.05)
        folder = dispatcher._transcript_dirs(
            worktree, {"CLAUDE_CONFIG_DIR": str(config)}
        )[0]
        folder.mkdir(parents=True)
        return dispatcher, worktree, folder

    def _now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1))

    @pytest.mark.asyncio
    async def test_a_looping_agent_is_stopped_with_the_reason(self, tmp_path: Path):
        dispatcher, worktree, folder = self._setup(tmp_path)
        t = _T(ts=self._now())
        for _ in range(5):
            t.bash("pytest")

        async def write_later():
            await asyncio.sleep(0.2)
            (folder / "s.jsonl").write_text("\n".join(t.rows) + "\n")

        writer = asyncio.create_task(write_later())
        started = time.monotonic()
        result = await dispatcher.spawn_session(
            github_access=False, session_id="dev-x-1-abc", prompt="p",
            working_dir=worktree, state_file=worktree / "state.json",
        )
        await writer
        assert time.monotonic() - started < 15
        assert result.stuck_reason is not None
        assert "same command 5 times" in result.stuck_reason
        assert "stuck" in result.stderr
        assert not result.timed_out

    @pytest.mark.asyncio
    async def test_a_progressing_agent_is_left_alone(self, tmp_path: Path):
        dispatcher, worktree, folder = self._setup(tmp_path)
        dispatcher.default_timeout = 1
        t = _T(ts=self._now())
        for i in range(5):
            t.bash(f"pytest tests/test_{i}.py")
        (folder / "s.jsonl").write_text("\n".join(t.rows) + "\n")

        result = await dispatcher.spawn_session(
            github_access=False, session_id="dev-x-1-abc", prompt="p",
            working_dir=worktree, state_file=worktree / "state.json",
        )
        assert result.timed_out
        assert result.stuck_reason is None
