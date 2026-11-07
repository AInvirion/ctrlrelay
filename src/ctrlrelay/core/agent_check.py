"""``ctrlrelay agent check``: prove one backend writes its checkpoint (#381).

A generic CLI is pluggable (#380) long before it is usable. What makes a
run countable is not that the CLI starts, but that it writes the ``DONE`` /
``BLOCKED_NEEDS_INPUT`` / ``FAILED`` checkpoint to ``CTRLRELAY_STATE_FILE``
by running the shell snippet the prompt gives it. Exiting zero is not
success, and before this the only way to find out which of those a backend
does was to start a full dev run against a real repository and read the
failure afterwards.

So this runs the one thing that has to work, on nothing that matters: a
throwaway git repository in a temporary directory, one session, a prompt
whose entire content is the checkpoint snippet, and a report that names
each observation separately — the exit code, the checkpoint, the native
session id, the identity, the elapsed time. They are reported apart
because they fail apart: a CLI that exits 0 and writes nothing, and one
that writes the checkpoint but reports no session id, are different
defects with different fixes, and a single pass/fail hides which one you
have.
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ctrlrelay.core.dispatcher import (
    SessionResult,
    make_repo_agent_dispatcher,
    select_enrolled_agent,
)
from ctrlrelay.core.obs import get_logger, log_event

if TYPE_CHECKING:
    from ctrlrelay.core.agent_plugins import AgentIdentity
    from ctrlrelay.core.config import Config

_logger = get_logger("core.agent_check")

# Long enough for a cold CLI start on a hosted model, short enough that an
# operator waits for it rather than backgrounding it.
DEFAULT_TIMEOUT_SECONDS = 300

# How much of a fingerprint is worth printing: enough to tell two
# manifests apart by eye, not enough to pretend it is the whole pin.
FINGERPRINT_PREFIX_CHARS = 12

# What the report says when nothing was written, and when something was
# written that could not be parsed as a checkpoint. They are separate
# because a file that exists and does not parse is a bug in what the agent
# wrote; an absent file is a bug in whether it wrote at all.
NO_CHECKPOINT = "none"
UNREADABLE_CHECKPOINT = "unreadable"

_INSTRUCTION = "Run this exact shell command, then stop."


class AgentCheckError(RuntimeError):
    """The check could not be run at all.

    An unknown agent type, a backend this node cannot build, or a
    temporary git repository that could not be created. Distinct from a
    check that ran and found the backend wanting — that comes back as a
    report.
    """


def check_prompt(state_file_path: str, session_id: str) -> str:
    """The whole prompt: the instruction, then the ``DONE`` snippet.

    The same shape as the dev pipeline's contract
    (``DevPipeline._checkpoint_contract``) rather than a shared block with
    it, for the reason that contract gives for not sharing: the dev DONE
    block carries the pull-request outputs the relay opens a PR from, and
    the three pipeline contracts differ in substance. What this check
    needs is the *mechanics* — ``mkdir -p --``, a quoted path, a heredoc
    into ``python3`` — because those mechanics are what a backend either
    can or cannot carry out.

    Paths are shell-quoted here for #191's reason: an apostrophe in a
    hand-quoted path either closed the quote early, so no checkpoint was
    written at all, or paired up and wrote to a different file.
    """
    quoted_path = shlex.quote(state_file_path)
    quoted_dir = shlex.quote(str(Path(state_file_path).parent))
    return f"""{_INSTRUCTION}

STATE_FILE: {state_file_path}
SESSION_ID: {session_id}

```bash
mkdir -p -- {quoted_dir}
python3 - {quoted_path} <<'CTRLRELAY_DONE'
import datetime, json, sys
state = {{
    "version": "1",
    "status": "DONE",
    "session_id": "{session_id}",
    "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    "summary": "agent check: wrote the checkpoint",
}}
with open(sys.argv[1], "w") as f:
    json.dump(state, f)
CTRLRELAY_DONE
```

Do nothing else. There is no repository to read and nothing to change:
this is a one-minute check that you can write that checkpoint where the
relay will look for it. The same two values are in your environment as
`CTRLRELAY_STATE_FILE` and `CTRLRELAY_SESSION_ID`.
"""


@dataclass(frozen=True)
class AgentCheckReport:
    """One observation per field. ``ok`` is true only for a DONE checkpoint."""

    agent_type: str
    plugin_id: str
    plugin_version: str
    model: str | None
    provider: str | None
    fingerprint: str | None
    exit_code: int
    # A :class:`CheckpointStatus` value, or ``NO_CHECKPOINT`` /
    # ``UNREADABLE_CHECKPOINT``. A string rather than an optional status so
    # "nothing was written" cannot be read as "status unknown".
    checkpoint: str
    agent_session_id: str | None
    elapsed_seconds: float
    timed_out: bool
    stderr_tail: str = ""

    @property
    def checkpoint_written(self) -> bool:
        return self.checkpoint != NO_CHECKPOINT

    @property
    def ok(self) -> bool:
        """Only ``DONE`` counts.

        Not ``exit_code == 0``: the whole reason this command exists is
        that a CLI can exit 0 having written nothing.
        """
        return self.checkpoint == "DONE"

    @property
    def fingerprint_prefix(self) -> str | None:
        if not self.fingerprint:
            return None
        return self.fingerprint[:FINGERPRINT_PREFIX_CHARS]

    def checkpoint_line(self) -> str:
        if self.checkpoint == NO_CHECKPOINT:
            return "checkpoint: no checkpoint — the agent wrote nothing"
        if self.checkpoint == UNREADABLE_CHECKPOINT:
            return (
                "checkpoint: written, but it could not be parsed as a checkpoint"
            )
        return f"checkpoint: written, status {self.checkpoint}"

    def lines(self) -> list[str]:
        """The report an operator reads, one observation per line."""
        exit_line = f"exit code: {self.exit_code}"
        if self.timed_out:
            exit_line += f" (the relay stopped it at {self.elapsed_seconds:.0f}s)"
        identity = f"agent: type={self.agent_type} plugin={self.plugin_id}"
        if self.model:
            identity += f" model={self.model}"
        if self.provider:
            identity += f" provider={self.provider}"
        identity += f" fingerprint={self.fingerprint_prefix or 'none'}"
        return [
            exit_line,
            self.checkpoint_line(),
            f"native session id: {self.agent_session_id or 'none'}",
            identity,
            f"elapsed: {self.elapsed_seconds:.1f}s",
        ]

    def as_dict(self) -> dict[str, Any]:
        """The same report as one JSON-serialisable object."""
        return {
            "ok": self.ok,
            "agent_type": self.agent_type,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "model": self.model,
            "provider": self.provider,
            "fingerprint": self.fingerprint,
            "fingerprint_prefix": self.fingerprint_prefix,
            "exit_code": self.exit_code,
            "checkpoint_written": self.checkpoint_written,
            "checkpoint": self.checkpoint,
            "agent_session_id": self.agent_session_id,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "timed_out": self.timed_out,
            "stderr_tail": self.stderr_tail,
        }


def _git(cwd: Path, *args: str) -> None:
    """Run one git command in the throwaway repo, or raise naming it.

    The operator's own git configuration is cut out with
    ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM``: a global commit template,
    ``core.hooksPath`` or an init template directory would otherwise make
    this fail on their machine only, and the failure would read as the
    backend's rather than as their git config.
    """
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise AgentCheckError(
            f"could not prepare the throwaway repository: git "
            f"{' '.join(args)} exited {proc.returncode}: {detail}"
        )


def make_throwaway_repo(root: Path) -> Path:
    """A git repository with one commit and a ``README.md``, under ``root``.

    A real repository rather than a bare directory, because a coding CLI
    may refuse to run outside one, and because the relay always hands the
    agent a worktree. A throwaway one rather than the repo the operator
    happens to be standing in: the agent runs unconfined on most generic
    plugins today, and a check must not be able to touch work in progress.
    """
    repo = root / "repo"
    repo.mkdir()
    (repo / "README.md").write_text(
        "Throwaway repository for `ctrlrelay agent check`.\n"
        "\n"
        "Created in a temporary directory and deleted when the check ends.\n"
    )
    _git(repo, "-c", "init.defaultBranch=main", "init", "-q")
    _git(repo, "add", "--", "README.md")
    _git(
        repo,
        "-c", "user.name=ctrlrelay",
        "-c", "user.email=ctrlrelay@localhost",
        "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "init",
    )
    return repo


def _report(
    identity: "AgentIdentity",
    result: SessionResult,
    state_file: Path,
    elapsed: float,
) -> AgentCheckReport:
    """Read one :class:`SessionResult` into a report.

    ``result.state`` is ``None`` both when nothing was written and when
    what was written would not parse — the dispatcher logs the difference
    and collapses it. The file itself tells them apart, because a
    checkpoint that parsed was deleted as it was read, and one that did
    not is still on disk.
    """
    if result.state is not None:
        checkpoint = result.state.status.value
    elif state_file.exists():
        checkpoint = UNREADABLE_CHECKPOINT
    else:
        checkpoint = NO_CHECKPOINT
    return AgentCheckReport(
        agent_type=identity.agent_type,
        plugin_id=identity.plugin_id,
        plugin_version=identity.plugin_version,
        model=identity.model,
        provider=identity.provider,
        fingerprint=identity.fingerprint,
        exit_code=result.exit_code,
        checkpoint=checkpoint,
        agent_session_id=result.agent_session_id,
        elapsed_seconds=elapsed,
        timed_out=result.timed_out,
        stderr_tail=(result.stderr or "")[-500:],
    )


async def run_agent_check(
    config: "Config",
    agent_type: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> AgentCheckReport:
    """Spawn one session on ``agent_type`` and report what it did.

    Raises :class:`AgentCheckError` when the backend cannot be selected or
    built, or the throwaway repository cannot be created. Everything the
    session itself does — including exiting zero without a checkpoint —
    comes back as a report.
    """
    try:
        dispatcher = make_repo_agent_dispatcher(
            config, outcome_hook=None, usage_store=None,
        )
        selection = select_enrolled_agent(dispatcher, agent_type)
    except (NotImplementedError, ValueError) as e:
        # `AgentSelectionError` is a `ValueError`, so an unknown type and a
        # deployment whose config does not build both land here naming
        # themselves.
        raise AgentCheckError(str(e)) from e

    root = Path(tempfile.mkdtemp(prefix="ctrlrelay-agent-check-"))
    try:
        repo = make_throwaway_repo(root)
        # Inside the worktree, where the dev pipeline puts it: under
        # `sandbox: "bwrap"` that is the only place the agent can write.
        state_file = repo / ".ctrlrelay" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        session_id = f"check-{agent_type}-{secrets.token_hex(4)}"
        log_event(
            _logger,
            "agent_check.started",
            session_id=session_id,
            agent_type=agent_type,
            timeout=timeout,
        )
        started = time.monotonic()
        result = await selection.dispatcher.spawn_session(
            session_id=session_id,
            prompt=check_prompt(str(state_file), session_id),
            working_dir=repo,
            state_file=state_file,
            timeout=timeout,
            resume_session_id=None,
            repo=None,
            issue_number=None,
            github_access=False,
        )
        elapsed = time.monotonic() - started
        report = _report(selection.identity, result, state_file, elapsed)
        log_event(
            _logger,
            "agent_check.finished",
            session_id=session_id,
            agent_type=agent_type,
            exit_code=report.exit_code,
            checkpoint=report.checkpoint,
            ok=report.ok,
        )
        return report
    finally:
        shutil.rmtree(root, ignore_errors=True)
        if root.exists():
            # Said rather than swallowed: a temporary directory the check
            # could not remove is a path somebody has to delete by hand,
            # and `ignore_errors` alone would leave no trace of it.
            _logger.warning(
                "agent_check.cleanup_failed",
                extra={"path": str(root), "agent_type": agent_type},
            )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "NO_CHECKPOINT",
    "UNREADABLE_CHECKPOINT",
    "AgentCheckError",
    "AgentCheckReport",
    "check_prompt",
    "make_throwaway_repo",
    "run_agent_check",
]
