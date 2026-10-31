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
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ctrlrelay.core.agent_env import (
    DEFAULT_SANDBOX_HIDE_PATHS,
    build_agent_env,
    bwrap_prefix,
    resolve_bwrap,
)
from ctrlrelay.core.binaries import resolve_binary
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

    The search itself lives in :func:`ctrlrelay.core.binaries.resolve_binary`
    now. It was duplicated here and nowhere else, and the review tool —
    which has the same problem for the same reason — had no copy at all
    (#197). Event names are unchanged, so existing log readers and the
    `dispatcher.binary.fallback` line already in the poller log still
    mean what they meant.
    """
    return resolve_binary(
        "claude", logger=_logger, event_prefix="dispatcher.binary"
    )


@dataclass(frozen=True)
class DispatchOutcome:
    """What one spawn did, handed to the outcome hook (#204).

    Reported from the dispatcher rather than from each pipeline on
    purpose. Three lanes spawn workers (dev, task, secops) and resumes
    and fix rounds spawn more, so recording the outcome at the call
    sites means a lane added later is silently exempt — and "silently
    exempt" is precisely how a two-day outage went unreported. Here
    every spawn is covered by construction.

    ``exit_code`` is ``None`` whenever no exit code was obtained: the
    binary could not be exec'd, or the child ran and reading its
    output failed. Either way it is a dispatch failure with no exit
    code rather than a dispatch that succeeded. ``-1`` is the existing
    convention for a timeout.
    """

    session_id: str
    repo: str | None
    issue_number: int | None
    exit_code: int | None
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """True only for a child that ran and exited 0.

        Says nothing about whether the work succeeded — an agent can
        exit 0 having written no checkpoint (#174). The question this
        answers is narrower and is the one the alert needs: can this
        relay still run a worker at all.
        """
        return self.exit_code == 0


# An async callback invoked once per spawn. It must not raise and must
# not block for long; the dispatcher guards it anyway, because a
# notification that breaks a pipeline is worse than a notification
# nobody gets.
DispatchOutcomeHook = Callable[[DispatchOutcome], Awaitable[None]]


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
    # Called once per spawn with a :class:`DispatchOutcome`. ``None``
    # means nothing records dispatch health for this dispatcher, which
    # is right for the one-shot `ctrlrelay dev`/`secops` commands an
    # operator is watching and wrong for the daemon.
    # `make_agent_dispatcher` takes it as a REQUIRED keyword so every
    # construction site has to say which of those it is, rather than
    # inheriting the pre-#204 silence by omission.
    outcome_hook: DispatchOutcomeHook | None = None
    # "none" or "bwrap" (#219). With "bwrap", a spawn WITHOUT GitHub
    # access runs under bubblewrap with ``sandbox_hide_paths`` replaced
    # by empty mounts, so the agent cannot read the SSH key or gh's
    # stored login either. Off by default: it needs bwrap installed and
    # unprivileged user namespaces allowed, which not every host has.
    sandbox: str = "none"
    sandbox_hide_paths: tuple[str, ...] = DEFAULT_SANDBOX_HIDE_PATHS

    def __post_init__(self) -> None:
        # Only auto-resolve the bare default. Absolute paths, relative paths
        # (resolved vs. working_dir by the child), and custom bare names pass
        # through so explicit config is never silently overridden.
        if self.claude_binary == "claude":
            resolved = shutil.which("claude")
            self.claude_binary = resolved or _find_claude()
        if self.sandbox not in ("none", "bwrap"):
            raise ValueError(
                f"agent.sandbox must be 'none' or 'bwrap', got {self.sandbox!r}"
            )
        # Resolved at construction so a missing bwrap fails the daemon
        # at startup, not every dispatch later with a spawn error that
        # names a path nobody configured.
        self._bwrap: str | None = (
            resolve_bwrap() if self.sandbox == "bwrap" else None
        )

    async def _report_outcome(
        self,
        *,
        session_id: str,
        repo: str | None,
        issue_number: int | None,
        exit_code: int | None,
        stdout: str,
        stderr: str,
    ) -> None:
        """Hand the outcome to the hook, swallowing anything it throws.

        Guarded rather than trusted. The hook writes to the state
        database and may post to a chat transport, and both can fail in
        ways nobody enumerated — but a dispatch that already ran must
        not be turned into an exception by the code that reports on it.
        A cancellation still propagates: the daemon is shutting down and
        the caller's own cleanup depends on it.
        """
        if self.outcome_hook is None:
            return
        try:
            await self.outcome_hook(
                DispatchOutcome(
                    session_id=session_id,
                    repo=repo,
                    issue_number=issue_number,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _logger.error(
                "dispatcher.outcome_hook_failed",
                extra={
                    "session_id": session_id,
                    "repo": repo,
                    "error_type": type(e).__name__,
                    "error": str(e)[:200],
                },
            )

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
        *,
        github_access: bool,
    ) -> SessionResult:
        """Spawn a Claude session and wait for completion.

        ``repo``/``issue_number`` are observability-only: they let the
        dispatcher's events be correlated with the pipeline's without
        re-parsing the composite session id.

        ``github_access`` is REQUIRED and has no default (#219). False
        strips every GitHub credential from the child (see
        :mod:`ctrlrelay.core.agent_env`); True hands it the operator's
        environment as before, which only secops still needs. A default
        would let a new lane inherit whichever answer was written here
        first without anybody deciding it.
        """
        timeout = timeout or self.default_timeout

        # Identity fields repeated on every event of this session, so a
        # single grep on session_id reconstructs the whole run.
        ident: dict[str, Any] = {
            "session_id": session_id,
            "repo": repo,
            "issue_number": issue_number,
        }

        base_env = os.environ.copy()
        base_env.update(self.extra_env)

        gh_config_dir: Path | None = None
        prefix: list[str] = []
        if github_access:
            env = base_env
            stripped: list[str] = []
        else:
            gh_config_dir = Path(
                tempfile.mkdtemp(prefix="ctrlrelay-agent-gh-")
            )
            env, stripped = build_agent_env(
                base_env, gh_config_dir=gh_config_dir
            )
            if self._bwrap is not None:
                prefix = bwrap_prefix(self._bwrap, self.sandbox_hide_paths)
        env["CTRLRELAY_SESSION_ID"] = session_id
        env["CTRLRELAY_STATE_FILE"] = str(state_file)

        # Names only, never values: this is the record that a given
        # session ran without the operator's GitHub login.
        log_event(
            _logger,
            "dispatcher.session.credentials",
            **ident,
            github_access=github_access,
            stripped_env=stripped,
            sandbox="bwrap" if prefix else "none",
        )

        # `--dangerously-skip-permissions` stays (#219). The CLI's other
        # unattended modes either still need every Bash command
        # allow-listed (`dontAsk`) or filter by command TEXT
        # (`--disallowedTools "Bash(git push:*)"`), which `bash -c` or
        # an alias walks straight past. The guard is that the process
        # holds no credential to push with, not what it is allowed to
        # type.
        cmd = [
            *prefix,
            self.claude_binary,
            "-p", prompt,
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])

        try:
            return await self._run_session(
                cmd=cmd,
                env=env,
                ident=ident,
                session_id=session_id,
                prompt=prompt,
                working_dir=working_dir,
                state_file=state_file,
                timeout=timeout,
                resume_session_id=resume_session_id,
                repo=repo,
                issue_number=issue_number,
            )
        finally:
            if gh_config_dir is not None:
                shutil.rmtree(gh_config_dir, ignore_errors=True)

    async def _run_session(
        self,
        *,
        cmd: list[str],
        env: dict[str, str],
        ident: dict[str, Any],
        session_id: str,
        prompt: str,
        working_dir: Path,
        state_file: Path,
        timeout: int,
        resume_session_id: str | None,
        repo: str | None,
        issue_number: int | None,
    ) -> SessionResult:
        """Run the child built by :meth:`spawn_session` to completion."""

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
            # A binary that cannot be exec'd fails every dispatch
            # identically, so a run of these is exactly the outage the
            # alert exists for — reported before the raise, with no
            # exit code, because the child never ran. #197's startup
            # probe catches the common cause at boot; this catches it
            # when the binary disappears mid-day.
            await self._report_outcome(
                session_id=session_id,
                repo=repo,
                issue_number=issue_number,
                exit_code=None,
                stdout="",
                stderr=f"{type(e).__name__}: {e}",
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
            await self._report_outcome(
                session_id=session_id,
                repo=repo,
                issue_number=issue_number,
                exit_code=-1,
                stdout="",
                stderr=f"Session timed out after {timeout}s",
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

        except Exception as e:
            # Anything else out of `communicate()` — an OSError reading
            # the pipes, a decode fault, a broken pipe. The child ran
            # and this dispatch failed, so it MUST be reported: the
            # previous version only reported the spawn failure, the
            # timeout and a normal exit, so a run of these would have
            # been the ticket's own defect in a narrower window —
            # dispatches failing with nothing counting them.
            #
            # Kill the child first. The exception means we are no
            # longer reading its output, and leaving it running against
            # the worktree is what the cancellation arm above exists to
            # prevent.
            # The kill is guarded. The child can exit between the
            # returncode check and the signal, and a bare
            # `ProcessLookupError` from here would REPLACE the original
            # `communicate()` failure and skip the outcome report
            # below — losing both the cause and the count.
            try:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            except Exception:
                pass
            _logger.error(
                "dispatcher.session.communicate_failed",
                extra={
                    **ident,
                    "error_type": type(e).__name__,
                    "error": str(e)[:200],
                    "elapsed_ms": _elapsed_ms(started),
                },
            )
            await self._report_outcome(
                session_id=session_id,
                repo=repo,
                issue_number=issue_number,
                exit_code=None,
                stdout="",
                stderr=f"{type(e).__name__}: {e}",
            )
            raise

        elapsed_ms = _elapsed_ms(started)
        # `or 0` is kept for `SessionResult`, whose `exit_code` is typed
        # `int` and whose downstream readers predate this change. But a
        # `None` returncode means the exit status was not obtained, and
        # the HOOK must not be told 0 for it: `DispatchOutcome.ok` is
        # `exit_code == 0`, so collapsing None to 0 reports a dispatch
        # whose status nobody read as a healthy one, resets the failure
        # run, and clears the alert. That is the "I could not see"
        # becoming "there was nothing to see" collapse, in the one
        # expression the whole alert rests on.
        reported_exit_code = proc.returncode
        exit_code = proc.returncode or 0
        # `errors="replace"`, not a second exception handler. A child
        # emitting invalid UTF-8 raised `UnicodeDecodeError` here —
        # AFTER the guarded `communicate()` and BEFORE the outcome
        # report, so that dispatch was neither persisted nor counted.
        # Replacing the bytes removes the failure mode instead of
        # catching it, and `_extract_agent_session_id` already returns
        # `None` for stdout that will not parse as JSON.
        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")

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

        await self._report_outcome(
            session_id=session_id,
            repo=repo,
            issue_number=issue_number,
            exit_code=reported_exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
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
        *,
        github_access: bool,
    ) -> SessionResult:
        ...


def make_agent_dispatcher(
    agent_config: "AgentConfig",
    *,
    outcome_hook: DispatchOutcomeHook | None,
) -> AgentAdapter:
    """Build an adapter for the configured ``agent.type``.

    Raises :class:`NotImplementedError` with a clear error message if
    the configured type has no adapter — makes a typo surface loudly
    at daemon startup instead of silently falling back to Claude.

    ``outcome_hook`` is a REQUIRED keyword that may be explicitly
    ``None``. It carries #204's correctness — without it nothing
    notices a run of failed dispatches — and an optional parameter
    whose default is the pre-fix behaviour is a silent opt-out that
    reads as an ordinary call. Making it required hands the
    enumeration of call sites to the interpreter and the suite instead
    of to whoever remembers.
    """
    t = agent_config.type
    if t == "claude":
        return ClaudeDispatcher(
            claude_binary=agent_config.binary,
            default_timeout=agent_config.default_timeout_seconds,
            outcome_hook=outcome_hook,
            sandbox=agent_config.sandbox,
            sandbox_hide_paths=tuple(agent_config.sandbox_hide_paths),
        )
    raise NotImplementedError(
        f"agent.type={t!r} has no adapter yet. Implement one that "
        "satisfies the AgentAdapter protocol in "
        "src/ctrlrelay/core/dispatcher.py and register it here, then "
        "open a PR. Supported today: 'claude'."
    )
