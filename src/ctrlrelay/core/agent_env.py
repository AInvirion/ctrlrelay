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

What it does NOT guarantee: the agent runs as the same OS user, so
without a sandbox a deliberately hostile agent can still read credential
files (``~/.config/gh/hosts.yml``, ``~/.ssh``), use them by hand, and
write anywhere the operator can: dotfiles, shell startup files, the
relay's own install. ``agent.sandbox: bwrap`` (#236) closes that: the
whole filesystem is read-only, the credential paths are empty, and only
the run's worktree, its repository's git directory, a private ``/tmp``,
the agent CLI's own state and an agent-only cache are writable. Within
the git directory it can still write objects and refs, which the relay's
push already treats as untrusted (#219).
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
    # Other credentials an operator's home commonly holds (#236).
    "~/.gnupg",
    "~/.aws",
    "~/.azure",
    "~/.config/gcloud",
    "~/.kube",
    "~/.docker/config.json",
    "~/.npmrc",
    "~/.pypirc",
    "~/.cargo/credentials",
    "~/.cargo/credentials.toml",
    # The relay's own config, which can hold chat tokens.
    "~/.config/ctrlrelay",
)

# Writable inside the sandbox besides the run's own paths (#236): the
# agent CLI keeps its login, session logs and history here.
DEFAULT_SANDBOX_WRITABLE_PATHS: tuple[str, ...] = ("~/.claude",)

# Read-only again inside a writable path: what the operator's own agent
# sessions load and run (settings with hooks, plugins, skills, agents),
# so a run cannot plant something that runs later as the operator.
DEFAULT_SANDBOX_READONLY_PATHS: tuple[str, ...] = (
    "~/.claude/settings.json",
    "~/.claude/settings.local.json",
    "~/.claude/CLAUDE.md",
    "~/.claude/agents",
    "~/.claude/commands",
    "~/.claude/skills",
    "~/.claude/hooks",
    "~/.claude/plugins",
    "~/.claude/output-styles",
    "~/.claude/scripts",
    # Each project's memory, which the operator's sessions in that project
    # load. One ``*`` part is expanded to the directories there now.
    "~/.claude/projects/*/memory",
)

# The agent's package caches: its own, never the operator's, so a run
# cannot poison a cache the operator builds from later.
DEFAULT_SANDBOX_CACHE_DIR = "~/.ctrlrelay/agent-cache"


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


def _existing(raw: str | Path) -> Path | None:
    """``raw`` with ``~`` and variables expanded and a symlink followed,
    or ``None`` when it names nothing. An unset variable names nothing:
    a literal "$XDG_RUNTIME_DIR" is not a path."""
    expanded = os.path.expandvars(os.path.expanduser(str(raw)))
    if "$" in expanded:
        return None
    # Every link on the way resolved, so the bind names the real place.
    p = Path(os.path.realpath(expanded))
    return p if p.exists() else None


def bwrap_prefix(
    bwrap: str,
    hide_paths: Sequence[str],
    *,
    writable: Sequence[str | Path] = (),
    readonly: Sequence[str | Path] = (),
) -> list[str]:
    """Argv prefix that runs a command in a read-only copy of this machine.

    In order, so each step wins over the one before: the whole
    filesystem read-only, a fresh ``/dev``, ``/proc`` and private
    ``/tmp``; ``writable`` bound back writable; ``readonly`` bound
    read-only again; and ``hide_paths`` made empty (a directory becomes an
    empty tmpfs, a file ``/dev/null``) wherever they are. Bind sources are
    read from the host, so a path under ``/tmp`` binds normally. A
    hidden or writable path that does not exist is skipped (bwrap would
    otherwise have to create a mount point for it). A missing read-only
    path inside a writable one is created first (see
    :func:`_create_readonly`); elsewhere it stays absent, since nothing
    can create it. The child gets its own process namespace and dies with
    the relay.
    """
    argv = [
        bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp", "--unshare-pid", "--die-with-parent",
    ]
    open_dirs: list[Path] = []
    for raw in writable:
        p = _existing(raw)
        if p is not None:
            argv += ["--bind", str(p), str(p)]
            open_dirs.append(p)
    for raw in _expand_globs(readonly):
        p = _existing(raw)
        if p is None:
            target = _expand(raw)
            if target is None:
                continue
            # Spelled through the same links as the writable paths, which
            # were resolved: ``~/.claude`` may itself be a symlink. The last
            # part is not followed, so a dangling link is judged where it
            # sits.
            target = Path(os.path.realpath(target.parent)) / target.name
            if not any(target.is_relative_to(d) for d in open_dirs):
                continue  # nothing writable reaches it: it stays absent
            p = _create_readonly(target)
        argv += ["--ro-bind", str(p), str(p)]
    # Last, so neither a writable nor a read-only path that contains or
    # names a hidden one brings it back.
    for raw in hide_paths:
        p = _existing(raw)
        if p is None:
            continue
        if p.is_dir():
            argv += ["--tmpfs", str(p)]
        else:
            argv += ["--ro-bind", "/dev/null", str(p)]
    argv.append("--")
    return argv


def _expand_globs(paths: Sequence[str | Path]) -> list[str | Path]:
    """Each path with a ``*`` part replaced by one path per directory
    that part matches now, the rest of the path kept after it."""
    out: list[str | Path] = []
    for raw in paths:
        text = os.path.expanduser(str(raw))
        parts = Path(text).parts
        star = next((i for i, part in enumerate(parts) if "*" in part), None)
        if star is None:
            out.append(raw)
            continue
        head = Path(*parts[: star + 1])
        rest = parts[star + 1:]
        for match in sorted(head.parent.glob(head.name)):
            if match.is_dir() and not match.is_symlink():
                out.append(match.joinpath(*rest))
    return out


def _expand(raw: str | Path) -> Path | None:
    expanded = os.path.expandvars(os.path.expanduser(str(raw)))
    return None if "$" in expanded else Path(expanded)


def _create_readonly(p: Path) -> Path:
    """Create a missing read-only path inside a writable one, so it can be
    bound read-only.

    Skipping it would leave the agent free to create it: a
    ``settings.local.json`` with a hook in it, or a git hook, which then
    runs later as the operator. A name with a suffix is made a file (``{}``
    for JSON, so the agent CLI still parses it), anything else an empty
    directory. Raises :class:`SandboxUnavailableError` when it cannot be
    made, a dangling symlink included (the agent could replace the link):
    the run does not start rather than start with the path open.
    """
    if p.is_symlink():
        raise SandboxUnavailableError(
            f"agent.sandbox: {p} is a link to something missing, which the "
            "agent could replace; remove the link, or take the path out of "
            "agent.sandbox_readonly_paths"
        )
    try:
        if p.suffix:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}\n" if p.suffix == ".json" else "")
        else:
            p.mkdir(parents=True)
    except OSError as e:
        raise SandboxUnavailableError(
            f"agent.sandbox: cannot create {p} to keep it read-only "
            f"({type(e).__name__}: {e}); fix or remove it, or take it out "
            "of agent.sandbox_readonly_paths"
        ) from e
    return p.resolve()


def git_dirs(working_dir: Path) -> list[Path]:
    """The worktree's git directories (its own and the shared one), which
    a commit writes to; empty when ``working_dir`` is not in a repository."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute",
             "--git-common-dir", "--git-dir"],
            cwd=working_dir, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [Path(line) for line in out.stdout.splitlines() if line.strip()]


def agent_cache_env(cache_dir: Path) -> dict[str, str]:
    """Point the usual package caches at the agent's own ``cache_dir``."""
    return {
        "XDG_CACHE_HOME": str(cache_dir),
        # Set as well, so an operator's own setting does not win.
        "PIP_CACHE_DIR": str(cache_dir / "pip"),
        "UV_CACHE_DIR": str(cache_dir / "uv"),
        "npm_config_cache": str(cache_dir / "npm"),
        "GOMODCACHE": str(cache_dir / "go-mod"),
        # Cargo keeps its registry under CARGO_HOME; the toolchain itself
        # is found through PATH and RUSTUP_HOME, which stay as they are.
        "CARGO_HOME": str(cache_dir / "cargo"),
    }
