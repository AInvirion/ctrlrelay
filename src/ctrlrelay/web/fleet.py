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
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
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
    except (ValueError, OverflowError, OSError):
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
    means that part of the state database could not be read. If either is
    ``None``, the state is ``unknown`` and slots and claims are ``None``,
    never empty or idle.

    Precedence: ``silent`` (the daemon's heartbeat is missing or older than
    ``silent_after``; nothing else here can tell a stopped poller from a
    quiet one), then ``unknown`` (the database could not be read), then
    ``paused-limit``, then ``working`` or ``idle``.
    """
    beat = heartbeat.get("heartbeat_at") if heartbeat else None
    silent = beat is None or now - beat > silent_after
    readable = running is not None and needs_you is not None
    if silent:
        state = SILENT
    elif not readable:
        state = UNKNOWN
    elif paused:
        state = PAUSED_LIMIT
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
        "slots": None if not readable or running is None else {
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


# A peer's answer is untrusted: only these shapes are passed on, so a bad
# or hostile peer can neither break this server nor the page.
_MAX_ITEMS = 200


def _text(value: Any, limit: int = 300) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _clean_claims(raw: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    out = []
    for c in raw[:_MAX_ITEMS]:
        if not isinstance(c, dict):
            continue
        number = c.get("issue_number")
        repo = _text(c.get("repo"))
        if type(number) is not int or repo is None:
            continue
        out.append({
            "repo": repo, "issue_number": number, "kind": _text(c.get("kind"), 40),
            "phase": _text(c.get("phase"), 80), "since": _text(c.get("since"), 40),
            "url": _text(c.get("url"), 500),
        })
    return out


def _clean_questions(raw: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    out = []
    for q in raw[:_MAX_ITEMS]:
        if not isinstance(q, dict) or _text(q.get("repo")) is None:
            continue
        refs = q.get("refs")
        out.append({
            "session_id": _text(q.get("session_id")),
            "pipeline": _text(q.get("pipeline"), 40),
            "repo": _text(q.get("repo")),
            "issue_number": q.get("issue_number") if type(q.get("issue_number")) is int else None,
            "title": _text(q.get("title")),
            "question": _text(q.get("question"), 4000),
            "asked": _text(q.get("asked"), 40),
            "asked_ts": q.get("asked_ts") if type(q.get("asked_ts")) is int else None,
            "held_until_ts": (
                q.get("held_until_ts") if type(q.get("held_until_ts")) is int else None
            ),
            "url": _text(q.get("url"), 500),
            "refs": [
                {"number": r["number"], "url": _text(r.get("url"), 500)}
                for r in (refs if isinstance(refs, list) else [])[:20]
                if isinstance(r, dict) and type(r.get("number")) is int
            ],
        })
    return out


def _clean_window(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return "unknown"
    used = raw.get("used")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return "unknown"
    return {"used": float(used), "resets_at": _text(raw.get("resets_at"), 40)}


def _clean_node(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    usage = raw.get("usage")
    slots = raw.get("slots")
    return {
        "name": _text(raw.get("name"), 100),
        "state": _text(raw.get("state"), 40),
        "version": _text(raw.get("version"), 40),
        "account_label": _text(raw.get("account_label"), 100),
        "heartbeat_at": _text(raw.get("heartbeat_at"), 40),
        "last_poll_at": _text(raw.get("last_poll_at"), 40),
        "slots": {"total": slots["total"], "busy": slots["busy"]}
        if isinstance(slots, dict) and type(slots.get("total")) is int
        and type(slots.get("busy")) is int else None,
        "usage": {
            "five_hour": _clean_window(usage.get("five_hour")),
            "seven_day": _clean_window(usage.get("seven_day")),
        } if isinstance(usage, dict) else "unknown",
        "claims": _clean_claims(raw.get("claims")),
    }


def _state_at(node: dict[str, Any] | None, now: float, silent_after: float) -> str:
    """A reachable peer's state as of ``now``: its heartbeat ages between
    reads, so this is worked out when the page asks, not when read."""
    beat = _parse_iso(node.get("heartbeat_at")) if node else None
    if beat is None or now - beat > silent_after:
        return SILENT
    return (node or {}).get("state") or UNKNOWN


def peer_entry(
    url: str,
    *,
    data: dict[str, Any] | None,
    error: str | None,
    checked_at: float,
    previous: dict[str, Any] | None,
    silent_after: float,
) -> dict[str, Any]:
    """The ``fleet.peers`` item for one read of one peer. ``needs_you``
    is ``None`` (unknown) when the peer did not answer or did not send a
    list, never ``[]``."""
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
            "needs_you": None,
        }
    node = _clean_node(data.get("node"))
    return {
        "url": url,
        "name": (node or {}).get("name") or _text(data.get("host"), 100),
        "reachable": True,
        "checked_at": _iso(checked_at),
        "error": None,
        "state": _state_at(node, checked_at, silent_after),
        "node": node,
        "last_known": None,
        "needs_you": _clean_questions(data.get("needs_you")),
    }


class PeerReader:
    """Reads every peer on a timer in a daemon thread; ``snapshot()``
    returns the latest ``fleet`` block and never waits on the network.

    Peers are read in parallel, each with a deadline for the whole read
    (name lookup and body included): one slow or stuck peer is reported
    as "timeout" and cannot hold up the others.
    """

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
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, min(len(self.peers), 8)),
            thread_name_prefix="ctrlrelay-web-peer",
        )
        self._thread = threading.Thread(
            target=self._run, name=self.thread_name, daemon=True,
        )

    def _read_one(self, url: str, future: Future) -> None:
        try:
            data, error = future.result(timeout=self._timeout * 2), None
        except FutureTimeout:
            data, error = None, "timeout"
        except Exception as exc:
            data, error = None, _peer_error(exc)
        with self._lock:
            previous = self._entries.get(url)
        try:
            entry = peer_entry(
                url, data=data, error=error, checked_at=self._clock(),
                previous=previous, silent_after=self._silent_after,
            )
        except Exception as exc:  # a shape the cleaning did not expect
            entry = peer_entry(
                url, data=None, error=f"bad answer: {type(exc).__name__}",
                checked_at=self._clock(), previous=previous,
                silent_after=self._silent_after,
            )
        with self._lock:
            self._entries[url] = entry

    def read_once(self) -> None:
        futures = {
            url: self._pool.submit(self._fetch, url, timeout=self._timeout)
            for url in self.peers
        }
        for url, future in futures.items():
            self._read_one(url, future)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            peers = []
            for url in self.peers:
                entry = self._entries.get(url)
                if entry is None:
                    peers.append({
                        "url": url, "name": None, "reachable": None,
                        "checked_at": None, "error": None, "state": "not read yet",
                        "node": None, "last_known": None, "needs_you": None,
                    })
                elif entry["reachable"]:
                    peers.append({
                        **entry,
                        "state": _state_at(entry["node"], now, self._silent_after),
                    })
                else:
                    peers.append(dict(entry))
        return {"peers": peers}

    def start(self) -> None:
        if self.peers:
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
        self._pool.shutdown(wait=False, cancel_futures=True)

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
