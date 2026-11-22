"""Contract behaviour added for BitChord 1.10 and the operational fixes.

Covers the immersive-audio hint, the title/artist ranking, stream expiry, the
unauthenticated-request ceiling and the all-time counter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import _atmos_mode, create_app
from app.models import AddonTrack
from app.pool import ProviderPool

from .conftest import FakeQobuz, make_settings

# ── `?atmos=` hint ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "auto"),
        ("auto", "auto"),
        ("AUTO", "auto"),
        ("1", "strict"),
        ("true", "strict"),
        ("0", "off"),
        ("false", "off"),
        ("off", "off"),
        ("never", "off"),
        ("stereo", "off"),
        ("something-else", "auto"),
    ],
)
def test_atmos_hint_normalisation(raw: str, expected: str) -> None:
    assert _atmos_mode(raw) == expected


async def test_stream_honours_the_atmos_parameter(
    http: httpx.AsyncClient,
) -> None:
    """`auto`/`0` play; `1` is strict, so a stereo-only track is a miss."""
    for value in ("auto", "0", "nonsense", ""):
        response = await http.get(
            "/stream/5966783", params={"quality": "max", "atmos": value}
        )
        assert response.status_code == 200, value
        assert response.json()["url"], value

    strict = await http.get(
        "/stream/5966783", params={"quality": "max", "atmos": "1"}
    )
    assert strict.status_code == 404
    assert strict.json()["code"] == "not_found"


async def test_stream_states_when_the_url_expires(
    http: httpx.AsyncClient,
) -> None:
    response = await http.get("/stream/5966783", params={"quality": "max"})
    expires = response.json().get("expiresAt")
    assert isinstance(expires, str) and expires.endswith("Z"), expires


# ── title / artist ranking ────────────────────────────────────────────────


def _row(track_id: str, title: str, artist: str) -> AddonTrack:
    return AddonTrack(id=track_id, title=title, artist=artist)


def test_rank_puts_the_exact_recording_first() -> None:
    rows = [
        _row("tidal:1", "Gehra Hua", "Arijit Singh"),
        _row("tidal:2", "Gehra Hua (Live)", "Shashwat Sachdev"),
        _row("tidal:3", "Gehra Hua", "Shashwat Sachdev"),
        _row("tidal:4", "Gehra Hua", "Someone Else"),
    ]
    ranked = ProviderPool._rank_for_target(
        rows, title="Gehra Hua", artist="Shashwat Sachdev"
    )
    assert ranked[0].id == "tidal:3"
    # Everything is still present: ranking never hides a row.
    assert {row.id for row in ranked} == {row.id for row in rows}


def test_rank_keeps_order_when_nothing_matches() -> None:
    rows = [_row("a", "Track A", "Artist A"), _row("b", "Track B", "Artist B")]
    assert ProviderPool._rank_for_target(
        rows, title="Unrelated", artist="Nobody"
    ) == rows


def test_rank_without_a_target_changes_nothing() -> None:
    rows = [_row("a", "Track A", "Artist A"), _row("b", "Track B", "Artist B")]
    assert ProviderPool._rank_for_target(rows) == rows


async def test_search_accepts_title_and_artist(http: httpx.AsyncClient) -> None:
    response = await http.get(
        "/search",
        params={
            "q": "one more time daft punk",
            "quality": "max",
            "title": "One More Time",
            "artist": "Daft Punk",
        },
    )
    assert response.status_code == 200
    tracks = response.json()["tracks"]
    assert tracks and tracks[0]["title"] == "One More Time"


# ── unauthenticated ceiling ───────────────────────────────────────────────


async def test_unauthenticated_floods_are_limited(tmp_path: Path) -> None:
    """The 401 path used to be unlimited: auth runs before the main limiter."""
    settings = make_settings(
        tmp_path,
        addon_auth_enabled=True,
        addon_hosts="addon.example.com",
        auth_failure_per_minute=3,
    )
    app = create_app(settings, transport=httpx.MockTransport(FakeQobuz()))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            statuses = []
            for _ in range(6):
                response = await client.get("/search", params={"q": "test"})
                statuses.append(response.status_code)
            last = await client.get("/search", params={"q": "test"})

    assert statuses[:3] == [401, 401, 401]
    assert statuses[3:] == [429, 429, 429]
    assert last.headers.get("retry-after")


async def test_unauthenticated_ceiling_leaves_other_routes_alone(
    tmp_path: Path,
) -> None:
    settings = make_settings(
        tmp_path,
        addon_auth_enabled=True,
        addon_hosts="addon.example.com",
        auth_failure_per_minute=1,
    )
    app = create_app(settings, transport=httpx.MockTransport(FakeQobuz()))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for _ in range(3):
                await client.get("/search", params={"q": "test"})
            # The manifest is not a credential-bearing endpoint.
            manifest = await client.get("/manifest.json")
            health = await client.get("/healthz")

    assert manifest.status_code == 200
    assert health.status_code == 200


# ── all-time counter ──────────────────────────────────────────────────────


def test_all_time_counter_survives_the_dedupe_window(monkeypatch: Any) -> None:
    from app import stats as stats_module

    monkeypatch.setattr(stats_module, "_MAX_CLIENTS", 2)
    counter = stats_module.TrafficStats(persist_path=None)
    for subject in ("a", "b", "c", "d"):
        counter.record_client(subject, "stream")

    # The set is capped for memory, the public number is not.
    assert len(counter._all_time_users) <= 2
    assert counter.snapshot()["users"]["total_all_time"] == 4


# ── pre-cache learns from playback, not from search order ────────────────


async def test_search_does_not_teach_a_sequence(
    app: Any, http: httpx.AsyncClient
) -> None:
    """Search-result adjacency used to pre-resolve a track nobody played."""
    pool = app.state.pool
    fake_rows = (await http.get("/search", params={"q": "one more time"})).json()
    assert fake_rows["tracks"], "fixture should return at least one row"
    for row in fake_rows["tracks"]:
        assert pool.get_next_track(row["id"]) is None


async def test_consecutive_plays_teach_the_sequence(
    app: Any, http: httpx.AsyncClient
) -> None:
    pool = app.state.pool
    first = await http.get("/stream/5966783", params={"quality": "max"})
    second = await http.get("/stream/555", params={"quality": "max"})
    assert first.status_code == 200 and second.status_code == 200
    # The second request is not real audio in the fake, but it is a real play
    # as far as the pool is concerned: A then B from one client.
    assert pool.get_next_track("5966783") == "555"


def test_precache_can_be_switched_off(http: Any) -> None:
    from app.config import Settings

    settings = Settings(_env_file=None, precache_enabled=False)
    assert settings.precache_enabled is False


# ── best rendition across the account pool ────────────────────────────────


class _StubQobuzAccount:
    """One Qobuz account that answers `resolve` at a fixed bit depth."""

    def __init__(self, depth: int, *, raises: bool = False) -> None:
        self.depth = depth
        self.raises = raises

    async def resolve(self, track_id: str, tier: str) -> Any:
        if self.raises:
            from app.qobuz import QobuzError

            raise QobuzError("account down")
        from app.qobuz import QobuzStream

        return QobuzStream(
            url=f"https://cdn.example/{self.depth}.flac",
            format_id=27 if self.depth >= 24 else 6,
            mime_type="audio/flac",
            bit_depth=self.depth,
            sampling_rate_khz=96.0 if self.depth >= 24 else 44.1,
            restrictions=(),
            requested_format_id=27,
        )


def _pool_with(tmp_path: Path, accounts: list[_StubQobuzAccount]) -> ProviderPool:
    from app.pool import ProviderPool
    from app.qobuz import QobuzClient
    from app.stats import TrafficStats

    settings = make_settings(tmp_path)
    pool = ProviderPool(
        settings, QobuzClient(settings, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))), TrafficStats(persist_path=None)
    )
    pool.active_qobuz_clients = lambda: [  # type: ignore[method-assign]
        (f"acct{index}", f"acct{index}", account) for index, account in enumerate(accounts)
    ]
    return pool


async def test_hi_res_is_preferred_over_the_first_account(tmp_path: Path) -> None:
    """A 16-bit answer must not hide a 24-bit account behind it."""
    pool = _pool_with(tmp_path, [_StubQobuzAccount(16), _StubQobuzAccount(24)])
    stream = await pool._resolve_qobuz_direct("1", "max")
    assert stream.bitDepth == 24
    assert "96" in str(stream.sampleRate)


async def test_cd_quality_is_still_returned_when_nothing_better_exists(
    tmp_path: Path,
) -> None:
    pool = _pool_with(tmp_path, [_StubQobuzAccount(16), _StubQobuzAccount(16)])
    stream = await pool._resolve_qobuz_direct("1", "max")
    assert stream.bitDepth == 16


async def test_a_dead_account_does_not_stop_the_pool(tmp_path: Path) -> None:
    pool = _pool_with(
        tmp_path, [_StubQobuzAccount(16, raises=True), _StubQobuzAccount(24)]
    )
    stream = await pool._resolve_qobuz_direct("1", "max")
    assert stream.bitDepth == 24


def test_target_bit_depth_per_tier() -> None:
    from app.pool import _target_bit_depth

    assert _target_bit_depth("max") == 24
    assert _target_bit_depth("hires") == 24
    assert _target_bit_depth("lossless") == 16
    assert _target_bit_depth("high") == 0


async def test_a_low_res_account_is_demoted_for_later_requests(tmp_path: Path) -> None:
    """Repeated streams must not pay the extra probe every time."""
    pool = _pool_with(tmp_path, [_StubQobuzAccount(16), _StubQobuzAccount(24)])
    first = await pool._resolve_qobuz_direct("1", "max")
    assert first.bitDepth == 24
    assert pool._qobuz_lores, "the CD-only account should be remembered"
    ordered = pool._order_by_capability(pool.active_qobuz_clients(), pool._qobuz_lores)
    assert ordered[0][0] == "acct1"
    second = await pool._resolve_qobuz_direct("1", "max")
    assert second.bitDepth == 24


# ── HEAD on the addon endpoints ───────────────────────────────────────────


async def test_head_on_search_and_stream_is_cheap(http: httpx.AsyncClient) -> None:
    """A HEAD must answer 200 without reaching a provider."""
    for path in ("/search?q=anything", "/stream/315416324?quality=max"):
        response = await http.head(path)
        assert response.status_code == 200, path
        assert response.content == b"", path
        assert response.headers["content-type"].startswith("application/json"), path
