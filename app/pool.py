"""Multi-provider, multi-account pool with automatic failover between Qobuz and Tidal.

Responsibilities:
- Manage persistent accounts in ``data/accounts.json`` (Qobuz & Tidal).
- Rotate across healthy accounts if one account hits a rate limit (429) or auth failure.
- Provide two-way automatic failover between Qobuz and Tidal on both ``/search``
  and ``/stream/{track_id}`` (matching missing or failed tracks by ISRC or
  Title + Artist + Duration).
- Manage 1-Click Tidal OAuth2 Device Code login in the background.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .logging_setup import register_secrets
from .models import AddonSearchResponse, AddonStream, AddonTrack
from .qobuz import (
    QobuzClient,
    QobuzGeoBlocked,
    QobuzNotFound,
    QobuzTrack,
)
from .quality import search_labels, stream_meta
from .stats import TrafficStats
from .tidal import (
    DEFAULT_TIDAL_CLIENT_ID,
    DEFAULT_TIDAL_CLIENT_SECRET,
    FIRE_TV_CLIENT_ID,
    LEGACY_TIDAL_CLIENT_ID,
    DeviceAuthPending,
    TidalClient,
    TidalNotFound,
    TidalStream,
    TidalTrack,
    poll_tidal_device_token,
    start_tidal_device_login,
)

log = logging.getLogger("pool")

_MAX_TRACK_META = 8192
# A Qobuz account marked dead (expired token / geo block) is retried this rarely.
_DEAD_RETRY_SECONDS = 300
_MAX_DASH_MANIFESTS = 16384
_DASH_TTL_SECONDS = 7200  # 2 hours so paused listeners don't hit 404 when resuming
# Counts alone do not bound memory: one MPD can be tens of kilobytes. Cap the
# total held bytes as well, so the cache cannot grow to hundreds of megabytes.
_DASH_MAX_BYTES_DEFAULT = 48 * 1024 * 1024


@dataclass(frozen=True)
class TrackMeta:
    id: str
    provider: str  # "qobuz" or "tidal"
    title: str
    artist: str
    album: str
    duration: int | None
    isrc: str | None
    alt_id: str | None = None  # Paired track ID on the other provider if already known
    audio_modes: tuple[str, ...] = ()
    # Presentation only: the cover the public /plays page shows. Providers
    # return it alongside the track, so it costs nothing to keep.
    artwork: str | None = None


@dataclass(frozen=True)
class ResolvedStream:
    """Internal stream result: public addon payload plus delivery metadata.

    ``stream`` is the only part serialized to BitChord. ``provider`` and the
    failover fields let the request boundary update telemetry without leaking
    provider internals into the public addon contract.
    """

    stream: AddonStream
    provider: str
    failover: bool = False
    failover_reason: str = ""
    is_atmos: bool = False

    @property
    def quality(self) -> str:
        return self.stream.quality

    @property
    def bit_depth(self) -> int | None:
        return self.stream.bitDepth

    @property
    def sample_rate(self) -> int | None:
        return self.stream.sampleRate


def _target_bit_depth(tier: str) -> int:
    """The bit depth a rendition must reach to satisfy ``tier``.

    `max`/`hires` want the hi-res master (24-bit); CD lossless is satisfied by
    16-bit; the lossy rungs accept whatever comes back.
    """
    from .quality import normalize_quality

    normalised = normalize_quality(tier)
    if normalised in ("max", "hires", "atmos"):
        return 24
    if normalised == "lossless":
        return 16
    return 0


def _stream_expiry(seconds: int = 3600) -> str:
    """ISO-8601 UTC timestamp for when a signed stream URL stops working.

    Provider URLs live about an hour; the spec lets an addon state it so a host
    can avoid handing out a dead link, and the reference host assumes an hour
    when it is absent.
    """
    from datetime import datetime, timedelta

    return (
        datetime.now(UTC) + timedelta(seconds=seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm_text(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


# Explicit edition markers that may be appended to a title. A candidate with
# one of these suffixes is still the same song; an unknown suffix (for example
# "Part 2") is not treated as a match.
_VERSION_MARKERS = frozenset(
    {
        "acoustic",
        "album",
        "anniversary",
        "bonus",
        "clean",
        "deluxe",
        "demo",
        "edit",
        "explicit",
        "extended",
        "instrumental",
        "live",
        "mix",
        "mono",
        "original",
        "radio",
        "remaster",
        "remastered",
        "remastering",
        "reissue",
        "remix",
        "single",
        "soundtrack",
        "stereo",
        "version",
    }
)
_TRAILING_VERSION_RE = re.compile(
    r"\s*(?:[\(\[\{]\s*([^)\]\}]*)\s*[\)\]\}]|-\s*([^-]+))\s*$"
)


def _title_parts(title: str) -> tuple[str, str]:
    """Return ``(base_title, version_suffix)`` with both normalized.

    Only trailing parenthetical/bracket groups and a trailing ``- suffix`` are
    treated as edition markers. Text in the middle of a title stays in the
    base, so two different songs cannot collide just because they share a
    prefix.
    """
    text = (title or "").strip()
    suffixes: list[str] = []
    while text:
        match = _TRAILING_VERSION_RE.search(text)
        if match is None:
            break
        suffix = (match.group(1) or match.group(2) or "").strip()
        if not suffix:
            break
        suffixes.insert(0, _norm_text(suffix))
        text = text[: match.start()].rstrip(" -")
    return _norm_text(text), " ".join(s for s in suffixes if s)


def _version_suffix_is_explicit(version: str) -> bool:
    tokens = version.split()
    return bool(tokens) and any(token in _VERSION_MARKERS for token in tokens)


def _primary_artist(value: str) -> str:
    primary = re.split(
        r"\s+(?:feat\.?|ft\.?|featuring|with)\s+|[,&]",
        value or "",
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return _norm_text(primary)


def _artists_match(meta_artist: str, cand_artist: str) -> bool:
    a1, a2 = _norm_text(meta_artist), _norm_text(cand_artist)
    if not a1 or not a2:
        return False
    if a1 == a2 or a1 in a2 or a2 in a1:
        return True
    p1, p2 = _primary_artist(meta_artist), _primary_artist(cand_artist)
    return bool(p1) and p1 == p2


def _tracks_match(
    meta: TrackMeta,
    cand_title: str,
    cand_artist: str,
    cand_dur: int | None,
    cand_isrc: str | None,
    *,
    strict: bool = True,
    duration_tolerance: float = 5.0,
) -> bool:
    """Return True only when a candidate is the same recording.

    An exact ISRC is conclusive and wins immediately. Otherwise the title
    must match exactly after normalization, the artist must be compatible,
    and (in strict mode) both durations must be present and within the
    configured tolerance. A known edition suffix on one side is allowed only
    when the base title is identical.
    """
    if meta.isrc and cand_isrc and meta.isrc.upper() == cand_isrc.upper():
        return True

    if not strict:
        # Explicit opt-out: keep the old fuzzy title/artist behaviour, but
        # still never let the caller take the first unrelated search result.
        t1, t2 = _norm_text(meta.title), _norm_text(cand_title)
        if not t1 or not t2 or (t1 != t2 and t1 not in t2 and t2 not in t1):
            return False
        a1, a2 = _norm_text(meta.artist), _norm_text(cand_artist)
        if a1 and a2:
            first_a1 = a1.split()[0] if a1.split() else ""
            if first_a1 and first_a1 not in a2 and a2 not in a1:
                return False
        if meta.duration and cand_dur and abs(meta.duration - cand_dur) > 8:
            return False
        return True

    base1, version1 = _title_parts(meta.title)
    base2, version2 = _title_parts(cand_title)
    if not base1 or not base2 or base1 != base2:
        return False
    if version1 != version2:
        if version1 and version2:
            return False
        extra = version1 or version2
        if not _version_suffix_is_explicit(extra):
            return False

    if not _artists_match(meta.artist, cand_artist):
        return False

    if meta.duration is not None and cand_dur is not None:
        if abs(meta.duration - cand_dur) > duration_tolerance:
            return False
    else:
        # Missing duration on either side is an unverifiable match. ISRC is
        # the only acceptable way to bypass that.
        return False
    return True


class ProviderPool:
    def __init__(
        self,
        settings: Settings,
        primary_qobuz: QobuzClient,
        stats: TrafficStats,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.primary_qobuz = primary_qobuz
        self.stats = stats
        self._transport = transport

        self.accounts_path: Path = Path(settings.data_dir).expanduser() / "accounts.json"
        self.primary_provider: str = getattr(settings, "primary_provider", "tidal")  # "qobuz", "tidal", or "both"
        self.auto_failover: bool = True

        # Additional Qobuz accounts added via Admin / Bot (beyond primary_qobuz)
        self.extra_qobuz: dict[str, dict[str, Any]] = {}
        self.qobuz_clients: dict[str, QobuzClient] = {}
        self.primary_qobuz_enabled: bool = True
        self.primary_qobuz_label: str = "Qobuz Primary (.env)"

        # Tidal accounts managed via Admin / Bot
        self.tidal_entries: dict[str, dict[str, Any]] = {}
        self.tidal_clients: dict[str, TidalClient] = {}

        # Round-robin indices
        self._qobuz_rr = 0
        self._tidal_rr = 0
        self._both_search_rr = 0

        # Track metadata LRU cache for cross-provider stream failover
        self._track_meta: OrderedDict[str, TrackMeta] = OrderedDict()

        # Consecutive track sequence memory for pre-caching next song in albums/playlists
        self._track_sequences: OrderedDict[str, str] = OrderedDict()
        self._last_alert_time: dict[str, float] = {}

        # accounts.json is written from worker threads; this serialises them
        self._pending_state: dict[str, Any] | None = None
        self._state_write_lock = threading.Lock()

        # Short-lived MPEG-DASH manifest cache for Tidal Hi-Res streams
        # manifest_id -> (expires_at_mono, mpd_xml_str)
        self._dash_manifests: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._dash_bytes = 0
        self._dash_max_bytes = (
            max(4, int(getattr(settings, "dash_cache_max_mb", 48))) * 1024 * 1024
        )

        # Playback-learned sequences: what a listener actually played next.
        # The old pre-cache guessed from search-result adjacency, which is not
        # play order, and spent a provider call per stream to do it.
        self._last_play: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._learned_next: OrderedDict[str, str] = OrderedDict()

        # Accounts that answered a hi-res request with a CD rendition. They are
        # tried last for a while, so the extra probe that finds the 24-bit
        # master is paid once per account rather than on every request.
        self._qobuz_lores: dict[str, float] = {}
        self._tidal_lores: dict[str, float] = {}

        # Active Tidal Device Auth flow state
        self.pending_device_auth: DeviceAuthPending | None = None
        self.device_auth_status: str = "idle"
        self.device_auth_last_account: str | None = None
        self._device_poll_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._warm_task: asyncio.Task[None] | None = None
        self._started = False

    # ── Lifecycle & Persistence ──────────────────────────────────────────

    async def start(self) -> None:
        if self._started:
            return
        self._load_state()
        for qclient in self.qobuz_clients.values():
            await qclient.start()
        for tclient in self.tidal_clients.values():
            await tclient.start()
            if tclient.configured and self.settings.qobuz_self_check:
                asyncio.create_task(tclient.check_health())
        self._register_pool_secrets()
        self._started = True
        self._health_task = asyncio.create_task(self._periodic_health_loop())
        self._warm_task = asyncio.create_task(self._keepalive_loop())

    async def stop(self) -> None:
        if self._warm_task is not None:
            self._warm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._warm_task
            self._warm_task = None
        if self._health_task is not None:
            self._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._health_task
            self._health_task = None
        if self._device_poll_task is not None:
            self._device_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._device_poll_task
            self._device_poll_task = None
        for qclient in list(self.qobuz_clients.values()):
            await qclient.stop()
        for tclient in list(self.tidal_clients.values()):
            await tclient.stop()
        # Flush both state files before the loop goes away: their writes are
        # normally deferred to worker threads, and a deferred write here
        # would be lost at shutdown.
        await asyncio.to_thread(self.save_state)
        await asyncio.to_thread(self.stats.save, True)
        self._started = False

    async def _keepalive_loop(self) -> None:
        """Every 60 s keep warm connections to the providers that are in use."""
        while True:
            for tclient in list(self.tidal_clients.values()):
                if tclient.configured:
                    await tclient.warm()
            for _, _, qclient in self.active_qobuz_clients():
                await qclient.warm()
            await asyncio.sleep(60)

    async def _periodic_health_loop(self) -> None:
        while True:
            await asyncio.sleep(180)  # Check every 3 minutes
            for tclient in list(self.tidal_clients.values()):
                if tclient.configured:
                    with contextlib.suppress(Exception):
                        await tclient.check_health()
            self.stats.save(force=True)

    def _register_pool_secrets(self) -> None:
        candidates: list[str] = [
            self.primary_qobuz._token,
            self.settings.qobuz_user_auth_token,
            self.settings.qobuz_app_secret,
            self.settings.qobuz_password,
            self.settings.qobuz_password_md5,
        ]
        for acc in self.extra_qobuz.values():
            for key in ("user_auth_token", "app_secret", "password", "password_md5"):
                val = acc.get(key)
                if isinstance(val, str) and val:
                    candidates.append(val)
        for qclient in self.qobuz_clients.values():
            candidates.extend(
                [
                    qclient._token,
                    qclient.settings.qobuz_user_auth_token,
                    qclient.settings.qobuz_app_secret,
                    qclient.settings.qobuz_password,
                    qclient.settings.qobuz_password_md5,
                ]
            )
        for acc in self.tidal_entries.values():
            for key in ("access_token", "refresh_token", "client_secret"):
                val = acc.get(key)
                if isinstance(val, str) and val:
                    candidates.append(val)
        for tclient in self.tidal_clients.values():
            candidates.extend(
                [
                    tclient.access_token,
                    tclient.refresh_token,
                    tclient.client_secret,
                ]
            )
        register_secrets(candidates)

    def _load_state(self) -> None:
        if not self.accounts_path.exists():
            return
        try:
            raw = json.loads(self.accounts_path.read_text(encoding="utf-8"))
            def_pri = getattr(self.settings, "primary_provider", "tidal")
            self.primary_provider = str(raw.get("primary_provider") or def_pri).lower()
            if self.primary_provider not in ("qobuz", "tidal", "both"):
                self.primary_provider = def_pri
            self.auto_failover = bool(raw.get("auto_failover", True))
            self.primary_qobuz_enabled = bool(raw.get("primary_qobuz_enabled", True))
            if raw.get("primary_qobuz_label"):
                self.primary_qobuz_label = str(raw["primary_qobuz_label"])

            for acc in raw.get("qobuz_accounts") or []:
                if not isinstance(acc, dict) or not acc.get("id"):
                    continue
                register_secrets(
                    str(acc.get(k) or "")
                    for k in ("user_auth_token", "app_secret", "password", "password_md5")
                )
                aid = str(acc["id"])
                self.extra_qobuz[aid] = acc
                self.qobuz_clients[aid] = self._build_qobuz_client(acc)

            for acc in raw.get("tidal_accounts") or []:
                if not isinstance(acc, dict) or not acc.get("id"):
                    continue
                register_secrets(
                    str(acc.get(k) or "")
                    for k in ("access_token", "refresh_token", "client_secret")
                )
                aid = str(acc["id"])
                self.tidal_entries[aid] = acc
                self.tidal_clients[aid] = self._build_tidal_client(acc)

            self._register_pool_secrets()
        except Exception as exc:
            log.warning("Failed to load accounts pool from %s: %s", self.accounts_path, exc)

    def save_state(self) -> None:
        """Snapshot accounts in memory; write the file off the event loop.

        Token refreshes and admin calls run on the event loop, so the atomic
        disk write is handed to a worker thread instead of stalling streams.
        """
        try:
            self._pending_state = self._state_payload()
        except Exception as exc:
            log.warning("Could not persist accounts.json: %s", self.accounts_path, exc)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_state()
        else:
            loop.create_task(asyncio.to_thread(self._write_state))

    def _state_payload(self) -> dict[str, Any]:
        # Sync latest tokens from active clients back into entries
        for aid, tclient in self.tidal_clients.items():
            if aid in self.tidal_entries:
                self.tidal_entries[aid].update(
                    {
                        "access_token": tclient.access_token,
                        "refresh_token": tclient.refresh_token,
                        "expires_at": tclient.expires_at,
                        "user_id": tclient.user_id,
                        "country_code": tclient.country_code,
                        "subscription": tclient.subscription,
                        "client_id": tclient.client_id,
                        "client_secret": tclient.client_secret,
                    }
                )
        for aid, qclient in self.qobuz_clients.items():
            if aid in self.extra_qobuz:
                snap = qclient.health_snapshot()
                self.extra_qobuz[aid]["user_auth_token"] = qclient._token
                self.extra_qobuz[aid]["user_id"] = qclient._user_id
                if snap.get("subscription"):
                    self.extra_qobuz[aid]["subscription"] = snap["subscription"]

        self._register_pool_secrets()

        # Plain copies: the write happens on another thread while this one
        # keeps managing the live entry dicts.
        return {
            "primary_provider": self.primary_provider,
            "auto_failover": self.auto_failover,
            "primary_qobuz_enabled": self.primary_qobuz_enabled,
            "primary_qobuz_label": self.primary_qobuz_label,
            "qobuz_accounts": [dict(entry) for entry in self.extra_qobuz.values()],
            "tidal_accounts": [dict(entry) for entry in self.tidal_entries.values()],
            "updated_at": int(time.time()),
        }

    def _write_state(self) -> None:
        """Atomic replace of accounts.json; the only blocking part."""
        with self._state_write_lock:
            payload = self._pending_state
            if payload is None:
                return
            try:
                self.accounts_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.accounts_path.with_suffix(".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2)
                    handle.write("\n")
                os.replace(tmp, self.accounts_path)
            except Exception as exc:
                log.warning("Could not persist accounts.json: %s", self.accounts_path, exc)

    def _build_qobuz_client(self, acc: dict[str, Any]) -> QobuzClient:
        aid = str(acc["id"])
        token_file = Path(self.settings.data_dir).expanduser() / f"qobuz-{aid}.json"
        copy_settings = self.settings.model_copy(
            update={
                "qobuz_user_auth_token": str(acc.get("user_auth_token") or ""),
                "qobuz_user_id": str(acc.get("user_id") or ""),
                "qobuz_email": str(acc.get("email") or ""),
                "qobuz_password": str(acc.get("password") or ""),
                "qobuz_password_md5": str(acc.get("password_md5") or ""),
                "qobuz_app_id": str(acc.get("app_id") or self.settings.qobuz_app_id),
                "qobuz_app_secret": str(acc.get("app_secret") or self.settings.qobuz_app_secret),
                "qobuz_token_file": str(token_file),
            }
        )
        return QobuzClient(copy_settings, transport=self._transport)

    def _build_tidal_client(self, acc: dict[str, Any]) -> TidalClient:
        aid = str(acc["id"])
        cid = str(acc.get("client_id") or "").strip()
        csec = str(acc.get("client_secret") or "").strip()
        # Upgrade deprecated Fire TV or legacy client IDs to native Atmos credentials
        if cid in (FIRE_TV_CLIENT_ID, LEGACY_TIDAL_CLIENT_ID):
            cid = DEFAULT_TIDAL_CLIENT_ID
            csec = DEFAULT_TIDAL_CLIENT_SECRET
            if acc.get("refresh_token"):
                access_token = ""
                expires_at = 0.0
            else:
                access_token = str(acc.get("access_token") or "")
                expires_at = float(acc.get("expires_at") or 0.0)
        else:
            if not cid:
                cid = DEFAULT_TIDAL_CLIENT_ID
                csec = DEFAULT_TIDAL_CLIENT_SECRET
            access_token = str(acc.get("access_token") or "")
            expires_at = float(acc.get("expires_at") or 0.0)

        return TidalClient(
            account_id=aid,
            label=str(acc.get("label") or f"Tidal ({aid})"),
            access_token=access_token,
            refresh_token=str(acc.get("refresh_token") or ""),
            expires_at=expires_at,
            user_id=str(acc.get("user_id") or ""),
            country_code=str(acc.get("country_code") or "US"),
            client_id=cid,
            client_secret=csec,
            proxy_url=self.settings.tidal_proxy_url,
            on_token_Refresh=lambda _: self.save_state(),
            transport=self._transport,
        )

    # ── Account Selection ────────────────────────────────────────────────

    def active_qobuz_clients(self) -> list[tuple[str, str, QobuzClient]]:
        """Return list of (id, label, QobuzClient) for enabled & configured Qobuz accounts."""
        items: list[tuple[str, str, QobuzClient]] = []
        if (
            self.primary_qobuz_enabled
            and self.settings.qobuz_user_auth_token.strip()
            and self.primary_qobuz.settings.qobuz_configured
        ):
            items.append(("qobuz-primary", self.primary_qobuz_label, self.primary_qobuz))
        for aid, entry in self.extra_qobuz.items():
            if not entry.get("enabled", True):
                continue
            client = self.qobuz_clients.get(aid)
            if client and client.settings.qobuz_configured:
                items.append((aid, str(entry.get("label") or aid), client))
        # Healthy accounts first. A dead one (expired token / geo block) is
        # left out so every request does not pay for a doomed login, and is
        # only given another chance every _DEAD_RETRY_SECONDS.
        healthy = [x for x in items if x[2]._token_ok is not False and x[2]._geo_ok is not False]
        now = time.monotonic()
        retry_due = [
            x
            for x in items
            if x not in healthy
            and now - max(x[2]._token_checked_at or 0.0, x[2]._geo_checked_at or 0.0)
            > _DEAD_RETRY_SECONDS
        ]
        if len(healthy) > 1:
            idx = self._qobuz_rr % len(healthy)
            self._qobuz_rr += 1
            healthy = healthy[idx:] + healthy[:idx]
        return healthy + retry_due

    def active_tidal_clients(self) -> list[tuple[str, str, TidalClient]]:
        """Return list of (id, label, TidalClient) for enabled & configured Tidal accounts."""
        items: list[tuple[str, str, TidalClient]] = []
        for aid, entry in self.tidal_entries.items():
            if not entry.get("enabled", True):
                continue
            client = self.tidal_clients.get(aid)
            if client and client.configured:
                items.append((aid, client.label, client))
        healthy = [x for x in items if x[2].is_healthy]
        unhealthy = [x for x in items if x not in healthy]
        if len(healthy) > 1:
            idx = self._tidal_rr % len(healthy)
            self._tidal_rr += 1
            healthy = healthy[idx:] + healthy[:idx]
        return healthy + unhealthy

    @property
    def has_healthy_qobuz(self) -> bool:
        clients = self.active_qobuz_clients()
        return any(getattr(c[2], "is_healthy", True) for c in clients)

    @property
    def has_healthy_tidal(self) -> bool:
        clients = self.active_tidal_clients()
        return any(getattr(c[2], "is_healthy", True) for c in clients)

    @property
    def has_tidal(self) -> bool:
        return self.has_healthy_tidal or len(self.active_tidal_clients()) > 0

    @property
    def has_qobuz(self) -> bool:
        return self.has_healthy_qobuz or len(self.active_qobuz_clients()) > 0

    # ── Track Metadata & DASH Manifest Caches ────────────────────────────

    def remember_track(self, meta: TrackMeta) -> None:
        if meta.id in self._track_meta:
            self._track_meta.move_to_end(meta.id)
        self._track_meta[meta.id] = meta
        while len(self._track_meta) > _MAX_TRACK_META:
            self._track_meta.popitem(last=False)

    def get_track_meta(self, track_id: str) -> TrackMeta | None:
        clean = track_id.removeprefix("qobuz:")
        return self._track_meta.get(track_id) or self._track_meta.get(clean)

    def store_dash_manifest(self, mpd_xml: str) -> str:
        digest = hashlib.sha256(mpd_xml.encode("utf-8")).hexdigest()[:24]
        now = time.monotonic()
        size = len(mpd_xml.encode("utf-8"))
        previous = self._dash_manifests.get(digest)
        if previous is not None:
            self._dash_bytes -= len(previous[1].encode("utf-8"))
        if size > self._dash_max_bytes:
            # One manifest larger than the whole budget: serve it, never store it.
            return digest
        self._dash_manifests[digest] = (now + _DASH_TTL_SECONDS, mpd_xml)
        self._dash_bytes += size
        self._dash_manifests.move_to_end(digest)
        while self._dash_manifests and (
            len(self._dash_manifests) > _MAX_DASH_MANIFESTS
            or self._dash_bytes > self._dash_max_bytes
        ):
            _, evicted = self._dash_manifests.popitem(last=False)
            self._dash_bytes -= len(evicted[1].encode("utf-8"))
        return digest

    def get_dash_manifest(self, manifest_id: str) -> str | None:
        item = self._dash_manifests.get(manifest_id)
        if item is None:
            return None
        expires_at, xml_str = item
        if time.monotonic() > expires_at:
            self._dash_manifests.pop(manifest_id, None)
            self._dash_bytes -= len(xml_str.encode("utf-8"))
            return None
        return xml_str

    def record_track_sequence(self, prev_id: str, next_id: str) -> None:
        """Remember a sequence actually observed in playback."""
        self._track_sequences[prev_id] = next_id
        while len(self._track_sequences) > 1024:
            self._track_sequences.popitem(last=False)

    def get_next_track(self, track_id: str) -> str | None:
        clean = track_id.removeprefix("qobuz:")
        return self._track_sequences.get(track_id) or self._track_sequences.get(clean)

    def record_play(self, client: str, track_id: str) -> None:
        """Learn what this listener plays next, from playback itself.

        Only a real consecutive play teaches a sequence, so the pre-cache stops
        resolving whatever merely sat below this row in a search answer.
        """
        if not client or not track_id:
            return
        now = time.monotonic()
        previous = self._last_play.get(client)
        if previous is not None:
            prev_id, at = previous
            # 15 minutes: an album side, not a whole session. Beyond that the
            # "next" track says more about what the listener browsed than
            # about what follows this one.
            if prev_id and prev_id != track_id and now - at <= 900:
                self.record_track_sequence(prev_id, track_id)
        self._last_play[client] = (track_id, now)
        self._last_play.move_to_end(client)
        while len(self._last_play) > 4096:
            self._last_play.popitem(last=False)

    async def notify_account_issue(self, provider: str, label: str, error_msg: str) -> None:
        log.warning("Account issue on %s (%s): %s", provider, label, error_msg)

    # ── Unified Search with Auto-Failover ────────────────────────────────

    async def search(
        self,
        query: str,
        tier: str,
        limit: int,
        *,
        title: str = "",
        artist: str = "",
    ) -> AddonSearchResponse:
        """Catalogue search, re-ranked for a known recording when asked.

        BitChord 1.10+ sends the target's own `title` and `artist` beside `q`
        when it is resolving a track it already knows. `q` folds both into one
        string, so the addon cannot tell which words name the singer — given
        them apart it can put the right recording first, which is what the
        host uses: it preserves this order and takes rows from the top.
        """
        payload = await self._search_providers(query, tier, limit)
        if not (title or artist) or not payload.tracks:
            return payload
        ranked = self._rank_for_target(payload.tracks, title=title, artist=artist)
        if ranked == payload.tracks:
            return payload
        return AddonSearchResponse(tracks=ranked)

    @staticmethod
    def _rank_for_target(
        rows: list[AddonTrack], *, title: str = "", artist: str = ""
    ) -> list[AddonTrack]:
        """Stable re-rank: exact recording first, everything else untouched.

        Deliberately conservative — it never drops a row, because a provider's
        relevance occasionally finds the track under a title this comparison
        does not recognise.
        """
        want_base, want_version = _title_parts(title) if title else ("", "")
        want_artist = _norm_text(artist) if artist else ""
        if not want_base and not want_artist:
            return rows

        def score(row: AddonTrack) -> int:
            total = 0
            if want_base:
                base, version = _title_parts(row.title)
                if base == want_base:
                    total += 4
                    if version == want_version:
                        total += 2
                    elif not version or not want_version:
                        total += 1
                elif _norm_text(row.title) == _norm_text(title):
                    total += 3
            if want_artist and row.artist:
                if _artists_match(artist, row.artist):
                    total += 3
                elif _norm_text(row.artist) == want_artist:
                    total += 3
            return total

        scored = sorted(enumerate(rows), key=lambda pair: (-score(pair[1]), pair[0]))
        return [row for _, row in scored]

    async def _search_providers(self, query: str, tier: str, limit: int) -> AddonSearchResponse:
        has_q = self.has_healthy_qobuz
        has_t = self.has_healthy_tidal

        # Both providers healthy in "both" mode: dual search in parallel and apply dynamic quality optimization
        if self.primary_provider == "both" and has_q and has_t:
            q_task = asyncio.create_task(self._search_qobuz_Safe(query, tier, limit))
            t_task = asyncio.create_task(self._search_tidal_safe(query, tier, limit))

            done, pending = await asyncio.wait([q_task, t_task], timeout=2.5)
            for p in pending:
                p.cancel()

            q_rows: list[AddonTrack] = []
            t_rows: list[AddonTrack] = []

            if q_task in done and not q_task.cancelled():
                try:
                    q_rows = q_task.result() or []
                except Exception as exc:
                    log.warning("Qobuz search error in dual mode: %s", exc)
                    q_rows = []

            if t_task in done and not t_task.cancelled():
                try:
                    t_rows = t_task.result() or []
                except Exception as exc:
                    log.warning("Tidal search error in dual mode: %s", exc)
                    t_rows = []

            merged = self._merge_tracks_smart(q_rows, t_rows, limit)
            return AddonSearchResponse(tracks=merged)

        # Primary Tidal mode (or only Tidal is healthy)
        if (self.primary_provider == "tidal" or (has_t and not has_q)) and has_t:
            try:
                t_rows = await self._search_tidal(query, tier, limit)
                if t_rows:
                    if self.auto_failover and has_q and len(t_rows) < min(5, limit):
                        q_rows = await self._search_qobuz_Safe(query, tier, limit)
                        return AddonSearchResponse(tracks=self._merge_tracks_smart(t_rows, q_rows, limit))
                    return AddonSearchResponse(tracks=t_rows)
            except Exception as exc:
                log.warning("Primary Tidal search failed (%s), failing over to Qobuz", exc)
                if not (self.auto_failover and has_q):
                    return AddonSearchResponse(tracks=[])
            if has_q:
                self.stats.record_failover("search: Tidal unavailable")
                try:
                    q_rows = await self._search_qobuz(query, tier, limit)
                    return AddonSearchResponse(tracks=q_rows)
                except Exception as exc:
                    log.warning("Qobuz failover search failed: %s", exc)
                    return AddonSearchResponse(tracks=[])
            return AddonSearchResponse(tracks=[])

        # Default: Qobuz Primary -> Tidal Failover
        last_qobuz_err: Exception | None = None
        if has_q:
            try:
                q_rows = await self._search_qobuz(query, tier, limit)
                if q_rows:
                    if self.auto_failover and has_t:
                        t_rows = await self._search_tidal_safe(query, tier, limit)
                        if t_rows:
                            return AddonSearchResponse(tracks=self._merge_tracks_smart(q_rows, t_rows, limit))
                    return AddonSearchResponse(tracks=q_rows)
            except QobuzGeoBlocked:
                if not (self.auto_failover and has_t):
                    raise
                log.warning("Primary Qobuz search geo-blocked, failing over to Tidal")
            except Exception as exc:
                last_qobuz_err = exc
                if not (self.auto_failover and has_t):
                    log.warning("Primary Qobuz search failed: %s", exc)
                    return AddonSearchResponse(tracks=[])
                log.warning("Primary Qobuz search failed (%s), failing over to Tidal", exc)

        if has_t and (self.auto_failover or not has_q):
            if last_qobuz_err is not None:
                self.stats.record_failover("search: Qobuz unavailable")
            try:
                t_rows = await self._search_tidal(query, tier, limit)
                return AddonSearchResponse(tracks=t_rows)
            except Exception as exc:
                log.warning("Tidal failover search failed: %s", exc)
                return AddonSearchResponse(tracks=[])

        return AddonSearchResponse(tracks=[])

    async def _search_qobuz(self, query: str, tier: str, limit: int) -> list[AddonTrack]:
        clients = self.active_qobuz_clients()
        last_err: Exception | None = None
        for _, _, client in clients:
            try:
                tracks = await client.search_tracks(query, limit)
                rows: list[AddonTrack] = []
                seen: set[str] = set()
                for t in tracks:
                    if t.id in seen:
                        continue
                    seen.add(t.id)
                    self.remember_track(
                        TrackMeta(
                            id=t.id,
                            provider="qobuz",
                            title=t.title,
                            artist=t.artist,
                            album=t.album,
                            duration=t.duration,
                            isrc=t.isrc,
                            artwork=t.artwork_url,
                        )
                    )
                    rows.append(self._map_qobuz_addon_track(t, tier))
                return rows
            except Exception as exc:
                last_err = exc
                continue
        if last_err is not None:
            raise last_err
        return []

    async def _search_qobuz_Safe(self, query: str, tier: str, limit: int) -> list[AddonTrack]:
        try:
            return await self._search_qobuz(query, tier, limit)
        except Exception:
            return []

    async def _search_tidal(self, query: str, tier: str, limit: int) -> list[AddonTrack]:
        clients = self.active_tidal_clients()
        last_err: Exception | None = None
        for _, _, client in clients:
            try:
                tracks = await client.search_tracks(query, max(limit * 2, 25))
                # Deduplicate identical tracks (same ISRC or title+artist+duration), keeping highest audio quality
                deduped: list[TidalTrack] = []
                seen_keys: dict[str, int] = {}
                for t in tracks:
                    dur_group = round((t.duration or 0) / 4)
                    key = (t.isrc or "").upper() or f"{_norm_text(t.title)}|{_norm_text(t.artist)}|{dur_group}"
                    if key in seen_keys:
                        existing_idx = seen_keys[key]
                        existing = deduped[existing_idx]
                        if t.hires_streamable and not existing.hires_streamable:
                            deduped[existing_idx] = t
                        continue
                    seen_keys[key] = len(deduped)
                    deduped.append(t)

                rows: list[AddonTrack] = []
                for t in deduped[:limit]:
                    tid = f"tidal:{t.id}"
                    self.remember_track(
                        TrackMeta(
                            id=tid,
                            provider="tidal",
                            title=t.title,
                            artist=t.artist,
                            album=t.album,
                            duration=t.duration,
                            isrc=t.isrc,
                            audio_modes=t.audio_modes,
                            artwork=t.artwork_url,
                        )
                    )
                    rows.append(self._map_tidal_addon_track(t, tier))
                return rows
            except Exception as exc:
                last_err = exc
                continue
        if last_err is not None:
            raise last_err
        return []

    async def _search_tidal_safe(self, query: str, tier: str, limit: int) -> list[AddonTrack]:
        try:
            return await self._search_tidal(query, tier, limit)
        except Exception:
            return []

    @classmethod
    def _is_atmos_track(cls, track: AddonTrack) -> bool:
        return bool(
            track.audioMode == "DOLBY_ATMOS"
            or "DOLBY_ATMOS" in (track.audioModes or [])
            or (track.format or "").lower() in ("eac3", "ec-3", "ac4", "ac-4")
            or (track.audioQuality or "").upper() == "DOLBY_ATMOS"
        )

    @classmethod
    def _track_quality_rank(cls, track: AddonTrack) -> int:
        if cls._is_atmos_track(track):
            return 100
        q = (track.audioQuality or "").upper()
        fmt = (track.format or "").lower()
        if "HI_RES" in q or "24" in q:
            return 30
        if "LOSSLESS" in q or fmt == "flac":
            return 20
        if "HIGH" in q or "320" in q or fmt in ("mp3", "m4a", "aac"):
            return 10
        return 0

    @classmethod
    def _merge_tracks_smart(
        cls,
        list_a: list[AddonTrack],
        list_b: list[AddonTrack],
        limit: int,
    ) -> list[AddonTrack]:
        """Merge tracks dynamically:
        1. Keep best Stereo master (Hi-Res 24-bit > Lossless 16-bit > MP3).
        2. Keep Dolby Atmos as companion spatial audio offering.
        3. Eliminate lower-quality stereo duplicates.
        """
        groups: dict[str, dict[str, Any]] = {}
        order: list[tuple[str, str]] = []

        def _get_group_key(row: AddonTrack) -> str:
            if row.isrc and len(row.isrc.strip()) >= 8:
                return f"isrc:{row.isrc.strip().upper()}"
            base_t, _ = _title_parts(row.title)
            art = _primary_artist(row.artist)
            dur = round((row.duration or 0) / 4)
            return f"{base_t}|{art}|{dur}"

        max_len = max(len(list_a), len(list_b))
        candidates: list[AddonTrack] = []
        for i in range(max_len):
            if i < len(list_a):
                candidates.append(list_a[i])
            if i < len(list_b):
                candidates.append(list_b[i])

        for row in candidates:
            gkey = _get_group_key(row)
            if gkey not in groups:
                groups[gkey] = {}

            if cls._is_atmos_track(row):
                if "atmos" not in groups[gkey]:
                    groups[gkey]["atmos"] = row
                    order.append((gkey, "atmos"))
            else:
                score = cls._track_quality_rank(row)
                if "stereo" not in groups[gkey]:
                    groups[gkey]["stereo"] = (score, row)
                    order.append((gkey, "stereo"))
                else:
                    existing_score, _ = groups[gkey]["stereo"]
                    if score > existing_score:
                        groups[gkey]["stereo"] = (score, row)

        out: list[AddonTrack] = []
        for gkey, slot in order:
            if slot == "atmos" and "atmos" in groups[gkey]:
                out.append(groups[gkey]["atmos"])
            elif slot == "stereo" and "stereo" in groups[gkey]:
                _, trk = groups[gkey]["stereo"]
                out.append(trk)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _map_qobuz_addon_track(track: QobuzTrack, tier: str) -> AddonTrack:
        labels = search_labels(hires_streamable=track.hires_streamable, requested_quality=tier)
        return AddonTrack(
            id=track.id,
            title=track.title,
            artist=track.artist,
            album=track.album,
            duration=track.duration,
            artworkURL=track.artwork_url,
            albumArtworkURL=track.artwork_url,
            format=labels.fmt,
            audioQuality=labels.audio_quality,
            audioMode="STEREO",
            audioModes=["STEREO"],
            isrc=track.isrc,
        )

    @staticmethod
    def _map_tidal_addon_track(track: TidalTrack, tier: str) -> AddonTrack:
        modes = list(track.audio_modes) if track.audio_modes else ["STEREO"]
        is_atmos = "DOLBY_ATMOS" in modes
        labels = search_labels(
            hires_streamable=track.hires_streamable,
            requested_quality=tier,
            is_atmos=is_atmos,
        )
        fmt = "m4a" if labels.fmt == "mp3" else labels.fmt
        return AddonTrack(
            id=f"tidal:{track.id}",
            title=track.title,
            artist=track.artist,
            album=track.album,
            duration=track.duration,
            artworkURL=track.artwork_url,
            albumArtworkURL=track.artwork_url,
            format=fmt,
            audioQuality=labels.audio_quality,
            # audioMode is the primary mix, so it must not read STEREO on a row
            # that also offers an immersive one (BitChord scores rows on it).
            audioMode="DOLBY_ATMOS" if is_atmos else modes[0],
            audioModes=modes,
            isrc=track.isrc,
        )

    # ── Unified Stream Resolution with Cross-Provider Failover ───────────

    async def resolve_stream(
        self,
        track_id: str,
        tier: str,
        *,
        dash_base_url: str = "",
        atmos: str = "auto",
    ) -> ResolvedStream:
        """Resolve one playable stream, honouring the immersive-audio hint.

        `atmos` is the spec's `?atmos=` value, normalised: `auto` prefers an
        immersive mix when the catalogue has one, `off` must never substitute
        one (the listener picked the stereo row), `strict` treats a missing
        immersive mix as a miss rather than answering with stereo.
        """
        is_tidal_id = track_id.startswith("tidal:")
        clean_qobuz_id = track_id.removeprefix("qobuz:")

        if is_tidal_id:
            try:
                stream, is_atmos = await self._resolve_tidal_direct(
                    track_id, tier, dash_base_url=dash_base_url, atmos=atmos
                )
                return ResolvedStream(stream=stream, provider="tidal", is_atmos=is_atmos)
            except Exception as tidal_exc:
                if not (self.auto_failover and self.has_healthy_qobuz):
                    raise
                reason = (
                    "Track not on Tidal"
                    if isinstance(tidal_exc, TidalNotFound)
                    else "Tidal error/rate-limit"
                )
                log.warning(
                    "Tidal stream failed for %s (%s); attempting Qobuz failover",
                    track_id,
                    tidal_exc,
                )
                fallback = await self._failover_to_qobuz(track_id, tier)
                if fallback is not None:
                    # Qobuz carries no immersive mixes: strict mode is a miss.
                    if atmos == "strict":
                        raise tidal_exc
                    return ResolvedStream(
                        stream=fallback,
                        provider="qobuz",
                        failover=True,
                        failover_reason=reason,
                    )
                raise tidal_exc

        # Qobuz ID path
        try:
            # Fast failover if Qobuz is offline/blocked
            if not self.has_healthy_qobuz and self.has_healthy_tidal:
                fallback = await self._failover_to_tidal(
                    clean_qobuz_id, tier, dash_base_url=dash_base_url
                )
                if fallback is not None:
                    is_atmos = getattr(fallback, "isAtmos", False)
                    return ResolvedStream(
                        stream=fallback,
                        provider="tidal",
                        failover=True,
                        failover_reason="Qobuz accounts offline",
                        is_atmos=is_atmos,
                    )
            stream = await self._resolve_qobuz_direct(clean_qobuz_id, tier)
            if atmos == "strict":
                # Asked only for an immersive mix; Qobuz has none.
                raise QobuzNotFound("No immersive mix for this track")
            return ResolvedStream(stream=stream, provider="qobuz")
        except Exception as qobuz_exc:
            if not (self.auto_failover and self.has_healthy_tidal):
                raise
            # These strings surface on the public status page, so they say what
            # happened to the request rather than naming the exception: "not in
            # the catalogue, Tidal served it" is routing, not a fault.
            reason = (
                "Qobuz session expired · Tidal served it"
                if (
                    "unauthenticated" in str(qobuz_exc).lower()
                    or not getattr(self.primary_qobuz, "is_healthy", False)
                )
                else (
                    "Not in Qobuz catalogue · Tidal served it"
                    if isinstance(qobuz_exc, QobuzNotFound)
                    else "Qobuz error/rate-limit"
                )
            )
            log.warning(
                "Qobuz stream failed for %s (%s); attempting Tidal failover",
                clean_qobuz_id,
                qobuz_exc,
            )
            fallback = await self._failover_to_tidal(
                clean_qobuz_id, tier, dash_base_url=dash_base_url, atmos=atmos
            )
            if fallback is not None:
                is_atmos = getattr(fallback, "isAtmos", False)
                return ResolvedStream(
                    stream=fallback,
                    provider="tidal",
                    failover=True,
                    failover_reason=reason,
                    is_atmos=is_atmos,
                )
            raise qobuz_exc

    @staticmethod
    def _order_by_capability(
        clients: list[tuple[str, str, Any]], low_res: dict[str, float]
    ) -> list[tuple[str, str, Any]]:
        """Put accounts that recently delivered Hi-Res ahead of the rest.

        The pool order is round-robin for fairness, but probing is only useful
        when the account can actually serve the requested rendition.
        """
        now = time.monotonic()
        capable = [c for c in clients if low_res.get(c[0], 0.0) <= now]
        limited = [c for c in clients if low_res.get(c[0], 0.0) > now]
        return capable + limited

    @staticmethod
    def _note_capability(
        low_res: dict[str, float], account_id: str, depth: int, target: int
    ) -> None:
        """Remember for half an hour that this account is below the rung."""
        if target and depth and depth < target:
            low_res[account_id] = time.monotonic() + 1800.0
        elif target and depth >= target:
            low_res.pop(account_id, None)

    async def _resolve_qobuz_direct(self, track_id: str, tier: str) -> AddonStream:
        """Resolve from the account pool, preferring the best rendition.

        Accounts are not equal: a subscription or a region can answer format 27
        with 16-bit. Returning the first account that answers at all therefore
        handed listeners CD quality while another account in the same pool held
        the 24-bit master. So an answer below the requested rung is kept as a
        fallback and the next account is asked; the first answer that meets the
        rung wins immediately.
        """
        clients = self.active_qobuz_clients()
        if not clients:
            raise QobuzNotFound("No active Qobuz accounts configured")
        target = _target_bit_depth(tier)
        probe_count = max(1, int(getattr(self.settings, "resolve_probe_accounts", 3)))
        probe = self._order_by_capability(clients, self._qobuz_lores)[:probe_count]
        best: tuple[AddonStream, int] | None = None
        last_err: Exception | None = None
        for account_id, _, client in probe:
            try:
                resolved = await client.resolve(track_id, tier)
            except QobuzNotFound as exc:
                # The catalogue does not hold this track at all; another account
                # would give the same answer.
                last_err = exc
                break
            except Exception as exc:
                last_err = exc
                continue
            meta = stream_meta(
                resolved.format_id,
                bit_depth=resolved.bit_depth,
                sampling_rate_khz=resolved.sampling_rate_khz,
                mime_type=resolved.mime_type,
            )
            stream = AddonStream(
                url=resolved.url,
                **meta.as_addon_fields(),
                expiresAt=_stream_expiry(),
            )
            depth = int(resolved.bit_depth or 0)
            self._note_capability(self._qobuz_lores, account_id, depth, target)
            if best is None or depth > best[1]:
                best = (stream, depth)
            if depth >= target:
                return stream
        if best is not None:
            return best[0]
        if last_err is not None:
            raise last_err
        raise QobuzNotFound("Track not available on Qobuz")

    async def _resolve_tidal_direct(
        self, track_id: str, tier: str, *, dash_base_url: str = "", atmos: str = "auto"
    ) -> tuple[AddonStream, bool]:
        clients = self.active_tidal_clients()
        if not clients:
            raise TidalNotFound("No active Tidal accounts configured")
        meta = self.get_track_meta(track_id)
        if meta is None:
            for _, _, client in clients:
                try:
                    t_track = await client.get_track(track_id)
                    if t_track:
                        meta = TrackMeta(
                            id=f"tidal:{t_track.id}",
                            provider="tidal",
                            title=t_track.title,
                            artist=t_track.artist,
                            album=t_track.album,
                            duration=t_track.duration,
                            isrc=t_track.isrc,
                            audio_modes=t_track.audio_modes,
                            artwork=t_track.artwork_url,
                        )
                        self.remember_track(meta)
                        break
                except Exception:
                    continue
        has_atmos_meta = bool(meta and "DOLBY_ATMOS" in getattr(meta, "audio_modes", ()))
        tier_wants_atmos = tier.lower() in (
            "atmos",
            "spatial",
            "dolby",
            "dolby_atmos",
            "dolby-atmos",
        )
        # `off` is a listener who chose the stereo row: never ask Tidal for the
        # immersive asset, so it cannot come back as one.
        try_atmos = atmos != "off" and (has_atmos_meta or tier_wants_atmos)
        # Tidal accounts differ too: a non-hi-res subscription answers
        # HI_RES_LOSSLESS with a 16-bit LOSSLESS rendition. Keep the best
        # answer seen and only stop early when it meets the requested rung.
        target = _target_bit_depth(tier)
        probe_count = max(1, int(getattr(self.settings, "resolve_probe_accounts", 3)))
        probe = self._order_by_capability(clients, self._tidal_lores)[:probe_count]
        best: tuple[int, AddonStream, bool] | None = None
        last_err: Exception | None = None
        for account_id, _, client in probe:
            try:
                try:
                    resolved = await client.resolve(
                        track_id,
                        tier,
                        audio_mode="DOLBY_ATMOS" if try_atmos else None,
                    )
                except TypeError:
                    resolved = await client.resolve(track_id, tier)
                is_atmos = bool(getattr(resolved, "is_atmos", False))
                if atmos == "off" and is_atmos:
                    # Tidal answered with the immersive asset even with the hint
                    # withheld. One retry, then skip this account: the listener
                    # picked the stereo row, so Atmos is a substitution.
                    try:
                        resolved = await client.resolve(track_id, tier, audio_mode=None)
                        is_atmos = bool(getattr(resolved, "is_atmos", False))
                    except Exception:
                        pass
                    if is_atmos:
                        continue
                if atmos == "strict" and not is_atmos:
                    continue
                stream = self._tidal_to_addon_stream(resolved, dash_base_url=dash_base_url)
                depth = int(getattr(resolved, "bit_depth", 0) or 0)
                self._note_capability(self._tidal_lores, account_id, depth, target)
                score = (depth, 1 if is_atmos else 0)
                if best is None or score > best[0]:
                    best = (score, stream, is_atmos)
                if is_atmos or depth >= target:
                    return stream, is_atmos
            except TidalNotFound as exc:
                last_err = exc
                break
            except Exception as exc:
                last_err = exc
                continue
        if best is not None:
            return best[1], best[2]
        if atmos == "strict":
            raise TidalNotFound("No immersive mix for this track")
        if last_err is not None:
            raise last_err
        raise TidalNotFound("Track not available on Tidal")

    def _tidal_to_addon_stream(
        self, resolved: TidalStream, *, dash_base_url: str = ""
    ) -> AddonStream:
        url = resolved.url
        manifest_type = resolved.manifest_type
        if manifest_type == "dash" and resolved.mpd_xml:
            mid = self.store_dash_manifest(resolved.mpd_xml)
            base = dash_base_url.rstrip("/")
            url = f"{base}/dash/{mid}.mpd"
        return AddonStream(
            url=url,
            format=resolved.codec,
            quality=resolved.quality_label,
            codec=resolved.codec,
            container=resolved.container,
            manifest=manifest_type,
            encrypted=False,
            sampleRate=resolved.sample_rate,
            bitDepth=resolved.bit_depth,
            bitrate=resolved.bitrate,
            isAtmos=resolved.is_atmos,
            audioMode="DOLBY_ATMOS" if resolved.is_atmos else "STEREO",
            expiresAt=_stream_expiry(),
        )

    def _strict_failover(self) -> tuple[bool, float]:
        return (
            bool(getattr(self.settings, "failover_strict_match", True)),
            float(
                getattr(self.settings, "failover_duration_tolerance_seconds", 5)
            ),
        )

    async def _failover_to_tidal(
        self,
        qobuz_track_id: str,
        tier: str,
        *,
        dash_base_url: str = "",
        atmos: str = "auto",
    ) -> AddonStream | None:
        if not self.has_healthy_tidal:
            return None
        meta = self.get_track_meta(qobuz_track_id)
        if meta is None:
            # The primary Qobuz client may be unconfigured. Look through every
            # active Qobuz account rather than assuming the .env client works.
            for _, label, qclient in self.active_qobuz_clients():
                try:
                    q_track = await qclient.get_track(qobuz_track_id)
                except Exception as exc:
                    log.debug(
                        "Qobuz metadata lookup failed on %s: %s", label, exc
                    )
                    continue
                if q_track is not None:
                    meta = TrackMeta(
                        id=q_track.id,
                        provider="qobuz",
                        title=q_track.title,
                        artist=q_track.artist,
                        album=q_track.album,
                        duration=q_track.duration,
                        isrc=q_track.isrc,
                    )
                    self.remember_track(meta)
                    break
        if meta is None:
            return None
        query = f"{meta.title} {meta.artist}".strip()
        if not query:
            return None
        strict, tolerance = self._strict_failover()
        for _, label, tclient in self.active_tidal_clients():
            try:
                candidates = await tclient.search_tracks(query, limit=10)
                matching: list[TidalTrack] = [
                    cand
                    for cand in candidates
                    if _tracks_match(
                        meta,
                        cand.title,
                        cand.artist,
                        cand.duration,
                        cand.isrc,
                        strict=strict,
                        duration_tolerance=tolerance,
                    )
                ]
                if not matching:
                    if candidates:
                        log.warning(
                            "Tidal failover found %d candidates for %r by %r on %s "
                            "but none matched safely; refusing to play a different song",
                            len(candidates),
                            meta.title,
                            meta.artist,
                            label,
                        )
                    continue
                matching.sort(
                    key=lambda c: (
                        1 if (meta.isrc and c.isrc and meta.isrc.upper() == c.isrc.upper()) else 0,
                        1 if c.hires_streamable else 0,
                        1 if c.audio_quality == "LOSSLESS" else 0,
                        -abs((meta.duration or 0) - (c.duration or 0)),
                    ),
                    reverse=True,
                )
                chosen = matching[0]
                is_atmos_cand = (
                    "DOLBY_ATMOS" in getattr(chosen, "audio_modes", ())
                ) or tier.lower() in ("atmos", "spatial", "dolby", "dolby_atmos", "dolby-atmos")
                if atmos == "off":
                    is_atmos_cand = False
                try:
                    resolved = await tclient.resolve(
                        chosen.id,
                        tier,
                        audio_mode="DOLBY_ATMOS" if is_atmos_cand else None,
                    )
                except TypeError:
                    resolved = await tclient.resolve(chosen.id, tier)
                if atmos == "strict" and not getattr(resolved, "is_atmos", False):
                    continue
                if atmos == "off" and getattr(resolved, "is_atmos", False):
                    continue
                return self._tidal_to_addon_stream(resolved, dash_base_url=dash_base_url)
            except Exception as exc:
                log.debug("Tidal failover candidate failed: %s", exc)
                continue
        return None

    async def _failover_to_qobuz(self, tidal_track_id: str, tier: str) -> AddonStream | None:
        if not self.has_healthy_qobuz:
            return None
        meta = self.get_track_meta(tidal_track_id)
        if meta is None:
            for _, label, tclient in self.active_tidal_clients():
                try:
                    t_track = await tclient.get_track(tidal_track_id)
                except Exception as exc:
                    log.debug(
                        "Tidal metadata lookup failed on %s: %s", label, exc
                    )
                    continue
                if t_track is not None:
                    meta = TrackMeta(
                        id=f"tidal:{t_track.id}",
                        provider="tidal",
                        title=t_track.title,
                        artist=t_track.artist,
                        album=t_track.album,
                        duration=t_track.duration,
                        isrc=t_track.isrc,
                    )
                    self.remember_track(meta)
                    break
        if meta is None:
            return None
        query = f"{meta.title} {meta.artist}".strip()
        if not query:
            return None
        strict, tolerance = self._strict_failover()
        for _, label, qclient in self.active_qobuz_clients():
            try:
                candidates = await qclient.search_tracks(query, limit=10)
                matching_q: list[QobuzTrack] = [
                    cand
                    for cand in candidates
                    if _tracks_match(
                        meta,
                        cand.title,
                        cand.artist,
                        cand.duration,
                        cand.isrc,
                        strict=strict,
                        duration_tolerance=tolerance,
                    )
                ]
                if not matching_q:
                    if candidates:
                        log.warning(
                            "Qobuz failover found %d candidates for %r by %r on %s "
                            "but none matched safely; refusing to play a different song",
                            len(candidates),
                            meta.title,
                            meta.artist,
                            label,
                        )
                    continue
                matching_q.sort(
                    key=lambda c: (
                        1 if (meta.isrc and c.isrc and meta.isrc.upper() == c.isrc.upper()) else 0,
                        1 if c.hires_streamable else 0,
                        -abs((meta.duration or 0) - (c.duration or 0)),
                    ),
                    reverse=True,
                )
                chosen = matching_q[0]
                resolved = await qclient.resolve(chosen.id, tier)
                smeta = stream_meta(
                    resolved.format_id,
                    bit_depth=resolved.bit_depth,
                    sampling_rate_khz=resolved.sampling_rate_khz,
                    mime_type=resolved.mime_type,
                )
                return AddonStream(url=resolved.url, **smeta.as_addon_fields())
            except Exception as exc:
                log.debug("Qobuz failover candidate failed: %s", exc)
                continue
        return None

    # ── Admin Operations (Local Admin API) ───────────────────────────────

    async def add_qobuz_account(
        self,
        *,
        label: str = "",
        user_auth_token: str = "",
        user_id: str = "",
        email: str = "",
        password: str = "",
        app_id: str = "",
        app_secret: str = "",
    ) -> dict[str, Any]:
        aid = f"qobuz-{secrets.token_hex(3)}"
        cleaned_token = user_auth_token.strip().strip("'\"").removeprefix("Bearer ").strip()
        cleaned_uid = user_id.strip().strip("'\"")

        # Auto-detect if user pasted a JSON snippet from browser DevTools localStorage
        if cleaned_token.startswith("{") and cleaned_token.endswith("}"):
            with contextlib.suppress(Exception):
                parsed = json.loads(cleaned_token)
                if isinstance(parsed, dict):
                    cleaned_token = str(
                        parsed.get("user_auth_token") or parsed.get("token") or cleaned_token
                    ).strip().strip("'\"")
                    if not cleaned_uid and (parsed.get("user_id") or parsed.get("id")):
                        cleaned_uid = str(parsed.get("user_id") or parsed.get("id")).strip()

        entry: dict[str, Any] = {
            "id": aid,
            "label": label.strip() or f"Qobuz ({cleaned_uid or email or aid})",
            "enabled": True,
            "user_auth_token": cleaned_token,
            "user_id": cleaned_uid,
            "email": email.strip(),
            "password": password,
            "app_id": app_id.strip() if app_id else "",
            "app_secret": app_secret.strip() if app_secret else "",
            "added_at": int(time.time()),
        }
        register_secrets(
            [
                user_auth_token,
                cleaned_token,
                password,
                entry["app_secret"],
            ]
        )
        client = self._build_qobuz_client(entry)
        await client.start()
        try:
            await asyncio.wait_for(client._check_health(force=True), timeout=25)
        except Exception as exc:
            log.warning("Initial Qobuz health check for %s raised: %s", aid, exc)

        snap = client.health_snapshot()
        entry["user_auth_token"] = client._token
        entry["user_id"] = client._user_id
        entry["subscription"] = snap.get("subscription")
        register_secrets([client._token])
        self.extra_qobuz[aid] = entry
        self.qobuz_clients[aid] = client
        self.save_state()
        return {
            "id": aid,
            "label": entry["label"],
            "enabled": True,
            "health": snap,
        }

    async def add_tidal_account(
        self,
        *,
        label: str = "",
        access_token: str,
        refresh_token: str = "",
        expires_in: int = 86400,
        user_id: str = "",
        country_code: str = "US",
    ) -> dict[str, Any]:
        aid = f"tidal-{secrets.token_hex(3)}"
        expires_at = time.time() + int(expires_in) if expires_in else 0.0
        entry: dict[str, Any] = {
            "id": aid,
            "label": label.strip() or f"Tidal ({user_id or country_code or aid})",
            "enabled": True,
            "access_token": access_token.strip(),
            "refresh_token": refresh_token.strip(),
            "expires_at": expires_at,
            "user_id": str(user_id or ""),
            "country_code": (country_code or "US").upper(),
            "added_at": int(time.time()),
        }
        register_secrets([access_token, refresh_token, entry["access_token"], entry["refresh_token"]])
        client = self._build_tidal_client(entry)
        await client.start()
        snap = await client.check_health()
        register_secrets([client.access_token, client.refresh_token])
        if client.user_id and not label.strip():
            client.label = f"Tidal #{client.user_id} ({client.country_code})"
            entry["label"] = client.label
        self.tidal_entries[aid] = entry
        self.tidal_clients[aid] = client
        self.save_state()
        return {
            "id": aid,
            "label": client.label,
            "enabled": True,
            "health": snap,
        }

    async def start_tidal_device_flow(self, label: str = "") -> dict[str, Any]:
        if self._device_poll_task is not None and not self._device_poll_task.done():
            self._device_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._device_poll_task

        pending = await start_tidal_device_login(
            proxy_url=self.settings.tidal_proxy_url,
            transport=self._transport,
        )
        self.pending_device_auth = pending
        self.device_auth_status = "pending"
        self.device_auth_last_account = None
        self._device_poll_task = asyncio.create_task(
            self._poll_device_flow_loop(pending, label=label)
        )
        return {
            "user_code": pending.user_code,
            "verification_uri": pending.verification_uri,
            "verification_uri_complete": pending.verification_uri_complete,
            "expires_in": pending.expires_in,
            "status": "pending",
        }

    async def _poll_device_flow_loop(
        self, pending: DeviceAuthPending, *, label: str = ""
    ) -> None:
        deadline = time.monotonic() + pending.expires_in
        while time.monotonic() < deadline:
            await asyncio.sleep(pending.interval)
            try:
                token_data = await poll_tidal_device_token(
                    pending,
                    proxy_url=self.settings.tidal_proxy_url,
                    transport=self._transport,
                )
            except Exception as exc:
                log.warning("Tidal device flow aborted: %s", exc)
                self.device_auth_status = f"failed: {exc}"
                self.pending_device_auth = None
                return

            if token_data is not None:
                register_secrets(
                    [
                        str(token_data.get("access_token") or ""),
                        str(token_data.get("refresh_token") or ""),
                    ]
                )
                user_obj = token_data.get("user") or {}
                uid = str(user_obj.get("userId") or "")
                cc = str(user_obj.get("countryCode") or "US")
                acc_info = await self.add_tidal_account(
                    label=label or f"Tidal ({uid or cc})",
                    access_token=str(token_data.get("access_token") or ""),
                    refresh_token=str(token_data.get("refresh_token") or ""),
                    expires_in=int(token_data.get("expires_in") or 86400),
                    user_id=uid,
                    country_code=cc,
                )
                self.device_auth_status = "completed"
                self.device_auth_last_account = acc_info["id"]
                self.pending_device_auth = None
                log.info("Linked new Tidal account %s via device code flow", acc_info["id"])
                return

        self.device_auth_status = "expired"
        self.pending_device_auth = None

    def toggle_account(self, account_id: str) -> dict[str, Any] | None:
        if account_id == "qobuz-primary":
            self.primary_qobuz_enabled = not self.primary_qobuz_enabled
            self.save_state()
            return {"id": "qobuz-primary", "enabled": self.primary_qobuz_enabled}
        if account_id in self.extra_qobuz:
            cur = bool(self.extra_qobuz[account_id].get("enabled", True))
            self.extra_qobuz[account_id]["enabled"] = not cur
            self.save_state()
            return {"id": account_id, "enabled": not cur}
        if account_id in self.tidal_entries:
            cur = bool(self.tidal_entries[account_id].get("enabled", True))
            self.tidal_entries[account_id]["enabled"] = not cur
            self.save_state()
            return {"id": account_id, "enabled": not cur}
        return None

    async def delete_account(self, account_id: str) -> bool:
        if account_id == "qobuz-primary":
            self.primary_qobuz_enabled = False
            self.save_state()
            return True
        if account_id in self.extra_qobuz:
            self.extra_qobuz.pop(account_id, None)
            client = self.qobuz_clients.pop(account_id, None)
            if client is not None:
                await client.stop()
            self.save_state()
            return True
        if account_id in self.tidal_entries:
            self.tidal_entries.pop(account_id, None)
            client = self.tidal_clients.pop(account_id, None)
            if client is not None:
                await client.stop()
            self.save_state()
            return True
        return False

    async def test_account(self, account_id: str) -> dict[str, Any] | None:
        if account_id == "qobuz-primary":
            await self.primary_qobuz._check_health(force=True)
            return self.primary_qobuz.health_snapshot()
        if account_id in self.qobuz_clients:
            qclient = self.qobuz_clients[account_id]
            await qclient._check_health(force=True)
            self.save_state()
            return qclient.health_snapshot()
        if account_id in self.tidal_clients:
            tclient = self.tidal_clients[account_id]
            snap = await tclient.check_health()
            self.save_state()
            return snap
        return None

    def set_routing(
        self, *, primary_provider: str | None = None, auto_failover: bool | None = None
    ) -> dict[str, Any]:
        if primary_provider is not None:
            if not isinstance(primary_provider, str):
                raise ValueError("primary_provider must be a string")
            normalized = primary_provider.strip().lower()
            if normalized not in ("qobuz", "tidal", "both"):
                raise ValueError("primary_provider must be 'qobuz', 'tidal', or 'both'")
            self.primary_provider = normalized
        if auto_failover is not None:
            self.auto_failover = bool(auto_failover)
        self.save_state()
        return {
            "primary_provider": self.primary_provider,
            "auto_failover": self.auto_failover,
        }

    # ── Status Snapshots (Public vs Admin) ───────────────────────────────

    def qobuz_accounts_summary(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if (
            self.primary_qobuz_enabled
            and self.settings.qobuz_user_auth_token.strip()
            and self.primary_qobuz.settings.qobuz_configured
        ):
            snap = self.primary_qobuz.health_snapshot()
            out.append(
                {
                    "id": "qobuz-primary",
                    "label": self.primary_qobuz_label,
                    "enabled": self.primary_qobuz_enabled,
                    "configured": snap["configured"],
                    "token_ok": snap["token_ok"],
                    "geo_ok": snap["geo_ok"],
                    "health_latency_ms": snap.get("health_latency_ms"),
                    "subscription": snap["subscription"],
                    "error": snap["token_error"] or snap["geo_error"],
                }
            )
        for aid, entry in self.extra_qobuz.items():
            client = self.qobuz_clients.get(aid)
            snap = client.health_snapshot() if client else {}
            out.append(
                {
                    "id": aid,
                    "label": entry.get("label") or aid,
                    "enabled": bool(entry.get("enabled", True)),
                    "configured": bool(snap.get("configured")),
                    "token_ok": snap.get("token_ok"),
                    "geo_ok": snap.get("geo_ok"),
                    "health_latency_ms": snap.get("health_latency_ms"),
                    "subscription": snap.get("subscription") or entry.get("subscription"),
                    "error": snap.get("token_error") or snap.get("geo_error"),
                }
            )
        return out

    def tidal_accounts_summary(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for aid, entry in self.tidal_entries.items():
            client = self.tidal_clients.get(aid)
            snap = client.health_snapshot() if client else {}
            out.append(
                {
                    "id": aid,
                    "label": entry.get("label") or aid,
                    "enabled": bool(entry.get("enabled", True)),
                    "configured": bool(snap.get("configured")),
                    "token_ok": snap.get("token_ok"),
                    "subscription": snap.get("subscription"),
                    "country_code": snap.get("country_code"),
                    "requests_served": snap.get("requests_served", 0),
                    "health_latency_ms": snap.get("health_latency_ms"),
                    "avg_latency_ms": snap.get("avg_latency_ms", 0.0),
                    "error": snap.get("token_error"),
                }
            )
        return out

    def public_services_status(self) -> dict[str, Any]:
        q_accs = self.qobuz_accounts_summary()
        q_active = [a for a in q_accs if a["enabled"] and a["configured"]]
        q_healthy = [
            a
            for a in q_active
            if a["token_ok"] is not False and a["geo_ok"] is not False
        ]
        q_any_configured = any(a["configured"] for a in q_accs)
        if q_healthy:
            q_status = "online"
        elif q_active:
            q_status = "degraded"
        elif q_any_configured:
            q_status = "offline"
        else:
            q_status = (
                "unconfigured"
                if self.primary_provider in ("qobuz", "both")
                else "standby"
            )

        t_accs = self.tidal_accounts_summary()
        t_active = [a for a in t_accs if a["enabled"] and a["configured"]]
        t_healthy = [a for a in t_active if a["token_ok"] is not False]
        t_any_configured = any(a["configured"] for a in t_accs)
        if t_healthy:
            t_status = "online"
        elif t_active:
            t_status = "degraded"
        elif t_any_configured:
            t_status = "offline"
        else:
            t_status = (
                "unconfigured"
                if self.primary_provider in ("tidal", "both")
                else "standby"
            )

        def _health_latency(accounts: list[dict[str, Any]]) -> float | None:
            values = [
                float(a["health_latency_ms"])
                for a in accounts
                if a.get("enabled") and a.get("health_latency_ms") is not None
            ]
            return round(sum(values) / len(values), 1) if values else None

        q_online = bool(q_healthy)
        t_online = bool(t_healthy)
        if self.primary_provider == "tidal":
            primary_online = t_online
            t_role = "Active Primary" if t_online else "Primary Unavailable"
            q_role = (
                "Failover Backup"
                if q_online
                else ("Standby" if q_status in ("standby", "unconfigured") else "Backup Unavailable")
            )
        elif self.primary_provider == "qobuz":
            primary_online = q_online
            q_role = "Active Primary" if q_online else "Primary Unavailable"
            t_role = (
                "Failover Backup"
                if t_online
                else ("Standby" if t_status in ("standby", "unconfigured") else "Backup Unavailable")
            )
        else:
            primary_online = q_online and t_online
            q_role = "Active (Dual Engine)" if q_online else "Dual Engine Degraded"
            t_role = "Active (Dual Engine)" if t_online else "Dual Engine Degraded"

        any_online = q_online or t_online
        failover_active = bool(any_online and not primary_online)
        q_required = self.primary_provider in ("qobuz", "both")
        t_required = self.primary_provider in ("tidal", "both")
        required_degraded = (q_required and q_status == "degraded") or (
            t_required and t_status == "degraded"
        )
        if not any_online:
            overall = "outage"
        elif primary_online and not required_degraded:
            overall = "operational"
        else:
            overall = "degraded"

        reasons: list[str] = []
        if not primary_online:
            reasons.append("primary_down")
        if failover_active:
            reasons.append("failover_active")
        if q_status in ("standby", "unconfigured"):
            reasons.append("qobuz_unconfigured")
        if t_status in ("standby", "unconfigured"):
            reasons.append("tidal_unconfigured")
        if (q_required and q_status == "degraded") or (
            t_required and t_status == "degraded"
        ):
            reasons.append("provider_degraded")
        if not any_online:
            reasons.append("no_provider_online")

        q_observed = self.stats.observed_quality.get("qobuz")
        t_observed = self.stats.observed_quality.get("tidal")
        q_observed_label = (
            str(q_observed.get("quality_label") or "") if q_observed else ""
        )
        t_observed_label = (
            str(t_observed.get("quality_label") or "") if t_observed else ""
        )
        q_claimed_tier = q_healthy[0].get("subscription") if q_healthy else None
        t_claimed_tier = t_healthy[0].get("subscription") if t_healthy else None
        q_health = _health_latency(q_healthy)
        t_health = _health_latency(t_healthy)
        q_egress = "proxy" if self.settings.qobuz_proxy_url.strip() else "direct"
        t_egress = "proxy" if self.settings.tidal_proxy_url.strip() else "direct"

        return {
            "status": overall,
            "status_reasons": reasons,
            "primary_provider": self.primary_provider,
            "primary_online": primary_online,
            "failover_active": failover_active,
            "last_failover_at": self.stats.last_failover_at or None,
            "auto_failover": self.auto_failover,
            "qobuz": {
                "status": q_status,
                "online": q_online,
                "configured": bool(q_active),
                "role": q_role,
                "active_nodes": len(q_healthy),
                "total_nodes": len(q_accs),
                "health_latency_ms": q_health,
                "egress": q_egress,
                "observed_max_quality": q_observed,
                "claimed_tier": q_claimed_tier,
                "max_quality_claim": q_observed_label or None,
                "streams_served": self.stats.qobuz_streams_served,
                # Deprecated aliases kept for one release.
                "max_quality": q_observed_label or "unknown",
                "tier": q_claimed_tier or "unknown",
                "network_path": q_egress,
            },
            "tidal": {
                "status": t_status,
                "online": t_online,
                "configured": bool(t_active),
                "role": t_role,
                "active_nodes": len(t_healthy),
                "total_nodes": len(t_accs),
                "health_latency_ms": t_health,
                "egress": t_egress,
                "observed_max_quality": t_observed,
                "claimed_tier": t_claimed_tier,
                "max_quality_claim": t_observed_label or None,
                "streams_served": self.stats.tidal_streams_served,
                # Deprecated aliases kept for one release.
                "max_quality": t_observed_label or "unknown",
                "tier": t_claimed_tier or "unknown",
                "avg_latency_ms": t_health,
                "network_path": t_egress,
            },
        }
