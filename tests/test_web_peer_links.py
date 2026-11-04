"""A peer machine's name links to that machine's status page (#319).

The address comes only from this machine's own ``fleet.peers`` entry; a
peer's answer never decides where its name links to.
"""

from __future__ import annotations

import json
import time

import pytest

from ctrlrelay.web.fleet import PeerReader
from ctrlrelay.web.links import peer_page_url

from .test_fleet import _serve, _status


@pytest.mark.parametrize(("url", "expected"), [
    ("http://node-a:8787", "http://node-a:8787/"),
    ("http://node-c.local:8787/", "http://node-c.local:8787/"),
    ("https://node-b.example", "https://node-b.example/"),
    ("http://192.168.1.20:8787", "http://192.168.1.20:8787/"),
    # A path or query in the config is dropped: the link opens the root.
    ("http://node-b:8787/api/status?x=1#y", "http://node-b:8787/"),
])
def test_a_configured_peer_gives_its_page_root(url: str, expected: str) -> None:
    assert peer_page_url(url) == expected


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "ftp://node-b",
    "file:///etc/passwd",
    "http://user:pw@node-b:8787",
    "http://evil.example@node-b",
    "http://",
    "http://:8787",
    "http://[::1]:8787",
    "http://node b:8787",
    "http://node%2fb:8787",
    "http://node-b:99999",
    "http://node-b:0",
    "http://node-b:x",
    "http://-node:8787",
    "http://node..b:8787",
    " http://node-b",
    "http://node-b\n",
    "http://node\n-a:8787",      # urlsplit drops the newline; the check must not
    "http://no\tde-a:8787",
    "http://node-a:8787\x7f",
    "http://nöde-b",
    "",
    None,
    42,
])
def test_anything_else_gives_no_link(url: object) -> None:
    assert peer_page_url(url) is None


def _entry(reader: PeerReader) -> dict:
    [entry] = reader.snapshot()["peers"]
    return entry


def test_every_peer_state_carries_the_link_from_config() -> None:
    reader = PeerReader(["http://b:8787"], interval_s=60, timeout_s=2, silent_after=300)
    assert _entry(reader)["page_url"] == "http://b:8787/"      # not read yet

    # A peer that answers names itself and offers its own addresses; none
    # of that moves the link.
    status = _status("someone-else", beat=time.time())
    status["page_url"] = "javascript:alert(1)"
    status["url"] = "http://evil.example/"
    status["node"]["page_url"] = "http://evil.example/"
    srv, url = _serve(200, json.dumps(status).encode())
    try:
        reader = PeerReader([url], interval_s=60, timeout_s=2, silent_after=300)
        reader.read_once()
        entry = _entry(reader)
    finally:
        srv.shutdown()
    assert entry["reachable"] is True and entry["name"] == "someone-else"
    assert entry["page_url"] == url + "/"

    # A peer that does not answer still links: its page may be up for a
    # browser even when this machine cannot reach it.
    srv.server_close()
    reader = PeerReader([url], interval_s=60, timeout_s=2, silent_after=300)
    reader.read_once()
    entry = _entry(reader)
    assert entry["reachable"] is False and entry["page_url"] == url + "/"


def test_a_peer_url_that_cannot_link_gives_none_not_a_crash() -> None:
    reader = PeerReader(["http://user:pw@b:8787"], interval_s=60, timeout_s=2,
                        silent_after=300)
    assert _entry(reader)["page_url"] is None
