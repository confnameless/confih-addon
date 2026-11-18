"""Aggregate play history for the public "what's playing" page.

What is kept, and what is deliberately not:

*   every play is attributed to a **track**, never to a listener. The only thing
    carried across requests is a salted hash of the client, used to say "how
    many different listeners played this" and nothing else;
*   timestamps are rounded to the minute for the recent feed and to the hour
    for the chart, so the page cannot be used to reconstruct who listened to
    what when;
*   the ledger is bounded on every axis — a maximum number of tracked tracks,
    a short recent feed and a capped per-track listener set — so it cannot grow
    without limit on a small box.

Turning the public feed off entirely is a deployment choice
(``PUBLIC_PLAYS=false``), not something a visitor can work around.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("addon")

#: Quality buckets, in the order the page lists them. Keys match
#: :func:`stats.quality_tier` so the two views agree.
TIER_ORDER: tuple[str, ...] = (
    "dolby_atmos",
    "hires_24bit",
    "lossless_16bit",
    "high_320",
)

TIER_LABELS: dict[str, str] = {
    "dolby_atmos": "Dolby Atmos",
    "hires_24bit": "24-bit Hi-Res",
    "lossless_16bit": "CD lossless",
    "high_320": "High 320 kbps",
}

_MAX_TRACKS = 500
# The feed is meant to hold a whole listening session, not just its tail: a
# burst of skips used to fall off a 40-entry window before the page could show
# them. Bounded, but well past any single sitting.
_MAX_RECENT = 200
_MAX_LISTENERS_PER_TRACK = 200
_MAX_HOURS = 48  # keep two days so the 24 h window is complete after a restart


def _hash_client(subject: str) -> str:
    """One-way client key. Salts with a value this process keeps in memory."""
    return hashlib.sha256(f"{_SALT}{subject}".encode()).hexdigest()[:12]


# Regenerated per process: hashes from a previous run do not link to this one.
_SALT = hashlib.sha256(os.urandom(32)).hexdigest()


def _hour_epoch(ts: int) -> int:
    return ts - (ts % 3600)


@dataclass
class TrackPlays:
    """Everything the page shows for one track. No listener data beyond counts."""

    id: str
    title: str
    artist: str
    album: str
    artwork: str | None
    plays: int = 0
    quality: dict[str, int] = field(default_factory=dict)
    listeners: set[str] = field(default_factory=set)
    # Accumulated count. The hashes themselves are dropped at restart (they
    # are salted per process on purpose), so this is deduplicated per session
    # and a returning listener is counted once more after a restart.
    listeners_total: int = 0
    first_seen: int = 0
    last_seen: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "artwork": self.artwork,
            "plays": self.plays,
            "quality": {k: int(v) for k, v in self.quality.items() if v},
            # Only the count survives to disk; the hashes are for live dedupe.
            "listeners": self.listeners_total,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TrackPlays:
        return cls(
            id=str(raw.get("id") or ""),
            title=str(raw.get("title") or ""),
            artist=str(raw.get("artist") or ""),
            album=str(raw.get("album") or ""),
            artwork=raw.get("artwork"),
            plays=int(raw.get("plays") or 0),
            quality={str(k): int(v) for k, v in (raw.get("quality") or {}).items()},
            listeners=set(),  # hashes intentionally dropped across restarts
            listeners_total=int(raw.get("listeners") or 0),
            first_seen=int(raw.get("first_seen") or 0),
            last_seen=int(raw.get("last_seen") or 0),
        )


class PlayLedger:
    """Bounded, aggregate play counters. Thread-safe; callers are async."""

    def __init__(self, persist_path: Path | str | None = None) -> None:
        # Coerce so a str path can never silently disable persistence.
        self.persist_path = Path(persist_path) if persist_path else None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._tracks: OrderedDict[str, TrackPlays] = OrderedDict()
        self._recent: deque[dict[str, Any]] = deque(maxlen=_MAX_RECENT)
        self._hourly: OrderedDict[int, dict[str, int]] = OrderedDict()
        self._total = 0
        self._last_save: float | None = None
        self._dirty = False
        if self.persist_path is not None:
            self._load()

    # ── persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        assert self.persist_path is not None
        try:
            if not self.persist_path.exists():
                return
            raw = json.loads(self.persist_path.read_text("utf-8"))
        except Exception as exc:  # pragma: no cover - corrupt file is not fatal
            log.warning("plays: could not read %s: %s", self.persist_path, exc)
            return
        try:
            self._total = int(raw.get("total") or 0)
            for item in raw.get("tracks") or []:
                track = TrackPlays.from_dict(item)
                if track.id:
                    self._tracks[track.id] = track
            for row in raw.get("recent") or []:
                self._recent.append(row)
            for entry in raw.get("hourly") or []:
                self._hourly[int(entry.get("hour") or 0)] = {
                    str(k): int(v) for k, v in (entry.get("tiers") or {}).items()
                }
            self._trim_hours()
        except Exception as exc:  # pragma: no cover
            log.warning("plays: could not parse %s: %s", self.persist_path, exc)

    def save(self, force: bool = False) -> None:
        if self.persist_path is None:
            return
        now = time.monotonic()
        with self._write_lock:
            with self._lock:
                if not self._dirty and not force:
                    return
                if not force and self._last_save is not None and now - self._last_save < 20:
                    return
                payload = {
                    "total": self._total,
                    "tracks": [t.to_dict() for t in self._tracks.values()],
                    "recent": list(self._recent),
                    "hourly": [
                        {"hour": hour, "tiers": tiers} for hour, tiers in self._hourly.items()
                    ],
                }
                self._dirty = False
            try:
                self.persist_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.persist_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload), "utf-8")
                os.replace(tmp, self.persist_path)
            except Exception as exc:  # pragma: no cover - disk trouble is not fatal
                with self._lock:
                    self._dirty = True
                log.warning("plays: could not write %s: %s", self.persist_path, exc)
            else:
                with self._lock:
                    self._last_save = now

    # ── recording ──────────────────────────────────────────────────────────

    def record(
        self,
        *,
        track_id: str,
        title: str,
        artist: str,
        album: str,
        artwork: str | None,
        tier: str,
        client: str,
    ) -> None:
        """Count one delivered stream against its track."""
        if not track_id:
            return
        key = tier if tier in TIER_ORDER else "high_320"
        now = int(time.time())
        client_key = _hash_client(client) if client else ""

        with self._lock:
            self._total += 1
            track = self._tracks.get(track_id)
            if track is None:
                track = TrackPlays(
                    id=track_id,
                    title=title or "Unknown track",
                    artist=artist or "",
                    album=album or "",
                    artwork=artwork,
                    first_seen=now,
                )
                self._tracks[track_id] = track
                self._tracks.move_to_end(track_id)
                while len(self._tracks) > _MAX_TRACKS:
                    self._tracks.popitem(last=False)
            else:
                # Keep the freshest presentation data; artwork arrives late.
                if title and track.title in ("", "Unknown track"):
                    track.title = title
                if artist and not track.artist:
                    track.artist = artist
                if album and not track.album:
                    track.album = album
                if artwork and not track.artwork:
                    track.artwork = artwork
                self._tracks.move_to_end(track_id)

            track.plays += 1
            track.quality[key] = track.quality.get(key, 0) + 1
            track.last_seen = now
            if (
                client_key
                and client_key not in track.listeners
                and len(track.listeners) < _MAX_LISTENERS_PER_TRACK
            ):
                track.listeners.add(client_key)
                track.listeners_total += 1

            hour = _hour_epoch(now)
            bucket = self._hourly.get(hour)
            if bucket is None:
                bucket = {}
                self._hourly[hour] = bucket
                self._trim_hours()
            bucket[key] = bucket.get(key, 0) + 1

            self._recent.appendleft(
                {
                    "id": track.id,
                    "title": track.title,
                    "artist": track.artist,
                    "artwork": track.artwork,
                    "tier": key,
                    # Rounded on purpose: enough to order the feed, not to audit it.
                    "at": now - (now % 60),
                }
            )
            self._dirty = True

    def _trim_hours(self) -> None:
        cutoff = _hour_epoch(int(time.time())) - (_MAX_HOURS * 3600)
        while self._hourly and next(iter(self._hourly)) < cutoff:
            self._hourly.popitem(last=False)

    # ── reading ────────────────────────────────────────────────────────────

    @staticmethod
    def _row(track: TrackPlays) -> dict[str, Any]:
        total = sum(track.quality.values()) or 1
        return {
            "id": track.id,
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "artwork": track.artwork,
            "plays": track.plays,
            "listeners": track.listeners_total,
            "lastSeen": track.last_seen,
            "quality": [
                {
                    "key": key,
                    "label": TIER_LABELS[key],
                    "count": int(track.quality.get(key, 0)),
                    "share": round(int(track.quality.get(key, 0)) / total, 4),
                }
                for key in TIER_ORDER
                if track.quality.get(key)
            ],
        }

    def top(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock:
            ranked = sorted(
                self._tracks.values(), key=lambda t: (-t.plays, -t.last_seen)
            )[: max(0, limit)]
            return [self._row(t) for t in ranked]

    def search(
        self,
        query: str = "",
        limit: int = 8,
        *,
        tier: str = "",
    ) -> list[dict[str, Any]]:
        """Case-insensitive substring search over recorded tracks, optionally filtered by tier."""
        clean_tier = (tier or "").strip().lower()
        if clean_tier == "all":
            clean_tier = ""
        needle = (query or "").strip()[:200].casefold()
        if not needle and not clean_tier:
            return self.top(limit)
        if clean_tier and clean_tier not in TIER_ORDER:
            return []
        tokens = [tok for tok in needle.split() if tok]
        with self._lock:
            matches = []
            for track in self._tracks.values():
                if clean_tier and not track.quality.get(clean_tier):
                    continue
                if tokens:
                    haystack = f"{track.title} {track.artist} {track.album}".casefold()
                    if not all(tok in haystack for tok in tokens):
                        continue
                matches.append(track)
            matches.sort(key=lambda t: (-t.plays, -t.last_seen))
            return [self._row(t) for t in matches[: max(0, limit)]]

    def recent(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock:
            # Order by the recorded time, not by insertion: a burst of records
            # or a restore from disk must not scramble the feed.
            rows = sorted(self._recent, key=lambda row: int(row.get("at") or 0), reverse=True)
            return [dict(row) for row in rows[: max(0, limit)]]

    def filter_recent(
        self,
        query: str = "",
        limit: int = 12,
        *,
        tier: str = "",
    ) -> list[dict[str, Any]]:
        """Recent play events filtered by query (title/artist/album) and/or quality tier."""
        clean_tier = (tier or "").strip().lower()
        if clean_tier == "all":
            clean_tier = ""
        needle = (query or "").strip()[:200].casefold()
        if not needle and not clean_tier:
            return self.recent(limit)
        if clean_tier and clean_tier not in TIER_ORDER:
            return []
        max_rows = max(0, limit)
        if max_rows == 0:
            return []
        tokens = [tok for tok in needle.split() if tok]
        with self._lock:
            rows = sorted(self._recent, key=lambda row: int(row.get("at") or 0), reverse=True)
            out: list[dict[str, Any]] = []
            for row in rows:
                if clean_tier and row.get("tier") != clean_tier:
                    continue
                if tokens:
                    track = self._tracks.get(str(row.get("id") or ""))
                    album = track.album if track else ""
                    haystack = f"{row.get('title') or ''} {row.get('artist') or ''} {album}".casefold()
                    if not all(tok in haystack for tok in tokens):
                        continue
                out.append(dict(row))
                if len(out) >= max_rows:
                    break
            return out

    def totals(self) -> dict[str, Any]:
        with self._lock:
            tier_counts: dict[str, int] = {key: 0 for key in TIER_ORDER}
            listeners = 0
            for track in self._tracks.values():
                for key, value in track.quality.items():
                    if key in tier_counts:
                        tier_counts[key] += int(value)
                listeners += track.listeners_total
            return {
                "plays": self._total,
                "tracks": len(self._tracks),
                "listeners": listeners,
                "tiers": [
                    {
                        "key": key,
                        "label": TIER_LABELS[key],
                        "count": tier_counts[key],
                    }
                    for key in TIER_ORDER
                ],
            }

    def timeline(self, hours: int = 24) -> list[dict[str, Any]]:
        """One row per hour, newest last, each stacked by quality tier."""
        now_hour = _hour_epoch(int(time.time()))
        with self._lock:
            rows = []
            for offset in range(max(1, hours) - 1, -1, -1):
                hour = now_hour - (offset * 3600)
                bucket = self._hourly.get(hour) or {}
                rows.append(
                    {
                        "hour": hour,
                        "total": sum(bucket.values()),
                        "tiers": {
                            key: int(bucket.get(key, 0))
                            for key in TIER_ORDER
                            if bucket.get(key)
                        },
                    }
                )
            return rows
