"""Tests for M1 Features 4 and 6: enriched /api/public-stats, /api/overview, and /badge.svg."""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import create_app
from app.stats import _sanitize_incident_message

from .conftest import FakeQobuz, make_settings


@pytest.mark.asyncio
async def test_public_stats_enrichment_and_zero_traffic_state(
    http: httpx.AsyncClient,
) -> None:
    response = await http.get("/api/public-stats")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()

    # Existing keys preserved
    for key in (
        "status",
        "status_reasons",
        "primary_provider",
        "primary_online",
        "failover_active",
        "last_failover_at",
        "addon",
        "users",
        "traffic",
        "quality_tiers",
        "observed_quality",
        "cache",
        "services",
        "timeline_24h",
    ):
        assert key in body, key

    # New M1 keys
    assert len(body["timeline_30d"]) == 30
    assert body["uptime_30d_percent"] is None
    assert body["total_requests_30d"] == 0
    assert all(
        day["status"] == "idle" and day["uptime_pct"] is None
        for day in body["timeline_30d"]
    )
    assert isinstance(body["incidents"], list)

    tel = body["telemetry"]
    for key in ("rss_mb", "cpu_cores", "load_1m", "load_5m", "load_15m", "arch", "python", "python_version"):
        assert key in tel, key
    assert tel["python"] == tel["python_version"]

    art = body["artwork_cache"]
    assert set(art.keys()) == {"files", "bytes", "megabytes", "max_files"}

    caps = body["capabilities"]
    assert caps["public_plays"] is True
    assert caps["addon_auth_enabled"] is False
    assert caps["path_secret"] is False
    assert caps["primary_provider"] == "tidal"
    assert [opt["value"] for opt in caps["quality_options"]] == [
        "max",
        "hires",
        "lossless",
        "high",
        "low",
    ]


@pytest.mark.asyncio
async def test_public_stats_incidents_are_sanitized_coalesced_and_bounded(
    http: httpx.AsyncClient, app
) -> None:
    stats = app.state.stats
    stats.begin("search", "10.0.0.1", addon=True)
    stats.end(200, elapsed_ms=12.0, addon=True)
    stats.begin("stream", "10.0.0.1", addon=True)
    stats.end(503, elapsed_ms=40.0, addon=True)

    # Coalesce identical failover events with sensitive URL/IP/token
    stats.record_failover(
        "Qobuz error/rate-limit https://secret.example/1.2.3.4?token=abc"
    )
    stats.record_failover(
        "Qobuz error/rate-limit https://secret.example/1.2.3.4?token=abc"
    )
    stats.record_failover("Tidal node 192.168.10.42 failed password=xyzzy")

    body = (await http.get("/api/public-stats")).json()
    assert body["total_requests_30d"] == 2
    assert body["uptime_30d_percent"] == 50.0
    assert len(body["incidents"]) <= 25

    failover_msgs = [ev for ev in body["incidents"] if ev["kind"] == "failover"]
    assert len(failover_msgs) == 2
    for ev in failover_msgs:
        msg = ev["message"]
        assert "https://" not in msg
        assert "1.2.3.4" not in msg
        assert "192.168.10.42" not in msg
        assert "abc" not in msg
        assert "xyzzy" not in msg
        assert ev["started_at"] <= ev["ended_at"]
    qobuz_ev = next(ev for ev in failover_msgs if ev["provider"] == "qobuz")
    assert qobuz_ev["count"] == 2
    tidal_ev = next(ev for ev in failover_msgs if ev["provider"] == "tidal")
    assert tidal_ev["count"] == 1


@pytest.mark.asyncio
async def test_overview_combines_bootstrap_payloads_and_respects_public_plays(
    tmp_path,
) -> None:
    # 1. Default public_plays=True
    settings_on = make_settings(tmp_path / "on", donate_demo=True)
    app_on = create_app(settings_on, transport=httpx.MockTransport(FakeQobuz()))
    async with LifespanManager(app_on):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_on),
            base_url="http://127.0.0.1:8899",
        ) as client:
            resp = await client.get("/api/overview")
            assert resp.status_code == 200
            assert resp.headers["cache-control"] == "no-store"
            data = resp.json()
            assert set(data.keys()) == {"stats", "support", "plays"}
            assert data["stats"]["addon"]["display_url"] == "http://127.0.0.1:8899"
            assert data["support"]["demo"] is True
            assert data["plays"] is not None
            assert data["plays"]["url"] == "http://127.0.0.1:8899"

    # 2. public_plays=False -> still 200, plays is null
    settings_off = make_settings(tmp_path / "off", public_plays=False)
    app_off = create_app(settings_off, transport=httpx.MockTransport(FakeQobuz()))
    async with LifespanManager(app_off):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_off),
            base_url="http://testserver",
        ) as client:
            resp = await client.get("/api/overview")
            assert resp.status_code == 200
            data = resp.json()
            assert data["plays"] is None
            assert data["stats"]["capabilities"]["public_plays"] is False


@pytest.mark.asyncio
async def test_support_payload_groups_tokens_by_network(tmp_path) -> None:
    """One address per chain, every token it accepts listed beside it."""
    app = create_app(
        make_settings(
            tmp_path,
            donate_btc="bc1qdonatetestaddress0000000000000000000",
            donate_sol="SoLdonate111111111111111111111111111111111",
            donate_polygon="0xDonate0000000000000000000000000000000001",
        ),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            resp = await client.get("/api/support")
            assert resp.status_code == 200
            data = resp.json()
            assert data["demo"] is False
            assert [asset["id"] for asset in data["assets"]] == ["btc", "sol", "polygon"]

            sol = data["assets"][1]
            assert sol["name"] == "Solana"
            assert sol["address"] == "SoLdonate111111111111111111111111111111111"
            assert [token["symbol"] for token in sol["accepts"]] == [
                "SOL",
                "USDC",
                "USDT",
            ]
            # Every token points at an inline sprite symbol, never a remote image.
            assert all(token["logo"].startswith("coin-") for token in sol["accepts"])

            polygon = data["assets"][2]
            assert [token["symbol"] for token in polygon["accepts"]] == ["USDC", "USDT"]

            # The same USDC string is never listed twice under one address.
            assert len({asset["address"] for asset in data["assets"]}) == 3


@pytest.mark.asyncio
async def test_badge_svg_reflects_live_status_and_supports_head(
    tmp_path, fake: FakeQobuz
) -> None:
    # 1. Operational + active streamer detail
    settings_ok = make_settings(tmp_path / "ok", primary_provider="qobuz")
    app_ok = create_app(settings_ok, transport=httpx.MockTransport(fake))
    async with LifespanManager(app_ok):
        app_ok.state.stats.record_client("10.0.0.1", "stream")
        app_ok.state.stats.record_client("10.0.0.2", "stream")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_ok),
            base_url="http://testserver",
        ) as client:
            get_resp = await client.get("/badge.svg")
            assert get_resp.status_code == 200
            assert "image/svg+xml" in get_resp.headers["content-type"]
            assert get_resp.headers["cache-control"] == "no-cache, max-age=30"
            assert "<svg" in get_resp.text
            assert "CONFIH" in get_resp.text
            assert "OPERATIONAL" in get_resp.text
            assert "2 live" in get_resp.text

            head_resp = await client.head("/badge.svg")
            assert head_resp.status_code == 200
            assert "image/svg+xml" in head_resp.headers["content-type"]
            assert head_resp.headers["cache-control"] == "no-cache, max-age=30"

    # 2. Degraded when primary (tidal) offline and backup (qobuz) online
    settings_deg = make_settings(tmp_path / "deg", primary_provider="tidal")
    app_deg = create_app(settings_deg, transport=httpx.MockTransport(fake))
    async with LifespanManager(app_deg):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_deg),
            base_url="http://testserver",
        ) as client:
            resp = await client.get("/badge.svg")
            assert resp.status_code == 200
            assert "DEGRADED" in resp.text

    # 3. Outage when unconfigured
    settings_down = make_settings(tmp_path / "down", qobuz_user_auth_token="")
    app_down = create_app(settings_down, transport=httpx.MockTransport(fake))
    async with LifespanManager(app_down):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_down),
            base_url="http://testserver",
        ) as client:
            resp = await client.get("/badge.svg")
            assert resp.status_code == 200
            assert "OFFLINE" in resp.text


def test_incident_message_sanitizes_socks_underscored_keys_quotes_and_bearer() -> None:
    cases = [
        (
            "Proxy error socks5://proxyuser:proxypass123@10.20.30.40:1080 refused",
            ["socks5://", "proxyuser", "proxypass123", "10.20.30.40"],
        ),
        (
            "Tidal proxy socks5h://user:secretpass@127.0.0.1:9050 timeout",
            ["socks5h://", "secretpass", "127.0.0.1"],
        ),
        (
            "Qobuz auth user_auth_token=QOBUZ_SECRET app_secret=APP_SEC api_key=KEY_123",
            ["QOBUZ_SECRET", "APP_SEC", "KEY_123"],
        ),
        (
            "OAuth client_secret=CLIENT_SEC access_token=ACC_TOK refresh_token=REF_TOK",
            ["CLIENT_SEC", "ACC_TOK", "REF_TOK"],
        ),
        (
            'Auth failed password="my secret pass" secret=\'quoted_sec\' user=bob',
            ["my secret pass", "quoted_sec"],
        ),
        (
            "Unauthorized Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig456",
            ["Bearer", "eyJhbGciOiJIUzI1NiJ9", "payload", "sig456"],
        ),
        (
            "Upstream Bearer eyJhbGciOi.abc.def rejected",
            ["Bearer", "eyJhbGciOi"],
        ),
    ]
    for raw, forbidden in cases:
        sanitized = _sanitize_incident_message(raw)
        for item in forbidden:
            assert item not in sanitized, (raw, sanitized, item)

    assert (
        _sanitize_incident_message("Key note on primary provider timeout")
        == "Key note on primary provider timeout"
    )


@pytest.mark.asyncio
async def test_incident_coalescing_300s_boundary(app) -> None:
    stats = app.state.stats
    stats._record_incident("failover", "Qobuz rate limit", now_ts=10020)
    stats._record_incident("failover", "Qobuz rate limit", now_ts=10319)
    stats._record_incident("failover", "Qobuz rate limit", now_ts=10320)

    incidents = stats.recent_incidents(10)
    assert len(incidents) == 2
    assert incidents[0]["started_at"] == 10320
    assert incidents[0]["count"] == 1
    assert incidents[1]["started_at"] == 10020
    assert incidents[1]["ended_at"] == 10260
    assert incidents[1]["count"] == 2


def test_incident_sanitization_hyphenated_headers_json_basic_and_ipv6() -> None:
    cases = [
        (
            "Rejected X-Stats-Token: supersecret999 and X-Api-Key=my_secret_api_key_42",
            ["supersecret999", "my_secret_api_key_42"],
        ),
        (
            'Upstream body {"access_token": "tok_abc_123", "password": "pw_xyz"} failed',
            ["tok_abc_123", "pw_xyz"],
        ),
        (
            "Upstream Authorization: Basic dXNlcjpzZWNyZXQ= rejected",
            ["dXNlcjpzZWNyZXQ="],
        ),
        (
            "Peer 2001:db8:85a3::8a2e:370:7334 and [2001:db8::1]:443 and ::1 failed",
            ["2001:db8:85a3::8a2e:370:7334", "2001:db8::1", "::1"],
        ),
    ]
    for raw, forbidden in cases:
        sanitized = _sanitize_incident_message(raw)
        for item in forbidden:
            assert item not in sanitized, (raw, sanitized, item)

    assert _sanitize_incident_message("Qobuz: rate limit at 12:30") == "Qobuz: rate limit at 12:30"


@pytest.mark.asyncio
async def test_record_incident_ignores_negative_clock_skew_coalescing(app) -> None:
    stats = app.state.stats
    stats._record_incident("failover", "Qobuz clock skew test", now_ts=20000)
    stats._record_incident("failover", "Qobuz clock skew test", now_ts=19700)

    incidents = stats.recent_incidents(10)
    assert len(incidents) == 2
    for ev in incidents:
        assert ev["ended_at"] >= ev["started_at"]


@pytest.mark.asyncio
async def test_single_flight_cache_waiter_cancellation_cleans_locks() -> None:
    import asyncio

    from app.cache import SingleFlightCache

    cache: SingleFlightCache[str, str] = SingleFlightCache(16, 60)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_factory() -> str:
        started.set()
        await release.wait()
        return "value"

    t1 = asyncio.create_task(cache.get_or_create("k", slow_factory))
    await started.wait()
    t2 = asyncio.create_task(cache.get_or_create("k", slow_factory))
    await asyncio.sleep(0)

    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2

    release.set()
    val, cached = await t1
    assert val == "value" and cached is False
    assert cache._locks == {}
    assert cache._lock_users == {}


def test_logging_redacts_oauth_query_params() -> None:
    import io
    import logging

    from app.logging_setup import configure_logging

    configure_logging("INFO", [])
    root_logger = logging.getLogger()
    handler = root_logger.handlers[0]
    buf = io.StringIO()
    orig_stream = handler.stream
    handler.stream = buf
    try:
        logging.getLogger("tidal").warning(
            "OAuth failed access_token=tidal_acc_999 refresh_token=tidal_ref_888 client_secret=cli_sec_777"
        )
    finally:
        handler.stream = orig_stream
    logged = buf.getvalue()
    for secret in ("tidal_acc_999", "tidal_ref_888", "cli_sec_777"):
        assert secret not in logged
    assert logged.count("***") >= 3
