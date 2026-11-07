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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctrlrelay.core.agent_plugins import AgentCapabilities, native_session_id_is_safe
from ctrlrelay.core.binaries import resolve_binary
from ctrlrelay.core.budget import BudgetScope
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
    outcome_hook: DispatchOutcomeHook | None = None
    sandbox: str = "none"
    usage_store: UsageStore | None = field(kw_only=True)
    active: int = field(default=0, init=False)
    started: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.binary or not self.binary.strip():
            raise ValueError("generic CLI binary must not be empty")
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
            usage=False,
            sandbox=False,
        )

    @property
    def agent_binary(self) -> str:
        return self.binary

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
