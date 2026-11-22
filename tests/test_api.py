from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import create_app

from .conftest import FakeQobuz, make_settings


@pytest.mark.asyncio
async def test_manifest_contract(http: httpx.AsyncClient) -> None:
    response = await http.get("/manifest.json")
    assert response.status_code == 200
    body = response.json()

    assert body["id"] == "test.qobuz"
    assert body["resources"] == ["search", "stream"]
    assert body["allowDownloads"] == 1
    assert body["checkValidLossless"] == 0
    assert body["bitchordAuth"] is False
    quality = body["settings"][0]
    assert quality["key"] == "quality"
    assert quality["default"] == "max"
    assert [option["value"] for option in quality["options"]] == [
        "max",
        "lossless",
        "high",
        "low",
    ]


@pytest.mark.asyncio
async def test_search_maps_catalogue_rows(http: httpx.AsyncClient) -> None:
    response = await http.get(
        "/search", params={"q": "one more time", "quality": "max"}
    )
    assert response.status_code == 200
    tracks = response.json()["tracks"]
    assert len(tracks) == 1

    track = tracks[0]
    assert track["id"] == "5966783"
    assert track["title"] == "One More Time"
    assert track["artist"] == "Daft Punk"
    assert track["album"] == "Discovery"
    assert track["duration"] == 320
    assert track["format"] == "flac"
    assert track["audioQuality"] == "HI_RES_LOSSLESS"
    assert track["artworkURL"].startswith("https://")
    assert track["audioMode"] == "STEREO"
    assert "streamURL" not in track


@pytest.mark.asyncio
async def test_search_at_high_tier_is_honest(http: httpx.AsyncClient) -> None:
    response = await http.get(
        "/search", params={"q": "one more time", "quality": "high"}
    )
    assert response.status_code == 200
    track = response.json()["tracks"][0]
    assert track["format"] == "mp3"
    assert track["audioQuality"] == "MP3_320"


@pytest.mark.asyncio
async def test_search_at_lossless_tier_does_not_claim_hires(
    http: httpx.AsyncClient,
) -> None:
    response = await http.get(
        "/search", params={"q": "one more time", "quality": "lossless"}
    )
    track = response.json()["tracks"][0]
    assert track["format"] == "flac"
    assert track["audioQuality"] == "LOSSLESS"


@pytest.mark.asyncio
async def test_empty_query_does_not_call_qobuz(
    http: httpx.AsyncClient, fake: FakeQobuz
) -> None:
    response = await http.get("/search", params={"q": "   "})
    assert response.status_code == 200
    assert response.json() == {"tracks": []}
    assert fake.count("/track/search") == 0


@pytest.mark.asyncio
async def test_search_is_cached(http: httpx.AsyncClient, fake: FakeQobuz) -> None:
    first = await http.get("/search", params={"q": "one more time", "quality": "max"})
    second = await http.get("/search", params={"q": "one more time", "quality": "max"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert fake.count("/track/search") == 1


@pytest.mark.asyncio
async def test_stream_reports_actual_hires_metadata(
    http: httpx.AsyncClient,
) -> None:
    response = await http.get("/stream/5966783", params={"quality": "max"})
    assert response.status_code == 200
    body = response.json()

    assert body["url"].startswith("https://cdn.example/")
    assert body["format"] == "flac"
    assert body["codec"] == "flac"
    assert body["container"] == "flac"
    assert body["manifest"] == "none"
    assert body["encrypted"] is False
    assert body["sampleRate"] == 192000
    assert body["bitDepth"] == 24
    assert "Hi-Res" in body["quality"]


@pytest.mark.asyncio
async def test_stream_labels_a_cd_fallback_as_cd(
    http: httpx.AsyncClient, fake: FakeQobuz
) -> None:
    # Qobuz answered the 24/192 request with a 16/44.1 rendition.
    fake.stream_data[27] = {
        "url": "https://cdn.example/audio/cd.flac?range=0-100",
        "format_id": 6,
        "mime_type": "audio/flac",
        "bit_depth": 16,
        "sampling_rate": 44.1,
    }
    response = await http.get("/stream/5966783", params={"quality": "hires"})
    body = response.json()

    assert response.status_code == 200
    assert body["format"] == "flac"
    assert body["sampleRate"] == 44100
    assert body["bitDepth"] == 16
    assert "Lossless" in body["quality"]
    assert "Hi-Res" not in body["quality"]


@pytest.mark.asyncio
async def test_stream_falls_back_past_a_400_rendition(
    http: httpx.AsyncClient, fake: FakeQobuz
) -> None:
    fake.stream_errors[27] = (400, "No result matching given argument")
    response = await http.get("/stream/5966783", params={"quality": "max"})
    body = response.json()

    assert response.status_code == 200
    assert body["sampleRate"] == 96000
    assert body["bitDepth"] == 24


@pytest.mark.asyncio
async def test_stream_is_cached(http: httpx.AsyncClient, fake: FakeQobuz) -> None:
    first = await http.get("/stream/5966783", params={"quality": "max"})
    second = await http.get("/stream/5966783", params={"quality": "max"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert fake.count("/track/getFileUrl") == 1


@pytest.mark.asyncio
async def test_annotated_rendition_is_not_read_as_a_miss(
    http: httpx.AsyncClient, fake: FakeQobuz
) -> None:
    # One of Qobuz's own edge nodes annotates every rung with a caveat code
    # above. The renditions are still full-length, so the play must keep its
    # 24/192 master instead of falling through to whatever a failover serves.
    for format_id in (27, 7, 6, 5):
        fake.restriction_codes[format_id] = ["SampleRestrictedByRightHolders"]

    response = await http.get("/stream/5966783", params={"quality": "max"})
    body = response.json()

    assert response.status_code == 200
    assert body["sampleRate"] == 192000
    assert body["bitDepth"] == 24
    assert "Hi-Res" in body["quality"]


@pytest.mark.asyncio
async def test_annotated_master_beats_a_clean_lower_rung(
    http: httpx.AsyncClient, fake: FakeQobuz
) -> None:
    # 24/192 arrives annotated while 24/96 is clean. The better master wins:
    # trading bit depth for a tidy response is the bug this guards.
    fake.restriction_codes[27] = ["FormatRestrictedByFormatAvailability"]

    response = await http.get("/stream/5966783", params={"quality": "max"})
    body = response.json()

    assert response.status_code == 200
    assert body["sampleRate"] == 192000
    assert body["bitDepth"] == 24


@pytest.mark.asyncio
async def test_annotated_sample_is_still_refused(
    http: httpx.AsyncClient, fake: FakeQobuz
) -> None:
    # A caveat code must not unlock a 30-second preview: the sample signals
    # stay fatal however the response is annotated.
    fake.restriction_codes[27] = ["SampleRestrictedByRightHolders"]
    fake.stream_data[27] = {
        "url": "https://cdn.example/audio/preview.flac?range=20-30",
        "format_id": 27,
        "sample": True,
        "mime_type": "audio/flac",
        "bit_depth": 24,
    }

    response = await http.get("/stream/5966783", params={"quality": "max"})

    assert response.status_code == 503
    assert response.json()["code"] == "qobuz_auth"


@pytest.mark.asyncio
async def test_sample_is_never_returned_as_audio(http: httpx.AsyncClient, fake: FakeQobuz) -> None:
    fake.sample = True
    response = await http.get("/stream/5966783", params={"quality": "max"})
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "qobuz_auth"
    assert "url" not in body


@pytest.mark.asyncio
async def test_restriction_is_never_returned_as_audio(
    http: httpx.AsyncClient, fake: FakeQobuz
) -> None:
    fake.auth_error = True
    response = await http.get("/stream/5966783", params={"quality": "max"})
    assert response.status_code == 503
    assert response.json()["code"] == "qobuz_auth"


@pytest.mark.asyncio
async def test_geo_block_is_reported(http: httpx.AsyncClient, fake: FakeQobuz) -> None:
    fake.geo_blocked = True
    response = await http.get("/search", params={"q": "something obscure"})
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "qobuz_geo_blocked"
    assert "FR/DE/NL/UK/US" in body["message"]


@pytest.mark.asyncio
async def test_invalid_track_id_is_a_miss(
    http: httpx.AsyncClient, fake: FakeQobuz
) -> None:
    response = await http.get("/stream/not-a-qobuz-id", params={"quality": "max"})
    assert response.status_code == 404
    assert fake.count("/track/getFileUrl") == 0


@pytest.mark.asyncio
async def test_health_and_readiness(http: httpx.AsyncClient) -> None:
    health = await http.get("/healthz")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["qobuz"]["configured"] is True
    assert body["path_secret"] is False

    ready = await http.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True


@pytest.mark.asyncio
async def test_stats_is_hidden_without_a_token(tmp_path) -> None:
    settings = make_settings(tmp_path)
    application = create_app(settings, transport=httpx.MockTransport(FakeQobuz()))
    async with LifespanManager(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/stats")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_stats_counts_traffic_and_requires_the_token(tmp_path) -> None:
    token = "s" * 32
    settings = make_settings(tmp_path, stats_token=token)
    fake = FakeQobuz()
    application = create_app(settings, transport=httpx.MockTransport(fake))
    async with LifespanManager(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            await client.get(
                "/search", params={"q": "one more time", "quality": "max"}
            )
            await client.get(
                "/search", params={"q": "one more time", "quality": "max"}
            )
            await client.get("/stream/5966783", params={"quality": "max"})
            denied = await client.get("/stats")
            allowed = await client.get(
                "/stats", headers={"X-Stats-Token": token}
            )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["requests"] >= 4
    assert body["by_endpoint"]["search"] == 2
    assert body["by_endpoint"]["stream"] == 1
    assert body["by_endpoint"]["stats"] == 2
    assert body["cache"]["search"] == {"hits": 1, "misses": 1}
    assert body["cache"]["stream"] == {"hits": 0, "misses": 1}
    assert body["clients"] == 1


@pytest.mark.asyncio
async def test_rate_limit_returns_retry_after(tmp_path) -> None:
    settings = make_settings(tmp_path, rate_limit_per_minute=1)
    fake = FakeQobuz()
    application = create_app(settings, transport=httpx.MockTransport(fake))
    async with LifespanManager(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            first = await client.get("/search", params={"q": "one"})
            second = await client.get("/search", params={"q": "two"})
            manifest = await client.get("/manifest.json")

    assert first.status_code == 200
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1
    assert second.json()["code"] == "rate_limited"
    assert manifest.status_code == 200


@pytest.mark.asyncio
async def test_path_secret_moves_every_route(tmp_path) -> None:
    secret = "x" * 32
    settings = make_settings(tmp_path, addon_path_secret=secret)
    fake = FakeQobuz()
    application = create_app(settings, transport=httpx.MockTransport(fake))
    async with LifespanManager(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            prefixed = await client.get(f"/s/{secret}/manifest.json")
            bare = await client.get("/manifest.json")
            health = await client.get(f"/s/{secret}/healthz")

    assert prefixed.status_code == 200
    assert bare.status_code == 404
    assert health.status_code == 200


@pytest.mark.asyncio
async def test_missing_addon_auth_is_challenged(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        addon_auth_enabled=True,
        addon_hosts="music.example.com",
    )
    fake = FakeQobuz()
    application = create_app(settings, transport=httpx.MockTransport(fake))
    async with LifespanManager(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            manifest = await client.get("/manifest.json")
            search = await client.get("/search", params={"q": "test"})
            health = await client.get("/healthz")

    assert manifest.status_code == 200
    assert manifest.json()["bitchordAuth"] is True
    assert search.status_code == 401
    assert search.headers["WWW-Authenticate"].startswith("BitChord")
    assert search.json()["code"] == "missing"
    assert health.status_code == 200
