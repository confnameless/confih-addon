"""Small async TTL/LRU cache with single-flight loading.

BitChord already caches addon answers (10 minutes for search, 5 for streams).
This cache is the server-side half: it keeps a popular album or a retry burst
from becoming one Qobuz API call per listener, and it holds the signed stream
URL for less than the URL's own lifetime.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    def __init__(self, max_entries: int, ttl_seconds: float):
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: K) -> V | None:
        if self._ttl <= 0:
            return None
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() >= expires_at:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    async def set(self, key: K, value: V) -> None:
        if self._ttl <= 0:
            return
        async with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: K) -> bool:
        if self._ttl <= 0:
            return False
        entry = self._data.get(key)
        if entry is None:
            return False
        expires_at, _ = entry
        return time.monotonic() < expires_at


class SingleFlightCache(Generic[K, V]):
    """A TTL cache plus per-key locks, so two identical misses make one call.

    Locks are reference-counted. A caller increments the count before waiting,
    and the count is decremented in ``finally`` after the lock is released.
    When the last user leaves, the lock is removed, so a long-lived process
    does not accumulate one lock per arbitrary search query.
    """

    def __init__(self, max_entries: int, ttl_seconds: float):
        self.cache: TTLCache[K, V] = TTLCache(max_entries, ttl_seconds)
        self._locks: dict[K, asyncio.Lock] = {}
        self._lock_users: dict[K, int] = {}

    async def get(self, key: K) -> V | None:
        return await self.cache.get(key)

    def __contains__(self, key: K) -> bool:
        return key in self.cache

    async def get_or_create(
        self, key: K, factory: Callable[[], Awaitable[V]]
    ) -> tuple[V, bool]:
        """Return ``(value, was_cached)``. Errors are never cached."""
        cached = await self.cache.get(key)
        if cached is not None:
            return cached, True

        lock = self._lock_for(key)
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            cached = await self.cache.get(key)
            if cached is not None:
                return cached, True
            value = await factory()
            await self.cache.set(key, value)
            return value, False
        finally:
            if acquired:
                lock.release()
            self._release_lock(key)

    def _lock_for(self, key: K) -> asyncio.Lock:
        # No await between the get/create and the refcount update: the event
        # loop cannot interleave another caller here.
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        return lock

    def _release_lock(self, key: K) -> None:
        users = self._lock_users.get(key, 1) - 1
        if users <= 0:
            self._lock_users.pop(key, None)
            self._locks.pop(key, None)
        else:
            self._lock_users[key] = users

    async def clear(self) -> None:
        await self.cache.clear()
