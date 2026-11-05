"""Config drift between machines on the status page (#307).

The end-to-end tests run two real status servers: node A reads node B's
``/api/status`` through its ``PeerReader``, exactly as in production, and
the drift is read from A's own ``/api/status``.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ctrlrelay import __version__
from ctrlrelay.core.config import Config
from ctrlrelay.web.drift import (
    clean_fingerprint,
    compare,
    config_fingerprint,
    peer_drift,
)
from ctrlrelay.web.fleet import PeerReader
from ctrlrelay.web.server import make_server
from tests.test_fleet import _serve as _serve_raw
from tests.test_web_server import _free_port, _run_js, _tracker

ALPHA = "example-org/alpha"
BETA = "example-org/beta"
GAMMA = "example-org/gamma"
# Values from the config that must never be on the wire.
LABEL = "route-to-node-zeta"
TOKEN = "ghp_FAKEtoken0123456789abcdefABCDEF0123"


def _config(
    tmp_path: Path, name: str, repos: list[dict[str, Any]], **fleet: Any,
) -> Config:
    base = tmp_path / name
    return Config.model_validate({
        "node_id": name,
        "paths": {
            "state_db": str(base / "state.db"),
            "worktrees": str(base / "wt"),
            "bare_repos": str(base / "bare"),
            "contexts": str(base / "ctx"),
            "skills": str(base / "skills"),
            "repo_root": str(base / "src"),
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {"inbox": str(base / "in"), "outbox": str(base / "out")},
        },
        "dashboard": {"enabled": False},
        "repos": repos,
        "web": {"host": "127.0.0.1", "port": _free_port()},
        "fleet": fleet,
    })


def _repo(name: str, *, exclude: list[str] | None = None, review: str | None = None) -> dict:
    automation: dict[str, Any] = {}
    if exclude is not None:
        automation["exclude_labels"] = exclude
    repo: dict[str, Any] = {
        "name": name, "automation": automation,
        # Not fingerprinted, and must not leak either.
        "code_review": {"cli_command": f"review --token {TOKEN}"},
    }
    if review is not None:
        repo["steps"] = {"review": review}
    return repo


@pytest.fixture
def fleet(tmp_path: Path) -> Iterator:
    """Start node B, then node A with B as its peer; return A's status."""
    started: list = []

    def run(a: Config, b: Config) -> dict:
        httpd_b = make_server(b, waiting=_tracker(b, tmp_path / "b"))
        threading.Thread(target=httpd_b.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()
        started.append(httpd_b)
        host, port = httpd_b.server_address[:2]
        reader = PeerReader([f"http://{host}:{port}"], interval_s=60,
                            timeout_s=5, silent_after=300)
        reader.read_once()
        httpd_a = make_server(a, waiting=_tracker(a, tmp_path / "a"), peers=reader)
        threading.Thread(target=httpd_a.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()
        started.append(httpd_a)
        run.server = httpd_a  # type: ignore[attr-defined]
        return _fetch(httpd_a)

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    yield run
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def _fetch(httpd) -> dict:
    host, port = httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        assert resp.status == 200
        return json.loads(resp.read())
    finally:
        conn.close()


def _drift(status: dict) -> dict:
    [peer] = status["fleet"]["peers"]
    assert peer["reachable"] is True, peer
    return peer["drift"]


# --- end to end: two real servers ---------------------------------------------


def test_a_repo_added_to_one_node_shows_as_drift_on_the_other(
    tmp_path: Path, fleet,
) -> None:
    a = _config(tmp_path, "node-a", [_repo(ALPHA)])
    b = _config(tmp_path, "node-b", [_repo(ALPHA), _repo(BETA), _repo(GAMMA)])
    drift = _drift(fleet(a, b))
    assert drift["status"] == "differs"
    assert drift["only_there"] == [BETA, GAMMA]
    assert drift["only_here"] == []
    assert drift["lines"] == [f"2 repos only there: {BETA}, {GAMMA}"]


def test_a_repo_only_on_this_node_is_named_as_only_here(tmp_path: Path, fleet) -> None:
    a = _config(tmp_path, "node-a", [_repo(ALPHA), _repo(BETA)])
    b = _config(tmp_path, "node-b", [_repo(ALPHA)])
    drift = _drift(fleet(a, b))
    assert drift["lines"] == [f"1 repo only here: {BETA}"]


def test_identical_configs_report_nothing(tmp_path: Path, fleet) -> None:
    repos = [_repo(ALPHA, exclude=["manual", LABEL]), _repo(BETA, review="required")]
    a = _config(tmp_path, "node-a", repos)
    b = _config(tmp_path, "node-b", list(reversed(repos)))  # order is not drift
    drift = _drift(fleet(a, b))
    assert drift == {
        "status": "same", "reason": None, "only_here": [], "only_there": [],
        "fields": {}, "lines": [],
    }


def test_a_field_that_differs_is_named_with_its_repos(tmp_path: Path, fleet) -> None:
    a = _config(tmp_path, "node-a", [_repo(ALPHA, exclude=["manual"]),
                                     _repo(BETA, exclude=["manual"], review="off")])
    b = _config(tmp_path, "node-b", [_repo(ALPHA, exclude=["manual", LABEL]),
                                     _repo(BETA, exclude=["manual", LABEL], review="required")])
    drift = _drift(fleet(a, b))
    assert drift["fields"] == {
        "automation.exclude_labels": [ALPHA, BETA], "steps.review": [BETA],
    }
    assert drift["lines"] == [
        f"automation.exclude_labels differs on 2 repos: {ALPHA}, {BETA}",
        f"steps.review differs on 1 repo: {BETA}",
    ]


def test_an_expected_difference_is_not_reported(tmp_path: Path, fleet) -> None:
    # The routing label only node B carries, and a repo only B works on.
    a = _config(tmp_path, "node-a", [_repo(ALPHA, exclude=["manual"])],
                expected_drift=["automation.exclude_labels", BETA.upper()])
    b = _config(tmp_path, "node-b", [_repo(ALPHA, exclude=["manual", LABEL]), _repo(BETA)])
    drift = _drift(fleet(a, b))
    assert drift["status"] == "same" and drift["lines"] == []


def test_an_expected_field_leaves_other_drift_reported(tmp_path: Path, fleet) -> None:
    a = _config(tmp_path, "node-a", [_repo(ALPHA, exclude=["manual"], review="off")],
                expected_drift=["automation.exclude_labels"])
    b = _config(tmp_path, "node-b", [_repo(ALPHA, exclude=["manual", LABEL], review="on")])
    drift = _drift(fleet(a, b))
    assert drift["fields"] == {"steps.review": [ALPHA]}


def test_schedules_are_compared(tmp_path: Path, fleet) -> None:
    a = _config(tmp_path, "node-a", [_repo(ALPHA)])
    b = _config(tmp_path, "node-b", [_repo(ALPHA)])
    b = b.model_copy(update={"schedules": b.schedules.model_copy(
        update={"secops_cron": "0 7 * * *"})})
    drift = _drift(fleet(a, b))
    assert drift["lines"] == ["schedules.secops_cron differs"]


def test_no_config_value_crosses_the_wire(tmp_path: Path, fleet) -> None:
    repos = [_repo(ALPHA, exclude=["manual", LABEL], review="required")]
    a = _config(tmp_path, "node-a", repos)
    b = _config(tmp_path, "node-b", repos + [_repo(BETA, exclude=[LABEL])])
    status = fleet(a, b)
    # Positive control: this node's fingerprint is there, and the peer's was
    # read over HTTP and compared (B's extra repo is named).
    mine = status["node"]["config_fingerprint"]
    assert set(mine["repos"]) == {ALPHA}
    [profile] = mine["profiles"].values()
    assert set(profile["automation"]) >= {"exclude_labels", "dependabot_patch"}
    assert _drift(status)["only_there"] == [BETA]
    body = json.dumps(status)
    assert LABEL not in body
    assert TOKEN not in body
    assert "required" not in json.dumps(mine)  # the steps.review value


def test_a_peers_fingerprint_is_not_echoed_but_still_compared_next_time(
    tmp_path: Path, fleet,
) -> None:
    a = _config(tmp_path, "node-a", [_repo(ALPHA)])
    b = _config(tmp_path, "node-b", [_repo(ALPHA), _repo(BETA)])
    first = fleet(a, b)
    again = _fetch(fleet.server)
    for status in (first, again):
        [peer] = status["fleet"]["peers"]
        assert "config_fingerprint" not in peer["node"]
        assert peer["node"]["name"] == "node-b"
        assert peer["drift"]["only_there"] == [BETA]


def test_a_shared_config_keeps_the_fingerprint_small(tmp_path: Path) -> None:
    repos = [_repo(f"example-org/repo-{i:03d}", exclude=["manual", LABEL]) for i in range(100)]
    fp = config_fingerprint(_config(tmp_path, "a", repos))
    assert len(fp["repos"]) == 100 and len(fp["profiles"]) == 1
    assert len(json.dumps(fp)) < 6000


def _peer_body(name: str, fp: Any, *, node_version: str | None = None) -> bytes:
    node: dict[str, Any] = {
        "name": name, "state": "idle",
        "heartbeat_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if node_version is not None:
        node["version"] = node_version
    if fp is not _NO_FP:
        node["config_fingerprint"] = fp
    return json.dumps({"host": name, "version": "0.22.0", "node": node}).encode()


_NO_FP = object()


def _status_with_raw_peers(tmp_path: Path, a: Config, bodies: list[bytes]) -> dict:
    """Node A's /api/status, with each body served as one peer's answer and
    read through A's PeerReader."""
    peers = [_serve_raw(200, body) for body in bodies]
    (tmp_path / "a").mkdir(exist_ok=True)
    reader = PeerReader([url for _, url in peers], interval_s=60, timeout_s=5,
                        silent_after=300)
    reader.read_once()
    httpd = make_server(a, waiting=_tracker(a, tmp_path / "a"), peers=reader)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    try:
        return _fetch(httpd)
    finally:
        httpd.shutdown()
        httpd.server_close()
        for srv, _ in peers:
            srv.shutdown()


def test_an_old_peer_without_a_fingerprint_is_unknown(tmp_path: Path) -> None:
    a = _config(tmp_path, "node-a", [_repo(ALPHA)])
    status = _status_with_raw_peers(tmp_path, a, [_peer_body("node-b", _NO_FP)])
    [peer] = status["fleet"]["peers"]
    assert peer["reachable"] is True and peer["error"] is None
    assert peer["drift"]["status"] == "unknown"
    assert "no config fingerprint" in peer["drift"]["reason"]
    assert peer["drift"]["lines"] == []
    assert not [e for e in status["errors"] if e.startswith("fleet")]


def test_a_peer_on_another_version_is_unknown_not_drift(tmp_path: Path) -> None:
    """Two versions can hash one config differently (a changed default), so
    a peer on another version is not compared, even with a fingerprint
    that would otherwise read as drift."""
    a = _config(tmp_path, "node-a", [_repo(ALPHA)])
    b = config_fingerprint(_config(tmp_path, "node-b", [_repo(ALPHA), _repo(BETA)]))
    status = _status_with_raw_peers(
        tmp_path, a, [_peer_body("node-b", _wire(b), node_version="0.0.1")],
    )
    [peer] = status["fleet"]["peers"]
    assert peer["drift"]["status"] == "unknown"
    assert "0.0.1" in peer["drift"]["reason"] and __version__ in peer["drift"]["reason"]
    assert peer["drift"]["lines"] == []


def test_a_peer_on_the_same_version_is_compared(tmp_path: Path) -> None:
    """The control for the test above: the same fingerprint, from a peer
    on this machine's version, is compared and shows the drift."""
    a = _config(tmp_path, "node-a", [_repo(ALPHA)])
    b = config_fingerprint(_config(tmp_path, "node-b", [_repo(ALPHA), _repo(BETA)]))
    status = _status_with_raw_peers(
        tmp_path, a, [_peer_body("node-b", _wire(b), node_version=__version__)],
    )
    [peer] = status["fleet"]["peers"]
    assert peer["drift"]["status"] == "differs"
    assert peer["drift"]["lines"] == [f"1 repo only there: {BETA}"]


def test_one_failing_comparison_leaves_the_other_peers_and_the_page(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    from ctrlrelay.web import drift as drift_module

    real = drift_module.compare

    def compare(mine, theirs, expected=()):
        if BETA in theirs["repos"]:
            raise RuntimeError("boom")
        return real(mine, theirs, expected)

    monkeypatch.setattr(drift_module, "compare", compare)
    a = _config(tmp_path, "node-a", [_repo(ALPHA)])
    bad = config_fingerprint(_config(tmp_path, "b", [_repo(ALPHA), _repo(BETA)]))
    good = config_fingerprint(_config(tmp_path, "c", [_repo(ALPHA), _repo(GAMMA)]))
    bodies = [_peer_body("node-b", bad), _peer_body("node-c", good)]
    peers = [_serve_raw(200, body) for body in bodies]
    (tmp_path / "a").mkdir()
    reader = PeerReader([url for _, url in peers], interval_s=60, timeout_s=5,
                        silent_after=300)
    reader.read_once()
    httpd = make_server(a, waiting=_tracker(a, tmp_path / "a"), peers=reader)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    try:
        with caplog.at_level(logging.WARNING, logger="ctrlrelay"):
            first, second = _fetch(httpd), _fetch(httpd)
    finally:
        httpd.shutdown()
        httpd.server_close()
        for srv, _ in peers:
            srv.shutdown()
    for status in (first, second):
        b_peer, c_peer = status["fleet"]["peers"]
        assert b_peer["drift"]["status"] == "unknown"
        assert b_peer["drift"]["reason"] == "could not compare: RuntimeError"
        assert b_peer["drift"]["lines"] == []
        assert "config_fingerprint" not in b_peer["node"]
        assert c_peer["drift"]["lines"] == [f"1 repo only there: {GAMMA}"]
        assert status["node"]["name"] == "node-a"
    logged = [r for r in caplog.records if r.getMessage() == "web.drift_compare_failed"]
    assert len(logged) == 1


# --- the pieces -----------------------------------------------------------------


def test_the_hash_does_not_depend_on_key_order() -> None:
    from ctrlrelay.web.drift import _hash

    assert _hash({"a": 1, "b": {"c": [1, 2], "d": None}}) == _hash(
        {"b": {"d": None, "c": [1, 2]}, "a": 1}
    )
    assert _hash({"a": 1}) != _hash({"a": 2})


def test_a_peer_not_read_or_not_reachable_is_unknown(tmp_path: Path) -> None:
    mine = config_fingerprint(_config(tmp_path, "a", [_repo(ALPHA)]))
    assert peer_drift({"reachable": None}, mine)["reason"] == "not read yet"
    down = peer_drift({"reachable": False, "node": None}, mine)
    assert down["status"] == "unknown" and down["reason"] == "not reachable from here"


def _rekey(fp: dict) -> dict:
    """Recompute a fingerprint's profile keys and repos_hash after editing
    a profile, so it is a valid fingerprint of the edited content."""
    from ctrlrelay.web.drift import _hash

    old_to_new = {}
    profiles = {}
    for key, profile in fp["profiles"].items():
        old_to_new[key] = _hash(profile)
        profiles[old_to_new[key]] = profile
    fp["profiles"] = profiles
    fp["repos"] = {name: old_to_new[key] for name, key in fp["repos"].items()}
    fp["repos_hash"] = _hash(sorted(n.lower() for n in fp["repos"]))
    return fp


def _wire(fp: dict) -> dict:
    return json.loads(json.dumps(fp))


def test_a_field_only_one_side_has_is_not_drift(tmp_path: Path) -> None:
    mine = config_fingerprint(_config(tmp_path, "a", [_repo(ALPHA)]))
    theirs = _wire(mine)
    [profile] = theirs["profiles"].values()
    profile["automation"]["a_newer_field"] = "0" * 16
    del theirs["schedules"]["weekly_summary"]
    clean = clean_fingerprint(_rekey(theirs))
    assert clean is not None
    assert compare(mine, clean)["status"] == "same"


def _profile(fp: dict) -> dict:
    return next(iter(fp["profiles"].values()))


def _many_profiles(fp: dict) -> None:
    from ctrlrelay.web.drift import _hash

    for i in range(5001):
        profile = {"automation": {"f": _hash(i)}, "steps": {}}
        fp["profiles"][_hash(profile)] = profile


def _short_field_hash(fp: dict) -> None:
    _profile(fp)["automation"]["exclude_labels"] = "00000000"
    _rekey(fp)


MALFORMED = {
    "the old format": lambda fp: fp.update(v=1),
    "no repos, any repos_hash": lambda fp: fp.update(repos={}),
    "a repo renamed, repos_hash kept": lambda fp: fp.update(
        repos={"other/name": next(iter(fp["repos"].values()))}),
    "no profiles": lambda fp: fp.pop("profiles"),
    "a repo naming no profile": lambda fp: fp["repos"].update({ALPHA: "f" * 16}),
    "a profile edited, key kept": lambda fp: _profile(fp)["automation"].update(
        exclude_labels="0" * 16),
    "a profile missing a section": lambda fp: _profile(fp).pop("steps"),
    # Rekeyed, so the profile key agrees and only the hash's shape is wrong.
    "a hash that is not one, rekeyed": lambda fp: (
        _profile(fp)["automation"].update(exclude_labels="manual"), _rekey(fp)),
    # repos_hash recomputed, so only the name's shape is wrong.
    "a repo name that is not owner/name": lambda fp: (
        fp.update(repos={"not a repo": next(iter(fp["repos"].values()))}),
        _rekey(fp)),
    "a schedule that is not a hash": lambda fp: fp["schedules"].update(secops_cron=7),
    "too many profiles": _many_profiles,
    "a schedule hash of 8 characters": lambda fp: fp["schedules"].update(
        secops_cron="00000000"),
    "a schedule hash with a newline": lambda fp: fp["schedules"].update(
        secops_cron="0" * 16 + "\n"),
    "a schedule hash of 32 characters": lambda fp: fp["schedules"].update(
        secops_cron="0" * 32),
    # Rekeyed, so the profile key agrees and only the hash's length is wrong.
    "a field hash of 8 characters, rekeyed": _short_field_hash,
}


def test_a_well_formed_peer_fingerprint_is_compared(tmp_path: Path) -> None:
    """The control for the malformed cases below: the same fingerprint,
    unmutated, through the same path, is compared."""
    a = _config(tmp_path, "node-a", [_repo(ALPHA)])
    fp = _wire(config_fingerprint(_config(tmp_path, "b", [_repo(ALPHA), _repo(BETA)])))
    [peer] = _status_with_raw_peers(tmp_path, a, [_peer_body("node-b", fp)])["fleet"]["peers"]
    assert peer["drift"]["lines"] == [f"1 repo only there: {BETA}"]


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_a_malformed_fingerprint_is_unknown_not_drift(tmp_path: Path, name: str) -> None:
    a = _config(tmp_path, "node-a", [_repo(ALPHA)])
    fp = _wire(config_fingerprint(_config(tmp_path, "b", [_repo(ALPHA), _repo(BETA)])))
    MALFORMED[name](fp)
    [peer] = _status_with_raw_peers(tmp_path, a, [_peer_body("node-b", fp)])["fleet"]["peers"]
    assert peer["reachable"] is True
    assert peer["drift"]["status"] == "unknown"
    assert peer["drift"]["lines"] == []
    assert peer["drift"]["only_here"] is None
    # Refused when read, not by the comparison failing on it.
    assert peer["drift"]["reason"].startswith("it sends no config fingerprint")


def test_an_honest_empty_repo_list_is_compared(tmp_path: Path) -> None:
    a = _config(tmp_path, "node-a", [_repo(ALPHA)])
    fp = _wire(config_fingerprint(_config(tmp_path, "b", [])))
    assert fp["repos"] == {} and fp["profiles"] == {}
    [peer] = _status_with_raw_peers(tmp_path, a, [_peer_body("node-b", fp)])["fleet"]["peers"]
    assert peer["drift"]["lines"] == [f"1 repo only here: {ALPHA}"]


@pytest.mark.parametrize("entry", [
    "automation.exclude_label",   # a typo
    "automation.",
    "repos",
    "agent.binary",
    "not a repo/at all",
    "example-org/beta\n",   # a trailing newline
])
def test_expected_drift_refuses_what_it_cannot_compare(tmp_path: Path, entry: str) -> None:
    with pytest.raises(ValidationError, match="expected_drift"):
        _config(tmp_path, "a", [], expected_drift=[entry])


def test_expected_drift_accepts_repos_sections_and_fields(tmp_path: Path) -> None:
    entries = [ALPHA, "automation", "steps.review", "schedules.secops_cron",
               "automation.exclude_labels"]
    assert _config(tmp_path, "a", [], expected_drift=entries).fleet.expected_drift == entries


# --- the page -------------------------------------------------------------------


def test_page_shows_drift_lines_and_nothing_when_the_same() -> None:
    differs = {"status": "differs", "lines": ["2 repos only there: a/b, a/c"]}
    assert _run_js(["driftLines"], f"driftLines({json.dumps(differs)})") == [
        "config: 2 repos only there: a/b, a/c",
    ]
    same = {"status": "same", "lines": []}
    assert _run_js(["driftLines"], f"driftLines({json.dumps(same)})") == []


@pytest.mark.parametrize("drift,expected", [
    ({"status": "unknown", "reason": "it sends no config fingerprint this version can read",
      "lines": []},
     ["config drift unknown: it sends no config fingerprint this version can read"]),
    (None, ["config drift unknown"]),   # a server from before #307
])
def test_page_shows_an_old_peer_as_unknown(drift, expected) -> None:
    assert _run_js(["driftLines"], f"driftLines({json.dumps(drift)})") == expected


_FAKE_DOM = """
function Node(tag) { this.tag = tag; this.className = ""; this.textContent = "";
  this.children = []; }
Node.prototype.appendChild = function (c) { this.children.push(c); return c; };
Node.prototype.removeChild = function (c) { this.children.splice(this.children.indexOf(c), 1); };
Object.defineProperty(Node.prototype, "firstChild",
  { get: function () { return this.children[0] || null; } });
var ids = {};
var document = {
  createElement: function (tag) { return new Node(tag); },
  getElementById: function (id) { return ids[id] || (ids[id] = new Node("div")); },
};
function texts(n) { return [n.textContent].concat(
  [].concat.apply([], n.children.map(texts))).filter(function (t) { return t; }); }
function ago(iso, now) { return "1m ago"; }
function renderWho() {}
"""


def _render_fleet(peers: list[dict]) -> list[dict]:
    """Run the page's own renderFleet in node over a fake DOM and return
    each machine card's class and texts."""
    import re
    import shutil
    import subprocess

    from tests.test_web_server import _js_function, _script

    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("node is not installed, so the page's script cannot run here")
    script = _script()
    defs = []
    for name in ("el", "put", "safePeerUrl", "peerLink", "pct", "usagePct",
                 "machineCard", "versionDiffers", "isBad", "driftLines", "renderFleet"):
        m = re.search(r"\bfunction\s+" + name + r"\s*\(([^)]*)\)", script)
        assert m, name
        defs.append(f"function {name}({m.group(1)}) {{{_js_function(script, name)}}}")
    consts = "\n".join(re.findall(r"^var (?:PEER_URL|BAD_STATES) = .*;$", script, re.M))
    assert consts.count("var ") == 2
    data = {"version": "0.23.0", "host": "node-a",
            "node": {"name": "node-a", "state": "idle", "version": "0.23.0"},
            "fleet": {"peers": peers}}
    program = "\n".join([
        _FAKE_DOM, consts, *defs,
        f"renderFleet({json.dumps(data)}, 1793490000);",
        "process.stdout.write(JSON.stringify(ids['fleet-strip'].children.map("
        "function (c) { return {cls: c.className, texts: texts(c)}; })));",
    ])
    done = subprocess.run([node, "-e", program], capture_output=True, text=True,
                          timeout=30, check=True)
    return json.loads(done.stdout)


def _peer(drift: dict | None) -> dict:
    return {"url": "http://node-b:8787", "name": "node-b", "reachable": True,
            "state": "idle", "page_url": "http://node-b:8787/", "drift": drift,
            "node": {"name": "node-b", "state": "idle", "version": "0.23.0",
                     "heartbeat_at": "2026-11-05T00:00:00Z"}}


def test_render_fleet_marks_a_drifted_peer_and_lists_its_lines() -> None:
    drift = {"status": "differs", "lines": [f"1 repo only there: {BETA}"]}
    me, peer = _render_fleet([_peer(drift)])
    assert me["cls"] == "machine"
    assert peer["cls"] == "machine bad"
    assert f"config: 1 repo only there: {BETA}" in peer["texts"]


def test_render_fleet_shows_nothing_for_the_same_config() -> None:
    _, peer = _render_fleet([_peer({"status": "same", "lines": []})])
    assert peer["cls"] == "machine"
    assert not [t for t in peer["texts"] if "config" in t]
    assert "node-b" in peer["texts"]  # the card itself was rendered


def test_render_fleet_shows_an_unknown_comparison_as_unknown() -> None:
    drift = {"status": "unknown", "reason": "it sends no config fingerprint this version can read",
             "lines": []}
    _, peer = _render_fleet([_peer(drift)])
    assert peer["cls"] == "machine"
    assert ("config drift unknown: it sends no config fingerprint this version can read"
            in peer["texts"])
