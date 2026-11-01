"""Every machine on one page (#249)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ctrlrelay.core.heartbeat import Heartbeat
from ctrlrelay.web.fleet import (
    IDLE,
    PAUSED_LIMIT,
    SILENT,
    UNKNOWN,
    WORKING,
    PeerReader,
    fetch_peer,
    node_activity,
    peer_entry,
)

NOW = 1_793_490_000.0


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


CARD = {"repo": "o/r", "issue_number": 7, "phase": "coding", "started": "x", "url": "u"}
QUESTION = {"repo": "o/r", "issue_number": 9, "asked": "y", "url": "v"}


class TestHeartbeat:
    def test_beat_and_poll_round_trip(self, tmp_path: Path) -> None:
        hb = Heartbeat(tmp_path / "heartbeat.json")
        assert hb.read() is None
        hb.beat(now=NOW)
        assert hb.read() == {"heartbeat_at": int(NOW), "last_poll_at": None}
        hb.polled(now=NOW + 5)
        assert hb.read() == {"heartbeat_at": int(NOW + 5), "last_poll_at": int(NOW + 5)}

    def test_a_damaged_file_reads_as_no_heartbeat(self, tmp_path: Path) -> None:
        path = tmp_path / "heartbeat.json"
        for text in ("{nope", '{"heartbeat_at": "soon"}', "[]"):
            path.write_text(text)
            assert Heartbeat(path).read() is None

    def test_a_write_that_fails_does_not_raise(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("")
        Heartbeat(blocker / "heartbeat.json").beat()

    @pytest.mark.asyncio
    async def test_it_beats_on_its_own_while_the_loop_is_busy(self, tmp_path: Path) -> None:
        import asyncio

        hb = Heartbeat(tmp_path / "heartbeat.json")
        task = asyncio.create_task(hb.run(interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        assert hb.read() is not None


class TestNodeActivity:
    def _act(self, **kw):
        base = dict(
            running=[], needs_you=[],
            heartbeat={"heartbeat_at": NOW - 10, "last_poll_at": NOW - 20},
            paused=None, now=NOW, silent_after=300,
        )
        base.update(kw)
        return node_activity(**base)

    def test_idle_working_paused(self) -> None:
        assert self._act()["state"] == IDLE
        assert self._act(running=[CARD])["state"] == WORKING
        assert self._act(running=[CARD], paused={"reason": "x"})["state"] == PAUSED_LIMIT

    def test_no_or_old_heartbeat_is_silent_whatever_else_is_true(self) -> None:
        assert self._act(heartbeat=None, running=[CARD])["state"] == SILENT
        assert self._act(heartbeat={"heartbeat_at": NOW - 301})["state"] == SILENT
        assert self._act(heartbeat={"heartbeat_at": NOW - 300})["state"] == IDLE

    def test_an_unreadable_database_is_unknown_never_empty(self) -> None:
        act = self._act(running=None, needs_you=None)
        assert act["state"] == UNKNOWN
        assert act["slots"] is None and act["claims"] is None
        assert act["open_questions"] is None

    def test_claims_are_running_issues_and_waiting_questions(self) -> None:
        act = self._act(running=[CARD], needs_you=[QUESTION])
        assert [(c["issue_number"], c["phase"]) for c in act["claims"]] == [
            (7, "coding"), (9, "waiting for an answer"),
        ]
        assert act["slots"] == {"total": 1, "busy": 1}
        assert act["open_questions"] == 1
        assert act["heartbeat_at"] == _iso(NOW - 10)
        assert act["last_poll_at"] == _iso(NOW - 20)


def _status(name: str, *, beat: float, needs: list | None = None) -> dict:
    return {
        "host": name,
        "node": {"name": name, "state": "idle", "heartbeat_at": _iso(beat)},
        "needs_you": needs or [],
    }


class TestPeerEntry:
    def test_a_reachable_peer_with_a_fresh_heartbeat(self) -> None:
        e = peer_entry("http://b", data=_status("node-b", beat=NOW - 5), error=None,
                       checked_at=NOW, previous=None, silent_after=300)
        assert (e["name"], e["reachable"], e["state"]) == ("node-b", True, "idle")

    def test_a_peer_that_answers_with_an_old_heartbeat_is_silent(self) -> None:
        e = peer_entry("http://b", data=_status("node-b", beat=NOW - 900), error=None,
                       checked_at=NOW, previous=None, silent_after=300)
        assert e["reachable"] and e["state"] == SILENT

    def test_an_unreachable_peer_keeps_its_last_known_block_and_is_never_idle(self) -> None:
        first = peer_entry("http://b", data=_status("node-b", beat=NOW - 5), error=None,
                           checked_at=NOW, previous=None, silent_after=300)
        down = peer_entry("http://b", data=None, error="refused", checked_at=NOW + 60,
                          previous=first, silent_after=300)
        assert down["reachable"] is False
        assert down["state"] == "not reachable from here"
        assert down["error"] == "refused"
        assert down["node"] is None
        assert down["last_known"]["name"] == "node-b"
        again = peer_entry("http://b", data=None, error="timeout", checked_at=NOW + 120,
                           previous=down, silent_after=300)
        assert again["last_known"]["name"] == "node-b"

    def test_peer_questions_come_through_for_needs_you(self) -> None:
        e = peer_entry("http://b", data=_status("node-b", beat=NOW, needs=[QUESTION, "junk"]),
                       error=None, checked_at=NOW, previous=None, silent_after=300)
        assert e["needs_you"] == [QUESTION]


class _Peer(BaseHTTPRequestHandler):
    status_code = 200
    body = b"{}"

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(self.status_code)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a) -> None:
        pass


def _serve(status: int, body: bytes):
    handler = type("H", (_Peer,), {"status_code": status, "body": body})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class TestReadingRealPeers:
    def test_a_status_page(self) -> None:
        srv, url = _serve(200, json.dumps(_status("node-b", beat=time.time())).encode())
        try:
            assert fetch_peer(url, timeout=2)["host"] == "node-b"
        finally:
            srv.shutdown()

    @pytest.mark.parametrize("status,body,reason", [
        (421, b"", "bad host header"),
        (500, b"", "HTTP 500"),
        (200, b"<html>", "JSONDecodeError"),
        (200, b'{"other": 1}', "not a ctrlrelay status page"),
    ])
    def test_each_failure_is_named(self, status: int, body: bytes, reason: str) -> None:
        srv, url = _serve(status, body)
        try:
            reader = PeerReader([url], interval_s=60, timeout_s=2, silent_after=300)
            reader.read_once()
            [entry] = reader.snapshot()["peers"]
        finally:
            srv.shutdown()
        assert entry["reachable"] is False
        assert reason in entry["error"]

    def test_a_refused_connection_is_refused(self) -> None:
        srv, url = _serve(200, b"{}")
        srv.shutdown()
        srv.server_close()
        reader = PeerReader([url], interval_s=60, timeout_s=2, silent_after=300)
        reader.read_once()
        assert reader.snapshot()["peers"][0]["error"] == "refused"

    def test_before_the_first_read_a_peer_is_not_read_yet(self) -> None:
        reader = PeerReader(["http://b"], interval_s=60, timeout_s=2, silent_after=300)
        [entry] = reader.snapshot()["peers"]
        assert entry["state"] == "not read yet" and entry["reachable"] is None

    def test_a_bad_host_header_tells_you_what_to_fix(self) -> None:
        from ctrlrelay.web.fleet import _peer_error

        err = urllib.error.HTTPError("http://b", 421, "Misdirected", None, None)
        assert "web.allowed_hosts" in _peer_error(err)


class TestTheStatusJSON:
    def test_node_and_fleet_blocks(self, tmp_path: Path) -> None:
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.web.status import build_status

        db = tmp_path / "state.db"
        StateDB(db).close()
        Heartbeat(tmp_path / "heartbeat.json").beat(now=NOW - 5)
        out = build_status(
            db_path=db, waiting={}, server=dict, version="9.9", host="node-a",
            now=int(NOW), node=lambda: {"name": "node-a", "paused": None},
            heartbeat=Heartbeat(tmp_path / "heartbeat.json").read,
            fleet=lambda: {"peers": []},
        )
        assert out["node"]["state"] == IDLE
        assert out["node"]["version"] == "9.9"
        assert out["node"]["claims"] == []
        assert out["fleet"] == {"peers": []}

    def test_a_fleet_reader_that_raises_is_null_with_an_error(self, tmp_path: Path) -> None:
        from ctrlrelay.web.status import build_status

        def boom() -> dict:
            raise RuntimeError("thread died")

        out = build_status(
            db_path=tmp_path / "missing.db", waiting={}, server=dict,
            version="v", host="h", fleet=boom,
        )
        assert out["fleet"] is None
        assert any("fleet: RuntimeError" in e for e in out["errors"])


class TestConfig:
    def test_an_email_is_not_a_label(self) -> None:
        from pydantic import ValidationError

        from ctrlrelay.core.config import FleetConfig

        with pytest.raises(ValidationError, match="label"):
            FleetConfig(account_label="me@example.com")
        assert FleetConfig(account_label="max-2").account_label == "max-2"

    def test_peers_are_urls(self) -> None:
        from pydantic import ValidationError

        from ctrlrelay.core.config import FleetConfig

        with pytest.raises(ValidationError):
            FleetConfig(peers=["node-b:8787"])
