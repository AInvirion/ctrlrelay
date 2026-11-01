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
an old heartbeat is "silent": its web server is up, and nothing confirms
its poller is.
"""

from __future__ import annotations

import json
import math
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ctrlrelay.core.obs import get_logger, log_event

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
    ``None``, slots and claims are ``None``, never empty, and the state is
    ``unknown``.

    Precedence: ``unknown`` (the database could not be read; the page's
    errors say why), then ``silent`` (the daemon's heartbeat is missing or
    older than ``silent_after``; nothing else here can tell a stopped
    poller from a quiet one), then ``paused-limit``, then ``working`` or
    ``idle``. Both of the first two are shown as a problem.
    """
    beat = heartbeat.get("heartbeat_at") if heartbeat else None
    silent = beat is None or now - beat > silent_after
    readable = running is not None and needs_you is not None
    if not readable:
        state = UNKNOWN
    elif silent:
        state = SILENT
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
            # A run with no issue (the security sweep) is still work this
            # machine holds: listed as a sweep, not left out while the
            # slots count it.
            number = card.get("issue_number")
            claims.append({
                "repo": card.get("repo"),
                "issue_number": number,
                "kind": "issue" if number is not None else "sweep",
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
    # ``timeout`` bounds each socket wait; the body is also read against a
    # wall-clock deadline, so a peer trickling one byte at a time cannot
    # hold the read for longer than ``timeout * 2`` in all.
    deadline = time.monotonic() + timeout * 2
    chunks: list[bytes] = []
    size = 0
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
        while size <= MAX_PEER_BYTES:
            if time.monotonic() > deadline:
                raise TimeoutError("the answer took too long")
            chunk = resp.read1(65536)  # what has arrived, not a full 64 KB
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    body = b"".join(chunks)
    if len(body) > MAX_PEER_BYTES:
        raise ValueError("answer too large to be a status page")
    data = json.loads(body)
    if not isinstance(data, dict) or "host" not in data:
        raise ValueError("not a ctrlrelay status page")
    return data


# A peer's answer is untrusted: only these shapes are passed on, so a bad
# or hostile peer can neither break this server nor the page.
# More than this many claims or questions from one peer is not believable;
# the list is then unknown rather than cut short.
_MAX_ITEMS = 1000


def _text(value: Any, limit: int = 300) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _clean_claims(raw: Any) -> list[dict[str, Any]] | None:
    """The peer's claims, or ``None`` (unknown) when the list is missing,
    too long, or holds any entry that is not a claim: a list with a bad
    entry dropped would read as fewer claims than the peer has."""
    if not isinstance(raw, list) or len(raw) > _MAX_ITEMS:
        return None
    out = []
    for c in raw:
        if not isinstance(c, dict):
            return None
        number = c.get("issue_number")
        repo = _text(c.get("repo"))
        if repo is None or not (type(number) is int or number is None):
            return None
        out.append({
            "repo": repo, "issue_number": number, "kind": _text(c.get("kind"), 40),
            "phase": _text(c.get("phase"), 80), "since": _text(c.get("since"), 40),
            "url": _text(c.get("url"), 500),
        })
    return out


def _clean_questions(raw: Any) -> list[dict[str, Any]] | None:
    """The peer's open questions, or ``None`` (unknown) when the list is
    missing, too long, or holds an entry that is not a question (an
    object with a repo). A question may lack an issue number (a sweep's);
    its ``refs`` are display links only and keep at most 20 valid ones."""
    if not isinstance(raw, list) or len(raw) > _MAX_ITEMS:
        return None
    out = []
    for q in raw:
        if not isinstance(q, dict) or _text(q.get("repo")) is None:
            return None
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
    try:
        value = float(used)
    except OverflowError:  # an integer with hundreds of digits
        return "unknown"
    if not math.isfinite(value):  # NaN would break the page's JSON
        return "unknown"
    return {"used": value, "resets_at": _text(raw.get("resets_at"), 40)}


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


def _state_at(
    node: dict[str, Any] | None, now: float, silent_after: float, offset: float = 0.0,
) -> str:
    """A reachable peer's state as of ``now``: its heartbeat ages between
    reads, so this is worked out when the page asks, not when read.
    ``offset`` is how far this machine's clock is ahead of the peer's
    (measured from the peer's own ``generated_ts``), so the heartbeat is
    aged on the peer's clock, not compared across two."""
    beat = _parse_iso(node.get("heartbeat_at")) if node else None
    if beat is None or (now - offset) - beat > silent_after:
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
    generated = data.get("generated_ts")
    offset = (
        checked_at - generated
        if type(generated) is int and 1_000_000_000 <= generated < 10_000_000_000
        else 0.0
    )
    return {
        "url": url,
        "name": (node or {}).get("name") or _text(data.get("host"), 100),
        "reachable": True,
        "checked_at": _iso(checked_at),
        "clock_offset": offset,
        "error": None,
        "state": _state_at(node, checked_at, silent_after, offset),
        "node": node,
        "last_known": None,
        "needs_you": _clean_questions(data.get("needs_you")),
    }


class PeerReader:
    """Reads every peer on a timer; ``snapshot()`` returns the latest
    ``fleet`` block and never waits on the network.

    Each peer is read in its own thread, at most one read in flight per
    peer, and each read's result is published the moment it arrives. The
    deadline, ``timeout_s * 2`` from a read's start, is checked when a
    cycle waits for its reads and again at each later cycle while the read
    is still going; past it, the peer is reported as "timeout". The read
    is left to finish (a late but complete answer is published then, with
    its own time), and its peer is not read again until it has. So a stuck
    peer costs one thread and holds up no other peer.
    """

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
        # Per peer: (when the read started, its identity) while one runs.
        self._in_flight: dict[str, tuple[float, object]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="ctrlrelay-web-fleet", daemon=True,
        )

    def _publish(
        self,
        url: str,
        data: dict[str, Any] | None,
        error: str | None,
        *,
        only_if_running: object | None = None,
        finishing: object | None = None,
    ) -> None:
        """Record one peer's entry. With ``only_if_running``, only while
        that read is still unfinished: a "timeout" must never land on top
        of the answer that read just delivered."""
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
            running = self._in_flight.get(url)
            if only_if_running is not None:
                if running is None or running[1] is not only_if_running:
                    return
            self._entries[url] = entry
            # The answer and "this read is done" in one step, so a deadline
            # can never see an answered read as still running.
            if finishing is not None and running is not None and running[1] is finishing:
                del self._in_flight[url]

    def _read(self, url: str, token: object) -> None:
        try:
            data, error = self._fetch(url, timeout=self._timeout), None
        except Exception as exc:
            data, error = None, _peer_error(exc)
        try:
            self._publish(url, data, error, finishing=token)
        finally:
            with self._lock:  # never leave the peer marked as being read
                if self._in_flight.get(url, (0, None))[1] is token:
                    del self._in_flight[url]

    def read_once(self, wait: bool = True) -> None:
        """Start a read of every peer not already being read, and report
        those still going past their deadline as "timeout". With ``wait``,
        return once every read finished or passed its deadline."""
        now = self._clock()
        threads = []
        for url in self.peers:
            token = object()
            with self._lock:
                running = self._in_flight.get(url)
                if running is None:
                    self._in_flight[url] = (now, token)
            if running is not None:
                if now - running[0] > self._timeout * 2:
                    self._publish(url, None, "timeout", only_if_running=running[1])
                continue
            t = threading.Thread(
                target=self._read, args=(url, token), daemon=True,
                name="ctrlrelay-web-peer",
            )
            try:
                t.start()
            except RuntimeError as exc:  # no thread to be had: try next cycle
                with self._lock:
                    self._in_flight.pop(url, None)
                self._publish(url, None, f"could not start a read: {exc}")
                continue
            threads.append((url, t, token))
        if not wait:
            return
        deadline = time.monotonic() + self._timeout * 2
        for url, t, token in threads:
            t.join(max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                self._publish(url, None, "timeout", only_if_running=token)

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
                        "state": _state_at(
                            entry["node"], now, self._silent_after,
                            entry.get("clock_offset", 0.0),
                        ),
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

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.read_once()
            except Exception as exc:  # never let the thread die quietly
                log_event(
                    _logger, "web.fleet_read_failed",
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
            self._stop.wait(self._interval)
