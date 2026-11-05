"""Hold back an issue whose GitHub "blocked by" issues are still open (#310).

A plan can carry its order as GitHub issue dependencies: B is blocked by A
because both change the same files. The poller used to start B as soon as
it was labelled and assigned, whatever A's state. Now, before a run is
claimed, the issue's blockers are read from GitHub, and an issue with an
open blocker is left for a later poll.

The dependency link is the only source. Free text such as "Starts after:
#A" in the issue body is not read: it cannot be told apart from prose.

"Could not read the blockers" (an API error, a rate limit, a 404 from a
GitHub without the dependencies API) is never read as "no blockers": the
issue is held the same way and asked about again on the next poll.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.blockers")


@dataclass(frozen=True)
class BlockerHold:
    """Why an issue was not started: ``line`` for the console, and
    ``chat`` for the chat, set only the first time the issue is held for
    this reason so the chat hears it once, not every poll."""

    line: str
    chat: str | None


def blocker_ref(repo: str, blocker: dict[str, Any]) -> str:
    """``"#N"`` for a blocker in ``repo``, ``"owner/repo#N"`` for one in
    another repository."""
    other = str(blocker["repo"])
    if other.lower() == repo.lower():
        return f"#{blocker['number']}"
    return f"{other}#{blocker['number']}"


async def hold_if_blocked(
    github: Any, poller: Any, repo: str, issue_number: int,
) -> BlockerHold | None:
    """Read the issue's blockers and decide whether it may start.

    ``None``: GitHub answered and none is open, so the run goes ahead (and
    any earlier hold is forgotten). Otherwise the issue is recorded on the
    poller as held, which also leaves it out of ``seen_issues`` so the next
    poll offers it again, and the returned hold says why.
    """
    where = f"#{issue_number} in {repo}"
    try:
        blockers = await github.list_blocked_by(repo, issue_number)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"
        log_event(
            _logger, "poll.issue.blockers_unknown", repo=repo,
            issue_number=issue_number, error=error,
        )
        news = poller.note_blocked(repo, issue_number, None)
        return BlockerHold(
            line=(
                f"{where} left for the next poll: could not read its "
                f"GitHub blockers ({error})."
            ),
            chat=(
                f"⏸️ {where} is not started: its GitHub \"blocked by\" "
                f"issues could not be read ({error}). It is asked again "
                "every poll and starts once they can be read and none is "
                "open."
            ) if news else None,
        )

    still_open = [
        blocker_ref(repo, b) for b in blockers if b["state"] == "open"
    ]
    if not still_open:
        poller.clear_blocked(repo, issue_number)
        return None

    log_event(
        _logger, "poll.issue.blocked", repo=repo,
        issue_number=issue_number, blocked_by=still_open,
    )
    news = poller.note_blocked(repo, issue_number, still_open)
    names = ", ".join(still_open)
    return BlockerHold(
        line=f"{where} left for later: blocked by {names}.",
        chat=(
            f"⏸️ {where} is not started: it is blocked by {names} on "
            "GitHub. It starts on the first poll after "
            + ("that closes." if len(still_open) == 1 else "those close.")
        ) if news else None,
    )
