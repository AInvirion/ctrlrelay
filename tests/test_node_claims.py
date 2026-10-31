"""Claims that name the machine (#245).

The fake below is one issue's comment thread shared by every machine
in a test, with GitHub's own behaviour: ids grow in posting order and
an edit moves ``updated_at``. Two machines racing are two coroutines
on that one thread.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from ctrlrelay.core.claims import (
    DISPATCH_FAILED_MARKER,
    HELD,
    WORKING,
    ClaimKeeper,
    ClaimSettings,
    acquire_claim,
    claim_body,
    live_claims,
    other_owner,
    parse_claims,
    release_marker,
    safe_node_name,
)

ACCOUNT = "relay-bot"
REPO = "o/r"


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Thread:
    """One issue's comments as the REST API returns them."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.comments: list[dict] = []
        self._next = 1000

    def add(self, body: str, author: str = ACCOUNT) -> int:
        self._next += 1
        now = _iso(self.clock())
        self.comments.append({
            "id": self._next, "body": body, "user": {"login": author},
            "created_at": now, "updated_at": now,
        })
        return self._next

    async def list_issue_comments(self, repo: str, issue_number: int) -> list[dict]:
        return [dict(c) for c in self.comments]

    async def post_issue_comment(self, repo: str, issue_number: int, body: str) -> int:
        return self.add(body)

    async def edit_issue_comment(self, repo: str, comment_id: int, body: str) -> None:
        for c in self.comments:
            if c["id"] == comment_id:
                c["body"] = body
                c["updated_at"] = _iso(self.clock())
                return
        raise RuntimeError("404")


def settings(node: str, **kw) -> ClaimSettings:
    kw.setdefault("race_wait_seconds", 5)
    kw.setdefault("refresh_seconds", 300)
    kw.setdefault("stale_after_seconds", 1800)
    return ClaimSettings(node=node, account=ACCOUNT, **kw)


async def no_sleep(_s: float) -> None:
    await asyncio.sleep(0)


class TestParsing:
    def test_a_claim_names_its_machine_and_state(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add(claim_body("node-a", "abc123", WORKING, clock()))
        [claim] = parse_claims(thread.comments, ACCOUNT)
        assert (claim.node, claim.claim_id, claim.state) == ("node-a", "abc123", WORKING)
        assert claim.updated_at == clock()

    def test_a_claim_from_before_245_counts_as_taken_by_unknown_and_never_expires(
        self,
    ) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add("CTRLRelay is working on this issue.\n\n<!-- ctrlrelay:claimed -->")
        clock.t += 10 * 86400
        [claim] = live_claims(thread.comments, settings("node-b"), clock())
        assert claim.node == "unknown"
        assert claim.state == HELD

    def test_a_marker_pasted_by_someone_else_is_not_a_claim(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add(claim_body("node-a", "abc123", WORKING, clock()), author="stranger")
        assert parse_claims(thread.comments, ACCOUNT) == []

    def test_a_release_by_someone_else_frees_nothing(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add(claim_body("node-a", "abc123", WORKING, clock()))
        thread.add(release_marker("abc123"), author="stranger")
        assert len(parse_claims(thread.comments, ACCOUNT)) == 1

    def test_a_release_ends_only_the_claim_it_names(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add(claim_body("node-a", "aaa", WORKING, clock()))
        thread.add(claim_body("node-b", "bbb", WORKING, clock()))
        thread.add(release_marker("bbb"))
        assert [c.node for c in parse_claims(thread.comments, ACCOUNT)] == ["node-a"]

    def test_the_old_failure_retraction_ends_every_claim_before_it(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add("<!-- ctrlrelay:claimed -->")
        thread.add(f"did not finish {DISPATCH_FAILED_MARKER}")
        thread.add(claim_body("node-b", "bbb", WORKING, clock()))
        assert [c.node for c in parse_claims(thread.comments, ACCOUNT)] == ["node-b"]

    def test_a_node_name_cannot_break_out_of_the_marker(self) -> None:
        assert safe_node_name("my host --> x") == "my-host-----x"
        assert safe_node_name("   ") == "unknown"


class TestStaleRule:
    def test_a_working_claim_goes_stale_after_the_set_time_and_not_before(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add(claim_body("node-a", "aaa", WORKING, clock()))
        me = settings("node-b")
        clock.t += 1799
        assert other_owner(thread.comments, me, clock()).node == "node-a"
        clock.t += 1
        assert other_owner(thread.comments, me, clock()) is None

    def test_a_held_claim_never_goes_stale(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add(claim_body("node-a", "aaa", HELD, clock()))
        clock.t += 30 * 86400
        assert other_owner(thread.comments, settings("node-b"), clock()).node == "node-a"


class TestAcquire:
    @pytest.mark.asyncio
    async def test_a_free_issue_is_won_and_the_claim_names_the_machine(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        out = await acquire_claim(
            thread, REPO, 1, settings("node-a"), clock=clock, sleep=no_sleep,
        )
        assert out.won
        assert "Taken by ctrlrelay on **node-a**" in thread.comments[0]["body"]
        assert "node=node-a" in thread.comments[0]["body"]

    @pytest.mark.asyncio
    async def test_another_machines_live_claim_is_skipped_without_posting(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add(claim_body("node-a", "aaa", WORKING, clock()))
        out = await acquire_claim(
            thread, REPO, 1, settings("node-b"), clock=clock, sleep=no_sleep,
        )
        assert not out.won
        assert out.owner.node == "node-a"
        assert len(thread.comments) == 1

    @pytest.mark.asyncio
    async def test_both_claim_at_once_the_oldest_wins_and_the_loser_backs_off(
        self,
    ) -> None:
        clock = Clock()
        thread = Thread(clock)
        both_read = asyncio.Event()
        posted = asyncio.Event()
        reads = posts = 0
        real_list = thread.list_issue_comments
        real_post = thread.post_issue_comment

        async def list_comments(repo, n):
            nonlocal reads
            out = await real_list(repo, n)
            reads += 1
            if reads == 2:
                both_read.set()
            return out

        async def post(repo, n, body):
            nonlocal posts
            # Neither posts until both have read an empty thread.
            await both_read.wait()
            cid = await real_post(repo, n, body)
            posts += 1
            if posts == 2:
                posted.set()
            return cid

        thread.list_issue_comments = list_comments
        thread.post_issue_comment = post

        async def wait_for_both(_s: float) -> None:
            # Neither re-reads until both claims are up: the race window.
            await posted.wait()

        a, b = await asyncio.gather(
            acquire_claim(thread, REPO, 1, settings("node-a"), clock=clock, sleep=wait_for_both),
            acquire_claim(thread, REPO, 1, settings("node-b"), clock=clock, sleep=wait_for_both),
        )
        # Whichever posted first holds the lower comment id and wins.
        first = "node-a" if "node=node-a" in thread.comments[0]["body"] else "node-b"
        winner, loser = (a, b) if first == "node-a" else (b, a)
        assert winner.won and not loser.won
        assert loser.owner.node == first
        assert "Backing off" in thread.comments[-1]["body"]
        assert f"**{first}**" in thread.comments[-1]["body"]
        assert [c.node for c in live_claims(thread.comments, settings("node-c"), clock())] == [
            first
        ]

    @pytest.mark.asyncio
    async def test_a_stale_claim_from_a_stopped_machine_is_taken_over(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add(claim_body("node-a", "aaa", WORKING, clock()))
        clock.t += 1801
        out = await acquire_claim(
            thread, REPO, 1, settings("node-b"), clock=clock, sleep=no_sleep,
        )
        assert out.won
        assert "earlier claim by node-a was not refreshed" in thread.comments[-1]["body"]

    @pytest.mark.asyncio
    async def test_a_live_machine_that_refreshes_is_never_taken_over(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        a = await acquire_claim(
            thread, REPO, 1, settings("node-a"), clock=clock, sleep=no_sleep,
        )
        ticks = 0

        async def tick(_s: float) -> None:
            nonlocal ticks
            ticks += 1
            clock.t += 300
            if ticks > 12:  # an hour of refreshes, then stop the loop
                raise asyncio.CancelledError
            await asyncio.sleep(0)

        keeper = ClaimKeeper(thread, REPO, a.claim, settings("node-a"), clock=clock, sleep=tick)
        keeper.start()
        with pytest.raises(asyncio.CancelledError):
            await keeper._task
        # An hour in: well past the stale time, but refreshed every 5 minutes.
        out = await acquire_claim(
            thread, REPO, 1, settings("node-b"), clock=clock, sleep=no_sleep,
        )
        assert not out.won
        assert out.owner.node == "node-a"

    @pytest.mark.asyncio
    async def test_an_earlier_claim_by_this_machine_is_adopted_not_duplicated(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add(claim_body("node-a", "aaa", HELD, clock()))
        out = await acquire_claim(
            thread, REPO, 1, settings("node-a"), clock=clock, sleep=no_sleep,
        )
        assert out.won and out.claim.claim_id == "aaa"
        assert len(thread.comments) == 1
        assert "state=working" in thread.comments[0]["body"]

    @pytest.mark.asyncio
    async def test_a_claim_from_before_245_blocks_every_machine(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        thread.add("<!-- ctrlrelay:claimed -->")
        out = await acquire_claim(
            thread, REPO, 1, settings("node-a"), clock=clock, sleep=no_sleep,
        )
        assert not out.won and out.owner.node == "unknown"


class TestKeeper:
    @pytest.mark.asyncio
    async def test_hold_marks_the_claim_held_so_it_never_goes_stale(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        a = await acquire_claim(
            thread, REPO, 1, settings("node-a"), clock=clock, sleep=no_sleep,
        )
        keeper = ClaimKeeper(thread, REPO, a.claim, settings("node-a"), clock=clock)
        keeper.start()
        await keeper.hold()
        assert "state=held" in thread.comments[0]["body"]
        clock.t += 86400
        assert other_owner(thread.comments, settings("node-b"), clock()).node == "node-a"

    @pytest.mark.asyncio
    async def test_a_failed_refresh_does_not_raise(self) -> None:
        clock = Clock()
        thread = Thread(clock)
        a = await acquire_claim(
            thread, REPO, 1, settings("node-a"), clock=clock, sleep=no_sleep,
        )
        thread.comments.clear()  # the comment was deleted
        keeper = ClaimKeeper(thread, REPO, a.claim, settings("node-a"), clock=clock)
        await keeper.hold()  # logged, not raised
