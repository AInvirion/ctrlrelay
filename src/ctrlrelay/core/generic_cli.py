"""Generic, manifest-friendly adapter for non-interactive coding CLIs.

The adapter owns only invocation and session-id discovery. Process lifetime,
checkpoint handling, outcome reporting, and credential isolation stay in
:class:`CliAgentAdapter` so every CLI gets the same safety behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ctrlrelay.core.agent_plugins import AgentCapabilities, native_session_id_is_safe
from ctrlrelay.core.binaries import resolve_binary
from ctrlrelay.core.budget import BudgetScope, RunUsage
from ctrlrelay.core.config import USAGE_FIELD_NAMES, GenericUsageConfig
from ctrlrelay.core.dispatcher import (
    CliAgentAdapter,
    DispatchOutcomeHook,
    SessionResult,
)
from ctrlrelay.core.obs import get_logger, log_event
from ctrlrelay.core.usage import UsageStore

_logger = get_logger("core.generic_cli")

_PROMPT = "{prompt}"
_RESUME_SESSION_ID = "{resume_session_id}"
_PLACEHOLDER_RE = re.compile(
    r"\{(prompt|session_id|state_file|resume_session_id|model|provider)\}"
)
_ANY_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
# Native sessions whose last running total is remembered, so a resumed
# round is charged only what it added (#387). Bounded so a long-lived node
# does not grow it without limit; a resume whose total was evicted is
# charged a floor and shown as unknown.
_NATIVE_READINGS_KEPT = 256


def _dotted(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


# No real run reports anywhere near this in one count; a number past it is
# a malformed report, and charging it would stop the session for nothing.
MAX_TOKEN_COUNT = 10**12


def _token_count(value: Any) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_TOKEN_COUNT
    ):
        return None
    return value


def read_usage_events(stdout_text: str, spec: GenericUsageConfig) -> RunUsage:
    """The usage a CLI reported in its JSON-lines output, per ``spec``.

    Unknown is never zero. The result is counted as unreadable, so the
    total shows as unknown and what was read is a floor, when there is no
    matching line at all; when any line that starts as a JSON object is not
    valid JSON (it may be a usage line cut off before its type was written);
    or when a matching line that counts has a count that is missing, not an
    integer, negative or above ``MAX_TOKEN_COUNT``, in which case that line
    contributes nothing. With ``reading: last`` only the last matching line
    counts.
    """
    readings: list[dict[str, int] | None] = []
    lost = 0
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            lost += 1
            continue
        if not isinstance(event, dict):
            continue
        if _dotted(event, spec.event_type_path) != spec.event_type:
            continue
        counts: dict[str, int] | None = {}
        for name, path in spec.fields.items():
            got = _token_count(_dotted(event, path))
            if got is None:
                counts = None
                break
            counts[name] = got
        if counts is not None and spec.input_includes_cache_read:
            uncached = counts["input_tokens"] - counts["cache_read_tokens"]
            counts = None if uncached < 0 else {**counts, "input_tokens": uncached}
        readings.append(counts)

    if not readings:
        return RunUsage(unreadable=1)
    if spec.reading == "last":
        readings = readings[-1:]
    totals = dict.fromkeys(USAGE_FIELD_NAMES, 0)
    unreadable = lost
    for counts in readings:
        if counts is None:
            unreadable += 1
            continue
        for name, value in counts.items():
            totals[name] += value
    return RunUsage(**totals, unreadable=unreadable)


def _minus(total: RunUsage, earlier: RunUsage) -> tuple[RunUsage, bool]:
    """``total - earlier`` per count, clamped at zero, and whether any
    count went below zero (the running total moved backwards)."""
    diffs = {
        name: getattr(total, name) - getattr(earlier, name)
        for name in USAGE_FIELD_NAMES
    }
    went_back = any(v < 0 for v in diffs.values())
    return RunUsage(
        **{name: max(0, v) for name, v in diffs.items()},
        unreadable=total.unreadable,
    ), went_back


@dataclass
class GenericCliDispatcher(CliAgentAdapter):
    """Run a generic CLI without shell interpolation or implicit resume behavior."""

    agent_type: str = "generic"
    binary: str = ""
    args: tuple[str, ...] = ()
    prompt_mode: str = "arg"
    resume_args: tuple[str, ...] | None = None
    session_id_path: str | None = None
    default_timeout: int = 1800
    model: str | None = None
    provider: str | None = None
    plugin_version: str = "1"
    extra_env: dict[str, str] = field(default_factory=dict)
    # Provider variables this CLI may receive (#390), from the manifest's
    # ``env_allow``. Empty: no well-known provider key reaches it.
    env_allow: tuple[str, ...] = ()
    instructions_files: tuple[str, ...] = ("AGENTS.md",)
    usage: GenericUsageConfig | None = None
    outcome_hook: DispatchOutcomeHook | None = None
    sandbox: str = "none"
    usage_store: UsageStore | None = field(kw_only=True)
    active: int = field(default=0, init=False)
    started: int = field(default=0, init=False)
    _native_readings: OrderedDict[str, RunUsage] = field(
        default_factory=OrderedDict, init=False, repr=False,
    )

    def __post_init__(self) -> None:
        if not self.binary or not self.binary.strip():
            raise ValueError("generic CLI binary must not be empty")
        self.instructions_files = tuple(self.instructions_files)
        if not self.instructions_files:
            raise ValueError("generic CLI instructions_files must not be empty")
        for name in self.instructions_files:
            path = Path(name)
            if (
                not name.strip()
                or path.is_absolute()
                or not path.parts
                or any(part in ("", ".", "..") for part in path.parts)
            ):
                raise ValueError(
                    f"generic CLI instructions file {name!r} must be a safe "
                    "relative path"
                )
        if self.prompt_mode not in ("arg", "stdin"):
            raise ValueError(
                f"generic CLI prompt_mode must be 'arg' or 'stdin', "
                f"got {self.prompt_mode!r}"
            )
        if self.sandbox != "none":
            raise ValueError(
                f"generic CLI agent_type {self.agent_type!r} does not support "
                f"agent.sandbox={self.sandbox!r}; only 'none' is supported. "
                "Refusing to run unconfined while sandboxing is configured."
            )
        if self.session_id_path is not None and not self.session_id_path.strip():
            raise ValueError("generic CLI session_id_path must not be empty")
        if self.resume_args is not None and self.session_id_path is None:
            raise ValueError(
                "generic CLI resume_args requires session_id_path so the native "
                "session id can be recovered for a later resume"
            )
        if any(_RESUME_SESSION_ID in arg for arg in self.args):
            raise ValueError(
                "generic CLI {resume_session_id} belongs in resume_args, not args"
            )
        if self.resume_args is not None and not any(
            _RESUME_SESSION_ID in arg for arg in self.resume_args
        ):
            raise ValueError(
                "generic CLI resume_args must contain {resume_session_id}"
            )
        configured_args = (*self.args, *(self.resume_args or ()))
        if self.prompt_mode == "stdin" and any(
            _PROMPT in arg for arg in configured_args
        ):
            raise ValueError(
                "generic CLI {prompt} is forbidden when prompt_mode='stdin'; "
                "the prompt is written to standard input"
            )
        for arg in configured_args:
            for match in _ANY_PLACEHOLDER_RE.finditer(arg):
                if _PLACEHOLDER_RE.fullmatch(match.group(0)) is None:
                    raise ValueError(
                        f"generic CLI argument {arg!r} contains unsupported "
                        f"placeholder {match.group(0)!r}"
                    )

        # A relative executable containing a separator would be resolved from
        # the repository worktree, letting repository content become the CLI.
        has_separator = os.sep in self.binary or (
            os.altsep is not None and os.altsep in self.binary
        )
        if has_separator and not Path(self.binary).is_absolute():
            raise ValueError(
                "generic CLI binary must be an absolute path or a bare name "
                "that resolves to one at startup"
            )
        if not has_separator:
            self.binary = resolve_binary(
                self.binary,
                logger=_logger,
                event_prefix="dispatcher.binary",
            )
        if not Path(self.binary).is_absolute():
            raise ValueError("generic CLI binary must resolve to an absolute path")

    @property
    def plugin_id(self) -> str:
        return f"generic:{self.agent_type}"

    @property
    def manifest_fingerprint(self) -> str:
        """Canonical invocation identity used to refuse cross-binary resumes."""
        payload = {
            "agent_type": self.agent_type,
            "binary": self.binary,
            "args": self.args,
            "prompt_mode": self.prompt_mode,
            "resume_args": self.resume_args,
            "session_id_path": self.session_id_path,
            "model": self.model,
            "provider": self.provider,
            "plugin_version": self.plugin_version,
            "extra_env": self.extra_env,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            resume=bool(self.resume_args and self.session_id_path),
            structured_events=False,
            usage=self.usage is not None,
            sandbox=False,
        )

    @property
    def agent_binary(self) -> str:
        return self.binary

    def _reads_usage_at_exit(self) -> bool:
        return self.usage is not None

    def _usage_at_exit(
        self,
        stdout_text: str,
        *,
        resume_session_id: str | None,
        agent_session_id: str | None,
        ident: dict[str, Any],
        exited_cleanly: bool = True,
    ) -> RunUsage | None:
        """This spawn's usage, from the CLI's output (#387).

        A run that did not exit cleanly (killed, timed out, non-zero exit)
        may have used more than its last report said, so its reading is
        only a floor and is marked unknown.

        A reading that ``covers`` the native session is a running total for
        that session. Its last total is remembered per relay session (and
        repo), under the id the round resumed and under the id it reported,
        so a resumed round of the same relay session is charged only what it
        added:

        - exact reading, remembered total, nothing moved backwards: the
          difference, exact; the reading becomes the remembered total.
        - inexact reading (unclean exit, a cut-off line) that still moved
          nothing backwards: the difference, as a floor marked unknown. A
          reading is never above what the CLI really used, so the difference
          is not either. The reading becomes the remembered total, so the
          next round is charged from it and this round's part is not charged
          again.
        - anything else charges nothing and is marked unknown, because
          nothing says which part of the running total is new: no remembered
          total (the relay restarted, it was evicted, or the native session
          is older than this relay session), or a total that moved
          backwards. The remembered total is left as it was, so whatever
          that round used is charged by the next round that can be measured
          against it; when there was none, an exact reading becomes it.

        A fresh native session's total is all its own round's: it is charged
        in full (a floor when inexact) and becomes the remembered total,
        replacing any older one under the same id.
        """
        spec = self.usage
        if spec is None:
            return None
        reading = read_usage_events(stdout_text, spec)
        if not exited_cleanly:
            reading = replace(reading, unreadable=reading.unreadable + 1)
        if spec.covers != "native_session":
            return reading

        if resume_session_id is None:
            if agent_session_id is not None:
                # Remembered even when inexact: it is what this round was
                # charged, so the next round charged from it adds up to the
                # CLI's own total, never more.
                self._remember(self._native_key(ident, agent_session_id), reading)
            return reading

        keys = [self._native_key(ident, resume_session_id)]
        if agent_session_id is not None and agent_session_id != resume_session_id:
            # A CLI that names a resumed session anew: the next round
            # resumes under the new id, so its baseline must be there too.
            keys.append(self._native_key(ident, agent_session_id))
        earlier = self._native_readings.get(keys[0])
        if earlier is not None:
            spawn, went_back = _minus(reading, earlier)
            if not went_back:
                for key in keys:
                    self._remember(key, reading)
                if not reading.complete:
                    log_event(
                        _logger, "dispatcher.usage.resume_floor", **ident,
                        agent_type=self.agent_type,
                    )
                return spawn
        elif earlier is None and reading.complete:
            for key in keys:
                self._remember(key, reading)
        log_event(
            _logger, "dispatcher.usage.resume_unmeasured", **ident,
            agent_type=self.agent_type,
            remembered=earlier is not None,
            reading_complete=reading.complete,
        )
        return RunUsage(unreadable=max(1, reading.unreadable))

    def _remember(self, key: str, reading: RunUsage) -> None:
        self._native_readings[key] = reading
        self._native_readings.move_to_end(key)
        while len(self._native_readings) > _NATIVE_READINGS_KEPT:
            self._native_readings.popitem(last=False)

    def _native_key(self, ident: dict[str, Any], native_id: str) -> str:
        """Where a native session's running total is remembered: per relay
        session and repo, so a total seen by one relay session is never the
        baseline of another (whose ``before`` does not include it), and two
        repositories sharing this adapter cannot overwrite each other's."""
        return "\x00".join((
            str(ident.get("session_id") or ""),
            str(ident.get("repo") or ""),
            native_id,
        ))

    def _build_argv(
        self,
        *,
        prompt: str,
        session_id: str,
        state_file: Path,
        resume_session_id: str | None,
        resume_requested: bool = False,
    ) -> list[str]:
        resuming = resume_requested or resume_session_id is not None
        if resuming and self.resume_args is None:
            raise ValueError(
                f"generic CLI agent_type {self.agent_type!r} cannot resume: "
                "resume_args is not configured; refusing to start a fresh session"
            )
        if resuming and (
            resume_session_id is None
            or not native_session_id_is_safe(resume_session_id)
        ):
            raise ValueError(
                "generic CLI resume requires a safe native session id; "
                "refusing to start a fresh session or substitute an option"
            )
        configured_args = self.resume_args if resuming else self.args
        assert configured_args is not None  # Narrowed by the resume guard above.

        values: dict[str, str | None] = {
            "prompt": prompt,
            "session_id": session_id,
            "state_file": str(state_file),
            "resume_session_id": resume_session_id or "",
            "model": self.model,
            "provider": self.provider,
        }
        for name in ("model", "provider"):
            placeholder = f"{{{name}}}"
            if not values[name] and any(
                placeholder in arg for arg in configured_args
            ):
                raise ValueError(
                    f"generic CLI argument placeholder {placeholder} requires "
                    f"{name} to be configured"
                )

        argv = [self.binary]
        for arg in configured_args:
            # One pass only: placeholder-looking text in a prompt or path is
            # data, not another template to expand.
            argv.append(_PLACEHOLDER_RE.sub(
                lambda match: values[match.group(1)] or "", arg
            ))
        if self.prompt_mode == "arg" and not any(
            _PROMPT in arg for arg in configured_args
        ):
            argv.append(prompt)
        return argv

    def _parse_agent_session_id(self, stdout_text: str) -> str | None:
        """Read only the configured dotted path from JSON objects in stdout."""
        if not self.session_id_path:
            return None

        def from_object(value: Any) -> str | None:
            current = value
            for part in self.session_id_path.split("."):
                if not isinstance(current, dict):
                    return None
                current = current.get(part)
            if isinstance(current, str) and native_session_id_is_safe(current):
                return current
            return None

        for line in stdout_text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            session_id = from_object(event)
            if session_id is not None:
                return session_id

        # Pretty-printed JSON is not JSONL. Accept one whole-output object,
        # while continuing to ignore prose and multi-object non-JSONL output.
        try:
            event = json.loads(stdout_text)
        except (ValueError, RecursionError):
            return None
        return from_object(event)

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
        timeout = timeout or self.default_timeout
        ident: dict[str, Any] = {
            "session_id": session_id,
            "repo": repo,
            "issue_number": issue_number,
        }

        env, stripped, private_root = self._session_environment(
            session_id, state_file, github_access
        )
        try:
            log_event(
                _logger,
                "dispatcher.session.credentials",
                **ident,
                github_access=github_access,
                stripped_env=stripped,
                sandbox="none",
            )
            cmd = self._build_argv(
                prompt=prompt,
                session_id=session_id,
                state_file=state_file,
                resume_session_id=resume_session_id,
                resume_requested=resume_requested,
            )

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
                    stdin_text=prompt if self.prompt_mode == "stdin" else None,
                )
            finally:
                self.active -= 1
        finally:
            if private_root is not None:
                shutil.rmtree(private_root, ignore_errors=True)
