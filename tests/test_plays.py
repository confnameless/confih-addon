"""Play ledger: aggregate counts, session dedupe, and honest ordering."""

from __future__ import annotations

from pathlib import Path

from app.plays import TIER_ORDER, PlayLedger
from app.stats import quality_tier


def _play(ledger: PlayLedger, track: str = "t1", **overrides) -> None:
    payload = {
        "track_id": track,
        "title": "One More Time",
        "artist": "Daft Punk",
        "album": "Discovery",
        "artwork": None,
        "tier": "hires_24bit",
        "client": "10.0.0.1",
    }
    payload.update(overrides)
    ledger.record(**payload)


def test_counts_plays_tracks_and_tiers() -> None:
    ledger = PlayLedger()
    _play(ledger)
    _play(ledger, tier="lossless_16bit")
    _play(ledger, track="t2", client="10.0.0.2", tier="high_320")

    totals = ledger.totals()
    assert totals["plays"] == 3
    assert totals["tracks"] == 2
    assert totals["listeners"] == 2
    by_key = {row["key"]: row["count"] for row in totals["tiers"]}
    assert by_key["hires_24bit"] == 1
    assert by_key["lossless_16bit"] == 1
    assert by_key["high_320"] == 1


def test_same_client_counts_once_per_track() -> None:
    ledger = PlayLedger()
    for _ in range(5):
        _play(ledger, client="10.0.0.1")
    _play(ledger, client="10.0.0.2")

    assert ledger.totals()["listeners"] == 2
    assert ledger.top(5)[0]["listeners"] == 2


def test_listener_count_survives_a_restart(tmp_path: Path) -> None:
    """The hashes are per-process, but the count they produced must persist."""
    path = tmp_path / "plays.json"

    first = PlayLedger(persist_path=path)
    _play(first, track="t1", client="10.0.0.1")
    _play(first, track="t1", client="10.0.0.2")
    assert first.totals()["listeners"] == 2
    first.save(force=True)

    second = PlayLedger(persist_path=path)
    assert second.totals()["listeners"] == 2
    assert second.totals()["plays"] == 2

    # A client new to this session still adds one, without re-adding old ones.
    _play(second, track="t1", client="10.0.0.3")
    assert second.totals()["listeners"] == 3


def test_recent_is_ordered_newest_first() -> None:
    ledger = PlayLedger()
    _play(ledger, track="old", client="10.0.0.1")
    _play(ledger, track="new", client="10.0.0.2")

    rows = ledger.recent(10)
    stamps = [row["at"] for row in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_timeline_covers_24_hours_and_is_stacked() -> None:
    ledger = PlayLedger()
    _play(ledger, tier="dolby_atmos")

    rows = ledger.timeline(24)
    assert len(rows) == 24
    assert sum(row["total"] for row in rows) == 1
    assert sum(row["tiers"].get("dolby_atmos", 0) for row in rows) == 1


def test_every_delivered_tier_is_reported() -> None:
    """Tiers shown on the page must match what the stream path actually calls."""
    ledger = PlayLedger()
    for tier in ("dolby_atmos", "hires_24bit", "lossless_16bit", "high_320"):
        _play(ledger, track=tier, tier=tier, client=tier)

    reported = {row["key"] for row in ledger.totals()["tiers"]}
    assert reported == set(TIER_ORDER)


def test_quality_tier_maps_into_the_ledger() -> None:
    """quality_tier() output must always be a tier the ledger can count."""
    ledger = PlayLedger()
    cases = [
        ("FLAC", 24, 96000, False),
        ("FLAC", 16, 44100, False),
        ("AAC", 16, 44100, False),
        ("EAC3", 16, 48000, True),
    ]
    for index, (codec, depth, rate, atmos) in enumerate(cases):
        tier = quality_tier(codec, depth, rate, atmos, codec)
        assert tier in TIER_ORDER, f"{codec} produced unknown tier {tier!r}"
        _play(ledger, track=f"t{index}", tier=tier, client=f"10.0.0.{index}")

    assert ledger.totals()["plays"] == len(cases)


def test_unknown_tier_falls_back_instead_of_dropping_the_play() -> None:
    ledger = PlayLedger()
    _play(ledger, tier="nonsense")
    assert ledger.totals()["plays"] == 1
    assert ledger.top(1)[0]["quality"][0]["key"] == "high_320"


def test_ledger_is_bounded() -> None:
    ledger = PlayLedger()
    for index in range(1200):
        _play(ledger, track=f"t{index}", client=f"10.0.0.{index % 50}")

    assert len(ledger.top(500)) <= 500
    assert len(ledger.recent(500)) <= 500


def test_save_debounces_and_saves_when_monotonic_under_20(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    clock = {"now": 5.0}
    monkeypatch.setattr("app.plays.time.monotonic", lambda: clock["now"])

    path = tmp_path / "plays.json"
    ledger = PlayLedger(persist_path=path)

    _play(ledger, track="t1")
    ledger.save(force=False)
    assert path.is_file()
    saved1 = json.loads(path.read_text("utf-8"))
    assert saved1["total"] == 1

    # Within 20s window: debounced
    clock["now"] = 6.0
    _play(ledger, track="t2", client="10.0.0.2")
    ledger.save(force=False)
    saved2 = json.loads(path.read_text("utf-8"))
    assert saved2["total"] == 1

    # Past 20s window: flushed
    clock["now"] = 26.0
    ledger.save(force=False)
    saved3 = json.loads(path.read_text("utf-8"))
    assert saved3["total"] == 2


def test_concurrent_saves_preserve_valid_json_and_latest_total(tmp_path: Path) -> None:
    import json
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "plays.json"
    ledger = PlayLedger(persist_path=path)

    def worker(idx: int) -> None:
        _play(ledger, track=f"t{idx}", client=f"10.0.0.{idx % 10}")
        ledger.save(force=True)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(30)))

    ledger.save(force=True)
    data = json.loads(path.read_text("utf-8"))
    assert data["total"] == 30


def test_search_and_filter_recent_with_query_and_tier() -> None:
    ledger = PlayLedger()
    _play(
        ledger,
        track="t1",
        title="One More Time",
        artist="Daft Punk",
        album="Discovery",
        tier="hires_24bit",
    )
    _play(
        ledger,
        track="t2",
        title="Harder Better Faster",
        artist="Daft Punk",
        album="Discovery",
        tier="dolby_atmos",
        client="10.0.0.2",
    )
    _play(
        ledger,
        track="t3",
        title="Teardrop",
        artist="Massive Attack",
        album="Mezzanine",
        tier="lossless_16bit",
        client="10.0.0.3",
    )

    # Case-insensitive album match
    disc = ledger.search("DISCOVERY", limit=10)
    assert {r["id"] for r in disc} == {"t1", "t2"}

    # Tier filtering
    atmos = ledger.search("", limit=10, tier="dolby_atmos")
    assert [r["id"] for r in atmos] == ["t2"]

    # Combined query + tier
    combo = ledger.search("daft", limit=10, tier="dolby_atmos")
    assert [r["id"] for r in combo] == ["t2"]

    # Invalid tier yields empty list
    assert ledger.search("daft", limit=10, tier="invalid_tier") == []

    # filter_recent matches album via _tracks and respects tier
    recent_mezz = ledger.filter_recent("mezzanine", limit=10)
    assert [r["id"] for r in recent_mezz] == ["t3"]
    recent_atmos = ledger.filter_recent("", limit=10, tier="dolby_atmos")
    assert [r["id"] for r in recent_atmos] == ["t2"]
    assert ledger.filter_recent("daft", limit=10, tier="invalid_tier") == []


def test_record_preserves_hot_tracks_via_lru_eviction() -> None:
    ledger = PlayLedger()
    _play(ledger, track="top_hit", title="Top Hit", client="10.0.0.1")
    for idx in range(499):
        _play(ledger, track=f"idle_{idx}", title=f"Idle {idx}", client="10.0.0.2")

    # Replaying an existing track must move it to MRU so subsequent insertions
    # evict idle tracks rather than the active track.
    for _ in range(5):
        _play(ledger, track="top_hit", title="Top Hit", client="10.0.0.1")

    for idx in range(10):
        _play(ledger, track=f"new_{idx}", title=f"New {idx}", client="10.0.0.3")

    surviving_ids = {row["id"] for row in ledger.top(500)}
    assert "top_hit" in surviving_ids
    for idx in range(10):
        assert f"idle_{idx}" not in surviving_ids
        assert f"new_{idx}" in surviving_ids


def test_save_retry_not_blocked_by_debounce_after_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    import json
    import os

    clock = {"now": 100.0}
    monkeypatch.setattr("app.plays.time.monotonic", lambda: clock["now"])

    path = tmp_path / "plays.json"
    ledger = PlayLedger(persist_path=path)
    _play(ledger, track="t1")

    real_replace = os.replace

    def failing_replace(src, dst):
        raise OSError("simulated disk error")

    monkeypatch.setattr("app.plays.os.replace", failing_replace)
    ledger.save(force=False)
    assert ledger._dirty is True
    assert ledger._last_save is None
    assert not path.exists()

    monkeypatch.setattr("app.plays.os.replace", real_replace)
    clock["now"] = 101.0
    ledger.save(force=False)
    assert path.is_file()
    assert json.loads(path.read_text("utf-8"))["total"] == 1


def test_unicode_casefold_search_matches_eszett_and_diacritics() -> None:
    ledger = PlayLedger()
    _play(
        ledger,
        track="de1",
        title="Große Straße",
        artist="Künstler",
        album="Alben",
    )
    _play(
        ledger,
        track="is1",
        title="Hyperballad",
        artist="Björk",
        album="Post",
        client="10.0.0.2",
    )

    assert [row["id"] for row in ledger.search("strasse", limit=10)] == ["de1"]
    assert [row["id"] for row in ledger.search("björk", limit=10)] == ["is1"]
    assert [row["id"] for row in ledger.filter_recent("strasse", limit=10)] == ["de1"]
