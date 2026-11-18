from __future__ import annotations

import asyncio

import pytest

from app.cache import SingleFlightCache, TTLCache


@pytest.mark.asyncio
async def test_ttl_cache_expires() -> None:
    cache: TTLCache[str, str] = TTLCache(max_entries=4, ttl_seconds=0.05)
    await cache.set("k", "v")
    assert await cache.get("k") == "v"
    await asyncio.sleep(0.08)
    assert await cache.get("k") is None


@pytest.mark.asyncio
async def test_single_flight_collapses_concurrent_misses() -> None:
    cache: SingleFlightCache[str, str] = SingleFlightCache(max_entries=8, ttl_seconds=60)
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return "value"

    results = await asyncio.gather(
        *(cache.get_or_create("same", factory) for _ in range(6))
    )
    assert calls == 1
    assert {value for value, _ in results} == {"value"}
    assert sum(1 for _, cached in results if not cached) == 1


@pytest.mark.asyncio
async def test_errors_are_not_cached() -> None:
    cache: SingleFlightCache[str, str] = SingleFlightCache(max_entries=8, ttl_seconds=60)
    attempts = 0

    async def failing() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("nope")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cache.get_or_create("k", failing)


@pytest.mark.asyncio
async def test_ttl_cache_contains() -> None:
    cache: TTLCache[str, str] = TTLCache(max_entries=4, ttl_seconds=0.05)
    await cache.set("k", "v")
    assert "k" in cache
    assert "missing" not in cache
    await asyncio.sleep(0.08)
    assert "k" not in cache
