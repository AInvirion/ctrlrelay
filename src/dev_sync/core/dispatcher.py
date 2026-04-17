"""Claude subprocess dispatcher for dev-sync."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dev_sync.core.checkpoint import CheckpointState, CheckpointStatus, read_checkpoint


@dataclass
class SessionResult:
    """Result of a Claude session."""

    session_id: str
    exit_code: int
    state: CheckpointState | None
    stdout: str = ""
    stderr: str = ""

    @property
    def success(self) -> bool:
        return self.state is not None and self.state.status == CheckpointStatus.DONE

    @property
    def blocked(self) -> bool:
        return (
            self.state is not None
            and self.state.status == CheckpointStatus.BLOCKED_NEEDS_INPUT
        )

    @property
    def failed(self) -> bool:
        return self.state is None or self.state.status == CheckpointStatus.FAILED


@dataclass
class ClaudeDispatcher:
    """Spawns and manages Claude subprocess sessions."""

    claude_binary: str = "claude"
    default_timeout: int = 1800
    extra_env: dict[str, str] = field(default_factory=dict)

    async def spawn_session(
        self,
        session_id: str,
        prompt: str,
        working_dir: Path,
        state_file: Path,
        timeout: int | None = None,
        resume_session_id: str | None = None,
    ) -> SessionResult:
        """Spawn a Claude session and wait for completion."""
        timeout = timeout or self.default_timeout

        env = os.environ.copy()
        env.update(self.extra_env)
        env["DEV_SYNC_SESSION_ID"] = session_id
        env["DEV_SYNC_STATE_FILE"] = str(state_file)

        cmd = [self.claude_binary, "-p", prompt, "--output-format", "json"]
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=working_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SessionResult(
                session_id=session_id,
                exit_code=-1,
                state=None,
                stderr="Session timed out",
            )

        state = None
        if state_file.exists():
            try:
                state = read_checkpoint(state_file, delete_after=True)
            except Exception:
                pass

        return SessionResult(
            session_id=session_id,
            exit_code=proc.returncode or 0,
            state=state,
            stdout=stdout.decode(),
            stderr=stderr.decode(),
        )
