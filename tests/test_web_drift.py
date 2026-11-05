"""Config drift between machines on the status page (#307).

The end-to-end tests run two real status servers: node A reads node B's
``/api/status`` through its ``PeerReader``, exactly as in production, and
the drift is read from A's own ``/api/status``.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

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
    # Positive control: the fingerprints are there, both this node's and the
    # peer's as read over HTTP, and the peer's carries B's extra repo.
    mine = status["node"]["config_fingerprint"]
    theirs = status["fleet"]["peers"][0]["node"]["config_fingerprint"]
    assert set(mine["repos"]) == {ALPHA}
    assert set(theirs["repos"]) == {ALPHA, BETA}
    assert set(theirs["repos"][ALPHA]["automation"]) >= {"exclude_labels", "dependabot_patch"}
    body = json.dumps(status)
    assert LABEL not in body
    assert TOKEN not in body
    assert "required" not in json.dumps(theirs)  # the steps.review value


def test_an_old_peer_without_a_fingerprint_is_unknown(tmp_path: Path) -> None:
    old = {"host": "node-b", "version": "0.22.0",
           "node": {"name": "node-b", "state": "idle",
                    "heartbeat_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}
    srv, url = _serve_raw(200, json.dumps(old).encode())
    a = _config(tmp_path, "node-a", [_repo(ALPHA)])
    (tmp_path / "a").mkdir()
    reader = PeerReader([url], interval_s=60, timeout_s=5, silent_after=300)
    reader.read_once()
    httpd = make_server(a, waiting=_tracker(a, tmp_path / "a"), peers=reader)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    try:
        status = _fetch(httpd)
    finally:
        httpd.shutdown()
        httpd.server_close()
        srv.shutdown()
    [peer] = status["fleet"]["peers"]
    assert peer["reachable"] is True and peer["error"] is None
    assert peer["drift"]["status"] == "unknown"
    assert "no config fingerprint" in peer["drift"]["reason"]
    assert peer["drift"]["lines"] == []
    assert not [e for e in status["errors"] if e.startswith("fleet")]


# --- the pieces -----------------------------------------------------------------


def test_the_hash_does_not_depend_on_key_order(tmp_path: Path) -> None:
    a = _config(tmp_path, "a", [_repo(ALPHA, exclude=["x"])])
    b = Config.model_validate(
        json.loads(json.dumps(a.model_dump(mode="json"), sort_keys=True))
    )
    assert config_fingerprint(a) == config_fingerprint(b)


def test_a_peer_not_read_or_not_reachable_is_unknown(tmp_path: Path) -> None:
    mine = config_fingerprint(_config(tmp_path, "a", [_repo(ALPHA)]))
    assert peer_drift({"reachable": None}, mine)["reason"] == "not read yet"
    down = peer_drift({"reachable": False, "node": None}, mine)
    assert down["status"] == "unknown" and down["reason"] == "not reachable from here"


def test_a_field_only_one_version_has_is_not_drift(tmp_path: Path) -> None:
    mine = config_fingerprint(_config(tmp_path, "a", [_repo(ALPHA)]))
    theirs = json.loads(json.dumps(mine))
    theirs["repos"][ALPHA]["automation"]["a_newer_field"] = "0" * 16
    del theirs["schedules"]["weekly_summary"]
    assert compare(mine, clean_fingerprint(theirs))["status"] == "same"


@pytest.mark.parametrize("mutate", [
    lambda fp: fp.update(v=2),
    lambda fp: fp.update(repos=[]),
    lambda fp: fp["repos"][ALPHA].pop("steps"),
    lambda fp: fp["repos"][ALPHA]["automation"].update(exclude_labels="manual"),
    lambda fp: fp["schedules"].update(secops_cron=7),
    lambda fp: fp.update(repos_hash=None),
])
def test_a_malformed_fingerprint_is_unknown_not_drift(tmp_path: Path, mutate) -> None:
    fp = json.loads(json.dumps(config_fingerprint(_config(tmp_path, "a", [_repo(ALPHA)]))))
    assert clean_fingerprint(fp) is not None
    mutate(fp)
    assert clean_fingerprint(fp) is None


@pytest.mark.parametrize("entry", [
    "automation.exclude_label",   # a typo
    "automation.",
    "repos",
    "agent.binary",
    "not a repo/at all",
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
    ({"status": "unknown", "reason": "it sends no config fingerprint (an older version?)",
      "lines": []},
     ["config drift unknown: it sends no config fingerprint (an older version?)"]),
    (None, ["config drift unknown"]),   # a server from before #307
])
def test_page_shows_an_old_peer_as_unknown(drift, expected) -> None:
    assert _run_js(["driftLines"], f"driftLines({json.dumps(drift)})") == expected


def test_render_fleet_marks_a_drifted_peer() -> None:
    from tests.test_web_server import _js_function, _script

    body = _js_function(_script(), "renderFleet")
    assert 'p.drift.status === "differs"' in body
    assert "|| drifted" in body
    assert "driftLines(p.drift)" in body
