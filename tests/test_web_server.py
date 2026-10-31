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
import socket
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
from ctrlrelay.web.status import make_sessions_reader
from ctrlrelay.web.transcript import transcript_dir_for
from ctrlrelay.web.waiting import WaitingTracker

ALPHA = "example-org/alpha"
HOSTILE_TITLE = "<img src=x onerror=alert(1)>"
HOSTILE_FILE = '"><script>alert(1)<'
TOP_KEYS = {
    "version", "host", "generated", "generated_ts", "running", "needs_you",
    "waiting", "today", "feed", "errors",
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


def test_page_escapes_titles_and_commands() -> None:
    """Titles come from GitHub, where anyone can file on a public repo, and
    the last command can carry a file name. The page builds every node with
    textContent/createElement; nothing in it parses a string as HTML."""
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


def test_web_start_refuses_when_already_running(tmp_path: Path) -> None:
    import os

    from typer.testing import CliRunner

    from ctrlrelay.cli import app

    cfg = _write_config(tmp_path)
    pid_file = tmp_path / "run" / "web.pid"
    pid_file.parent.mkdir()
    pid_file.write_text(str(os.getppid()))  # alive, and not this process

    result = CliRunner().invoke(app, ["web", "start", "-F", "-c", str(cfg)])

    assert result.exit_code == 1
    assert "already running" in result.output
    assert pid_file.read_text() == str(os.getppid())


def test_web_status_reads_the_pid_file_next_to_state_db(tmp_path: Path) -> None:
    import os

    from typer.testing import CliRunner

    from ctrlrelay.cli import app

    cfg = _write_config(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["web", "status", "-c", str(cfg)]).exit_code == 1
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "web.pid").write_text(str(os.getppid()))
    result = runner.invoke(app, ["web", "status", "-c", str(cfg)])
    assert result.exit_code == 0
    assert str(os.getppid()) in result.output


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
