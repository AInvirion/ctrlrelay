"""How many agent runs one node may have going at once (#210).

One :class:`AgentSlots` per daemon, shared by every track that starts an
agent: the poll loop's new issues, the sweeper that resumes answered
questions, and the scheduled security sweep. ``agent.max_parallel`` sets
the limit. Unset, there is no shared limit and the poll loop takes its
issues one at a time, exactly as before the setting existed.

A slot is taken before the repo lock, never while holding it. A run that
waits for a slot while holding a repo lock could wait on a run that is
itself waiting to get that lock back (the dev pipeline lets go of the lock
during its CI wait and takes it again before a fix).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator


class AgentSlots:
    """A node-wide cap on agent runs. ``limit=None`` caps nothing."""

    def __init__(self, limit: int | None) -> None:
        if limit is not None and limit < 1:
            raise ValueError(f"max_parallel must be at least 1, got {limit}")
        self.limit = limit
        self._sem = asyncio.Semaphore(limit) if limit is not None else None
        self.busy = 0
        self.closed = False
        self._released = asyncio.Event()

    async def acquire(self) -> None:
        """Wait for a slot. Raises ``CancelledError`` once :meth:`close` was
        called, also for a caller already waiting: a daemon shutting down
        must not start new runs in the slots its cancelled runs free."""
        if self.closed:
            raise asyncio.CancelledError("agent slots closed")
        if self._sem is not None:
            await self._sem.acquire()
            if self.closed:
                self._sem.release()
                raise asyncio.CancelledError("agent slots closed")
        self.busy += 1

    def try_acquire(self) -> bool:
        """A slot if one is free now and nobody is queued for one, else
        False. Never waits, so the poll loop keeps polling while full."""
        if self.closed:
            return False
        if self._sem is not None:
            if self._sem.locked():
                return False
            # Not locked: the value is above zero and no waiter is queued,
            # so this cannot block; done by hand because acquire() is a
            # coroutine.
            self._sem._value -= 1
        self.busy += 1
        return True

    def release(self) -> None:
        self.busy -= 1
        if self._sem is not None:
            self._sem.release()
        self._released.set()

    async def wait_released(self, timeout: float) -> None:
        """Return when a slot is given back, or after ``timeout`` seconds.
        Call :meth:`clear_released` before looking for a free slot, so a
        release in between is not missed."""
        try:
            await asyncio.wait_for(self._released.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def clear_released(self) -> None:
        self._released.clear()

    def close(self) -> None:
        """No more slots: waiting and later :meth:`acquire` calls raise
        ``CancelledError``. Wakes everyone waiting so they see it."""
        self.closed = True
        if self._sem is not None:
            # Wake every waiter; each re-checks `closed`, gives the slot
            # back and raises.
            for _ in range(len(getattr(self._sem, "_waiters", None) or ())):
                self._sem.release()

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        await self.acquire()
        try:
            yield
        finally:
            self.release()
