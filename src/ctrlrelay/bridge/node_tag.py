"""Which machine a chat message is from, and which one it is for (#248).

With several machines on one chat bot, every machine hears every message.
Each machine starts what it posts with its name in brackets::

    [node-a] ❓ Blocked on o/r#7: which table?

and acts only on replies to its own posts. A reply to a post tagged with
another machine's name is that machine's business and is ignored quietly.

A message that is not a reply can say which machine it is for::

    node-a: retry the sweep
    @node-a retry the sweep

When the chat is shared (``shared: true``), a message that names no machine
is not acted on by any: there would be no telling which one should.
"""

from __future__ import annotations

import re

from ctrlrelay.core.claims import safe_node_name

_TAG = re.compile(r"^\s*\[(?P<node>[A-Za-z0-9._%-]{1,64})\]")
_ADDRESS = re.compile(r"^\s*@?(?P<node>[A-Za-z0-9._%-]{1,64})(?::|,|\s)\s*(?P<rest>.*)\Z", re.S)


def tag(node: str, text: str) -> str:
    """``text`` as this machine posts it."""
    return f"[{safe_node_name(node)}] {text}"


def tagged_node(text: str | None) -> str | None:
    """The machine a post names in its leading tag, or ``None``."""
    if not text:
        return None
    m = _TAG.match(text)
    return m.group("node") if m else None


def addressed(text: str, node: str) -> tuple[bool, str]:
    """Whether a fresh message is addressed to ``node``, and its text
    without the address. A message addressed to another machine, or to
    none, is ``(False, text)``."""
    m = _ADDRESS.match(text)
    if m and m.group("node") == safe_node_name(node):
        return True, m.group("rest").strip()
    return False, text
