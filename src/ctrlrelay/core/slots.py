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

    async def acquire(self) -> None:
        if self._sem is not None:
            await self._sem.acquire()
        self.busy += 1

    def release(self) -> None:
        self.busy -= 1
        if self._sem is not None:
            self._sem.release()

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        await self.acquire()
        try:
            yield
        finally:
            self.release()
