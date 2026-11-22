from __future__ import annotations

import base64
import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import create_app

from .conftest import STREAM_CD, FakeQobuz, make_settings


class DualMockBackend:
    """Mock transport handling both Qobuz and Tidal API calls."""

    def __init__(self, fake_qobuz: FakeQobuz) -> None:
        self.qobuz = fake_qobuz
        self.tidal_fail = False
        self.tidal_dash = False
        self.get_track_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path

        if "tidal.com" in host:
            if self.tidal_fail:
                return httpx.Response(503, json={"error": "tidal_down"})
            if path.endswith("/sessions"):
                return httpx.Response(
                    200,
                    json={"sessionId": "sess-1", "userId": 999111, "countryCode": "US"},
                )
            if "/subscription" in path:
                return httpx.Response(
                    200,
                    json={"subscription": {"type": "HIFI_PLUS"}, "status": "ACTIVE"},
                )
            if path.endswith("/tracks/77889900"):
                self.get_track_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "id": 77889900,
                        "title": "One More Time",
                        "artist": {"name": "Daft Punk"},
                        "album": {
                            "title": "Discovery",
                            "cover": "11111111-2222-3333-4444-555555555555",
                        },
                        "duration": 320,
                        "isrc": "GBDUW0000053",
                        "allowStreaming": True,
                        "streamReady": True,
                        "audioQuality": "HI_RES_LOSSLESS",
                        "mediaMetadata": {"tags": ["LOSSLESS", "HIRES_LOSSLESS"]},
                        "audioModes": ["STEREO"],
                    },
                )
            if path.endswith("/search/tracks"):
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": 77889900,
                                "title": "One More Time",
                                "artist": {"name": "Daft Punk"},
                                "album": {
                                    "title": "Discovery",
                                    "cover": "11111111-2222-3333-4444-555555555555",
                                },
                                "duration": 320,
                                "isrc": "GBDUW0000053",
                                "allowStreaming": True,
                                "streamReady": True,
                                "audioQuality": "HI_RES_LOSSLESS",
                                "mediaMetadata": {"tags": ["LOSSLESS", "HIRES_LOSSLESS"]},
                                "audioModes": ["STEREO"],
                            }
                        ]
                    },
                )
            if "/playbackinfopostpaywall" in path:
                if self.tidal_dash:
                    mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period>
    <AdaptationSet mimeType="audio/flac">
      <Representation id="1" codecs="flac" audioSamplingRate="192000">
        <SegmentTemplate media="https://sp-pr-cf.audio.tidal.com/seg-$Number$.flac" initialization="https://sp-pr-cf.audio.tidal.com/init.flac"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""
                    b64 = base64.b64encode(mpd.encode("utf-8")).decode("ascii")
                    return httpx.Response(
                        200,
                        json={
                            "trackId": 77889900,
                            "audioQuality": "HI_RES_LOSSLESS",
                            "audioMode": "STEREO",
                            "bitDepth": 24,
                            "sampleRate": 192000,
                            "manifestMimeType": "application/dash+xml",
                            "manifest": b64,
                        },
                    )
                bts = {
                    "mimeType": "audio/flac",
                    "codecs": "flac",
                    "encryptionType": "NONE",
                    "urls": ["https://sp-pr-cf.audio.tidal.com/direct/77889900.flac"],
                }
                b64 = base64.b64encode(json.dumps(bts).encode("utf-8")).decode("ascii")
                return httpx.Response(
                    200,
                    json={
                        "trackId": 77889900,
                        "audioQuality": "HI_RES_LOSSLESS",
                        "audioMode": "STEREO",
                        "bitDepth": 24,
                        "sampleRate": 96000,
                        "manifestMimeType": "application/vnd.tidal.bts",
                        "manifest": b64,
                    },
                )
            return httpx.Response(404, json={"error": "not_found"})

        return self.qobuz(request)


@pytest.mark.asyncio
async def test_failover_qobuz_to_tidal_on_search_and_stream(tmp_path) -> None:
    admin_tok = "a" * 32
    settings = make_settings(tmp_path, stats_token=admin_tok, primary_provider="qobuz")
    fake_q = FakeQobuz()
    dual = DualMockBackend(fake_q)
    app = create_app(settings, transport=httpx.MockTransport(dual))

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # 1. Add a Tidal account via the local admin endpoint
            add_resp = await client.post(
                "/internal/admin/tidal/add",
                headers={"X-Admin-Token": admin_tok},
                json={"access_token": "tidal-tok-123", "label": "Tidal Main"},
            )
            assert add_resp.status_code == 200
            assert add_resp.json()["ok"] is True

            # 2. Search works on Qobuz first (caches TrackMeta for 5966783)
            s1 = await client.get("/search", params={"q": "one more time", "quality": "max"})
            assert s1.status_code == 200
            assert s1.json()["tracks"][0]["id"] == "5966783"

            # 3. Now simulate Qobuz failing (e.g. token expired / sample restriction) during /stream
            fake_q.auth_error = True
            st = await client.get("/stream/5966783", params={"quality": "max"})
            assert st.status_code == 200
            body = st.json()
            assert body["url"] == "https://sp-pr-cf.audio.tidal.com/direct/77889900.flac"
            assert body["codec"] == "flac"
            assert body["bitDepth"] == 24

            # 4. Simulate Qobuz search completely down -> automatically fails over to Tidal search
            fake_q.geo_blocked = True
            s2 = await client.get("/search", params={"q": "daft punk discovery", "quality": "max"})
            assert s2.status_code == 200
            tracks2 = s2.json()["tracks"]
            assert len(tracks2) == 1
            assert tracks2[0]["id"] == "tidal:77889900"


@pytest.mark.asyncio
async def test_tidal_dash_manifest_serving(tmp_path) -> None:
    admin_tok = "b" * 32
    settings = make_settings(tmp_path, stats_token=admin_tok)
    fake_q = FakeQobuz()
    dual = DualMockBackend(fake_q)
    dual.tidal_dash = True
    app = create_app(settings, transport=httpx.MockTransport(dual))

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            await client.post(
                "/internal/admin/tidal/add",
                headers={"X-Admin-Token": admin_tok},
                json={"access_token": "tidal-tok-123"},
            )
            st = await client.get("/stream/tidal:77889900", params={"quality": "max"})
            assert st.status_code == 200
            body = st.json()
            assert body["manifest"] == "dash"
            assert body["url"].endswith(".mpd")
            assert body["sampleRate"] == 192000
            assert body["bitDepth"] == 24

            # Verify the MPD endpoint serves valid DASH XML without requiring BitChordAuth headers
            mpd_path = body["url"].replace("http://testserver", "")
            mpd_resp = await client.get(mpd_path)
            assert mpd_resp.status_code == 200
            assert "application/dash+xml" in mpd_resp.headers["content-type"]
            assert "<MPD" in mpd_resp.text


@pytest.mark.asyncio
async def test_public_monitor_page_and_cloudflare_admin_isolation(tmp_path) -> None:
    admin_tok = "c" * 32
    settings = make_settings(tmp_path, stats_token=admin_tok)
    app = create_app(settings, transport=httpx.MockTransport(FakeQobuz()))

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # Browser visiting / gets HTML monitor page
            html_root = await client.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
            assert html_root.status_code == 200
            assert "Confih Hi-Res Gateway" in html_root.text

            # API client visiting / gets JSON
            json_root = await client.get("/", headers={"Accept": "application/json"})
            assert json_root.status_code == 200
            assert json_root.json()["manifest"] == "/manifest.json"

            # Public stats endpoint returns live counters
            pub = await client.get("/api/public-stats")
            assert pub.status_code == 200
            pbody = pub.json()
            assert "users" in pbody
            assert "services" in pbody
            assert "qobuz" in pbody["services"]
            assert "tidal" in pbody["services"]

            # Admin endpoint is blocked (404) if request came through Cloudflare Tunnel
            cf_attempt = await client.get(
                "/internal/admin/summary",
                headers={"X-Admin-Token": admin_tok, "CF-Ray": "abc123-DEL"},
            )
            assert cf_attempt.status_code == 404

            # Admin endpoint works locally without Cloudflare headers
            local_admin = await client.get(
                "/internal/admin/summary",
                headers={"X-Admin-Token": admin_tok},
            )
            assert local_admin.status_code == 200
            assert local_admin.json()["primary_provider"] == "tidal"


@pytest.mark.asyncio
async def test_tidal_quality_ranking_and_precache(fake: FakeQobuz, tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        primary_provider="tidal",
        qobuz_user_auth_token="",  # Expired/missing Qobuz
    )
    backend = DualMockBackend(fake)
    app = create_app(settings, transport=httpx.MockTransport(backend))

    async with LifespanManager(app):
        pool = app.state.pool
        await pool.add_tidal_account(
            label="Tidal #1",
            access_token="tok-1",
            refresh_token="ref-1",
            expires_in=86400,
            user_id="user-1",
            country_code="US",
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # Search populates sequence and prefetches top track
            resp = await client.get("/search?q=Daft+Punk")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["tracks"]) >= 1

            # Verify public stats includes engine roles and failover reasons
            pub = await client.get("/api/public-stats")
            assert pub.status_code == 200
            stats = pub.json()
            assert stats["services"]["tidal"]["role"] == "Active Primary"
            assert "failover_reasons" in stats["traffic"]

            # Sequence memory test: record sequence track-1 -> track-2
            pool.record_track_sequence("tidal:77889900", "tidal:77889901")
            assert pool.get_next_track("tidal:77889900") == "tidal:77889901"

            # Stream track 1
            s_resp = await client.get("/stream/tidal:77889900")
            assert s_resp.status_code == 200


@pytest.mark.asyncio
async def test_direct_tidal_stream_remembers_track_meta(
    fake: FakeQobuz, tmp_path
) -> None:
    settings = make_settings(
        tmp_path,
        primary_provider="tidal",
        qobuz_user_auth_token="",
    )
    backend = DualMockBackend(fake)
    app = create_app(settings, transport=httpx.MockTransport(backend))

    async with LifespanManager(app):
        pool = app.state.pool
        await pool.add_tidal_account(
            label="Tidal #1",
            access_token="tok-1",
            refresh_token="ref-1",
            expires_in=86400,
            user_id="user-1",
            country_code="US",
        )
        await pool.add_tidal_account(
            label="Tidal #2",
            access_token="tok-2",
            refresh_token="ref-2",
            expires_in=86400,
            user_id="user-2",
            country_code="US",
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # Direct stream without prior /search
            s_resp = await client.get("/stream/tidal:77889900", params={"quality": "max"})
            assert s_resp.status_code == 200
            assert backend.get_track_calls == 1

            meta = pool.get_track_meta("tidal:77889900")
            assert meta is not None
            assert meta.title == "One More Time"
            assert meta.artist == "Daft Punk"
            assert meta.album == "Discovery"
            assert meta.isrc == "GBDUW0000053"
            assert meta.artwork

            plays_resp = await client.get("/api/plays")
            assert plays_resp.status_code == 200
            top = plays_resp.json()["top"]
            assert top[0]["title"] == "One More Time"
            assert top[0]["artwork"].startswith("/art/")


@pytest.mark.asyncio
async def test_a_cd_master_on_qobuz_loses_to_tidals_hi_res_copy(tmp_path) -> None:
    # `max` means "best these catalogues can do". Qobuz holding only 16-bit must
    # not settle the request while Tidal holds the 24-bit master.
    admin_tok = "a" * 32
    settings = make_settings(tmp_path, stats_token=admin_tok, primary_provider="qobuz")
    fake_q = FakeQobuz()
    for format_id in (27, 7, 6):
        fake_q.stream_data[format_id] = STREAM_CD
    dual = DualMockBackend(fake_q)
    app = create_app(settings, transport=httpx.MockTransport(dual))

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            await client.post(
                "/internal/admin/tidal/add",
                headers={"X-Admin-Token": admin_tok},
                json={"access_token": "tidal-tok-123", "label": "Tidal Main"},
            )
            await client.get("/search", params={"q": "one more time", "quality": "max"})

            response = await client.get("/stream/5966783", params={"quality": "max"})
            body = response.json()

    assert response.status_code == 200
    assert body["url"] == "https://sp-pr-cf.audio.tidal.com/direct/77889900.flac"
    assert body["bitDepth"] == 24
    assert body["sampleRate"] == 96000


@pytest.mark.asyncio
async def test_a_hi_res_master_on_qobuz_never_asks_tidal(tmp_path) -> None:
    admin_tok = "a" * 32
    settings = make_settings(tmp_path, stats_token=admin_tok, primary_provider="qobuz")
    fake_q = FakeQobuz()
    dual = DualMockBackend(fake_q)
    app = create_app(settings, transport=httpx.MockTransport(dual))

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            await client.post(
                "/internal/admin/tidal/add",
                headers={"X-Admin-Token": admin_tok},
                json={"access_token": "tidal-tok-123", "label": "Tidal Main"},
            )
            await client.get("/search", params={"q": "one more time", "quality": "max"})
            response = await client.get("/stream/5966783", params={"quality": "max"})

    assert response.status_code == 200
    assert response.json()["bitDepth"] == 24
    assert dual.get_track_calls == 0


@pytest.mark.asyncio
async def test_a_lossless_request_is_answered_without_the_second_platform(tmp_path) -> None:
    # Only the hi-res rung is worth a second provider call: CD quality is CD
    # quality on both services, so the request must not pay for the check.
    admin_tok = "a" * 32
    settings = make_settings(tmp_path, stats_token=admin_tok, primary_provider="qobuz")
    fake_q = FakeQobuz()
    for format_id in (27, 7, 6):
        fake_q.stream_data[format_id] = STREAM_CD
    dual = DualMockBackend(fake_q)
    app = create_app(settings, transport=httpx.MockTransport(dual))

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            await client.post(
                "/internal/admin/tidal/add",
                headers={"X-Admin-Token": admin_tok},
                json={"access_token": "tidal-tok-123", "label": "Tidal Main"},
            )
            await client.get("/search", params={"q": "one more time", "quality": "lossless"})
            response = await client.get("/stream/5966783", params={"quality": "lossless"})

    assert response.status_code == 200
    assert response.json()["bitDepth"] == 16
    assert dual.get_track_calls == 0


def test_rejected_accounts_queue_behind_healthy_ones() -> None:
    # A pool of expired tokens must not fill the probe budget: the working
    # account has to be asked first, or the play lands on the other platform.
    from app.pool import ProviderPool

    class _Account:
        def __init__(self, healthy: bool) -> None:
            self.is_healthy = healthy

    clients = [
        ("dead-1", "Dead One", _Account(False)),
        ("dead-2", "Dead Two", _Account(False)),
        ("live", "Live", _Account(True)),
    ]

    ordered = ProviderPool._order_by_capability(clients, {})

    assert [account_id for account_id, _, _ in ordered] == ["live", "dead-1", "dead-2"]
