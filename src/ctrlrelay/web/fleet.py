"""Every machine on one page (#249).

Each machine's status page carries a ``node`` block about itself. This
module adds what the page needs to show the others:

* :func:`node_activity` fills in the parts of this machine's ``node``
  block that come from the state database and the daemon's heartbeat:
  its state, slots, claims and heartbeat;
* :class:`PeerReader` reads each configured peer's ``/api/status`` on a
  timer, server side, and keeps the answer for the ``fleet`` block.

A peer that does not answer is "not reachable from here", with the
reason and its last known block, never "idle". A peer that answers with
an old heartbeat is "silent": its web server is up, its poller is not.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ctrlrelay.core.obs import get_logger

_logger = get_logger("web.fleet")

# What a machine is doing, as the page shows it.
WORKING, IDLE, PAUSED_LIMIT, SILENT, UNKNOWN = (
    "working", "idle", "paused-limit", "silent", "unknown",
)
# The daemon handles one issue at a time.
SLOTS_TOTAL = 1
# A peer's /api/status is small; anything bigger is not one.
MAX_PEER_BYTES = 2_000_000


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def node_activity(
    *,
    running: list[dict[str, Any]] | None,
    needs_you: list[dict[str, Any]] | None,
    heartbeat: dict[str, Any] | None,
    paused: Any,
    now: float,
    silent_after: float,
) -> dict[str, Any]:
    """State, slots, claims and heartbeat for this machine's ``node`` block.

    ``running`` and ``needs_you`` are the page's own sections; ``None``
    means the state database could not be read, and the parts built from
    it are ``None`` too, never empty. The state is ``silent`` when the
    daemon's heartbeat is missing or older than ``silent_after``: nothing
    else here can tell a stopped poller from a quiet one.
    """
    beat = heartbeat.get("heartbeat_at") if heartbeat else None
    silent = beat is None or now - beat > silent_after
    if silent:
        state = SILENT
    elif paused:
        state = PAUSED_LIMIT
    elif running is None:
        state = UNKNOWN
    elif running:
        state = WORKING
    else:
        state = IDLE

    claims: list[dict[str, Any]] | None = None
    if running is not None and needs_you is not None:
        claims = []
        for card in running:
            if card.get("issue_number") is None:
                continue
            claims.append({
                "repo": card.get("repo"),
                "issue_number": card.get("issue_number"),
                "kind": "issue",
                "phase": card.get("phase"),
                "since": card.get("started"),
                "url": card.get("url"),
            })
        for q in needs_you:
            if q.get("issue_number") is None:
                continue
            claims.append({
                "repo": q.get("repo"),
                "issue_number": q.get("issue_number"),
                "kind": "issue",
                "phase": "waiting for an answer",
                "since": q.get("asked"),
                "url": q.get("url"),
            })

    return {
        "state": state,
        "slots": None if running is None else {
            "total": SLOTS_TOTAL,
            "busy": min(len(running), SLOTS_TOTAL),
        },
        "claims": claims,
        "heartbeat_at": _iso(beat),
        "last_poll_at": _iso(heartbeat.get("last_poll_at") if heartbeat else None),
        "open_questions": None if needs_you is None else len(needs_you),
    }


def _peer_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 421:
            return (
                "bad host header: add this peer's name, as the URL uses it, "
                "to the peer's web.allowed_hosts"
            )
        return f"HTTP {exc.code}"
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    if isinstance(reason, socket.gaierror):
        return "name not found"
    return f"{type(reason).__name__}: {reason}"[:200]


def fetch_peer(url: str, *, timeout: float) -> dict[str, Any]:
    """One peer's ``/api/status``. Raises on anything that is not one."""
    target = url.rstrip("/") + "/api/status"
    request = urllib.request.Request(target, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
        body = resp.read(MAX_PEER_BYTES + 1)
    if len(body) > MAX_PEER_BYTES:
        raise ValueError("answer too large to be a status page")
    data = json.loads(body)
    if not isinstance(data, dict) or "host" not in data:
        raise ValueError("not a ctrlrelay status page")
    return data


def peer_entry(
    url: str,
    *,
    data: dict[str, Any] | None,
    error: str | None,
    checked_at: float,
    previous: dict[str, Any] | None,
    silent_after: float,
) -> dict[str, Any]:
    """The ``fleet.peers`` item for one read of one peer."""
    if data is None:
        last = previous.get("node") if previous else None
        if last is None and previous:
            last = previous.get("last_known")
        return {
            "url": url,
            "name": (previous or {}).get("name"),
            "reachable": False,
            "checked_at": _iso(checked_at),
            "error": error,
            "state": "not reachable from here",
            "node": None,
            "last_known": last,
            "needs_you": [],
        }
    node = data.get("node") if isinstance(data.get("node"), dict) else None
    beat = _parse_iso(node.get("heartbeat_at")) if node else None
    silent = beat is None or checked_at - beat > silent_after
    needs = data.get("needs_you") if isinstance(data.get("needs_you"), list) else []
    return {
        "url": url,
        "name": (node or {}).get("name") or data.get("host"),
        "reachable": True,
        "checked_at": _iso(checked_at),
        "error": None,
        "state": SILENT if silent else ((node or {}).get("state") or UNKNOWN),
        "node": node,
        "last_known": None,
        "needs_you": [q for q in needs if isinstance(q, dict)],
    }


class PeerReader:
    """Reads every peer on a timer in a daemon thread; ``snapshot()``
    returns the latest ``fleet`` block and never waits on the network."""

    thread_name = "ctrlrelay-web-fleet"

    def __init__(
        self,
        peers: list[str],
        *,
        interval_s: float,
        timeout_s: float,
        silent_after: float,
        fetch: Callable[..., dict[str, Any]] = fetch_peer,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.peers = list(peers)
        self._interval = interval_s
        self._timeout = timeout_s
        self._silent_after = silent_after
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=self.thread_name, daemon=True,
        )

    def read_once(self) -> None:
        for url in self.peers:
            try:
                data, error = self._fetch(url, timeout=self._timeout), None
            except Exception as exc:
                data, error = None, _peer_error(exc)
            with self._lock:
                previous = self._entries.get(url)
            entry = peer_entry(
                url, data=data, error=error, checked_at=self._clock(),
                previous=previous, silent_after=self._silent_after,
            )
            with self._lock:
                self._entries[url] = entry

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            peers = [
                self._entries.get(url) or {
                    "url": url, "name": None, "reachable": None,
                    "checked_at": None, "error": None, "state": "not read yet",
                    "node": None, "last_known": None, "needs_you": [],
                }
                for url in self.peers
            ]
        return {"peers": peers}

    def start(self) -> None:
        if self.peers:
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.read_once()
            except Exception as exc:  # never let the thread die quietly
                _logger.warning(
                    "web.fleet_read_failed",
                    extra={"error": f"{type(exc).__name__}: {exc}"[:300]},
                )
            self._stop.wait(self._interval)
