"""The status page's HTTP server (#209).

Standard library only. ``GET`` is the only method served; every other
method is ``405``, so no request can change anything. Routes:

- ``/``: the page, one HTML file with inline CSS and JS.
- ``/api/status``: :func:`ctrlrelay.web.status.build_status`.
- ``/api/events?since=<epoch seconds>``: :func:`list_feed`. Without
  ``since`` the history window is used; a ``since`` that is not one
  integer is ``400``.

Before any routing, for every method, the ``Host`` header must name this
server: loopback, ``web.host``, this machine's hostname (and ``.local``) or
one of ``web.allowed_hosts``, optionally with ``:web.port``. Anything else,
or no ``Host`` at all, is ``421``. Without that, a page on any other site
could rebind its own hostname to this address and read ``/api/status``.

Handlers never call GitHub. The waiting line is refreshed by
:class:`WaitingRefresher` on its own asyncio loop in a daemon thread, and
a handler reads only the tracker's last snapshot and titles. A handler
that raises answers ``500 {"error": "internal error"}``; the traceback goes
to the log, never to the client.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from ctrlrelay import __version__
from ctrlrelay.core.config import Config, WebConfig
from ctrlrelay.core.obs import get_logger, log_event
from ctrlrelay.web.status import build_status, list_feed

_logger = get_logger("web.server")

CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
    "img-src 'self' data:; frame-ancestors 'none'"
)
_REFRESH_PLACEHOLDER = "__REFRESH_SECONDS__"
_ALL_INTERFACES = {"", "0.0.0.0", "::", "[::]"}
# Distinct refused Host values remembered for "log once"; past this many
# the log stays quiet rather than the set growing without bound.
_MISDIRECTED_LOG_LIMIT = 256


class _Tracker(Protocol):
    def snapshot(self) -> dict[str, Any]: ...
    def titles(self) -> dict[str, str]: ...


class _Refreshable(Protocol):
    async def refresh(self) -> None: ...


def bind_warning(host: str) -> str | None:
    """A warning when ``host`` listens on every interface, else ``None``."""
    if host.strip() in _ALL_INTERFACES:
        return (
            f"web.host is {host!r}: the status page listens on every network "
            "interface. It has no login and names private repositories; set "
            "web.host to this machine's LAN address and never expose it to "
            "the internet."
        )
    return None


def _norm_name(name: str) -> str:
    return name.strip().lower().rstrip(".")


def allowed_host_names(web: WebConfig) -> set[str]:
    """Names a ``Host`` header may carry, lowercased, trailing dot stripped."""
    hostname = socket.gethostname()
    names = {"localhost", "127.0.0.1", "[::1]", hostname, hostname + ".local",
             *web.allowed_hosts}
    if web.host.strip() not in _ALL_INTERFACES:
        host = web.host.strip()
        names.add(f"[{host}]" if ":" in host and not host.startswith("[") else host)
    return {n for n in (_norm_name(x) for x in names) if n}


def host_allowed(header: str | None, names: set[str], port: int) -> bool:
    """Whether ``header`` is one of ``names``, alone or as ``name:port``."""
    if not header:
        return False
    value = header.strip().lower()
    name, port_text = value, None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return False
        name, rest = value[: end + 1], value[end + 1:]
        if rest:
            if not rest.startswith(":"):
                return False
            port_text = rest[1:]
    elif ":" in value:
        name, _, port_text = value.rpartition(":")
    if port_text is not None and port_text != str(port):
        return False
    return _norm_name(name) in names


def load_page(refresh_seconds: int) -> bytes:
    """The packaged page, with the refresh interval filled in."""
    html = (
        resources.files("ctrlrelay.web").joinpath("static/index.html")
        .read_text(encoding="utf-8")
    )
    if _REFRESH_PLACEHOLDER not in html:
        raise RuntimeError("static/index.html has no refresh placeholder")
    return html.replace(_REFRESH_PLACEHOLDER, str(int(refresh_seconds))).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server: StatusServer
    server_version = "ctrlrelay-web"
    sys_version = ""

    # Every method but GET. BaseHTTPRequestHandler looks up ``do_<METHOD>``
    # and answers 501 when it is missing; this answers 405 for all of them,
    # including methods nobody listed.
    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_") and name != "do_GET":
            return self._not_allowed
        raise AttributeError(name)

    def _host_ok(self) -> bool:
        """Answers 421 and returns False when the Host header does not name
        this server. Runs first, for every method."""
        header = self.headers.get("Host")
        if host_allowed(header, self.server.host_names, self.server.config.web.port):
            return True
        self.server.note_misdirected(header)
        self._json(421, {"error": "misdirected request"})
        return False

    def end_headers(self) -> None:
        # Here rather than per response, so the stdlib's own error pages
        # (a malformed request line) carry them too.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # One line per poll would bury everything else in web.log.
        _logger.debug("web.request " + (format % args))

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _not_allowed(self) -> None:
        if not self._host_ok():
            return
        self._json(405, {"error": "method not allowed"}, {"Allow": "GET"})

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self._host_ok():
                self._route()
        except Exception:
            _logger.exception("web.handler_failed", extra={"path": self.path[:200]})
            try:
                self._json(500, {"error": "internal error"})
            except Exception:
                pass

    def _route(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        srv = self.server
        if url.path == "/":
            self._send(200, srv.page, "text/html; charset=utf-8")
        elif url.path == "/api/status":
            self._json(200, srv.status())
        elif url.path == "/api/events":
            query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
            values = query.get("since")
            if values is None:
                since = srv.default_since()
            else:
                if len(values) != 1 or not _is_int(values[0]):
                    self._json(400, {"error": "since must be one integer (epoch seconds)"})
                    return
                since = int(values[0])
            self._json(200, list_feed(db_path=srv.config.paths.state_db, since_ts=since))
        else:
            self._json(404, {"error": "not found"})


def _is_int(text: str) -> bool:
    text = text.strip()
    if text.startswith("-"):
        text = text[1:]
    return text.isascii() and text.isdigit()


class StatusServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        config: Config,
        waiting: _Tracker,
        *,
        home: Path | None = None,
    ) -> None:
        self.config = config
        self.waiting = waiting
        self.home = home
        self.page = load_page(config.web.refresh_seconds)
        self.host_names = allowed_host_names(config.web)
        self._misdirected: set[str] = set()
        self._misdirected_lock = threading.Lock()
        host = config.web.host
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, config.web.port), _Handler)

    def note_misdirected(self, header: str | None) -> None:
        """Log a refused Host once per distinct value, not once per request."""
        shown = "(none)" if header is None else header[:200]
        with self._misdirected_lock:
            if shown in self._misdirected or len(self._misdirected) >= _MISDIRECTED_LOG_LIMIT:
                return
            self._misdirected.add(shown)
        _logger.warning("web.misdirected_host", extra={"host": shown})

    def default_since(self) -> int:
        return int(time.time()) - self.config.web.history_days * 86_400

    def status(self) -> dict[str, Any]:
        web = self.config.web
        return build_status(
            db_path=self.config.paths.state_db,
            waiting=self.waiting.snapshot(),
            titles=self.waiting.titles(),
            version=__version__,
            host=self.config.node_id,
            stale_after_s=web.stale_after_minutes * 60,
            history_days=web.history_days,
            home=self.home,
        )


def make_server(
    config: Config, *, waiting: _Tracker, home: Path | None = None,
) -> ThreadingHTTPServer:
    """Bind ``config.web.host:port`` and return the (not yet serving) server.

    Logs one ``web.started`` line naming the bound address, and a warning
    when the host is every interface."""
    srv = StatusServer(config, waiting, home=home)
    host, port = srv.server_address[:2]
    shown = f"[{host}]" if ":" in str(host) else host
    log_event(_logger, "web.started", address=f"http://{shown}:{port}/",
              version=__version__)
    warning = bind_warning(config.web.host)
    if warning:
        _logger.warning(warning)
    return srv


class WaitingRefresher:
    """Refreshes the waiting line every ``interval_s`` on its own asyncio
    loop in a daemon thread. A failed refresh is logged and the next one
    still runs; request threads never wait for it."""

    thread_name = "ctrlrelay-web-waiting"

    def __init__(self, tracker: _Refreshable, *, interval_s: float) -> None:
        self._tracker = tracker
        self._interval = interval_s
        self._loop = asyncio.new_event_loop()
        # Not bound to a loop until first awaited, so it can be made here
        # and a stop() that comes before the first refresh is not lost.
        self._stop = asyncio.Event()
        self._thread = threading.Thread(
            target=self._run, name=self.thread_name, daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._request_stop)
            self._thread.join(timeout)

    def _request_stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tracker.refresh()
            except Exception as exc:
                _logger.warning(
                    "web.waiting_refresh_failed",
                    extra={"error": f"{type(exc).__name__}: {exc}"[:300]},
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
