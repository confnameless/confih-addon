"""Rate limiting: a token bucket for Qobuz and a window for clients."""

from __future__ import annotations

import asyncio
import time
from collections import deque


class SlidingWindowLimiter:
    """At most ``limit`` requests per key in any rolling 60 seconds.

    Personal addons have one listener, but the URL can leak and Qobuz quotas
    are shared. This is deliberately in-memory: a restart is allowed to forget.
    """

    def __init__(self, limit_per_minute: int):
        self._limit = max(0, limit_per_minute)
        self._events: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def hit(self, key: str) -> tuple[bool, int]:
        if self._limit <= 0:
            return True, 0
        now = time.monotonic()
        window = 60.0
        async with self._lock:
            events = self._events.get(key)
            if events is None:
                events = deque()
                self._events[key] = events
            while events and now - events[0] >= window:
                events.popleft()
            if len(events) >= self._limit:
                retry_after = max(1, int(window - (now - events[0])))
                return False, retry_after
            events.append(now)
            if len(self._events) > 10_000:
                self._prune(now, window)
            return True, 0

    def _prune(self, now: float, window: float) -> None:
        stale = [
            key
            for key, events in self._events.items()
            if not events or now - events[-1] >= window
        ]
        for key in stale:
            self._events.pop(key, None)


class TokenBucket:
    def __init__(self, rate_per_second: float, burst: int):
        self._rate = max(0.0, rate_per_second)
        self._capacity = max(1, burst)
        self._tokens = float(self._capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        if self._rate <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                need = tokens - self._tokens
                await asyncio.sleep(need / self._rate)
