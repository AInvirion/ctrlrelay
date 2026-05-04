"""Render and install service unit files (launchd, systemd) from
in-package templates.

The bridge and poller are meant to run as supervised long-lived
services. Hand-writing platform unit files is error-prone (absolute
paths, label/filename consistency, exit-timeout values that prevent
mid-cleanup SIGKILL) and historically left operator boxes with
hard-coded ``/Users/$ME/...`` strings — fine on one machine, broken
on the next.

This module ships templates inside the package and exposes a small
substitution + write helper so ``ctrlrelay install launchd|systemd``
can produce ready-to-load files for whoever is running it. No
``sudo``, no daemon-level paths — everything lives under the user's
home directory.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import string
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


_TEMPLATE_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _read_template(platform: str, name: str) -> str:
    pkg = f"ctrlrelay.templates.{platform}"
    return resources.files(pkg).joinpath(name).read_text(encoding="utf-8")


def _substitute(text: str, values: dict[str, str]) -> str:
    # We use Template.safe_substitute over plain str.format because the
    # plist/systemd templates contain literal braces that .format would
    # interpret. safe_substitute leaves unknown ${VAR} references intact
    # rather than raising — useful for the bot-token placeholder when the
    # operator hasn't exported the env var at install time.
    return string.Template(text).safe_substitute(values)


def _missing_vars(rendered: str) -> list[str]:
    return sorted(set(_TEMPLATE_VAR_RE.findall(rendered)))


@dataclass(frozen=True)
class RenderedUnit:
    """A rendered unit file ready to write to disk."""

    platform: str  # "launchd" | "systemd"
    service: str  # "bridge" | "poller"
    content: str
    target_path: Path
    # Variables that were left unsubstituted (e.g. CTRLRELAY_TELEGRAM_TOKEN
    # when the env var wasn't set at render time). Caller decides whether
    # to warn the operator or refuse to write.
    unresolved: list[str]


def _build_substitution_values(
    *,
    workdir: Path,
    label_prefix: str,
    poller_interval: int,
    ctrlrelay_bin: str | None = None,
) -> dict[str, str]:
    bin_path = ctrlrelay_bin or shutil.which("ctrlrelay") or "ctrlrelay"
    home = Path.home()
    values = {
        "USER": getpass.getuser(),
        "HOME": str(home),
        "CTRLRELAY_BIN": bin_path,
        "WORKDIR": str(workdir.expanduser()),
        "LABEL_PREFIX": label_prefix,
        "POLLER_INTERVAL": str(poller_interval),
    }
    # Pass through the bot token from the current environment if the
    # operator has it exported at install time. Leaving it out means the
    # placeholder survives in the rendered file and the operator must
    # edit by hand — ``unresolved`` flags this.
    token = os.environ.get("CTRLRELAY_TELEGRAM_TOKEN")
    if token:
        values["CTRLRELAY_TELEGRAM_TOKEN"] = token
    return values


def render_launchd(
    *,
    workdir: Path,
    label_prefix: str = "com.ctrlrelay",
    poller_interval: int = 300,
    ctrlrelay_bin: str | None = None,
    target_dir: Path | None = None,
) -> list[RenderedUnit]:
    """Render the bridge and poller launchd plists.

    ``target_dir`` defaults to ``~/Library/LaunchAgents`` — override
    in tests. The filename uses ``{label_prefix}.ctrlrelay-{service}.plist``
    so it lines up with the ``<Label>`` value inside the plist (launchd
    won't load otherwise).
    """
    values = _build_substitution_values(
        workdir=workdir,
        label_prefix=label_prefix,
        poller_interval=poller_interval,
        ctrlrelay_bin=ctrlrelay_bin,
    )
    target_dir = target_dir or Path.home() / "Library" / "LaunchAgents"
    units: list[RenderedUnit] = []
    for service in ("bridge", "poller"):
        raw = _read_template("launchd", f"{service}.plist.template")
        rendered = _substitute(raw, values)
        target = target_dir / f"{label_prefix}.ctrlrelay-{service}.plist"
        units.append(
            RenderedUnit(
                platform="launchd",
                service=service,
                content=rendered,
                target_path=target,
                unresolved=_missing_vars(rendered),
            )
        )
    return units


def render_systemd(
    *,
    workdir: Path,
    poller_interval: int = 300,
    ctrlrelay_bin: str | None = None,
    target_dir: Path | None = None,
) -> list[RenderedUnit]:
    """Render the bridge and poller systemd user unit files.

    ``target_dir`` defaults to ``~/.config/systemd/user`` — the canonical
    location for user-scoped units (no root needed). systemd doesn't
    require label_prefix-style namespacing the way launchd does, so the
    filenames are fixed: ``ctrlrelay-{bridge,poller}.service``.
    """
    values = _build_substitution_values(
        workdir=workdir,
        # systemd templates don't reference LABEL_PREFIX, but the helper
        # still needs the key for uniformity — pass an unused literal.
        label_prefix="ctrlrelay",
        poller_interval=poller_interval,
        ctrlrelay_bin=ctrlrelay_bin,
    )
    target_dir = target_dir or Path.home() / ".config" / "systemd" / "user"
    units: list[RenderedUnit] = []
    for service in ("bridge", "poller"):
        raw = _read_template("systemd", f"{service}.service.template")
        rendered = _substitute(raw, values)
        target = target_dir / f"ctrlrelay-{service}.service"
        units.append(
            RenderedUnit(
                platform="systemd",
                service=service,
                content=rendered,
                target_path=target,
                unresolved=_missing_vars(rendered),
            )
        )
    return units


def write_units(
    units: list[RenderedUnit],
    *,
    overwrite: bool = False,
) -> list[Path]:
    """Write rendered units to their target paths.

    Refuses to clobber an existing file unless ``overwrite=True`` —
    silently overwriting an operator's customised plist would be a foot-gun.
    Returns the list of paths actually written. Raises ``FileExistsError``
    if any target exists and overwrite is False.
    """
    written: list[Path] = []
    for unit in units:
        unit.target_path.parent.mkdir(parents=True, exist_ok=True)
        if unit.target_path.exists() and not overwrite:
            raise FileExistsError(
                f"refusing to overwrite {unit.target_path}; "
                "pass --force to replace it"
            )
        unit.target_path.write_text(unit.content, encoding="utf-8")
        written.append(unit.target_path)
    return written
