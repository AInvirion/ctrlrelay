"""What the coding agent's process is allowed to carry (#219).

The relay runs the agent as the operator's own user, under the
operator's own environment. Before #219 that environment reached the
agent whole: a ``GH_TOKEN`` if one was set, ``SSH_AUTH_SOCK``, the
chat bot tokens, and through them ``gh``'s stored login, git's
credential helpers and the operator's SSH key. Text the agent reads
(an issue body, a file in the repository) could therefore push to any
branch or merge a pull request with the operator's login.

For the dev and task lanes the relay now does every GitHub write
itself, after the agent exits, so the agent needs none of that. This
module builds the environment that says so:

- GitHub token variables are removed, and so is everything else in
  the ``GH_*`` and ``GITHUB_*`` families, because ``gh`` reads its
  settings from the first and CI-style tooling reads tokens from the
  second.
- ``GH_CONFIG_DIR`` points at a fresh empty directory, so ``gh`` finds
  no stored login.
- git gets ``credential.helper`` reset to empty through
  ``GIT_CONFIG_COUNT``, ``GIT_SSH_COMMAND=false`` so any SSH remote
  (including one reached through an ``insteadOf`` rewrite, which is
  how alkaturbo pushes) fails before a key is offered, and
  ``GIT_TERMINAL_PROMPT=0`` so nothing waits on a prompt.
- ``SSH_AUTH_SOCK`` and friends are removed.
- The relay's own chat tokens are removed: the agent has no reason to
  hold the credentials of the channel the operator is reached on.

What it keeps is everything else, on purpose. The agent still has to
run the model CLI (which authenticates from its own files under
``~/.claude`` or from ``ANTHROPIC_*`` variables) and build and test
the project with whatever toolchain the operator installed.

What #219 guarantees: no GitHub credential is handed to the agent
through its environment; ctrlrelay chooses the push URL, the branch and
the base; main, master, the default branch and any other branch are
refused; a remote branch ctrlrelay did not push is not overwritten;
agent-written PR and comment text is sanitized; and an agent that only
uses its normal tooling (git, gh, ssh as configured) cannot push.

What it does NOT guarantee: the agent runs as the same OS user, so a
deliberately hostile agent can still read credential files
(``~/.config/gh/hosts.yml``, ``~/.ssh``), use them by hand, and write
to the relay's push directory, the bare clone's object store and any
``objects/info/alternates`` chain it adds there, racing a push. Closing
that needs a separate user or a read-only home (#236). ``agent.sandbox:
bwrap`` hides the credential paths; it does not stop the writes.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

# Named explicitly as well as caught by the prefix rule below, so the
# list a reader greps for is in the file and a test can pin it.
GITHUB_TOKEN_VARS: tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)

# Whole families, by prefix. ``gh`` reads GH_* (GH_HOST, GH_REPO,
# GH_CONFIG_DIR, ...); GITHUB_* is where Actions-style tooling looks
# for tokens. Nothing a build needs lives in either.
_STRIPPED_PREFIXES: tuple[str, ...] = ("GH_", "GITHUB_")

# Ways git or ssh could reach a credential without a token variable.
# GIT_CONFIG_* is cleared so an inherited GIT_CONFIG_COUNT cannot
# re-add a helper after ours (entries are numbered, and a second
# count would replace ours wholesale). GIT_CONFIG_PARAMETERS is the
# carrier for `git -c`, which outranks GIT_CONFIG_COUNT.
_STRIPPED_EXACT: frozenset[str] = frozenset({
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "SSH_ASKPASS",
    "GIT_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
})
_GIT_CONFIG_INDEXED = re.compile(r"^GIT_CONFIG_(KEY|VALUE)_\d+$")

# The relay's own secrets. Every CTRLRELAY_* variable is dropped and
# the two the checkpoint protocol needs are set again by the
# dispatcher, so a chat token added later under a new name is covered
# without anybody remembering to list it.
_RELAY_PREFIX = "CTRLRELAY_"

# Deliberately unconnectable. See build_agent_env.
_NO_SESSION_BUS = "unix:path=/nonexistent/ctrlrelay-agent-has-no-session-bus"

# Applied through GIT_CONFIG_COUNT. An empty `credential.helper`
# resets the helper list, so helpers from ~/.gitconfig and from
# /etc/gitconfig are never consulted.
_GIT_CONFIG_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("credential.helper", ""),
    ("core.askPass", ""),
)


def _is_stripped(name: str) -> bool:
    if name in GITHUB_TOKEN_VARS or name in _STRIPPED_EXACT:
        return True
    if name.startswith(_STRIPPED_PREFIXES) or name.startswith(_RELAY_PREFIX):
        return True
    return bool(_GIT_CONFIG_INDEXED.match(name))


def build_agent_env(
    base: Mapping[str, str],
    *,
    gh_config_dir: Path,
    runtime_dir: Path,
) -> tuple[dict[str, str], list[str]]:
    """Return ``(env, stripped_names)`` for an agent with no GitHub access.

    ``gh_config_dir`` and ``runtime_dir`` must be empty directories the
    agent did not choose; the caller creates and removes them.
    ``runtime_dir`` becomes ``XDG_RUNTIME_DIR``, so a client that looks
    for the session bus under it finds nothing there. ``stripped_names`` is
    the sorted list of variable NAMES removed, for the log. Values
    are never returned or logged.
    """
    env: dict[str, str] = {}
    stripped: list[str] = []
    for name, value in base.items():
        if _is_stripped(name):
            stripped.append(name)
            continue
        env[name] = value

    env["GH_CONFIG_DIR"] = str(gh_config_dir)
    # gh falls back to the desktop keyring (the Secret Service, over the
    # D-Bus session bus) when its config dir has no token, and so does
    # anything built on libsecret. Measured on a laptop host on
    # 2026-10-31: with GH_CONFIG_DIR empty, `gh auth token` still printed
    # the operator's token, with or without DBUS_SESSION_BUS_ADDRESS set,
    # because the D-Bus client then dials /run/user/<uid>/bus by itself.
    # An address that cannot connect is what stops it. macOS keeps the
    # login keychain outside anything an environment variable reaches;
    # see the configuration docs.
    env["DBUS_SESSION_BUS_ADDRESS"] = _NO_SESSION_BUS
    # Also replaced, so a client that derives the bus from it finds an
    # empty private directory. Measured: gh's D-Bus client falls back to
    # /run/user/<uid>/bus by uid when no address is set, ignoring this
    # variable, so it is the address above that blocks that fallback;
    # this closes the XDG-based one.
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    # `false` exits 1 without reading anything, so every SSH remote
    # fails before a key or an agent is touched. Takes precedence over
    # core.sshCommand in any config file.
    env["GIT_SSH_COMMAND"] = "false"
    env["GIT_CONFIG_COUNT"] = str(len(_GIT_CONFIG_OVERRIDES))
    for i, (key, value) in enumerate(_GIT_CONFIG_OVERRIDES):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env, sorted(stripped)


DEFAULT_SANDBOX_HIDE_PATHS: tuple[str, ...] = (
    "~/.ssh",
    "~/.config/gh",
    "~/.git-credentials",
    "~/.config/git/credentials",
    "~/.netrc",
    # The desktop keyring's files and the session bus it answers on,
    # so resetting DBUS_SESSION_BUS_ADDRESS inside the sandbox does not
    # reach it either.
    "~/.local/share/keyrings",
    "$XDG_RUNTIME_DIR/bus",
)


class SandboxUnavailableError(RuntimeError):
    """``agent.sandbox: bwrap`` was configured and cannot be honoured."""


def resolve_bwrap() -> str:
    """Absolute path of ``bwrap``, or raise naming what was searched."""
    found = shutil.which("bwrap")
    if found:
        return found
    for candidate in ("/usr/bin/bwrap", "/usr/local/bin/bwrap"):
        if os.access(candidate, os.X_OK):
            return candidate
    raise SandboxUnavailableError(
        "agent.sandbox is 'bwrap' but no bwrap binary was found on PATH "
        "or in /usr/bin, /usr/local/bin. Install bubblewrap or set "
        "agent.sandbox: none."
    )


def bwrap_prefix(bwrap: str, hide_paths: Sequence[str]) -> list[str]:
    """Argv prefix that runs a command with ``hide_paths`` made empty.

    The rest of the filesystem is bound through unchanged and writable,
    so the agent's toolchain, caches and the model CLI's own state
    behave exactly as without the sandbox. Only the named paths change:
    a directory becomes an empty tmpfs and a file becomes /dev/null.
    A path that does not exist is skipped (bwrap would otherwise have
    to create a mount point for it).
    """
    argv = [bwrap, "--dev-bind", "/", "/", "--die-with-parent"]
    for raw in hide_paths:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        if "$" in expanded:
            # An unset variable: there is nothing at a path we cannot
            # name, and a literal "$XDG_RUNTIME_DIR" is not a path.
            continue
        p = Path(expanded)
        if p.is_symlink():
            p = p.resolve()
        if p.is_dir():
            argv += ["--tmpfs", str(p)]
        elif p.exists():
            argv += ["--ro-bind", "/dev/null", str(p)]
    argv.append("--")
    return argv
