"""Headless coding-agent subprocess dispatcher for ctrlrelay.

Today the only concrete adapter is :class:`ClaudeDispatcher` (wraps
``claude -p``). The :func:`make_agent_dispatcher` factory is the seam
where additional backends (Codex, OpenCode, Hermes, Kiro, …) will plug
in: each adapter must expose the same async ``spawn_session`` shape
so pipelines can stay agent-agnostic.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from ctrlrelay.core.agent_env import (
    CLAUDE_PROVIDER_ENV,
    DEFAULT_SANDBOX_CACHE_DIR,
    DEFAULT_SANDBOX_HIDE_PATHS,
    DEFAULT_SANDBOX_READONLY_PATHS,
    DEFAULT_SANDBOX_WRITABLE_PATHS,
    SandboxUnavailableError,
    agent_cache_env,
    build_agent_env,
    bwrap_args_via_fd,
    bwrap_prefix,
    git_dirs,
    resolve_bwrap,
    strip_relay_and_foreign_provider_vars,
)
from ctrlrelay.core.agent_plugins import (
    AgentCapabilities,
    AgentIdentity,
    AgentIdentityConflictError,
    AgentSelection,
    AgentSelectionError,
    native_session_id_is_safe,
)
from ctrlrelay.core.agent_presets import PRESET_NAMES, preset_manifest
from ctrlrelay.core.binaries import resolve_binary
from ctrlrelay.core.budget import (
    DEFAULT_POLL_SECONDS as BUDGET_POLL_SECONDS,
)
from ctrlrelay.core.budget import (
    BudgetScope,
    BudgetWatch,
    ExitReadingMeter,
    RunUsage,
    TranscriptMeter,
    is_budget_reason,
    parse_result_cost,
    resolve_limits,
)
from ctrlrelay.core.checkpoint import CheckpointState, CheckpointStatus, read_checkpoint
from ctrlrelay.core.obs import get_logger, hash_text, log_event
from ctrlrelay.core.stuck import StuckPolicy, watch_transcript
from ctrlrelay.core.usage import (
    UsageStore,
    parse_agent_session_id,
    parse_rate_limit_event,
)

if TYPE_CHECKING:
    from ctrlrelay.core.config import AgentConfig, BudgetConfig, Config

_logger = get_logger("core.dispatcher")

# Enough of the child's stderr to name the failure without dumping agent
# output into the log stream.
_STDERR_TAIL_CHARS = 500

# How long an agent's process group gets between SIGTERM and SIGKILL when
# the relay ends it: long enough for a CLI to flush its transcript, short
# enough that a timeout still means roughly what it says.
_GROUP_GRACE_SECONDS = 3.0


def _still_ours(proc: Any, pgid: int) -> bool:
    """Is ``pgid`` still the group this agent's child led?

    While the relay has not reaped the child, it holds its pid — a zombie
    included — so the number cannot have been reused. Once it is reaped the
    group it led has no leader any more: if a process answers at that pid,
    the number was freed and taken by somebody else, possibly much later (a
    descendant holding the output pipes keeps ``communicate()`` waiting
    until the timeout). A new group is always created by a live leader, so
    a leaderless group under that id is the one the child left behind.
    """
    if proc.returncode is None:
        return True
    try:
        os.getpgid(pgid)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _agent_group(proc: Any) -> int | None:
    """The process group the agent leads, or ``None`` to signal it alone.

    Every agent is spawned with ``start_new_session=True``, so its group id
    is its own pid and nothing of the relay's is in it (#394). Signalling
    that group is what reaches a background job the agent left behind.

    The guard is the point of this function. Signalling a group the relay
    is itself in would stop the relay — and every run it holds — which is
    exactly what an agent that shared the relay's group could do to it
    before this. So a group is only ever returned when it provably is not
    the relay's and is still the agent's: a real subprocess, leading its
    own group, not this process's group, and — once reaped — with no other
    process holding the number (:func:`_still_ours`). Anything else (a
    test double, a child that somehow did not get its own session, a
    reused id) falls back to signalling the child alone.
    """
    if not isinstance(proc, asyncio.subprocess.Process):
        return None
    pid = proc.pid
    own = os.getpgrp()
    if not isinstance(pid, int) or pid <= 1 or pid == own:
        return None
    if proc.returncode is not None:
        return pid if _still_ours(proc, pid) else None
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return None
    if pgid != pid or pgid == own:
        return None
    return pgid


def _signal_group(proc: Any, pgid: int, sig: int) -> bool:
    """Signal the group; ``False`` once nothing of the agent's is left in it.

    Re-checked before every signal, not once: the group can empty and its
    id be taken between two signals of the same grace period.
    """
    if not _still_ours(proc, pgid):
        return False
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _kill_agent_now(proc: Any) -> None:
    """SIGKILL the agent's whole group at once, or the child alone."""
    pgid = _agent_group(proc)
    if pgid is not None:
        _signal_group(proc, pgid, signal.SIGKILL)
        return
    try:
        if proc.returncode is None:
            proc.kill()
    except ProcessLookupError:
        pass


async def _end_agent(
    proc: Any, *, running: bool, grace: float = _GROUP_GRACE_SECONDS,
) -> None:
    """Stop the agent and everything left in its group, then reap the child.

    SIGTERM, up to ``grace`` seconds for the group to empty, then SIGKILL.
    Called after a normal exit too (``running=False``): the child is gone by
    then, but a job it put in the background is not, and nothing else would
    ever stop it. Without a group of its own there is nothing left to reap
    after a normal exit, and on a timeout (``running=True``) the child is
    killed alone, as before.
    """
    pgid = _agent_group(proc)
    if pgid is None:
        if running:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        return
    if _signal_group(proc, pgid, signal.SIGTERM):
        deadline = time.monotonic() + grace
        while _signal_group(proc, pgid, 0):
            if time.monotonic() >= deadline:
                _signal_group(proc, pgid, signal.SIGKILL)
                break
            await asyncio.sleep(0.05)
    if proc.returncode is None:
        await proc.wait()


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


def _normalise_builtin_binary(binary: str, *, label: str) -> str:
    """Anchor a bare built-in name; refuse ambiguous relative paths.

    A relative path is resolved by the child against its worktree, which
    the identity pin cannot see before the worktree exists. Refusing it
    keeps the pinned launcher stable. An unresolved bare name is left
    alone: the spawn will fail exactly as before, and no other program can
    appear without PATH changing under the daemon.
    """
    if Path(binary).is_absolute():
        return binary
    if "/" in binary or "\\" in binary:
        raise ValueError(
            f"{label} must be absolute or a bare name, got {binary!r}; "
            "a relative path would resolve against the run's worktree"
        )
    found = shutil.which(binary)
    return str(Path(found).absolute()) if found else binary


def _absolute_path_only(env: dict[str, str]) -> dict[str, str]:
    """``env`` with every relative ``PATH`` entry removed.

    An agent starts in the run's worktree, which is repository content.
    Any lookup through a relative entry (``.``, an empty entry, ``bin``)
    is a lookup inside the repository: ``exec`` finding a bare agent name,
    bwrap's own search inside the sandbox, an ``#!/usr/bin/env node``
    launcher finding its interpreter, and every command the agent runs
    after that. Dropping those entries from the child's environment covers
    all of them at once; a name found nowhere else fails as a missing
    binary always did.
    """
    entries = env.get("PATH", "").split(os.pathsep)
    kept = [entry for entry in entries if os.path.isabs(entry)]
    if kept == entries:
        return env
    return {**env, "PATH": os.pathsep.join(kept)}


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
    # The relay killed the agent at its time limit (#311). Set only by the
    # relay's own timeout path, never parsed from anything the agent wrote.
    timed_out: bool = False
    # The relay stopped the agent, and why: its transcript showed it going
    # in circles (#224), or it went over its hard token limit (#223; the
    # reason then starts with ``budget: ``). Set only by the relay's own
    # watches, which share one stop path.
    stuck_reason: str | None = None
    # What the session has used so far, this spawn included (#223). None
    # when nothing could be measured (a backend with no known transcript).
    usage: RunUsage | None = None
    # Concrete identity selected at the run boundary (#379). Pipelines
    # compare this with the sessions-row pin before storing a native id.
    agent: AgentIdentity | None = None

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


class CliAgentAdapter:
    """What every CLI-driven adapter does identically, in one place.

    Spawning the child, enforcing the timeout, killing it on cancel,
    decoding its output, reading the checkpoint, reporting the outcome
    to the hook — none of that is specific to which agent CLI ran, and
    all of it is where the hard-won correctness lives: #204's outcome
    report on every exit path, the guarded ``communicate()``, the
    ``errors="replace"`` decode, and the rule that a ``None`` returncode
    is NOT reported to the hook as 0.

    It lived on :class:`ClaudeDispatcher` while there was one adapter.
    Extracting it is the alternative to a second copy, and a second copy
    is how the next backend gets a dispatch that fails without being
    counted — the exact defect #204 was filed for, reintroduced
    somewhere nobody is looking.

    Subclasses are dataclasses that own the argv and the environment
    (that part IS agent-specific), hold ``outcome_hook``, ``active`` and
    ``started`` as fields, and supply these hooks:

    - ``agent_type``   the ``agent.type`` value this adapter answers to,
                       recorded on every event so a log with two
                       backends in it can be read.
    - ``agent_binary`` the resolved executable, for the start event and
                       the spawn-failure record.
    - ``_parse_agent_session_id`` / ``_record_usage``  how THIS CLI
                       reports its own session id and its usage, which
                       is the one thing no two of them agree on.
    """

    # Set by each subclass. Declared here so the base's own events can
    # name the backend without every subclass remembering to.
    agent_type: ClassVar[str] = "unknown"
    plugin_id: ClassVar[str] = "builtin"
    plugin_version: ClassVar[str] = "1"
    capabilities: ClassVar[AgentCapabilities] = AgentCapabilities()
    # Context filenames this CLI reads for repository instructions. The
    # pipeline links the same context file under each name.
    instructions_files: ClassVar[tuple[str, ...]] = ("CLAUDE.md",)

    @property
    def provider_env_allow(self) -> tuple[str, ...]:
        """The model-provider variables this backend may receive (#390).

        Exact names or ``PREFIX*`` patterns, matched against
        :data:`ctrlrelay.core.agent_env.PROVIDER_KEY_VARS`; every other
        provider key is removed from the agent's environment. The base
        answer is the backend's ``env_allow`` field, which the opencode
        and generic adapters carry from config; claude overrides it.
        """
        return tuple(getattr(self, "env_allow", ()))

    def _agent_env_for_run(
        self, base_env: dict[str, str], github_access: bool,
        private_root: Path | None,
    ) -> tuple[dict[str, str], list[str]]:
        """The one place the credential rules are applied to an agent.

        Without GitHub access: :func:`build_agent_env` (#219) plus the
        provider rule. With it (secops): only the relay's own secrets and
        other backends' provider keys go (#390); GitHub stays, on purpose.
        ``extra_env`` gets no exemption: a provider key reaches the agent
        only through ``provider_env_allow``, whoever put it in the env.
        """
        if github_access:
            return strip_relay_and_foreign_provider_vars(
                base_env, provider_env_allow=self.provider_env_allow,
            )
        assert private_root is not None
        return build_agent_env(
            base_env,
            gh_config_dir=private_root / "gh",
            runtime_dir=private_root / "run",
            provider_env_allow=self.provider_env_allow,
        )

    @property
    def manifest_fingerprint(self) -> str:
        """Built-in invocation identity: adapter contract plus launcher."""
        payload = {
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "binary": self.agent_binary,
            "executable": self._executable_fingerprint(),
        }
        encoded = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _executable_fingerprint(self) -> dict[str, Any]:
        """Identify the stable launcher the operator configured.

        The launcher path, not the resolved versioned target or its bytes,
        is the compatibility boundary. Claude's installer updates by
        retargeting its launcher to a new version directory, and pinning
        either the target or the contents would turn every self-update
        into a false identity conflict on the next resume. Changing the
        launcher itself — or a bare name resolving somewhere else at
        startup — still changes the pin.
        """
        return {"launcher": self.agent_binary}

    @property
    def agent_identity(self) -> AgentIdentity:
        """The actual plugin/configuration this adapter would run."""
        model = getattr(self, "model", None)
        provider = getattr(self, "provider", None)
        if provider is None and isinstance(model, str) and "/" in model:
            provider = model.split("/", 1)[0]
        return AgentIdentity(
            agent_type=str(self.agent_type),
            plugin_id=str(self.plugin_id),
            plugin_version=str(self.plugin_version),
            model=model if isinstance(model, str) else None,
            provider=provider if isinstance(provider, str) else None,
            fingerprint=getattr(self, "manifest_fingerprint", None),
        )

    # How the live transcript is watched for a run going in circles (#224).
    # A plain class attribute, not a dataclass field: every adapter gets
    # the watch by construction, and a test sets it on the instance.
    # ``None`` turns the watch off.
    stuck_policy: StuckPolicy | None = StuckPolicy()
    # The token budget (#223), from ``agent.budget``; ``None`` is no limit.
    # Usage is measured and reported whatever this says. Class attributes
    # for the same reason as ``stuck_policy``.
    budget: BudgetConfig | None = None
    budget_poll_seconds: float = BUDGET_POLL_SECONDS

    @property
    def agent_binary(self) -> str:
        """The executable this adapter runs. Subclasses override."""
        raise NotImplementedError

    def _session_environment(
        self, session_id: str, state_file: Path, github_access: bool
    ) -> tuple[dict[str, str], list[str], Path | None]:
        """Build one CLI-neutral environment and its cleanup handle.

        The temporary root exists only when GitHub credentials are stripped.
        The caller owns its cleanup so every adapter gets the same credential
        boundary without duplicating the private-directory lifecycle.
        """
        base_env = os.environ.copy()
        base_env.update(getattr(self, "extra_env", {}))
        private_root: Path | None = None
        if not github_access:
            private_root = Path(tempfile.mkdtemp(prefix="ctrlrelay-agent-"))
            (private_root / "gh").mkdir()
            (private_root / "run").mkdir(mode=0o700)
        env, stripped = self._agent_env_for_run(
            base_env, github_access, private_root,
        )
        env["CTRLRELAY_SESSION_ID"] = session_id
        env["CTRLRELAY_STATE_FILE"] = str(state_file)
        return env, stripped, private_root

    def _transcript_dirs(
        self, working_dir: Path, env: dict[str, str]
    ) -> list[Path]:
        """Where this CLI writes the live transcript of a run in
        ``working_dir``. Empty by default: a backend whose transcript is
        not known is not watched, rather than watched somewhere wrong."""
        return []

    async def _communicate_watched(
        self,
        proc: Any,
        *,
        watches: list[tuple[str, Callable[[], Awaitable[str]]]],
        ident: dict[str, Any],
        stops: list[tuple[str, str]],
        stdin_text: str | None = None,
    ) -> tuple[bytes, bytes]:
        """``proc.communicate()``, while the watches run.

        The one "stop with a reason" path, shared by the stuck watch (#224)
        and the budget watch (#223). Each watch is ``(kind, coroutine)``;
        the coroutine returns only when the run must stop, with the
        reason. The first to return is recorded in ``stops`` and the child
        is killed, which ends ``communicate()`` the normal way. Every watch
        is cancelled however this returns, the time limit included.
        """
        stdin_data = stdin_text.encode() if stdin_text is not None else None
        if not watches:
            if stdin_data is None:
                return await proc.communicate()
            return await proc.communicate(input=stdin_data)

        async def _watch(kind: str, watch: Callable[[], Awaitable[str]]) -> None:
            reason = await watch()
            if stops:
                return
            stops.append((kind, reason))
            _logger.warning(
                "dispatcher.session.stuck" if kind == "stuck"
                else f"dispatcher.session.stopped_{kind}",
                extra={**ident, "reason": reason},
            )
            _kill_agent_now(proc)

        watchers = [asyncio.create_task(_watch(k, w)) for k, w in watches]
        try:
            if stdin_data is None:
                return await proc.communicate()
            return await proc.communicate(input=stdin_data)
        finally:
            for watcher in watchers:
                watcher.cancel()
            for watcher in watchers:
                try:
                    await watcher
                except BaseException:
                    pass

    def _parse_agent_session_id(self, stdout_text: str) -> str | None:
        """The agent's own session id, for a later resume.

        ``None`` by default: an adapter that cannot read one from its
        output is honest about it, and the pipelines already treat
        ``None`` as "cannot resume, start fresh".
        """
        return None

    def _record_agent_pid(self, session_id: str, pid: int | None) -> None:
        """Note which process is this session's agent (#309), in
        ``agents/<session_id>.pid`` beside the state database, so the status
        page can tell an agent inside a long command from one that stopped.
        Several runs share the poller's pid, so only the agent's own pid
        says which command belongs to which run. Best-effort; files older
        than a week are pruned here."""
        store = getattr(self, "usage_store", None)
        if store is None or not pid:
            return
        folder = Path(store.path).parent / "agents"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            cutoff = time.time() - 7 * 86400
            for old in folder.glob("*.pid"):
                try:
                    if old.stat().st_mtime < cutoff:
                        old.unlink()
                except OSError:
                    pass
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
            (folder / f"{safe}.pid").write_text(f"{int(pid)}\n")
        except Exception as e:  # noqa: BLE001 - must never fail a run
            log_event(
                _logger, "dispatcher.agent_pid.write_failed",
                session_id=session_id, error=str(e)[:200],
            )

    async def _round_unmeasured(self, budget: BudgetWatch | None) -> RunUsage | None:
        """Record a round whose output was lost (timed out, cancelled, or
        unreadable pipes) as unknown, for a backend measured at exit (#387).

        The round used something, so the session's total becomes unknown
        rather than staying at a number that leaves it out. Returns the
        session total, or None for a backend that is not measured at exit
        (the transcript meter already recorded what it saw).
        """
        if budget is None or not self._reads_usage_at_exit():
            return None
        return budget.total(await budget.finish_with(RunUsage(unreadable=1)))

    def _reads_usage_at_exit(self) -> bool:
        """True when this backend reports its token usage in its own output
        (#387) rather than in a transcript the relay can watch live."""
        return False

    def _usage_at_exit(
        self,
        stdout_text: str,
        *,
        resume_session_id: str | None,
        agent_session_id: str | None,
        ident: dict[str, Any],
        exited_cleanly: bool = True,
    ) -> RunUsage | None:
        """This spawn's usage read from the CLI's output, or None when the
        backend does not report it there (the transcript meter is used).
        ``exited_cleanly`` is False for a run that was killed, timed out or
        exited non-zero, whose last report may not be its final usage."""
        return None

    def _record_usage(self, stdout_text: str, ident: dict) -> None:
        """Keep this run's usage reading, if the backend reports one.

        Nothing by default. A backend with no subscription window has
        no reading to keep, and writing "unknown" for it would erase a
        reading another backend legitimately holds.
        """
        return None

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
        budget_scope: BudgetScope | None = None,
        sandbox_prefix_len: int = 0,
        stdin_text: str | None = None,
    ) -> SessionResult:
        """Run the child built by :meth:`spawn_session` to completion.

        The first ``sandbox_prefix_len`` items of ``cmd`` are the bwrap
        prefix. Its options are moved into a file descriptor right before
        the child starts, and closed right after (#362), so no error path
        can leave the descriptor open.
        """

        # The prompt can carry issue bodies and operator answers, so only its
        # hash and length are ever logged (#41).
        log_event(
            _logger,
            "dispatcher.session.start",
            **ident,
            agent_type=self.agent_type,
            resume=resume_session_id is not None,
            binary=self.agent_binary,
            working_dir=str(working_dir),
            timeout=timeout,
            prompt_hash=hash_text(prompt),
            prompt_len=len(prompt),
        )
        started = time.monotonic()
        # Transcript rows are stamped in wall-clock time. A second of slack
        # for the clock the CLI reads being a hair behind this one.
        wall_started = time.time() - 1.0
        stops: list[tuple[str, str]] = []
        folders = self._transcript_dirs(working_dir, env)
        budget = self._budget_watch(
            folders, since=wall_started, scope=budget_scope, ident=ident,
        )
        watches: list[tuple[str, Callable[[], Awaitable[str]]]] = []
        if folders and self.stuck_policy is not None:
            policy = self.stuck_policy
            watches.append(("stuck", lambda: watch_transcript(
                folders, since=wall_started, policy=policy, ident=ident,
            )))
        if budget is not None and not self._reads_usage_at_exit():
            # A backend measured at exit has nothing to poll while it runs;
            # its limit is checked before it starts, below.
            watches.append(("budget", budget.run))

        if budget is not None and self._reads_usage_at_exit():
            # A backend measured only at exit: nothing in the round itself
            # can stop it, and no watch polls it while it runs. So a session
            # already at its hard limit is refused here, before the round
            # starts and spends anything (#387).
            refused = await budget.check(RunUsage())
            if refused is not None:
                log_event(
                    _logger, "dispatcher.session.stopped_budget", **ident,
                    reason=refused, before_start=True,
                )
                stderr_text = f"Session stopped: {refused}\n"
                await self._report_outcome(
                    session_id=session_id,
                    repo=repo,
                    issue_number=issue_number,
                    exit_code=-1,
                    stdout="",
                    stderr=stderr_text,
                )
                return SessionResult(
                    session_id=session_id,
                    exit_code=-1,
                    state=None,
                    stderr=stderr_text,
                    stuck_reason=refused,
                    usage=budget.total(RunUsage()),
                    agent=self.agent_identity,
                )

        try:
            # The sandbox's options go through a file descriptor, not the
            # command line, so no number of mounts can pass ARG_MAX (#362).
            short, args_fd = bwrap_args_via_fd(cmd[:sandbox_prefix_len])
            pass_fds = () if args_fd is None else (args_fd,)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *short, *cmd[sandbox_prefix_len:],
                    cwd=working_dir,
                    env=_absolute_path_only(env),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    pass_fds=pass_fds,
                    # Its own session, so its own process group: the relay
                    # can then stop everything the agent started without
                    # the agent being able to stop the relay (#394).
                    start_new_session=True,
                    # Never the daemon's own stdin. A CLI that appends
                    # piped stdin to its prompt (codex exec does) waits
                    # for an end-of-file that an inherited pipe never
                    # sends, and the run sits until the timeout.
                    stdin=(
                        asyncio.subprocess.DEVNULL if stdin_text is None
                        else asyncio.subprocess.PIPE
                    ),
                )
            finally:
                # The child has its own copy now; bwrap reads it at start.
                for fd in pass_fds:
                    with contextlib.suppress(OSError):
                        os.close(fd)
        except Exception as e:
            # Usually the binary isn't where we resolved it — the exception
            # propagates to the pipeline, but without this it lands there
            # with no record of which path we tried.
            _logger.error(
                "dispatcher.session.spawn_failed",
                extra={
                    **ident,
                    "binary": self.agent_binary,
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

        self._record_agent_pid(session_id, getattr(proc, "pid", None))

        try:
            stdout, stderr = await asyncio.wait_for(
                self._communicate_watched(
                    proc, watches=watches, ident=ident, stops=stops,
                    stdin_text=stdin_text,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            await _end_agent(proc, running=True)
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
            timed_out_usage = await self._round_unmeasured(budget)
            return SessionResult(
                session_id=session_id,
                exit_code=-1,
                state=None,
                stderr="Session timed out",
                timed_out=True,
                usage=timed_out_usage,
                agent=self.agent_identity,
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
            _kill_agent_now(proc)
            if proc.returncode is None:
                try:
                    await asyncio.shield(proc.wait())
                except asyncio.CancelledError:
                    pass
            try:
                await asyncio.shield(self._round_unmeasured(budget))
            except BaseException:  # noqa: BLE001 - the cancel must still propagate
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
                _kill_agent_now(proc)
                if proc.returncode is None:
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
            try:
                await self._round_unmeasured(budget)
            except Exception:  # noqa: BLE001 - keep the original failure
                pass
            raise

        # The child exited on its own. Anything it left running in the
        # background is still in its group; stop it before the run is
        # reported done, or it keeps running as the relay user afterwards.
        await _end_agent(proc, running=False)

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
        # catching it, and every adapter's `_parse_agent_session_id`
        # already returns `None` for stdout it cannot read.
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

        agent_session_id = self._parse_agent_session_id(stdout_text)
        self._record_usage(stdout_text, ident)
        usage: RunUsage | None = None
        if budget is not None:
            exit_reading = self._usage_at_exit(
                stdout_text,
                resume_session_id=resume_session_id,
                agent_session_id=agent_session_id,
                ident=ident,
                exited_cleanly=exit_code == 0 and not stops,
            )
            if exit_reading is not None:
                spawn_usage: RunUsage | None = await budget.finish_with(exit_reading)
            else:
                spawn_usage = await budget.finish(parse_result_cost(stdout_text))
            if spawn_usage is not None:
                usage = budget.total(spawn_usage)

        stop_kind, stop_reason = stops[0] if stops else (None, None)
        if stop_kind == "budget" and state is not None:
            # Over budget is not handed over, whatever the agent wrote just
            # before the kill landed (#223).
            log_event(
                _logger, "dispatcher.budget.checkpoint_discarded", **ident,
                checkpoint_status=state.status.value,
            )
            state = None
        # A checkpoint the agent wrote wins over a stuck stop: it finished,
        # or asked, before the kill landed, so it was not stuck at the end.
        stuck_reason = stop_reason if state is None else None
        if stuck_reason is not None:
            # In stderr too, so a lane that reads only stderr still says why.
            said = (
                stuck_reason if is_budget_reason(stuck_reason)
                else f"stuck: {stuck_reason}"
            )
            stderr_text = f"Session stopped: {said}\n{stderr_text}"

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
            stuck_reason=stuck_reason,
            usage=usage,
            agent=self.agent_identity,
        )

    def _budget_watch(
        self,
        folders: list[Path],
        *,
        since: float,
        scope: BudgetScope | None,
        ident: dict[str, Any],
    ) -> BudgetWatch | None:
        """The usage meter and budget check for one spawn (#223), or None
        when this backend's transcript is not known and nothing can be
        measured. A limit that cannot be enforced is said in the log."""
        scope = scope or BudgetScope()
        limits = resolve_limits(self.budget, scope.labels)
        if not folders and self._reads_usage_at_exit():
            if limits.any:
                # Said once per spawn: the limit is never applied in the
                # middle of a round. With a session total (dev, task) it is
                # applied before the next round; with none, never.
                log_event(
                    _logger, "dispatcher.budget.measured_at_exit", **ident,
                    agent_type=self.agent_type, soft=limits.soft,
                    hard=limits.hard,
                    # The pre-start check only bites when earlier rounds can
                    # be counted: a session scope that records them, or a
                    # total handed in.
                    applies_between_rounds=(
                        scope.on_usage is not None or scope.before != RunUsage()
                    ),
                )
            return BudgetWatch(
                ExitReadingMeter(),
                limits=limits, scope=scope, ident=ident,
                poll_seconds=self.budget_poll_seconds,
            )
        if not folders:
            if limits.any:
                log_event(
                    _logger, "dispatcher.budget.unmeasured", **ident,
                    agent_type=self.agent_type, soft=limits.soft,
                    hard=limits.hard,
                )
            return None
        return BudgetWatch(
            TranscriptMeter(folders, since=since),
            limits=limits, scope=scope, ident=ident,
            poll_seconds=self.budget_poll_seconds,
        )


@dataclass
class ClaudeDispatcher(CliAgentAdapter):
    """Spawns and manages Claude subprocess sessions."""

    @property
    def provider_env_allow(self) -> tuple[str, ...]:
        """Anthropic's variables only; a Codex or Gemini key stays out (#390)."""
        return CLAUDE_PROVIDER_ENV

    agent_type: ClassVar[str] = "claude"
    plugin_id: ClassVar[str] = "claude"
    capabilities: ClassVar[AgentCapabilities] = AgentCapabilities(
        resume=True, structured_events=True, usage=True, sandbox=True,
    )

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
    # "none" or "bwrap" (#219, #236). With "bwrap", a spawn WITHOUT GitHub
    # access runs under bubblewrap in a read-only copy of the machine:
    # ``sandbox_hide_paths`` empty, and only the worktree, its git
    # directory, a private /tmp, ``sandbox_writable_paths`` (minus
    # ``sandbox_readonly_paths``) and ``sandbox_cache_dir`` writable. Off
    # by default: it needs bwrap installed and unprivileged user
    # namespaces allowed, which not every host has.
    sandbox: str = "none"
    sandbox_hide_paths: tuple[str, ...] = DEFAULT_SANDBOX_HIDE_PATHS
    sandbox_writable_paths: tuple[str, ...] = DEFAULT_SANDBOX_WRITABLE_PATHS
    sandbox_readonly_paths: tuple[str, ...] = DEFAULT_SANDBOX_READONLY_PATHS
    sandbox_cache_dir: str = DEFAULT_SANDBOX_CACHE_DIR
    # Where each run's subscription usage reading is kept (#247). Required,
    # here and in `make_agent_dispatcher`: ``None`` keeps no reading, and
    # a construction has to say so rather than get it by omission.
    usage_store: UsageStore | None = field(kw_only=True)
    # Agent sessions running now, and started in all. The idle usage
    # probe starts only at zero running and drops its reading if any
    # session started while it was out, so it never overwrites a newer
    # reading from a run.
    active: int = field(default=0, init=False)
    started: int = field(default=0, init=False)

    @property
    def agent_binary(self) -> str:
        return self.claude_binary

    def _parse_agent_session_id(self, stdout_text: str) -> str | None:
        value = _extract_agent_session_id(stdout_text)
        return value if value is not None and native_session_id_is_safe(value) else None

    def _transcript_dirs(
        self, working_dir: Path, env: dict[str, str]
    ) -> list[Path]:
        """``<config dir>/projects/<working dir, "/", "." and "_" as "-">``,
        the folder :mod:`ctrlrelay.web.transcript` reads too. Both the path
        as given and its resolved form, since which one the CLI sees
        depends on how the worktree path was spelled."""
        base = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        out: list[Path] = []
        for spelled in (str(working_dir), str(working_dir.resolve())):
            folder = base / "projects" / re.sub(r"[/._]", "-", spelled)
            if folder not in out:
                out.append(folder)
        return out

    def __post_init__(self) -> None:
        # Only auto-resolve the bare default. Custom bare names are anchored
        # through PATH so the pinned file and the executed file are one and
        # the same. Relative paths are refused because the child resolves
        # them against the worktree, which the pin cannot see.
        if self.claude_binary == "claude":
            resolved = shutil.which("claude") or _find_claude()
            self.claude_binary = _normalise_builtin_binary(
                resolved or "claude", label="agent.binary",
            )
        else:
            self.claude_binary = _normalise_builtin_binary(
                self.claude_binary, label="agent.binary",
            )
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

    def _sandbox_prefix(
        self,
        env: dict[str, str],
        working_dir: Path,
        state_file: Path,
        private_root: Path,
    ) -> list[str]:
        """The bwrap argv for one run (#236), and the agent's cache
        variables set in ``env``. Raises :class:`SandboxUnavailableError`
        rather than start a run it cannot confine as configured."""
        if env.get("CLAUDE_CONFIG_DIR"):
            raise SandboxUnavailableError(
                "CLAUDE_CONFIG_DIR is set, and the sandbox only knows the "
                "agent CLI's default state (~/.claude, ~/.claude.json); "
                "unset it for the relay to run with agent.sandbox: bwrap"
            )
        cache_raw = os.path.expandvars(os.path.expanduser(self.sandbox_cache_dir))
        if "$" in cache_raw:
            raise SandboxUnavailableError(
                f"agent.sandbox_cache_dir {self.sandbox_cache_dir!r} names an "
                "unset variable"
            )
        cache_dir = Path(cache_raw)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SandboxUnavailableError(
                f"agent.sandbox_cache_dir {cache_dir}: {type(e).__name__}: {e}"
            ) from e
        # It is bound writable as a whole: one that is, or links to, the
        # home directory or above would open all of it.
        if Path.home().resolve().is_relative_to(cache_dir.resolve()):
            raise SandboxUnavailableError(
                f"agent.sandbox_cache_dir {cache_dir} resolves to "
                f"{cache_dir.resolve()}, which contains the home directory; "
                "point it at a directory of its own"
            )
        # The checkpoint is written inside the worktree; a state file
        # elsewhere would need its directory opened, which could be home.
        if not state_file.parent.resolve().is_relative_to(working_dir.resolve()):
            raise SandboxUnavailableError(
                f"the state file {state_file} is outside the worktree "
                f"{working_dir}, so the sandbox cannot let the agent write it"
            )
        state_file.parent.mkdir(parents=True, exist_ok=True)
        env.update(agent_cache_env(cache_dir))
        dot_git = working_dir / ".git"
        repo_dirs = git_dirs(working_dir)
        if not repo_dirs:
            # Every sandboxed run is in a git worktree. Without its git
            # directory the agent could not commit, and a writable
            # worktree with no ``.git`` lets it plant one, hooks and all,
            # for the relay's later git commands there.
            raise SandboxUnavailableError(
                f"could not find the git directories of {working_dir} (it "
                "must be a git worktree's top, and git 2.31 or later is "
                "needed); not starting the agent"
            )
        # What every later git command in this clone runs, the relay's
        # included: its config (core.hooksPath, core.fsmonitor, filters),
        # per-worktree config, hooks, and the worktree's pointer to its
        # git directory. The agent may not write them.
        git_protected: list[Path] = []
        for i, d in enumerate(repo_dirs):
            # Files, made here when missing: the sandbox would otherwise
            # make a missing name without a suffix a directory.
            for name in ("config", "config.worktree"):
                (d / name).touch(exist_ok=True)
                git_protected.append(d / name)
            # Submodules' git dirs, whose config git reads in each
            # submodule when run in this one.
            if (d / "modules").exists():
                git_protected.append(d / "modules")
            if i == 0:
                git_protected.append(d / "hooks")
                # Every worktree's git dir is in here: the others belong to
                # other runs, so the folder is read-only and only this
                # run's own (bound later, deeper) is writable.
                if (d / "worktrees").exists():
                    git_protected.append(d / "worktrees")
            else:
                # A linked worktree's pointers back to the shared clone:
                # rewritten, they would send git to a config of the agent's.
                git_protected += [
                    d / n for n in ("commondir", "gitdir") if (d / n).exists()
                ]
        if dot_git.is_file():
            git_protected.append(dot_git)

        # The agent CLI rewrites its main config file on every start, and it
        # also holds the operator's MCP server commands: the run gets its
        # own copy, thrown away with the private dir.
        copies: list[tuple[Path, Path]] = []
        main_config = Path.home() / ".claude.json"
        if main_config.is_file():
            copy = private_root / "claude.json"
            shutil.copyfile(main_config, copy)
            copy.chmod(0o600)
            # At the real path: a bind on a link lands where it leads.
            copies.append((copy, Path(os.path.realpath(main_config))))
        # Where the agent CLI writes this run's transcript: the only part
        # of the read-only projects folder the run may write (#362). Made
        # here, since the sandbox cannot create it under a read-only one.
        transcript_dirs = self._transcript_dirs(working_dir, env)
        projects = Path(os.path.realpath(Path.home() / ".claude" / "projects"))
        for folder in transcript_dirs:
            folder.mkdir(parents=True, exist_ok=True)
            # Bound writable as it resolves: a link left there (a run before
            # #362 could write the projects folder) would open its target,
            # the home directory for one. Only a plain folder directly in
            # `projects` is opened.
            if folder.is_symlink() or Path(os.path.realpath(folder)).parent != projects:
                raise SandboxUnavailableError(
                    f"the transcript folder {folder} is a link or is not "
                    f"directly in {projects}; remove it before running "
                    "sandboxed"
                )
        return bwrap_prefix(
            self._bwrap,
            self.sandbox_hide_paths,
            writable=[
                working_dir, *repo_dirs, private_root, cache_dir,
                *self.sandbox_writable_paths, *transcript_dirs,
            ],
            readonly=[*self.sandbox_readonly_paths, *git_protected],
            copies=copies,
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
        budget_scope: BudgetScope | None = None,
        resume_requested: bool = False,
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
        if resume_session_id is not None and not native_session_id_is_safe(
            resume_session_id,
        ):
            raise ValueError("unsafe native agent session id")
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

        private_root: Path | None = None
        prefix: list[str] = []
        if github_access:
            env, stripped = self._agent_env_for_run(base_env, True, None)
        else:
            private_root = Path(tempfile.mkdtemp(prefix="ctrlrelay-agent-"))
            (private_root / "gh").mkdir()
            (private_root / "run").mkdir(mode=0o700)
            env, stripped = self._agent_env_for_run(
                base_env, False, private_root,
            )
            if self._bwrap is not None:
                try:
                    prefix = self._sandbox_prefix(
                        env, working_dir, state_file, private_root,
                    )
                except Exception:
                    shutil.rmtree(private_root, ignore_errors=True)
                    raise
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
        # is handed no credential to push with, not what it is allowed
        # to type. That stops an agent using normal tooling; one that
        # deliberately reads credential files as the same OS user is
        # stopped only by ``agent.sandbox: bwrap`` (#236).
        cmd = [
            *prefix,
            self.claude_binary,
            "-p", prompt,
            # stream-json, not json: only the stream carries the
            # `rate_limit_event` line with the subscription usage (#247).
            # `--verbose` is required with it in print mode.
            "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions",
        ]
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])

        self.active += 1
        self.started += 1
        try:
            return await self._run_session(
                sandbox_prefix_len=len(prefix),
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
                budget_scope=budget_scope,
            )
        finally:
            self.active -= 1
            if private_root is not None:
                shutil.rmtree(private_root, ignore_errors=True)

    def _record_usage(self, stdout_text: str, ident: dict) -> None:
        """Keep the run's usage reading. A run with no readable reading
        records unknown: an older number must not go on pausing the
        machine, or be shown as current, after the latest run could not
        confirm it."""
        if self.usage_store is None:
            return
        try:
            reading = parse_rate_limit_event(stdout_text)
        except Exception as e:  # an undocumented field: never fail the run
            log_event(
                _logger, "dispatcher.usage.parse_failed", **ident,
                error=str(e)[:200],
            )
            self.usage_store.mark_unknown()
            return
        if reading is None:
            log_event(_logger, "dispatcher.usage.absent", **ident)
            self.usage_store.mark_unknown()
            return
        self.usage_store.write(reading)


# What an `opencode run --format json` event line calls the session the
# run belongs to, in the order to try. The schema has moved between
# versions — camel, snake, and nested under `info` have all been seen in
# the wild — and reading the WRONG key would resume somebody else's
# session, so each spelling is an exact key rather than a substring
# search over the line.
_OPENCODE_SESSION_KEYS: tuple[str, ...] = ("sessionID", "sessionId", "session_id")


def _extract_opencode_session_id(stdout_text: str) -> str | None:
    """The opencode session id, for ``opencode run --session``.

    ``None`` whenever nothing in the output carries one: an empty run, a
    panic, a line that is not JSON, or a version whose events name it
    something else again. The caller then starts a fresh session, which
    is recoverable — a guessed id is not.
    """
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        for scope in (event, event.get("info"), event.get("session")):
            if not isinstance(scope, dict):
                continue
            for key in _OPENCODE_SESSION_KEYS:
                value = scope.get(key)
                if isinstance(value, str) and native_session_id_is_safe(value):
                    return value
    return None


@dataclass
class OpenCodeDispatcher(CliAgentAdapter):
    """Spawns ``opencode run`` sessions (#286).

    The third backend, and the first that is neither ``claude`` nor
    ``codex``. It was chosen over ``aider`` for one reason that decides
    the whole adapter: **the checkpoint is written by the agent, not by
    us.** The prompt tells the agent to run a shell heredoc that writes
    ``state.json``, and the pipeline reads that file — so a harness
    without a working tool-calling loop cannot signal DONE, BLOCKED or
    FAILED at all, whatever else it can do to a file. ``opencode`` has a
    bash tool and a non-interactive ``run`` subcommand; ``aider`` drives
    an edit/commit loop and would need the relay to parse its prose.

    That is also why the model measurements on #286 matter here and not
    as a cosmetic preference: of the local candidates measured on
    alkamax, ``qwen3-coder:30b`` emitted tool calls (3/3) and
    ``qwen2.5-coder:32b`` emitted none (0/3) **while advertising
    ``tools`` in its metadata**. A declared capability is not evidence.
    An adapter is correct or not on its own; whether a given model can
    drive it is a question only a live call answers.

    Everything that is not argv and environment — the timeout, the kill
    on cancel, the decode, the checkpoint read, #204's outcome report on
    every exit path — comes from :class:`CliAgentAdapter`, not from a
    second copy here.

    Two deliberate differences from :class:`ClaudeDispatcher`:

    - **The usage store is never written.** A self-hosted model has no
      subscription window to read. Recording "unknown" after every run
      would erase a reading the claude backend legitimately holds and
      un-pause a machine that is actually at its limit, so this backend
      reports nothing rather than reporting ignorance.
    - **``sandbox: "bwrap"`` is refused, not ignored.** That confinement
      binds ``~/.claude/*`` writable and copies ``~/.claude.json``: it
      is written for one CLI. Accepting the setting and running
      unconfined would be a security guard that silently stopped
      applying, which is worse than a daemon that will not start.
    """

    agent_type: ClassVar[str] = "opencode"
    plugin_id: ClassVar[str] = "opencode"
    capabilities: ClassVar[AgentCapabilities] = AgentCapabilities(
        resume=True, structured_events=False, usage=False, sandbox=False,
    )
    instructions_files: ClassVar[tuple[str, ...]] = ("CLAUDE.md", "AGENTS.md")

    # Its own binary, NOT ``agent.binary``. Most deployed configs set
    # ``agent.binary: "claude"`` explicitly, so reading that field here
    # would exec `claude` with opencode's argv and blame the model.
    opencode_binary: str = "opencode"
    default_timeout: int = 1800
    # ``provider/model`` as opencode spells it, e.g.
    # ``local/qwen3-coder:30b`` against a provider whose ``baseURL``
    # points at the self-hosted OpenAI-compatible endpoint. ``None``
    # leaves the choice to opencode's own config, which is the right
    # default: the endpoint and its credentials live there, not here.
    model: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    # Provider variables this backend may receive (#390), from
    # ``agent.opencode.env_allow``: whatever its opencode.json reads.
    env_allow: tuple[str, ...] = ()
    outcome_hook: DispatchOutcomeHook | None = None
    # Accepted so the shape matches `agent.sandbox`, and validated to
    # "none". See the class docstring: silently ignoring "bwrap" is the
    # failure this rejects.
    sandbox: str = "none"
    # Required keyword for the same reason as on `ClaudeDispatcher`
    # (#247): a construction has to say what it does about usage rather
    # than get an answer by omission. Here the answer is "nothing", and
    # it is held only so the routing factory can hand every backend the
    # same store without special-casing.
    usage_store: UsageStore | None = field(kw_only=True)
    active: int = field(default=0, init=False)
    started: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        # Anchor the bare default exactly as the claude adapter does, and
        # refuse relative paths for the same reason: the child would resolve
        # them against the worktree, so the pinned file could differ.
        if self.opencode_binary == "opencode":
            self.opencode_binary = resolve_binary(
                "opencode", logger=_logger, event_prefix="dispatcher.binary"
            )
        self.opencode_binary = _normalise_builtin_binary(
            self.opencode_binary, label="agent.opencode.binary",
        )
        if self.sandbox != "none":
            raise ValueError(
                f"agent.sandbox={self.sandbox!r} is not implemented for "
                "agent.type 'opencode'. The bwrap confinement is written "
                "for the claude CLI (it binds ~/.claude/* writable and "
                "copies ~/.claude.json), so running opencode under it "
                "would confine the wrong paths. Running it unconfined "
                "while the config says otherwise is worse, so this is "
                "refused rather than ignored: set agent.sandbox: \"none\" "
                "to use the opencode backend. Credential stripping (#219) "
                "applies to it either way."
            )

    @property
    def agent_binary(self) -> str:
        return self.opencode_binary

    def _parse_agent_session_id(self, stdout_text: str) -> str | None:
        return _extract_opencode_session_id(stdout_text)

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
        budget_scope: BudgetScope | None = None,
        resume_requested: bool = False,
    ) -> SessionResult:
        """Spawn an ``opencode run`` session and wait for completion.

        ``github_access`` means the same thing it means everywhere else
        (#219) and is enforced the same way: False strips every GitHub
        credential from the child through
        :func:`ctrlrelay.core.agent_env.build_agent_env`. That guard is
        not claude-specific and a new backend inheriting the operator's
        token would be a credential regression wearing a feature's
        clothes.
        """
        if resume_session_id is not None and not native_session_id_is_safe(
            resume_session_id,
        ):
            raise ValueError("unsafe native agent session id")
        timeout = timeout or self.default_timeout

        ident: dict[str, Any] = {
            "session_id": session_id,
            "repo": repo,
            "issue_number": issue_number,
        }

        env, stripped, private_root = self._session_environment(
            session_id, state_file, github_access
        )

        log_event(
            _logger,
            "dispatcher.session.credentials",
            **ident,
            github_access=github_access,
            stripped_env=stripped,
            # Named rather than omitted: this backend cannot be
            # sandboxed yet, and a reader of the log should not have to
            # infer that from a missing field.
            sandbox="none",
        )

        cmd = [self.opencode_binary, "run"]
        # `--format json` is the only output that carries the session id
        # the resume needs. `--dangerously-skip-permissions` approves the
        # tool calls an unattended run has nobody to answer for; without
        # it `opencode run` auto-REJECTS every permission request, so the
        # agent could not even write its checkpoint. The guard is that
        # the process holds no GitHub credential, not what it may type —
        # the same reasoning as the claude adapter's flag of that name.
        #
        # Not `--auto`, although it means the same thing: `run` gained
        # `--auto` only in 1.17.12, and its parser is strict, so on any
        # earlier opencode `--auto` is "Unknown argument" and every
        # dispatch dies before the prompt is read. This spelling is
        # accepted from 1.4 through 2.x (a hidden alias of `--auto`
        # since 1.17.12). The option lists it was checked against are
        # in tests/test_opencode_adapter.py.
        cmd += ["--format", "json", "--dangerously-skip-permissions"]
        if self.model:
            cmd += ["--model", self.model]
        if resume_session_id:
            cmd += ["--session", resume_session_id]
        # LAST, and nothing may be appended after it: `opencode run
        # [message..]` takes the message as a variadic positional, so a
        # flag added below this line would be eaten as prompt text.
        cmd.append(prompt)

        self.active += 1
        self.started += 1
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
                budget_scope=budget_scope,
            )
        finally:
            self.active -= 1
            if private_root is not None:
                shutil.rmtree(private_root, ignore_errors=True)


def _route_key(repo: str | None) -> str:
    """The repo name as a routing key.

    Lower-cased because GitHub treats ``owner/repo`` case-insensitively
    and the config, an issue payload and ``gh`` output do not reliably
    agree on the case. A route that depends on which of them reached us
    works until the day it does not, and the symptom would be one repo
    quietly running on the other backend.
    """
    return (repo or "").strip().lower()


@dataclass
class RepoAgentRouter:
    """Sends each repo's sessions to the backend configured for it.

    #286 asks for an **opt-in, per-repo** backend with ``claude`` the
    default, not a global flip: ctrlrelay runs unattended over many
    repositories and opens pull requests, and a weaker model behind the
    whole daemon produces bad pull requests at machine speed with nobody
    watching.

    It routes on the ``repo`` argument ``spawn_session`` already
    receives rather than being selected at the call sites. ``cli.py``
    passes ``dispatcher=dispatcher`` eight times and the three pipelines
    pass it on twice each — the poller's dev and task lanes, the
    one-shot commands, the pending-resume paths, the scheduled secops
    sweep, the pull-request watcher's fix rounds. One of those left
    unconverted is a repo running on a backend nobody chose, which is
    exactly the class of defect that stays invisible until it writes a
    pull request. Routing in one place means there is nothing to miss.

    An unroutable spawn goes to ``default``. That direction is
    deliberate: ``repo`` was observability-only before this, so a caller
    may legitimately omit it, and the safe answer to "I do not know
    which repo this is" is the default backend rather than a local
    model.
    """

    default: AgentAdapter
    # Keyed by :func:`_route_key`. One adapter per backend, shared by
    # every repo that names it: each holds the session counters the idle
    # usage probe reads, and a copy per repo would see only its own runs.
    by_repo: dict[str, AgentAdapter] = field(default_factory=dict)
    # Every backend built at startup: the default, repo overrides and
    # assignable types. Other enrolled plugins join on first selection.
    by_type: dict[str, AgentAdapter] = field(default_factory=dict)
    # Explicit assignment overrides are denied by default. A pinned resume
    # is not a new override and does not need to be in this set.
    assignable_types: frozenset[str] = frozenset()
    # Built only if a persisted run needs that backend again. Not counted as
    # actively routed until selected, so the idle subscription probe is honest.
    resume_factories: dict[str, Callable[[], AgentAdapter]] = field(
        default_factory=dict,
    )

    def __post_init__(self) -> None:
        # Normalised HERE rather than trusted from the caller. The
        # lookup uses `_route_key`, so a key that was not passed through
        # it is a route that silently never matches — and the symptom is
        # a repo running on the default backend, which looks exactly
        # like a repo that was never configured.
        self.by_repo = {_route_key(k): v for k, v in self.by_repo.items()}
        enrolled = dict(self.by_type)
        for backend in [self.default, *self.by_repo.values()]:
            agent_type = getattr(backend, "agent_type", None)
            if isinstance(agent_type, str):
                enrolled.setdefault(agent_type, backend)
        self.by_type = enrolled
        self.assignable_types = frozenset(self.assignable_types)

    def _backend(self, repo: str | None) -> AgentAdapter:
        return self.by_repo.get(_route_key(repo), self.default)

    def select(
        self,
        repo: str | None,
        *,
        requested_type: str | None = None,
        pinned: AgentIdentity | None = None,
    ) -> AgentSelection:
        """Resolve one run without re-routing its later fix/resume rounds."""
        if pinned is not None and requested_type not in (None, pinned.agent_type):
            raise AgentIdentityConflictError(
                f"requested agent {requested_type!r} conflicts with the pinned "
                f"agent {pinned.agent_type!r}"
            )
        if pinned is not None:
            backend = self.by_type.get(pinned.agent_type)
            if backend is None and pinned.agent_type in self.resume_factories:
                backend = _build_lazily(
                    pinned.agent_type, self.resume_factories[pinned.agent_type],
                )
                self.by_type[pinned.agent_type] = backend
            if backend is None:
                raise AgentSelectionError(
                    f"pinned agent {pinned.agent_type!r} is not enrolled on this node"
                )
            identity = agent_identity_for(backend)
            _ensure_identity_pin(pinned, identity)
            return AgentSelection(backend, identity)
        if requested_type is not None:
            if (
                requested_type not in self.by_type
                and requested_type not in self.resume_factories
            ):
                raise AgentSelectionError(
                    f"requested agent {requested_type!r} is not enrolled on this node"
                )
            if requested_type not in self.assignable_types:
                raise AgentSelectionError(
                    f"requested agent {requested_type!r} is not in "
                    "agent.assignable_types"
                )
            backend = self.by_type.get(requested_type)
            if backend is None and requested_type in self.resume_factories:
                backend = _build_lazily(
                    requested_type, self.resume_factories[requested_type],
                )
                self.by_type[requested_type] = backend
            if backend is None:
                raise AgentSelectionError(
                    f"requested agent {requested_type!r} is not enrolled on this node"
                )
            return AgentSelection(backend, agent_identity_for(backend))
        backend = self._backend(repo)
        return AgentSelection(backend, agent_identity_for(backend))

    @property
    def _backends(self) -> list[AgentAdapter]:
        """``default`` first, then each distinct override. Order matters
        for :attr:`claude_binary`: the default backend's answer wins."""
        seen: list[AgentAdapter] = [self.default]
        for backend in [*self.by_repo.values(), *self.by_type.values()]:
            if not any(backend is s for s in seen):
                seen.append(backend)
        return seen

    # --- what the daemon's idle usage probe reads off its dispatcher ---
    #
    # `cli.py` asks for `active`, `started` and `claude_binary` with
    # `getattr(..., default)`. Answering none of them would turn the
    # probe off silently; answering only for `default` would fire a
    # subscription call in the middle of another backend's run, which is
    # the reading that then gets written over the run's own.

    @property
    def active(self) -> int:
        return sum(getattr(b, "active", 0) for b in self._backends)

    @property
    def started(self) -> int:
        return sum(getattr(b, "started", 0) for b in self._backends)

    @property
    def claude_binary(self) -> str | None:
        """The claude CLI, if any backend here is claude.

        ``None`` for a deployment with no claude backend: there is then
        no subscription to read, and the probe skips on a falsy value.
        That is the honest answer rather than a path that does not run.
        """
        for backend in self._backends:
            binary = getattr(backend, "claude_binary", None)
            if binary:
                return str(binary)
        return None

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
        budget_scope: BudgetScope | None = None,
        resume_requested: bool = False,
    ) -> SessionResult:
        backend = self._backend(repo)
        if backend is not self.default:
            log_event(
                _logger,
                "dispatcher.session.routed",
                session_id=session_id,
                repo=repo,
                issue_number=issue_number,
                agent_type=getattr(backend, "agent_type", "unknown"),
            )
        # Every argument by keyword, so adding one to the protocol
        # cannot silently shift a positional here.
        return await backend.spawn_session(
            session_id=session_id,
            prompt=prompt,
            working_dir=working_dir,
            state_file=state_file,
            timeout=timeout,
            resume_session_id=resume_session_id,
            repo=repo,
            issue_number=issue_number,
            github_access=github_access,
            budget_scope=budget_scope,
            resume_requested=resume_requested,
        )


def _build_lazily(
    agent_type: str, factory: Callable[[], AgentAdapter],
) -> AgentAdapter:
    """Build an enrolled backend on first use, refusing cleanly if it can't run.

    A lazily built backend was never checked at startup, so a sandbox it
    does not support or a binary that no longer resolves surfaces here.
    That is a refusal for this run, never an unhandled error in a pipeline.
    """
    try:
        return factory()
    except (ValueError, NotImplementedError, OSError) as e:
        raise AgentSelectionError(
            f"agent {agent_type!r} cannot be built on this node: {e}"
        ) from e


def agent_identity_for(dispatcher: AgentAdapter) -> AgentIdentity:
    """Read a concrete adapter identity, including lightweight test doubles."""
    identity = getattr(dispatcher, "agent_identity", None)
    if isinstance(identity, AgentIdentity):
        return identity
    agent_type = getattr(dispatcher, "agent_type", "unknown")
    if not isinstance(agent_type, str) or not agent_type:
        agent_type = "unknown"
    return AgentIdentity(agent_type=agent_type, plugin_id=agent_type)


def _ensure_identity_pin(pinned: AgentIdentity, actual: AgentIdentity) -> None:
    differences = [
        field_name
        for field_name in (
            "agent_type", "plugin_id", "plugin_version", "model", "provider",
            "fingerprint",
        )
        if getattr(pinned, field_name) != getattr(actual, field_name)
    ]
    if differences:
        raise AgentIdentityConflictError(
            "pinned agent identity no longer matches the enrolled plugin: "
            + ", ".join(differences)
        )


def select_agent(
    dispatcher: AgentAdapter,
    *,
    repo: str | None,
    requested_type: str | None = None,
    pinned: AgentIdentity | None = None,
) -> AgentSelection:
    """Select a concrete plugin once at a run or resume boundary.

    Standalone callers omit ``requested_type`` and keep the existing
    repo/global defaults. A future assignment may request only an enrolled
    and allowlisted type; it cannot supply a binary or argv.
    """
    if isinstance(dispatcher, RepoAgentRouter):
        return dispatcher.select(
            repo, requested_type=requested_type, pinned=pinned,
        )
    identity = agent_identity_for(dispatcher)
    if pinned is not None and requested_type not in (None, pinned.agent_type):
        raise AgentIdentityConflictError(
            f"requested agent {requested_type!r} conflicts with the pinned "
            f"agent {pinned.agent_type!r}"
        )
    if pinned is not None:
        if pinned.agent_type != identity.agent_type:
            factory = getattr(dispatcher, "resume_factories", {}).get(
                pinned.agent_type,
            )
            if factory is not None:
                backend = _build_lazily(pinned.agent_type, factory)
                actual = agent_identity_for(backend)
                _ensure_identity_pin(pinned, actual)
                return AgentSelection(backend, actual)
        _ensure_identity_pin(pinned, identity)
        return AgentSelection(dispatcher, identity)
    if requested_type is not None:
        if requested_type != identity.agent_type:
            raise AgentSelectionError(
                f"requested agent {requested_type!r} is not enrolled on this node; "
                f"only {identity.agent_type!r} is available"
            )
        raise AgentSelectionError(
            "explicit agent selection requires a router with "
            "agent.assignable_types policy"
        )
    return AgentSelection(dispatcher, identity)


def enrolled_agent_types(dispatcher: AgentAdapter) -> list[str]:
    """Every agent type this node could build for a run, sorted.

    Built backends and ones that are only buildable on demand both count.
    Enrolment is a compatibility promise, which is a different question
    from ``agent.assignable_types`` — that governs what an assignment
    arriving from outside this machine may ask for.
    """
    types: set[str] = set(getattr(dispatcher, "by_type", None) or {})
    types |= set(getattr(dispatcher, "resume_factories", None) or {})
    own = getattr(dispatcher, "agent_type", None)
    if isinstance(own, str) and own:
        types.add(own)
    return sorted(types)


def select_enrolled_agent(
    dispatcher: AgentAdapter, agent_type: str
) -> AgentSelection:
    """One enrolled backend by type, ignoring ``agent.assignable_types`` (#381).

    :func:`select_agent` is the *assignment* seam: a requested type must
    also be allowlisted, because the request can come from an issue or a
    control plane rather than from this machine. This is the *operator*
    seam, for a command run on the box by somebody who already holds its
    config, so it asks only whether the type is enrolled here. Keeping the
    two apart is the point: an operator proof that quietly needed an
    allowlist entry would push operators to widen the allowlist, which is
    the one thing it exists to keep narrow.

    Raises :class:`AgentSelectionError` naming the enrolled types when
    ``agent_type`` is not one of them, and when an enrolled backend cannot
    be built on this node.
    """
    backend: AgentAdapter | None = (
        getattr(dispatcher, "by_type", None) or {}
    ).get(agent_type)
    if backend is None and getattr(dispatcher, "agent_type", None) == agent_type:
        # A single-backend deployment keeps its bare-adapter shape, so the
        # default backend is not in any `by_type` mapping.
        backend = dispatcher
    if backend is None:
        factory = (getattr(dispatcher, "resume_factories", None) or {}).get(
            agent_type,
        )
        if factory is not None:
            # Not cached into `by_type`, unlike `RepoAgentRouter.select`:
            # that caches so a long-lived daemon shares one adapter's
            # session counters, and an operator command makes exactly one
            # selection per process. Caching here would instead enrol a
            # backend on a running daemon's router that nothing routed to.
            backend = _build_lazily(agent_type, factory)
    if backend is None:
        enrolled = enrolled_agent_types(dispatcher)
        raise AgentSelectionError(
            f"agent {agent_type!r} is not enrolled on this node; enrolled: "
            + (", ".join(enrolled) if enrolled else "none")
        )
    return AgentSelection(backend, agent_identity_for(backend))


def _elapsed_ms(started: float) -> int:
    """Milliseconds since ``started`` (a :func:`time.monotonic` reading)."""
    return int((time.monotonic() - started) * 1000)


def _extract_agent_session_id(stdout_text: str) -> str | None:
    """The agent's session UUID, required by ``--resume``.

    Read from the stream-json lines (#247), with the single object of the
    older ``--output-format json`` still accepted. ``None`` when nothing
    carries one (errors, interrupted runs), and the caller decides how to
    fall back.
    """
    return parse_agent_session_id(stdout_text)


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
        budget_scope: BudgetScope | None = None,
        resume_requested: bool = False,
    ) -> SessionResult:
        ...


def make_agent_dispatcher(
    agent_config: "AgentConfig",
    *,
    outcome_hook: DispatchOutcomeHook | None,
    usage_store: UsageStore | None,
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

    ``usage_store`` is required for the same reason (#247): a run that
    keeps no usage reading leaves the page showing an older number, or
    unknown, and the pause before the limit blind.
    """
    adapter = _make_adapter(
        agent_config, outcome_hook=outcome_hook, usage_store=usage_store,
    )
    from ctrlrelay.core.config import BudgetConfig

    budget = getattr(agent_config, "budget", None)
    # Only a validated block: anything else would compare as a limit.
    adapter.budget = budget if isinstance(budget, BudgetConfig) else None
    return adapter


def _make_adapter(
    agent_config: "AgentConfig",
    *,
    outcome_hook: DispatchOutcomeHook | None,
    usage_store: UsageStore | None,
) -> CliAgentAdapter:
    t = agent_config.type
    if t == "claude":
        return ClaudeDispatcher(
            claude_binary=agent_config.binary,
            default_timeout=agent_config.default_timeout_seconds,
            outcome_hook=outcome_hook,
            sandbox=agent_config.sandbox,
            sandbox_hide_paths=tuple(agent_config.sandbox_hide_paths),
            sandbox_writable_paths=tuple(agent_config.sandbox_writable_paths),
            sandbox_readonly_paths=tuple(agent_config.sandbox_readonly_paths),
            sandbox_cache_dir=agent_config.sandbox_cache_dir,
            usage_store=usage_store,
        )
    if t == "opencode":
        oc = getattr(agent_config, "opencode", None)
        return OpenCodeDispatcher(
            # From the opencode block, never from `agent.binary`: that
            # field is claude's and most deployed configs set it to the
            # literal "claude", so reading it here would exec the wrong
            # program and the failure would look like the model's.
            opencode_binary=(
                oc.binary if oc is not None else "opencode"
            ),
            model=oc.model if oc is not None else None,
            env_allow=tuple(oc.env_allow) if oc is not None else (),
            default_timeout=(
                oc.default_timeout_seconds
                if oc is not None and oc.default_timeout_seconds is not None
                else agent_config.default_timeout_seconds
            ),
            outcome_hook=outcome_hook,
            # Passed through rather than defaulted to "none" so the
            # adapter can REFUSE a bwrap config instead of quietly
            # running unconfined under one.
            sandbox=agent_config.sandbox,
            usage_store=usage_store,
        )
    manifest = getattr(agent_config, "plugins", {}).get(t)
    if manifest is None and t in PRESET_NAMES:
        # A built-in preset (#384): a manifest built in code, so its safe
        # arguments cannot be dropped by a hand-written config.
        manifest = preset_manifest(
            t, getattr(agent_config, "presets", {}).get(t),
        )
    if manifest is not None:
        from ctrlrelay.core.generic_cli import GenericCliDispatcher

        return GenericCliDispatcher(
            agent_type=t,
            binary=manifest.binary,
            args=tuple(manifest.args),
            prompt_mode=manifest.prompt_mode,
            resume_args=(
                tuple(manifest.resume_args)
                if manifest.resume_args is not None else None
            ),
            session_id_path=manifest.session_id_path,
            default_timeout=(
                manifest.default_timeout_seconds
                if manifest.default_timeout_seconds is not None
                else agent_config.default_timeout_seconds
            ),
            model=manifest.model,
            provider=manifest.provider,
            plugin_version=manifest.plugin_version,
            instructions_files=tuple(manifest.instructions_files),
            env_allow=tuple(manifest.env_allow),
            usage=manifest.usage,
            outcome_hook=outcome_hook,
            sandbox=agent_config.sandbox,
            usage_store=usage_store,
        )
    supported = [
        "claude", "opencode",
        *sorted({*PRESET_NAMES, *getattr(agent_config, "plugins", {})}),
    ]
    raise NotImplementedError(
        f"agent.type={t!r} has no adapter yet. Implement one that "
        "satisfies the AgentAdapter protocol in "
        "src/ctrlrelay/core/dispatcher.py and register it here, then "
        f"open a PR. Supported today: {', '.join(repr(x) for x in supported)}."
    )


def make_repo_agent_dispatcher(
    config: "Config",
    *,
    outcome_hook: DispatchOutcomeHook | None,
    usage_store: UsageStore | None,
) -> AgentAdapter:
    """The adapter the daemon and the one-shot commands should use.

    ``agent.type`` is the default for every repo; a repo may name its
    own ``agent_type`` to opt into another backend. #286's reason for
    the per-repo form rather than a global switch: ctrlrelay runs
    unattended over issue and pull-request text from many repositories
    and opens pull requests, so a weaker model behind the whole daemon
    produces bad pull requests at machine speed with nobody watching.
    Low-stakes repos can opt in; ``claude`` stays the default
    everywhere else.

    With no repo overriding anything this returns the plain adapter —
    not a router wrapping it. An existing ``claude`` deployment then
    gets back exactly the object it got before this change, which is the
    cheapest way to keep "unaffected" true rather than merely intended.

    An unknown per-repo type raises :class:`NotImplementedError` naming
    the repo, for the same reason ``agent.type`` does: a typo must stop
    the daemon at startup rather than silently leave that repo on the
    default.
    """
    default = make_agent_dispatcher(
        config.agent, outcome_hook=outcome_hook, usage_store=usage_store
    )
    global_type = config.agent.type
    by_type: dict[str, AgentAdapter] = {global_type: default}
    by_repo: dict[str, AgentAdapter] = {}

    requested = [(repo.name, getattr(repo, "agent_type", None)) for repo in config.repos]
    # Built now, so a routed type that cannot run here (a sandbox it does
    # not support, a binary that does not resolve) stops startup by name.
    requested += [
        ("agent.assignable_types", wanted)
        for wanted in getattr(config.agent, "assignable_types", [])
    ]
    for repo_name, wanted in requested:
        if not wanted:
            continue
        if wanted not in by_type:
            per_repo_config = config.agent.model_copy(update={"type": wanted})
            try:
                by_type[wanted] = make_agent_dispatcher(
                    per_repo_config,
                    outcome_hook=outcome_hook,
                    usage_store=usage_store,
                )
            except NotImplementedError as e:
                raise NotImplementedError(f"{repo_name}: {e}") from e
            except ValueError as e:
                raise ValueError(f"{repo_name}: {e}") from e
        if repo_name != "agent.assignable_types" and wanted != global_type:
            by_repo[_route_key(repo_name)] = by_type[wanted]

    # Every other enrolled backend is built only if a run selects it again
    # (a pinned resume after an allowlist or repo change). Being enrolled is
    # a compatibility promise, not a routing one, so a listed plugin that
    # cannot run under this node's sandbox must not stop the daemon.
    resume_factories: dict[str, Callable[[], AgentAdapter]] = {}
    for wanted in ("claude", "opencode", *sorted(PRESET_NAMES), *config.agent.plugins):
        if wanted in by_type:
            continue

        def resume_factory(wanted: str = wanted) -> AgentAdapter:
            return make_agent_dispatcher(
                config.agent.model_copy(update={"type": wanted}),
                outcome_hook=outcome_hook,
                usage_store=usage_store,
            )

        resume_factories[wanted] = resume_factory
    # A single-backend deployment keeps its historical bare-adapter shape while
    # retaining a way to reconstruct an older pinned backend if it ever resumes.
    default.resume_factories = resume_factories

    if not by_repo and len(by_type) == 1 and not config.agent.assignable_types:
        return default
    return RepoAgentRouter(
        default=default,
        by_repo=by_repo,
        by_type=by_type,
        assignable_types=frozenset(config.agent.assignable_types),
        resume_factories=resume_factories,
    )
