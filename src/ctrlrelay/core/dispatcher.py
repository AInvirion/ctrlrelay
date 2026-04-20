"""Claude subprocess dispatcher for ctrlrelay."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus, read_checkpoint


def _find_claude() -> str:
    """Find claude binary, checking common paths if not in PATH."""
    claude = shutil.which("claude")
    if claude:
        return claude
    for path in [
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return "claude"


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

    claude_binary: str = field(default_factory=_find_claude)
    default_timeout: int = 1800
    extra_env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Only auto-resolve the bare default. Absolute paths, relative paths
        # (resolved vs. working_dir by the child), and custom bare names pass
        # through so explicit config is never silently overridden.
        if self.claude_binary == "claude":
            resolved = shutil.which("claude")
            self.claude_binary = resolved or _find_claude()

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
        env["CTRLRELAY_SESSION_ID"] = session_id
        env["CTRLRELAY_STATE_FILE"] = str(state_file)

        cmd = [
            self.claude_binary,
            "-p", prompt,
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]
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
        except asyncio.CancelledError:
            # Scheduler/shutdown cancel: kill the child BEFORE re-raising
            # so `claude` isn't left running against the worktree after
            # the daemon exits. Shield the wait so further cancels don't
            # orphan the process between `kill()` and the reaping.
            if proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.shield(proc.wait())
                except asyncio.CancelledError:
                    pass
            raise

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
