"""Built-in agent presets: generic CLI manifests whose arguments live in code (#384).

A preset is a :class:`~ctrlrelay.core.config.GenericCliConfig` that the
operator does not write. Its name works anywhere an agent type does
(``agent.type``, ``repos[].agent_type``, ``agent.assignable_types``)
without an ``agent.plugins`` entry, and ``agent.presets.<name>`` may change
only the binary, the model and the timeout.

The arguments are fixed because some of them are what make the backend
safe to run unattended. Copilot without ``--disable-builtin-mcps`` connects
its built-in GitHub MCP server under its own login, which hands the agent
the GitHub access that credential stripping (#219) withholds. A hand-written
manifest can drop that flag without anything noticing; a preset cannot.

This module imports nothing from ``config`` at load time, because
``config`` imports :data:`PRESET_NAMES` to validate agent types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctrlrelay.core.config import GenericCliConfig, PresetOverrides


@dataclass(frozen=True)
class _Preset:
    binary: str
    args: tuple[str, ...]
    resume_args: tuple[str, ...]
    session_id_path: str
    instructions_files: tuple[str, ...]
    # The provider variables this CLI may receive (#390). Fixed here, like
    # the arguments: ``agent.presets.<name>`` cannot widen it.
    env_allow: tuple[str, ...]
    # Bumped whenever ``args`` or ``resume_args`` change, so a run pinned
    # to the old arguments is refused instead of resumed with new ones.
    version: str
    # How a model override is passed: the flag, and how many leading
    # arguments it goes after in each list (``None`` appends it at the end).
    # Codex wants it right after its subcommand, so a later positional
    # (the session id, the prompt) cannot be read as the model.
    model_flag: str = "--model"
    model_after: int | None = None
    resume_model_after: int | None = None


_COPILOT_ARGS = (
    "-p", "{prompt}",
    "--allow-all-tools",
    "--disable-builtin-mcps",
    "--output-format", "json",
)

_CODEX_SAFETY = "--dangerously-bypass-approvals-and-sandbox"

_PRESETS: dict[str, _Preset] = {
    # Checked against Copilot CLI 1.0.107 and 1.0.109 (#383): the final
    # `result` event carries a top-level `sessionId`, and `--resume <id>`
    # continues the conversation.
    "copilot": _Preset(
        binary="copilot",
        args=_COPILOT_ARGS,
        resume_args=(*_COPILOT_ARGS, "--resume", "{resume_session_id}"),
        session_id_path="sessionId",
        instructions_files=(".github/copilot-instructions.md", "AGENTS.md"),
        # Copilot's documented token variable. Its file login under
        # ~/.copilot needs no entry; this is for a node that sets the token
        # in the daemon's environment instead (#383).
        env_allow=("COPILOT_GITHUB_TOKEN",),
        version="1",
    ),
    # Checked against Codex CLI 0.166 and 0.167 (#383): the first event is
    # `{"type":"thread.started","thread_id":...}`, and `exec resume <id>`
    # continues that thread. Bypass mode, not `--sandbox workspace-write`:
    # Codex's own sandbox cannot write the linked worktree's git directory,
    # which lives outside the worktree, so a dev run can never commit
    # (#383). Codex is therefore unconfined, like every generic plugin
    # until the container runner (#352). The flag also stops Codex waiting
    # for an approval nobody is there to give.
    "codex": _Preset(
        binary="codex",
        args=("exec", "--json", _CODEX_SAFETY, "{prompt}"),
        resume_args=(
            "exec", "resume", "--json", _CODEX_SAFETY,
            "{resume_session_id}", "{prompt}",
        ),
        session_id_path="thread_id",
        instructions_files=("AGENTS.md",),
        version="1",
        model_flag="-m",
        model_after=1,
        resume_model_after=2,
    ),
}

PRESET_NAMES: frozenset[str] = frozenset(_PRESETS)


def preset_manifest(
    name: str, overrides: PresetOverrides | None = None,
) -> GenericCliConfig | None:
    """The manifest for preset ``name``, or ``None`` if there is no such preset.

    ``overrides`` may set the binary, the model and the timeout. A model
    is passed with the preset's own flag and at its own position in both
    argument lists (Copilot: ``--model <m>`` at the end; Codex: ``-m <m>``
    right after ``exec`` and ``exec resume``).
    """
    from ctrlrelay.core.config import GenericCliConfig, PresetOverrides

    preset = _PRESETS.get(name)
    if preset is None:
        return None
    o = overrides if overrides is not None else PresetOverrides()
    return GenericCliConfig(
        binary=o.binary or preset.binary,
        args=_with_model(preset.args, preset.model_flag, o.model, preset.model_after),
        resume_args=_with_model(
            preset.resume_args, preset.model_flag, o.model,
            preset.resume_model_after,
        ),
        session_id_path=preset.session_id_path,
        default_timeout_seconds=o.default_timeout_seconds,
        model=o.model,
        plugin_version=preset.version,
        instructions_files=list(preset.instructions_files),
        env_allow=list(preset.env_allow),
    )


def _with_model(
    args: tuple[str, ...], flag: str, model: str | None, after: int | None,
) -> list[str]:
    if not model:
        return list(args)
    at = len(args) if after is None else after
    return [*args[:at], flag, model, *args[at:]]
