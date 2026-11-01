"""Setup checks for a machine, and the duplicate-name warnings (#250).

``ctrlrelay node check`` runs :func:`run_checks` and prints each line. The
daemon runs :func:`peers_with_my_name` once at startup and
:func:`claim_refreshed_by_another` on the claim reads it already makes, and
warns when either finds another machine using this machine's ``node_id``:
claims, chat tags and the status page would then mix the two up.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ctrlrelay.core.claims import (
    PROCESS_RUN,
    WORKING,
    ClaimSettings,
    parse_claims,
    safe_node_name,
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None  # None: skipped
    detail: str


def claim_refreshed_by_another(
    comments: list[dict[str, Any]] | None,
    settings: ClaimSettings,
    *,
    started_at: float,
) -> bool:
    """A working claim under this machine's name, written after this
    process started, on an issue this process is not running.

    Each claim names the process that last wrote it (``run``) and when, by
    that process's clock (``seen``). One written by this process, or
    adopted by it (adopting rewrites it), carries ``PROCESS_RUN`` and never
    counts. One from this machine before a restart carries an older run,
    but its ``seen`` is by this machine's clock and not after
    ``started_at``. Another process's claim on a live run is refreshed, so
    it is caught once a refresh is stamped after ``started_at`` by that
    process's clock: on the first refresh when the clocks agree, later (or
    not, on a short run) when its clock is behind. A claim from a version
    before #250 names no run and is never counted. It is a warning, not a
    proof: a clock stepped back across a restart can show this machine's
    own leftover as another process's.

    Only the issues the daemon looks at are read: a refresh on an issue it
    never looks at again is not seen.
    """
    for claim in parse_claims(comments, settings.account):
        if (
            claim.node == settings.node
            and claim.state == WORKING
            and claim.run is not None
            and claim.run != PROCESS_RUN
            and claim.seen is not None
            and claim.seen > started_at
        ):
            return True
    return False


async def fetch_detached(
    fetch: Callable[..., dict[str, Any]], url: str, *, timeout: float,
) -> dict[str, Any]:
    """``fetch(url, timeout=...)`` in a daemon thread, given up after
    ``timeout * 3``. Not an executor thread: one stuck in a name lookup
    would hold the process at exit; a daemon thread is simply dropped."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[dict[str, Any]] = loop.create_future()

    def run() -> None:
        try:
            result = fetch(url, timeout=timeout)
        except BaseException as e:  # noqa: BLE001 - handed to the waiter
            settle = lambda exc=e: done.done() or done.set_exception(exc)  # noqa: E731
        else:
            settle = lambda: done.done() or done.set_result(result)  # noqa: E731
        try:
            loop.call_soon_threadsafe(settle)
        except RuntimeError:
            # Given up on, and the loop has since closed: nobody is waiting.
            pass

    threading.Thread(target=run, daemon=True, name="ctrlrelay-peer-check").start()
    return await asyncio.wait_for(done, timeout=timeout * 3)


def _peer_name(data: Any) -> str | None:
    """The name a peer's status page gives itself, or ``None`` when the
    answer is not a status page."""
    if not isinstance(data, dict) or not isinstance(
        data.get("generated_ts"), (int, float),
    ):
        return None
    node = data.get("node")
    name = node.get("name") if isinstance(node, dict) else None
    if not isinstance(name, str):
        name = data.get("host")
    return name if isinstance(name, str) and name.strip() else None


async def peers_with_my_name(
    node_id: str,
    peers: list[str],
    *,
    fetch: Callable[..., dict[str, Any]],
    timeout: float,
) -> list[str]:
    """The peer URLs whose status page answers with this machine's name."""
    me = safe_node_name(node_id)
    found = []
    for url in peers:
        try:
            data = await fetch_detached(fetch, url, timeout=timeout)
        except Exception:
            continue
        name = _peer_name(data)
        if name is not None and safe_node_name(name) == me:
            found.append(url)
    return found


async def run_checks(
    config: Any,
    *,
    github_login: Callable[[], Awaitable[str]],
    agent_probe: Callable[[], Awaitable[Any]] | None,
    fetch: Callable[..., dict[str, Any]],
) -> list[Check]:
    """Each setup check, in order. ``agent_probe`` is ``None`` to skip the
    agent check, which spends one tiny call on the subscription."""
    checks = [Check("node_id", True, f"{config.node_id} (named on claims, chat and the page)")]

    try:
        login = await github_login()
        checks.append(Check("GitHub login", True, login))
    except Exception as e:
        checks.append(Check(
            "GitHub login", False,
            f"{type(e).__name__}: {str(e)[:200]}. Run `gh auth status`; if it "
            "is not logged in, run `gh auth login` with a token of this "
            "machine's own, on the same account as the other machines",
        ))

    if agent_probe is None:
        checks.append(Check("agent login", None, "skipped (--no-agent)"))
    else:
        started = time.monotonic()
        try:
            reading = await agent_probe()
        except Exception as e:
            reading, error = None, (
                f"{type(e).__name__}: {str(e)[:200]}. Check `agent.binary` in "
                "orchestrator.yaml points at the agent CLI, and run it once by "
                "hand to log in"
            )
        else:
            error = None
        if reading is not None:
            five = reading.windows.get("five_hour")
            used = f"{round(five.used * 100)}% of the 5-hour window" if five else "usage read"
            checks.append(Check("agent login", True, f"works; {used}"))
        else:
            checks.append(Check(
                "agent login", False,
                error or "the agent call returned no usage reading; run "
                "`claude` once by hand to log in, then check again "
                f"({time.monotonic() - started:.0f}s)",
            ))

    peers = list(config.fleet.peers)
    if not peers:
        checks.append(Check("peers", None, "none configured (fleet.peers)"))
    for url in peers:
        try:
            data = await fetch_detached(fetch, url, timeout=config.fleet.timeout_seconds)
        except Exception as e:
            from ctrlrelay.web.fleet import _peer_error

            reason = "timeout" if isinstance(e, TimeoutError) else _peer_error(e)
            checks.append(Check(
                f"peer {url}", False,
                f"{reason}. Check the URL, that the peer's status page is "
                "running (`ctrlrelay web status` there) and listens on an "
                "address this machine can reach (`web.host`), and that this "
                "network allows the connection",
            ))
            continue
        name = _peer_name(data)
        if name is None:
            checks.append(Check(
                f"peer {url}", False,
                "answered, but not with a ctrlrelay status page. Check the "
                "URL points at the peer's `ctrlrelay web` port",
            ))
        elif safe_node_name(name) == safe_node_name(config.node_id):
            checks.append(Check(
                f"peer {url}", False,
                f"answers with this machine's own name {name!r}: two machines "
                "share a node_id, or the peer URL points back at this machine. "
                "Give each machine its own node_id in orchestrator.yaml, and "
                "check this peer URL is the other machine's",
            ))
        else:
            checks.append(Check(f"peer {url}", True, f"reachable, {name}"))
    return checks
