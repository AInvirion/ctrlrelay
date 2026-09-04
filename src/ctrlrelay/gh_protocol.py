"""Detect the git transport protocol the operator has configured for
GitHub via the ``gh`` CLI.

``ctrlrelay`` clones a handful of GitHub repos on the operator's
behalf (project repos, the personalization repo). Hardcoding HTTPS
for these clones breaks operators who've set ``gh config set
git_protocol ssh`` and have no HTTPS credential helper wired up —
``git`` then fails with "could not read Username" instead of using
the SSH key ``gh`` already knows about.
"""

from __future__ import annotations

import subprocess

_VALID_PROTOCOLS = ("ssh", "https")


def detect_git_protocol(*, default: str = "https") -> str:
    """Return ``"ssh"`` or ``"https"``, per ``gh config get git_protocol``.

    Falls back to ``default`` whenever ``gh`` isn't installed, isn't
    configured, or returns something unexpected — this is a
    convenience read, not a hard requirement, so any failure here
    should degrade to the previous hardcoded-HTTPS behavior rather
    than blocking a clone.
    """
    try:
        proc = subprocess.run(
            ["gh", "config", "get", "git_protocol"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return default
    value = proc.stdout.strip().lower()
    if proc.returncode == 0 and value in _VALID_PROTOCOLS:
        return value
    return default


def github_clone_url(repo: str, *, protocol: str) -> str:
    """Build a clone URL for ``owner/repo`` under the given protocol."""
    if protocol == "ssh":
        return f"git@github.com:{repo}.git"
    return f"https://github.com/{repo}.git"
