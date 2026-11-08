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

``--resume`` (#382) asks the next question, which one fresh session cannot
answer: does the backend come back knowing what it was doing? Resume is
the half of the contract every blocked run depends on — a backend that
cannot resume fails every run that has to ask a question — and before
this the only way to exercise it was a real dev run that blocked and
waited for a chat answer. So this runs two rounds against the throwaway
repository: the first is told a random token and writes
``BLOCKED_NEEDS_INPUT``, the second resumes that native session and is
asked for the token back. The token is generated here and is deliberately
absent from the second prompt: without it a second round could answer
from its prompt alone, and the check would pass on a backend that had
started a fresh session instead of resuming.
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ctrlrelay.core.checkpoint import (
    CheckpointError,
    CheckpointState,
    CheckpointStatus,
    read_checkpoint,
)
from ctrlrelay.core.dispatcher import (
    SessionResult,
    make_repo_agent_dispatcher,
    select_enrolled_agent,
)
from ctrlrelay.core.obs import get_logger, log_event

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ctrlrelay.core.agent_plugins import AgentIdentity, AgentSelection
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

# The question round 1 is told to write, and the status each round must
# write. Named because the report, the prompts and the verdict all have to
# agree on them, and a verdict comparing against its own spelling of a
# status is a check that passes on any backend.
RESUME_QUESTION = "What is the token?"
ROUND1_EXPECTED = "BLOCKED_NEEDS_INPUT"
ROUND2_EXPECTED = "DONE"

# 8 hex characters: short enough that a model carries it in context and
# echoes it without transcription errors, long enough that a backend
# cannot produce it by accident. Fresh per run, so a cached transcript
# from an earlier check cannot answer this one.
RESUME_TOKEN_BYTES = 4


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


def resume_round1_prompt(
    state_file_path: str, session_id: str, token: str
) -> str:
    """Round 1: remember ``token``, then write ``BLOCKED_NEEDS_INPUT``.

    The ``printf`` snippet is the shape of
    :func:`ctrlrelay.pipelines.dev._blocked_and_failed_blocks` rather than
    a shared block with it, for the same reason :func:`check_prompt` gives:
    what is under test is the *mechanics* a real run uses — the quoted
    path, the ``date`` substitution, the single line of shell — and the
    dev contract's own blocks carry placeholders a check must not leave in.

    The question is fixed rather than a ``<QUESTION>`` placeholder because
    the relay only has to see that a question arrived; what it says is the
    agent's business on a real run and noise here.
    """
    quoted_path = shlex.quote(state_file_path)
    quoted_dir = shlex.quote(str(Path(state_file_path).parent))
    return f"""Remember the token below. Then run this exact shell command and stop.

STATE_FILE: {state_file_path}
SESSION_ID: {session_id}
TOKEN: {token}

```bash
mkdir -p -- {quoted_dir}
printf '{{"version":"1","status":"{ROUND1_EXPECTED}",'\
'"session_id":"{session_id}","timestamp":"%s",'\
'"question":"{RESUME_QUESTION}"}}' \\
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > {quoted_path}
```

Do nothing else. There is no repository to read and nothing to change.
This is the first half of a two-minute check: the relay will resume this
same session and ask you for the token, so keep it in mind rather than
writing it to a file. The two values above are in your environment as
`CTRLRELAY_STATE_FILE` and `CTRLRELAY_SESSION_ID`.
"""


def resume_round2_prompt(state_file_path: str, session_id: str) -> str:
    """Round 2: write ``DONE`` whose summary is the remembered token.

    The token is not in this prompt, and must never be put in it. A round
    that can read the answer off its own prompt proves nothing about
    whether the session resumed: it would pass just as well against a
    backend that silently started a fresh one.
    """
    quoted_path = shlex.quote(state_file_path)
    quoted_dir = shlex.quote(str(Path(state_file_path).parent))
    return f"""Write a DONE checkpoint whose summary is the token you were asked to
remember. Run this exact shell command with `<TOKEN>` replaced by that
token, then stop.

STATE_FILE: {state_file_path}
SESSION_ID: {session_id}

```bash
mkdir -p -- {quoted_dir}
python3 - {quoted_path} <<'CTRLRELAY_DONE'
import datetime, json, sys
state = {{
    "version": "1",
    "status": "{ROUND2_EXPECTED}",
    "session_id": "{session_id}",
    "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    "summary": "<TOKEN>",
}}
with open(sys.argv[1], "w") as f:
    json.dump(state, f)
CTRLRELAY_DONE
```

Do nothing else. The token is not in this prompt on purpose: whether you
still have it from the earlier turn is the whole of what is being
checked. If you do not have it, write the FAILED checkpoint instead,
with `"error"` saying so — that is a real answer and a guess is not.
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
    # What the checkpoint said, when it parsed: the ``summary`` of a
    # ``DONE``, the ``question`` of a ``BLOCKED_NEEDS_INPUT``, the
    # ``error`` of a ``FAILED``. ``None`` when nothing readable was
    # written. The resume check reads the token out of it (#382), and on
    # any round it is what the operator wants to see next after a status.
    checkpoint_summary: str | None = None
    # The status this round was asked for. ``DONE`` for a single-round
    # check; ``BLOCKED_NEEDS_INPUT`` for the first resume round, where a
    # ``DONE`` is a failure rather than an unexpected success.
    expected_checkpoint: str = "DONE"

    @property
    def checkpoint_written(self) -> bool:
        return self.checkpoint != NO_CHECKPOINT

    @property
    def ok(self) -> bool:
        """Only the expected status, from a session that ended on its own.

        Not ``exit_code == 0``: the whole reason this command exists is
        that a CLI can exit 0 having written nothing. And not a checkpoint
        the relay had to kill the CLI after: a dev run waits for the
        process, so a backend that writes the checkpoint and never exits
        still never finishes a run.
        """
        return self.checkpoint == self.expected_checkpoint and not self.timed_out

    @property
    def checkpoint_phrase(self) -> str:
        """The checkpoint in the middle of a sentence, for a round line."""
        if self.checkpoint == NO_CHECKPOINT:
            return "no checkpoint"
        if self.checkpoint == UNREADABLE_CHECKPOINT:
            return "an unreadable checkpoint"
        return f"checkpoint {self.checkpoint}"

    def round_line(self, label: str) -> str:
        """One round as one line: exit code, checkpoint, native session id.

        The same three observations the five-line report names separately,
        on one line each round, because across two rounds what an operator
        is comparing is the rounds rather than the fields.
        """
        exit_part = f"exit code {self.exit_code}"
        if self.timed_out:
            exit_part += f" (the relay stopped it at {self.elapsed_seconds:.0f}s)"
        return (
            f"{label}: {exit_part}, {self.checkpoint_phrase}, "
            f"native session id {self.agent_session_id or 'none'}"
        )

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
            "checkpoint_summary": self.checkpoint_summary,
            "expected_checkpoint": self.expected_checkpoint,
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
    happens to be standing in, so a backend that follows the prompt has
    nothing of theirs in its working directory. That is a choice of
    working directory, not confinement: without a sandbox the agent runs
    as the operator and can reach anything they can.
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


def _said(state: CheckpointState) -> str | None:
    """What the checkpoint said, from the field its own status puts it in.

    One accessor rather than three call sites, because every caller wants
    "what did it say" and none of them wants to know that a ``DONE`` says
    it in ``summary`` and a ``FAILED`` in ``error``.

    Dispatched on the status rather than written as
    ``summary or question or error``: that shape is #174's precedence bug
    — whichever field happens to be truthy wins, and an agent that filled
    in two of them silently decides which one you read. The status already
    says which field is the answer (the model validator requires it), so
    there is nothing to guess.
    """
    if state.status == CheckpointStatus.BLOCKED_NEEDS_INPUT:
        return state.question
    if state.status == CheckpointStatus.FAILED:
        return state.error
    return state.summary


def _report(
    identity: "AgentIdentity",
    result: SessionResult,
    state_file: Path,
    elapsed: float,
    *,
    expected: str = "DONE",
) -> AgentCheckReport:
    """Read one :class:`SessionResult` into a report.

    ``result.state`` is ``None`` when nothing was written, when what was
    written would not parse, and on a timeout — where the dispatcher
    returns before reading the checkpoint at all. A checkpoint that parsed
    was deleted as it was read, so whatever is left on disk is parsed here:
    a backend that wrote a good ``DONE`` and then hung is a different
    defect from one that wrote rubbish, and the report has to say which.
    """
    if result.state is not None:
        checkpoint = result.state.status.value
        said = _said(result.state)
    else:
        checkpoint, said = _leftover_checkpoint(state_file)
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
        checkpoint_summary=said,
        expected_checkpoint=expected,
    )


def _leftover_checkpoint(state_file: Path) -> tuple[str, str | None]:
    """The status and text of a checkpoint still on disk, or why there is none.

    :func:`read_checkpoint` opens only a small regular file (#394), so a
    FIFO or a huge file the agent left there is refused rather than read.
    ``lstat`` first only to tell "nothing there" from "something there that
    is not a checkpoint", which the report names differently.
    """
    try:
        os.lstat(state_file)
    except FileNotFoundError:
        return NO_CHECKPOINT, None
    except OSError:
        return UNREADABLE_CHECKPOINT, None
    try:
        state = read_checkpoint(state_file)
    except CheckpointError:
        return UNREADABLE_CHECKPOINT, None
    return state.status.value, _said(state)


@dataclass(frozen=True)
class AgentResumeCheckReport:
    """Two rounds, each reported on its own, and why the pair failed (#382).

    ``failures`` is a list rather than a boolean for the same reason the
    single-round report has one field per observation: "resume is broken"
    is not actionable, and the three ways it breaks — round 1 never asked,
    round 1 reported no native session id to resume, round 2 came back
    without the token — have three different fixes.

    ``warnings`` is for what an operator should know and must not fail a
    backend over: a CLI that mints a new native session id per turn has
    still resumed, since the token came back.
    """

    agent_type: str
    token: str
    round1: AgentCheckReport
    # ``None`` when round 1 did not meet its requirement: there was then
    # nothing to resume from, and a second fresh session would have proved
    # nothing while costing a real one.
    round2: AgentCheckReport | None
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    # The fresh session spawned between the rounds with a different token,
    # so a backend that resumes "the latest session" rather than the id it
    # was handed answers with the wrong one. ``None`` when round 1 failed
    # and nothing after it ran.
    decoy: AgentCheckReport | None = None
    decoy_token: str | None = None

    @property
    def rounds_run(self) -> int:
        return 1 if self.round2 is None else 2

    @property
    def ok(self) -> bool:
        return not self.failures and self.round2 is not None

    @property
    def elapsed_seconds(self) -> float:
        total = self.round1.elapsed_seconds
        if self.decoy is not None:
            total += self.decoy.elapsed_seconds
        if self.round2 is not None:
            total += self.round2.elapsed_seconds
        return total

    @property
    def token_echoed(self) -> bool:
        """Did round 2 say the token back?

        Case-insensitive and a substring, not equality: the token is
        unguessable either way, and failing a backend for wrapping it in a
        sentence or upper-casing it would report a formatting habit as a
        broken resume.
        """
        if self.round2 is None:
            return False
        said = self.round2.checkpoint_summary or ""
        return self.token.lower() in said.lower()

    def lines(self) -> list[str]:
        """The report an operator reads: one line per round, then the rest."""
        out = [self.round1.round_line("round 1 (fresh)")]
        if self.decoy is not None:
            out.append(self.decoy.round_line("decoy (fresh)"))
        if self.round2 is None:
            out.append("round 2 (resume): not run")
        else:
            out.append(self.round2.round_line("round 2 (resume)"))
        if self.token_echoed:
            token_line = f"token: {self.token} — round 2 said it back"
        elif self.round2 is None:
            token_line = f"token: {self.token} — never asked for"
        else:
            said = self.round2.checkpoint_summary
            token_line = (
                f"token: {self.token} — round 2 said "
                + (f"{said!r}" if said else "nothing")
            )
        out.append(token_line)
        identity = (
            f"agent: type={self.round1.agent_type} "
            f"plugin={self.round1.plugin_id}"
        )
        if self.round1.model:
            identity += f" model={self.round1.model}"
        if self.round1.provider:
            identity += f" provider={self.round1.provider}"
        identity += f" fingerprint={self.round1.fingerprint_prefix or 'none'}"
        out.append(identity)
        out.append(f"elapsed: {self.elapsed_seconds:.1f}s")
        return out

    def as_dict(self) -> dict[str, Any]:
        """Both rounds in one JSON-serialisable object.

        ``resume: true`` so a script reading the stream can tell a
        two-round report from a one-round one without guessing from which
        keys are present.
        """
        return {
            "ok": self.ok,
            "resume": True,
            "agent_type": self.agent_type,
            "token": self.token,
            "token_echoed": self.token_echoed,
            "rounds_run": self.rounds_run,
            "round1": self.round1.as_dict(),
            "round2": self.round2.as_dict() if self.round2 else None,
            "decoy": self.decoy.as_dict() if self.decoy else None,
            "decoy_token": self.decoy_token,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


def _select(config: "Config", agent_type: str) -> "AgentSelection":
    """The enrolled backend for ``agent_type``, or an `AgentCheckError`."""
    try:
        dispatcher = make_repo_agent_dispatcher(
            config, outcome_hook=None, usage_store=None,
        )
        return select_enrolled_agent(dispatcher, agent_type)
    except (NotImplementedError, ValueError) as e:
        # `AgentSelectionError` is a `ValueError`, so an unknown type and a
        # deployment whose config does not build both land here naming
        # themselves.
        raise AgentCheckError(str(e)) from e


@contextmanager
def _throwaway_run(agent_type: str) -> "Iterator[tuple[Path, Path, str]]":
    """A throwaway repo, its state file and a session id, removed after.

    Shared by the one-round and the resume checks so there is one cleanup
    path rather than two: the second copy is where a temporary directory
    starts being left behind on the exception nobody tested.
    """
    root = Path(tempfile.mkdtemp(prefix="ctrlrelay-agent-check-"))
    try:
        repo = make_throwaway_repo(root)
        # Inside the worktree, where the dev pipeline puts it: under
        # `sandbox: "bwrap"` that is the only place the agent can write.
        state_file = repo / ".ctrlrelay" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        yield repo, state_file, f"check-{agent_type}-{secrets.token_hex(4)}"
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


async def _spawn_round(
    selection: "AgentSelection",
    *,
    session_id: str,
    prompt: str,
    repo: Path,
    state_file: Path,
    timeout: int,
    resume_session_id: str | None = None,
    expected: str = "DONE",
) -> AgentCheckReport:
    """One session against the throwaway repo, read into a report.

    ``resume_session_id`` is passed together with ``resume_requested``
    exactly as the pipelines pass them, so what runs here is the same
    argv a dev run would build — which is the only version of resume
    worth checking.
    """
    started = time.monotonic()
    result = await selection.dispatcher.spawn_session(
        session_id=session_id,
        prompt=prompt,
        working_dir=repo,
        state_file=state_file,
        timeout=timeout,
        resume_session_id=resume_session_id,
        repo=None,
        issue_number=None,
        github_access=False,
        resume_requested=resume_session_id is not None,
    )
    elapsed = time.monotonic() - started
    return _report(
        selection.identity, result, state_file, elapsed, expected=expected,
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
    selection = _select(config, agent_type)
    with _throwaway_run(agent_type) as (repo, state_file, session_id):
        log_event(
            _logger,
            "agent_check.started",
            session_id=session_id,
            agent_type=agent_type,
            timeout=timeout,
        )
        report = await _spawn_round(
            selection,
            session_id=session_id,
            prompt=check_prompt(str(state_file), session_id),
            repo=repo,
            state_file=state_file,
            timeout=timeout,
        )
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


async def run_agent_resume_check(
    config: "Config",
    agent_type: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> AgentResumeCheckReport:
    """Two rounds on ``agent_type``: block on a question, then resume (#382).

    ``timeout`` applies to each round, as it does to each spawn of a real
    run. Raises :class:`AgentCheckError` when the backend cannot be
    selected, when it does not report ``capabilities.resume`` — refused
    before anything is spawned, since the manifest already answers it —
    and when the resume could not be built at all. What either session
    does comes back as a report.
    """
    selection = _select(config, agent_type)
    if not selection.dispatcher.capabilities.resume:
        raise AgentCheckError(
            f"resume not configured for {agent_type}: the backend reports no "
            "resume capability, so there is nothing to check. A generic "
            "plugin needs both resume_args and session_id_path."
        )

    token = secrets.token_hex(RESUME_TOKEN_BYTES)
    with _throwaway_run(agent_type) as (repo, state_file, session_id):
        log_event(
            _logger,
            "agent_check.resume.started",
            session_id=session_id,
            agent_type=agent_type,
            timeout=timeout,
        )
        round1 = await _spawn_round(
            selection,
            session_id=session_id,
            prompt=resume_round1_prompt(str(state_file), session_id, token),
            repo=repo,
            state_file=state_file,
            timeout=timeout,
            expected=ROUND1_EXPECTED,
        )
        failures = _round1_failures(round1, timeout)
        if failures:
            # Deliberately no second round: with no question asked or no
            # native id captured there is nothing to resume, and spawning
            # a second fresh session would report a pass for a backend
            # that never resumed anything.
            return _finish_resume(
                agent_type, token, round1, None, failures, (), session_id,
            )

        # A decoy: a second fresh session in the same directory, told a
        # different token. Without it a backend that resumes "the most
        # recent session" — a manifest built on `--continue` or
        # `resume --last` — passes, because the only session there is to
        # resume is the right one. On a node where runs interleave that
        # backend resumes somebody else's session. Its own outcome is not
        # judged: it only has to have run, so that "latest" and "the id
        # round 2 is given" are different sessions.
        state_file.unlink(missing_ok=True)
        decoy_token = token
        while decoy_token == token:
            decoy_token = secrets.token_hex(RESUME_TOKEN_BYTES)
        decoy_session_id = f"{session_id}-decoy"
        decoy = await _spawn_round(
            selection,
            session_id=decoy_session_id,
            prompt=resume_round1_prompt(
                str(state_file), decoy_session_id, decoy_token,
            ),
            repo=repo,
            state_file=state_file,
            timeout=timeout,
            expected=ROUND1_EXPECTED,
        )

        # The dispatcher deletes a checkpoint as it reads it, so round 2
        # starts with nothing at that path. Removed again anyway: round 1's
        # file surviving would be read as round 2's answer, which is the
        # one mistake this check cannot afford to make.
        state_file.unlink(missing_ok=True)
        try:
            round2 = await _spawn_round(
                selection,
                session_id=session_id,
                prompt=resume_round2_prompt(str(state_file), session_id),
                repo=repo,
                state_file=state_file,
                timeout=timeout,
                resume_session_id=round1.agent_session_id,
                expected=ROUND2_EXPECTED,
            )
        except ValueError as e:
            # The adapter refused to build a resume argv — a safe native
            # id it would not take, or a mapping the capability flag said
            # was there. A configuration answer, not a verdict on the run.
            raise AgentCheckError(f"round 2 could not be started: {e}") from e

        report = _finish_resume(
            agent_type,
            token,
            round1,
            round2,
            _decoy_failures(round1, decoy)
            + _round2_failures(round2, token, timeout, decoy_token=decoy_token),
            _round2_warnings(round1, round2),
            session_id,
            decoy=decoy,
            decoy_token=decoy_token,
        )
        return report


def _round1_failures(round1: AgentCheckReport, timeout: int) -> tuple[str, ...]:
    """Why there is nothing to resume from, in the order it went wrong."""
    failures: list[str] = []
    if round1.timed_out:
        failures.append(f"round 1 did not exit within {timeout}s")
    if round1.checkpoint != ROUND1_EXPECTED:
        failures.append(
            f"round 1 wrote {round1.checkpoint_phrase}, "
            f"not {ROUND1_EXPECTED}"
        )
    if not round1.agent_session_id:
        failures.append(
            "round 1 captured no native session id, so there is nothing to "
            "resume: the adapter reads it from the CLI's own output"
        )
    return tuple(failures)


def _decoy_failures(
    round1: AgentCheckReport, decoy: AgentCheckReport
) -> tuple[str, ...]:
    """Why the decoy could not do its one job, if it could not.

    Its checkpoint is not judged. What it has to be is another session:
    one with a native id of its own. Without that, "the most recent
    session" is round 1's again, a backend that resumes by recency passes,
    and the check cannot tell — which is an inconclusive result, not a pass.
    """
    if decoy.agent_session_id is None:
        why = "reported no native session id"
    elif decoy.agent_session_id == round1.agent_session_id:
        why = "reported the same native session id as round 1"
    else:
        return ()
    return (
        f"the decoy session {why}, so resuming by recency could not be "
        "ruled out: inconclusive",
    )


def _round2_failures(
    round2: AgentCheckReport,
    token: str,
    timeout: int,
    *,
    decoy_token: str | None = None,
) -> tuple[str, ...]:
    """Why the resumed round did not answer. Each is a separate defect."""
    failures: list[str] = []
    if round2.timed_out:
        failures.append(f"round 2 did not exit within {timeout}s")
    if round2.checkpoint != ROUND2_EXPECTED:
        failures.append(
            f"round 2 wrote {round2.checkpoint_phrase}, not {ROUND2_EXPECTED}"
        )
        return tuple(failures)
    said = round2.checkpoint_summary or ""
    if decoy_token and decoy_token.lower() in said.lower():
        failures.append(
            "round 2 answered with the decoy session's token: the backend "
            "resumed the most recent session, not the native id it was "
            "given — check that resume_args pass {resume_session_id}"
        )
    elif token.lower() not in said.lower():
        failures.append(
            "round 2 did not have the token: its summary said "
            + (f"{said!r}" if said else "nothing")
            + " — the session did not carry the first round's context"
        )
    return tuple(failures)


def _round2_warnings(
    round1: AgentCheckReport,
    round2: AgentCheckReport,
) -> tuple[str, ...]:
    """What to say about the native session id without failing the backend.

    A CLI that mints a new id per turn, or reports none on a resume, has
    still resumed if the token came back. The pipelines store whatever the
    adapter reports, so the operator does want to know — as a warning.
    """
    return _session_id_warnings(round1, round2)


def _session_id_warnings(
    round1: AgentCheckReport, round2: AgentCheckReport
) -> tuple[str, ...]:
    """Round 2's native id against round 1's."""
    if round2.agent_session_id is None:
        return (
            "round 2 reported no native session id; the relay would keep "
            f"resuming from {round1.agent_session_id}",
        )
    if round2.agent_session_id != round1.agent_session_id:
        return (
            f"round 2 reported a new native session id "
            f"{round2.agent_session_id} (round 1 captured "
            f"{round1.agent_session_id}); the relay stores the newer one",
        )
    return ()


def _finish_resume(
    agent_type: str,
    token: str,
    round1: AgentCheckReport,
    round2: AgentCheckReport | None,
    failures: tuple[str, ...],
    warnings: tuple[str, ...],
    session_id: str,
    *,
    decoy: AgentCheckReport | None = None,
    decoy_token: str | None = None,
) -> AgentResumeCheckReport:
    """Build the pair report and log it, on both the stop and pass paths."""
    report = AgentResumeCheckReport(
        agent_type=agent_type,
        token=token,
        round1=round1,
        round2=round2,
        failures=failures,
        warnings=warnings,
        decoy=decoy,
        decoy_token=decoy_token,
    )
    log_event(
        _logger,
        "agent_check.resume.finished",
        session_id=session_id,
        agent_type=agent_type,
        rounds_run=report.rounds_run,
        round1_checkpoint=round1.checkpoint,
        round2_checkpoint=round2.checkpoint if round2 else None,
        token_echoed=report.token_echoed,
        failures=list(failures),
        warnings=list(warnings),
        ok=report.ok,
    )
    return report


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "NO_CHECKPOINT",
    "RESUME_QUESTION",
    "RESUME_TOKEN_BYTES",
    "ROUND1_EXPECTED",
    "ROUND2_EXPECTED",
    "UNREADABLE_CHECKPOINT",
    "AgentCheckError",
    "AgentCheckReport",
    "AgentResumeCheckReport",
    "check_prompt",
    "make_throwaway_repo",
    "resume_round1_prompt",
    "resume_round2_prompt",
    "run_agent_check",
    "run_agent_resume_check",
]
