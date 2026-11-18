"""Tidal API client: OAuth2 device login, token refresh, search, and Hi-Res/Lossless stream resolution.

Like QobuzClient, this client never proxies audio bytes. For 16-bit/44.1 kHz
FLAC (BTS manifest) it hands the direct signed Tidal CDN URL to BitChord. For
24-bit/192 kHz Hi-Res FLAC (MPEG-DASH manifest) it either extracts a direct
BaseURL when single-file or caches the tiny (~2 KB) MPD XML so BitChord's
DashMediaSource streams segments directly from Tidal's CDN.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx

from .config import DEFAULT_USER_AGENT
from .ratelimit import TokenBucket

log = logging.getLogger("tidal")

TIDAL_AUTH_BASE = "https://auth.tidal.com/v1/oauth2"
TIDAL_API_BASE = "https://api.tidal.com/v1"

# Tidal app identifiers, with their secrets supplied by the deployment.
#
# The client ids are public app identifiers and are harmless on their own. The
# secrets are credentials: they are read from the environment only, so a public
# checkout never ships someone else's OAuth secret and a rotated pair is an
# environment change, not a code change. Missing secrets disable Tidal and the
# pool falls back to Qobuz alone.
def _env_secret(name: str) -> str:
    return (os.environ.get(name) or "").strip()


# Android Automotive app: Dolby Atmos (E-AC-3 JOC) and Hi-Res.
ATMOS_CLIENT_ID = "4N3n6Q1x95LL5K7p"
ATMOS_CLIENT_SECRET = _env_secret("TIDAL_ATMOS_CLIENT_SECRET")

# Mobile Atmos fallback app (client id only; no secret is used for its flow).
MOBILE_ATMOS_CLIENT_ID = "km8T1xS355y7dd3H"

# Fire TV app.
FIRE_TV_CLIENT_ID = "7m7Ap0JC9j1cOM3n"
FIRE_TV_CLIENT_SECRET = _env_secret("TIDAL_FIRE_TV_CLIENT_SECRET")

# Legacy Android TV app, kept for backward-compatible token refresh.
LEGACY_TIDAL_CLIENT_ID = "fX2JxdmntZWK0ixT"
LEGACY_TIDAL_CLIENT_SECRET = _env_secret("TIDAL_LEGACY_CLIENT_SECRET")

DEFAULT_TIDAL_CLIENT_ID = ATMOS_CLIENT_ID
DEFAULT_TIDAL_CLIENT_SECRET = ATMOS_CLIENT_SECRET

TIDAL_QUALITY_PLANS: dict[str, tuple[str, ...]] = {
    "atmos": ("LOSSLESS", "HIGH", "LOW", "HI_RES_LOSSLESS"),
    "max": ("HI_RES_LOSSLESS", "LOSSLESS", "HIGH"),
    "hires": ("HI_RES_LOSSLESS", "LOSSLESS", "HIGH"),
    "lossless": ("LOSSLESS", "HIGH"),
    "high": ("HIGH", "LOW"),
    "low": ("LOW",),
}


class TidalError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int = 502,
        code: str = "tidal_error",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retryable = retryable


class TidalAuthError(TidalError):
    def __init__(self, message: str, *, code: str = "tidal_auth"):
        super().__init__(message, status=503, code=code, retryable=False)


class TidalNotFound(TidalError):
    def __init__(self, message: str = "Not held by Tidal", *, code: str = "not_found"):
        super().__init__(message, status=404, code=code, retryable=False)


class TidalRateLimited(TidalError):
    def __init__(self, message: str, *, retry_after: int = 5):
        super().__init__(message, status=429, code="rate_limited", retryable=True)
        self.retry_after = max(1, int(retry_after))


class TidalUnavailable(TidalError):
    def __init__(self, message: str, *, code: str = "tidal_unavailable"):
        super().__init__(message, status=503, code=code, retryable=True)


@dataclass(frozen=True)
class TidalTrack:
    id: str  # Raw numeric ID string, e.g. "12345678"
    title: str
    artist: str
    album: str
    duration: int | None
    artwork_url: str | None
    isrc: str | None
    streamable: bool
    hires_streamable: bool
    audio_quality: str
    audio_modes: tuple[str, ...]


@dataclass(frozen=True)
class TidalStream:
    url: str
    manifest_type: str  # "none" or "dash"
    codec: str
    container: str
    quality_label: str
    bit_depth: int | None
    sample_rate: int | None
    bitrate: int | None
    mpd_xml: str | None = None
    is_atmos: bool = False


@dataclass
class DeviceAuthPending:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    created_at: float


def _cover_url(cover_uuid: str | None, size: int = 640) -> str | None:
    if not cover_uuid or not isinstance(cover_uuid, str):
        return None
    clean = cover_uuid.strip().replace("-", "/")
    if not clean:
        return None
    return f"https://resources.tidal.com/images/{clean}/{size}x{size}.jpg"


def map_tidal_track(item: dict[str, Any]) -> TidalTrack | None:
    try:
        raw_id = item.get("id")
        title = str(item.get("title") or "").strip()
        if raw_id is None or not title:
            return None
        version = str(item.get("version") or "").strip()
        if version and version.lower() not in title.lower():
            title = f"{title} ({version})"

        if item.get("allowStreaming") is False or item.get("streamReady") is False:
            return None

        artist_name = ""
        artist_obj = item.get("artist")
        if isinstance(artist_obj, dict) and artist_obj.get("name"):
            artist_name = str(artist_obj["name"]).strip()
        elif isinstance(item.get("artists"), list) and item["artists"]:
            names = [
                str(a.get("name")).strip()
                for a in item["artists"]
                if isinstance(a, dict) and a.get("name")
            ]
            artist_name = ", ".join(n for n in names if n)

        album_obj = item.get("album") or {}
        album_title = str(album_obj.get("title") or "").strip()
        artwork = _cover_url(album_obj.get("cover"))

        duration = item.get("duration")
        dur_int = int(duration) if isinstance(duration, (int, float)) and duration > 0 else None

        isrc = str(item.get("isrc")).strip() if item.get("isrc") else None
        audio_quality = str(item.get("audioQuality") or "LOSSLESS").upper()
        tags = ((item.get("mediaMetadata") or {}).get("tags")) or []
        hires = (
            "HIRES_LOSSLESS" in tags
            or "HI_RES_LOSSLESS" in tags
            or audio_quality in ("HI_RES_LOSSLESS", "HI_RES")
        )
        modes_raw = item.get("audioModes") or ["STEREO"]
        modes = tuple(str(m).upper() for m in modes_raw if m) or ("STEREO",)

        return TidalTrack(
            id=str(raw_id),
            title=title,
            artist=artist_name,
            album=album_title,
            duration=dur_int,
            artwork_url=artwork,
            isrc=isrc,
            streamable=True,
            hires_streamable=hires,
            audio_quality=audio_quality,
            audio_modes=modes,
        )
    except Exception:
        return None


def _parse_mpd_metadata(mpd_xml: str) -> tuple[str | None, int | None, str | None]:
    """Extract (direct_base_url_if_single_file, sample_rate_hz, codec) from a DASH MPD."""
    sample_rate: int | None = None
    codec: str | None = None
    direct_url: str | None = None
    try:
        root = ET.fromstring(mpd_xml)
        has_segment_template = False
        base_urls: list[str] = []
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Representation":
                sr = elem.attrib.get("audioSamplingRate")
                if sr and sr.isdigit():
                    sample_rate = int(sr)
                c = elem.attrib.get("codecs")
                if c:
                    codec = c.lower()
            elif tag == "SegmentTemplate":
                has_segment_template = True
            elif tag == "BaseURL" and elem.text:
                text = elem.text.strip()
                if text.startswith(("http://", "https://")):
                    base_urls.append(text)
        if not has_segment_template and len(base_urls) == 1:
            direct_url = base_urls[0]
    except Exception:
        pass
    return direct_url, sample_rate, codec


class TidalClient:
    """Async Tidal API client for a single Tidal account."""

    def __init__(
        self,
        *,
        account_id: str = "tidal-1",
        label: str = "Tidal",
        access_token: str = "",
        refresh_token: str = "",
        expires_at: float = 0.0,
        user_id: str = "",
        country_code: str = "US",
        client_id: str = DEFAULT_TIDAL_CLIENT_ID,
        client_secret: str = DEFAULT_TIDAL_CLIENT_SECRET,
        proxy_url: str = "",
        timeout_seconds: float = 20.0,
        rate_limit_per_second: float = 8.0,
        rate_limit_burst: int = 16,
        max_concurrency: int = 12,
        on_token_Refresh: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.account_id = account_id
        self.label = label
        self.access_token = access_token.strip()
        self.refresh_token = refresh_token.strip()
        self.expires_at = float(expires_at or 0.0)
        self.user_id = str(user_id or "").strip()
        self.country_code = (country_code or "US").strip().upper()
        self.client_id = client_id.strip() or DEFAULT_TIDAL_CLIENT_ID
        self.client_secret = client_secret.strip() or DEFAULT_TIDAL_CLIENT_SECRET
        self.proxy_url = proxy_url.strip()
        self.timeout_seconds = timeout_seconds
        self._on_token_refresh = on_token_Refresh
        self._transport = transport

        self._http: httpx.AsyncClient | None = None
        self._started = False
        self._refresh_lock = asyncio.Lock()
        self._bucket = TokenBucket(rate_limit_per_second, rate_limit_burst)
        self._sem = asyncio.Semaphore(max_concurrency)

        self.subscription: str | None = None
        self.token_ok: bool | None = None
        self.token_error: str | None = None
        self.token_checked_at: float | None = None
        self.cooldown_until: float = 0.0
        self.requests_served: int = 0
        self.errors_count: int = 0
        # API request latency (search/resolve), not CDN download latency.
        self.avg_latency_ms: float = 0.0
        # Latency of the explicit health check only.
        self.health_latency_ms: float | None = None

    @property
    def configured(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    @property
    def is_healthy(self) -> bool:
        if not self.configured:
            return False
        if self.token_ok is False:
            return False
        if time.monotonic() < self.cooldown_until:
            return False
        return True

    async def start(self) -> None:
        if self._started:
            return
        kwargs: dict[str, Any] = {
            "headers": {
                "Accept": "application/json",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            "timeout": httpx.Timeout(self.timeout_seconds, connect=10.0),
            "follow_redirects": True,
            # Keep TLS connections open between songs; a cold handshake is the
            # slowest part of a first search.
            "limits": httpx.Limits(
                max_connections=40, max_keepalive_connections=10, keepalive_expiry=120.0
            ),
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        elif self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        self._http = httpx.AsyncClient(**kwargs)
        self._started = True

    async def warm(self) -> None:
        """Open (or refresh) a TLS connection so the next real request is fast."""
        if self._http is None:
            return
        try:
            await self._http.get(f"{TIDAL_API_BASE}/", timeout=8.0)
        except Exception:  # best effort only
            pass

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._started = False

    def mark_cooldown(self, seconds: float = 60.0, reason: str = "") -> None:
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + seconds)
        if reason:
            self.token_error = reason

    async def ensure_valid_token(self, force: bool = False) -> None:
        if not self.configured:
            raise TidalAuthError("Tidal account has no token configured")
        now = time.time()
        needs_refresh = (
            force
            or not self.access_token
            or self.expires_at <= 0
            or now >= self.expires_at - 120
        )
        if not needs_refresh:
            return
        if not self.refresh_token:
            if self.access_token and not force:
                return
            raise TidalAuthError("Tidal access token expired and no refresh_token is available")

        async with self._refresh_lock:
            now = time.time()
            if (
                not force
                and self.access_token
                and (self.expires_at <= 0 or now < self.expires_at - 120)
            ):
                return
            await self._do_refresh()

    async def _do_refresh(self) -> None:
        if self._http is None:
            await self.start()
        assert self._http is not None
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        try:
            resp = await self._http.post(f"{TIDAL_AUTH_BASE}/token", data=data)
            if resp.status_code != 200 and self.client_id != LEGACY_TIDAL_CLIENT_ID:
                fallback_data = dict(data)
                fallback_data["client_id"] = LEGACY_TIDAL_CLIENT_ID
                fallback_data["client_secret"] = LEGACY_TIDAL_CLIENT_SECRET
                fallback_resp = await self._http.post(f"{TIDAL_AUTH_BASE}/token", data=fallback_data)
                if fallback_resp.status_code == 200:
                    resp = fallback_resp
        except httpx.HTTPError as exc:
            raise TidalUnavailable(f"Failed to contact Tidal auth server: {exc}") from exc

        if resp.status_code != 200:
            self.token_ok = False
            self.token_error = f"Token refresh failed (HTTP {resp.status_code})"
            raise TidalAuthError(self.token_error)

        payload = resp.json()
        new_access = str(payload.get("access_token") or "").strip()
        if not new_access:
            self.token_ok = False
            self.token_error = "Tidal refresh response contained no access_token"
            raise TidalAuthError(self.token_error)

        self.access_token = new_access
        expires_in = int(payload.get("expires_in") or 86400)
        self.expires_at = time.time() + expires_in
        if payload.get("refresh_token"):
            self.refresh_token = str(payload["refresh_token"]).strip()
        user_obj = payload.get("user") or {}
        if user_obj.get("userId"):
            self.user_id = str(user_obj["userId"])
        if user_obj.get("countryCode"):
            self.country_code = str(user_obj["countryCode"]).upper()

        self.token_ok = True
        self.token_error = None
        self.token_checked_at = time.monotonic()
        if callable(self._on_token_refresh):
            try:
                res = self._on_token_refresh(self)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                log.warning("Callback on_token_refresh failed: %s", exc)

    async def check_health(self) -> dict[str, Any]:
        if not self.configured:
            self.token_ok = False
            self.token_error = "Not configured"
            self.health_latency_ms = None
            return self.health_snapshot()
        if self._http is None:
            await self.start()
        assert self._http is not None

        t0 = time.monotonic()

        def _finish() -> dict[str, Any]:
            self.health_latency_ms = round(
                (time.monotonic() - t0) * 1000.0, 1
            )
            return self.health_snapshot()

        try:
            await self.ensure_valid_token(force=(self.token_ok is False))
            resp = await self._http.get(
                f"{TIDAL_API_BASE}/sessions",
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            if resp.status_code == 401 and self.refresh_token:
                await self.ensure_valid_token(force=True)
                resp = await self._http.get(
                    f"{TIDAL_API_BASE}/sessions",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )
            if resp.status_code != 200:
                self.token_ok = False
                self.token_error = f"Session check returned HTTP {resp.status_code}"
                return _finish()

            sess = resp.json()
            if sess.get("countryCode"):
                self.country_code = str(sess["countryCode"]).upper()
            if sess.get("userId"):
                self.user_id = str(sess["userId"])

            # Optional subscription check
            if self.user_id:
                sub_resp = await self._http.get(
                    f"{TIDAL_API_BASE}/users/{self.user_id}/subscription",
                    params={"countryCode": self.country_code},
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )
                if sub_resp.status_code == 200:
                    sub_data = sub_resp.json()
                    sub_type = (sub_data.get("subscription") or {}).get("type") or sub_data.get("status")
                    if sub_type:
                        self.subscription = f"Tidal {sub_type}"
            if not self.subscription:
                self.subscription = f"Tidal ({self.country_code})"

            dt_ms = (time.monotonic() - t0) * 1000.0
            self.avg_latency_ms = round(dt_ms, 1) if self.avg_latency_ms == 0 else round(0.7 * self.avg_latency_ms + 0.3 * dt_ms, 1)
            self.token_ok = True
            self.token_error = None
            self.token_checked_at = time.monotonic()
        except Exception as exc:
            self.token_ok = False
            self.token_error = str(exc)
            self.token_checked_at = time.monotonic()
        return _finish()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._http is None:
            await self.start()
        assert self._http is not None

        await self.ensure_valid_token(force=False)
        await self._bucket.acquire()

        qparams = {"countryCode": self.country_code}
        if params:
            qparams.update(params)

        url = f"{TIDAL_API_BASE}/{path.lstrip('/')}"
        t0 = time.monotonic()
        async with self._sem:
            try:
                resp = await self._http.request(
                    method,
                    url,
                    params=qparams,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )
                if resp.status_code == 401 and self.refresh_token:
                    await self.ensure_valid_token(force=True)
                    resp = await self._http.request(
                        method,
                        url,
                        params=qparams,
                        headers={"Authorization": f"Bearer {self.access_token}"},
                    )
            except httpx.HTTPError as exc:
                self.errors_count += 1
                raise TidalUnavailable(f"Tidal request failed: {exc}") from exc

        dt_ms = (time.monotonic() - t0) * 1000.0
        self.avg_latency_ms = (
            round(dt_ms, 1)
            if self.avg_latency_ms == 0
            else round(0.8 * self.avg_latency_ms + 0.2 * dt_ms, 1)
        )

        if resp.status_code == 404:
            raise TidalNotFound("Track not found on Tidal")
        if resp.status_code == 429:
            self.errors_count += 1
            retry_after = int(resp.headers.get("Retry-After", "10") or "10")
            self.mark_cooldown(retry_after, "Rate limited (429)")
            raise TidalRateLimited("Tidal rate limit reached", retry_after=retry_after)
        if resp.status_code in (401, 403):
            try:
                body = resp.json()
            except Exception:
                body = {}
            sub_status = body.get("subStatus")
            msg = str(body.get("userMessage") or "")

            # 4005: "Asset is not ready for playback"
            # 4034: "No content matching client capabilities"
            # These are track-specific availability issues, NOT account auth revocation!
            if sub_status in (4005, 4034) or "not ready" in msg.lower() or "capabilities" in msg.lower():
                raise TidalNotFound(f"Tidal track unavailable: {msg}")

            self.errors_count += 1
            self.token_ok = False
            self.token_error = f"HTTP {resp.status_code} from Tidal"
            raise TidalAuthError(f"Tidal auth rejected (HTTP {resp.status_code})")
        if resp.status_code >= 500:
            self.errors_count += 1
            raise TidalUnavailable(f"Tidal upstream error (HTTP {resp.status_code})")
        if resp.status_code != 200:
            self.errors_count += 1
            raise TidalError(f"Unexpected Tidal response (HTTP {resp.status_code})", status=resp.status_code)

        self.requests_served += 1
        self.token_ok = True
        self.token_error = None
        return resp.json()

    async def search_tracks(self, query: str, limit: int = 25) -> list[TidalTrack]:
        data = await self._request(
            "GET",
            "search/tracks",
            params={"query": query, "limit": max(1, min(limit, 50)), "offset": 0},
        )
        items = data.get("items") or []
        results: list[TidalTrack] = []
        for item in items:
            if isinstance(item, dict):
                mapped = map_tidal_track(item)
                if mapped is not None:
                    results.append(mapped)
        return results

    async def get_track(self, track_id: str) -> TidalTrack | None:
        clean_id = track_id.removeprefix("tidal:").strip()
        try:
            data = await self._request("GET", f"tracks/{clean_id}")
            return map_tidal_track(data) if isinstance(data, dict) else None
        except Exception:
            return None

    async def resolve(
        self,
        track_id: str,
        quality_tier: str = "max",
        audio_mode: str | None = None,
    ) -> TidalStream:
        clean_id = track_id.removeprefix("tidal:").strip()
        plan = TIDAL_QUALITY_PLANS.get(quality_tier.lower(), TIDAL_QUALITY_PLANS["max"])

        try_atmos = (
            audio_mode == "DOLBY_ATMOS"
            or quality_tier.lower() in ("atmos", "spatial", "dolby", "dolby_atmos", "dolby-atmos")
        )

        last_err: Exception | None = None
        # Try playbackinfopostpaywall first (paid accounts), then playbackinfo
        endpoints = (
            f"tracks/{clean_id}/playbackinfopostpaywall",
            f"tracks/{clean_id}/playbackinfo",
        )

        # 1. Attempt Dolby Atmos first if requested
        if try_atmos:
            atmos_qualities = ("LOSSLESS", "HIGH", "LOW", "HI_RES_LOSSLESS")
            for ep_path in endpoints:
                for tidal_quality in atmos_qualities:
                    try:
                        data = await self._request(
                            "GET",
                            ep_path,
                            params={
                                "playbackmode": "STREAM",
                                "assetpresentation": "FULL",
                                "audioquality": tidal_quality,
                                "audioMode": "DOLBY_ATMOS",
                            },
                        )
                        return self._parse_playback_info(data, tidal_quality)
                    except TidalNotFound:
                        continue
                    except (TidalRateLimited, TidalAuthError) as exc:
                        last_err = exc
                        continue
                    except Exception as exc:
                        last_err = exc
                        continue

        # 2. Standard resolution path (or fallback if Atmos asset unavailable)
        for ep_path in endpoints:
            for tidal_quality in plan:
                try:
                    data = await self._request(
                        "GET",
                        ep_path,
                        params={
                            "playbackmode": "STREAM",
                            "assetpresentation": "FULL",
                            "audioquality": tidal_quality,
                        },
                    )
                    return self._parse_playback_info(data, tidal_quality)
                except TidalNotFound as exc:
                    last_err = exc
                    continue
                except (TidalRateLimited, TidalAuthError) as exc:
                    last_err = exc
                    continue
                except Exception as exc:
                    last_err = exc
                    continue

        if isinstance(last_err, TidalError):
            raise last_err
        raise TidalUnavailable(f"Could not resolve Tidal stream for {clean_id}")

    def _parse_playback_info(
        self, data: dict[str, Any], requested_quality: str
    ) -> TidalStream:
        manifest_b64 = str(data.get("manifest") or "").strip()
        mime_type = str(data.get("manifestMimeType") or "").strip().lower()
        audio_quality = str(data.get("audioQuality") or requested_quality).upper()
        audio_mode = str(data.get("audioMode") or "").upper()
        is_atmos = (audio_mode == "DOLBY_ATMOS") or (requested_quality.lower() == "atmos")
        bit_depth = data.get("bitDepth")
        sample_rate = data.get("sampleRate")

        bit_depth_int = int(bit_depth) if isinstance(bit_depth, (int, float)) and bit_depth > 0 else None
        sample_rate_int = int(sample_rate) if isinstance(sample_rate, (int, float)) and sample_rate > 0 else None

        if not manifest_b64:
            raise TidalUnavailable("Tidal playback info returned an empty manifest")

        raw_manifest = base64.b64decode(manifest_b64).decode("utf-8")

        if "vnd.tidal.bts" in mime_type:
            bts = json.loads(raw_manifest)
            encryption = str(bts.get("encryptionType") or "NONE").upper()
            if encryption != "NONE":
                raise TidalUnavailable("Tidal returned an encrypted stream")
            urls = bts.get("urls") or []
            if not urls or not isinstance(urls[0], str):
                raise TidalUnavailable("Tidal BTS manifest contained no stream URL")
            url = urls[0]
            codec_raw = str(bts.get("codecs") or "flac").lower()

            # Dolby Atmos bitstreams: E-AC-3 (eac3/ec-3) and AC-4
            if any(c in codec_raw for c in ("eac3", "ec-3", "ec3")):
                is_atmos = True
                codec = "eac3"
                container = "m4a"
                if sample_rate_int is None:
                    sample_rate_int = 48_000
                if bit_depth_int is None:
                    bit_depth_int = 16
                bitrate = int(bts.get("bitrate") or 768_000)
            elif any(c in codec_raw for c in ("ac-4", "ac4")):
                is_atmos = True
                codec = "ac4"
                container = "m4a"
                if sample_rate_int is None:
                    sample_rate_int = 48_000
                if bit_depth_int is None:
                    bit_depth_int = 16
                bitrate = int(bts.get("bitrate") or 448_000)
            elif "flac" in codec_raw:
                codec = "flac"
                container = "flac"
                if bit_depth_int is None:
                    bit_depth_int = 24 if "HI_RES" in audio_quality else 16
                if sample_rate_int is None:
                    sample_rate_int = 44_100
                bitrate = None
            else:
                codec = "aac"
                container = "m4a"
                if sample_rate_int is None:
                    sample_rate_int = 44_100
                if bit_depth_int is None:
                    bit_depth_int = 16
                bitrate = 320_000

            label = self._format_label(
                codec, bit_depth_int, sample_rate_int, audio_quality, is_atmos=is_atmos
            )
            return TidalStream(
                url=url,
                manifest_type="none",
                codec=codec,
                container=container,
                quality_label=label,
                bit_depth=bit_depth_int,
                sample_rate=sample_rate_int,
                bitrate=bitrate,
                mpd_xml=None,
                is_atmos=is_atmos,
            )

        if "dash+xml" in mime_type or raw_manifest.lstrip().startswith("<"):
            direct_url, mpd_sr, mpd_codec = _parse_mpd_metadata(raw_manifest)
            if sample_rate_int is None and mpd_sr:
                sample_rate_int = mpd_sr
            mpd_c = (mpd_codec or "").lower()

            # Dolby Atmos bitstreams in DASH representations
            if any(c in mpd_c for c in ("eac3", "ec-3", "ec3")):
                is_atmos = True
                codec = "eac3"
                container = "m4a"
                if sample_rate_int is None:
                    sample_rate_int = 48_000
                if bit_depth_int is None:
                    bit_depth_int = 16
                bitrate = 768_000
            elif any(c in mpd_c for c in ("ac-4", "ac4")):
                is_atmos = True
                codec = "ac4"
                container = "m4a"
                if sample_rate_int is None:
                    sample_rate_int = 48_000
                if bit_depth_int is None:
                    bit_depth_int = 16
                bitrate = 448_000
            elif "flac" in mpd_c or not mpd_c:
                codec = "flac"
                container = "flac"
                if bit_depth_int is None:
                    bit_depth_int = 24 if ("HI_RES" in audio_quality or (sample_rate_int or 0) > 48_000) else 16
                if sample_rate_int is None:
                    sample_rate_int = 96_000 if bit_depth_int >= 24 else 44_100
                bitrate = None
            else:
                codec = "aac"
                container = "m4a"
                if sample_rate_int is None:
                    sample_rate_int = 44_100
                if bit_depth_int is None:
                    bit_depth_int = 16
                bitrate = 320_000

            label = self._format_label(
                codec, bit_depth_int, sample_rate_int, audio_quality, is_atmos=is_atmos
            )
            if direct_url:
                return TidalStream(
                    url=direct_url,
                    manifest_type="none",
                    codec=codec,
                    container=container,
                    quality_label=label,
                    bit_depth=bit_depth_int,
                    sample_rate=sample_rate_int,
                    bitrate=bitrate,
                    mpd_xml=None,
                    is_atmos=is_atmos,
                )
            return TidalStream(
                url="",  # Filled with the addon's /dash/{id}.mpd capability URL
                manifest_type="dash",
                codec=codec,
                container=container,
                quality_label=label,
                bit_depth=bit_depth_int,
                sample_rate=sample_rate_int,
                bitrate=bitrate,
                mpd_xml=raw_manifest,
                is_atmos=is_atmos,
            )

        raise TidalUnavailable(f"Unsupported Tidal manifest type: {mime_type}")

    @staticmethod
    def _format_label(
        codec: str,
        bit_depth: int | None,
        sample_rate: int | None,
        audio_quality: str,
        is_atmos: bool = False,
    ) -> str:
        if is_atmos or codec in ("eac3", "ec-3", "ac4", "ac-4"):
            details: list[str] = []
            if codec in ("eac3", "ec-3"):
                details.append("E-AC-3 JOC")
            elif codec in ("ac4", "ac-4"):
                details.append("AC-4")
            if sample_rate:
                khz = sample_rate / 1000.0
                details.append(f"{khz:g} kHz")
            suffix = f" ({' · '.join(details)})" if details else ""
            return f"Dolby Atmos{suffix}"
        if codec != "flac":
            return "AAC 320 kbps"
        is_hires = (bit_depth or 0) >= 24 or (sample_rate or 0) > 48_000 or "HI_RES" in audio_quality
        prefix = "Hi-Res" if is_hires else "Lossless"
        if bit_depth and sample_rate:
            khz = sample_rate / 1000.0
            return f"{prefix} · {bit_depth}-bit / {khz:g} kHz"
        return prefix

    def health_snapshot(self) -> dict[str, Any]:
        checked_ago = (
            int(time.monotonic() - self.token_checked_at)
            if self.token_checked_at is not None
            else None
        )
        in_cooldown = time.monotonic() < self.cooldown_until
        return {
            "id": self.account_id,
            "label": self.label,
            "configured": self.configured,
            "token_ok": self.token_ok,
            "token_error": self.token_error,
            "token_checked_seconds_ago": checked_ago,
            "subscription": self.subscription or (f"Tidal ({self.country_code})" if self.configured else None),
            "country_code": self.country_code,
            "user_id": self.user_id or None,
            "in_cooldown": in_cooldown,
            "requests_served": self.requests_served,
            "errors_count": self.errors_count,
            "health_latency_ms": self.health_latency_ms,
            "avg_latency_ms": self.avg_latency_ms,
        }


async def start_tidal_device_login(
    client_id: str = DEFAULT_TIDAL_CLIENT_ID,
    proxy_url: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> DeviceAuthPending:
    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(15.0)}
    if transport is not None:
        kwargs["transport"] = transport
    elif proxy_url.strip():
        kwargs["proxy"] = proxy_url.strip()

    async with httpx.AsyncClient(**kwargs) as http:
        resp = await http.post(
            f"{TIDAL_AUTH_BASE}/device_authorization",
            data={"client_id": client_id, "scope": "r_usr w_usr w_sub"},
        )
        if resp.status_code != 200:
            raise TidalAuthError(f"Tidal device authorization failed (HTTP {resp.status_code})")
        body = resp.json()
        uri_complete = str(body.get("verificationUriComplete") or "")
        if uri_complete and not uri_complete.startswith("http"):
            uri_complete = f"https://{uri_complete}"
        uri_base = str(body.get("verificationUri") or "https://link.tidal.com")
        if not uri_base.startswith("http"):
            uri_base = f"https://{uri_base}"
        return DeviceAuthPending(
            device_code=str(body["deviceCode"]),
            user_code=str(body["userCode"]),
            verification_uri=uri_base,
            verification_uri_complete=uri_complete,
            expires_in=int(body.get("expiresIn") or 300),
            interval=max(2, int(body.get("interval") or 2)),
            created_at=time.monotonic(),
        )


async def poll_tidal_device_token(
    pending: DeviceAuthPending,
    client_id: str = DEFAULT_TIDAL_CLIENT_ID,
    client_secret: str = DEFAULT_TIDAL_CLIENT_SECRET,
    proxy_url: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any] | None:
    """Perform a single poll for device authorization completion.

    Returns token dict when user approves, None when still pending, or raises
    TidalAuthError when expired/denied.
    """
    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(15.0)}
    if transport is not None:
        kwargs["transport"] = transport
    elif proxy_url.strip():
        kwargs["proxy"] = proxy_url.strip()

    async with httpx.AsyncClient(**kwargs) as http:
        resp = await http.post(
            f"{TIDAL_AUTH_BASE}/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": pending.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "scope": "r_usr w_usr w_sub",
            },
        )
        if resp.status_code == 200:
            return resp.json()
        try:
            err = resp.json()
        except Exception:
            err = {}
        err_code = str(err.get("error") or "").lower()
        sub_status = int(err.get("sub_status") or 0)
        if err_code in ("authorization_pending", "slow_down") or resp.status_code == 400 and sub_status == 1002:
            return None
        if err_code in ("expired_token", "access_denied"):
            raise TidalAuthError(f"Tidal device code {err_code}")
        return None
