"""Public Live Monitor & Status Page for bitchord.confih.tech.

Serves a quiet, Apple-style live status and analytics page for human visitors
(when ``Accept: text/html`` or on ``/status`` / ``/monitor``) while keeping the
JSON addon contract intact for the BitChord app.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _safe_json_script(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":"))
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def build_public_stats_payload(
    *,
    addon_name: str,
    addon_version: str,
    public_url: str,
    stats_snapshot: dict[str, Any],
    services_status: dict[str, Any],
    timeline_24h: list[dict[str, Any]],
    cache_entries: dict[str, int],
    path_secret: bool = False,
    daily_timeline: dict[str, Any] | None = None,
    incidents: list[dict[str, Any]] | None = None,
    telemetry: dict[str, Any] | None = None,
    artwork_cache: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    users = stats_snapshot.get("users") or {}
    cache = stats_snapshot.get("cache") or {}
    providers = stats_snapshot.get("providers") or {}
    q_up = bool((services_status.get("qobuz") or {}).get("online"))
    t_up = bool((services_status.get("tidal") or {}).get("online"))

    if services_status.get("status") in ("operational", "degraded", "outage"):
        overall = str(services_status["status"])
    else:
        overall = "operational" if (q_up or t_up) else "degraded"

    display_url = public_url or "https://bitchord.confih.tech"
    daily = daily_timeline or {}
    return {
        "status": overall,
        "status_reasons": services_status.get("status_reasons", []),
        "primary_provider": services_status.get(
            "primary_provider", "unknown"
        ),
        "primary_online": bool(services_status.get("primary_online", False)),
        "failover_active": bool(services_status.get("failover_active", False)),
        "last_failover_at": services_status.get("last_failover_at"),
        "addon": {
            "name": addon_name,
            "version": addon_version,
            "url": display_url,
            "display_url": display_url,
            "path_secret": path_secret,
            "uptime_seconds": stats_snapshot.get("uptime_seconds", 0),
        },
        "users": {
            "active_now": users.get("active_now", 0),
            "active_now_window_seconds": users.get(
                "active_now_window_seconds", 90
            ),
            "active_streamers_5m": users.get("active_streamers_5m", 0),
            "active_streamers_window_seconds": users.get(
                "active_streamers_window_seconds", 300
            ),
            "active_1h": users.get("active_1h", 0),
            "active_24h": users.get("active_24h", 0),
            "total_all_time": users.get("total_all_time", 0),
        },
        "traffic": {
            "total_requests": stats_snapshot.get("total_requests_all_time", 0),
            "monitor_requests": stats_snapshot.get("monitor_requests", 0),
            "total_searches": stats_snapshot.get("total_searches_all_time", 0),
            "total_streams": stats_snapshot.get("total_streams_all_time", 0),
            "streams_served": stats_snapshot.get("streams_served", 0),
            "in_flight": stats_snapshot.get("in_flight", 0),
            "peak_in_flight": stats_snapshot.get("peak_in_flight", 0),
            "avg_response_ms": stats_snapshot.get("avg_response_ms", 0.0),
            "monitor_avg_response_ms": stats_snapshot.get(
                "monitor_avg_response_ms", 0.0
            ),
            "failovers": providers.get(
                "failovers", providers.get("failover_resolutions", 0)
            ),
            "failover_resolutions": providers.get(
                "failover_resolutions", providers.get("failovers", 0)
            ),
            "failover_streams_served": providers.get(
                "failover_streams_served", 0
            ),
            "failover_reasons": providers.get("failover_reasons", {}),
            "precache_resolutions": stats_snapshot.get(
                "precache_resolutions", 0
            ),
        },
        "quality_tiers": stats_snapshot.get("quality_tiers", {}),
        "observed_quality": stats_snapshot.get("observed_quality", {}),
        "cache": {
            "hit_rate_percent": cache.get("hit_rate_percent", 0.0),
            "search_entries": cache_entries.get("search_entries", 0),
            "stream_entries": cache_entries.get("stream_entries", 0),
            "search_hits": (cache.get("search") or {}).get("hits", 0),
            "stream_hits": (cache.get("stream") or {}).get("hits", 0),
        },
        "services": services_status,
        "timeline_24h": timeline_24h,
        "timeline_30d": list(daily.get("days") or []),
        "uptime_30d_percent": daily.get("uptime_percent"),
        "total_requests_30d": int(daily.get("total_requests_30d") or 0),
        "incidents": (
            list(incidents[:25])
            if incidents is not None
            else list((daily.get("incidents") or [])[:25])
        ),
        "telemetry": dict(telemetry or {}),
        "artwork_cache": dict(artwork_cache or {}),
        "capabilities": dict(capabilities or {}),
    }


#: Token metadata for the Support page. ``logo`` names an inline SVG symbol in
#: the shell sprite, so no third-party asset is ever fetched for a coin mark.
_SUPPORT_TOKENS: dict[str, dict[str, str]] = {
    "btc": {"symbol": "BTC", "name": "Bitcoin", "logo": "coin-btc"},
    "sol": {"symbol": "SOL", "name": "Solana", "logo": "coin-sol"},
    "usdc": {"symbol": "USDC", "name": "USD Coin", "logo": "coin-usdc"},
    "usdt": {"symbol": "USDT", "name": "Tether USD", "logo": "coin-usdt"},
    "eth": {"symbol": "ETH", "name": "Ethereum", "logo": "coin-eth"},
}

_DEMO_SUPPORT_ASSETS: tuple[dict[str, Any], ...] = (
    {
        "id": "btc",
        "name": "Bitcoin",
        "network": "Bitcoin network",
        "symbol": "BTC",
        "logo": "coin-btc",
        "accepts": ["btc"],
        "address": "bc1qexample8v6z0v9w4m2k7c5n3p8r6t4y2u0s7d5f",
    },
    {
        "id": "sol",
        "name": "Solana",
        "network": "Solana network",
        "symbol": "SOL",
        "logo": "coin-sol",
        "accepts": ["sol", "usdc", "usdt"],
        "address": "BiqDpAFGfkbnS2UqqnrSE19rGSZZGfTjoTWe1uvL1PV5",
    },
    {
        "id": "polygon",
        "name": "Polygon",
        "network": "Polygon PoS",
        "symbol": "USDC",
        "logo": "coin-polygon",
        "accepts": ["usdc", "usdt"],
        "address": "0x876cF516c35802aa3A6d09d72Ac466F01D10C51e",
    },
)


def _support_asset(
    asset_id: str,
    name: str,
    network: str,
    logo: str,
    accepts: tuple[str, ...],
    address: str,
) -> dict[str, Any] | None:
    """One row per address, listing every token that address can receive.

    A Solana address takes SOL, USDC and USDT, so listing the same string three
    times would be noise; the tokens travel as ``accepts`` instead.
    """
    cleaned = (address or "").strip()
    if not cleaned:
        return None
    tokens = [
        {**_SUPPORT_TOKENS[key], "key": key} for key in accepts if key in _SUPPORT_TOKENS
    ]
    return {
        "id": asset_id,
        "name": name,
        "network": network,
        "logo": logo,
        "symbol": tokens[0]["symbol"] if tokens else name[:4].upper(),
        "accepts": tokens,
        "address": cleaned,
    }


def build_support_payload(
    *,
    btc: str = "",
    eth: str = "",
    usdc: str = "",
    sol: str = "",
    polygon: str = "",
    demo: bool = False,
) -> dict[str, Any]:
    """Return configured donation addresses without inventing any.

    Real addresses always win. Demo addresses are only returned when the
    deployment explicitly opts in and has not configured a real address.
    """
    candidates = (
        _support_asset("btc", "Bitcoin", "Bitcoin network", "coin-btc", ("btc",), btc),
        _support_asset(
            "sol", "Solana", "Solana network", "coin-sol", ("sol", "usdc", "usdt"), sol
        ),
        _support_asset(
            "polygon",
            "Polygon",
            "Polygon PoS",
            "coin-polygon",
            ("usdc", "usdt"),
            polygon,
        ),
        _support_asset("eth", "Ethereum", "Ethereum mainnet", "coin-eth", ("eth",), eth),
        _support_asset(
            "usdc-erc20",
            "USD Coin",
            "Ethereum (ERC-20)",
            "coin-usdc",
            ("usdc",),
            usdc,
        ),
    )
    assets = [asset for asset in candidates if asset is not None]
    if assets:
        return {"demo": False, "assets": assets}
    if demo:
        return {
            "demo": True,
            "assets": [dict(asset) for asset in _DEMO_SUPPORT_ASSETS],
        }
    return {"demo": False, "assets": []}


def build_overview_payload(
    *,
    stats_payload: dict[str, Any],
    support_payload: dict[str, Any],
    plays_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Combined bootstrap payload for the public gateway shell."""
    return {
        "stats": stats_payload,
        "support": support_payload,
        "plays": plays_payload,
    }


#: How much of the ledger the page is handed. Both lists used to stop at 12,
#: which made a run of quick skips look like it had lost tracks the ledger had
#: in fact counted; the recent feed now covers a whole sitting.
TOP_LIMIT = 25
RECENT_LIMIT = 60


def build_plays_payload(
    ledger: Any,
    *,
    public_url: str = "",
    addon_name: str = "",
    artwork_for: Callable[[str], str | None] | None = None,
    query: str = "",
    tier: str = "",
    limit: int | None = None,
) -> dict[str, Any]:
    """The public play feed: aggregate counts plus an anonymous history.

    Nothing in here identifies a listener. ``listeners`` is a deduplicated
    count of hashed client keys, and ``at`` is rounded to the minute.
    """
    totals = ledger.totals()

    def cover(row: dict[str, Any]) -> dict[str, Any]:
        """Send covers through this host so the browser makes no third-party
        request (content-security-policy is img-src 'self')."""
        raw_art = row.get("artwork")
        source = raw_art.strip() if isinstance(raw_art, str) else ""
        if not source:
            return {**row, "artwork": None}
        served = artwork_for(source) if artwork_for is not None else None
        if not served:
            return {**row, "artwork": None}
        return {**row, "artwork": served}

    q_clean = (query or "").strip()[:200]
    tier_raw = (tier or "").strip().lower()
    tier_clean = "" if tier_raw in ("", "all") else tier_raw
    has_filters = bool(q_clean or tier_clean or limit is not None)
    effective_limit = max(1, min(int(limit if limit is not None else 12), 50))

    if not has_filters:
        # Every recorded play has to be reachable from the page: capping the two
        # lists at a dozen rows made a run of skips look like it was losing
        # tracks that the ledger had counted.
        return {
            "addon": addon_name,
            "url": public_url,
            "totals": totals,
            "top": [cover(row) for row in ledger.top(TOP_LIMIT)],
            "recent": [cover(row) for row in ledger.recent(RECENT_LIMIT)],
            "timeline": ledger.timeline(24),
        }

    matched_top = [
        cover(row)
        for row in ledger.search(q_clean, limit=effective_limit, tier=tier_clean)
    ]
    matched_recent = [
        cover(row)
        for row in ledger.filter_recent(
            q_clean, limit=effective_limit, tier=tier_clean
        )
    ]
    return {
        "addon": addon_name,
        "url": public_url,
        "totals": totals,
        "top": matched_top,
        "recent": matched_recent,
        "timeline": ledger.timeline(24),
        "query": q_clean,
        "tier": tier_clean,
        "limit": effective_limit,
        "search_results": matched_top,
    }


_VIEW_META: dict[str, dict[str, str]] = {
    "status": {
        "title": "Status — Confih Gateway",
        "desc": "Live status, provider health and connection details for the Confih Hi-Res gateway for BitChord.",
        "og_title": "Confih — live gateway status",
        "og_desc": "Provider health, request reliability and connection details for the Confih Hi-Res gateway for BitChord.",
        "robots": "",
    },
    "plays": {
        "title": "Plays — Confih Gateway",
        "desc": "What is playing through the Confih Hi-Res gateway: ranked tracks with covers, delivered audio quality, and an anonymous recent feed.",
        "og_title": "Plays — the Confih gateway",
        "og_desc": "Ranked tracks with covers, delivered audio quality, and an anonymous recent feed.",
        "robots": '<meta name="robots" content="noindex, nofollow" />',
    },
    "about": {
        "title": "About — Confih Gateway",
        "desc": "What the Confih Hi-Res gateway for BitChord does, how it resolves Qobuz and Tidal streams, what its numbers mean, and what it does with your data.",
        "og_title": "About the Confih gateway",
        "og_desc": "How the addon resolves hi-res Qobuz and Tidal streams, the privacy model, and what its status numbers mean.",
        "robots": "",
    },
    "support": {
        "title": "Support — Confih Gateway",
        "desc": "How to support the Confih Hi-Res gateway for BitChord, what donations cover, and what they never buy.",
        "og_title": "Support the Confih gateway",
        "og_desc": "How to support the Confih Hi-Res gateway for BitChord, what donations cover, and what they never buy.",
        "robots": '<meta name="robots" content="noindex, nofollow" />',
    },
}

APP_SHELL_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark" data-initial-view="{{initial_view}}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <title>{{title}}</title>
  <meta name="description" content="{{description}}" />
  <meta name="theme-color" id="theme-color-meta" content="#09090b" />
  <meta name="color-scheme" content="dark" />
  {{robots_meta}}
  <link rel="preload" as="font" type="font/woff2" crossorigin href="/assets/fonts/inter-latin.woff2?v={{v}}" />
  <link rel="canonical" href="{{canonical_url}}" />
  <link rel="icon" href="/assets/confih-mark.svg?v={{v}}" type="image/svg+xml" />

  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="Confih" />
  <meta property="og:title" content="{{og_title}}" />
  <meta property="og:description" content="{{og_description}}" />
  <meta property="og:url" content="{{canonical_url}}" />
  <meta name="twitter:title" content="{{og_title}}" />
  <meta name="twitter:description" content="{{og_description}}" />
  <meta property="og:image" content="{{origin}}/assets/og-card.png?v={{v}}" />
  <meta property="og:image:width" content="1200" />
  <meta property="og:image:height" content="630" />
  <meta property="og:image:alt" content="Confih — a small gateway with one job." />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:image" content="{{origin}}/assets/og-card.png?v={{v}}" />

  <script>
    (function () {
      document.documentElement.setAttribute('data-theme', 'dark');
      document.documentElement.classList.add('has-js');
    })();
  </script>

  <style>
    @font-face {
      font-family: "InterVar";
      font-style: normal;
      font-weight: 100 900;
      font-display: swap;
      src: url("/assets/fonts/inter-latin.woff2?v={{v}}") format("woff2");
    }
  </style>

  <link rel="stylesheet" href="/assets/confih.css?v={{v}}" />
  <link rel="stylesheet" href="/assets/content.css?v={{v}}" />
  <link rel="stylesheet" href="/assets/monitor.css?v={{v}}" />
  {{plays_css}}
  <link rel="stylesheet" href="/assets/support.css?v={{v}}" />
  <link rel="stylesheet" href="/assets/about.css?v={{v}}" />

  <script type="application/json" id="confih-boot">{{boot_json}}</script>
  <script src="/assets/theme.js?v={{v}}" defer></script>
  <script src="/assets/spatial-gl.js?v={{v}}" defer></script>
  <script src="/assets/site.js?v={{v}}" defer></script>
  <script src="/assets/router.js?v={{v}}" defer></script>
  <script src="/assets/monitor.js?v={{v}}" defer></script>
  {{plays_script}}
  <script src="/assets/support.js?v={{v}}" defer></script>
</head>
<body data-initial-view="{{active_view}}" data-initial-anchor="{{initial_anchor}}" data-public-plays="{{public_plays_flag}}" data-path-secret="{{path_secret_flag}}">
  <a class="skip-link" href="#main-content">Skip to content</a>

  <noscript>
    <div class="banner" role="status">
      <span class="banner-text">Live status and play feeds require JavaScript. Raw JSON is available at <a href="/api/public-stats">/api/public-stats</a>.</span>
    </div>
  </noscript>

  <div class="spatial-stage" id="spatial-stage" aria-hidden="true" hidden>
    <canvas id="spatial-canvas" hidden></canvas>
    <div id="spatial-css-fallback" class="spatial-css-fallback" hidden></div>
  </div>

  <header class="site-head">
    <div class="shell head-inner">
      <a class="brand" href="{{home_href}}" data-nav-view="status">
        <span class="brand-logo-stage">
          <img class="brand-logo" src="/assets/confih-logo.svg?v={{v}}" alt="Confih" width="102" height="38" />
          <img class="brand-logo-loader" src="/assets/confih-loader.svg?v={{v}}" alt="" aria-hidden="true" width="102" height="38" />
        </span>
        <span class="brand-suffix">Gateway</span>
      </a>
      <nav class="site-nav" aria-label="Primary">
        <a href="{{home_href}}" data-nav-view="status"{{status_current}}>Status</a>
        {{plays_nav_link}}
        <a href="/about" data-nav-view="about"{{about_current}}>About</a>
        <a href="/support" data-nav-view="support"{{support_current}}>Support</a>
      </nav>
      <div class="head-actions">
        <a class="quiet-link" href="https://github.com/kushagrasinghx/BitChord/releases" target="_blank" rel="noopener noreferrer" aria-label="BitChord app releases on GitHub">
          <span>BitChord app</span>
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M6 3.5H12.5V10M12.5 3.5L4 12" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </a>
      </div>
    </div>
  </header>

  <div class="banner" id="conn-banner" hidden role="status" aria-live="polite" aria-atomic="true">
    <img class="banner-loader-logo" src="/assets/confih-loader.svg?v={{v}}" alt="" aria-hidden="true" width="84" height="31" />
    <span class="banner-text" id="conn-banner-text">Can't reach the status service. Retrying…</span>
    <button type="button" id="conn-retry">Try again</button>
  </div>

  <main id="main-content" tabindex="-1">
    <div class="shell view-stage" id="view-stage">

      <!-- ── View 1: Gateway / Status ──────────────────────────────────── -->
      <section class="view-panel" id="view-status" data-view="status"{{status_hidden}}>
        <section class="hero" aria-labelledby="status-headline">
          <div class="hero-copy rise" style="--i:0" data-gateway-state="unknown">
            <p class="status-line unknown" id="status-line">
              <span class="led warn pulse" aria-hidden="true"></span>
              <span id="status-label">Checking status</span>
            </p>
            <h1 id="status-headline">Connecting to the gateway…</h1>
            <p class="hero-sub" id="status-detail">Verifying upstream lossless engines and stream routing.</p>
            <p class="hero-meta">
              <span>Uptime <span id="uptime">—</span></span>
              <span class="meta-sep" aria-hidden="true">·</span>
              <span>Checked <time id="last-updated" datetime="">just now</time></span>
            </p>
            <p class="hero-specs" aria-label="Gateway audio capabilities">
              <span>24-bit / 192 kHz</span>
              <span class="meta-sep" aria-hidden="true">·</span>
              <span>Dolby Atmos</span>
              <span class="meta-sep" aria-hidden="true">·</span>
              <span>Pass-through</span>
            </p>
            <p class="sr-only" id="status-announcer" role="status" aria-live="polite" aria-atomic="true"></p>

            <aside class="connect-card" aria-labelledby="connect-title">
              <div class="connect-head">
                <div>
                  <h2 id="connect-title">Connect BitChord</h2>
                  <p>BitChord → Settings → Addons · Paste endpoint URL</p>
                </div>
                <span class="connect-note"><span id="addon-name">{{addon_name}}</span><span aria-hidden="true">·</span><span>v<span id="addon-version">{{addon_version}}</span></span></span>
              </div>
              <span class="field-label sr-only" id="addon-url-label">Addon URL</span>
              <div class="connect-bar" id="connect-bar">
                <span class="connect-cinema-sweep" aria-hidden="true"></span>
                <span class="url-field has-patch-dot" id="addon-url" role="textbox" tabindex="0" aria-readonly="true" aria-labelledby="addon-url-label">{{addon_url_display}}</span>
                <span class="connect-bridge" aria-hidden="true">
                  <span class="connect-bridge-track"></span>
                  <span class="connect-bridge-spark"></span>
                </span>
                <div class="connect-actions">
                  <button type="button" class="button primary connect-btn-copy" id="copy-btn"{{copy_disabled}}>
                    <span class="btn-cinema-fill" aria-hidden="true"></span>
                    <span class="btn-cinema-ring" aria-hidden="true"></span>
                    <svg class="copy-cinema-svg" width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      <path class="copy-sheet-back" d="M10.5 5.4V3.9A1.4 1.4 0 0 0 9.1 2.5H3.9A1.4 1.4 0 0 0 2.5 3.9v5.2a1.4 1.4 0 0 0 1.4 1.4h1.5" stroke="currentColor" stroke-width="1.45" stroke-linecap="round" />
                      <g class="copy-sheet-front">
                        <rect x="5.5" y="5.5" width="8" height="8" rx="1.7" stroke="currentColor" stroke-width="1.45" />
                        <path class="copy-doc-lines" d="M7.6 8.3h3.8M7.6 10.6h2.4" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" />
                      </g>
                      <path class="copy-check-path" d="M3.4 8.5L6.5 11.6L12.7 4.8" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" pathLength="16" />
                    </svg>
                    <span class="btn-label-viewport">
                      <span class="btn-label">Copy URL</span>
                    </span>
                  </button>
                  <span class="connect-seam" aria-hidden="true">
                    <span class="connect-seam-spark"></span>
                  </span>
                  <button type="button" class="button secondary connect-btn-qr" id="open-qr-btn" aria-haspopup="dialog">
                    <svg class="qr-cinema-svg" width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      <rect class="qr-finder qr-finder-tl" x="2.2" y="2.2" width="4.2" height="4.2" rx="0.8" stroke="currentColor" stroke-width="1.4" />
                      <rect class="qr-finder qr-finder-tr" x="9.6" y="2.2" width="4.2" height="4.2" rx="0.8" stroke="currentColor" stroke-width="1.4" />
                      <rect class="qr-finder qr-finder-bl" x="2.2" y="9.6" width="4.2" height="4.2" rx="0.8" stroke="currentColor" stroke-width="1.4" />
                      <path class="qr-data-bits" d="M9.6 9.6h1.7v1.7H9.6zm2.4 2.4h1.6v1.6h-1.6zm0-2.4h1.6M9.6 12.2v1.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                      <line class="qr-laser-scan" x1="1.6" y1="3" x2="14.4" y2="3" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" />
                    </svg>
                    <span class="btn-label">QR code</span>
                  </button>
                </div>
              </div>
            </aside>
          </div>

          <div class="hero-rail">
            <div class="spatial-window hero-stage-slot" data-spatial-slot="status" tabindex="0" role="region" aria-label="3D Signal Path">
              <div class="rack-3d-scene">
                <div class="rack-3d" id="status-rack-3d">
                  <div class="rack-source-row">
                    <button type="button" class="rack-listener spatial-node-pill" data-rack-node="listener" data-spatial-node="listener" aria-pressed="false" aria-label="Listener request origin">
                      <span class="rack-beam" aria-hidden="true">
                        <svg class="rack-beam-svg" overflow="visible">
                          <defs>
                            <linearGradient id="beam-grad-listener" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="44" y2="44">
                              <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0" />
                              <stop offset="30%" stop-color="#c4b5fd" stop-opacity="0.48" />
                              <stop offset="65%" stop-color="#f9a8d4" stop-opacity="0.82" />
                              <stop offset="88%" stop-color="#fef3c7" stop-opacity="0.96" />
                              <stop offset="100%" stop-color="#ffffff" stop-opacity="1" />
                            </linearGradient>
                          </defs>
                          <path class="rack-beam-glow" data-beam-glow d="" pathLength="200" stroke="url(#beam-grad-listener)" />
                          <path class="rack-beam-core" data-beam-core d="" pathLength="200" stroke="url(#beam-grad-listener)" />
                          <circle class="rack-beam-spark" data-beam-spark cx="0" cy="0" r="2.1" />
                        </svg>
                      </span>
                      <svg class="rack-listener-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <circle cx="12" cy="8.25" r="3.15" stroke="currentColor" stroke-width="1.6" />
                        <path d="M5.75 19.25a6.25 6.25 0 0 1 12.5 0" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
                      </svg>
                    </button>
                  </div>

                  <div class="rack-bridge is-source" aria-hidden="true">
                    <svg class="rack-bus rack-bus-source" viewBox="0 0 24 20" preserveAspectRatio="none">
                      <defs>
                        <linearGradient id="bus-grad-listener" gradientUnits="userSpaceOnUse" x1="12" y1="0" x2="12" y2="20">
                          <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0" />
                          <stop offset="30%" stop-color="#c4b5fd" stop-opacity="0.52" />
                          <stop offset="66%" stop-color="#f9a8d4" stop-opacity="0.86" />
                          <stop offset="90%" stop-color="#fef3c7" stop-opacity="0.96" />
                          <stop offset="100%" stop-color="#ffffff" stop-opacity="1" />
                        </linearGradient>
                      </defs>
                      <path class="rack-bus-track" d="M 12 -4 L 12 24" />
                      <path class="rack-bus-pulse" id="bus-pulse-listener" d="M 12 -1 L 12 21" pathLength="100" stroke="url(#bus-grad-listener)" />
                      <circle class="rack-bus-bead" id="bus-bead-listener" cx="12" cy="10" r="2.3" />
                    </svg>
                  </div>

                  <button type="button" class="rack-plane plane-client spatial-node-pill" data-rack-node="bitchord" data-spatial-node="bitchord" aria-pressed="false">
                    <span class="rack-beam" aria-hidden="true">
                      <svg class="rack-beam-svg" overflow="visible">
                        <defs>
                          <linearGradient id="beam-grad-bitchord" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="100" y2="0">
                            <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0" />
                            <stop offset="30%" stop-color="#c4b5fd" stop-opacity="0.48" />
                            <stop offset="65%" stop-color="#f9a8d4" stop-opacity="0.82" />
                            <stop offset="88%" stop-color="#fef3c7" stop-opacity="0.96" />
                            <stop offset="100%" stop-color="#ffffff" stop-opacity="1" />
                          </linearGradient>
                        </defs>
                        <path class="rack-beam-glow" data-beam-glow d="" pathLength="200" stroke="url(#beam-grad-bitchord)" />
                        <path class="rack-beam-core" data-beam-core d="" pathLength="200" stroke="url(#beam-grad-bitchord)" />
                        <circle class="rack-beam-spark" data-beam-spark cx="0" cy="0" r="2.4" />
                      </svg>
                    </span>
                    <div class="rack-node-row">
                      <span class="rack-node-icon is-bitchord" aria-hidden="true">
                        <svg class="brand-glyph brand-bitchord" width="20" height="14" viewBox="0 0 730 484" fill="currentColor" aria-hidden="true">
                          <path d="M6.73729,316.937C20.201,294.159 49.6986,286.649 72.4033,300.174H72.4277C237.603,398.282 510.121,407.302 667.823,359.439C693.147,351.759 719.927,366.055 727.602,391.378C735.276,416.715 720.992,443.477 695.649,451.176C514.618,506.132 213.674,495.514 23.5058,382.622C0.678772,369.103 -6.78757,339.685 6.73729,316.937ZM77.8135,154.865C89.38,136.046 113.982,130.11 132.77,141.676C270.528,226.369 480.519,250.891 643.46,201.43C664.592,195.047 686.911,206.957 693.324,228.052C699.689,249.184 687.774,271.46 666.679,277.886C480.555,334.36 249.169,307.004 90.9707,209.79C72.1827,198.224 66.247,173.628 77.8135,154.865ZM140.542,15.3012C149.722,0.246392 169.428,-4.52761 184.482,4.71335C304.811,78.2128 456.29,94.8593 634.684,54.101C651.874,50.1843 669.01,60.9551 672.927,78.1518C676.862,95.3486 666.134,112.484 648.9,116.401C453.677,161.021 286.219,141.799 151.129,59.2417C136.074,50.0006 131.301,30.3561 140.542,15.3012Z" />
                        </svg>
                      </span>
                      <span class="rack-node-copy">
                        <strong class="rack-plane-title">BitChord</strong>
                        <span class="rack-plane-sub">Client request</span>
                      </span>
                      <span class="rack-plane-badge" data-rack-client-badge>HTTP v2</span>
                    </div>
                  </button>

                  <div class="rack-bridge" aria-hidden="true">
                    <svg class="rack-bus rack-bus-top" viewBox="0 0 24 28" preserveAspectRatio="none">
                      <defs>
                        <linearGradient id="bus-grad-top" gradientUnits="userSpaceOnUse" x1="12" y1="0" x2="12" y2="28">
                          <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0" />
                          <stop offset="30%" stop-color="#c4b5fd" stop-opacity="0.52" />
                          <stop offset="66%" stop-color="#f9a8d4" stop-opacity="0.86" />
                          <stop offset="90%" stop-color="#fef3c7" stop-opacity="0.96" />
                          <stop offset="100%" stop-color="#ffffff" stop-opacity="1" />
                        </linearGradient>
                      </defs>
                      <path class="rack-bus-track" d="M 12 -4 L 12 32" />
                      <path class="rack-bus-pulse" id="bus-pulse-top" d="M 12 -1 L 12 29" pathLength="100" stroke="url(#bus-grad-top)" />
                      <circle class="rack-bus-bead" id="bus-bead-top" cx="12" cy="14" r="2.5" />
                    </svg>
                  </div>

                  <button type="button" class="rack-plane plane-core spatial-node-pill" data-rack-node="confih" data-spatial-node="confih" aria-pressed="false">
                    <span class="rack-beam" aria-hidden="true">
                      <svg class="rack-beam-svg" overflow="visible">
                        <defs>
                          <linearGradient id="beam-grad-confih" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="100" y2="0">
                            <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0" />
                            <stop offset="30%" stop-color="#c4b5fd" stop-opacity="0.48" />
                            <stop offset="65%" stop-color="#f9a8d4" stop-opacity="0.82" />
                            <stop offset="88%" stop-color="#fef3c7" stop-opacity="0.96" />
                            <stop offset="100%" stop-color="#ffffff" stop-opacity="1" />
                          </linearGradient>
                        </defs>
                        <path class="rack-beam-glow" data-beam-glow d="" pathLength="200" stroke="url(#beam-grad-confih)" />
                        <path class="rack-beam-core" data-beam-core d="" pathLength="200" stroke="url(#beam-grad-confih)" />
                        <circle class="rack-beam-spark" data-beam-spark cx="0" cy="0" r="2.4" />
                      </svg>
                    </span>
                    <div class="rack-node-row">
                      <span class="rack-node-icon is-core" aria-hidden="true">
                        <svg class="brand-glyph brand-confih" width="22" height="22" viewBox="0 0 24 24" fill="none" shape-rendering="geometricPrecision" aria-hidden="true">
                          <defs>
                            <linearGradient id="rackConfihArc" x1="3" y1="3" x2="18" y2="21" gradientUnits="userSpaceOnUse">
                              <stop offset="0%" stop-color="#ff8fa3" />
                              <stop offset="45%" stop-color="#fa233b" />
                              <stop offset="100%" stop-color="#be123c" />
                            </linearGradient>
                          </defs>
                          <path d="M16.4 5.2a8.2 8.2 0 1 0 0 13.6" stroke="url(#rackConfihArc)" stroke-width="2.3" stroke-linecap="round" />
                          <path d="M16.2 5.5l1.6 1.2M16.2 18.5l1.6-1.2" stroke="#e4e4e7" stroke-width="1.35" stroke-linecap="round" />
                          <g transform="translate(18.6 8.3) rotate(40)">
                            <rect x="-2.6" y="-1.7" width="5.2" height="3.1" rx="1.3" fill="#fa233b" stroke="#ffdbe2" stroke-width="0.45" />
                            <rect x="-2.2" y="1.1" width="4.4" height="1.2" rx="0.6" fill="#27272a" />
                          </g>
                          <g transform="translate(18.6 15.7) rotate(-40)">
                            <rect x="-2.6" y="-1.4" width="5.2" height="3.1" rx="1.3" fill="#e11d48" stroke="#ffdbe2" stroke-width="0.45" />
                            <rect x="-2.2" y="-2.3" width="4.4" height="1.2" rx="0.6" fill="#27272a" />
                          </g>
                        </svg>
                      </span>
                      <span class="rack-node-copy">
                        <strong class="rack-plane-title">Confih Gateway</strong>
                        <span class="rack-plane-sub">Direct pass-through</span>
                      </span>
                      <span class="rack-plane-status">
                        <span class="led" data-rack-core-led aria-hidden="true"></span>
                        <span class="rack-plane-badge is-core" data-rack-core-badge>Routing</span>
                      </span>
                    </div>
                  </button>

                  <div class="rack-bridge is-split" aria-hidden="true">
                    <svg class="rack-bus rack-bus-split" viewBox="0 0 376 34" preserveAspectRatio="none">
                      <defs>
                        <linearGradient id="bus-grad-qobuz" gradientUnits="userSpaceOnUse" x1="188" y1="0" x2="91" y2="34">
                          <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0" />
                          <stop offset="30%" stop-color="#c4b5fd" stop-opacity="0.52" />
                          <stop offset="66%" stop-color="#f9a8d4" stop-opacity="0.86" />
                          <stop offset="90%" stop-color="#fef3c7" stop-opacity="0.96" />
                          <stop offset="100%" stop-color="#ffffff" stop-opacity="1" />
                        </linearGradient>
                        <linearGradient id="bus-grad-tidal" gradientUnits="userSpaceOnUse" x1="188" y1="0" x2="285" y2="34">
                          <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0" />
                          <stop offset="30%" stop-color="#c4b5fd" stop-opacity="0.52" />
                          <stop offset="66%" stop-color="#f9a8d4" stop-opacity="0.86" />
                          <stop offset="90%" stop-color="#fef3c7" stop-opacity="0.96" />
                          <stop offset="100%" stop-color="#ffffff" stop-opacity="1" />
                        </linearGradient>
                      </defs>
                      <path class="rack-bus-track" d="M 188 -4 L 188 9 Q 188 17 180 17 L 99 17 Q 91 17 91 25 L 91 38" />
                      <path class="rack-bus-track" d="M 188 -4 L 188 9 Q 188 17 196 17 L 277 17 Q 285 17 285 25 L 285 38" />
                      <path class="rack-bus-pulse" id="bus-pulse-qobuz" d="M 188 -1 L 188 9 Q 188 17 180 17 L 99 17 Q 91 17 91 25 L 91 35" pathLength="100" stroke="url(#bus-grad-qobuz)" />
                      <path class="rack-bus-pulse" id="bus-pulse-tidal" d="M 188 -1 L 188 9 Q 188 17 196 17 L 277 17 Q 285 17 285 25 L 285 35" pathLength="100" stroke="url(#bus-grad-tidal)" />
                      <circle class="rack-bus-bead" id="bus-bead-qobuz" cx="91" cy="17" r="2.5" />
                      <circle class="rack-bus-bead" id="bus-bead-tidal" cx="285" cy="17" r="2.5" />
                    </svg>
                  </div>

                  <div class="rack-plane plane-providers">
                    <button type="button" class="rack-chip spatial-node-pill" data-rack-node="qobuz" data-spatial-node="qobuz" aria-pressed="false">
                      <span class="rack-beam" aria-hidden="true">
                        <svg class="rack-beam-svg" overflow="visible">
                          <defs>
                            <linearGradient id="beam-grad-qobuz" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="100" y2="0">
                              <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0" />
                              <stop offset="30%" stop-color="#c4b5fd" stop-opacity="0.48" />
                              <stop offset="65%" stop-color="#f9a8d4" stop-opacity="0.82" />
                              <stop offset="88%" stop-color="#fef3c7" stop-opacity="0.96" />
                              <stop offset="100%" stop-color="#ffffff" stop-opacity="1" />
                            </linearGradient>
                          </defs>
                          <path class="rack-beam-glow" data-beam-glow d="" pathLength="200" stroke="url(#beam-grad-qobuz)" />
                          <path class="rack-beam-core" data-beam-core d="" pathLength="200" stroke="url(#beam-grad-qobuz)" />
                          <circle class="rack-beam-spark" data-beam-spark cx="0" cy="0" r="2.4" />
                        </svg>
                      </span>
                      <div class="rack-chip-head">
                        <span class="rack-chip-key is-qobuz" aria-hidden="true">
                          <svg class="brand-glyph brand-qobuz" width="18" height="18" viewBox="0 0 24 24" fill="none" shape-rendering="geometricPrecision" aria-hidden="true">
                            <path fill-rule="evenodd" clip-rule="evenodd" d="M11.2 3a8.2 8.2 0 1 0 4.53 15.04l2.92 2.91a1.15 1.15 0 0 0 1.63-1.63l-2.91-2.92A8.2 8.2 0 0 0 11.2 3zm0 2.6a5.6 5.6 0 1 0 0 11.2 5.6 5.6 0 0 0 0-11.2zm0 2.55a3.05 3.05 0 1 1 0 6.1 3.05 3.05 0 0 1 0-6.1z" fill="currentColor" />
                            <circle cx="11.2" cy="11.2" r="1.15" fill="currentColor" />
                          </svg>
                        </span>
                        <span class="led" data-rack-qobuz-led aria-hidden="true"></span>
                      </div>
                      <strong class="rack-chip-title">Qobuz</strong>
                      <span class="rack-chip-sub" data-rack-qobuz-meta>FLAC Hi-Res</span>
                    </button>
                    <button type="button" class="rack-chip spatial-node-pill" data-rack-node="tidal" data-spatial-node="tidal" aria-pressed="false">
                      <span class="rack-beam" aria-hidden="true">
                        <svg class="rack-beam-svg" overflow="visible">
                          <defs>
                            <linearGradient id="beam-grad-tidal" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="100" y2="0">
                              <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0" />
                              <stop offset="30%" stop-color="#c4b5fd" stop-opacity="0.48" />
                              <stop offset="65%" stop-color="#f9a8d4" stop-opacity="0.82" />
                              <stop offset="88%" stop-color="#fef3c7" stop-opacity="0.96" />
                              <stop offset="100%" stop-color="#ffffff" stop-opacity="1" />
                            </linearGradient>
                          </defs>
                          <path class="rack-beam-glow" data-beam-glow d="" pathLength="200" stroke="url(#beam-grad-tidal)" />
                          <path class="rack-beam-core" data-beam-core d="" pathLength="200" stroke="url(#beam-grad-tidal)" />
                          <circle class="rack-beam-spark" data-beam-spark cx="0" cy="0" r="2.4" />
                        </svg>
                      </span>
                      <div class="rack-chip-head">
                        <span class="rack-chip-key is-tidal" aria-hidden="true">
                          <svg class="brand-glyph brand-tidal" width="18" height="18" viewBox="0 0 24 24" fill="currentColor" shape-rendering="geometricPrecision" aria-hidden="true">
                            <rect x="3.5" y="7.1" width="3.8" height="3.8" rx="0.3" transform="rotate(45 5.4 9)" />
                            <rect x="10.1" y="7.1" width="3.8" height="3.8" rx="0.3" transform="rotate(45 12 9)" />
                            <rect x="16.7" y="7.1" width="3.8" height="3.8" rx="0.3" transform="rotate(45 18.6 9)" />
                            <rect x="10.1" y="13.7" width="3.8" height="3.8" rx="0.3" transform="rotate(45 12 15.6)" />
                          </svg>
                        </span>
                        <span class="led" data-rack-tidal-led aria-hidden="true"></span>
                      </div>
                      <strong class="rack-chip-title">Tidal</strong>
                      <span class="rack-chip-sub" data-rack-tidal-meta>Dolby Atmos · FLAC</span>
                    </button>
                  </div>
                </div>
              </div>
              <p class="spatial-inspect-readout" id="spatial-inspect-readout" aria-live="polite">Checking upstream connectivity…</p>
            </div>
          </div>
        </section>

        <section class="section rise" id="providers" style="--i:1" aria-labelledby="providers-title">
          <div class="section-head">
            <div>
              <span class="label">Engines</span>
              <h2 id="providers-title">Providers</h2>
              <p id="routing-note">Checking which engines are serving requests.</p>
            </div>
            <p class="section-side failover-pill" id="failover-note" data-failover="unknown"></p>
          </div>

          <ul class="provider-list">
            <li class="provider-row" id="qobuz-card" data-engine-state="unconfigured">
              <div class="provider-ident">
                <span class="monogram" aria-hidden="true">
                  <svg class="brand-glyph brand-qobuz" width="23" height="23" viewBox="0 0 24 24" fill="none" shape-rendering="geometricPrecision" aria-hidden="true">
                    <path fill-rule="evenodd" clip-rule="evenodd" d="M11.2 3a8.2 8.2 0 1 0 4.53 15.04l2.92 2.91a1.15 1.15 0 0 0 1.63-1.63l-2.91-2.92A8.2 8.2 0 0 0 11.2 3zm0 2.6a5.6 5.6 0 1 0 0 11.2 5.6 5.6 0 0 0 0-11.2zm0 2.55a3.05 3.05 0 1 1 0 6.1 3.05 3.05 0 0 1 0-6.1z" fill="currentColor" />
                    <circle cx="11.2" cy="11.2" r="1.15" fill="currentColor" />
                  </svg>
                </span>
                <div>
                  <div class="provider-title-line">
                    <h3>Qobuz</h3>
                    <span class="provider-tier">FLAC 24-bit · 192 kHz</span>
                  </div>
                  <p id="qobuz-role">Checking…</p>
                </div>
              </div>
              <div class="provider-state">
                <span class="state unconfigured" id="qobuz-state">Checking</span>
              </div>
              <dl class="provider-stats">
                <div class="provider-stat provider-quality">
                  <dt>Quality</dt>
                  <dd id="qobuz-quality">—</dd>
                </div>
                <div class="provider-stat">
                  <dt>Latency</dt>
                  <dd id="qobuz-response">—</dd>
                </div>
                <div class="provider-stat">
                  <dt>Streams</dt>
                  <dd id="qobuz-streams">—</dd>
                </div>
              </dl>
              <div class="provider-meter" aria-hidden="true">
                <div class="provider-meter-track">
                  <span class="provider-meter-fill" id="qobuz-share-bar"></span>
                </div>
                <span class="provider-meter-pct" id="qobuz-share-pct">—</span>
              </div>
            </li>
            <li class="provider-row" id="tidal-card" data-engine-state="unconfigured">
              <div class="provider-ident">
                <span class="monogram" aria-hidden="true">
                  <svg class="brand-glyph brand-tidal" width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                    <rect x="3.5" y="7.1" width="3.8" height="3.8" rx="0.3" transform="rotate(45 5.4 9)" />
                    <rect x="10.1" y="7.1" width="3.8" height="3.8" rx="0.3" transform="rotate(45 12 9)" />
                    <rect x="16.7" y="7.1" width="3.8" height="3.8" rx="0.3" transform="rotate(45 18.6 9)" />
                    <rect x="10.1" y="13.7" width="3.8" height="3.8" rx="0.3" transform="rotate(45 12 15.6)" />
                  </svg>
                </span>
                <div>
                  <div class="provider-title-line">
                    <h3>Tidal</h3>
                    <span class="provider-tier">Dolby Atmos · Master FLAC</span>
                  </div>
                  <p id="tidal-role">Checking…</p>
                </div>
              </div>
              <div class="provider-state">
                <span class="state unconfigured" id="tidal-state">Checking</span>
              </div>
              <dl class="provider-stats">
                <div class="provider-stat provider-quality">
                  <dt>Quality</dt>
                  <dd id="tidal-quality">—</dd>
                </div>
                <div class="provider-stat">
                  <dt>Latency</dt>
                  <dd id="tidal-response">—</dd>
                </div>
                <div class="provider-stat">
                  <dt>Streams</dt>
                  <dd id="tidal-streams">—</dd>
                </div>
              </dl>
              <div class="provider-meter" aria-hidden="true">
                <div class="provider-meter-track">
                  <span class="provider-meter-fill" id="tidal-share-bar"></span>
                </div>
                <span class="provider-meter-pct" id="tidal-share-pct">—</span>
              </div>
            </li>
          </ul>
        </section>

        <section class="section rise" id="reliability" style="--i:2" aria-labelledby="reliability-title">
          <div class="section-head">
            <div>
              <span class="label">Uptime</span>
              <h2 id="reliability-title">Reliability</h2>
              <p id="timeline-summary">Waiting for the first status update.</p>
            </div>
            <div class="timeline-stat" id="timeline-stat" data-health="unknown">
              <strong id="success-rate">—</strong>
              <span id="success-label">24h ok</span>
            </div>
          </div>

          <div class="uptime-card" id="uptime-card" data-health="unknown">
            <div class="uptime-row">
              <div class="uptime-row-head">
                <span class="uptime-row-title">Last 24 hours</span>
                <span class="chart-readout" id="chart-readout" role="status" aria-live="polite">Hourly API health</span>
              </div>
              <div class="bars-stage">
                <div class="bars" id="timeline-bars" role="group" aria-label="Request history for the last 24 hours, one bar per hour"></div>
                <div class="chart-axis" aria-hidden="true">
                  <span>24h ago</span>
                  <span>12h ago</span>
                  <span>Now</span>
                </div>
              </div>
            </div>

            <div class="uptime-divider"></div>

            <div class="uptime-row" id="reliability-30d">
              <div class="uptime-row-head">
                <span class="uptime-row-title">30-day history · <strong id="uptime-30d-pct">—</strong> <span class="uptime-muted" id="requests-30d"></span></span>
                <span class="chart-readout" id="timeline-30d-readout" role="status" aria-live="polite">Daily uptime</span>
              </div>
              <div class="bars-30d-stage">
                <div class="bars-30d" id="timeline-30d-bars" role="group" aria-label="30-day daily reliability"></div>
                <div class="chart-axis chart-axis-30d" aria-hidden="true">
                  <span>30d ago</span>
                  <span>15d ago</span>
                  <span>Today</span>
                </div>
              </div>
            </div>

            <div class="incidents-wrap" id="incidents-wrap" hidden>
              <div class="incidents-head">
                <span class="incidents-title">Recent routing &amp; health events</span>
              </div>
              <div class="incidents-list" id="incidents-list"></div>
            </div>
          </div>
        </section>

        <section class="section rise" id="usage" style="--i:3" aria-labelledby="usage-title">
          <div class="section-head">
            <div>
              <span class="label">Telemetry</span>
              <h2 id="usage-title">Traffic &amp; quality</h2>
              <p>Cumulative stream resolution and lossless format mix across sessions.</p>
            </div>
            <p class="section-side quality-pill" id="quality-note">Since last start</p>
          </div>

          <div class="metrics" id="usage-metrics">
            <div class="metric" style="--m-i:0" data-metric-tone="neutral">
              <div class="metric-head">
                <p class="metric-value" id="m-streams">—</p>
                <span class="metric-icon" aria-hidden="true">
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <path d="M2.5 9.5v-3M5.5 11.8V4.2M8.5 13.2V2.8M11.5 10.8V5.2M14 8.8V7.2" stroke="currentColor" stroke-width="1.45" stroke-linecap="round" />
                  </svg>
                </span>
              </div>
              <p class="metric-label">Streams served</p>
              <p class="metric-note" id="m-streams-note">All-time resolved</p>
            </div>
            <div class="metric" style="--m-i:1" data-metric-tone="gold">
              <div class="metric-head">
                <p class="metric-value" id="m-hires">—</p>
                <span class="metric-icon" aria-hidden="true">
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <path d="M8 2.2l1.65 3.75L13.4 7.6 9.65 9.25 8 13l-1.65-3.75L2.6 7.6l3.75-1.65L8 2.2z" stroke="currentColor" stroke-width="1.35" stroke-linejoin="round" />
                  </svg>
                </span>
              </div>
              <p class="metric-label">Hi-Res &amp; Atmos</p>
              <p class="metric-note" id="m-hires-note">Studio-master share</p>
            </div>
            <div class="metric" style="--m-i:2" data-metric-tone="emerald" id="metric-card-clients">
              <div class="metric-head">
                <p class="metric-value" id="m-clients">—</p>
                <span class="metric-icon" aria-hidden="true">
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <circle cx="8" cy="5.5" r="2.3" stroke="currentColor" stroke-width="1.4" />
                    <path d="M3.6 13.2c.6-2.1 2.2-3.3 4.4-3.3s3.8 1.2 4.4 3.3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                  </svg>
                </span>
              </div>
              <p class="metric-label">Active clients</p>
              <p class="metric-note" id="m-clients-note"></p>
            </div>
            <div class="metric" style="--m-i:3" data-metric-tone="cyan">
              <div class="metric-head">
                <p class="metric-value" id="m-response">—</p>
                <span class="metric-icon" aria-hidden="true">
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <circle cx="8" cy="8.5" r="5.3" stroke="currentColor" stroke-width="1.4" />
                    <path d="M8 8.5l2.4-2.1M6.3 1.8h3.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                  </svg>
                </span>
              </div>
              <p class="metric-label">Avg latency</p>
              <p class="metric-note" id="m-response-note">Upstream pass-through</p>
            </div>
          </div>

          <div class="quality" id="quality-card">
            <div class="quality-head">
              <span class="quality-head-title">Codec &amp; bitrate distribution</span>
              <span class="chart-readout" id="quality-hover-readout" role="status" aria-live="polite">4 tiers tracked</span>
            </div>
            <div class="quality-stage">
              <div class="quality-bar" id="quality-bar" role="img" aria-label="Stream quality mix is loading">
                <span class="atmos" id="q-atmos" data-tier="atmos" style="--seg-i:0"></span>
                <span class="hires" id="q-hires" data-tier="hires" style="--seg-i:1"></span>
                <span class="cd" id="q-cd" data-tier="cd" style="--seg-i:2"></span>
                <span class="high" id="q-high" data-tier="high" style="--seg-i:3"></span>
              </div>
            </div>
            <ul class="quality-legend" id="quality-legend">
              <li data-tier="atmos" style="--tier-i:0" tabindex="0"><span class="legend-swatch" aria-hidden="true"></span><span class="legend-name">Dolby Atmos</span><b id="qt-atmos">—</b><span class="pct" id="qp-atmos">—</span></li>
              <li data-tier="hires" style="--tier-i:1" tabindex="0"><span class="legend-swatch" aria-hidden="true"></span><span class="legend-name">24-bit Hi-Res</span><b id="qt-hires">—</b><span class="pct" id="qp-hires">—</span></li>
              <li data-tier="cd" style="--tier-i:2" tabindex="0"><span class="legend-swatch" aria-hidden="true"></span><span class="legend-name">CD Lossless</span><b id="qt-cd">—</b><span class="pct" id="qp-cd">—</span></li>
              <li data-tier="high" style="--tier-i:3" tabindex="0"><span class="legend-swatch" aria-hidden="true"></span><span class="legend-name">320 kbps</span><b id="qt-high">—</b><span class="pct" id="qp-high">—</span></li>
            </ul>
          </div>
        </section>
      </section>

      {{plays_section}}

      <!-- ── View 3: Consolidated About & Support ──────────────────────── -->
      <section class="view-panel" id="view-about" data-view="about"{{about_hidden}}>
        <section class="page-hero rise" style="--i:0" aria-labelledby="about-headline">
          <div>
            <span class="eyebrow"><span class="led signal pulse" aria-hidden="true"></span>About</span>
            <h1 id="about-headline">A small gateway with one job.</h1>
            <p class="lede">Confih connects BitChord to the music providers it already uses. It does not host music, create accounts or track listeners.</p>
            <div class="hero-chips">
              <span class="chip">No accounts</span>
              <span class="chip">No tracking</span>
              <span class="chip">No ads</span>
            </div>
          </div>
          <div class="spatial-window" id="about-signal-plate" data-spatial-slot="info" aria-label="Signal path 3D schematic">
            <div class="arch-3d-stack" tabindex="0" role="group" aria-label="3D Exploded Protocol Stack">
              <div class="arch-plate arch-plate-1" data-arch-layer="1">
                <div class="arch-plate-head">
                  <span class="arch-plate-index">01 · Match &amp; Verify</span>
                  <span class="arch-plate-badge">ISRC · Pool</span>
                </div>
                <p class="arch-plate-copy">Checks configured Qobuz and Tidal pools and verifies bit-depth before failover.</p>
              </div>
              <div class="arch-plate arch-plate-2 is-accent" data-arch-layer="2">
                <div class="arch-plate-head">
                  <span class="arch-plate-index">02 · Direct Lossless Pass-Through</span>
                  <span class="arch-plate-badge is-signal">24/192 · Atmos</span>
                </div>
                <p class="arch-plate-copy">Returns signed upstream FLAC and Dolby Atmos URLs with zero transcoding.</p>
              </div>
              <div class="arch-plate arch-plate-3" data-arch-layer="3">
                <div class="arch-plate-head">
                  <span class="arch-plate-index">03 · First-Party Privacy Shield</span>
                  <span class="arch-plate-badge is-ok">Zero Third-Party Calls</span>
                </div>
                <p class="arch-plate-copy">Local SVG QR codes, self-hosted fonts, and proxied covers under strict 'self' CSP.</p>
              </div>
            </div>
          </div>
        </section>

        <section class="block rise" style="--i:1" aria-labelledby="does-title">
          <div class="block-head">
            <span class="label">How it works</span>
            <h2 id="does-title">What it does</h2>
            <p>Three steps, every time you play a track.</p>
          </div>
          <div class="steps">
            <article class="step" data-tilt>
              <span class="step-index" aria-hidden="true">01</span>
              <h3>Search</h3>
              <p>BitChord asks the gateway for a track. The gateway checks the providers it is configured to use.</p>
            </article>
            <article class="step" data-tilt>
              <span class="step-index" aria-hidden="true">02</span>
              <h3>Resolve</h3>
              <p>It picks the best available match and quality, then falls back to the other provider only when the match is safe.</p>
            </article>
            <article class="step" data-tilt>
              <span class="step-index" aria-hidden="true">03</span>
              <h3>Stream</h3>
              <p>BitChord receives a playable URL with honest codec, bit-depth and sample-rate metadata. Nothing is transcoded.</p>
            </article>
          </div>
        </section>

        <section class="block rise" style="--i:2" aria-labelledby="privacy-title">
          <div class="split">
            <div>
              <div class="block-head">
                <span class="label">Data</span>
                <h2 id="privacy-title">Privacy</h2>
              </div>
              <div class="prose">
                <p>The public status and play feeds contain aggregate counts: active clients, streams, errors, ranked tracks and quality totals. Neither contains account names, email addresses or a per-listener history.</p>
                <p>On the Plays page, timestamps are rounded to the minute and listener sessions are deduplicated with an in-memory salted hash that is dropped on restart (so a returning listener counts again after a restart). Album covers and QR codes are served or generated locally so your browser makes zero third-party requests.</p>
                <p>The gateway needs provider credentials to work, but those stay on the server. When you play a track, the stream request still goes to Qobuz or Tidal under their own terms and privacy policies.</p>
              </div>
            </div>
            <div class="privacy-stack">
              <aside class="note-plate">
                <h3>No advertising or analytics profiles</h3>
                <p>There are no ads, no third-party trackers and no cross-site profiles on this page.</p>
              </aside>
              <aside class="note-plate">
                <h3>What the status page can see</h3>
                <p>Aggregate counters only. Nothing here identifies a listener, a device or a request path.</p>
              </aside>
            </div>
          </div>
        </section>

        <section class="block rise" style="--i:3" aria-labelledby="independent-title">
          <div class="block-head">
            <span class="label">Independence</span>
            <h2 id="independent-title">Independent by design</h2>
          </div>
          <p class="statement">This is a personal gateway, <em>not an official service</em>.</p>
          <div class="split">
            <div class="prose">
              <p>Provider names and marks belong to their owners. Quality claims come from what the provider returns, not from marketing copy.</p>
            </div>
            <div class="prose">
              <p>The gateway is free to use. Donations are optional and never unlock extra access. If something is broken, the status page and the repository are the honest places to look{{contact_lead}}</p>
              <div class="cta-row">
                <a class="button primary" href="{{home_href}}" data-nav-view="status">View live status</a>
                <a class="button secondary" href="/support" data-nav-view="support">Support the gateway</a>
                <a class="button quiet" href="https://github.com/confnameless/confih-addon" target="_blank" rel="noopener noreferrer">Gateway source</a>
                <a class="button quiet" href="https://github.com/kushagrasinghx/BitChord" target="_blank" rel="noopener noreferrer">BitChord on GitHub</a>
              </div>
            </div>
          </div>
        </section>

        <section class="block rise" style="--i:4" aria-labelledby="tech-title">
          <div class="block-head">
            <span class="label">Capabilities</span>
            <h2 id="tech-title">Under the hood</h2>
            <p>Protocol guarantees built for BitChord.</p>
          </div>
          <ul class="stack">
            <li>BitChord HTTP v2</li>
            <li>24-bit / 192 kHz</li>
            <li>Dolby Atmos</li>
            <li>Zero Transcoding</li>
            <li>Auto-Failover</li>
            <li>First-Party Art Proxy</li>
          </ul>
          <p class="tech-next">
            <span class="led signal" aria-hidden="true"></span>
            Qobuz and Tidal today. The addon core is provider-agnostic, so more platforms are
            planned — no dates, no promises.
          </p>
          <div class="cta-row">
            <a class="button primary" href="/support" data-nav-view="support">Support the gateway</a>
          </div>
        </section>
      </section>

      <!-- ── View 4: Support ───────────────────────────────────────────── -->
      <section class="view-panel" id="view-support" data-view="support"{{support_hidden}}>
        <!-- Coin marks are inline symbols: nothing on this page leaves the host. -->
        <svg class="coin-sprite" aria-hidden="true" focusable="false" width="0" height="0">
          <defs>
            <linearGradient id="coin-sol-grad" x1="0" y1="1" x2="1" y2="0">
              <stop offset="0" stop-color="#9945ff" />
              <stop offset="1" stop-color="#14f195" />
            </linearGradient>
            <linearGradient id="coin-poly-grad" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stop-color="#7b3fe4" />
              <stop offset="1" stop-color="#b48bf7" />
            </linearGradient>
          </defs>
          <symbol id="coin-btc" viewBox="0 0 32 32">
            <circle cx="16" cy="16" r="16" fill="#f7931a" />
            <text x="16" y="16.6" text-anchor="middle" dominant-baseline="central" font-family="DejaVu Sans, ui-sans-serif, system-ui, sans-serif" font-size="15.5" font-weight="700" fill="#ffffff">₿</text>
          </symbol>
          <symbol id="coin-sol" viewBox="0 0 32 32">
            <circle cx="16" cy="16" r="16" fill="#101014" />
            <g fill="url(#coin-sol-grad)">
              <polygon points="11,8.6 22.4,8.6 19.9,12.2 8.5,12.2" />
              <polygon points="8.5,14.2 19.9,14.2 22.4,17.8 11,17.8" />
              <polygon points="11,19.8 22.4,19.8 19.9,23.4 8.5,23.4" />
            </g>
          </symbol>
          <symbol id="coin-polygon" viewBox="0 0 32 32">
            <circle cx="16" cy="16" r="16" fill="url(#coin-poly-grad)" />
            <path d="M16 7.4 23.5 11.7v8.6L16 24.6 8.5 20.3v-8.6z" fill="none" stroke="#ffffff" stroke-width="1.5" stroke-linejoin="round" opacity="0.95" />
            <path d="M16 12.2 19.7 14.3v4.4L16 20.8 12.3 18.7v-4.4z" fill="#ffffff" opacity="0.55" />
          </symbol>
          <symbol id="coin-usdc" viewBox="0 0 32 32">
            <circle cx="16" cy="16" r="16" fill="#2775ca" />
            <circle cx="16" cy="16" r="11.2" fill="none" stroke="#ffffff" stroke-width="1.35" opacity="0.8" />
            <text x="16" y="16.6" text-anchor="middle" dominant-baseline="central" font-family="DejaVu Sans, ui-sans-serif, system-ui, sans-serif" font-size="14" font-weight="700" fill="#ffffff">$</text>
          </symbol>
          <symbol id="coin-usdt" viewBox="0 0 32 32">
            <circle cx="16" cy="16" r="16" fill="#26a17b" />
            <path d="M9.4 11.2h13.2" stroke="#ffffff" stroke-width="2.4" stroke-linecap="round" />
            <path d="M16 11.2v11.2" stroke="#ffffff" stroke-width="2.4" stroke-linecap="round" />
          </symbol>
          <symbol id="coin-eth" viewBox="0 0 32 32">
            <circle cx="16" cy="16" r="16" fill="#627eea" />
            <path d="M16 7.2 23.9 16.3 16 21 8.1 16.3z" fill="#ffffff" opacity="0.92" />
            <path d="M16 22.7 23.9 18.1 16 25.4 8.1 18.1z" fill="#ffffff" opacity="0.55" />
          </symbol>
          <symbol id="coffee-cup" viewBox="0 0 32 32">
            <rect width="32" height="32" rx="9" fill="#ffdd00" />
            <path d="M9.2 13.4h12.1v5.9a4.6 4.6 0 0 1-4.6 4.6h-2.9a4.6 4.6 0 0 1-4.6-4.6z" fill="#0b0b0d" />
            <path d="M21.3 14.6h1.6a2.5 2.5 0 0 1 0 5h-1.6" fill="none" stroke="#0b0b0d" stroke-width="1.9" />
            <path d="M12.6 9.1c-.9.9-.9 1.8 0 2.7M16.1 8.4c-.9.9-.9 1.8 0 2.7M19.6 9.1c-.9.9-.9 1.8 0 2.7" fill="none" stroke="#0b0b0d" stroke-width="1.5" stroke-linecap="round" opacity="0.85" />
          </symbol>
        </svg>

        <section class="page-hero rise" style="--i:0" id="donations-bay" aria-labelledby="support-title">
          <div>
            <span class="eyebrow"><span class="led signal pulse" aria-hidden="true"></span>Support</span>
            <h1 id="support-title">Keep the music flowing.</h1>
            <p class="lede">This gateway is one server, one modest bill and a lot of late evenings chasing provider changes. No ads, no accounts, nothing about you sold — just a quiet bridge between BitChord and the music you already pay for.</p>
            <div class="hero-chips">
              <span class="chip">No ads</span>
              <span class="chip">No trackers</span>
              <span class="chip">One person</span>
              <span class="chip">Card or crypto</span>
            </div>
          </div>
          <div class="support-side">
            <div class="card support-note">
              <h3>What a donation is</h3>
              <p>A contribution toward server time and bandwidth, from someone who got something out of this. Not a subscription, not a token, not a queue jump — the gateway behaves the same for everyone.</p>
            </div>
            <div class="card support-note is-quiet">
              <h3>Where it goes</h3>
              <ul class="support-uses">
                <li>Compute for search and stream resolution</li>
                <li>Egress through the provider proxies</li>
                <li>Persistent account state and backups</li>
                <li>Maintenance when a provider changes</li>
              </ul>
            </div>
          </div>
        </section>

        <section class="block rise support-story" style="--i:1" aria-labelledby="story-title">
          <div class="story-head">
            <span class="label">Why this exists</span>
            <h2 id="story-title">A note from the person who runs it</h2>
          </div>
          <div class="story-body">
            <p class="story-lead">Confih is not a company. It is one machine in a datacenter, a tunnel, two provider subscriptions and me, keeping the queue clear.</p>
            <p>Everything here happens after work: the fixes, the retries, watching a token rotate at two in the morning so your next track still plays. Nobody pays me for that, and I would keep doing it anyway. But the bill arrives whether anyone listens that week or not.</p>
            <p class="story-ask">If this gateway has been good to your library — if it found you an album you thought was gone, or saved you a subscription — be good to it once. There is no plan to cancel later, and nothing extra you can buy.</p>
            <p class="story-sign">— Confih <span>one listener, not a company</span></p>
          </div>
        </section>

        <div class="donations-bay" tabindex="-1">
          <section class="block rise" style="--i:2" aria-labelledby="addresses-title">
            <div class="block-head">
              <span class="label">Donations</span>
              <h2 id="addresses-title">Two ways to send it</h2>
              <p>A card or wallet you already have, one transfer, nothing recurring. If you use crypto, send only the asset and network listed beside the address — those transfers are irreversible.</p>
            </div>

            {{coffee_panel}}

            <p class="support-demo" id="support-demo" hidden>Demo data — no live addresses are configured.</p>
            <p class="support-status" id="support-status" role="status" aria-live="polite">Loading donation details…</p>
            <div class="asset-list" id="support-assets"></div>
            <p class="support-impact" id="support-impact" hidden></p>
          </section>

          <div id="support-covers-block"{{covers_hidden}}>
            <section class="block rise" style="--i:3" aria-labelledby="covers-title">
              <div class="block-head">
                <span class="label">Where it goes</span>
                <h2 id="covers-title">What the money does</h2>
                <p>Four things keep a small gateway alive. None of them are optional.</p>
              </div>
              <div class="steps">
                <article class="step">
                  <span class="step-index">Compute</span>
                  <h3>Every lookup</h3>
                  <p>Each track you play costs a search, a match and a signature on this machine.</p>
                </article>
                <article class="step">
                  <span class="step-index">Egress</span>
                  <h3>The audio itself</h3>
                  <p>Lossless streams pass through this server, and that traffic is the largest line on the bill.</p>
                </article>
                <article class="step">
                  <span class="step-index">State</span>
                  <h3>Nothing forgotten</h3>
                  <p>Accounts, statistics and small backups, so a restart never erases what you listened to.</p>
                </article>
                <article class="step">
                  <span class="step-index">Maintenance</span>
                  <h3>The invisible part</h3>
                  <p>Provider changes, expired tokens and new BitChord releases — the work nobody sees from the player.</p>
                </article>
              </div>
            </section>

            <section class="block rise" style="--i:4" aria-labelledby="expect-title">
              <div class="block-head">
                <span class="label">Expectations</span>
                <h2 id="expect-title">What they don't buy</h2>
              </div>
              <div class="prose">
                <p>Donations are not a subscription. There is no priority queue, no private endpoint and no donation token or credit system. The gateway behaves the same for everyone.</p>
                <p>There are also no perks that expose another listener's data, and no way to buy extra quality. Quality is limited by what the providers actually deliver. A thank-you is all you get, and it is genuine.</p>
              </div>
            </section>
          </div>

          <section class="block rise" style="--i:5" aria-labelledby="help-title">
            <div class="block-head">
              <span class="label">No cost</span>
              <h2 id="help-title">Other ways to help</h2>
              <p>Donations are optional. These cost nothing and are just as useful.</p>
            </div>
            <div class="cards">
              <a class="card card-link" data-tilt href="https://github.com/confnameless/confih-addon/issues" target="_blank" rel="noopener noreferrer">
                <h3>Report a problem <span class="arrow" aria-hidden="true">↗</span></h3>
                <p>Open an issue with a track ID and the quality you requested.</p>
              </a>
              <a class="card card-link" data-tilt href="https://github.com/confnameless/confih-addon" target="_blank" rel="noopener noreferrer">
                <h3>Star the gateway <span class="arrow" aria-hidden="true">↗</span></h3>
                <p>Help other BitChord listeners find this gateway.</p>
              </a>
              <a class="card card-link" data-tilt href="{{home_href}}" data-nav-view="status">
                <h3>Check live status <span class="arrow" aria-hidden="true">→</span></h3>
                <p>Inspect live provider health and 30-day gateway reliability.</p>
              </a>
            </div>
          </section>

          <section class="block rise" style="--i:6" aria-labelledby="faq-title">
            <div class="block-head">
              <span class="label">Common questions</span>
              <h2 id="faq-title">Questions</h2>
            </div>
            <div class="faq">
              <details class="faq-item">
                <summary>Do I need an account to donate?</summary>
                <div class="faq-body">No. These are plain crypto addresses. There is no sign-in, no email and no receipt tied to your identity.</div>
              </details>
              <details class="faq-item">
                <summary>Will I get priority or extra features?</summary>
                <div class="faq-body">No. Donations keep the service running for everyone. They do not change routing, quality or rate limits.</div>
              </details>
              <details class="faq-item">
                <summary>Which coin do I send, and on which network?</summary>
                <div class="faq-body">Pick the address whose list includes your coin: Bitcoin for BTC, the Solana address for SOL, USDC or USDT, the Polygon address for USDC or USDT. Never send a token to a chain that is not listed — it may be unrecoverable.</div>
              </details>
              <details class="faq-item">
                <summary>Is there a minimum, or a monthly thing?</summary>
                <div class="faq-body">There is no minimum and nothing recurring. One transfer, any size, once. The monthly server bill does not change based on who donates.</div>
              </details>
              <details class="faq-item">
                <summary>Are donations tax-deductible?</summary>
                <div class="faq-body">No. This is not a charity or a registered nonprofit. Treat a donation as a personal contribution with no tax benefit.</div>
              </details>
            </div>
          </section>

          <section class="block rise" style="--i:7" aria-labelledby="contact-title">
            <div class="block-head">
              <span class="label">Contact</span>
              <h2 id="contact-title">Reach a person</h2>
              <p>No ticket system and no chatbot. Two addresses, both read by the same human.</p>
            </div>
            <div class="contact-grid">{{contact_cards}}</div>
          </section>
        </div>
      </section>

    </div>
  </main>

  <footer class="site-foot">
    <div class="shell foot-inner">
      <div>
        <span class="foot-brand"><img class="foot-logo" src="/assets/confih-logo.svg?v={{v}}" alt="Confih" width="92" height="34" /></span>
        <p>BitChord is an open-source YouTube Music client. This is the Confih Hi-Res Gateway. Not affiliated with Qobuz, Tidal, YouTube or BitChord.{{abuse_line}}</p>
        <span class="foot-time">Last checked <time id="foot-updated" datetime="">—</time></span>
      </div>
      <div class="foot-links">
        <a href="{{home_href}}" data-nav-view="status">Status</a>
        {{plays_foot_link}}
        <a href="/about" data-nav-view="about">About</a>
        <a href="/support" data-nav-view="support">Support</a>
        <a href="https://github.com/confnameless/confih-addon" target="_blank" rel="noopener noreferrer">Source</a>
        <a href="/api/public-stats">Status JSON</a>
        {{plays_json_foot_link}}
      </div>
    </div>
  </footer>

  <div class="spatial-tooltip" id="spatial-node-tooltip" role="tooltip" hidden></div>

  <div class="modal-overlay" id="qr-modal" role="dialog" aria-modal="true" aria-labelledby="qr-title" aria-describedby="qr-help">
    <div class="modal-card">
      <button type="button" class="modal-close-btn" id="qr-close-btn" aria-label="Close QR code">×</button>
      <h2 id="qr-title">Add to BitChord</h2>
      <p id="qr-help">Scan this code with your phone camera, or copy the URL and paste it into BitChord.</p>
      <div class="qr-container">
        <span class="qr-reticle qr-reticle-tl" aria-hidden="true"></span>
        <span class="qr-reticle qr-reticle-tr" aria-hidden="true"></span>
        <span class="qr-reticle qr-reticle-bl" aria-hidden="true"></span>
        <span class="qr-reticle qr-reticle-br" aria-hidden="true"></span>
        <span class="qr-stage-scan" aria-hidden="true"></span>
        <div class="qr-svg-slot" id="qr-svg-slot" role="img" aria-label="QR code linking to the BitChord addon URL"></div>
        <p class="qr-msg err" id="qr-error" hidden>Could not generate the QR code. Copy the URL instead.</p>
      </div>
      <p class="modal-url-line" id="qr-url-line">—</p>
      <p class="qr-msg" id="qr-note" hidden>This gateway uses a private path for access control. The public page does not expose it, and no QR code is generated. Get the full addon URL from the operator.</p>
      <button class="button primary connect-btn-copy" id="copy-modal-btn" type="button"{{copy_disabled}}>
        <span class="btn-cinema-fill" aria-hidden="true"></span>
        <span class="btn-cinema-ring" aria-hidden="true"></span>
        <svg class="copy-cinema-svg" width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path class="copy-sheet-back" d="M10.5 5.4V3.9A1.4 1.4 0 0 0 9.1 2.5H3.9A1.4 1.4 0 0 0 2.5 3.9v5.2a1.4 1.4 0 0 0 1.4 1.4h1.5" stroke="currentColor" stroke-width="1.45" stroke-linecap="round" />
          <g class="copy-sheet-front">
            <rect x="5.5" y="5.5" width="8" height="8" rx="1.7" stroke="currentColor" stroke-width="1.45" />
            <path class="copy-doc-lines" d="M7.6 8.3h3.8M7.6 10.6h2.4" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" />
          </g>
          <path class="copy-check-path" d="M3.4 8.5L6.5 11.6L12.7 4.8" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" pathLength="16" />
        </svg>
        <span class="btn-label">Copy addon URL</span>
      </button>
      <p class="modal-foot">BitChord for Android is available on <a href="https://github.com/kushagrasinghx/BitChord/releases" target="_blank" rel="noopener noreferrer">GitHub</a>.</p>
    </div>
  </div>
</body>
</html>"""

_PLAYS_SECTION_HTML = r"""      <!-- ── View 2: Plays ────────────────────────────────────────────── -->
      <section class="view-panel" id="view-plays" data-view="plays"{{plays_hidden}}>
        <section class="page-hero rise" style="--i:0" aria-labelledby="plays-headline">
          <div>
            <span class="eyebrow"><span class="led signal pulse" aria-hidden="true"></span>Live Feed</span>
            <h1 id="plays-headline">What’s spinning through the gateway.</h1>
            <p class="lede">Real-time tracks and delivered studio tiers across Qobuz and Tidal — anonymous by design.</p>
            <div class="hero-chips">
              <span class="chip">
                <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <path d="M8 2.1L12.8 4v3.7c0 3-2 5.4-4.8 6.2-2.8-.8-4.8-3.2-4.8-6.2V4L8 2.1z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
                </svg>
                <span>Anonymous sessions</span>
              </span>
              <span class="chip">
                <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <path d="M8 2.2l1.55 3.55L13.1 7.3 9.55 8.85 8 12.4 6.45 8.85 2.9 7.3l3.55-1.55L8 2.2z" stroke="currentColor" stroke-width="1.35" stroke-linejoin="round" />
                </svg>
                <span>24-bit &amp; Atmos verified</span>
              </span>
              <span class="chip">
                <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <circle cx="8" cy="8" r="5.4" stroke="currentColor" stroke-width="1.4" />
                  <path d="M4.2 11.8l7.6-7.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                </svg>
                <span>Zero user logs</span>
              </span>
            </div>
          </div>
          <div class="hero-plate telemetry-plate spatial-window" id="plays-telemetry-plate" data-spatial-slot="plays" aria-label="Latest resolved stream">
            <div class="telemetry-ambient" id="telemetry-ambient" aria-hidden="true" data-art="0">
              <i data-layer="0"></i>
              <i data-layer="1"></i>
            </div>
            <div class="telemetry-box">
              <div class="telemetry-top">
                <span class="telemetry-state">
                  <span class="telemetry-live-dot" id="telemetry-live-dot" aria-hidden="true"></span>
                  <span id="telemetry-state-label">Latest stream</span>
                </span>
                <div class="telemetry-head-right">
                  <span id="telemetry-mode">Standby</span>
                  <div class="telemetry-stepper" id="telemetry-stepper" hidden role="group" aria-label="Browse recent streams">
                    <button type="button" class="telemetry-step-btn" id="telemetry-prev" aria-label="Previous recent stream">‹</button>
                    <span class="telemetry-step-count" id="telemetry-step-count">1 / 1</span>
                    <button type="button" class="telemetry-step-btn" id="telemetry-next" aria-label="Next recent stream">›</button>
                  </div>
                </div>
              </div>
              <div class="telemetry-track">
                <div class="sleeve-3d-plinth">
                  <div class="sleeve-3d-unit">
                    <div class="sleeve-3d-disc" id="telemetry-disc" aria-hidden="true">
                      <div class="sleeve-3d-label" id="telemetry-disc-label"></div>
                    </div>
                    <div class="track-cover" id="telemetry-track-cover">
                      <div class="cover-fallback"><span class="cover-monogram">♪</span></div>
                    </div>
                  </div>
                </div>
                <div class="telemetry-meta" id="telemetry-meta">
                  <div class="telemetry-signal-row">
                    <span class="telemetry-eq" aria-hidden="true"><i></i><i></i><i></i></span>
                    <span class="telemetry-tier-pill" id="telemetry-tier-pill" hidden></span>
                    <span class="telemetry-codec-spec" id="telemetry-codec-spec">Lossless ledger</span>
                  </div>
                  <p class="telemetry-title" id="telemetry-track-title">Reading ledger…</p>
                  <div class="telemetry-sub-row">
                    <p class="telemetry-sub" id="telemetry-track-sub">Awaiting active stream</p>
                    <span class="telemetry-album" id="telemetry-track-album" hidden></span>
                  </div>
                </div>
              </div>
              <div class="telemetry-foot">
                <span class="telemetry-tally" id="telemetry-tally">24h stream window</span>
                <div class="telemetry-foot-right">
                  <div class="telemetry-spark" id="telemetry-spark" aria-hidden="true"></div>
                  <strong id="telemetry-peak">—</strong>
                </div>
              </div>
            </div>
          </div>
        </section>

        <p class="support-status" id="plays-status" role="status" aria-live="polite">Loading play history…</p>

        <section class="block rise" style="--i:1" aria-labelledby="summary-title">
          <div class="block-head">
            <span class="label">Totals</span>
            <h2 id="summary-title">Since the gateway started counting</h2>
            <p>Cumulative stream and catalog counts across sessions — zero personal identifiers stored.</p>
            <p class="plays-filter-note" id="plays-filter-note" hidden></p>
          </div>
          <div class="plays-strip" id="plays-stats"></div>
        </section>

        <section class="block rise" style="--i:2" aria-labelledby="chart-title">
          <div class="block-head">
            <span class="label">Last 24 hours</span>
            <h2 id="chart-title">Plays over time</h2>
            <p>Each column is one hour, stacked by the quality the provider actually returned.</p>
          </div>
          <div class="plays-split">
            <div>
              <div class="plays-chart-stage">
                <div class="plays-chart" id="plays-chart" role="group" aria-label="Plays per hour over the last 24 hours, stacked by delivered quality"></div>
                <div class="plays-axis" id="plays-axis" aria-hidden="true"></div>
              </div>
              <p class="plays-readout" id="plays-readout" role="status" aria-live="polite">Select a column for the exact reading.</p>
            </div>
            <aside class="plays-side">
              <h3>Quality mix</h3>
              <p>Every stream this gateway has served, grouped by what came back from the provider — not by what was asked for. Pick a tier to isolate it in the chart.</p>
              <div id="plays-mix"></div>
            </aside>
          </div>
        </section>

        <section class="block rise" style="--i:3" aria-labelledby="top-title">
          <div class="block-head-bar">
            <div class="block-head">
              <span class="label">Ranked</span>
              <h2 id="top-title">Most played</h2>
              <p>Play count, how many listener sessions, and the quality spread underneath each row.</p>
            </div>
            <div class="plays-toolbar">
              <div class="plays-search-wrap">
                <input type="search" id="plays-search" placeholder="Search tracks or artists" aria-label="Search played tracks" />
                <button type="button" class="plays-search-clear" id="plays-search-clear" aria-label="Clear search" hidden>×</button>
              </div>
              <span class="plays-live" title="This feed updates itself every 10 seconds">
                <span class="led ok pulse" aria-hidden="true"></span>Live
              </span>
              <span class="plays-updated" id="plays-updated" aria-live="polite"></span>
            </div>
          </div>
          <div class="track-rank" id="plays-top" role="region" tabindex="0"
               aria-label="Most played tracks, scrollable"></div>
        </section>

        <section class="block rise" style="--i:4" aria-labelledby="recent-title">
          <div class="block-head">
            <span class="label">Anonymous feed</span>
            <h2 id="recent-title">Recently played</h2>
            <p>Times are rounded to the minute. No listener is named — sessions are deduplicated with a salted hash that is dropped on restart.</p>
          </div>
          <div class="recent-list" id="plays-recent" role="region" tabindex="0"
               aria-label="Recently played tracks, scrollable"></div>
        </section>
      </section>"""


def render_app_shell(
    *,
    initial_view: str = "status",
    public_url: str = "https://bitchord.confih.tech",
    addon_name: str = "Confih Hi-Res",
    addon_version: str = "2.0.0",
    public_plays: bool = True,
    donate_configured: bool = False,
    donate_demo: bool = False,
    addon_base_prefix: str = "",
    capabilities: dict[str, Any] | None = None,
    canonical_path: str = "/",
    support_email: str = "",
    security_email: str = "",
    abuse_email: str = "",
    coffee_url: str = "",
) -> str:
    view_key = (initial_view or "status").strip().lower()
    if view_key not in _VIEW_META:
        view_key = "status"
    if not public_plays and view_key == "plays":
        view_key = "status"

    active_view = view_key
    initial_anchor = ""

    caps = dict(capabilities or {})
    caps["public_plays"] = bool(public_plays)
    path_secret = bool(caps.get("path_secret", False))
    home_href = "/status" if path_secret else "/"

    origin = (public_url or "https://bitchord.confih.tech").rstrip("/")
    c_path = canonical_path if canonical_path.startswith("/") else f"/{canonical_path}"
    canonical_url = f"{origin}{c_path}"
    meta = _VIEW_META[view_key]

    boot_payload = {
        "initial_view": view_key,
        "public_plays": bool(public_plays),
        "donate_configured": bool(donate_configured),
        "donate_demo": bool(donate_demo),
        "addon_base_prefix": "",
        "public_url": origin,
        "addon_name": addon_name,
        "addon_version": addon_version,
        "asset_version": ASSET_VERSION,
        "capabilities": caps,
    }
    # Contact routes are derived from the host the request arrived on, so the
    # HTML never bakes in this deployment's domain. Empty when no mail host is
    # configured, which keeps the site from advertising an address that bounces.
    def _mail(addr: str) -> str:
        addr = (addr or "").strip()
        if "@" not in addr:
            return ""
        user, _, host = addr.partition("@")
        return addr if user and "." in host else ""

    # Buy Me a Coffee (or whatever card-payment link the operator sets). Only
    # rendered for an https URL, so an empty setting simply hides the panel and
    # a typo can never become a javascript: link.
    coffee_href = (coffee_url or "").strip()
    if not coffee_href.startswith("https://"):
        coffee_panel = ""
    else:
        coffee_panel = (
            '<div class="coffee-panel">'
            '<span class="coffee-mark" aria-hidden="true">'
            '<svg viewBox="0 0 32 32" focusable="false"><use href="#coffee-cup"/></svg>'
            "</span>"
            '<div class="coffee-copy">'
            '<span class="coffee-kind">Easiest · card, Apple Pay, PayPal</span>'
            "<h3>Buy me a coffee</h3>"
            "<p>No account, no crypto, no amount that buys anything extra. If the gateway "
            "saved you a subscription this month, this is the shortest thank-you.</p>"
            "</div>"
            f'<a class="button primary coffee-cta" href="{html.escape(coffee_href, quote=True)}" '
            'target="_blank" rel="noopener noreferrer">'
            'Open Buy Me a Coffee<span class="arrow" aria-hidden="true">↗</span></a>'
            "</div>"
            '<p class="crypto-divider"><span>or send crypto directly</span></p>'
        )

    mail_support = _mail(support_email)
    mail_security = _mail(security_email)
    mail_abuse = _mail(abuse_email)

    # Cloudflare's Scrape Shield rewrites literal mail addresses into a
    # /cdn-cgi/l/email-protection link that only works once its script runs.
    # The address is therefore assembled rather than written: user and domain
    # in two spans, the @ drawn by CSS, and site.js upgrading the link to a
    # real mailto: from data-mail. Readable and mailable with or without that
    # Cloudflare feature, and no injected script on the page either way.
    def _mail_html(address: str) -> str:
        user, _, host = address.partition("@")
        return (
            f'<span class="mail-user">{html.escape(user)}</span>'
            '<span class="mail-at" aria-hidden="true"></span>'
            '<span class="sr-only"> at </span>'
            f'<span class="mail-domain">{html.escape(host)}</span>'
        )

    def _mail_pair(address: str) -> str:
        user, _, host = address.partition("@")
        return html.escape(f"{user}|{host}", quote=True)

    def _contact_card(kind: str, address: str, blurb: str) -> str:
        user, _, host = address.partition("@")
        pair = html.escape(f"{user}|{host}", quote=True)
        return (
            f'<a class="card card-link contact-card" data-mail="{pair}" href="#contact-title">'
            f'<span class="contact-kind">{html.escape(kind)}</span>'
            f"<h3>{_mail_html(address)}</h3>"
            f"<p>{html.escape(blurb)}</p>"
            "</a>"
        )

    cards = ""
    if mail_support:
        cards += _contact_card(
            "Support · donations",
            mail_support,
            "Something is broken, a donation bounced, or you want to tell me the gateway "
            "found you an album.",
        )
    if mail_security:
        cards += _contact_card(
            "Security",
            mail_security,
            "A vulnerability, a leaked credential or something that exposes another listener. "
            "Please do not post exploit details in a public issue.",
        )
    contact_cards = cards or (
        '<p class="contact-empty">No mail address is configured on this deployment yet. '
        "The repository is the fastest route in the meantime.</p>"
    )
    contact_lead = (
        f', or write to <a class="mail-link" data-mail="{_mail_pair(mail_support)}" '
        f'href="#contact-title">{_mail_html(mail_support)}</a>.'
        if mail_support
        else "."
    )
    abuse_line = (
        " Rights-holder or abuse enquiries: "
        f'<a class="mail-link" data-mail="{_mail_pair(mail_abuse)}" '
        f'href="#contact-title">{_mail_html(mail_abuse)}</a>.'
        if mail_abuse
        else ""
    )

    del addon_base_prefix  # never embed path secret in public HTML

    plays_section = (
        _PLAYS_SECTION_HTML.replace(
            "{{plays_hidden}}", "" if active_view == "plays" else " hidden"
        )
        if public_plays
        else ""
    )

    esc = lambda val: html.escape(str(val), quote=True)  # noqa: E731
    addon_url_display = "Private path hidden" if path_secret else origin
    manifest_url_display = "Private path hidden" if path_secret else f"{origin}/manifest.json"
    covers_hidden = "" if (donate_configured or donate_demo) else " hidden"

    replacements = {
        "{{v}}": ASSET_VERSION,
        "{{initial_view}}": esc(view_key),
        "{{active_view}}": esc(active_view),
        "{{initial_anchor}}": esc(initial_anchor),
        "{{public_plays_flag}}": "1" if public_plays else "0",
        "{{path_secret_flag}}": "1" if path_secret else "0",
        "{{title}}": esc(meta["title"]),
        "{{description}}": esc(meta["desc"]),
        "{{robots_meta}}": meta["robots"],
        "{{og_title}}": esc(meta["og_title"]),
        "{{og_description}}": esc(meta["og_desc"]),
        "{{origin}}": esc(origin),
        "{{canonical_url}}": esc(canonical_url),
        "{{home_href}}": esc(home_href),
        "{{addon_name}}": esc(addon_name),
        "{{addon_version}}": esc(addon_version),
        "{{addon_url_display}}": esc(addon_url_display),
        "{{manifest_url_display}}": esc(manifest_url_display),
        "{{copy_disabled}}": " disabled" if path_secret else "",
        "{{status_current}}": ' aria-current="page"' if active_view == "status" else "",
        "{{about_current}}": ' aria-current="page"' if active_view == "about" else "",
        "{{support_current}}": ' aria-current="page"' if active_view == "support" else "",
        "{{about_hidden}}": "" if active_view == "about" else " hidden",
        "{{support_hidden}}": "" if active_view == "support" else " hidden",
        "{{plays_nav_link}}": (
            '<a href="/plays" data-nav-view="plays"'
            + (' aria-current="page"' if active_view == "plays" else "")
            + ">Plays</a>"
            if public_plays
            else ""
        ),
        "{{plays_foot_link}}": (
            '<a href="/plays" data-nav-view="plays">Plays</a>' if public_plays else ""
        ),
        "{{plays_json_foot_link}}": '<a href="/api/plays">Plays JSON</a>' if public_plays else "",
        "{{plays_css}}": (
            f'<link rel="stylesheet" href="/assets/plays.css?v={ASSET_VERSION}" />'
            if public_plays
            else ""
        ),
        "{{plays_script}}": (
            f'<script src="/assets/plays.js?v={ASSET_VERSION}" defer></script>'
            if public_plays
            else ""
        ),
        "{{status_hidden}}": "" if active_view == "status" else " hidden",
        "{{plays_section}}": plays_section,
        "{{covers_hidden}}": covers_hidden,
        "{{coffee_panel}}": coffee_panel,
        "{{contact_cards}}": contact_cards,
        "{{contact_lead}}": contact_lead,
        "{{abuse_line}}": abuse_line,
        "{{boot_json}}": _safe_json_script(boot_payload),
    }

    token_re = re.compile("|".join(re.escape(token) for token in replacements))
    return token_re.sub(lambda match: replacements[match.group(0)], APP_SHELL_HTML)

_ADMIN_VAULT_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <meta name="robots" content="noindex, nofollow" />
  <title>Admin vault — Confih</title>
  <meta name="theme-color" id="theme-color-meta" content="#09090b" />
  <meta name="color-scheme" content="dark" />
  <link rel="preload" as="font" type="font/woff2" crossorigin href="/assets/fonts/inter-latin.woff2?v={{v}}" />
  <link rel="icon" href="/assets/confih-mark.svg?v={{v}}" type="image/svg+xml" />

  <script>
    (function () {
      document.documentElement.setAttribute('data-theme', 'dark');
      document.documentElement.classList.add('has-js');
    })();
  </script>

  <style>
    @font-face {
      font-family: "InterVar";
      font-style: normal;
      font-weight: 100 900;
      font-display: swap;
      src: url("/assets/fonts/inter-latin.woff2?v={{v}}") format("woff2");
    }
  </style>

  <link rel="stylesheet" href="/assets/confih.css?v={{v}}" />
  <link rel="stylesheet" href="/assets/admin.css?v={{v}}" />
</head>
<body>
  <a class="skip-link" href="#main-content">Skip to content</a>

  <noscript>
    <div class="noscript-note" role="status">The admin vault needs JavaScript to sign in and manage accounts.</div>
  </noscript>

  <header class="site-head">
    <div class="shell head-inner">
      <a class="brand" href="{{home_href}}" aria-label="Confih — return to status page">
        <span class="brand-logo-stage">
          <img class="brand-logo" src="/assets/confih-logo.svg?v={{v}}" alt="Confih" width="102" height="38" />
          <img class="brand-logo-loader" src="/assets/confih-loader.svg?v={{v}}" alt="" aria-hidden="true" width="102" height="38" />
        </span>
        <span class="brand-suffix">Admin</span>
      </a>
      <div class="head-actions">
        <span class="privacy-note">Private · noindex</span>
      </div>
    </div>
  </header>

  <main id="main-content" tabindex="-1">
    <div class="shell">

      <!-- Auth gate -->
      <section class="login-wrap" id="login-box" aria-labelledby="login-title">
        <div class="login-card rise" data-tilt style="--i:0">
          <img class="login-mark" src="/assets/confih-mark.svg?v={{v}}" alt="" width="64" height="60" />
          <h1 id="login-title">Admin vault</h1>
          <p class="lede">Unlock provider routing, account pools, and device sign-in for this addon.</p>

          <form id="vault-form" autocomplete="on" novalidate>
            <div class="field">
              <label for="vault-key">Vault token</label>
              <div class="field-wrap">
                <input
                  type="password"
                  id="vault-key"
                  name="vault_key"
                  placeholder="Paste STATS_TOKEN"
                  autocomplete="current-password"
                  spellcheck="false"
                  autofocus
                  aria-describedby="login-err"
                />
                <button type="button" id="toggle-pwd-btn" class="reveal" aria-label="Show vault token value" aria-pressed="false" aria-controls="vault-key">Show</button>
              </div>
            </div>
            <button type="submit" id="unlock-btn" class="button primary wide">Unlock vault</button>
          </form>
          <div class="confih-loader confih-loader-compact" id="admin-login-loader" hidden aria-hidden="true">
            <img class="confih-loader-logo" src="/assets/confih-loader.svg?v={{v}}" alt="" width="112" height="42" />
            <span class="confih-loader-caption" id="admin-login-loader-text">Verifying vault token…</span>
          </div>
          <p class="form-error" id="login-err" role="alert" aria-live="assertive"></p>
          <p class="fineprint">The key is the <code>STATS_TOKEN</code> value from the server environment. It stays in this tab and is cleared when you lock the vault or close the tab.</p>
        </div>
      </section>

      <!-- Console -->
      <div id="admin-console" class="console hidden">
        <header class="console-head">
          <div>
            <span class="eyebrow"><span class="led ok pulse" aria-hidden="true"></span>Unlocked</span>
            <h1 id="console-heading" tabindex="-1">Vault</h1>
            <p class="lede">Provider routing, account health, and live failover for the addon.</p>
          </div>
          <div class="head-buttons">
            <button class="button secondary" id="btn-refresh" type="button">Refresh</button>
            <button class="button secondary" id="btn-clear-cache" type="button">Clear cache</button>
            <button class="button danger" id="btn-lock" type="button">Lock vault</button>
          </div>
        </header>
        <div class="confih-loader confih-loader-compact" id="admin-console-loader" hidden aria-hidden="true">
          <img class="confih-loader-logo" src="/assets/confih-loader.svg?v={{v}}" alt="" width="108" height="40" />
          <span class="confih-loader-caption" id="admin-console-loader-text">Syncing vault…</span>
        </div>

        <section class="panel rise" style="--i:0" aria-labelledby="routing-title">
          <div class="panel-head">
            <div>
              <span class="label">Routing</span>
              <h2 id="routing-title">Primary provider</h2>
              <p>Choose which engine serves requests first. Auto-failover stays on.</p>
            </div>
          </div>
          <div class="panel-body">
            <div class="segmented" role="group" aria-label="Primary provider">
              <button id="btn-pri-qobuz" class="secondary" type="button" aria-pressed="false">Qobuz first</button>
              <button id="btn-pri-tidal" class="secondary" type="button" aria-pressed="false">Tidal first</button>
              <button id="btn-pri-both" class="secondary" type="button" aria-pressed="false">Parallel</button>
            </div>
            <p class="routing-line" id="routing-status-line" role="status" aria-live="polite">Current mode: loading…</p>
          </div>
        </section>

        <div class="provider-grid">
          <!-- Qobuz -->
          <section class="panel rise" style="--i:1" aria-labelledby="qobuz-title">
            <div class="panel-head">
              <div>
                <span class="label">Pool</span>
                <h2 id="qobuz-title">Qobuz accounts</h2>
                <p>Studio accounts in this pool.</p>
              </div>
              <span class="count" id="qobuz-count">—</span>
            </div>
            <div class="panel-body">
              <div id="qobuz-list" class="account-list" role="group" aria-label="Qobuz account list" aria-live="polite"></div>

              <hr class="divider" />

              <h3 class="form-title">Add a token</h3>
              <p class="form-hint">App ID and secret fall back to the server defaults when left blank.</p>

              <div class="field">
                <label for="q-label">Label</label>
                <input type="text" id="q-label" name="qobuz_label" placeholder="Qobuz · GB" autocomplete="off" />
              </div>
              <div class="field">
                <label for="q-token">User auth token</label>
                <div class="field-wrap">
                  <input type="password" id="q-token" name="qobuz_token" placeholder="user_auth_token" autocomplete="off" spellcheck="false" />
                  <button type="button" class="reveal" id="toggle-q-token" aria-label="Show Qobuz token value" aria-pressed="false" aria-controls="q-token">Show</button>
                </div>
              </div>
              <div class="field-row">
                <div class="field">
                  <label for="q-userid">User ID <span class="sr-only">(optional)</span></label>
                  <input type="text" id="q-userid" name="qobuz_user_id" placeholder="Optional" autocomplete="off" inputmode="numeric" spellcheck="false" />
                </div>
                <div class="field">
                  <label for="q-appid">App ID <span class="sr-only">(optional)</span></label>
                  <input type="text" id="q-appid" name="qobuz_app_id" placeholder="Optional" autocomplete="off" inputmode="numeric" spellcheck="false" />
                </div>
              </div>
              <div class="field">
                <label for="q-appsecret">App secret <span class="sr-only">(optional)</span></label>
                <div class="field-wrap">
                  <input type="password" id="q-appsecret" name="qobuz_app_secret" placeholder="Optional" autocomplete="off" spellcheck="false" />
                  <button type="button" class="reveal" id="toggle-q-appsecret" aria-label="Show Qobuz app secret value" aria-pressed="false" aria-controls="q-appsecret">Show</button>
                </div>
              </div>
              <p class="form-error" id="q-err" role="alert" aria-live="assertive"></p>
              <button id="btn-qobuz-add" class="button primary wide" type="button">Verify and save</button>
            </div>
          </section>

          <!-- Tidal -->
          <section class="panel rise" style="--i:2" aria-labelledby="tidal-title">
            <div class="panel-head">
              <div>
                <span class="label">Pool</span>
                <h2 id="tidal-title">Tidal accounts</h2>
                <p>Hi-Res and MAX accounts in this pool.</p>
              </div>
              <span class="count" id="tidal-count">—</span>
            </div>
            <div class="panel-body">
              <div id="tidal-list" class="account-list" role="group" aria-label="Tidal account list" aria-live="polite"></div>

              <hr class="divider" />

              <h3 class="form-title">Link with device login</h3>
              <p class="form-hint">Opens the official Tidal activation page. The server saves the account when you sign in.</p>
              <button class="button secondary wide" id="btn-tidal-device" type="button">Generate Tidal login link</button>
              <div class="device-box" id="tidal-device-box"></div>

              <hr class="divider" />

              <h3 class="form-title">Or paste tokens</h3>
              <p class="form-hint">A refresh token keeps the account signed in automatically.</p>

              <div class="field">
                <label for="t-label">Label</label>
                <input type="text" id="t-label" name="tidal_label" placeholder="Tidal MAX · 1" autocomplete="off" />
              </div>
              <div class="field">
                <label for="t-access">Access token</label>
                <div class="field-wrap">
                  <input type="password" id="t-access" name="tidal_access_token" placeholder="access_token" autocomplete="off" spellcheck="false" />
                  <button type="button" class="reveal" id="toggle-t-access" aria-label="Show Tidal access token value" aria-pressed="false" aria-controls="t-access">Show</button>
                </div>
              </div>
              <div class="field">
                <label for="t-refresh">Refresh token</label>
                <div class="field-wrap">
                  <input type="password" id="t-refresh" name="tidal_refresh_token" placeholder="refresh_token" autocomplete="off" spellcheck="false" />
                  <button type="button" class="reveal" id="toggle-t-refresh" aria-label="Show Tidal refresh token value" aria-pressed="false" aria-controls="t-refresh">Show</button>
                </div>
              </div>
              <p class="form-error" id="t-err" role="alert" aria-live="assertive"></p>
              <button id="btn-tidal-add" class="button primary wide" type="button">Save Tidal account</button>
            </div>
          </section>
        </div>

        <p class="console-foot">Admin session only. Actions here change live routing and account pools immediately.</p>
      </div>

    </div>
  </main>

  <div id="toast" class="toast hidden" role="status" aria-live="polite"></div>

  <script src="/assets/theme.js?v={{v}}" defer></script>
  <script src="/assets/site.js?v={{v}}" defer></script>
  <script src="/assets/admin.js?v={{v}}" defer></script>
</body>
</html>"""


_ASSET_HASH_SUFFIXES = frozenset({
    ".css",
    ".js",
    ".mjs",
    ".svg",
    ".json",
    ".glsl",
    ".png",
    ".woff2",
})


def _asset_version(static_dir: Path | None = None) -> str:
    """Content hash of served static assets.

    Stamped into every asset URL so a deploy (or a local reload) can never
    hand a returning visitor a stale stylesheet, script, shader, font, or image.
    """
    import hashlib

    root = static_dir or (Path(__file__).resolve().parent / "static")
    digest = hashlib.sha256()
    if root.is_dir():
        files = sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in _ASSET_HASH_SUFFIXES
            ),
            key=lambda p: p.relative_to(root).as_posix(),
        )
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()[:10]


ASSET_VERSION = _asset_version()


def _stamp(markup: str) -> str:
    return markup.replace("{{v}}", ASSET_VERSION)


def render_admin_vault_html(*, path_secret: bool = False) -> str:
    home_href = "/status" if path_secret else "/"
    return _stamp(_ADMIN_VAULT_TEMPLATE).replace("{{home_href}}", home_href)


ADMIN_VAULT_HTML = render_admin_vault_html()
