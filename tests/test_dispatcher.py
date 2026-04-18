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
