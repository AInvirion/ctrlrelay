"""GitHub links for the status page (#232).

Every link the JSON carries is built here, from a repository name and a
number, and is a github.com issue or pull request address or ``None``.
Nothing anybody wrote (an issue title, a ``pr_url`` an agent reported) is
ever used as a link: the page refuses anything else again on its side.
"""

from __future__ import annotations

import re
from typing import Any

# owner/name, each one or more of the characters GitHub allows. fullmatch,
# not ``$``: in Python ``$`` also matches before a trailing newline.
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_KINDS = ("issues", "pull")


def github_url(repo: Any, number: Any, *, kind: str = "issues") -> str | None:
    """``https://github.com/<repo>/<kind>/<number>``, or ``None`` when
    ``repo`` is not ``owner/name`` or ``number`` is not a positive int.

    A segment made only of dots is refused too: a URL resolves it away
    (``github.com/../x`` is ``github.com/x``), so it never names a repo.
    """
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {_KINDS}, not {kind!r}")
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        return None
    if any(set(part) == {"."} for part in repo.split("/")):
        return None
    if type(number) is not int or number < 1:
        return None
    return f"https://github.com/{repo}/{kind}/{number}"
