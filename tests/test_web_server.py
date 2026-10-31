"""Tests for the status page's HTTP server and page (#209).

Every server here binds the configured host and port, in a thread, over a
real ``state.db`` built by ``StateDB``. The waiting line is the real
``WaitingTracker`` over a stand-in GitHub, so titles reach the page the way
they do in production: from ``tracker.titles()``.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import pytest

from ctrlrelay.core.config import Config, WebConfig
from ctrlrelay.core.state import StateDB
from ctrlrelay.web import server as server_module
from ctrlrelay.web.server import (
    CSP,
    WaitingRefresher,
    bind_warning,
    make_server,
)
from ctrlrelay.web.status import FEED_LIMIT, make_sessions_reader
from ctrlrelay.web.transcript import transcript_dir_for
from ctrlrelay.web.waiting import WaitingTracker

ALPHA = "example-org/alpha"
HOSTILE_TITLE = "<img src=x onerror=alert(1)>"
HOSTILE_FILE = '"><script>alert(1)<'
TOP_KEYS = {
    "version", "host", "generated", "generated_ts", "running", "needs_you",
    "waiting", "skipped", "server", "today", "feed", "feed_total",
    "feed_truncated", "feed_limit", "errors",
}


# --- helpers ----------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(tmp_path: Path, *, db: Path | None = None, **web) -> Config:
    return Config.model_validate({
        "node_id": "test-node",
        "paths": {
            "state_db": str(db or tmp_path / "state.db"),
            "worktrees": str(tmp_path / "wt"),
            "bare_repos": str(tmp_path / "bare"),
            "contexts": str(tmp_path / "ctx"),
            "skills": str(tmp_path / "skills"),
            "repo_root": str(tmp_path / "src"),
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {"inbox": str(tmp_path / "in"), "outbox": str(tmp_path / "out")},
        },
        "dashboard": {"enabled": False},
        "repos": [{"name": ALPHA, "automation": {"accept_foreign_assignments": True}}],
        "web": {"host": "127.0.0.1", "port": _free_port(), **web},
    })


class FakeGitHub:
    """Answers the three calls the tracker makes, read only."""

    def __init__(self, issues: list[dict]) -> None:
        self.issues = issues

    async def current_login(self) -> str:
        return "octo"

    async def search_assigned_issues(self, assignee: str, *, limit: int = 1000) -> list[dict]:
        return [dict(i) for i in self.issues]

    async def list_issues_by_label(self, repo: str, *, label: str) -> list[dict]:
        return []


def _issue(number: int, title: str) -> dict:
    return {
        "repo": ALPHA, "repository": {"nameWithOwner": ALPHA}, "number": number,
        "title": title, "labels": [], "assignees": [{"login": "octo"}],
        "updatedAt": "2026-10-30T00:00:00Z",
    }


def _tracker(config: Config, tmp_path: Path, issues: list[dict] | None = None) -> WaitingTracker:
    tracker = WaitingTracker(
        config, FakeGitHub(issues or []),  # type: ignore[arg-type]
        sessions=make_sessions_reader(config.paths.state_db),
        poller_state_file=tmp_path / "poller_state.json",
    )
    return tracker


def _hostile_db(tmp_path: Path) -> Path:
    """A running session on #215 whose transcript last edited a file with a
    hostile name, so its last command carries markup."""
    db = StateDB(tmp_path / "state.db")
    wt = str(tmp_path / ".ctrlrelay" / "worktrees" / "alpha-215")
    d = transcript_dir_for(wt, tmp_path)
    d.mkdir(parents=True)
    stamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    row = {
        "type": "assistant", "timestamp": stamp,
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "x", "name": "Edit",
            "input": {"file_path": f"{wt}/{HOSTILE_FILE}"},
        }]},
    }
    (d / "t.jsonl").write_text(json.dumps(row) + "\n")
    db.execute(
        """INSERT INTO sessions
           (id, pipeline, repo, worktree_path, status, started_at, issue_number)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("s-215", "dev", ALPHA, wt, "running", int(time.time()) - 60, 215),
    )
    db.commit()
    db.close()
    return tmp_path / "state.db"


class Served:
    def __init__(self, httpd) -> None:
        self.httpd = httpd
        self.host, self.port = httpd.server_address[:2]

    def request(self, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
        finally:
            conn.close()

    def json(self, path: str) -> tuple[int, dict]:
        status, headers, body = self.request("GET", path)
        assert headers["content-type"].startswith("application/json")
        return status, json.loads(body)


@pytest.fixture
def serve() -> Iterator:
    started: list = []

    def _serve(config: Config, tracker, **kw) -> Served:
        httpd = make_server(config, waiting=tracker, **kw)
        t = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )
        t.start()
        started.append((httpd, t))
        return Served(httpd)

    yield _serve
    for httpd, t in started:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _digest(path: Path) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for p in (path, Path(f"{path}-wal")):
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    return out


# --- the page -----------------------------------------------------------------


def _js_function(script: str, name: str) -> str:
    """The body of ``function <name>(...) {...}``, braces matched past string
    literals and comments. Raises when the function is missing, so a rename
    fails loudly instead of handing back an empty body."""
    m = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", script)
    if m is None:
        raise AssertionError(f"function {name} not found in the page")
    i, depth = m.end(), 1
    while depth:
        if i >= len(script):
            raise AssertionError(f"function {name} never closes")
        ch = script[i]
        if ch in "\"'":
            j = i + 1
            while script[j] != ch:
                j += 2 if script[j] == "\\" else 1
            i = j + 1
            continue
        if script.startswith("//", i):
            i = script.index("\n", i)
            continue
        if script.startswith("/*", i):
            i = script.index("*/", i) + 2
            continue
        depth += {"{": 1, "}": -1}.get(ch, 0)
        i += 1
    return script[m.end(): i - 1]


def _script() -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", _page_source(), re.S))


def test_null_section_counts_read_unknown() -> None:
    """A section the server could not read is null, and its count in the
    heading must say "unknown", never blank and never 0. Every count is
    written by setCount; setCount writes "unknown" for anything that is not
    a number; each renderer passes null on its unreadable path."""
    script = _script()
    set_count = _js_function(script, "setCount")
    assert re.fullmatch(
        r'\s*document\.getElementById\(id\)\.textContent\s*=\s*'
        r'typeof value === "number" \? String\(value\) : "unknown";\s*',
        set_count,
    ), set_count
    for renderer, count_id in (("renderRunning", "running-count"),
                               ("renderNeeds", "needs-count"),
                               ("renderWaiting", "waiting-count")):
        body = _js_function(script, renderer)
        calls = re.findall(r'setCount\("' + count_id + r'",\s*([^;]*)\);', body)
        assert calls, (renderer, "never sets its count")
        assert any(c.strip() == "null" for c in calls), (renderer, calls)
    # Nothing but setCount writes a count element: every mention of a
    # count id in the script is the first argument of a setCount call.
    for count_id in ("running-count", "needs-count", "waiting-count"):
        mentions = script.count(f'"{count_id}"')
        assert mentions >= 2, count_id
        assert script.count(f'setCount("{count_id}",') == mentions, count_id
    # Today: a count that is not a number is drawn as "unknown".
    today = _js_function(script, "renderToday")
    assert re.search(r'else\s*\{\s*cell\.appendChild\(el\("div", "n unknown", "unknown"\)\)', today)


def _page_source() -> str:
    return (
        resources.files("ctrlrelay.web").joinpath("static/index.html")
        .read_text(encoding="utf-8")
    )


def test_index_served_with_no_external_urls(tmp_path: Path, serve) -> None:
    StateDB(tmp_path / "state.db").close()
    config = _config(tmp_path)
    srv = serve(config, _tracker(config, tmp_path))

    status, headers, body = srv.request("GET", "/")

    assert status == 200
    assert headers["content-type"].startswith("text/html")
    html = body.decode()
    # Present case first, so the absence below is about the real page.
    assert "/api/status" in html
    assert "http://" not in html and "https://" not in html
    # Protocol-relative loads are external too.
    assert not re.search(r"""(src|href)\s*=\s*["']//""", html)
    assert "@import" not in html


def test_security_headers_on_every_response(tmp_path: Path, serve) -> None:
    StateDB(tmp_path / "state.db").close()
    config = _config(tmp_path)
    srv = serve(config, _tracker(config, tmp_path))

    for method, path in (("GET", "/"), ("GET", "/api/status"), ("GET", "/nope"),
                         ("POST", "/api/status"), ("GET", "/api/events?since=x")):
        _, headers, _ = srv.request(method, path)
        assert headers["x-content-type-options"] == "nosniff", (method, path)
        assert headers["content-security-policy"] == CSP, (method, path)
    assert CSP == (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'"
    )


def test_index_carries_refresh_seconds(tmp_path: Path, serve) -> None:
    StateDB(tmp_path / "state.db").close()
    config = _config(tmp_path, refresh_seconds=7)
    srv = serve(config, _tracker(config, tmp_path))

    _, _, body = srv.request("GET", "/")

    assert re.search(r'data-refresh-seconds="7"', body.decode())


def test_page_source_has_no_html_parsing_dom_sinks() -> None:
    """A source scan, not a rendering test: the page file contains none of
    the DOM APIs that parse a string as HTML. Titles come from GitHub, where
    anyone can file on a public repo, so the page must build every node with
    textContent/createElement. That the hostile values reach the JSON as
    plain strings is test_api_status_contract."""
    html = _page_source()
    script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    # Present case: the page really does build its DOM in this script.
    assert "textContent" in script and "createElement" in script
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                 "DOMParser", "createContextualFragment", "eval(", "new Function",
                 "setAttribute(\"on", "srcdoc"):
        assert sink not in html, sink


def test_api_status_contract(tmp_path: Path, serve) -> None:
    db = _hostile_db(tmp_path)
    config = _config(tmp_path, db=db)
    tracker = _tracker(config, tmp_path, issues=[_issue(215, HOSTILE_TITLE)])
    asyncio.run(tracker.refresh())
    srv = serve(config, tracker, home=tmp_path)

    status, headers, body = srv.request("GET", "/api/status")

    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert headers["cache-control"] == "no-store"
    out = json.loads(body)
    assert set(out) == TOP_KEYS
    assert out["host"] == "test-node"
    assert out["errors"] == []
    [card] = out["running"]
    # The title is only in titles(): #215 is running, so the waiting line
    # drops it. Passing only the snapshot would leave this None.
    assert out["waiting"]["items"] == []
    assert card["title"] == HOSTILE_TITLE
    assert card["last_command"] == HOSTILE_FILE
    assert out["waiting"]["status"] == "ok"
    assert card["url"] == f"https://github.com/{ALPHA}/issues/215"
    # Server health is measured for real, on this machine.
    assert [d["name"] for d in out["server"]["disks"]] == ["home", "worktrees"]
    assert out["skipped"] == []


def test_api_status_uses_web_settings(tmp_path: Path, serve, monkeypatch) -> None:
    StateDB(tmp_path / "state.db").close()
    config = _config(tmp_path, stale_after_minutes=3, history_days=2)
    seen: dict = {}

    def fake_build_status(**kw):
        seen.update(kw)
        return {"ok": True}

    monkeypatch.setattr(server_module, "build_status", fake_build_status)
    srv = serve(config, _tracker(config, tmp_path))
    srv.json("/api/status")
    assert seen["stale_after_s"] == 180
    assert seen["history_days"] == 2
    assert seen["db_path"] == config.paths.state_db
    assert seen["host"] == "test-node"
    # The health reader measures the home folder and the configured
    # worktrees folder, and is handed over uncalled, so build_status can
    # turn a failure into a null section.
    disks = seen["server"]()["disks"]
    assert [(d["name"], d["path"]) for d in disks] == [
        ("home", str(Path.home())), ("worktrees", str(config.paths.worktrees)),
    ]


def test_status_error_is_200_with_errors(tmp_path: Path, serve) -> None:
    config = _config(tmp_path, db=tmp_path / "missing" / "state.db")
    srv = serve(config, _tracker(config, tmp_path))

    status, out = srv.json("/api/status")

    assert status == 200
    assert out["running"] is None and out["needs_you"] is None and out["feed"] is None
    assert all(v is None for v in out["today"].values())
    assert any("state.db" in e for e in out["errors"])
    assert not (tmp_path / "missing").exists()


# --- events -------------------------------------------------------------------


def _events_db(tmp_path: Path) -> Path:
    db = StateDB(tmp_path / "state.db")
    now = int(time.time())
    for i, kind in enumerate(("dispatched", "pr_opened", "merged")):
        db.record_event(kind, repo=ALPHA, issue_number=215, ts=now - 300 + i * 100)
    db.record_event("dispatched", repo=ALPHA, ts=now - 30 * 86_400)
    db.close()
    return tmp_path / "state.db"


def test_api_events_since(tmp_path: Path, serve) -> None:
    _events_db(tmp_path)
    config = _config(tmp_path)
    srv = serve(config, _tracker(config, tmp_path))
    now = int(time.time())

    status, out = srv.json(f"/api/events?since={now - 250}")

    assert status == 200
    assert out["error"] is None
    assert [e["kind"] for e in out["events"]] == ["merged", "pr_opened"]


def test_api_events_without_since_is_the_history_window(tmp_path: Path, serve) -> None:
    _events_db(tmp_path)
    config = _config(tmp_path, history_days=7)
    srv = serve(config, _tracker(config, tmp_path))

    status, out = srv.json("/api/events")

    assert status == 200
    # The 30-day-old row is outside the window; the three recent ones are in.
    assert [e["kind"] for e in out["events"]] == ["merged", "pr_opened", "dispatched"]
    assert out["feed_total"] == 3
    assert out["feed_truncated"] is False
    assert out["limit"] == FEED_LIMIT


def _feed_note(data: dict) -> object:
    """Run the page's own feedNote on ``data`` in node."""
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("node is not installed, so the page's script cannot run here")
    body = _js_function(_script(), "feedNote")
    program = (
        "function feedNote(data) {" + body + "}\n"
        "process.stdout.write(JSON.stringify(feedNote(" + json.dumps(data) + ")));"
    )
    done = subprocess.run([node, "-e", program], capture_output=True, text=True,
                          timeout=30, check=True)
    return json.loads(done.stdout)


def test_page_says_when_the_feed_was_cut() -> None:
    feed = [{"ts": 1, "kind": "merged"}] * 3
    assert _feed_note({"feed": feed, "feed_total": 250, "feed_truncated": True}) == (
        "showing newest 3 of 250"
    )
    assert _feed_note({"feed": feed, "feed_total": 3, "feed_truncated": False}) is None
    assert _feed_note({"feed": None, "feed_total": None, "feed_truncated": False}) is None
    # The renderer shows it.
    render = _js_function(_script(), "renderFeed")
    assert "feedNote(data)" in render


@pytest.mark.parametrize("query", ["since=abc", "since=", "since=1.5", "since=1&since=2"])
def test_api_events_bad_since_is_400(tmp_path: Path, serve, query: str) -> None:
    _events_db(tmp_path)
    config = _config(tmp_path)
    srv = serve(config, _tracker(config, tmp_path))

    status, out = srv.json(f"/api/events?{query}")

    assert status == 400
    assert "since" in out["error"]


# --- routing ------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/nope", "/api/status/x", "/index.html", "/api"])
def test_unknown_path_404(tmp_path: Path, serve, path: str) -> None:
    StateDB(tmp_path / "state.db").close()
    config = _config(tmp_path)
    srv = serve(config, _tracker(config, tmp_path))

    status, out = srv.json(path)

    assert status == 404
    assert out == {"error": "not found"}


def test_post_put_delete_are_405(tmp_path: Path, serve) -> None:
    _events_db(tmp_path)
    config = _config(tmp_path)
    srv = serve(config, _tracker(config, tmp_path))
    before = _digest(config.paths.state_db)

    # A GET first, so the database has been opened by the server.
    assert srv.request("GET", "/api/status")[0] == 200
    for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "BREW"):
        for path in ("/", "/api/status", "/api/events"):
            status, headers, body = srv.request(method, path)
            assert status == 405, (method, path)
            assert headers["allow"] == "GET", (method, path)
            if method != "HEAD":
                assert json.loads(body) == {"error": "method not allowed"}

    assert _digest(config.paths.state_db) == before


def test_handler_exception_is_500_without_traceback(
    tmp_path: Path, serve, monkeypatch, caplog,
) -> None:
    StateDB(tmp_path / "state.db").close()
    config = _config(tmp_path)

    def boom(**kw):
        raise RuntimeError("secret detail /home/someone/.ctrlrelay")

    monkeypatch.setattr(server_module, "build_status", boom)
    srv = serve(config, _tracker(config, tmp_path))

    with caplog.at_level(logging.ERROR, logger="ctrlrelay"):
        status, headers, body = srv.request("GET", "/api/status")

    assert status == 500
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body) == {"error": "internal error"}
    assert b"Traceback" not in body and b"secret" not in body
    logged = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert logged and logged[0].exc_info is not None


# --- binding --------------------------------------------------------------------


def test_binds_exactly_the_configured_address(tmp_path: Path, serve, caplog) -> None:
    StateDB(tmp_path / "state.db").close()
    config = _config(tmp_path)
    with caplog.at_level(logging.INFO, logger="ctrlrelay"):
        srv = serve(config, _tracker(config, tmp_path))
    assert (srv.host, srv.port) == ("127.0.0.1", config.web.port)
    started = [r for r in caplog.records if r.getMessage() == "web.started"]
    assert len(started) == 1
    assert started[0].address == f"http://127.0.0.1:{config.web.port}/"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", ""])
def test_all_interfaces_is_warned(host: str) -> None:
    warning = bind_warning(host)
    assert warning is not None and "every network interface" in warning


@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.1.20", "localhost"])
def test_specific_host_is_not_warned(host: str) -> None:
    assert bind_warning(host) is None


# --- the waiting line refresh ------------------------------------------------------


class CountingTracker:
    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_first = fail_first

    async def refresh(self) -> None:
        self.calls.append(threading.current_thread().name)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("first refresh fails")


def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_refresher_runs_off_the_request_threads_and_repeats() -> None:
    tracker = CountingTracker()
    refresher = WaitingRefresher(tracker, interval_s=0.05)  # type: ignore[arg-type]
    refresher.start()
    try:
        assert _wait_for(lambda: len(tracker.calls) >= 3)
    finally:
        refresher.stop()
    assert set(tracker.calls) == {refresher.thread_name}
    assert refresher.thread_name != threading.current_thread().name
    assert not refresher.is_alive()
    count = len(tracker.calls)
    time.sleep(0.15)
    assert len(tracker.calls) == count


def test_refresher_survives_a_failed_refresh(caplog) -> None:
    tracker = CountingTracker(fail_first=True)
    refresher = WaitingRefresher(tracker, interval_s=0.05)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="ctrlrelay"):
        refresher.start()
        try:
            assert _wait_for(lambda: len(tracker.calls) >= 2)
        finally:
            refresher.stop()
    assert any(r.getMessage() == "web.waiting_refresh_failed" for r in caplog.records)


# --- config ---------------------------------------------------------------------


def test_web_config_defaults(tmp_path: Path) -> None:
    config = Config.model_validate({
        "node_id": "n",
        "paths": {k: str(tmp_path / k) for k in
                  ("state_db", "worktrees", "bare_repos", "contexts", "skills")},
        "transport": {"type": "file_mock", "file_mock": {
            "inbox": str(tmp_path / "in"), "outbox": str(tmp_path / "out")}},
    })
    assert config.web == WebConfig()
    assert config.web.host == "127.0.0.1"
    assert config.web.port == 8787
    assert config.web.history_days == 7
    assert config.web.refresh_seconds == 5
    assert config.web.github_check_seconds == 60
    assert config.web.stale_after_minutes == 15


@pytest.mark.parametrize("field,value", [
    ("port", 0), ("port", 65536), ("history_days", 0), ("refresh_seconds", 0),
    ("github_check_seconds", 14), ("stale_after_minutes", 0),
])
def test_web_config_bounds(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        WebConfig(**{field: value})


# --- the command ------------------------------------------------------------------


def _write_config(tmp_path: Path) -> Path:
    import yaml

    data = {
        "node_id": "test-node",
        "paths": {k: str(tmp_path / k) for k in
                  ("worktrees", "bare_repos", "contexts", "skills")},
        "transport": {"type": "file_mock", "file_mock": {
            "inbox": str(tmp_path / "in"), "outbox": str(tmp_path / "out")}},
        "web": {"port": _free_port()},
    }
    data["paths"]["state_db"] = str(tmp_path / "run" / "state.db")
    path = tmp_path / "orchestrator.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _kills(monkeypatch) -> list[tuple[int, int]]:
    """Record os.kill instead of sending anything, so a regression that
    signals pid 0 or this process cannot take the test run down with it.
    Every pid answers as alive."""
    import os

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append((pid, sig)))
    return calls


@pytest.mark.parametrize("pid", ["0", "-5", "1", "self"])
def test_web_stop_never_signals_a_process_that_is_not_ours(
    tmp_path: Path, monkeypatch, pid: str,
) -> None:
    import os
    import signal

    from typer.testing import CliRunner

    from ctrlrelay.cli import app

    cfg = _write_config(tmp_path)
    pid_file = tmp_path / "run" / "web.pid"
    pid_file.parent.mkdir()
    # "self" is this pytest process: its command line names this test
    # file and this checkout, so a substring match would call it ours.
    pid_file.write_text(str(os.getpid()) if pid == "self" else pid)
    calls = _kills(monkeypatch)

    result = CliRunner().invoke(app, ["web", "stop", "-c", str(cfg)])

    assert [c for c in calls if c[1] == signal.SIGTERM] == []
    assert not pid_file.exists()
    assert "stale pid file" in result.output.lower()


@pytest.mark.parametrize("pid", ["0", "-5", "self"])
def test_web_status_does_not_report_a_process_that_is_not_ours(
    tmp_path: Path, monkeypatch, pid: str,
) -> None:
    import os

    from typer.testing import CliRunner

    from ctrlrelay.cli import app

    cfg = _write_config(tmp_path)
    pid_file = tmp_path / "run" / "web.pid"
    pid_file.parent.mkdir()
    pid_file.write_text(str(os.getpid()) if pid == "self" else pid)
    _kills(monkeypatch)

    result = CliRunner().invoke(app, ["web", "status", "-c", str(cfg)])

    assert result.exit_code == 1
    assert "running (PID" not in result.output
    assert "stale pid file" in result.output.lower()
    assert not pid_file.exists()


@pytest.fixture
def live_web(tmp_path: Path) -> Iterator[tuple[Path, object]]:
    """A real `ctrlrelay web start -F`, with no `gh` on its PATH so it never
    reaches GitHub (the waiting line is simply unknown)."""
    import os
    import subprocess
    import sys

    cfg = _write_config(tmp_path)
    env = {**os.environ, "PATH": os.path.dirname(sys.executable)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "ctrlrelay.cli", "web", "start", "-F", "-c", str(cfg)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pid_file = tmp_path / "run" / "web.pid"
    try:
        assert _wait_for(
            lambda: pid_file.exists() and pid_file.read_text().strip() == str(proc.pid),
            timeout=20,
        ), "web start never wrote its pid"
        yield cfg, proc
    finally:
        if proc.poll() is None:
            proc.kill()  # only the process this fixture started
        proc.wait(timeout=10)


def test_web_start_refuses_when_already_running(live_web, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from ctrlrelay.cli import app

    cfg, proc = live_web
    pid_file = tmp_path / "run" / "web.pid"

    result = CliRunner().invoke(app, ["web", "start", "-F", "-c", str(cfg)])

    assert result.exit_code == 1
    assert f"already running (PID {proc.pid})" in result.output
    assert pid_file.read_text() == str(proc.pid)
    assert proc.poll() is None


def test_web_start_replaces_a_pid_file_naming_an_unrelated_process(
    tmp_path: Path,
) -> None:
    """The PID in the file is alive but is not a status page (the PID was
    reused): the file is stale, and start proceeds."""
    import os
    import subprocess
    import sys

    cfg = _write_config(tmp_path)
    pid_file = tmp_path / "run" / "web.pid"
    pid_file.parent.mkdir()
    unrelated = os.getppid()
    os.kill(unrelated, 0)  # alive
    pid_file.write_text(str(unrelated))

    env = {**os.environ, "PATH": os.path.dirname(sys.executable)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "ctrlrelay.cli", "web", "start", "-F", "-c", str(cfg)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        started = _wait_for(
            lambda: proc.poll() is not None or (
                pid_file.exists() and pid_file.read_text().strip() == str(proc.pid)
            ),
            timeout=20,
        )
        assert started and proc.poll() is None, (
            proc.stdout.read() if proc.poll() is not None else "never wrote its pid"
        )
    finally:
        if proc.poll() is None:
            proc.kill()  # only the process this test started
        proc.wait(timeout=10)
        proc.stdout.close()


def test_web_status_and_stop_act_on_a_live_web_process(live_web, tmp_path: Path) -> None:
    import signal

    from typer.testing import CliRunner

    from ctrlrelay.cli import app

    cfg, proc = live_web
    runner = CliRunner()

    status = runner.invoke(app, ["web", "status", "-c", str(cfg)])
    assert status.exit_code == 0
    assert f"running (PID {proc.pid})" in status.output

    stop = runner.invoke(app, ["web", "stop", "-c", str(cfg)])
    assert f"Stopped status page (PID {proc.pid})" in stop.output
    assert proc.wait(timeout=10) in (0, -signal.SIGTERM)
    assert not (tmp_path / "run" / "web.pid").exists()


@pytest.mark.parametrize("proc_root", ["/proc", "missing"])
def test_web_process_check_by_command_line(live_web, tmp_path: Path, proc_root: str) -> None:
    """Both ways of reading a command line: /proc on Linux, `ps` elsewhere."""
    import os

    from ctrlrelay.cli import _is_our_web_process

    _, proc = live_web
    root = Path(proc_root) if proc_root == "/proc" else tmp_path / "no-proc"
    assert _is_our_web_process(proc.pid, proc_root=root) is True
    assert _is_our_web_process(os.getpid(), proc_root=root) is False
    assert _is_our_web_process(os.getppid(), proc_root=root) is False
    for pid in (0, -1, 1):
        assert _is_our_web_process(pid, proc_root=root) is False


# --- Host header (DNS rebinding) ------------------------------------------------------


def _with_host(srv: Served, method: str, path: str, host: str | None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(srv.host, srv.port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", host)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


@pytest.fixture
def guarded(tmp_path: Path, serve) -> Served:
    StateDB(tmp_path / "state.db").close()
    config = _config(tmp_path, allowed_hosts=["alkabox", "Alkabox.LAN."])
    return serve(config, _tracker(config, tmp_path))


@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "[::1]", "alkabox", "alkabox.lan",
    "ALKABOX", "alkabox.", "LocalHost.", socket.gethostname(),
    socket.gethostname() + ".local",
])
def test_allowed_host_is_served(guarded: Served, host: str) -> None:
    for h in (host, f"{host}:{guarded.port}"):
        status, _ = _with_host(guarded, "GET", "/api/status", h)
        assert status == 200, h


@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:{port}", "127.0.0.1:1", "alkabox:80",
    "localhost:{port}x", "", "alkabox.lan.evil.example", "127.0.0.1:",
])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unmatched_host_is_421(guarded: Served, host: str, method: str) -> None:
    status, body = _with_host(guarded, method, "/api/status", host.format(port=guarded.port))
    assert status == 421, host
    assert json.loads(body) == {"error": "misdirected request"}


def test_missing_host_is_421(guarded: Served) -> None:
    status, body = _with_host(guarded, "GET", "/", None)
    assert status == 421
    assert json.loads(body) == {"error": "misdirected request"}


def test_unmatched_host_is_logged_once(guarded: Served, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="ctrlrelay"):
        for _ in range(3):
            _with_host(guarded, "GET", "/", "evil.example")
        _with_host(guarded, "GET", "/", "other.example")
    hits = [r for r in caplog.records if r.getMessage() == "web.misdirected_host"]
    assert sorted(r.host for r in hits) == ["evil.example", "other.example"]


def test_all_interfaces_host_is_not_an_allowed_name(tmp_path: Path) -> None:
    from ctrlrelay.web.server import allowed_host_names

    config = _config(tmp_path, host="0.0.0.0")
    names = allowed_host_names(config.web)
    assert "0.0.0.0" not in names
    assert {"localhost", "127.0.0.1", "[::1]"} <= names
    assert "192.168.1.20" in allowed_host_names(_config(tmp_path, host="192.168.1.20").web)


# --- #232: live feel, links, skipped, server ------------------------------------


def _run_js(functions: list[str], expr: str) -> object:
    """Run some of the page's own functions in node and return ``expr``."""
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("node is not installed, so the page's script cannot run here")
    script = _script()
    defs = []
    for name in functions:
        m = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\(([^)]*)\)", script)
        assert m, name
        defs.append(f"function {name}({m.group(1)}) {{{_js_function(script, name)}}}")
    consts = "\n".join(re.findall(r"^var [A-Z_]+ = /.*?/;$", script, re.M))
    program = (
        consts + "\n" + "\n".join(defs)
        + f"\nprocess.stdout.write(JSON.stringify({expr}));"
    )
    done = subprocess.run([node, "-e", program], capture_output=True, text=True,
                          timeout=30, check=True)
    return json.loads(done.stdout)


@pytest.mark.parametrize("url", [
    "https://github.com/example-org/alpha/issues/5",
    "https://github.com/Org_1.x-y/Repo.Name_2-z/pull/12",
])
def test_page_accepts_a_github_issue_or_pr_url(url: str) -> None:
    assert _run_js(["safeGithubUrl"], f"safeGithubUrl({json.dumps(url)})") == url


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "http://github.com/example-org/alpha/issues/5",
    "https://github.com.evil.example/a/b/issues/5",
    "https://github.com@evil.example/a/b/issues/5",
    "https://evil.example/https://github.com/a/b/issues/5",
    "https://github.com/a/b/issues/5\n",
    "https://github.com/a/b/issues/5#x",
    "https://github.com/a/b/issues/5?x",
    "https://github.com/a/b/issues/05",
    "https://github.com/a/b/issues/0",
    "https://github.com/a/b/commit/5",
    "https://github.com/a/b/c/issues/5",
    "https://github.com/../b/issues/5",
    "https://github.com/a/./issues/5",
    "//github.com/a/b/issues/5",
    "",
    None,
    5,
])
def test_page_refuses_any_other_url(url: object) -> None:
    assert _run_js(["safeGithubUrl"], f"safeGithubUrl({json.dumps(url)})") is None


def test_every_href_goes_through_one_helper() -> None:
    """Only ``ghLink`` sets an href, and only to what ``safeGithubUrl``
    returned; it makes the anchor with createElement, opens a new tab with
    no opener and no referrer, and writes the text with textContent."""
    script = _script()
    helper = _js_function(script, "ghLink")
    # Present case: the helper really does set an href.
    assert len(re.findall(r"\.href\s*=", helper)) == 1
    assert re.search(r"\.href\s*=\s*safe\s*;", helper), helper
    assert re.search(r"var safe\s*=\s*safeGithubUrl\(url\)\s*;", helper), helper
    assert 'createElement("a")' in helper or "el(\"a\"" in helper
    assert re.search(r'\.rel\s*=\s*"noopener noreferrer"', helper)
    assert re.search(r'\.target\s*=\s*"_blank"', helper)
    assert "textContent" in helper or "el(" in helper
    # Nowhere else: no href, no setAttribute of a URL-bearing attribute.
    outside = script.replace(helper, "")
    assert not re.search(r"\.href\b", outside), "href set outside ghLink"
    assert not re.search(r"""setAttribute\(\s*["'](href|src|action|formaction)""", script)
    assert not re.search(r"""\[\s*["']href["']\s*\]""", script)
    # And the links are used: one call per section that links.
    for renderer in ("runningCard", "renderNeeds", "renderWaiting", "renderSkipped",
                     "renderFeed"):
        assert "ghLink(" in _js_function(script, renderer), renderer
    assert "questionParts(" in _js_function(script, "renderNeeds")


def test_no_server_value_reaches_html_parsing() -> None:
    """Every node is built with createElement and textContent. The scan for
    HTML sinks is test_page_source_has_no_html_parsing_dom_sinks; this adds
    that nothing is written through attributes that run or load code."""
    script = _script()
    assert "textContent" in script
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", ".src =",
                 "location =", "location.href", "window.open", "srcdoc"):
        assert sink not in script, sink


def test_the_header_ticks_every_second() -> None:
    script = _script()
    assert re.search(r"setInterval\(\s*tick\s*,\s*1000\s*\)", script)
    tick = _js_function(script, "tick")
    assert "updatedAgo(" in tick and "liveness(" in tick and "TICKING" in tick
    html = _page_source()
    assert 'id="live-dot"' in html and 'id="updated-ago"' in html


@pytest.mark.parametrize(("ok_at", "now", "expected"), [
    (None, 50_000, "never"),
    (40_000, 50_000, "live"),       # 10 s: within two 5 s intervals
    (40_000, 50_001, "stale"),      # just past two intervals
])
def test_live_dot_is_green_only_within_two_refresh_intervals(
    ok_at: int | None, now: int, expected: str,
) -> None:
    assert _run_js(["liveness"], f"liveness({json.dumps(ok_at)}, {now}, 5)") == expected


@pytest.mark.parametrize(("ok_at", "now", "expected"), [
    (None, 50_000, "updated never"),
    (50_000, 50_400, "updated 0s ago"),
    (40_000, 52_999, "updated 12s ago"),
    (0, 125_000, "updated 2m 05s ago"),
    (0, 3_725_000, "updated 1h 02m 05s ago"),
])
def test_updated_ago_counts_seconds(ok_at, now, expected) -> None:
    got = _run_js(["updatedAgo", "clockText"], f"updatedAgo({json.dumps(ok_at)}, {now})")
    assert got == expected


@pytest.mark.parametrize("start_s", [125, 3_725, 90_000])
def test_updated_ago_changes_every_second_past_a_minute_and_an_hour(start_s: int) -> None:
    """One second later, the text is different: past 2m, past 1h, past 1d."""
    texts = _run_js(
        ["updatedAgo", "clockText"],
        f"[updatedAgo(0, {start_s * 1000}), updatedAgo(0, {(start_s + 1) * 1000})]",
    )
    assert texts[0] != texts[1], texts


def test_elapsed_times_tick_from_the_server_value() -> None:
    """A running card's elapsed time is the server's ``elapsed_s`` plus the
    whole seconds since that answer arrived, so it moves between polls."""
    assert _run_js(["tickedSeconds"], "tickedSeconds(100, 1000, 4999)") == 103
    assert _run_js(["tickedSeconds"], "tickedSeconds(100, 1000, 1000)") == 100
    assert _run_js(["tickedSeconds"], "tickedSeconds(null, 1000, 4999)") is None
    card = _js_function(_script(), "runningCard")
    assert "ticking(" in card


@pytest.mark.parametrize(("secs", "expected"), [
    (None, "running unknown"),
    (45, "running 45s"),
    (605, "running 10m 05s"),
    (3_725, "running 1h 02m 05s"),
    (90_061, "running 1d 01h 01m 01s"),
])
def test_ticking_text_always_shows_seconds(secs, expected) -> None:
    got = _run_js(["tickText", "clockText"],
                  f'tickText({json.dumps(secs)}, "running ", "")')
    assert got == expected


@pytest.mark.parametrize("start_s", [65, 3_725, 90_000])
def test_ticking_text_changes_every_second(start_s: int) -> None:
    texts = _run_js(
        ["tickText", "clockText"],
        f'[tickText({start_s}, "", ""), tickText({start_s + 1}, "", "")]',
    )
    assert texts[0] != texts[1], texts


def test_skipped_section_is_collapsed_with_a_count() -> None:
    html = _page_source()
    m = re.search(r"<details[^>]*id=\"skipped-box\"[^>]*>", html)
    assert m, "no skipped <details>"
    assert " open" not in m.group(0)
    assert 'id="skipped-count"' in html
    body = _js_function(_script(), "renderSkipped")
    assert re.search(r'setCount\("skipped-count",\s*null\)', body)


def test_server_panel_flags_low_memory_and_disk() -> None:
    body = _js_function(_script(), "renderServer")
    assert re.search(r'm\.low === true \? " low"', body), body
    assert re.search(r'd\.low === true \? " low"', body), body
    assert ".server .line.low" in _page_source()
    assert 'id="server"' in _page_source()


def test_running_card_links_the_pr_and_shows_the_current_activity() -> None:
    """#238: the PR link goes through ghLink like every other link, and the
    card names what the agent is doing now beside the furthest-phase bar."""
    card = _js_function(_script(), "runningCard")
    assert re.search(r"ghLink\(\s*card\.pr_url\s*,", card), card
    assert re.search(r"if\s*\(\s*card\.pr_url\s*\)", card), card
    assert "card.current_activity" in card
    assert re.search(r"phaseLabel\(\s*card\.current_activity\s*\)", card), card


@pytest.mark.parametrize(("phase", "label"), [
    ("ci", "CI"), ("reading", "reading"), ("pr", "PR"), ("unknown", None),
    ("nope", None), (None, None),
])
def test_phase_label_names_only_known_phases(phase, label) -> None:
    script = _script()
    m = re.search(r"^var PHASES = \[.*?\];$", script, re.M | re.S)
    assert m
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("node is not installed, so the page's script cannot run here")
    fn = _js_function(script, "phaseLabel")
    program = (m.group(0) + f"\nfunction phaseLabel(phase) {{{fn}}}\n"
               f"process.stdout.write(JSON.stringify(phaseLabel({json.dumps(phase)})));")
    done = subprocess.run([node, "-e", program], capture_output=True, text=True,
                          timeout=30, check=True)
    assert json.loads(done.stdout) == label


def test_question_numbers_become_links_only_when_the_server_sent_one() -> None:
    """#241: the page links a #N in a question only to the URL the server
    built for that N. A number with no ref, an entity and a path stay text."""
    refs = [{"number": 8, "url": "https://github.com/o/r/issues/8"},
            {"number": 7, "url": "https://github.com/o/r/issues/7"}]
    text = "Approve #8 and #7? Not #3, &#8 or a/#7. Again #8."
    parts = _run_js(["questionParts"],
                    f"questionParts({json.dumps(text)}, {json.dumps(refs)})")
    assert parts == [
        {"text": "Approve ", "url": None},
        {"text": "#8", "url": "https://github.com/o/r/issues/8"},
        {"text": " and ", "url": None},
        {"text": "#7", "url": "https://github.com/o/r/issues/7"},
        {"text": "? Not #3, &#8 or a/#7. Again ", "url": None},
        {"text": "#8", "url": "https://github.com/o/r/issues/8"},
        {"text": ".", "url": None},
    ]
    assert "".join(p["text"] for p in parts) == text


@pytest.mark.parametrize("refs", [None, [], "x", [{"number": "8", "url": "u"}]])
def test_question_with_no_usable_refs_is_one_text_part(refs: object) -> None:
    text = "Approve #8?"
    parts = _run_js(["questionParts"],
                    f"questionParts({json.dumps(text)}, {json.dumps(refs)})")
    assert parts == [{"text": text, "url": None}]


def test_question_parts_agree_with_the_server_on_non_ascii() -> None:
    """The server's pattern is ASCII (#241), like the page's: a non-ASCII
    letter before #N does not hide it, and a non-ASCII digit ends N."""
    refs = [{"number": 3, "url": "https://github.com/o/r/issues/3"},
            {"number": 4, "url": "https://github.com/o/r/issues/4"}]
    text = "\u00e9#3 and #4\u0663"
    parts = _run_js(["questionParts"],
                    f"questionParts({json.dumps(text)}, {json.dumps(refs)})")
    assert [p["text"] for p in parts if p["url"]] == ["#3", "#4"]
    assert "".join(p["text"] for p in parts) == text


@pytest.mark.parametrize("question", [None, 5, {"toString": None}])
def test_a_question_that_is_not_text_renders_as_no_text(question: object) -> None:
    parts = _run_js(["questionParts"], f"questionParts({json.dumps(question)}, [])")
    assert parts == [{"text": "", "url": None}]
