"""Artwork cache: it must never fetch outside the allowed public hosts."""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest

from app.artwork import ArtworkCache, key_for

PUBLIC = [("AF_INET", "SOCK_STREAM", 6, "", ("93.184.216.34", 0))]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for DNS so tests do not hit the network."""
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port, *a, **k: list(PUBLIC)
    )


@pytest.fixture
def cache(tmp_path: Path) -> ArtworkCache:
    return ArtworkCache(tmp_path / "artwork", allowed_hosts="")


def test_key_is_stable_and_short() -> None:
    url = "https://static.qobuz.com/images/covers/ab/cd/ef/600x600.jpg"
    assert key_for(url) == key_for(url)
    assert 0 < len(key_for(url)) <= 32
    assert key_for(url) != key_for(url + "?x=1")


@pytest.mark.parametrize(
    "url",
    [
        "http://static.qobuz.com/cover.jpg",  # not https
        "https://localhost/cover.jpg",
        "https://127.0.0.1/cover.jpg",
        "https://10.0.0.5/cover.jpg",
        "https://evil.example.com/cover.jpg",  # not a provider host
        "https://qobuz.com.evil.example.com/x.jpg",  # suffix spoof
        "https://user:pass@static.qobuz.com/x.jpg",
        "https://static.qobuz.com:8080/x.jpg",
        "",
        None,
    ],
)
def test_register_refuses_unsafe_or_unrelated_hosts(cache: ArtworkCache, url) -> None:
    assert cache.register(url) is None


def test_register_accepts_a_provider_host(cache: ArtworkCache) -> None:
    url = "https://static.qobuz.com/images/covers/ab/cd/ef/600x600.jpg"
    key = cache.register(url)
    assert key == key_for(url)


def test_register_is_idempotent(cache: ArtworkCache) -> None:
    url = "https://resources.tidal.com/images/abc/640x640.jpg"
    assert cache.register(url) == cache.register(url)


def test_unknown_key_yields_nothing(cache: ArtworkCache) -> None:
    assert cache.path_for("nope") is not None
    assert cache.path_for("../../etc/passwd") is None
    assert cache.path_for("") is None


def test_key_that_was_never_registered_is_not_fetchable(cache: ArtworkCache) -> None:
    import asyncio

    assert asyncio.run(cache.get(key_for("https://static.qobuz.com/x.jpg"))) is None


def test_serves_from_disk_without_refetching(cache: ArtworkCache, tmp_path: Path) -> None:
    import asyncio

    url = "https://static.qobuz.com/images/covers/ab/cd/ef/600x600.jpg"
    key = cache.register(url)
    assert key is not None

    import json

    (cache.root / f"{key}.img").write_bytes(b"\xff\xd8cached")
    (cache.root / f"{key}.json").write_text(
        json.dumps({"url": url, "at": 1, "type": "image/jpeg"}), "utf-8"
    )

    class Exploding:
        async def get(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("cache miss should not happen")

    cache._client = Exploding()
    body, content_type = asyncio.run(cache.get(key))
    assert body == b"\xff\xd8cached"
    assert content_type == "image/jpeg"


def test_fetches_once_on_a_miss_and_stores(cache: ArtworkCache) -> None:
    import asyncio

    url = "https://static.qobuz.com/images/covers/zz/yy/xx/600x600.jpg"
    key = cache.register(url)
    assert key is not None

    calls: list[str] = []

    class Stub:
        async def get(self, request_url, **kwargs):
            calls.append(request_url)
            return httpx.Response(
                200, content=b"\xff\xd8fetched", headers={"content-type": "image/jpeg"}
            )

    cache._client = Stub()

    body, content_type = asyncio.run(cache.get(key))
    assert body == b"\xff\xd8fetched"
    assert content_type == "image/jpeg"
    assert calls == [url]
    assert (cache.root / f"{key}.img").read_bytes() == b"\xff\xd8fetched"

    # Second call is a disk hit.
    asyncio.run(cache.get(key))
    assert calls == [url]


def test_refuses_non_image_upstream(cache: ArtworkCache) -> None:
    import asyncio

    key = cache.register("https://static.qobuz.com/images/covers/aa/bb/cc/600x600.jpg")
    assert key is not None

    class Html:
        async def get(self, *args, **kwargs):
            return httpx.Response(
                200, content=b"<html>", headers={"content-type": "text/html"}
            )

    cache._client = Html()
    assert asyncio.run(cache.get(key)) is None


def test_upstream_failure_is_not_cached_as_a_hit(cache: ArtworkCache) -> None:
    import asyncio

    key = cache.register("https://static.qobuz.com/images/covers/dd/ee/ff/600x600.jpg")
    assert key is not None

    class Broken:
        async def get(self, *args, **kwargs):
            raise httpx.ConnectError("nope")

    cache._client = Broken()
    assert asyncio.run(cache.get(key)) is None
    assert not (cache.root / f"{key}.img").exists()


@pytest.mark.asyncio
async def test_concurrent_misses_fetch_once_and_release_locks(cache: ArtworkCache) -> None:
    import asyncio

    url = "https://static.qobuz.com/images/covers/11/22/33/600x600.jpg"
    key = cache.register(url)
    assert key is not None

    calls = 0

    class SlowStub:
        async def get(self, request_url, **kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return httpx.Response(
                200, content=b"\xff\xd8fetched", headers={"content-type": "image/jpeg"}
            )

    cache._client = SlowStub()
    results = await asyncio.gather(*(cache.get(key) for _ in range(8)))
    assert calls == 1
    assert all(res == (b"\xff\xd8fetched", "image/jpeg") for res in results)
    assert cache._key_locks == {}
    assert cache._key_lock_users == {}


@pytest.mark.asyncio
async def test_concurrent_different_keys_do_not_serialize(cache: ArtworkCache) -> None:
    import asyncio

    key_slow = cache.register("https://static.qobuz.com/images/covers/slow/600x600.jpg")
    key_fast = cache.register("https://static.qobuz.com/images/covers/fast/600x600.jpg")
    assert key_slow is not None and key_fast is not None

    release_slow = asyncio.Event()
    slow_started = asyncio.Event()

    class DualStub:
        async def get(self, request_url, **kwargs):
            if "slow" in request_url:
                slow_started.set()
                await release_slow.wait()
                return httpx.Response(
                    200, content=b"\xff\xd8slow", headers={"content-type": "image/jpeg"}
                )
            return httpx.Response(
                200, content=b"\xff\xd8fast", headers={"content-type": "image/jpeg"}
            )

    cache._client = DualStub()
    task_slow = asyncio.create_task(cache.get(key_slow))
    await slow_started.wait()

    # Fast key completes immediately even while slow key is in flight
    fast_res = await cache.get(key_fast)
    assert fast_res == (b"\xff\xd8fast", "image/jpeg")
    assert not task_slow.done()

    release_slow.set()
    slow_res = await task_slow
    assert slow_res == (b"\xff\xd8slow", "image/jpeg")
    assert cache._key_locks == {}


@pytest.mark.asyncio
async def test_concurrent_upstream_failure_does_not_poison_cache(
    cache: ArtworkCache,
) -> None:
    import asyncio

    url = "https://static.qobuz.com/images/covers/retry/600x600.jpg"
    key = cache.register(url)
    assert key is not None

    attempt = 0

    class FlakyStub:
        async def get(self, request_url, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt <= 2:
                return httpx.Response(500, content=b"err")
            return httpx.Response(
                200, content=b"\xff\xd8ok", headers={"content-type": "image/jpeg"}
            )

    cache._client = FlakyStub()
    res1 = await asyncio.gather(*(cache.get(key) for _ in range(2)))
    assert res1 == [None, None]
    assert not (cache.root / f"{key}.img").exists()
    assert cache._key_locks == {}

    res2 = await cache.get(key)
    assert res2 == (b"\xff\xd8ok", "image/jpeg")


def test_prune_removes_oldest_overflow_entries(
    cache: ArtworkCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import time

    import app.artwork as artwork_mod

    monkeypatch.setattr(artwork_mod, "_MAX_FILES", 3)
    now = time.time()
    keys = []
    for idx in range(6):
        k = cache.register(f"https://static.qobuz.com/images/covers/{idx}/600x600.jpg")
        assert k is not None
        img = cache.root / f"{k}.img"
        meta = cache.root / f"{k}.json"
        img.write_bytes(b"\xff\xd8x")
        mtime = now - 60 + idx  # strictly increasing, all fresh
        os.utime(img, (mtime, mtime))
        os.utime(meta, (mtime, mtime))
        keys.append(k)

    cache._pruned_at = 0.0
    cache._prune()

    # Oldest 3 removed, newest 3 kept
    for k in keys[:3]:
        assert not (cache.root / f"{k}.img").exists()
        assert not (cache.root / f"{k}.json").exists()
    for k in keys[3:]:
        assert (cache.root / f"{k}.img").exists()
        assert (cache.root / f"{k}.json").exists()


def test_prune_removes_all_stale_entries_even_under_max_files(
    cache: ArtworkCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import time

    import app.artwork as artwork_mod

    monkeypatch.setattr(artwork_mod, "_MAX_FILES", 10)
    monkeypatch.setattr(artwork_mod, "_MAX_AGE_SECONDS", 60)
    now = time.time()

    stale_keys = []
    for idx in range(3):
        k = cache.register(f"https://static.qobuz.com/images/covers/stale/{idx}.jpg")
        assert k is not None
        img = cache.root / f"{k}.img"
        img.write_bytes(b"\xff\xd8s")
        os.utime(img, (now - 120 - idx, now - 120 - idx))
        stale_keys.append(k)

    fresh_keys = []
    for idx in range(2):
        k = cache.register(f"https://static.qobuz.com/images/covers/fresh/{idx}.jpg")
        assert k is not None
        img = cache.root / f"{k}.img"
        img.write_bytes(b"\xff\xd8f")
        os.utime(img, (now - idx, now - idx))
        fresh_keys.append(k)

    cache._pruned_at = 0.0
    cache._prune()

    for k in stale_keys:
        assert not (cache.root / f"{k}.img").exists()
        assert not (cache.root / f"{k}.json").exists()
    for k in fresh_keys:
        assert (cache.root / f"{k}.img").exists()
        assert (cache.root / f"{k}.json").exists()


@pytest.mark.parametrize(
    "ip",
    [
        "64:ff9b::100.64.0.1",
        "100.64.0.1",
        "64:ff9b::224.0.0.1",
        "224.0.0.1",
        "ff02::1",
        "ff05::1",
        "ff0e::1",
        "64:ff9b::0.0.0.0",
        "0.0.0.0",
        "::",
        "::ffff:127.0.0.1",
        "::ffff:100.64.0.1",
        "::ffff:169.254.169.254",
    ],
)
def test_host_is_public_rejects_cgnat_multicast_unspecified_and_mapped(
    monkeypatch: pytest.MonkeyPatch, ip: str
) -> None:
    from app.artwork import _host_is_public

    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    addr = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(family, socket.SOCK_STREAM, 6, "", addr)],
    )
    assert _host_is_public("resources.tidal.com", ("tidal.com",)) is False


def test_host_is_public_rejects_empty_or_unparseable_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.artwork import _host_is_public

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    assert _host_is_public("resources.tidal.com", ("tidal.com",)) is False

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 0))],
    )
    assert _host_is_public("resources.tidal.com", ("tidal.com",)) is False


def test_host_is_public_allows_public_nat64_and_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.artwork import _host_is_public

    for ip in ("64:ff9b::6356:aff3", "93.184.216.34", "2606:4700:4700::1111"):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        addr = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *a, _f=family, _a=addr, **k: [(_f, socket.SOCK_STREAM, 6, "", _a)],
        )
        assert _host_is_public("resources.tidal.com", ("tidal.com",)) is True


@pytest.mark.asyncio
async def test_fetch_blocks_unsafe_redirect_and_allows_safe_redirects(
    cache: ArtworkCache,
) -> None:
    url = "https://resources.tidal.com/images/cover.jpg"
    key = cache.register(url)
    assert key is not None

    seen_urls: list[str] = []

    def bad_redirect_handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        )

    cache._client = httpx.AsyncClient(
        transport=httpx.MockTransport(bad_redirect_handler),
        follow_redirects=False,
    )
    try:
        assert await cache.get(key) is None
        assert seen_urls == [url]
        assert not (cache.root / f"{key}.img").exists()
    finally:
        await cache._client.aclose()

    # Relative redirect onto the same allowed HTTPS host succeeds
    safe_url = "https://resources.tidal.com/images/rel.jpg"
    safe_key = cache.register(safe_url)
    assert safe_key is not None
    hops: list[str] = []

    def safe_redirect_handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        if len(hops) == 1:
            return httpx.Response(302, headers={"Location": "/images/final.jpg"})
        return httpx.Response(
            200,
            content=b"\xff\xd8redirected",
            headers={"content-type": "image/jpeg"},
        )

    cache._client = httpx.AsyncClient(
        transport=httpx.MockTransport(safe_redirect_handler),
        follow_redirects=False,
    )
    try:
        res = await cache.get(safe_key)
        assert res == (b"\xff\xd8redirected", "image/jpeg")
        assert hops == [safe_url, "https://resources.tidal.com/images/final.jpg"]
    finally:
        await cache._client.aclose()

    # Excessive redirect loop (>3 hops) is rejected
    loop_url = "https://resources.tidal.com/images/loop.jpg"
    loop_key = cache.register(loop_url)
    assert loop_key is not None

    def loop_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/images/loop.jpg"})

    cache._client = httpx.AsyncClient(
        transport=httpx.MockTransport(loop_handler),
        follow_redirects=False,
    )
    try:
        assert await cache.get(loop_key) is None
    finally:
        await cache._client.aclose()


def test_safe_url_strips_whitespace_and_rejects_control_chars(cache: ArtworkCache) -> None:
    from app.artwork import _safe_url

    allowed = ("qobuz.com",)
    assert (
        _safe_url("  https://static.qobuz.com/images/covers/600x600.jpg  ", allowed)
        == "https://static.qobuz.com/images/covers/600x600.jpg"
    )
    for bad in (
        "https://static.qobuz.com/cover.jpg\r\n",
        "https://static.qobuz.com/cover\n.jpg",
        "https://static.qobuz.com/cover\x00.jpg",
        "https://static.qobuz.com/cover\x1f.jpg",
        "https://static.qobuz.com/cover\x7f.jpg",
        "\thttps://static.qobuz.com/cover.jpg",
    ):
        assert _safe_url(bad, allowed) is None
        assert cache.register(bad) is None


@pytest.mark.asyncio
async def test_fetch_handles_httpx_invalid_url(cache: ArtworkCache) -> None:
    key = cache.register("https://static.qobuz.com/images/covers/bad.jpg")
    assert key is not None

    class InvalidUrlClient:
        async def get(self, *args, **kwargs):
            raise httpx.InvalidURL("malformed URL")

    cache._client = InvalidUrlClient()
    assert await cache.get(key) is None
    assert not (cache.root / f"{key}.img").exists()


def test_safe_url_rejects_malformed_ports_ipv6_c1_surrogates_and_empty_labels(
    cache: ArtworkCache,
) -> None:
    from app.artwork import _host_is_public, _safe_url

    allowed = ("qobuz.com",)
    assert (
        _safe_url("https://static.qobuz.com:443/images/covers/600x600.jpg", allowed)
        == "https://static.qobuz.com:443/images/covers/600x600.jpg"
    )
    assert (
        _safe_url("https://static.qobuz.com./images/covers/600x600.jpg", allowed)
        == "https://static.qobuz.com./images/covers/600x600.jpg"
    )

    for bad in (
        "https://static.qobuz.com:99999/cover.jpg",
        "https://static.qobuz.com:abc/cover.jpg",
        "https://static.qobuz.com:-1/cover.jpg",
        "https://static.qobuz.com:/cover.jpg",
        "https://static.qobuz.com:0443/cover.jpg",
        "https://static.qobuz.com:443:443/cover.jpg",
        "https://[bad]/cover.jpg",
        "https://[2001:db8::1/cover.jpg",
        "https://2001:db8::1]/cover.jpg",
        "https://.qobuz.com/cover.jpg",
        "https://static..qobuz.com/cover.jpg",
        "https://static.qobuz.com../cover.jpg",
        "https://:@static.qobuz.com/cover.jpg",
        "https://@static.qobuz.com/cover.jpg",
        "https://static.qobuz.com/cover\x80.jpg",
        "https://static.qobuz.com/cover\x85.jpg",
        "https://static.qobuz.com/cover\x9f.jpg",
        "https://static.qobuz.com/cover\ud800.jpg",
        "https://static.qobuz.com/cover\udfff.jpg",
    ):
        assert _safe_url(bad, allowed) is None, bad
        assert cache.register(bad) is None, bad

    for bad_host in (".qobuz.com", "static..qobuz.com", "static.qobuz.com..", ""):
        assert _host_is_public(bad_host, allowed) is False, bad_host


@pytest.mark.asyncio
async def test_routes_survive_malformed_artwork_urls_without_500(
    tmp_path: Path,
) -> None:
    import json

    from asgi_lifespan import LifespanManager

    from app.main import create_app
    from app.plays import PlayLedger

    from .conftest import FakeQobuz, make_settings

    bad_urls = [
        "https://static.qobuz.com:99999/cover.jpg",
        "https://static.qobuz.com:abc/cover.jpg",
        "https://static.qobuz.com:-1/cover.jpg",
        "https://static.qobuz.com:/cover.jpg",
        "https://[bad]/cover.jpg",
        "https://[2001:db8::1/cover.jpg",
        "https://.qobuz.com/cover.jpg",
        "https://static.qobuz.com/cover\x85.jpg",
    ]

    ledger = PlayLedger(persist_path=tmp_path / "plays.json")
    for idx, bad_url in enumerate(bad_urls):
        ledger.record(
            track_id=f"qobuz:{1000 + idx}",
            title=f"Track {idx}",
            artist="Artist",
            album="Album",
            artwork=bad_url,
            tier="hires_24bit",
            client="listener-1",
        )
    ledger.save(force=True)

    art_dir = tmp_path / "artwork"
    art_dir.mkdir(parents=True, exist_ok=True)
    bad_art_key = "abcdef1234567890"
    (art_dir / f"{bad_art_key}.json").write_text(
        json.dumps({"url": "https://static.qobuz.com:99999/cover.jpg", "at": 1}),
        "utf-8",
    )

    app = create_app(
        make_settings(tmp_path),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            plays_resp = await client.get("/api/plays")
            assert plays_resp.status_code == 200
            plays_body = plays_resp.json()
            assert len(plays_body["top"]) == len(bad_urls)
            assert all(item["artwork"] is None for item in plays_body["top"])
            assert all(item["artwork"] is None for item in plays_body["recent"])

            overview_resp = await client.get("/api/overview")
            assert overview_resp.status_code == 200
            overview_body = overview_resp.json()
            assert all(item["artwork"] is None for item in overview_body["plays"]["top"])

            art_resp = await client.get(f"/art/{bad_art_key}")
            assert art_resp.status_code == 404


def test_safe_url_and_host_is_public_reject_oversized_idna_and_format_chars(
    cache: ArtworkCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.artwork import _host_is_public, _safe_url

    allowed = ("qobuz.com",)
    for good in (
        "https://static.qobuz.com/cover.jpg",
        "https://static.qobuz.com:443/cover.jpg",
        "https://static.qobuz.com./cover.jpg",
    ):
        assert _safe_url(good, allowed) == good, good

    bad_hosts = [
        ("a" * 64) + ".qobuz.com",
        ("a" * 70) + ".qobuz.com",
        ".".join(["a" * 63] * 4) + ".qobuz.com",
        "xn--" + ("z" * 60) + ".qobuz.com",
        "xn--" + ("z" * 59) + ".qobuz.com",
        "\ufffd.qobuz.com",
        "static\u202e.qobuz.com",
        "static\u200b.qobuz.com",
        "static\u00ad.qobuz.com",
        "bad\ud800.qobuz.com",
        "sub\u3002foo.qobuz.com",
        "static\u3002qobuz.com",
    ]
    for bad_host in bad_hosts:
        assert _host_is_public(bad_host, allowed) is False, repr(bad_host)

    bad_urls = [f"https://{host}/cover.jpg" for host in bad_hosts] + [
        "https://[2001:db8::1]:0443/cover.jpg",
        "https://[::1]:0443/cover.jpg",
        "https://[2001:db8::1]:443:443/cover.jpg",
        "https://[2001:db8::1]:/cover.jpg",
    ]
    for bad_url in bad_urls:
        assert _safe_url(bad_url, allowed) is None, repr(bad_url)
        assert cache.register(bad_url) is None, repr(bad_url)

    def raising_getaddrinfo(*args, **kwargs):
        raise UnicodeError("simulated idna failure")

    monkeypatch.setattr(socket, "getaddrinfo", raising_getaddrinfo)
    assert _host_is_public("static.qobuz.com", allowed) is False


@pytest.mark.asyncio
async def test_routes_survive_idna_and_nondict_artwork_metadata_without_500(
    tmp_path: Path,
) -> None:
    import json

    from asgi_lifespan import LifespanManager

    from app.main import create_app
    from app.plays import PlayLedger

    from .conftest import FakeQobuz, make_settings

    bad_urls = [
        "https://" + ("a" * 64) + ".qobuz.com/cover.jpg",
        "https://" + ("a" * 70) + ".qobuz.com/cover.jpg",
        "https://xn--" + ("z" * 60) + ".qobuz.com/cover.jpg",
        "https://\ufffd.qobuz.com/cover.jpg",
        "https://static\u202e.qobuz.com/cover.jpg",
        "https://static\u200b.qobuz.com/cover.jpg",
        "https://static\u00ad.qobuz.com/cover.jpg",
        "https://bad\ud800.qobuz.com/cover.jpg",
    ]

    ledger = PlayLedger(persist_path=tmp_path / "plays.json")
    for idx, bad_url in enumerate(bad_urls):
        ledger.record(
            track_id=f"qobuz:{2000 + idx}",
            title=f"IDNA Track {idx}",
            artist="Artist",
            album="Album",
            artwork=bad_url,
            tier="hires_24bit",
            client="listener-1",
        )
    ledger.save(force=True)

    art_dir = tmp_path / "artwork"
    art_dir.mkdir(parents=True, exist_ok=True)
    bad_idna_key = "badidnakey123456"
    (art_dir / f"{bad_idna_key}.json").write_text(
        json.dumps({"url": "https://" + ("a" * 70) + ".qobuz.com/cover.jpg", "at": 1}),
        "utf-8",
    )
    nondict_keys = {
        "badlistkey123456": json.dumps(["not", "a", "dict"]),
        "badnumkey1234567": json.dumps(123),
        "badnullkey123456": json.dumps(None),
    }
    for key, payload in nondict_keys.items():
        (art_dir / f"{key}.json").write_text(payload, "utf-8")

    cached_img_nondict_key = "cachednondict123"
    (art_dir / f"{cached_img_nondict_key}.json").write_text(
        json.dumps(["not", "a", "dict"]), "utf-8"
    )
    (art_dir / f"{cached_img_nondict_key}.img").write_bytes(b"\xff\xd8cached")

    app = create_app(
        make_settings(tmp_path),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            plays_resp = await client.get("/api/plays")
            assert plays_resp.status_code == 200
            plays_body = plays_resp.json()
            assert len(plays_body["top"]) == len(bad_urls)
            assert all(item["artwork"] is None for item in plays_body["top"])

            overview_resp = await client.get("/api/overview")
            assert overview_resp.status_code == 200
            overview_body = overview_resp.json()
            assert all(item["artwork"] is None for item in overview_body["plays"]["top"])

            assert (await client.get(f"/art/{bad_idna_key}")).status_code == 404
            for key in nondict_keys:
                assert (await client.get(f"/art/{key}")).status_code == 404

            cached_resp = await client.get(f"/art/{cached_img_nondict_key}")
            assert cached_resp.status_code == 200
            assert cached_resp.headers["content-type"] == "image/jpeg"
            assert cached_resp.content == b"\xff\xd8cached"


@pytest.mark.asyncio
async def test_rejects_svg_and_non_raster_and_accepts_uppercase_raster_with_sandbox_csp(
    tmp_path: Path,
) -> None:
    from asgi_lifespan import LifespanManager

    from app.main import create_app

    from .conftest import FakeQobuz, make_settings

    svg_url = "https://static.qobuz.com/images/covers/xss.svg"
    upper_jpg_url = "https://static.qobuz.com/images/covers/upper.jpg"

    app = create_app(
        make_settings(tmp_path),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        cache_dir = tmp_path / "artwork"
        art = ArtworkCache(cache_dir, allowed_hosts="")
        svg_key = art.register(svg_url)
        jpg_key = art.register(upper_jpg_url)
        assert svg_key is not None and jpg_key is not None

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == svg_url:
                return httpx.Response(
                    200,
                    content=b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                    headers={"Content-Type": "image/svg+xml; charset=utf-8"},
                )
            return httpx.Response(
                200,
                content=b"\xff\xd8\xff\xe0UPPER",
                headers={"Content-Type": "Image/JPEG; charset=utf-8"},
            )

        art._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            assert await art.get(svg_key) is None
            assert not (cache_dir / f"{svg_key}.img").exists()

            res = await art.get(jpg_key)
            assert res == (b"\xff\xd8\xff\xe0UPPER", "image/jpeg")
        finally:
            await art._client.aclose()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            ok_resp = await client.get(f"/art/{jpg_key}")
            assert ok_resp.status_code == 200
            assert ok_resp.headers["content-type"] == "image/jpeg"
            assert ok_resp.headers["content-security-policy"] == "default-src 'none'; sandbox"
            assert ok_resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_cached_artwork_register_skips_sync_dns_and_survives_dns_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from asgi_lifespan import LifespanManager

    from app.main import create_app
    from app.plays import PlayLedger

    from .conftest import FakeQobuz, make_settings

    urls = [
        "https://static.qobuz.com/images/covers/cached_1.jpg",
        "https://resources.tidal.com/images/cached_2.jpg",
    ]
    ledger = PlayLedger(persist_path=tmp_path / "plays.json")
    art = ArtworkCache(tmp_path / "artwork", allowed_hosts="")
    keys = []
    for idx, url in enumerate(urls):
        key = art.register(url)
        assert key is not None
        (art.root / f"{key}.img").write_bytes(b"\xff\xd8cached")
        (art.root / f"{key}.json").write_text(
            json.dumps({"url": url, "at": 1, "type": "image/jpeg"}),
            "utf-8",
        )
        keys.append(key)
        ledger.record(
            track_id=f"t{idx}",
            title=f"Track {idx}",
            artist="Artist",
            album="Album",
            artwork=url,
            tier="hires_24bit",
            client="listener-1",
        )
    ledger.save(force=True)

    app = create_app(
        make_settings(tmp_path),
        transport=httpx.MockTransport(FakeQobuz()),
    )

    dns_calls = 0

    def failing_dns(*args, **kwargs):
        nonlocal dns_calls
        dns_calls += 1
        raise socket.gaierror("DNS temporarily unreachable")

    monkeypatch.setattr(socket, "getaddrinfo", failing_dns)

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            plays_resp = await client.get("/api/plays")
            assert plays_resp.status_code == 200
            plays_body = plays_resp.json()
            assert dns_calls == 0
            assert {row["artwork"] for row in plays_body["top"]} == {f"/art/{k}" for k in keys}

            ov_resp = await client.get("/api/overview")
            assert ov_resp.status_code == 200
            assert dns_calls == 0
            assert {row["artwork"] for row in ov_resp.json()["plays"]["top"]} == {
                f"/art/{k}" for k in keys
            }

            for key in keys:
                img_resp = await client.get(f"/art/{key}")
                assert img_resp.status_code == 200
                assert img_resp.content == b"\xff\xd8cached"


@pytest.mark.asyncio
async def test_oversized_streaming_artwork_aborts_without_caching(cache: ArtworkCache) -> None:
    import app.artwork as artwork_mod

    url = "https://static.qobuz.com/images/covers/huge.png"
    key = cache.register(url)
    assert key is not None

    chunks_sent = 0

    class HugeStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal chunks_sent
            for _ in range(16):
                chunks_sent += 1
                yield b"A" * (512 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "image/png"},
            stream=HugeStream(),
        )

    cache._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    try:
        assert await cache.get(key) is None
        assert not (cache.root / f"{key}.img").exists()
        assert chunks_sent < (artwork_mod._MAX_BYTES // (512 * 1024)) + 3
    finally:
        await cache._client.aclose()


def test_idna_custom_artwork_hosts_normalized(tmp_path: Path) -> None:
    from app.artwork import _safe_url

    cache = ArtworkCache(tmp_path / "art_idn", allowed_hosts="münchen.de")
    assert "xn--mnchen-3ya.de" in cache.allowed
    assert (
        _safe_url("https://cdn.münchen.de/cover.jpg", cache.allowed, resolve_dns=False)
        == "https://cdn.münchen.de/cover.jpg"
    )
    assert (
        _safe_url(
            "https://cdn.xn--mnchen-3ya.de/cover.jpg",
            cache.allowed,
            resolve_dns=False,
        )
        == "https://cdn.xn--mnchen-3ya.de/cover.jpg"
    )
