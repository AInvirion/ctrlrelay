"""Headless coding-agent subprocess dispatcher for ctrlrelay.

Today the only concrete adapter is :class:`ClaudeDispatcher` (wraps
``claude -p``). The :func:`make_agent_dispatcher` factory is the seam
where additional backends (Codex, OpenCode, Hermes, Kiro, …) will plug
in: each adapter must expose the same async ``spawn_session`` shape
so pipelines can stay agent-agnostic.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus, read_checkpoint
from ctrlrelay.core.obs import get_logger, hash_text, log_event

if TYPE_CHECKING:
    from ctrlrelay.core.config import AgentConfig

_logger = get_logger("core.dispatcher")

# Enough of the child's stderr to name the failure without dumping agent
# output into the log stream.
_STDERR_TAIL_CHARS = 500


def _find_claude() -> str:
    """Find claude binary, checking common paths if not in PATH.

    Both fallback branches log at WARNING: under systemd/launchd the unit's
    PATH is minimal, so ``shutil.which`` missing is the usual precursor to
    every session failing to spawn, and it is worth seeing in the log
    before the failures start.
    """
    claude = shutil.which("claude")
    if claude:
        return claude
    for path in [
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            _logger.warning(
                "dispatcher.binary.fallback",
                extra={"binary": path, "reason": "not_on_path"},
            )
            return path
    _logger.warning(
        "dispatcher.binary.unresolved",
        extra={"binary": "claude", "reason": "not_on_path"},
    )
    return "claude"


@dataclass
class SessionResult:
    """Result of a Claude session.

    - ``session_id`` is the orchestrator's composite id (``dev-<owner>-<repo>-
      <issue>-<hex>``) used as the primary key in state_db.
    - ``agent_session_id`` is Claude's own session UUID, parsed from the JSON
      stdout. It's the value that must be passed to ``claude --resume`` on
      subsequent calls; newer CLI versions reject non-UUID strings.
      ``None`` when stdout wasn't parseable JSON or didn't include the field.
    """

    session_id: str
    exit_code: int
    state: CheckpointState | None
    stdout: str = ""
    stderr: str = ""
    agent_session_id: str | None = None

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
        repo: str | None = None,
        issue_number: int | None = None,
    ) -> SessionResult:
        """Spawn a Claude session and wait for completion.

        ``repo``/``issue_number`` are observability-only: they let the
        dispatcher's events be correlated with the pipeline's without
        re-parsing the composite session id.
        """
        timeout = timeout or self.default_timeout

        # Identity fields repeated on every event of this session, so a
        # single grep on session_id reconstructs the whole run.
        ident: dict[str, Any] = {
            "session_id": session_id,
            "repo": repo,
            "issue_number": issue_number,
        }

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

        # The prompt can carry issue bodies and operator answers, so only its
        # hash and length are ever logged (#41).
        log_event(
            _logger,
            "dispatcher.session.start",
            **ident,
            resume=resume_session_id is not None,
            binary=self.claude_binary,
            working_dir=str(working_dir),
            timeout=timeout,
            prompt_hash=hash_text(prompt),
            prompt_len=len(prompt),
        )
        started = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=working_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            # Usually the binary isn't where we resolved it — the exception
            # propagates to the pipeline, but without this it lands there
            # with no record of which path we tried.
            _logger.error(
                "dispatcher.session.spawn_failed",
                extra={
                    **ident,
                    "binary": self.claude_binary,
                    "error_type": type(e).__name__,
                    "error": str(e)[:200],
                },
            )
            raise

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as e:
            proc.kill()
            await proc.wait()
            _logger.error(
                "dispatcher.session.timeout",
                extra={
                    **ident,
                    "error_type": type(e).__name__,
                    "timeout": timeout,
                    "elapsed_ms": _elapsed_ms(started),
                },
            )
            return SessionResult(
                session_id=session_id,
                exit_code=-1,
                state=None,
                stderr="Session timed out",
            )
        except asyncio.CancelledError as e:
            # Scheduler/shutdown cancel: kill the child BEFORE re-raising
            # so `claude` isn't left running against the worktree after
            # the daemon exits. Shield the wait so further cancels don't
            # orphan the process between `kill()` and the reaping.
            log_event(
                _logger,
                "dispatcher.session.cancelled",
                **ident,
                error_type=type(e).__name__,
                elapsed_ms=_elapsed_ms(started),
            )
            if proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.shield(proc.wait())
                except asyncio.CancelledError:
                    pass
            raise

        elapsed_ms = _elapsed_ms(started)
        exit_code = proc.returncode or 0
        stdout_text = stdout.decode()
        stderr_text = stderr.decode()

        state = None
        if state_file.exists():
            try:
                state = read_checkpoint(state_file, delete_after=True)
            except Exception as e:
                _logger.error(
                    "dispatcher.checkpoint.read_failed",
                    extra={
                        **ident,
                        "state_file": str(state_file),
                        "error_type": type(e).__name__,
                        "error": str(e)[:200],
                    },
                )
        else:
            # The session ended without signalling: the pipeline will report
            # it as failed, and this is the only record of why.
            _logger.warning(
                "dispatcher.checkpoint.missing",
                extra={
                    **ident,
                    "state_file": str(state_file),
                    "exit_code": exit_code,
                },
            )

        if exit_code != 0:
            _logger.error(
                "dispatcher.session.subprocess_failed",
                extra={
                    **ident,
                    "exit_code": exit_code,
                    "stderr_tail": stderr_text[-_STDERR_TAIL_CHARS:],
                    "elapsed_ms": elapsed_ms,
                },
            )

        agent_session_id = _extract_agent_session_id(stdout_text)

        log_event(
            _logger,
            "dispatcher.session.finished",
            **ident,
            exit_code=exit_code,
            checkpoint_status=state.status.value if state else None,
            elapsed_ms=elapsed_ms,
            agent_session_id=agent_session_id,
            stdout_hash=hash_text(stdout_text),
            stdout_len=len(stdout_text),
        )

        return SessionResult(
            session_id=session_id,
            exit_code=exit_code,
            state=state,
            stdout=stdout_text,
            stderr=stderr_text,
            agent_session_id=agent_session_id,
        )


def _elapsed_ms(started: float) -> int:
    """Milliseconds since ``started`` (a :func:`time.monotonic` reading)."""
    return int((time.monotonic() - started) * 1000)


def _extract_agent_session_id(stdout_text: str) -> str | None:
    """Pull Claude's session UUID out of ``claude -p --output-format json``.

    Claude emits a single JSON object on stdout whose ``session_id`` field is
    the UUID required by ``--resume``. If the output is empty or not JSON
    (errors, interrupted runs) we return ``None`` and let the caller decide
    how to fall back.
    """
    if not stdout_text.strip():
        return None
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("session_id")
    return value if isinstance(value, str) and value else None


class AgentAdapter(Protocol):
    """Protocol every coding-agent adapter must satisfy.

    An adapter is a thin wrapper over an agent CLI that handles:
    spawning the subprocess, passing the prompt and state-file path,
    enforcing the timeout, and translating the checkpoint JSON the
    agent writes into a :class:`SessionResult`.

    Pipelines (dev, secops) only ever interact with this protocol —
    they never import concrete adapters — so adding a new backend
    means implementing this and registering it in
    :func:`make_agent_dispatcher`.
    """

    async def spawn_session(
        self,
        session_id: str,
        prompt: str,
        working_dir: Path,
        state_file: Path,
        timeout: int | None = None,
        resume_session_id: str | None = None,
        repo: str | None = None,
        issue_number: int | None = None,
    ) -> SessionResult:
        ...


def make_agent_dispatcher(agent_config: "AgentConfig") -> AgentAdapter:
    """Build an adapter for the configured ``agent.type``.

    Raises :class:`NotImplementedError` with a clear error message if
    the configured type has no adapter — makes a typo surface loudly
    at daemon startup instead of silently falling back to Claude.
    """
    t = agent_config.type
    if t == "claude":
        return ClaudeDispatcher(
            claude_binary=agent_config.binary,
            default_timeout=agent_config.default_timeout_seconds,
        )
    raise NotImplementedError(
        f"agent.type={t!r} has no adapter yet. Implement one that "
        "satisfies the AgentAdapter protocol in "
        "src/ctrlrelay/core/dispatcher.py and register it here, then "
        "open a PR. Supported today: 'claude'."
    )
