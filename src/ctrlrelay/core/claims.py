"""Claims that name the machine, so several machines can share one account.

Two ctrlrelay machines on one GitHub account see the same assigned
issue, and each one's "already seen" list and run lock live on that
machine only. So the claim comment on the issue is the one place both
can read, and it has to say which machine took the issue (#245).

The rules, in the order they are applied:

* A claim is a comment by the relay's own account carrying
  ``<!-- ctrlrelay:claimed node=... id=... state=... -->``. A comment
  by anyone else is ignored, so nobody can hold or free an issue by
  pasting the marker.
* A claim from before #245 (the bare marker) names no machine. It
  counts as taken by ``unknown`` and never goes stale, because nothing
  ever refreshed it and its age says nothing about whether it is done.
* ``<!-- ctrlrelay:released id=... -->`` ends one claim. The older
  failure retraction with no id ends the older, unnamed claims before it,
  and never a named one.
* A ``working`` claim is refreshed by its owner every
  ``refresh_seconds``. One not refreshed for ``stale_after_seconds`` is
  stale: its machine stopped, and another may take the issue. A
  ``held`` claim (the run finished, or is waiting on a question) never
  goes stale.
* Race check: post the claim, wait ``race_wait_seconds``, read again.
  The live claim with the lowest comment id wins. GitHub numbers the
  comments, so no machine's clock decides the order. A loser posts a
  release for its own claim and drops the issue.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.claims")

# The prefix every claim marker starts with, old and new. Used to keep
# our own comments out of the agent's prompt.
CLAIM_MARKER_PREFIX = "<!-- ctrlrelay:claimed"
RELEASE_MARKER_PREFIX = "<!-- ctrlrelay:released"
# The retraction #204 posts. Kept here so the parser and the poster
# agree on one string; ``pipelines.dev`` re-exports it.
DISPATCH_FAILED_MARKER = "<!-- ctrlrelay:dispatch-failed -->"

UNKNOWN_NODE = "unknown"
WORKING = "working"
HELD = "held"

_CLAIM_RE = re.compile(r"<!-- ctrlrelay:claimed(?P<attrs>[^>]*?)-->")
_RELEASE_RE = re.compile(r"<!-- ctrlrelay:released id=(?P<id>[A-Za-z0-9]+) -->")
_ATTR_RE = re.compile(r"(\w+)=([^\s]+)")


def safe_node_name(node: str) -> str:
    """The node name as it can sit inside an HTML comment marker.

    ``node_id`` is free-form in config (it defaults to the hostname), and
    a ``>`` or a space would end the marker early or split it. Percent-
    encoded rather than replaced, so two different names never become
    one: ``host a`` and ``host-a`` would otherwise both read as
    ``host-a``, and each machine would take the other's claim for its own.
    """
    name = node.strip()
    if not name:
        raise ValueError("a machine needs a non-empty node_id to claim issues")
    if name == UNKNOWN_NODE:
        # The owner name of claims from before #245: a machine called
        # this would take every one of them for its own.
        raise ValueError(f"node_id {UNKNOWN_NODE!r} is reserved; set another")
    return quote(name, safe="._-")


@dataclass(frozen=True)
class ClaimSettings:
    """What a machine needs to claim issues on a shared account.

    ``account`` is the GitHub login the relay posts as; claims by any
    other author are ignored.
    """

    node: str
    account: str
    race_wait_seconds: float = 5.0
    refresh_seconds: float = 300.0
    stale_after_seconds: float = 1800.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "node", safe_node_name(self.node))


@dataclass(frozen=True)
class Claim:
    comment_id: int
    node: str
    claim_id: str | None
    state: str
    updated_at: float | None
    # The claim ids this one took over, comma-separated, when it replaced
    # stale ones. Kept on every refresh: it is how a machine that comes
    # back finds out its claim was taken.
    replaces: str | None = None

    def stale_at(self, stale_after: float) -> float | None:
        """When this claim goes stale, or ``None`` if it never does."""
        if self.state != WORKING or self.updated_at is None:
            return None
        return self.updated_at + stale_after


@dataclass(frozen=True)
class ClaimOutcome:
    """``won`` with our ``claim``, or lost to ``owner``."""

    won: bool
    claim: Claim | None = None
    owner: Claim | None = None


def _parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _now_iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def claim_body(
    node: str,
    claim_id: str,
    state: str,
    now: float,
    *,
    replaces: str | None = None,
    taking_over_from: list[Claim] | None = None,
) -> str:
    """The claim comment. ``seen`` changes on every refresh, so the edit
    is a real change and GitHub moves the comment's ``updated_at``."""
    if state == WORKING:
        line = (
            f"Taken by ctrlrelay on **{node}**. It is working on this "
            "issue; a pull request or a report will follow."
        )
    else:
        line = (
            f"Taken by ctrlrelay on **{node}**. The run has stopped "
            "(finished, or waiting for an answer); this machine keeps the "
            "issue."
        )
    if taking_over_from:
        nodes = ", ".join(sorted({c.node for c in taking_over_from}))
        line += (
            f" The earlier claim by {nodes} was not refreshed in time, so "
            "that machine is treated as stopped."
        )
    replaced = f" replaces={replaces}" if replaces else ""
    marker = (
        f"{CLAIM_MARKER_PREFIX} node={node} id={claim_id} state={state}"
        f"{replaced} seen={_now_iso(now)} -->"
    )
    return f"{line}\n\n{marker}"


def release_marker(claim_id: str) -> str:
    return f"{RELEASE_MARKER_PREFIX} id={claim_id} -->"


def backing_off_body(node: str, owner: str, claim_id: str) -> str:
    return (
        f"Backing off: ctrlrelay on **{owner}** has this issue, so "
        f"{node} dropped it.\n\n{release_marker(claim_id)}"
    )


def _author(comment: dict[str, Any]) -> str | None:
    user = comment.get("user") or comment.get("author") or {}
    return user.get("login") if isinstance(user, dict) else None


def parse_claims(comments: list[dict[str, Any]] | None, account: str) -> list[Claim]:
    """Every claim not yet released, oldest first. Stale ones included."""
    claims: list[Claim] = []
    for comment in comments or []:
        # Logins are case-insensitive on GitHub.
        if (_author(comment) or "").lower() != account.lower():
            continue
        body = comment.get("body") or ""
        released = _RELEASE_RE.search(body)
        if released:
            gone = released.group("id")
            claims = [c for c in claims if c.claim_id != gone]
            continue
        if DISPATCH_FAILED_MARKER in body:
            # A failure comment with no release id comes from a version
            # before #245, whose own claim had no id either. It ends those
            # claims only: on a shared account, a machine still on the old
            # version must not free a newer machine's claim.
            claims = [c for c in claims if c.claim_id is not None]
            continue
        match = _CLAIM_RE.search(body)
        if not match:
            continue
        attrs = dict(_ATTR_RE.findall(match.group("attrs")))
        try:
            comment_id = int(comment.get("id"))
        except (TypeError, ValueError):
            # Unreadable, so it counts as taken, never as free.
            claims.append(Claim(0, UNKNOWN_NODE, None, HELD, None))
            continue
        if "node" not in attrs:
            claims.append(Claim(comment_id, UNKNOWN_NODE, None, HELD, None))
            continue
        claims.append(
            Claim(
                comment_id=comment_id,
                node=attrs["node"],
                claim_id=attrs.get("id"),
                state=attrs.get("state", HELD),
                updated_at=_parse_time(
                    comment.get("updated_at") or comment.get("created_at")
                ),
                replaces=attrs.get("replaces"),
            )
        )
    return claims


def live_claims(
    comments: list[dict[str, Any]] | None,
    settings: ClaimSettings,
    now: float,
) -> list[Claim]:
    """Unreleased, unexpired claims, lowest comment id first.

    A claim that another unreleased claim names in ``replaces`` is out,
    whatever its comment id and however fresh. Editing a comment is not
    a compare-and-swap, so a machine that comes back after its claim was
    taken can always refresh it once more before it notices. Without
    this rule that one refresh would put the old claim, with its lower
    id, back in first place while the taker is running.
    """
    claims = parse_claims(comments, settings.account)
    replaced = _replaced_ids(claims)
    live = []
    for claim in claims:
        if claim.claim_id in replaced:
            continue
        stale_at = claim.stale_at(settings.stale_after_seconds)
        if stale_at is not None and now >= stale_at:
            continue
        live.append(claim)
    return sorted(live, key=lambda c: c.comment_id)


def _replaced_ids(claims: list[Claim]) -> set[str]:
    return {
        cid
        for c in claims if c.replaces
        for cid in c.replaces.split(",")
    }


def other_owner(
    comments: list[dict[str, Any]] | None,
    settings: ClaimSettings,
    now: float,
) -> Claim | None:
    """The live claim of another machine that outranks any of ours."""
    live = live_claims(comments, settings, now)
    if live and live[0].node != settings.node:
        return live[0]
    return None


def _stale_claims(
    comments: list[dict[str, Any]] | None, settings: ClaimSettings, now: float,
) -> list[Claim]:
    """Other machines' claims that went stale: the ones a new claim takes."""
    out = []
    for claim in parse_claims(comments, settings.account):
        stale_at = claim.stale_at(settings.stale_after_seconds)
        if (
            claim.node != settings.node and claim.claim_id is not None
            and stale_at is not None and now >= stale_at
        ):
            out.append(claim)
    return out


async def acquire_claim(
    github: Any,
    repo: str,
    issue_number: int,
    settings: ClaimSettings,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> ClaimOutcome:
    """Take the issue for this machine, or say who has it.

    A live claim of our own (an earlier attempt on this machine) is
    adopted rather than duplicated.
    """
    comments = await github.list_issue_comments(repo, issue_number)
    now = clock()
    owner = other_owner(comments, settings, now)
    if owner is not None:
        return ClaimOutcome(won=False, owner=owner)

    live = live_claims(comments, settings, now)
    if live and live[0].claim_id is not None:
        mine = live[0]
        # An earlier run's background hold must not mark this new run's
        # claim held after it starts working.
        pending = _BACKGROUND_HOLDS.pop(mine.claim_id, None)
        if pending is not None:
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
        await github.edit_issue_comment(
            repo, mine.comment_id,
            claim_body(
                settings.node, mine.claim_id, WORKING, now, replaces=mine.replaces,
            ),
        )
        return ClaimOutcome(
            won=True,
            claim=dataclasses.replace(mine, state=WORKING, updated_at=now),
        )

    claim_id = secrets.token_hex(6)
    stale = _stale_claims(comments, settings, now)
    # Everything already replaced is replaced again, so the history does
    # not depend on the claim that first recorded it: if that claim is
    # later released, what it had taken must not come back to life.
    already = _replaced_ids(parse_claims(comments, settings.account))
    ids = {c.claim_id for c in stale if c.claim_id} | already
    replaces = ",".join(sorted(ids)) or None
    comment_id = await github.post_issue_comment(
        repo, issue_number,
        claim_body(
            settings.node, claim_id, WORKING, now,
            replaces=replaces, taking_over_from=stale,
        ),
    )
    await sleep(settings.race_wait_seconds)

    comments = await github.list_issue_comments(repo, issue_number)
    live = live_claims(comments, settings, clock())
    if not any(c.claim_id == claim_id for c in live):
        # The read does not show the claim just posted, so it cannot say
        # whether an older one is hiding from it too. Not a win: release
        # ours and let the run fail before it starts, to be retried.
        await _post_release(github, repo, issue_number, settings, claim_id, None)
        raise ClaimNotVisibleError(
            f"the claim posted on {repo}#{issue_number} was not in the "
            "comments read back, so the race check could not be decided"
        )
    winner = live[0]
    if winner.claim_id == claim_id:
        log_event(
            _logger, "claims.won", repo=repo, issue_number=issue_number,
            node=settings.node, claim_id=claim_id,
        )
        return ClaimOutcome(
            won=True,
            claim=Claim(
                comment_id, settings.node, claim_id, WORKING, now, replaces=replaces,
            ),
        )

    log_event(
        _logger, "claims.lost", repo=repo, issue_number=issue_number,
        node=settings.node, owner=winner.node,
    )
    await _post_release(github, repo, issue_number, settings, claim_id, winner.node)
    return ClaimOutcome(won=False, owner=winner)


class ClaimNotVisibleError(Exception):
    """The race check could not see the claim it had just posted."""


async def _post_release(
    github: Any,
    repo: str,
    issue_number: int,
    settings: ClaimSettings,
    claim_id: str,
    owner: str | None,
) -> None:
    if owner is not None:
        body = backing_off_body(settings.node, owner, claim_id)
    else:
        body = (
            f"Backing off: ctrlrelay on **{settings.node}** could not confirm "
            "its claim and will try again later.\n\n"
            f"{release_marker(claim_id)}"
        )
    try:
        await github.post_issue_comment(repo, issue_number, body)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Our claim then stands unreleased. Nobody refreshes it, so as a
        # working claim it goes stale on its own; until then it is younger
        # than the winner's and never outranks it.
        log_event(
            _logger, "claims.backoff_failed", repo=repo,
            issue_number=issue_number, error=str(e)[:200],
        )


class ClaimKeeper:
    """Refreshes a won claim while the run works, then settles it.

    The refresh is what lets another machine tell a stopped run from a
    long one. A failed refresh is logged and retried at the next tick;
    it is never allowed to fail the run.
    """

    def __init__(
        self,
        github: Any,
        repo: str,
        issue_number: int,
        claim: Claim,
        settings: ClaimSettings,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if claim.claim_id is None:
            raise ValueError("a claim without an id cannot be refreshed")
        self.github = github
        self.repo = repo
        self.claim = claim
        self.claim_id: str = claim.claim_id
        self.settings = settings
        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        # One write at a time: a hold must not be overtaken on the wire by
        # a refresh that was already in flight when the run ended.
        self._write_lock = asyncio.Lock()
        self.issue_number = issue_number
        # Set when another machine took the issue over while this run was
        # still working: the claim went stale (GitHub unreachable for the
        # whole stale time) and is no longer this machine's.
        self.taken_over_by: str | None = None
        # When GitHub last confirmed a write of this claim: the stale time
        # runs from here, in every other machine's eyes.
        self._confirmed_at: float = (
            claim.updated_at if claim.updated_at is not None else clock()
        )

    async def _write(self, state: str) -> bool:
        async with self._write_lock:
            return await self._write_unlocked(state)

    async def _write_unlocked(self, state: str) -> bool:
        try:
            await self.github.edit_issue_comment(
                self.repo, self.claim.comment_id,
                claim_body(
                    self.settings.node, self.claim_id, state, self._clock(),
                    replaces=self.claim.replaces,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_event(
                _logger, "claims.refresh_failed", repo=self.repo,
                comment_id=self.claim.comment_id, state=state,
                error=str(e)[:200],
            )
            return False
        self._confirmed_at = self._clock()
        return True

    async def _taken_over(self) -> tuple[bool, str | None]:
        """``(checked, taker)``: whether the comments could be read, and
        the machine whose claim names ours as the one it replaced.

        Refreshing a claim that was taken would make it outrank the
        taker's in every other machine's eyes. Only a claim that names
        ours counts: a race loser whose release failed also leaves a
        later claim, and it took nothing.
        """
        try:
            comments = await self.github.list_issue_comments(
                self.repo, self.issue_number,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_event(
                _logger, "claims.takeover_check_failed", repo=self.repo,
                issue_number=self.issue_number, error=str(e)[:200],
            )
            return False, None
        for c in parse_claims(comments, self.settings.account):
            if c.replaces and self.claim_id in c.replaces.split(","):
                return True, c.node
        return True, None

    async def _loop(self) -> None:
        while True:
            await self._sleep(self.settings.refresh_seconds)
            checked, taker = await self._taken_over()
            if not checked:
                # Could not tell whether the claim is still ours, so it is
                # not refreshed this time. One missed tick is far inside
                # the stale time; refreshing a taken claim is not.
                continue
            if taker is not None:
                self.taken_over_by = taker
                _logger.error(
                    "claims.taken_over",
                    extra={
                        "repo": self.repo, "issue_number": self.issue_number,
                        "node": self.settings.node, "taker": taker,
                    },
                )
                return
            await self._write(WORKING)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def hold(self, attempts: int = 3) -> bool:
        """Stop refreshing and mark the claim ``held``: the run is over
        or waiting on a question, and the issue stays with this machine.

        A claim left ``working`` goes stale, and another machine would
        then take an issue whose run already opened a pull request or
        asked a question. So it is tried ``attempts`` times here, and if
        GitHub is still refusing, again in the background every
        ``refresh_seconds`` until the stale time counted from the last
        write GitHub confirmed. That retry lives only as long as this
        process, and a new run adopting the claim cancels it. Returns
        whether it was written here; a claim taken over is not reclaimed
        (and could not be: the taker's claim names it as replaced).
        """
        await self.stop()
        if self.taken_over_by is not None:
            return False
        for attempt in range(attempts):
            if await self._write(HELD):
                return True
            if attempt < attempts - 1:
                await self._sleep(min(5.0 * (attempt + 1), 30.0))
        _logger.error(
            "claims.hold_failed",
            extra={
                "repo": self.repo, "issue_number": self.issue_number,
                "comment_id": self.claim.comment_id,
            },
        )
        task = asyncio.create_task(self._hold_later())
        _BACKGROUND_HOLDS[self.claim_id] = task

        def _forget(t: asyncio.Task[None], cid: str = self.claim_id) -> None:
            if _BACKGROUND_HOLDS.get(cid) is t:
                del _BACKGROUND_HOLDS[cid]

        task.add_done_callback(_forget)
        return False

    async def _hold_later(self) -> None:
        deadline = self._confirmed_at + self.settings.stale_after_seconds
        while True:
            await self._sleep(self.settings.refresh_seconds)
            if self._clock() >= deadline:
                return
            checked, taker = await self._taken_over()
            if taker is not None:
                return
            if checked and await self._write(HELD):
                log_event(
                    _logger, "claims.hold_recovered", repo=self.repo,
                    issue_number=self.issue_number,
                )
                return


# Background hold retries by claim id: referenced so they are not
# collected mid-run, and found by id so a new run can cancel one.
_BACKGROUND_HOLDS: dict[str, asyncio.Task[None]] = {}
