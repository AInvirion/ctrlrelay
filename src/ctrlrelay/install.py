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
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import cost: the CLI defers config loading too
    from ctrlrelay.core.config import Config

_TEMPLATE_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# The variable the bridge and poller read their chat token from when
# nothing names one. It is the Telegram default from ``TelegramConfig``,
# and it is only a fallback: the rendered unit should carry whatever
# ``transport.<type>.bot_token_env`` says, which is the whole of #267 —
# a Mattermost box got units mentioning a Telegram variable it never
# sets, so the bridge could not log in once systemd or launchd started
# it. Callers pass ``token_env_var`` to say what the config named.
DEFAULT_TOKEN_ENV_VAR = "CTRLRELAY_TELEGRAM_TOKEN"


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
    service: str  # "bridge" | "poller" | "web" (systemd only)
    content: str
    target_path: Path
    # Variables that were left unsubstituted (e.g. the chat token when its
    # env var wasn't set at render time). Caller decides whether to warn
    # the operator or refuse to write.
    unresolved: list[str]
    # The env var this unit expects its chat token in, as named by the
    # active transport's ``bot_token_env``. Carried so the caller can
    # tell the operator which variable to export without guessing which
    # of ``unresolved`` is the token — on a renamed variable there is no
    # recognisable shape to match on.
    token_env_var: str = DEFAULT_TOKEN_ENV_VAR


def token_env_var_from_config(config: Config) -> str:
    """The env var this config's transport keeps its chat token in.

    One place answers "which variable does this install need", so the
    rendered unit, the warning and the hint cannot disagree. The name
    comes from ``transport.<type>.bot_token_env``, which is free text:
    an operator may point either chat transport at any variable, so this
    reads the setting rather than branching on the transport type.

    ``file_mock`` has no token and no bridge, so there is nothing to
    name; that falls back to ``DEFAULT_TOKEN_ENV_VAR``, which leaves a
    flagged placeholder in the unit exactly as it did before #267 rather
    than inventing a variable or writing a unit with no token line.
    """
    from ctrlrelay.bridge.factory import HandlerConfigError, token_env_var

    try:
        return token_env_var(config.transport)
    except HandlerConfigError:
        return DEFAULT_TOKEN_ENV_VAR


def _build_substitution_values(
    *,
    workdir: Path,
    label_prefix: str,
    poller_interval: int,
    ctrlrelay_bin: str | None = None,
    token_env_var: str = DEFAULT_TOKEN_ENV_VAR,
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
    # The templates name the token variable twice over: ``TOKEN_ENV_VAR``
    # is the name the unit sets, ``TOKEN_ENV_VALUE`` is what it sets it to.
    values["TOKEN_ENV_VAR"] = token_env_var
    # Pass through the bot token from the current environment if the
    # operator has it exported at install time. Without it, the value
    # becomes a placeholder naming the variable itself, so the rendered
    # file says which one to fill in and ``_missing_vars`` reports it
    # under that name rather than under ``TOKEN_ENV_VALUE``, which would
    # tell the operator nothing. ``safe_substitute`` does not rescan what
    # it substituted, so a ``${...}`` inserted here survives the pass.
    token = os.environ.get(token_env_var)
    values["TOKEN_ENV_VALUE"] = token if token else "${" + token_env_var + "}"
    return values


def render_launchd(
    *,
    workdir: Path,
    label_prefix: str = "com.ctrlrelay",
    poller_interval: int = 300,
    ctrlrelay_bin: str | None = None,
    target_dir: Path | None = None,
    token_env_var: str = DEFAULT_TOKEN_ENV_VAR,
) -> list[RenderedUnit]:
    """Render the bridge and poller launchd plists.

    ``target_dir`` defaults to ``~/Library/LaunchAgents`` — override
    in tests. The filename uses ``{label_prefix}.ctrlrelay-{service}.plist``
    so it lines up with the ``<Label>`` value inside the plist (launchd
    won't load otherwise).

    ``token_env_var`` is the variable the units set their chat token in —
    ``transport.<type>.bot_token_env`` for the configured transport. See
    ``token_env_var_from_config``.
    """
    values = _build_substitution_values(
        workdir=workdir,
        label_prefix=label_prefix,
        poller_interval=poller_interval,
        ctrlrelay_bin=ctrlrelay_bin,
        token_env_var=token_env_var,
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
                token_env_var=token_env_var,
            )
        )
    return units


def render_systemd(
    *,
    workdir: Path,
    poller_interval: int = 300,
    ctrlrelay_bin: str | None = None,
    target_dir: Path | None = None,
    token_env_var: str = DEFAULT_TOKEN_ENV_VAR,
) -> list[RenderedUnit]:
    """Render the bridge, poller and status-page systemd user unit files.

    ``target_dir`` defaults to ``~/.config/systemd/user`` — the canonical
    location for user-scoped units (no root needed). systemd doesn't
    require label_prefix-style namespacing the way launchd does, so the
    filenames are fixed: ``ctrlrelay-{bridge,poller,web}.service``. The
    web unit (#209) is rendered here only; launchd has no template for it.

    ``token_env_var`` is the variable the bridge and poller units set
    their chat token in — ``transport.<type>.bot_token_env`` for the
    configured transport. The web unit sends nothing and takes no token.
    """
    values = _build_substitution_values(
        workdir=workdir,
        # systemd templates don't reference LABEL_PREFIX, but the helper
        # still needs the key for uniformity — pass an unused literal.
        label_prefix="ctrlrelay",
        poller_interval=poller_interval,
        ctrlrelay_bin=ctrlrelay_bin,
        token_env_var=token_env_var,
    )
    target_dir = target_dir or Path.home() / ".config" / "systemd" / "user"
    units: list[RenderedUnit] = []
    for service in ("bridge", "poller", "web"):
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
                token_env_var=token_env_var,
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

    Also ensures ``~/.ctrlrelay/logs/`` exists — both the launchd and
    systemd templates set StandardOutput/StandardError paths under it,
    and systemd refuses to start a unit at all (EXIT_STDOUT, code 209)
    when the directory is missing; combined with ``Restart=always``
    that's an immediate crash-loop (#137).

    Unit files are chmod'd ``0600`` after writing since they embed the
    chat token as plaintext.
    """
    (Path.home() / ".ctrlrelay" / "logs").mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for unit in units:
        unit.target_path.parent.mkdir(parents=True, exist_ok=True)
        if unit.target_path.exists() and not overwrite:
            raise FileExistsError(
                f"refusing to overwrite {unit.target_path}; "
                "pass --force to replace it"
            )
        unit.target_path.write_text(unit.content, encoding="utf-8")
        unit.target_path.chmod(0o600)
        written.append(unit.target_path)
    return written
