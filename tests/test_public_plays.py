"""The public play feed: shape, privacy, and that covers stay first-party."""

from __future__ import annotations

import httpx
import pytest

from app.main import create_app

from .conftest import FakeQobuz, make_settings


def _record(app, track_id: str, artwork: str | None, client: str = "10.0.0.1") -> None:
    app.state.plays.record(
        track_id=track_id,
        title="One More Time",
        artist="Daft Punk",
        album="Discovery",
        artwork=artwork,
        tier="hires_24bit",
        client=client,
    )


@pytest.mark.asyncio
async def test_plays_endpoint_shape(http: httpx.AsyncClient, app) -> None:
    _record(app, "t1", None)

    response = await http.get("/api/plays")
    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"]["plays"] == 1
    assert payload["totals"]["tracks"] == 1
    assert {row["key"] for row in payload["totals"]["tiers"]} == {
        "dolby_atmos",
        "hires_24bit",
        "lossless_16bit",
        "high_320",
    }
    assert payload["top"][0]["title"] == "One More Time"
    assert len(payload["timeline"]) == 24
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_covers_are_served_from_this_host_not_a_provider(
    http: httpx.AsyncClient, app
) -> None:
    """A cover URL must never reach the browser: the CSP is img-src 'self'."""
    _record(app, "t1", "https://static.qobuz.com/images/covers/ab/cd/ef/600x600.jpg")

    payload = (await http.get("/api/plays")).json()
    for row in payload["top"] + payload["recent"]:
        artwork = row.get("artwork")
        assert artwork is None or artwork.startswith("/art/"), artwork
        assert "qobuz.com" not in (artwork or "")
        assert "tidal.com" not in (artwork or "")


@pytest.mark.asyncio
async def test_play_page_exposes_the_hooks_the_script_binds(
    http: httpx.AsyncClient,
) -> None:
    response = await http.get("/plays")
    assert response.status_code == 200
    body = response.text
    for element_id in (
        "plays-status",
        "plays-stats",
        "plays-chart",
        "plays-axis",
        "plays-readout",
        "plays-top",
        "plays-recent",
        "plays-mix",
    ):
        assert f'id="{element_id}"' in body, element_id
    # The quality mix is the tier filter, so there is no second set of chips.
    assert 'id="plays-legend"' not in body
    assert "/assets/plays.js" in body
    assert 'href="/plays"' in body  # reachable from the nav


@pytest.mark.asyncio
async def test_unknown_artwork_is_a_clean_404(http: httpx.AsyncClient) -> None:
    response = await http.get("/art/doesnotexist0000000000")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_artwork_path_cannot_escape_the_cache(tmp_path) -> None:
    settings = make_settings(tmp_path)
    application = create_app(settings, transport=httpx.MockTransport(FakeQobuz()))
    from asgi_lifespan import LifespanManager

    async with LifespanManager(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            for attempt in ("../../etc/passwd", "..%2Fetc%2Fpasswd", "x" * 200):
                response = await client.get(f"/art/{attempt}")
                assert response.status_code in (404, 400)


@pytest.mark.asyncio
async def test_feed_can_be_turned_off(tmp_path) -> None:
    settings = make_settings(tmp_path, public_plays=False)
    application = create_app(settings, transport=httpx.MockTransport(FakeQobuz()))
    from asgi_lifespan import LifespanManager

    async with LifespanManager(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            assert (await client.get("/api/plays")).status_code == 404
            assert (await client.get("/plays")).status_code == 404
            assert (await client.get("/api/plays?q=daft")).status_code == 404


@pytest.mark.asyncio
async def test_plays_persist_across_app_restart(tmp_path) -> None:
    from asgi_lifespan import LifespanManager

    settings = make_settings(tmp_path)
    app1 = create_app(settings, transport=httpx.MockTransport(FakeQobuz()))

    async with LifespanManager(app1):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app1),
            base_url="http://testserver",
        ) as client:
            search_resp = await client.get("/search", params={"q": "one more time"})
            assert search_resp.status_code == 200
            stream_resp = await client.get("/stream/5966783", params={"quality": "max"})
            assert stream_resp.status_code == 200

    assert (tmp_path / "plays.json").is_file()

    app2 = create_app(settings, transport=httpx.MockTransport(FakeQobuz()))
    async with LifespanManager(app2):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2),
            base_url="http://testserver",
        ) as client:
            plays_resp = await client.get("/api/plays")
            assert plays_resp.status_code == 200
            payload = plays_resp.json()
            assert payload["totals"]["plays"] == 1
            assert payload["totals"]["tracks"] == 1
            assert payload["top"][0]["id"] == "5966783"
            assert payload["top"][0]["title"] == "One More Time"
            assert payload["recent"][0]["id"] == "5966783"


@pytest.mark.asyncio
async def test_plays_endpoint_search_tier_and_limit_filtering(
    http: httpx.AsyncClient, app
) -> None:
    app.state.plays.record(
        track_id="t1",
        title="One More Time",
        artist="Daft Punk",
        album="Discovery",
        artwork="https://static.qobuz.com/images/covers/ab/cd/ef/600x600.jpg",
        tier="hires_24bit",
        client="10.0.0.1",
    )
    app.state.plays.record(
        track_id="t2",
        title="Aerodynamic",
        artist="Daft Punk",
        album="Discovery",
        artwork="https://resources.tidal.com/images/11111111/640x640.jpg",
        tier="dolby_atmos",
        client="10.0.0.2",
    )
    app.state.plays.record(
        track_id="t3",
        title="Teardrop",
        artist="Massive Attack",
        album="Mezzanine",
        artwork=None,
        tier="lossless_16bit",
        client="10.0.0.3",
    )

    # The first page carries the page metadata the scroller needs
    base_resp = await http.get("/api/plays")
    assert base_resp.status_code == 200
    base = base_resp.json()
    assert set(base.keys()) == {
        "addon",
        "url",
        "totals",
        "top",
        "top_total",
        "has_more",
        "next_offset",
        "page_size",
        "recent",
        "timeline",
        "query",
        "tier",
        "limit",
        "search_results",
    }
    assert base["top_total"] == 3
    assert base["has_more"] is False
    assert base["next_offset"] == 3

    # Query filter matches album & keeps totals global
    q_resp = await http.get("/api/plays", params={"q": "discovery"})
    assert q_resp.status_code == 200
    q_body = q_resp.json()
    assert q_body["totals"]["plays"] == 3
    assert len(q_body["timeline"]) == 24
    assert q_body["query"] == "discovery"
    assert {r["id"] for r in q_body["search_results"]} == {"t1", "t2"}
    assert {r["id"] for r in q_body["top"]} == {"t1", "t2"}
    assert {r["id"] for r in q_body["recent"]} == {"t1", "t2"}
    for row in q_body["search_results"] + q_body["recent"]:
        art = row.get("artwork")
        assert art is None or art.startswith("/art/")

    # Tier filter
    tier_resp = await http.get("/api/plays", params={"tier": "dolby_atmos"})
    assert tier_resp.status_code == 200
    t_body = tier_resp.json()
    assert t_body["tier"] == "dolby_atmos"
    assert [r["id"] for r in t_body["search_results"]] == ["t2"]
    assert [r["id"] for r in t_body["recent"]] == ["t2"]

    # Invalid tier yields empty results cleanly
    bad_tier = await http.get("/api/plays", params={"tier": "invalid"})
    assert bad_tier.status_code == 200
    assert bad_tier.json()["search_results"] == []

    # Limit clamping
    lim0 = await http.get("/api/plays", params={"limit": 0})
    assert lim0.json()["limit"] == 1
    assert len(lim0.json()["top"]) == 1
    lim999 = await http.get("/api/plays", params={"limit": 999})
    assert lim999.json()["limit"] == 50


@pytest.mark.asyncio
async def test_plays_paging_walks_the_whole_ranking(
    http: httpx.AsyncClient, app
) -> None:
    """A reader can reach every track, one small page at a time."""
    for index in range(30):
        app.state.plays.record(
            track_id=f"track_{index:02d}",
            title=f"Track {index:02d}",
            artist="Pager",
            album="Paging",
            artwork=None,
            tier="hires_24bit",
            client=f"10.0.0.{index}",
        )
    # Make the ranking unambiguous: higher index, more plays.
    for index in range(10):
        for _ in range(index + 1):
            app.state.plays.record(
                track_id=f"hot_{index:02d}",
                title=f"Hot {index:02d}",
                artist="Pager",
                album="Paging",
                artwork=None,
                tier="hires_24bit",
                client=f"10.1.0.{index}",
            )

    first = (await http.get("/api/plays", params={"limit": 10})).json()
    assert len(first["top"]) == 10
    assert first["top_total"] == 40
    assert first["has_more"] is True
    assert first["next_offset"] == 10
    # Ranked first: the track with the most plays.
    assert first["top"][0]["id"] == "hot_09"

    seen: list[str] = [row["id"] for row in first["top"]]
    offset = first["next_offset"]
    while True:
        page = (
            await http.get("/api/plays", params={"limit": 10, "offset": offset})
        ).json()
        # A follow-up page is rows only; the stats and chart are already drawn.
        assert set(page.keys()) == {
            "addon",
            "url",
            "top",
            "top_total",
            "has_more",
            "next_offset",
        }
        assert "recent" not in page, "a follow-up page is rows only"
        seen.extend(row["id"] for row in page["top"])
        if not page["has_more"]:
            break
        offset = page["next_offset"]

    assert len(seen) == 40, "every rank is reachable by scrolling"
    assert len(set(seen)) == 40, "no page repeats a row"
    assert seen[0] == "hot_09"

    # Past the end: empty page, not an error.
    beyond = (
        await http.get("/api/plays", params={"limit": 10, "offset": 40})
    ).json()
    assert beyond["top"] == []
    assert beyond["has_more"] is False


@pytest.mark.asyncio
async def test_empty_artwork_string_is_normalized_to_none(
    http: httpx.AsyncClient, app
) -> None:
    _record(app, "t_empty_art", "")

    payload = (await http.get("/api/plays")).json()
    assert payload["top"][0]["artwork"] is None
    assert payload["recent"][0]["artwork"] is None

    filtered = (await http.get("/api/plays", params={"q": "One More"})).json()
    assert filtered["top"][0]["artwork"] is None
    assert filtered["recent"][0]["artwork"] is None
    assert filtered["search_results"][0]["artwork"] is None


@pytest.mark.asyncio
async def test_poisoned_plays_json_non_string_artwork_returns_200(tmp_path) -> None:
    import json

    from asgi_lifespan import LifespanManager

    (tmp_path / "plays.json").write_text(
        json.dumps(
            {
                "total": 2,
                "tracks": [
                    {
                        "id": "bad1",
                        "title": "Poisoned Int",
                        "artist": "Tester",
                        "album": "Edge",
                        "artwork": 12345,
                        "plays": 2,
                        "quality": {"hires_24bit": 2},
                        "listeners": 1,
                        "first_seen": 1000,
                        "last_seen": 2000,
                    }
                ],
                "recent": [
                    {
                        "id": "bad1",
                        "title": "Poisoned List",
                        "artist": "Tester",
                        "artwork": ["https://static.qobuz.com/cover.jpg"],
                        "tier": "hires_24bit",
                        "at": 1980,
                    }
                ],
                "hourly": [],
            }
        ),
        encoding="utf-8",
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
            assert plays_body["top"][0]["artwork"] is None
            assert plays_body["recent"][0]["artwork"] is None

            ov_resp = await client.get("/api/overview")
            assert ov_resp.status_code == 200
            assert ov_resp.json()["plays"]["top"][0]["artwork"] is None
