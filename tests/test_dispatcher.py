"""Tests for Claude dispatcher."""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestClaudeDispatcher:
    @pytest.mark.asyncio
    async def test_spawn_session_sets_env_vars(self, tmp_path: Path) -> None:
        """Should set CTRLRELAY env vars for checkpoint protocol."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            state_file = tmp_path / "state.json"
            state_file.write_text(json.dumps({
                "version": "1",
                "status": "DONE",
                "session_id": "test-123",
                "timestamp": "2026-04-17T12:00:00Z",
                "summary": "Test completed",
            }))

            await dispatcher.spawn_session(
                session_id="test-123",
                prompt="Test prompt",
                working_dir=tmp_path,
                state_file=state_file,
            )

            call_kwargs = mock_exec.call_args.kwargs
            env = call_kwargs.get("env", {})
            assert "CTRLRELAY_SESSION_ID" in env
            assert "CTRLRELAY_STATE_FILE" in env

    @pytest.mark.asyncio
    async def test_spawn_session_handles_timeout(self, tmp_path: Path) -> None:
        """Should kill process on timeout."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude", default_timeout=1)

        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await dispatcher.spawn_session(
                session_id="test-123",
                prompt="Test",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                timeout=1,
            )

            assert result.exit_code == -1
            assert "timed out" in result.stderr
            mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_session_kills_child_on_cancel(
        self, tmp_path: Path
    ) -> None:
        """Regression for codex round-3 [P1]: a CancelledError during
        `proc.communicate()` (scheduler shutdown / SIGTERM during a
        scheduled secops run) must kill the child process before
        re-raising, so `claude` isn't left running against the worktree
        after the daemon exits."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude", default_timeout=60)

        mock_proc = AsyncMock()
        mock_proc.returncode = None  # still running
        mock_proc.communicate.side_effect = asyncio.CancelledError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(asyncio.CancelledError):
                await dispatcher.spawn_session(
                    session_id="test-cancel",
                    prompt="Test",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    timeout=60,
                )

        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited()

    @pytest.mark.asyncio
    async def test_spawn_session_parses_done_state(self, tmp_path: Path) -> None:
        """Should parse DONE checkpoint state."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b'{"result": "ok"}', b"")
        mock_proc.returncode = 0

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "version": "1",
            "status": "DONE",
            "session_id": "test-123",
            "timestamp": "2026-04-17T12:00:00Z",
            "summary": "Merged 3 PRs",
            "outputs": {"merged_prs": [1, 2, 3]},
        }))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await dispatcher.spawn_session(
                session_id="test-123",
                prompt="Test",
                working_dir=tmp_path,
                state_file=state_file,
            )

            assert result.success
            assert result.state is not None
            assert result.state.summary == "Merged 3 PRs"
            assert result.state.outputs["merged_prs"] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_spawn_session_captures_agent_session_id_from_json(
        self, tmp_path: Path
    ) -> None:
        """Should parse Claude's session_id UUID out of JSON stdout and attach
        it to the returned SessionResult as agent_session_id."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        agent_uuid = "b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10"
        payload = json.dumps({
            "type": "result",
            "session_id": agent_uuid,
            "result": "ok",
        }).encode()

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (payload, b"")
        mock_proc.returncode = 0

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "version": "1",
            "status": "DONE",
            "session_id": "dev-o-r-1-abc",
            "timestamp": "2026-04-20T00:00:00Z",
            "summary": "ok",
        }))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await dispatcher.spawn_session(
                session_id="dev-o-r-1-abc",
                prompt="x",
                working_dir=tmp_path,
                state_file=state_file,
            )

        assert result.agent_session_id == agent_uuid
        # Composite id still lives on .session_id for orchestrator bookkeeping.
        assert result.session_id == "dev-o-r-1-abc"

    @pytest.mark.asyncio
    async def test_spawn_session_agent_session_id_none_when_stdout_not_json(
        self, tmp_path: Path
    ) -> None:
        """If Claude didn't emit JSON (e.g. error output), agent_session_id is None."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"not json at all", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await dispatcher.spawn_session(
                session_id="sid",
                prompt="x",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
            )

        assert result.agent_session_id is None

    @pytest.mark.asyncio
    async def test_spawn_session_passes_resume_session_id_verbatim(
        self, tmp_path: Path
    ) -> None:
        """--resume must receive whatever resume_session_id we pass — the
        dispatcher does not substitute our composite id."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        agent_uuid = "b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10"

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"{}", b"")
        mock_proc.returncode = 0

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            await dispatcher.spawn_session(
                session_id="dev-o-r-1-abc",
                prompt="x",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
                resume_session_id=agent_uuid,
            )

        argv = mock_exec.call_args.args
        assert "--resume" in argv
        assert argv[argv.index("--resume") + 1] == agent_uuid
        # Never our composite id.
        assert "dev-o-r-1-abc" not in argv

    @pytest.mark.asyncio
    async def test_spawn_session_no_resume_flag_when_none(
        self, tmp_path: Path
    ) -> None:
        """Fresh spawns (resume_session_id=None) must not include --resume."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"{}", b"")
        mock_proc.returncode = 0

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            await dispatcher.spawn_session(
                session_id="dev-o-r-1-abc",
                prompt="x",
                working_dir=tmp_path,
                state_file=tmp_path / "state.json",
            )

        argv = mock_exec.call_args.args
        assert "--resume" not in argv

    @pytest.mark.asyncio
    async def test_spawn_session_parses_blocked_state(self, tmp_path: Path) -> None:
        """Should parse BLOCKED_NEEDS_INPUT checkpoint state."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "version": "1",
            "status": "BLOCKED_NEEDS_INPUT",
            "session_id": "test-123",
            "timestamp": "2026-04-17T12:00:00Z",
            "question": "Pin to 2.4.1 or bump to 2.5.0?",
            "question_context": {"pr": 42},
        }))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await dispatcher.spawn_session(
                session_id="test-123",
                prompt="Test",
                working_dir=tmp_path,
                state_file=state_file,
            )

            assert result.blocked
            assert result.state is not None
            assert "2.4.1" in result.state.question


class TestDispatcherLogging:
    """#156: the dispatcher spawns and supervises every agent session, so it
    must leave a trail. Prompts and agent output stay out of the logs — hash
    and length only."""

    @staticmethod
    def _events(caplog) -> list:
        return [r for r in caplog.records if r.name == "ctrlrelay.core.dispatcher"]

    @classmethod
    def _find(cls, caplog, event: str):
        matches = [r for r in cls._events(caplog) if r.getMessage() == event]
        assert matches, (
            f"expected {event}, got: "
            f"{[r.getMessage() for r in cls._events(caplog)]}"
        )
        return matches[0]

    @pytest.mark.asyncio
    async def test_session_start_logs_identity_and_resume_mode(
        self, tmp_path: Path, caplog
    ) -> None:
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="/usr/bin/claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"{}", b"")
        mock_proc.returncode = 0

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await dispatcher.spawn_session(
                    session_id="dev-o-r-9-abc",
                    prompt="secret prompt text",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    timeout=42,
                    repo="o/r",
                    issue_number=9,
                )

        rec = self._find(caplog, "dispatcher.session.start").__dict__
        assert rec["session_id"] == "dev-o-r-9-abc"
        assert rec["repo"] == "o/r"
        assert rec["issue_number"] == 9
        assert rec["resume"] is False
        assert rec["binary"] == "/usr/bin/claude"
        assert rec["timeout"] == 42

    @pytest.mark.asyncio
    async def test_session_start_marks_resume(self, tmp_path: Path, caplog) -> None:
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"{}", b"")
        mock_proc.returncode = 0

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await dispatcher.spawn_session(
                    session_id="dev-o-r-9-abc",
                    prompt="x",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    resume_session_id="b6a0e6f8-8e9b-4e4f-9a33-5a2e1f7c8a10",
                )

        assert self._find(caplog, "dispatcher.session.start").__dict__["resume"] is True

    @pytest.mark.asyncio
    async def test_prompt_and_output_are_never_logged_in_plaintext(
        self, tmp_path: Path, caplog
    ) -> None:
        """#41: no prompt or agent output in the log stream — hash/length only."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher
        from ctrlrelay.core.obs import hash_text

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        prompt = "SUPER-SECRET-PROMPT-NEEDLE"
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b'{"result": "AGENT-OUTPUT-NEEDLE"}', b"")
        mock_proc.returncode = 0

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "version": "1",
            "status": "DONE",
            "session_id": "sid",
            "timestamp": "2026-04-17T12:00:00Z",
            "summary": "ok",
        }))

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await dispatcher.spawn_session(
                    session_id="sid",
                    prompt=prompt,
                    working_dir=tmp_path,
                    state_file=state_file,
                )

        blob = json.dumps(
            [
                {k: str(v) for k, v in r.__dict__.items() if not k.startswith("_")}
                for r in self._events(caplog)
            ]
        )
        assert "SUPER-SECRET-PROMPT-NEEDLE" not in blob
        assert "AGENT-OUTPUT-NEEDLE" not in blob

        start = self._find(caplog, "dispatcher.session.start").__dict__
        assert start["prompt_hash"] == hash_text(prompt)
        assert start["prompt_len"] == len(prompt)

    @pytest.mark.asyncio
    async def test_checkpoint_status_is_logged_on_finish(
        self, tmp_path: Path, caplog
    ) -> None:
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"{}", b"")
        mock_proc.returncode = 0

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "version": "1",
            "status": "BLOCKED_NEEDS_INPUT",
            "session_id": "sid",
            "timestamp": "2026-04-17T12:00:00Z",
            "question": "Pin or bump?",
        }))

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await dispatcher.spawn_session(
                    session_id="sid",
                    prompt="x",
                    working_dir=tmp_path,
                    state_file=state_file,
                )

        rec = self._find(caplog, "dispatcher.session.finished").__dict__
        assert rec["checkpoint_status"] == "BLOCKED_NEEDS_INPUT"
        assert rec["exit_code"] == 0
        assert "elapsed_ms" in rec
        # The question text itself must not ride along.
        assert "Pin or bump?" not in json.dumps(
            {k: str(v) for k, v in rec.items() if not k.startswith("_")}
        )

    @pytest.mark.asyncio
    async def test_missing_checkpoint_is_logged(self, tmp_path: Path, caplog) -> None:
        """A session that ends without writing a checkpoint is the exact case
        that used to vanish silently."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"{}", b"")
        mock_proc.returncode = 0

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await dispatcher.spawn_session(
                    session_id="sid",
                    prompt="x",
                    working_dir=tmp_path,
                    state_file=tmp_path / "missing.json",
                )

        rec = self._find(caplog, "dispatcher.checkpoint.missing").__dict__
        assert rec["session_id"] == "sid"
        assert rec["levelname"] == "WARNING"

    @pytest.mark.asyncio
    async def test_unreadable_checkpoint_logs_exception_type(
        self, tmp_path: Path, caplog
    ) -> None:
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"{}", b"")
        mock_proc.returncode = 0

        state_file = tmp_path / "state.json"
        state_file.write_text("{ not json")

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await dispatcher.spawn_session(
                    session_id="sid",
                    prompt="x",
                    working_dir=tmp_path,
                    state_file=state_file,
                )

        assert result.state is None
        rec = self._find(caplog, "dispatcher.checkpoint.read_failed").__dict__
        assert rec["error_type"]
        assert rec["levelname"] == "ERROR"

    @pytest.mark.asyncio
    async def test_nonzero_exit_logs_error_with_stderr_tail(
        self, tmp_path: Path, caplog
    ) -> None:
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"boom: could not start\n")
        mock_proc.returncode = 2

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await dispatcher.spawn_session(
                    session_id="sid",
                    prompt="x",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                )

        rec = self._find(caplog, "dispatcher.session.subprocess_failed").__dict__
        assert rec["exit_code"] == 2
        assert "boom: could not start" in rec["stderr_tail"]
        assert rec["levelname"] == "ERROR"

    @pytest.mark.asyncio
    async def test_timeout_logs_event_with_exception_type_and_timeout(
        self, tmp_path: Path, caplog
    ) -> None:
        """Acceptance for #156: the failure path emits an event carrying the
        exception type, plus the timeout value that was applied."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude", default_timeout=1)

        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await dispatcher.spawn_session(
                    session_id="sid",
                    prompt="x",
                    working_dir=tmp_path,
                    state_file=tmp_path / "state.json",
                    timeout=7,
                )

        rec = self._find(caplog, "dispatcher.session.timeout").__dict__
        assert rec["error_type"] == "TimeoutError"
        assert rec["timeout"] == 7
        assert rec["levelname"] == "ERROR"

    @pytest.mark.asyncio
    async def test_cancel_logs_event_with_exception_type(
        self, tmp_path: Path, caplog
    ) -> None:
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.communicate.side_effect = asyncio.CancelledError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                with pytest.raises(asyncio.CancelledError):
                    await dispatcher.spawn_session(
                        session_id="sid",
                        prompt="x",
                        working_dir=tmp_path,
                        state_file=tmp_path / "state.json",
                    )

        rec = self._find(caplog, "dispatcher.session.cancelled").__dict__
        assert rec["error_type"] == "CancelledError"

    @pytest.mark.asyncio
    async def test_spawn_failure_logs_exception_type(
        self, tmp_path: Path, caplog
    ) -> None:
        """If the binary can't be exec'd at all (the systemd PATH case), the
        dispatcher must say so instead of letting a bare OSError bubble up
        unrecorded."""
        from ctrlrelay.core.dispatcher import ClaudeDispatcher

        dispatcher = ClaudeDispatcher(claude_binary="claude")

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch(
                "asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError("no claude"),
            ):
                with pytest.raises(FileNotFoundError):
                    await dispatcher.spawn_session(
                        session_id="sid",
                        prompt="x",
                        working_dir=tmp_path,
                        state_file=tmp_path / "state.json",
                    )

        rec = self._find(caplog, "dispatcher.session.spawn_failed").__dict__
        assert rec["error_type"] == "FileNotFoundError"
        assert rec["levelname"] == "ERROR"

    def test_binary_fallback_warns_when_which_misses(self, caplog) -> None:
        """Under systemd PATH is minimal; falling back to a hard-coded path is
        worth a WARNING because it is where sessions silently stop starting."""
        from ctrlrelay.core.dispatcher import _find_claude

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("shutil.which", return_value=None):
                with patch("os.path.isfile", return_value=True):
                    with patch("os.access", return_value=True):
                        resolved = _find_claude()

        assert resolved.endswith("claude")
        rec = self._find(caplog, "dispatcher.binary.fallback").__dict__
        assert rec["levelname"] == "WARNING"
        assert rec["binary"] == resolved

    def test_binary_unresolved_warns(self, caplog) -> None:
        """Nothing on disk either: we still return the bare name, but the log
        has to record that no real path was found."""
        from ctrlrelay.core.dispatcher import _find_claude

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("shutil.which", return_value=None):
                with patch("os.path.isfile", return_value=False):
                    resolved = _find_claude()

        assert resolved == "claude"
        rec = self._find(caplog, "dispatcher.binary.unresolved").__dict__
        assert rec["levelname"] == "WARNING"

    def test_no_warning_when_binary_is_on_path(self, caplog) -> None:
        from ctrlrelay.core.dispatcher import _find_claude

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ctrlrelay"):
            with patch("shutil.which", return_value="/usr/bin/claude"):
                assert _find_claude() == "/usr/bin/claude"

        assert not self._events(caplog)

import asyncio  # noqa: E402 — needed for TimeoutError reference in test body
