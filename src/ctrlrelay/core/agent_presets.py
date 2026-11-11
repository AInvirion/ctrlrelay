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
    # Bumped whenever ``args`` or ``resume_args`` change, so a run pinned
    # to the old arguments is refused instead of resumed with new ones.
    version: str


_COPILOT_ARGS = (
    "-p", "{prompt}",
    "--allow-all-tools",
    "--disable-builtin-mcps",
    "--output-format", "json",
)

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
        version="1",
    ),
}

PRESET_NAMES: frozenset[str] = frozenset(_PRESETS)


def preset_manifest(
    name: str, overrides: PresetOverrides | None = None,
) -> GenericCliConfig | None:
    """The manifest for preset ``name``, or ``None`` if there is no such preset.

    ``overrides`` may set the binary, the model and the timeout. A model
    is passed as ``--model <model>`` at the end of both argument lists.
    """
    from ctrlrelay.core.config import GenericCliConfig, PresetOverrides

    preset = _PRESETS.get(name)
    if preset is None:
        return None
    o = overrides if overrides is not None else PresetOverrides()
    model_args: tuple[str, ...] = ("--model", o.model) if o.model else ()
    return GenericCliConfig(
        binary=o.binary or preset.binary,
        args=[*preset.args, *model_args],
        resume_args=[*preset.resume_args, *model_args],
        session_id_path=preset.session_id_path,
        default_timeout_seconds=o.default_timeout_seconds,
        model=o.model,
        plugin_version=preset.version,
        instructions_files=list(preset.instructions_files),
    )
