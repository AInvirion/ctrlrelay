"""The GitHub API budget this account has left, and what to do about it (#237).

Every call from the account, on every machine and every session, spends
one shared hourly budget per bucket (``core``, ``search``, ``graphql``).
When a bucket runs out, every later call fails until it resets, and the
poller would quietly miss new issues for that long.

So the daemon reads ``/rate_limit`` on a schedule (that call is free),
keeps the reading in a small JSON file beside the state database for the
status page, and acts on it:

* **low** (below ``low_at`` of the limit): non-urgent work slows down;
* **out** (nothing left): calls to that bucket are not made at all until
  its reset time, and fail at once as rate limited instead of going to
  GitHub and failing there;

with one chat line when a bucket gets low, one when it runs out, and one
when it is back.

A reading that could not be taken is ``None``, published as
``"unknown"``. It is never treated as a full budget or an empty one.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.github_budget")

BUCKETS = ("core", "search", "graphql")
OK, LOW, OUT, UNKNOWN = "ok", "low", "out", "unknown"
# Epoch seconds between 2001 and 2286; outside that a reset is not a time.
_MIN_TS, _MAX_TS = 1_000_000_000, 10_000_000_000


@dataclass(frozen=True)
class Bucket:
    remaining: int
    limit: int
    resets_at: int

    def state(self, low_at: float, now: float) -> str:
        if now >= self.resets_at:
            # The reset has passed: this reading says nothing about now.
            # Unknown, which blocks nothing and announces nothing.
            return UNKNOWN
        if self.remaining <= 0:
            return OUT
        if self.limit > 0 and self.remaining / self.limit < low_at:
            return LOW
        return OK


@dataclass(frozen=True)
class BudgetReading:
    buckets: dict[str, Bucket]
    read_at: int

    def to_json(self) -> dict[str, Any]:
        return {
            "read_at": self.read_at,
            **{
                name: {"remaining": b.remaining, "limit": b.limit, "resets_at": b.resets_at}
                for name, b in self.buckets.items()
            },
        }

    @classmethod
    def from_json(cls, data: Any) -> BudgetReading | None:
        if not isinstance(data, dict) or type(data.get("read_at")) is not int:
            return None
        buckets = {}
        for name in BUCKETS:
            b = _bucket(data.get(name), reset_key="resets_at")
            if b is not None:
                buckets[name] = b
        if not _MIN_TS <= data["read_at"] < _MAX_TS:
            return None
        return cls(buckets=buckets, read_at=data["read_at"]) if buckets else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bucket(raw: Any, *, reset_key: str) -> Bucket | None:
    if not isinstance(raw, dict):
        return None
    remaining, limit, reset = (
        _int(raw.get("remaining")), _int(raw.get("limit")), _int(raw.get(reset_key)),
    )
    if remaining is None or limit is None or reset is None:
        return None
    if not _MIN_TS <= reset < _MAX_TS or limit < 0:
        return None
    return Bucket(remaining=remaining, limit=limit, resets_at=reset)


def parse_rate_limit(text: str, *, now: float | None = None) -> BudgetReading | None:
    """``gh api rate_limit`` output, or ``None`` when it does not parse."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    resources = data.get("resources") if isinstance(data, dict) else None
    if not isinstance(resources, dict):
        return None
    buckets = {}
    for name in BUCKETS:
        b = _bucket(resources.get(name), reset_key="reset")
        if b is not None:
            buckets[name] = b
    if not buckets:
        return None
    return BudgetReading(buckets=buckets, read_at=int(time.time() if now is None else now))


def bucket_for(args: Sequence[str]) -> str:
    """Which bucket a ``gh`` command spends, as far as the command says.

    ``gh api``: the endpoint argument (skipping options and their values)
    is ``graphql``, a ``search/`` path, or REST (``core``). ``gh search``
    is ``search``. ``gh run`` and ``gh release`` are REST. The rest of the
    commands this relay uses (``issue``, ``pr``, ``repo``) go through
    GraphQL. A best effort: the exact endpoints of a ``gh`` command are
    not part of its interface.
    """
    if not args:
        return "core"
    if args[0] == "search":
        return "search"
    if args[0] == "api":
        path = _api_endpoint(args[1:]).lstrip("/")
        if path.startswith("graphql"):
            return "graphql"
        if path.startswith("search/"):
            return "search"
        return "core"
    if args[0] in ("run", "release"):
        return "core"
    return "graphql"


# `gh api` options that take a value as the next argument.
_API_OPTS_WITH_VALUE = frozenset({
    "-X", "--method", "-H", "--header", "-f", "--raw-field", "-F", "--field",
    "-q", "--jq", "-t", "--template", "--input", "--hostname", "-p",
    "--preview", "--cache",
})


def _api_endpoint(rest: Sequence[str]) -> str:
    skip = False
    for arg in rest:
        if skip:
            skip = False
            continue
        if arg in _API_OPTS_WITH_VALUE:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return ""


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def budget_json(
    reading: BudgetReading | None, *, low_at: float, now: float,
) -> dict[str, Any] | str:
    """The ``github_api`` value of ``/api/status``: numbers, or ``"unknown"``."""
    if reading is None:
        return "unknown"
    out: dict[str, Any] = {"read_at": _iso(reading.read_at)}
    for name in BUCKETS:
        b = reading.buckets.get(name)
        out[name] = "unknown" if b is None else {
            "remaining": b.remaining,
            "limit": b.limit,
            "resets_at": _iso(b.resets_at),
            "state": b.state(low_at, now),
        }
    return out


class BudgetStore:
    """The last reading, in a JSON file written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def beside(cls, state_db: Path) -> BudgetStore:
        return cls(Path(state_db).parent / "github_budget.json")

    def write(self, reading: BudgetReading) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".github-budget-")
            with os.fdopen(fd, "w") as f:
                json.dump(reading.to_json(), f)
            os.replace(tmp, self.path)
        except Exception as e:
            log_event(_logger, "github_budget.write_failed", error=str(e)[:200])

    def mark_unknown(self, now: float) -> None:
        """The latest read failed: the page says unknown, not the older
        numbers as if current."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".github-budget-")
            with os.fdopen(fd, "w") as f:
                json.dump({"unknown": True, "read_at": int(now)}, f)
            os.replace(tmp, self.path)
        except Exception as e:
            log_event(_logger, "github_budget.write_failed", error=str(e)[:200])

    def read(self) -> BudgetReading | None:
        try:
            return BudgetReading.from_json(json.loads(self.path.read_text()))
        except (OSError, ValueError, RecursionError):
            return None


class GitHubBudget:
    """This process's view of the budget, and the gate ``gh`` calls pass.

    ``refresh`` reads ``/rate_limit`` through ``fetch`` (which must not
    itself go through the gate), stores it, and sends one chat line per
    change of state. ``blocked_until`` is what the ``gh`` wrapper asks
    before each call.
    """

    def __init__(
        self,
        store: BudgetStore,
        *,
        low_at: float,
        fetch: Callable[[], Awaitable[str]],
        notify: Callable[[str], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.low_at = low_at
        self._fetch = fetch
        self.notify = notify
        self._clock = clock
        self.reading: BudgetReading | None = store.read()
        self._said: dict[str, str] = {}
        self._marking: asyncio.Task[BudgetReading | None] | None = None

    def blocked_until(self, bucket: str) -> int | None:
        """The reset time, while ``bucket`` is out; otherwise ``None``."""
        if self.reading is None:
            return None
        b = self.reading.buckets.get(bucket)
        if b is None or b.state(self.low_at, self._clock()) != OUT:
            return None
        return b.resets_at

    def is_low(self) -> bool:
        """Any bucket low or out: time to slow down non-urgent work."""
        if self.reading is None:
            return False
        now = self._clock()
        return any(
            b.state(self.low_at, now) in (LOW, OUT)
            for b in self.reading.buckets.values()
        )

    def mark_out(self, bucket: str) -> None:
        """GitHub answered a call with its primary rate limit before the
        next scheduled read saw it. Read the budget now, rather than guess:
        the read records it, the page shows it, and the chat line goes out."""
        if self._marking is not None and not self._marking.done():
            return
        try:
            self._marking = asyncio.get_running_loop().create_task(self.refresh())
        except RuntimeError:
            pass

    async def refresh(self) -> BudgetReading | None:
        try:
            text = await self._fetch()
            reading = parse_rate_limit(text, now=self._clock())
            if reading is None:
                raise ValueError("unparseable")
        except Exception as e:
            # Could not read: unknown, which blocks nothing. Keeping an
            # older "used up" reading would go on refusing calls on the
            # strength of something no longer known.
            log_event(_logger, "github_budget.read_failed", error=str(e)[:200])
            self.reading = None
            self.store.mark_unknown(self._clock())
            return None
        self.reading = reading
        self.store.write(reading)
        await self._announce(reading)
        return reading

    async def _announce(self, reading: BudgetReading) -> None:
        now = self._clock()
        for name, b in reading.buckets.items():
            state = b.state(self.low_at, now)
            if state == UNKNOWN:
                continue  # an expired reading says nothing to announce
            before = self._said.get(name, OK)
            if state == before:
                continue
            self._said[name] = state
            resets = _iso(b.resets_at)
            if state == OUT:
                text = (
                    f"⛔ GitHub {name} API budget used up ({b.limit} an hour, "
                    f"shared by every machine on the account). GitHub calls are "
                    f"paused until it resets at {resets}."
                )
            elif state == LOW and before == OUT:
                text = (
                    f"\u2705 GitHub {name} API budget is back, but low: "
                    f"{b.remaining} of {b.limit} left until {resets}."
                )
            elif state == LOW:
                text = (
                    f"⚠️ GitHub {name} API budget low: {b.remaining} of "
                    f"{b.limit} left until {resets}. Slowing down."
                )
            else:
                text = f"✅ GitHub {name} API budget is back ({b.remaining} of {b.limit})."
            log_event(_logger, "github_budget.state", bucket=name, state=state,
                      remaining=b.remaining, limit=b.limit, resets_at=resets)
            if self.notify is not None:
                try:
                    await self.notify(text)
                except Exception as e:
                    log_event(_logger, "github_budget.notify_failed", error=str(e)[:200])
