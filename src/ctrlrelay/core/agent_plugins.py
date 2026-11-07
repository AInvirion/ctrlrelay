"""Shared identity and capability types for coding-agent plugins.

The control plane is not involved in standalone runs. These types give it a
small, backend-neutral vocabulary for later assignments without letting an
assignment supply an executable or arbitrary command-line arguments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from ctrlrelay.core.dispatcher import AgentAdapter


_NATIVE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def native_session_id_is_safe(value: str) -> bool:
    """Native ids are data, never a CLI option, path, or whitespace fragment."""
    return _NATIVE_SESSION_ID_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class AgentCapabilities:
    """Optional features a plugin can honestly provide to the runtime.

    Every capability defaults to unavailable. A missing transcript parser or
    usage reader must never become an implied yes: unknown usage is not zero,
    and no structured events means no live activity or loop detection.
    """

    resume: bool = False
    structured_events: bool = False
    usage: bool = False
    sandbox: bool = False


@dataclass(frozen=True)
class AgentIdentity:
    """The concrete plugin selected for one logical run.

    ``plugin_version`` is the adapter contract/manifest version, not a claim
    that the external CLI was probed. ``agent_session_id`` remains in the
    sessions table beside this identity because it belongs to the CLI itself.
    """

    agent_type: str
    plugin_id: str
    plugin_version: str = "1"
    model: str | None = None
    provider: str | None = None
    # Canonical invocation fingerprint. Generic manifests include their full
    # mapping; built-ins include the adapter contract and resolved executable.
    fingerprint: str | None = None


@dataclass(frozen=True)
class AgentSelection:
    """A concrete adapter and the identity that must be pinned before use."""

    dispatcher: AgentAdapter
    identity: AgentIdentity


class AgentSelectionError(ValueError):
    """An explicit selection is unknown, disallowed, or conflicts with a pin."""


class AgentIdentityConflictError(AgentSelectionError):
    """A run would change plugin identity instead of preserving its pin."""


def agent_identity_from_mapping(row: Mapping[str, Any] | None) -> AgentIdentity | None:
    """Read an identity from a sessions-row-shaped mapping.

    Rows written before #379 have no type and return ``None``. Their backend
    cannot be inferred safely from the current configuration.
    """

    if not row:
        return None
    agent_type = row.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type:
        return None

    def optional_text(key: str) -> str | None:
        value = row.get(key)
        return value if isinstance(value, str) and value else None

    return AgentIdentity(
        agent_type=agent_type,
        plugin_id=optional_text("agent_plugin_id") or agent_type,
        plugin_version=optional_text("agent_plugin_version") or "1",
        model=optional_text("agent_model"),
        provider=optional_text("agent_provider"),
        fingerprint=optional_text("agent_fingerprint"),
    )
