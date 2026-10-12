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
#
# Known gap, named rather than guessed at: a per-user pip install on a
# macOS framework Python puts scripts under `~/Library/Python/X.Y/bin`,
# which is version dependent and so cannot be a fixed entry here. The
# fleet is Linux; globbing for it would be machinery larger than the
# defect.
# Order matters and is the dispatcher's original order, unchanged: this
# helper replaced a private copy there, and a different order is a
# different program on a box that has two. `~/.local/share/uv/tools` is
# NOT here on purpose — that directory holds one subdirectory per tool,
# so a candidate built from it is never a file, and adding it only
# reordered the search.
USER_INSTALL_DIRS: tuple[str, ...] = (
    "~/.local/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)


def _search(name: str) -> tuple[str | None, str]:
    """``(where, how)`` for ``name``. The only search in this module.

    ``how`` is ``"given"``, ``"path"`` or ``"fallback"``, which is what
    lets :func:`resolve_binary` log a fallback without running the
    search a second time. Two searches were not merely wasteful: the
    answers can differ if the filesystem changes between them, and the
    second answer was the one being returned.
    """
    if os.sep in name or (os.altsep and os.altsep in name):
        # The operator named a file. Report on that file, and do not go
        # looking for something of that name anywhere else.
        if os.path.isfile(name) and os.access(name, os.X_OK):
            return name, "given"
        return None, "given"

    on_path = shutil.which(name)
    if on_path:
        return on_path, "path"

    for directory in USER_INSTALL_DIRS:
        candidate = os.path.join(os.path.expanduser(directory), name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate, "fallback"
    return None, "fallback"


def probe_binary(name: str) -> str | None:
    """Where ``name`` would be found, or ``None``. Logs nothing.

    Split out from :func:`resolve_binary` so a startup check can ask the
    question without emitting a warning that reads as a failed spawn.
    One search, several callers — the alternative was a second copy of
    the directory list, which is what #197 was about.
    """
    found, _how = _search(name)
    return found


def resolve_binary(name: str, *, logger, event_prefix: str) -> str:
    """Where to find ``name``, or ``name`` unchanged.

    A name found in one of the fallback directories comes back
    absolute, which is what this function is for. Everything else comes
    back exactly as given — including a name found through a RELATIVE
    ``PATH`` entry, because ``shutil.which`` searched that entry from
    THIS process's directory while ``exec`` would search it from the
    child's, and those are two different programs. Handing the name
    back leaves that case behaving exactly as it did before.

    Returning the bare name on failure is deliberate. The caller then
    fails exactly as it did before — ``FileNotFoundError`` out of
    ``exec`` — so a genuinely missing tool keeps producing the error
    that names it, rather than a new error about resolution. What
    changes is that the log says so first, at WARNING, before the
    failures start.

    A ``name`` that already contains a separator is returned untouched
    — not searched for, and not anchored either. The operator has said
    exactly which file they mean, relative to the directory the child
    will run in, and both searching elsewhere and re-anchoring it here
    would override them.
    """
    found, how = _search(name)

    if found is not None:
        if how == "given":
            # Handed back exactly as written. Rewriting a RELATIVE path
            # the operator gave would resolve it against the daemon's
            # directory when they meant the child's — overriding them
            # with a different file, which is the opposite of honouring
            # a path they named.
            return found

        if how == "path" and not os.path.isabs(found):
            # A relative PATH entry. `shutil.which` searched it from
            # OUR directory; `exec` would search it from the CHILD's,
            # and those are different programs. Measured: with
            # `PATH=bin` and an executable under both, a child started
            # with `cwd=<worktree>` runs the WORKTREE-side one.
            #
            # So hand the name back untouched and let exec do exactly
            # what it did before this change. Rewriting it either way
            # would be this function picking a winner, and the thing
            # #197 is about is a tool that is on no PATH at all.
            return name

        if how == "fallback":
            logger.warning(
                f"{event_prefix}.fallback",
                extra={"binary": found, "reason": "not_on_path"},
            )
        # Every candidate from USER_INSTALL_DIRS is built on an
        # expanduser'd absolute directory, and a `which` hit that
        # reaches here is absolute, so there is nothing left to anchor.
        return found

    if how == "given":
        # Not searched for elsewhere. A miss is still reported — saying
        # nothing was the one loss path that stayed quiet — but only
        # for an ABSOLUTE name, which is the only one this process can
        # answer about. A relative one is resolved by `exec` against
        # the CHILD's working directory, the repo's worktree, so a
        # verdict from here would be about the wrong tree.
        if os.path.isabs(name):
            logger.warning(
                f"{event_prefix}.unresolved",
                extra={"binary": name, "reason": "not_a_usable_file"},
            )
        return name

    logger.warning(
        f"{event_prefix}.unresolved",
        extra={"binary": name, "reason": "not_on_path"},
    )
    return name
