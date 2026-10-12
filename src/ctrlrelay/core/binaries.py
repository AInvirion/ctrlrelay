"""Finding a tool the operator installed for themselves.

A systemd user unit and a launchd job get a minimal ``PATH`` and do not
read ``~/.bashrc``, so anything under ``~/.local/bin`` — which is where
``pip --user``, ``uv tool`` and ``pipx`` all put their shims — is
invisible to the daemon while working perfectly in the operator's
terminal. The error is ``command not found`` for something that is
plainly installed.

The dispatcher already carried this fix for the agent binary, and
``#197`` is what it cost to not have it for the review tool: every
``code_review`` spawn on alkaturbo died with ``FileNotFoundError`` on
the configured reviewer's name, while ``command -v`` on that same name
answered with a path under ``~/.local/bin``. A pull request on a
reviewed-plus-green repository carried zero reviews, which on the forge
is indistinguishable from a review that has not run yet.

This module is the single place that list of directories lives. Two
copies of it would drift, and the one that drifts is the one nobody is
looking at.
"""

from __future__ import annotations

import os
import shutil

# Where a user-level install puts an executable, in the order a shell
# would find them. Not an attempt to enumerate every layout — the real
# fix is the unit's own PATH, and the installer templates now carry
# `~/.local/bin`. This is the fallback for a daemon whose unit predates
# that, which is every unit already on disk.
USER_INSTALL_DIRS: tuple[str, ...] = (
    "~/.local/bin",
    "~/.local/share/uv/tools",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)


def probe_binary(name: str) -> str | None:
    """Where ``name`` would be found, or ``None``. Logs nothing.

    Split out from :func:`resolve_binary` so a startup check can ask the
    question without emitting a warning that reads as a failed spawn.
    One search, two callers — the alternative was a second copy of the
    directory list, which is what #197 was about.
    """
    if os.sep in name or (os.altsep and os.altsep in name):
        # The operator named a file. Report whether it is usable, rather
        # than searching somewhere else for something by that name.
        if os.path.isfile(name) and os.access(name, os.X_OK):
            return name
        return None

    found = shutil.which(name)
    if found:
        return found

    for directory in USER_INSTALL_DIRS:
        candidate = os.path.join(os.path.expanduser(directory), name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def resolve_binary(name: str, *, logger, event_prefix: str) -> str:
    """An absolute path to ``name``, or ``name`` unchanged.

    Returning the bare name on failure is deliberate. The caller then
    fails exactly as it did before — ``FileNotFoundError`` out of
    ``exec`` — so a genuinely missing tool keeps producing the error
    that names it, rather than a new error about resolution. What
    changes is that the log says so first, at WARNING, before the
    failures start.

    A ``name`` that already contains a separator is returned untouched:
    the operator has said exactly which file they mean, and searching
    for it somewhere else would override them.
    """
    if os.sep in name or (os.altsep and os.altsep in name):
        return name

    on_path = shutil.which(name)
    if on_path:
        # The ordinary case, and not worth a log line.
        return on_path

    found = probe_binary(name)
    if found is not None:
        logger.warning(
            f"{event_prefix}.fallback",
            extra={"binary": found, "reason": "not_on_path"},
        )
        return found

    logger.warning(
        f"{event_prefix}.unresolved",
        extra={"binary": name, "reason": "not_on_path"},
    )
    return name
