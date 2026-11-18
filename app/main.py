"""FastAPI application implementing BitChord's HTTP addon contract (Qobuz + Tidal).

Three endpoints are the whole BitChord addon contract:

* ``GET {prefix}/manifest.json`` — what this addon is and which quality values
  it understands.
* ``GET {prefix}/search?q=…&quality=…`` — catalogue rows.
* ``GET {prefix}/stream/{id}?quality=…`` — one playable URL plus honest
  codec/bit-depth/sample-rate metadata.

Additional operational features:
* Multi-provider pool (Qobuz + Tidal) with automatic cross-platform failover.
* Public live status & user monitor dashboard at ``/status`` (and ``/`` in browsers).
* Local-only ``/internal/admin/*`` control plane.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import mimetypes
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .artwork import ArtworkCache
from .cache import SingleFlightCache
from .config import Settings
from .dashboard import (
    build_overview_payload,
    build_plays_payload,
    build_public_stats_payload,
    build_support_payload,
    render_admin_vault_html,
    render_app_shell,
)
from .logging_setup import configure_logging, register_secrets
from .models import (
    AddonManifest,
    AddonQualityOption,
    AddonSearchResponse,
    AddonSetting,
)
from .plays import PlayLedger
from .pool import ProviderPool, ResolvedStream
from .qobuz import (
    QobuzClient,
    QobuzError,
    QobuzGeoBlocked,
    QobuzNotFound,
    QobuzRateLimited,
)
from .quality import QUALITY_OPTIONS, normalize_quality
from .ratelimit import SlidingWindowLimiter
from .stats import TrafficStats, quality_tier
from .telemetry import collect_host_telemetry, render_status_badge_svg
from .tidal import TidalError, TidalRateLimited
from .vendor.bitchord_addon_auth import BitChordAuth

log = logging.getLogger("addon")

# Accepts Qobuz numeric IDs ("5966783" or "qobuz:5966783") and Tidal IDs ("tidal:12345678").
_TRACK_ID_RE = re.compile(r"^(?:(?:qobuz|tidal):)?[0-9]{1,24}$")
_DASH_ID_RE = re.compile(r"^[a-f0-9]{16,64}$")
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]+$")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_MAX_QUERY_LENGTH = 200
_ADDON_ENDPOINTS = frozenset({"search", "stream", "dash", "manifest"})
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/plain", ".glsl")

_HTML_CSP = (
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'"
)


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings()
    prefix = settings.route_prefix

    search_cache: SingleFlightCache[tuple[str, str, int], AddonSearchResponse] = (
        SingleFlightCache(settings.cache_max_entries, settings.search_cache_ttl_seconds)
    )
    stream_cache: SingleFlightCache[tuple[str, str], ResolvedStream] = SingleFlightCache(
        settings.cache_max_entries, settings.stream_cache_ttl_seconds
    )
    limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)
    stats_file = Path(settings.data_dir).expanduser() / "stats.json"
    stats = TrafficStats(persist_path=stats_file)
    plays_file = Path(settings.data_dir).expanduser() / "plays.json"
    plays = PlayLedger(persist_path=plays_file)
    artwork = ArtworkCache(
        Path(settings.data_dir).expanduser() / "artwork",
        allowed_hosts=settings.artwork_hosts,
    )

    def _artwork_url(source: str) -> str | None:
        """Map a provider cover URL onto this host's cached copy."""
        key = artwork.register(source)
        return f"/art/{key}" if key else None
    verifier = (
        BitChordAuth(hosts=settings.hosts) if settings.addon_auth_enabled else None
    )
    manifest = _build_manifest(settings)
    qobuz = QobuzClient(settings, transport=transport)
    pool = ProviderPool(settings, qobuz, stats, transport=transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        startup_secrets = [
            settings.qobuz_app_secret,
            settings.qobuz_user_auth_token,
            settings.qobuz_password,
            settings.qobuz_password_md5,
            settings.addon_path_secret,
            settings.admin_path_secret,
            settings.admin_path_secret.strip().lstrip("/"),
            settings.stats_token,
            settings.telegram_bot_token,
        ]
        for proxy_raw in (settings.qobuz_proxy_url, settings.tidal_proxy_url):
            if proxy_raw:
                with contextlib.suppress(ValueError):
                    proxy_pw = urlparse(proxy_raw).password
                    if proxy_pw:
                        startup_secrets.append(proxy_pw)
        configure_logging(settings.log_level, startup_secrets)
        log.info(
            "Starting %s %s (path secret: %s, BitChord auth: %s, proxy: %s)",
            settings.addon_name,
            settings.addon_version,
            "yes" if settings.addon_path_secret else "no",
            "yes" if settings.addon_auth_enabled else "no",
            "yes" if settings.qobuz_proxy_url else "no",
        )
        if not settings.qobuz_configured:
            log.warning(
                "Qobuz credentials are incomplete. Set QOBUZ_APP_ID, "
                "QOBUZ_APP_SECRET and QOBUZ_USER_AUTH_TOKEN (+ QOBUZ_USER_ID), "
                "or QOBUZ_EMAIL + QOBUZ_PASSWORD."
            )
        stats_task: asyncio.Task[None] | None = None
        if settings.stats_log_interval_seconds > 0:
            stats_task = asyncio.create_task(
                _traffic_log_loop(stats, plays, settings.stats_log_interval_seconds)
            )
        await qobuz.start()
        if qobuz._token:
            register_secrets([qobuz._token])
        await pool.start()
        try:
            yield
        finally:
            if stats_task is not None:
                stats_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stats_task
            await pool.stop()
            await qobuz.stop()
            if settings.public_plays:
                await asyncio.to_thread(plays.save, True)
            log.info("Stopped %s", settings.addon_name)

    app = FastAPI(
        title=f"{settings.addon_name} addon",
        version=settings.addon_version,
        docs_url=f"{prefix}/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url=f"{prefix}/openapi.json" if settings.enable_docs else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.qobuz = qobuz
    app.state.pool = pool
    app.state.search_cache = search_cache
    app.state.stream_cache = stream_cache
    app.state.limiter = limiter
    app.state.stats = stats
    app.state.plays = plays
    app.state.started_at = time.monotonic()

    # ── Static assets for the human-facing pages ─────────────────────────
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    # ── Middleware: BitChord auth, then rate limiting ────────────────────
    manifest_path = f"{prefix}/manifest.json"
    search_path = f"{prefix}/search"
    stream_prefix = f"{prefix}/stream/"
    dash_prefix = f"{prefix}/dash/"
    stats_path = f"{prefix}/stats"

    if settings.admin_path_secret:
        admin_slug = settings.admin_path_secret.strip().lstrip("/")
        vault_prefix = f"/{admin_slug}"
    else:
        vault_slug = (
            hashlib.sha256(f"bitchord-vault:{settings.stats_token}".encode()).hexdigest()[:20]
            if settings.stats_token
            else ""
        )
        vault_prefix = f"/_vault_{vault_slug}" if vault_slug else ""

    def _secure_response(request: Request, response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path.startswith("/assets/"):
            if response.status_code < 400:
                response.headers.setdefault(
                    "Cache-Control",
                    "public, max-age=300, stale-while-revalidate=86400",
                )
            else:
                response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type.startswith("text/html"):
            response.headers.setdefault(
                "Cross-Origin-Opener-Policy", "same-origin"
            )
            response.headers.setdefault("Content-Security-Policy", _HTML_CSP)
        forwarded_proto = (
            (request.headers.get("x-forwarded-proto") or "")
            .split(",", 1)[0]
            .strip()
            .lower()
        )
        if request.url.scheme == "https" or forwarded_proto == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        data_path = path == search_path or path.startswith(stream_prefix)

        subject = _client_ip(request, settings)
        ep_name = _endpoint_name(path, prefix, vault_prefix)
        is_addon = ep_name in _ADDON_ENDPOINTS
        stats.begin(ep_name, subject, addon=is_addon)
        status = 500
        t0 = time.monotonic()
        try:
            auth_ok = verifier is None or not data_path
            if data_path and verifier is not None:
                result = await asyncio.to_thread(
                    verifier.verify_headers, request.headers
                )
                if not result.ok:
                    status = result.status
                    return _secure_response(
                        request,
                        Response(
                            content=result.body,
                            status_code=result.status,
                            headers=result.headers,
                        ),
                    )
                auth_ok = True
                claims = result.claims or {}
                if claims.get("sub"):
                    subject = str(claims["sub"])

            # Only addon clients count as listeners. A status-page visitor or
            # a failed auth attempt must never become a user.
            if is_addon and auth_ok and (
                data_path or path.startswith(dash_prefix)
            ):
                stats.record_client(subject, ep_name)

            if data_path and (path == search_path or path.startswith(stream_prefix)):
                allowed, retry_after = await limiter.hit(subject)
                if not allowed:
                    stats.client_rate_limited += 1
                    status = 429
                    return _secure_response(
                        request,
                        JSONResponse(
                            {
                                "error": "rate_limited",
                                "code": "rate_limited",
                                "message": "Too many addon requests; retry shortly.",
                            },
                            status_code=429,
                            headers={
                                "Retry-After": str(retry_after),
                                "Cache-Control": "no-store",
                            },
                        ),
                    )

            try:
                response = await call_next(request)
                status = response.status_code
                return _secure_response(request, response)
            except RuntimeError as exc:
                if "No response returned" in str(exc):
                    status = 499
                    return _error(
                        499,
                        "client_abort",
                        "Client closed connection before response completed",
                    )
                raise
        finally:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            stats.end(status, elapsed_ms=elapsed_ms, addon=is_addon)

    # ── Manifest ──────────────────────────────────────────────────────────
    @app.get(manifest_path)
    async def get_manifest() -> Response:
        return _json(manifest, cache="public, max-age=600")

    # ── Search ────────────────────────────────────────────────────────────
    @app.get(search_path)
    async def search(
        request: Request,
        q: str = "",
        quality: str = "",
        limit: int | None = None,
    ) -> Response:
        query = q.strip()
        if not query:
            return _json(
                AddonSearchResponse().model_dump(exclude_none=True),
                cache="private, max-age=600",
            )
        if len(query) > _MAX_QUERY_LENGTH:
            return _error(
                400,
                "invalid_query",
                f"Search queries are limited to {_MAX_QUERY_LENGTH} characters.",
            )

        tier = normalize_quality(quality)
        count = settings.search_limit
        if limit is not None:
            count = max(1, min(limit, 50))

        key = (f"{pool.primary_provider}:{query.casefold()}", tier, count)
        try:
            payload, cached = await search_cache.get_or_create(
                key, lambda: pool.search(query, tier, count)
            )
        except (QobuzGeoBlocked, QobuzError):
            raise
        except Exception as exc:
            log.warning("Search failed for query %r: %s", query, exc)
            payload = AddonSearchResponse(tracks=[])
            cached = False
        stats.search_cache(cached)
        if not cached:
            log.debug("search %r quality=%s -> %d rows", query, tier, len(payload.tracks))

        if payload.tracks:
            for i in range(len(payload.tracks) - 1):
                pool.record_track_sequence(payload.tracks[i].id, payload.tracks[i + 1].id)

        return _json(payload.model_dump(exclude_none=True), cache="private, max-age=300")

    # ── Stream ────────────────────────────────────────────────────────────
    @app.get(f"{prefix}/stream/{{track_id}}")
    async def stream(
        request: Request,
        track_id: str,
        quality: str = "",
    ) -> Response:
        if not _TRACK_ID_RE.fullmatch(track_id):
            raise QobuzNotFound("Not one of this addon's track ids")
        tier = normalize_quality(quality)
        dash_base = _public_display_url(settings, request).rstrip("/") + prefix
        resolved, cached = await stream_cache.get_or_create(
            (track_id, tier),
            lambda: pool.resolve_stream(track_id, tier, dash_base_url=dash_base),
        )
        stats.stream_cache(cached)
        # A fallback resolution is one event even if the same signed stream is
        # later served from cache. Served-stream counters, by contrast, fire
        # for every successful /stream response.
        if resolved.failover and not cached:
            stats.record_failover(
                resolved.failover_reason or "stream failover"
            )
        payload = resolved.stream
        stats.record_stream_served(
            resolved.provider,
            resolved.quality,
            failover=resolved.failover,
            failover_reason=resolved.failover_reason,
            bit_depth=resolved.bit_depth,
            sample_rate=resolved.sample_rate,
            is_atmos=getattr(resolved, "is_atmos", False),
            codec=getattr(payload, "codec", ""),
        )
        if settings.public_plays:
            # Aggregate and anonymous: the ledger keys clients by a per-process
            # hash and keeps only counts, so this cannot become a listen log.
            meta = pool.get_track_meta(track_id)
            plays.record(
                track_id=track_id,
                title=(meta.title if meta else "") or "",
                artist=(meta.artist if meta else "") or "",
                album=(meta.album if meta else "") or "",
                artwork=(meta.artwork if meta else None),
                tier=quality_tier(
                    payload.quality,
                    payload.bitDepth,
                    payload.sampleRate,
                    getattr(resolved, "is_atmos", False),
                    getattr(payload, "codec", ""),
                ),
                client=_client_ip(request, settings),
            )
            await asyncio.to_thread(plays.save, False)
        log.info(
            "stream: track=%s req_quality=%s -> quality=%s format=%s codec=%s (%s-bit/%s Hz) provider=%s failover=%s cached=%s",
            track_id,
            tier,
            payload.quality,
            payload.format,
            payload.codec,
            payload.bitDepth or "?",
            payload.sampleRate or "?",
            resolved.provider,
            resolved.failover,
            cached,
        )

        # Ahead-of-time pre-cache: pre-resolve next song in album / playlist sequence
        next_track_id = pool.get_next_track(track_id)
        if next_track_id:
            asyncio.create_task(
                _precache_stream(
                    pool, stream_cache, stats, next_track_id, tier, dash_base
                )
            )

        return _json(payload.model_dump(exclude_none=True), cache="no-store")

    # ── MPEG-DASH manifest endpoint for Tidal Hi-Res streams ──────────────
    @app.get(f"{prefix}/dash/{{manifest_id}}.mpd", include_in_schema=False)
    async def get_dash_manifest(manifest_id: str) -> Response:
        if not _DASH_ID_RE.fullmatch(manifest_id):
            return _error(404, "not_found", "Manifest not found")
        mpd_xml = pool.get_dash_manifest(manifest_id)
        if mpd_xml is None:
            return _error(404, "not_found", "Manifest expired or not found")
        return Response(
            content=mpd_xml,
            media_type="application/dash+xml",
            headers={"Cache-Control": "private, max-age=300"},
        )

    # ── Root & Public Monitor Dashboard ───────────────────────────────────
    @app.api_route(f"{prefix}/" if prefix else "/", methods=["GET", "HEAD"])
    async def root(request: Request) -> Response:
        accept = (request.headers.get("accept") or "").lower()
        user_agent = (request.headers.get("user-agent") or "").lower()
        is_crawler = any(
            bot in user_agent
            for bot in ("bot", "crawler", "spider", "google", "bing", "yahoo", "duckduck", "yandex", "slurp")
        )
        is_html = (
            "text/html" in accept
            or is_crawler
            or ("application/json" not in accept and "bitchord" not in user_agent)
        )
        if is_html and "bitchord" not in user_agent:
            if request.method == "HEAD":
                return Response(
                    status_code=200,
                    media_type="text/html",
                    headers={
                        "Cache-Control": "no-store, no-cache, must-revalidate",
                        "Vary": "Accept, User-Agent",
                    },
                )
            return HTMLResponse(
                content=_render_shell(
                    request,
                    initial_view="status",
                    canonical_path="/status" if settings.addon_path_secret else "/",
                ),
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate",
                    "Vary": "Accept, User-Agent",
                },
            )
        if request.method == "HEAD":
            return Response(status_code=200, media_type="application/json")
        return _json(
            {
                "addon": settings.addon_name,
                "version": settings.addon_version,
                "manifest": f"{prefix}/manifest.json",
                "resources": ["search", "stream"],
            },
            cache="public, max-age=600",
        )

    @app.api_route("/robots.txt", methods=["GET", "HEAD"], include_in_schema=False)
    async def robots_txt(request: Request) -> Response:
        pub_url = _public_display_url(settings, request)
        lines = [
            "User-agent: *",
            "Allow: /",
        ]
        if not settings.addon_path_secret:
            lines.append("Allow: /manifest.json")
        lines.extend(
            [
                "Disallow: /admin_panel_*",
                "Disallow: /admin",
                "Disallow: /vault",
                "Disallow: /_vault_",
                "Disallow: /internal/",
                "Disallow: /s/",
            ]
        )
        if not settings.public_plays:
            lines.append("Disallow: /plays")
        lines.append(f"Sitemap: {pub_url}/sitemap.xml")
        content = "\n".join(lines) + "\n"
        if request.method == "HEAD":
            return Response(status_code=200, media_type="text/plain")
        return Response(
            content=content,
            media_type="text/plain",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.api_route("/sitemap.xml", methods=["GET", "HEAD"], include_in_schema=False)
    async def sitemap_xml(request: Request) -> Response:
        pub_url = _public_display_url(settings, request)
        esc_url = html.escape(pub_url, quote=True)
        entries: list[tuple[str, str, str]] = []
        if not settings.addon_path_secret:
            entries.append((f"{esc_url}/", "daily", "1.0"))
            entries.append((f"{esc_url}/manifest.json", "weekly", "0.8"))
            entries.append((f"{esc_url}/status", "daily", "0.9"))
        else:
            entries.append((f"{esc_url}/status", "daily", "1.0"))
        if settings.public_plays:
            entries.append((f"{esc_url}/plays", "hourly", "0.8"))
        entries.append((f"{esc_url}/about", "weekly", "0.7"))
        entries.append((f"{esc_url}/support", "monthly", "0.6"))
        url_nodes = "\n".join(
            f"  <url><loc>{loc}</loc><changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
            for loc, freq, prio in entries
        )
        content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{url_nodes}\n"
            "</urlset>"
        )
        if request.method == "HEAD":
            return Response(status_code=200, media_type="application/xml")
        return Response(
            content=content,
            media_type="application/xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    def _public_capabilities() -> dict[str, Any]:
        return {
            "public_plays": bool(settings.public_plays),
            "addon_auth_enabled": bool(settings.addon_auth_enabled),
            "path_secret": bool(settings.addon_path_secret),
            "primary_provider": pool.primary_provider,
            "quality_options": [
                {"value": value, "label": label}
                for value, label in QUALITY_OPTIONS
            ],
        }

    def _donate_configured() -> bool:
        return bool(
            (settings.donate_btc or "").strip()
            or (settings.donate_eth or "").strip()
            or (settings.donate_usdc_erc20 or "").strip()
            or (settings.donate_sol or "").strip()
            or (settings.donate_polygon or "").strip()
        )

    def _render_shell(
        request: Request,
        *,
        initial_view: str,
        canonical_path: str,
    ) -> str:
        return render_app_shell(
            initial_view=initial_view,
            public_url=_public_display_url(settings, request),
            addon_name=settings.addon_name,
            addon_version=settings.addon_version,
            public_plays=bool(settings.public_plays),
            donate_configured=_donate_configured(),
            donate_demo=bool(settings.donate_demo),
            addon_base_prefix="",
            capabilities=_public_capabilities(),
            canonical_path=canonical_path,
        )

    @app.get("/status", include_in_schema=False)
    @app.get("/monitor", include_in_schema=False)
    async def status_page(request: Request) -> Response:
        return HTMLResponse(
            content=_render_shell(
                request,
                initial_view="status",
                canonical_path="/status" if settings.addon_path_secret else "/",
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/support", include_in_schema=False)
    @app.get("/support/", include_in_schema=False)
    async def support_page(request: Request) -> Response:
        return HTMLResponse(
            content=_render_shell(
                request,
                initial_view="support",
                canonical_path="/support",
            ),
            headers={
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    @app.get("/about", include_in_schema=False)
    @app.get("/about/", include_in_schema=False)
    async def about_page(request: Request) -> Response:
        return HTMLResponse(
            content=_render_shell(
                request,
                initial_view="about",
                canonical_path="/about",
            ),
            headers={
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    def _current_public_stats_payload(request: Request | None = None) -> dict[str, Any]:
        snap = stats.snapshot()
        snap["in_flight"] = max(0, int(snap.get("in_flight", 1)) - 1)
        return build_public_stats_payload(
            addon_name=settings.addon_name,
            addon_version=settings.addon_version,
            public_url=_public_display_url(settings, request),
            stats_snapshot=snap,
            services_status=pool.public_services_status(),
            timeline_24h=stats.hourly_timeline(24),
            cache_entries={
                "search_entries": len(search_cache.cache),
                "stream_entries": len(stream_cache.cache),
            },
            path_secret=bool(settings.addon_path_secret),
            daily_timeline=stats.daily_timeline(30),
            incidents=stats.recent_incidents(25),
            telemetry=collect_host_telemetry(),
            artwork_cache=artwork.footprint(),
            capabilities=_public_capabilities(),
        )

    @app.get("/api/public-stats", include_in_schema=False)
    async def public_stats(request: Request) -> Response:
        return _json(_current_public_stats_payload(request), cache="no-store")

    @app.get("/api/overview", include_in_schema=False)
    async def public_overview(request: Request) -> Response:
        stats_payload = _current_public_stats_payload(request)
        support_payload = build_support_payload(
            btc=settings.donate_btc,
            eth=settings.donate_eth,
            usdc=settings.donate_usdc_erc20,
            sol=settings.donate_sol,
            polygon=settings.donate_polygon,
            demo=settings.donate_demo,
        )
        plays_payload = (
            build_plays_payload(
                plays,
                public_url=_public_display_url(settings, request),
                addon_name=settings.addon_name,
                artwork_for=_artwork_url,
            )
            if settings.public_plays
            else None
        )
        return _json(
            build_overview_payload(
                stats_payload=stats_payload,
                support_payload=support_payload,
                plays_payload=plays_payload,
            ),
            cache="no-store",
        )

    @app.api_route("/badge.svg", methods=["GET", "HEAD"], include_in_schema=False)
    async def status_badge(request: Request) -> Response:
        svc = pool.public_services_status()
        status = str(svc.get("status") or "outage")
        failover_active = bool(svc.get("failover_active", False))
        users = stats.active_user_counts()
        streamers = int(users.get("active_streamers_5m") or 0)
        detail = ""
        if status != "outage":
            if streamers > 0:
                detail = f"{streamers} live"
            elif stats.avg_response_ms > 0:
                detail = f"{int(round(stats.avg_response_ms))}ms"
        svg = render_status_badge_svg(
            status,
            failover_active=failover_active,
            detail=detail,
        )
        headers = {"Cache-Control": "no-cache, max-age=30"}
        if request.method == "HEAD":
            return Response(status_code=200, media_type="image/svg+xml", headers=headers)
        return Response(content=svg, media_type="image/svg+xml", headers=headers)

    @app.get("/art/{key}", include_in_schema=False)
    async def artwork_image(key: str) -> Response:
        """Cover art, cached here so the browser never talks to a third party."""
        cached = await artwork.get(key)
        if cached is None:
            return _error(404, "not_found", "artwork unavailable")
        body, content_type = cached
        return Response(
            content=body,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @app.get("/api/plays", include_in_schema=False)
    async def public_plays(
        request: Request,
        q: str = "",
        tier: str = "",
        limit: int | None = None,
    ) -> Response:
        if not settings.public_plays:
            return _error(404, "not_found", "The public play feed is disabled")
        return _json(
            build_plays_payload(
                plays,
                public_url=_public_display_url(settings, request),
                addon_name=settings.addon_name,
                artwork_for=_artwork_url,
                query=q,
                tier=tier,
                limit=limit,
            ),
            cache="no-store",
        )

    @app.get("/plays", include_in_schema=False)
    @app.get("/plays/", include_in_schema=False)
    async def plays_page(request: Request) -> Response:
        if not settings.public_plays:
            return _error(404, "not_found", "The public play feed is disabled")
        return HTMLResponse(
            content=_render_shell(
                request,
                initial_view="plays",
                canonical_path="/plays",
            ),
            headers={
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    @app.get("/api/support", include_in_schema=False)
    async def support_info() -> Response:
        return _json(
            build_support_payload(
                btc=settings.donate_btc,
                eth=settings.donate_eth,
                usdc=settings.donate_usdc_erc20,
                sol=settings.donate_sol,
                polygon=settings.donate_polygon,
                demo=settings.donate_demo,
            ),
            cache="no-store",
        )

    # ── Health & Readiness ────────────────────────────────────────────────
    async def health(request: Request) -> Response:
        snapshot = qobuz.health_snapshot()
        svc_status = pool.public_services_status()
        ready = _is_ready(snapshot) or bool((svc_status.get("tidal") or {}).get("online"))
        payload = {
            "status": "ok" if ready else "degraded",
            "overall_status": svc_status.get("status"),
            "status_reasons": svc_status.get("status_reasons", []),
            "addon": settings.addon_name,
            "version": settings.addon_version,
            "uptime_seconds": int(time.monotonic() - app.state.started_at),
            "auth_enabled": settings.addon_auth_enabled,
            "path_secret": bool(settings.addon_path_secret),
            "primary_provider": pool.primary_provider,
            "primary_online": svc_status.get("primary_online"),
            "failover_active": svc_status.get("failover_active"),
            "last_failover_at": svc_status.get("last_failover_at"),
            "auto_failover": pool.auto_failover,
            "qobuz": snapshot,
            "tidal": svc_status.get("tidal"),
            "cache": {
                "search_entries": len(search_cache.cache),
                "stream_entries": len(stream_cache.cache),
            },
        }
        return _json(payload, cache="no-store")

    async def ready(request: Request) -> Response:
        snapshot = qobuz.health_snapshot()
        svc_status = pool.public_services_status()
        tidal_online = bool((svc_status.get("tidal") or {}).get("online"))
        ready_now = _is_ready(snapshot) or tidal_online
        return _json(
            {
                "ready": ready_now,
                "configured": snapshot["configured"] or pool.has_tidal,
                "token_ok": snapshot["token_ok"],
                "geo_ok": snapshot["geo_ok"],
                "tidal_ready": tidal_online,
                "overall_status": svc_status.get("status"),
                "status_reasons": svc_status.get("status_reasons", []),
                "primary_provider": pool.primary_provider,
                "primary_online": svc_status.get("primary_online"),
                "failover_active": svc_status.get("failover_active"),
            },
            status=200 if ready_now else 503,
            cache="no-store",
        )

    health_paths = {"/healthz"}
    ready_paths = {"/readyz"}
    if prefix:
        health_paths.add(f"{prefix}/healthz")
        ready_paths.add(f"{prefix}/readyz")
    for path in sorted(health_paths):
        app.add_api_route(path, health, methods=["GET"], include_in_schema=False)
    for path in sorted(ready_paths):
        app.add_api_route(path, ready, methods=["GET"], include_in_schema=False)

    # ── Optional traffic view ─────────────────────────────────────────────
    @app.get(stats_path, include_in_schema=False)
    async def get_stats(request: Request) -> Response:
        if not settings.stats_token:
            return _error(404, "not_found", "Not found")
        supplied = request.headers.get("x-stats-token", "")
        if not hmac.compare_digest(supplied, settings.stats_token):
            return _error(
                401,
                "unauthorized",
                "A valid X-Stats-Token header is required.",
            )
        payload = stats.snapshot()
        payload["in_flight"] = max(0, int(payload["in_flight"]) - 1)
        return _json(payload, cache="no-store")

    # ── Local-Only Admin Control Plane + Hidden Web Vault ─────────────────
    vault_limiter = SlidingWindowLimiter(20)  # Max 20 failed vault password attempts/min per IP

    def _require_local_admin(request: Request) -> Response | None:
        # 1. Never respond on /internal/admin if the request passed through Cloudflare Tunnel
        if (
            request.headers.get("cf-ray")
            or request.headers.get("cf-connecting-ip")
            or request.headers.get("cf-visitor")
        ):
            return _error(404, "not_found", "Not found")
        # 2. Require valid stats_token in X-Admin-Token or X-Stats-Token
        secret = settings.stats_token
        if not secret:
            return _error(404, "not_found", "Not found")
        supplied = (
            request.headers.get("x-admin-token")
            or request.headers.get("x-stats-token")
            or ""
        )
        if not hmac.compare_digest(supplied, secret):
            return _error(401, "unauthorized", "Unauthorized")
        return None

    async def _require_vault_auth(request: Request) -> Response | None:
        secret = (settings.stats_token or "").strip().strip("'\"")
        if not secret:
            return _error(404, "not_found", "Not found")
        auth_header = request.headers.get("authorization") or ""
        bearer = (
            auth_header[7:].strip()
            if auth_header.lower().startswith("bearer ")
            else ""
        )
        # Header-only on purpose: query-string secrets leak through browser
        # history, proxy logs, and Referer headers.
        supplied = (
            request.headers.get("x-vault-key")
            or request.headers.get("x-admin-token")
            or request.headers.get("x-stats-token")
            or bearer
            or ""
        )
        supplied = supplied.strip().strip("'\"")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip().strip("'\"")
        if hmac.compare_digest(supplied, secret):
            return None
        ip = _client_ip(request, settings)
        allowed, retry_after = await vault_limiter.hit(f"vault-fail:{ip}")
        if not allowed:
            return JSONResponse(
                {"error": "rate_limited", "message": "Too many failed login attempts"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return _error(401, "unauthorized", "Invalid Vault Password")

    async def vault_page_handler(request: Request) -> Response:
        return HTMLResponse(
            content=render_admin_vault_html(path_secret=bool(settings.addon_path_secret)),
            headers={
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    if settings.admin_path_secret:
        vault_page_paths = [vault_prefix, f"{vault_prefix}/"]
        vault_prefixes = [vault_prefix]
    else:
        vault_page_paths = ["/admin", "/admin/", "/vault", "/vault/"]
        if vault_prefix:
            vault_page_paths.extend([vault_prefix, f"{vault_prefix}/"])
        vault_prefixes = list(
            dict.fromkeys(p for p in (vault_prefix, "/admin", "/vault") if p)
        )
    for p in vault_page_paths:
        app.add_api_route(p, vault_page_handler, methods=["GET"], include_in_schema=False)

    async def _handle_admin_summary(request: Request | None = None) -> Response:
        snap = stats.snapshot()
        pub_base = _public_display_url(settings, request)
        return _json(
            {
                "primary_provider": pool.primary_provider,
                "auto_failover": pool.auto_failover,
                "vault_url": f"{pub_base}{vault_prefix}" if vault_prefix else None,
                "users": snap.get("users"),
                "providers_traffic": snap.get("providers"),
                "cache": snap.get("cache"),
                "qobuz_accounts": pool.qobuz_accounts_summary(),
                "tidal_accounts": pool.tidal_accounts_summary(),
                "device_auth": {
                    "status": pool.device_auth_status,
                    "last_account": pool.device_auth_last_account,
                    "pending_code": (
                        pool.pending_device_auth.user_code
                        if pool.pending_device_auth
                        else None
                    ),
                    "pending_url": (
                        pool.pending_device_auth.verification_uri_complete
                        if pool.pending_device_auth
                        else None
                    ),
                },
            }
        )

    @app.get("/internal/admin/summary", include_in_schema=False)
    async def admin_summary(request: Request) -> Response:
        denied = _require_local_admin(request)
        if denied is not None:
            return denied
        return await _handle_admin_summary(request)

    async def _parse_json_object(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
        try:
            body = await request.json()
        except Exception:
            return None, _error(400, "invalid_request", "Request body must be valid JSON.")
        if not isinstance(body, dict):
            return None, _error(400, "invalid_request", "Request body must be a JSON object.")
        return body, None

    @app.post("/internal/admin/qobuz/add", include_in_schema=False)
    async def admin_add_qobuz(request: Request) -> Response:
        denied = _require_local_admin(request)
        if denied is not None:
            return denied
        body, err = await _parse_json_object(request)
        if err is not None:
            return err
        assert body is not None
        user_auth_token = str(body.get("user_auth_token") or body.get("token") or "").strip()
        email = str(body.get("email") or "").strip()
        password = str(body.get("password") or "").strip()
        app_secret = str(body.get("app_secret") or "").strip()
        register_secrets([user_auth_token, password, app_secret])
        if not user_auth_token and not (email and password):
            return _error(400, "invalid_request", "user_auth_token or email and password is required")
        acc = await pool.add_qobuz_account(
            label=str(body.get("label") or ""),
            user_auth_token=user_auth_token,
            user_id=str(body.get("user_id") or ""),
            email=email,
            password=password,
            app_id=str(body.get("app_id") or ""),
            app_secret=app_secret,
        )
        await search_cache.clear()
        return _json({"ok": True, "account": acc})

    @app.post("/internal/admin/tidal/device-start", include_in_schema=False)
    async def admin_start_tidal_device(request: Request) -> Response:
        denied = _require_local_admin(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        info = await pool.start_tidal_device_flow(label=str(body.get("label") or ""))
        return _json({"ok": True, **info})

    @app.post("/internal/admin/tidal/add", include_in_schema=False)
    async def admin_add_tidal(request: Request) -> Response:
        denied = _require_local_admin(request)
        if denied is not None:
            return denied
        body, err = await _parse_json_object(request)
        if err is not None:
            return err
        assert body is not None
        access_token = str(body.get("access_token") or body.get("token") or "").strip()
        refresh_token = str(body.get("refresh_token") or "").strip()
        register_secrets([access_token, refresh_token])
        if not access_token and not refresh_token:
            return _error(400, "invalid_request", "access_token or refresh_token is required")
        acc = await pool.add_tidal_account(
            label=str(body.get("label") or ""),
            access_token=access_token,
            refresh_token=refresh_token,
            user_id=str(body.get("user_id") or ""),
            country_code=str(body.get("country_code") or "US"),
        )
        await search_cache.clear()
        return _json({"ok": True, "account": acc})

    @app.post("/internal/admin/accounts/{account_id}/toggle", include_in_schema=False)
    async def admin_toggle_account(account_id: str, request: Request) -> Response:
        denied = _require_local_admin(request)
        if denied is not None:
            return denied
        res = pool.toggle_account(account_id)
        if res is None:
            return _error(404, "not_found", f"Account {account_id} not found")
        await search_cache.clear()
        return _json({"ok": True, **res})

    @app.post("/internal/admin/accounts/{account_id}/test", include_in_schema=False)
    async def admin_test_account(account_id: str, request: Request) -> Response:
        denied = _require_local_admin(request)
        if denied is not None:
            return denied
        res = await pool.test_account(account_id)
        if res is None:
            return _error(404, "not_found", f"Account {account_id} not found")
        return _json({"ok": True, "health": res})

    @app.delete("/internal/admin/accounts/{account_id}", include_in_schema=False)
    async def admin_delete_account(account_id: str, request: Request) -> Response:
        denied = _require_local_admin(request)
        if denied is not None:
            return denied
        deleted = await pool.delete_account(account_id)
        if not deleted:
            return _error(404, "not_found", f"Account {account_id} not found")
        await search_cache.clear()
        return _json({"ok": True, "deleted": account_id})

    @app.post("/internal/admin/routing", include_in_schema=False)
    async def admin_set_routing(request: Request) -> Response:
        denied = _require_local_admin(request)
        if denied is not None:
            return denied
        body, err = await _parse_json_object(request)
        if err is not None:
            return err
        assert body is not None
        try:
            res = pool.set_routing(
                primary_provider=body.get("primary_provider"),
                auto_failover=body.get("auto_failover"),
            )
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        await search_cache.clear()
        return _json({"ok": True, **res})

    @app.post("/internal/admin/cache/clear", include_in_schema=False)
    async def admin_clear_cache(request: Request) -> Response:
        denied = _require_local_admin(request)
        if denied is not None:
            return denied
        s_count = len(search_cache.cache)
        st_count = len(stream_cache.cache)
        await search_cache.clear()
        await stream_cache.clear()
        return _json(
            {
                "ok": True,
                "cleared_search_entries": s_count,
                "cleared_stream_entries": st_count,
            }
        )


    # Mount admin actions across configured vault paths

    async def vault_summary(request: Request) -> Response:
        denied = await _require_vault_auth(request)
        return denied if denied is not None else await _handle_admin_summary(request)

    async def vault_add_qobuz(request: Request) -> Response:
        denied = await _require_vault_auth(request)
        if denied is not None:
            return denied
        body, err = await _parse_json_object(request)
        if err is not None:
            return err
        assert body is not None
        user_auth_token = str(body.get("user_auth_token") or body.get("token") or "").strip()
        email = str(body.get("email") or "").strip()
        password = str(body.get("password") or "").strip()
        app_secret = str(body.get("app_secret") or "").strip()
        register_secrets([user_auth_token, password, app_secret])
        if not user_auth_token and not (email and password):
            return _error(400, "invalid_request", "user_auth_token or email and password is required")
        acc = await pool.add_qobuz_account(
            label=str(body.get("label") or ""),
            user_auth_token=user_auth_token,
            user_id=str(body.get("user_id") or ""),
            email=email,
            password=password,
            app_id=str(body.get("app_id") or ""),
            app_secret=app_secret,
        )
        await search_cache.clear()
        return _json({"ok": True, "account": acc})

    async def vault_start_tidal_device(request: Request) -> Response:
        denied = await _require_vault_auth(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        info = await pool.start_tidal_device_flow(label=str(body.get("label") or ""))
        return _json({"ok": True, **info})

    async def vault_add_tidal(request: Request) -> Response:
        denied = await _require_vault_auth(request)
        if denied is not None:
            return denied
        body, err = await _parse_json_object(request)
        if err is not None:
            return err
        assert body is not None
        access_token = str(body.get("access_token") or body.get("token") or "").strip()
        refresh_token = str(body.get("refresh_token") or "").strip()
        register_secrets([access_token, refresh_token])
        if not access_token and not refresh_token:
            return _error(400, "invalid_request", "access_token or refresh_token is required")
        acc = await pool.add_tidal_account(
            label=str(body.get("label") or ""),
            access_token=access_token,
            refresh_token=refresh_token,
            user_id=str(body.get("user_id") or ""),
            country_code=str(body.get("country_code") or "US"),
        )
        await search_cache.clear()
        return _json({"ok": True, "account": acc})

    async def vault_toggle_account(account_id: str, request: Request) -> Response:
        denied = await _require_vault_auth(request)
        if denied is not None:
            return denied
        res = pool.toggle_account(account_id)
        if res is None:
            return _error(404, "not_found", f"Account {account_id} not found")
        await search_cache.clear()
        return _json({"ok": True, **res})

    async def vault_test_account(account_id: str, request: Request) -> Response:
        denied = await _require_vault_auth(request)
        if denied is not None:
            return denied
        res = await pool.test_account(account_id)
        if res is None:
            return _error(404, "not_found", f"Account {account_id} not found")
        return _json({"ok": True, "health": res})

    async def vault_delete_account(account_id: str, request: Request) -> Response:
        denied = await _require_vault_auth(request)
        if denied is not None:
            return denied
        deleted = await pool.delete_account(account_id)
        if not deleted:
            return _error(404, "not_found", f"Account {account_id} not found")
        await search_cache.clear()
        return _json({"ok": True, "deleted": account_id})

    async def vault_set_routing(request: Request) -> Response:
        denied = await _require_vault_auth(request)
        if denied is not None:
            return denied
        body, err = await _parse_json_object(request)
        if err is not None:
            return err
        assert body is not None
        try:
            res = pool.set_routing(
                primary_provider=body.get("primary_provider"),
                auto_failover=body.get("auto_failover"),
            )
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        await search_cache.clear()
        return _json({"ok": True, **res})

    async def vault_clear_cache(request: Request) -> Response:
        denied = await _require_vault_auth(request)
        if denied is not None:
            return denied
        s_count = len(search_cache.cache)
        st_count = len(stream_cache.cache)
        await search_cache.clear()
        await stream_cache.clear()
        return _json(
            {
                "ok": True,
                "cleared_search_entries": s_count,
                "cleared_stream_entries": st_count,
            }
        )

    for p in vault_prefixes:
        app.add_api_route(f"{p}/api/summary", vault_summary, methods=["GET"], include_in_schema=False)
        app.add_api_route(f"{p}/api/qobuz/add", vault_add_qobuz, methods=["POST"], include_in_schema=False)
        app.add_api_route(f"{p}/api/tidal/device-start", vault_start_tidal_device, methods=["POST"], include_in_schema=False)
        app.add_api_route(f"{p}/api/tidal/add", vault_add_tidal, methods=["POST"], include_in_schema=False)
        app.add_api_route(f"{p}/api/accounts/{{account_id}}/toggle", vault_toggle_account, methods=["POST"], include_in_schema=False)
        app.add_api_route(f"{p}/api/accounts/{{account_id}}/test", vault_test_account, methods=["POST"], include_in_schema=False)
        app.add_api_route(f"{p}/api/accounts/{{account_id}}", vault_delete_account, methods=["DELETE"], include_in_schema=False)
        app.add_api_route(f"{p}/api/routing", vault_set_routing, methods=["POST"], include_in_schema=False)
        app.add_api_route(f"{p}/api/cache/clear", vault_clear_cache, methods=["POST"], include_in_schema=False)


    # ── Error mapping ─────────────────────────────────────────────────────
    @app.exception_handler(QobuzError)
    async def qobuz_error_handler(request: Request, exc: QobuzError) -> Response:
        headers = {"Cache-Control": "no-store"}
        if isinstance(exc, QobuzRateLimited):
            headers["Retry-After"] = str(exc.retry_after)
        stats.qobuz_errors[exc.code] += 1
        log.warning("Qobuz %s: %s", exc.code, exc.message)
        return JSONResponse(
            {"error": exc.code, "code": exc.code, "message": exc.message},
            status_code=exc.status,
            headers=headers,
        )

    @app.exception_handler(TidalError)
    async def tidal_error_handler(request: Request, exc: TidalError) -> Response:
        headers = {"Cache-Control": "no-store"}
        if isinstance(exc, TidalRateLimited):
            headers["Retry-After"] = str(exc.retry_after)
        stats.tidal_errors[exc.code] += 1
        log.warning("Tidal %s: %s", exc.code, exc.message)
        return JSONResponse(
            {"error": exc.code, "code": exc.code, "message": exc.message},
            status_code=exc.status,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> Response:
        return _error(400, "invalid_request", "The request parameters are invalid.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        message = str(exc.detail) if exc.detail else "Request failed"
        return JSONResponse(
            {"error": "http_error", "code": str(exc.status_code), "message": message},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
        log.exception("Unhandled error while serving %s", request.url.path)
        return _error(500, "internal_error", "The addon hit an unexpected error.")

    return app


# ── Helpers ───────────────────────────────────────────────────────────────


def _build_manifest(settings: Settings) -> dict[str, Any]:
    return AddonManifest(
        id=settings.addon_id,
        name=settings.addon_name,
        version=settings.addon_version,
        resources=["search", "stream"],
        allowDownloads=1 if settings.allow_downloads else 0,
        checkValidLossless=1 if settings.check_valid_lossless else 0,
        bitchordAuth=settings.addon_auth_enabled,
        settings=[
            AddonSetting(
                key="quality",
                type="select",
                label="Audio quality",
                default="max",
                options=[
                    AddonQualityOption(value=value, label=label)
                    for value, label in QUALITY_OPTIONS
                ],
            )
        ],
    ).model_dump()


def _is_ready(snapshot: dict[str, Any]) -> bool:
    return bool(
        snapshot.get("configured")
        and snapshot.get("token_ok") is not False
        and snapshot.get("geo_ok") is not False
    )


def _client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        if settings.trust_cf_headers and (request.headers.get("cf-ray") or "").strip():
            cf_ip = (request.headers.get("cf-connecting-ip") or "").strip()
            if cf_ip:
                return cf_ip
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
        if settings.trust_cf_headers:
            cf_ip = (request.headers.get("cf-connecting-ip") or "").strip()
            if cf_ip:
                return cf_ip
    return request.client.host if request.client else "unknown"


def _endpoint_name(path: str, prefix: str, vault_prefix: str = "") -> str:
    if prefix and (path == prefix or path.startswith(f"{prefix}/")):
        path = path[len(prefix):] or "/"
    if path == "/manifest.json":
        return "manifest"
    if path == "/search":
        return "search"
    if path.startswith("/stream/"):
        return "stream"
    if path.startswith("/dash/"):
        return "dash"
    if path in ("/healthz", "/readyz"):
        return "health"
    if path == "/stats":
        return "stats"
    if path in ("/status", "/status/", "/monitor", "/monitor/"):
        return "status_page"
    if path == "/api/public-stats":
        return "public_stats"
    if path in ("/plays", "/plays/"):
        return "plays_page"
    if path == "/api/plays":
        return "api_plays"
    if path in ("/support", "/support/"):
        return "support_page"
    if path == "/api/support":
        return "api_support"
    if path in ("/about", "/about/"):
        return "about_page"
    if path == "/api/overview":
        return "overview"
    if path.startswith("/art/"):
        return "artwork"
    if path == "/badge.svg":
        return "badge"
    if path.startswith("/assets/"):
        return "assets"
    if (
        path.startswith("/internal/admin")
        or path == "/admin"
        or path.startswith("/admin/")
        or path.startswith("/vault")
        or path.startswith("/_vault_")
        or (vault_prefix and path.startswith(vault_prefix))
    ):
        return "admin"
    if path in ("/docs", "/redoc", "/openapi.json"):
        return "docs"
    if path in ("/robots.txt", "/sitemap.xml"):
        return "seo"
    if path == "/":
        return "root"
    return "other"


def _valid_host(raw: str) -> str:
    host = raw.strip()
    if not host or not _SAFE_HOST_RE.fullmatch(host):
        return ""
    try:
        parsed = urlparse(f"http://{host}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if not hostname:
        return ""
    if port is not None and not (1 <= port <= 65535):
        return ""
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            return ""
        expected = f"[{hostname}]" if port is None else f"[{hostname}]:{port}"
        if host.lower() != expected:
            return ""
        return host
    if "[" in host or "]" in host or ":" in hostname:
        return ""
    expected = hostname if port is None else f"{hostname}:{port}"
    if host.lower() != expected:
        return ""
    if not (1 <= len(hostname) <= 253):
        return ""
    labels = hostname.split(".")
    if all(lbl.isdigit() for lbl in labels):
        if len(labels) != 4:
            return ""
        try:
            ipaddress.IPv4Address(hostname)
        except ValueError:
            return ""
        return host
    if labels[-1].isdigit() or not all(
        _HOST_LABEL_RE.fullmatch(lbl) for lbl in labels
    ):
        return ""
    return host


def _public_display_url(
    settings: Settings, request: Request | None = None
) -> str:
    """A public origin URL that never contains ``ADDON_PATH_SECRET``."""
    configured = settings.public_base_url.rstrip("/")
    if configured:
        if settings.addon_path_secret:
            parsed = urlparse(configured)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
            return configured.split("/s/", 1)[0].rstrip("/")
        return configured

    if request is not None:
        scheme = request.url.scheme.lower()
        host = _valid_host(request.url.netloc)
        if settings.trust_proxy_headers:
            fwd_proto = (
                (request.headers.get("x-forwarded-proto") or "")
                .split(",", 1)[0]
                .strip()
                .lower()
            )
            if fwd_proto in ("http", "https"):
                scheme = fwd_proto
            fwd_host = _valid_host(
                (request.headers.get("x-forwarded-host") or "").split(",", 1)[0]
            )
            if fwd_host:
                host = fwd_host
        if scheme in ("http", "https") and host:
            return f"{scheme}://{host}"

    return "https://bitchord.confih.tech"


async def _precache_stream(
    pool: ProviderPool,
    stream_cache: SingleFlightCache[tuple[str, str], ResolvedStream],
    stats: TrafficStats,
    track_id: str,
    tier: str,
    dash_base: str,
) -> None:
    cache_key = (track_id, tier)
    if cache_key in stream_cache.cache:
        return
    try:
        _, cached = await stream_cache.get_or_create(
            cache_key,
            lambda: pool.resolve_stream(track_id, tier, dash_base_url=dash_base),
        )
        # A prefetch is not a listener. Only the dedicated counter moves, and
        # only when this task actually performed the resolution.
        if not cached:
            stats.record_precache_resolution()
    except Exception:
        pass


async def _traffic_log_loop(
    stats: TrafficStats, plays: PlayLedger, interval: int
) -> None:
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(plays.save, False)
        snapshot = stats.snapshot()
        if not snapshot["requests"]:
            continue
        log.info(
            "traffic: %s",
            json.dumps(snapshot, separators=(",", ":"), sort_keys=True),
        )


def _json(
    payload: Any,
    *,
    status: int = 200,
    cache: str = "no-store",
) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status,
        headers={"Cache-Control": cache},
    )


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": code, "code": code, "message": message},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )
