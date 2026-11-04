"""GitHub links for the status page (#232).

Every link the JSON carries is built here, from a repository name and a
number, and is a github.com issue or pull request address or ``None``.
Nothing anybody wrote (an issue title, a ``pr_url`` an agent reported) is
ever used as a link: the page refuses anything else again on its side.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

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


# A peer's host, checked by its characters only: letters, digits, dots and
# hyphens, not starting or ending with a dot or hyphen. It is not a full DNS
# or IPv4 check (``999.999.999.999`` passes); the address is the operator's
# own config, and the point is that nothing else (userinfo, brackets,
# percent escapes, spaces) can become a link.
_PEER_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


def peer_page_url(url: Any) -> str | None:
    """The status page of a configured peer: ``<scheme>://<host>[:port]/``,
    or ``None`` when ``url`` is not a plain http(s) address.

    Built only from this machine's own ``fleet.peers`` entry, never from
    anything the peer answered, so a peer cannot choose where its name
    links to. Path, query, fragment and userinfo are dropped or refused:
    the link opens the page's root and nothing else.
    """
    # Printable ASCII only: urlsplit silently drops a tab or newline inside
    # the address, which would turn "node\n-a" into a link to "node-a".
    if not isinstance(url, str) or not all(33 <= ord(ch) < 127 for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    netloc = parts.netloc
    host = netloc.rsplit(":", 1)[0] if port is not None or netloc.endswith(":") else netloc
    # Userinfo ("user@host") and brackets fail the host pattern.
    if not _PEER_HOST.fullmatch(host) or ".." in host:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "") + "/"
