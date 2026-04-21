"""Tests for Claude dispatcher."""

import json
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


import asyncio  # noqa: E402 — needed for TimeoutError reference in test body
