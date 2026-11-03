"""Concurrency helpers shared by adapters."""

from __future__ import annotations

import asyncio


class LoopLock:
    """An `asyncio.Lock` per event loop, so one adapter can serve successive `asyncio.run` calls.

    Adapters use it for single-flight caching: when many reads arrive together on a cold cache,
    the first one fetches and the rest wait and reuse its result (FAILURES.md, 2026-11-03).
    """

    def __init__(self) -> None:
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

    def __call__(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            # forget locks of finished loops, so a long run of asyncio.run calls cannot grow this
            self._locks = {lp: lk for lp, lk in self._locks.items() if not lp.is_closed()}
            lock = self._locks[loop] = asyncio.Lock()
        return lock
