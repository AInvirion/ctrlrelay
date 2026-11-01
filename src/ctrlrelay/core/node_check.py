"""Setup checks for a machine, and the duplicate-name warnings (#250).

``ctrlrelay node check`` runs :func:`run_checks` and prints each line. The
daemon runs :func:`peers_with_my_name` once at startup and
:func:`claim_refreshed_by_another` on the claim reads it already makes, and
warns when either finds another machine using this machine's ``node_id``:
claims, chat tags and the status page would then mix the two up.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ctrlrelay.core.claims import WORKING, ClaimSettings, parse_claims, safe_node_name


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None  # None: skipped
    detail: str


def claim_refreshed_by_another(
    comments: list[dict[str, Any]] | None, settings: ClaimSettings, now: float,
) -> bool:
    """A working claim under this machine's name, refreshed within the last
    1.5 refresh intervals, on an issue this machine is not running.

    Called where the daemon peeks at an issue it is about to consider, so
    it holds no claim there. Its own claims from before a restart are not
    refreshed any more and age out of the window; one that is still being
    refreshed is another process using this name.
    """
    window = settings.refresh_seconds * 1.5
    for claim in parse_claims(comments, settings.account):
        if (
            claim.node == settings.node
            and claim.state == WORKING
            and claim.updated_at is not None
            and now - claim.updated_at <= window
        ):
            return True
    return False


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
            data = await asyncio.wait_for(
                asyncio.to_thread(fetch, url, timeout=timeout), timeout=timeout * 3,
            )
        except Exception:
            continue
        node = data.get("node") if isinstance(data, dict) else None
        name = node.get("name") if isinstance(node, dict) else None
        if isinstance(name, str) and safe_node_name(name) == me:
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
        checks.append(Check("GitHub login", False, f"{type(e).__name__}: {str(e)[:200]}"))

    if agent_probe is None:
        checks.append(Check("agent login", None, "skipped (--no-agent)"))
    else:
        started = time.monotonic()
        try:
            reading = await agent_probe()
        except Exception as e:
            reading, error = None, f"{type(e).__name__}: {str(e)[:200]}"
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
            data = await asyncio.wait_for(
                asyncio.to_thread(fetch, url, timeout=config.fleet.timeout_seconds),
                timeout=config.fleet.timeout_seconds * 3,
            )
        except Exception as e:
            from ctrlrelay.web.fleet import _peer_error

            checks.append(Check(f"peer {url}", False, _peer_error(e)))
            continue
        node = data.get("node") if isinstance(data.get("node"), dict) else {}
        name = node.get("name") or data.get("host")
        if isinstance(name, str) and safe_node_name(name) == safe_node_name(config.node_id):
            checks.append(Check(
                f"peer {url}", False,
                f"answers with this machine's own name {name!r}: two machines "
                "share a node_id, or the peer URL points back at this machine",
            ))
        else:
            checks.append(Check(f"peer {url}", True, f"reachable, {name}"))
    return checks
