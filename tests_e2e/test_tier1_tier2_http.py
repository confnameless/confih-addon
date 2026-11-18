"""Tier 1 (Functional) & Tier 2 (BVA/Negative/Security) Opaque-Box HTTP/API E2E Tests."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx
import pytest

from tests_e2e.conftest import (
    DEFAULT_ADMIN_SECRET,
    DEFAULT_PATH_SECRET,
    DEFAULT_STATS_TOKEN,
    REPO_ROOT,
    IsolatedUvicornServer,
    probe_boot_failure,
)

# ── Feature Area A: Public HTML Routes & Shell ──────────────────────────────


def test_t1_a1_status_and_monitor_html(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for path in ("/status", "/monitor"):
            resp = client.get(path)
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/html")
            assert "no-store" in resp.headers.get("cache-control", "")
            assert resp.headers.get("x-frame-options") == "DENY"
            assert resp.headers.get("x-content-type-options") == "nosniff"
            assert resp.headers.get("referrer-policy") == "no-referrer"
            assert resp.headers.get("cross-origin-opener-policy") == "same-origin"
            assert "content-security-policy" in resp.headers
            body = resp.text
            assert "Confih Hi-Res Gateway" in body
            for hook in ("status-line", "addon-url", "timeline-bars", "quality-bar"):
                assert f'id="{hook}"' in body


def test_t1_a2_plays_html_hooks(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/plays")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers.get("x-robots-tag") == "noindex, nofollow"
        assert "no-store" in resp.headers.get("cache-control", "")
        body = resp.text
        assert "Confih Hi-Res Gateway" in body
        assert "/assets/plays.js" in body
        assert 'href="/plays"' in body
        for hook in (
            "plays-status",
            "plays-stats",
            "plays-chart",
            "plays-axis",
            "plays-readout",
            "plays-legend",
            "plays-top",
            "plays-recent",
            "plays-mix",
        ):
            assert f'id="{hook}"' in body


def test_t1_a3_support_html_hooks(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/support")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers.get("x-robots-tag") == "noindex, nofollow"
        assert "no-store" in resp.headers.get("cache-control", "")
        body = resp.text
        for hook in ("support-assets", "support-status", "support-demo"):
            assert f'id="{hook}"' in body


def test_t1_a4_about_html(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/about")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers.get("x-robots-tag") == "noindex, nofollow"
        assert "no-store" in resp.headers.get("cache-control", "")
        assert "Confih" in resp.text
        assert ('id="about-headline"' in resp.text) or ('id="support-assets"' in resp.text)


def test_t1_a5_admin_and_vault_html(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for path in ("/admin", "/vault"):
            resp = client.get(path)
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/html")
            assert resp.headers.get("x-robots-tag") == "noindex, nofollow"
            assert "no-store" in resp.headers.get("cache-control", "")
            for hook in ("login-box", "vault-form", "vault-key", "admin-console"):
                assert f'id="{hook}"' in resp.text


def test_t2_a1_trailing_slashes(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for path in ("/plays/", "/support/", "/about/", "/admin/", "/vault/"):
            resp = client.get(path)
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/html")


def test_t2_a2_disallowed_http_methods(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for method, path in (
            ("POST", "/status"),
            ("DELETE", "/plays"),
            ("PUT", "/support"),
            ("PATCH", "/about"),
        ):
            resp = client.request(method, path)
            assert resp.status_code == 405
            data = resp.json()
            assert data.get("code") == "405"


def test_t2_a3_self_only_csp_enforcement(std_server: IsolatedUvicornServer) -> None:
    """M2/PROJECT.md Contract: HTML CSP must be strictly 'self'-only with zero 3rd-party hosts."""
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for path in ("/", "/status", "/plays", "/support", "/about"):
            resp = client.get(path, headers={"Accept": "text/html"})
            assert resp.status_code == 200
            csp = resp.headers.get("content-security-policy", "")
            assert "default-src 'self'" in csp
            assert "frame-ancestors 'none'" in csp
            assert "base-uri 'none'" in csp
            assert "img-src 'self' data:" in csp
            assert "cloudflareinsights.com" not in csp
            assert "qrserver.com" not in csp
            assert "https://" not in csp
            assert "http://" not in csp


def test_t2_a4_plays_404_when_disabled(noplays_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=noplays_server.base_url, timeout=5.0) as client:
        for path in ("/plays", "/plays/"):
            resp = client.get(path)
            assert resp.status_code == 404
            assert resp.json().get("code") == "not_found"


def test_t2_a5_plays_nav_omitted_when_disabled(noplays_server: IsolatedUvicornServer) -> None:
    """M2/PROJECT.md Contract: PUBLIC_PLAYS=false omits href="/plays" from public nav/footer."""
    with httpx.Client(base_url=noplays_server.base_url, timeout=5.0) as client:
        for path in ("/", "/status", "/support", "/about"):
            resp = client.get(path, headers={"Accept": "text/html"})
            assert resp.status_code == 200
            nav_or_footer_links = re.findall(
                r"<(?:nav|footer)\b[^>]*>.*?</(?:nav|footer)>",
                resp.text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            combined = "\n".join(nav_or_footer_links)
            assert 'href="/plays"' not in combined, f"Found /plays nav link on {path} with PUBLIC_PLAYS=false"


def test_t2_a6_unknown_route_404(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/nonexistent-page-xyz")
        assert resp.status_code == 404
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert "Traceback" not in resp.text
        assert resp.json().get("code") == "404"


# ── Feature Area B: Content Negotiation, Discovery & Static/Media ───────────


def test_t1_b1_root_html_negotiation(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "no-store" in resp.headers.get("cache-control", "")
        assert "Confih Hi-Res Gateway" in resp.text


def test_t1_b2_root_json_accept(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/", headers={"Accept": "application/json", "User-Agent": "curl/8.0"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.headers.get("cache-control") == "public, max-age=600"
        data = resp.json()
        assert set(data.keys()) == {"addon", "version", "manifest", "resources"}
        assert data["manifest"] == "/manifest.json"
        assert data["resources"] == ["search", "stream"]


def test_t1_b3_root_bitchord_ua(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/", headers={"User-Agent": "BitChord/2.1.0", "Accept": "*/*"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json()["manifest"] == "/manifest.json"


def test_t1_b4_robots_and_sitemap(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        robots = client.get("/robots.txt")
        assert robots.status_code == 200
        assert robots.headers["content-type"].startswith("text/plain")
        assert robots.headers.get("cache-control") == "public, max-age=86400"
        assert "Disallow: /internal/" in robots.text
        assert "Disallow: /s/" in robots.text

        sitemap = client.get("/sitemap.xml")
        assert sitemap.status_code == 200
        assert sitemap.headers["content-type"].startswith("application/xml")
        assert sitemap.headers.get("cache-control") == "public, max-age=86400"
        assert "<urlset" in sitemap.text


def test_t1_b5_static_assets_mime(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        expected = (
            ("/assets/confih.css", "text/css"),
            ("/assets/site.js", "javascript"),
            ("/assets/confih-mark.svg", "image/svg+xml"),
            ("/assets/fonts/inter-latin.woff2", "font/woff2"),
        )
        for path, mime_part in expected:
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert mime_part in resp.headers["content-type"].lower(), (path, resp.headers["content-type"])
            assert resp.headers.get("x-content-type-options") == "nosniff"
            assert len(resp.content) > 0


def test_t2_b1_bitchord_ua_overrides_html_accept(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/", headers={"Accept": "text/html", "User-Agent": "BitChord/1.0"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert "manifest" in resp.json()


def test_t2_b2_mixed_accept_prefers_html(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/", headers={"Accept": "application/json, text/html", "User-Agent": "Mozilla/5.0"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")


def test_t2_b3_crawler_ua_overrides_json_accept(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get(
            "/",
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")


def test_t2_b4_head_requests(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        r_html = client.head("/", headers={"Accept": "text/html"})
        assert r_html.status_code == 200
        assert r_html.headers["content-type"].startswith("text/html")
        assert r_html.content == b""

        r_json = client.head("/", headers={"Accept": "application/json", "User-Agent": "curl/8.0"})
        assert r_json.status_code == 200
        assert r_json.headers["content-type"].startswith("application/json")
        assert r_json.content == b""

        assert client.head("/robots.txt").status_code == 200
        assert client.head("/sitemap.xml").status_code == 200


def test_t2_b5_artwork_traversal_and_offline_seed(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for attempt in ("../../etc/passwd", "..%2Fetc%2Fpasswd", "a" * 33, "doesnotexist0000000000"):
            resp = client.get(f"/art/{attempt}")
            assert resp.status_code in (400, 404), (attempt, resp.status_code)
        # Seeded offline artwork key must return 200 image/png with zero external requests
        key = std_server.seed_summary["artwork_keys"][0]
        ok = client.get(f"/art/{key}")
        assert ok.status_code == 200
        assert ok.headers["content-type"].startswith("image/png")
        assert ok.headers.get("x-content-type-options") == "nosniff"
        assert "immutable" in ok.headers.get("cache-control", "")


def test_t2_b6_static_assets_traversal_and_missing(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for path in ("/assets/../config.py", "/assets/nonexistent.js"):
            resp = client.get(path)
            assert resp.status_code == 404


# ── Feature Area C: BitChord Addon Protocol ─────────────────────────────────


def test_t1_c1_manifest_defaults(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/manifest.json")
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "public, max-age=600"
        m = resp.json()
        assert m["resources"] == ["search", "stream"]
        assert m["allowDownloads"] == 1
        assert m["checkValidLossless"] == 0
        assert m["bitchordAuth"] is False
        options = [opt["value"] for opt in m["settings"][0]["options"]]
        assert options == ["max", "hires", "lossless", "high", "low"]


def test_t1_c2_search_empty_and_whitespace(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for q in ("", "   "):
            resp = client.get("/search", params={"q": q})
            assert resp.status_code == 200
            assert resp.headers.get("cache-control") == "private, max-age=600"
            assert resp.json() == {"tracks": []}


def test_t1_c3_search_unconfigured_providers(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/search", params={"q": "daft punk", "quality": "max"})
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "private, max-age=300"
        assert resp.json() == {"tracks": []}


def test_t1_c4_manifest_flag_overrides() -> None:
    with IsolatedUvicornServer(
        {"ALLOW_DOWNLOADS": "false", "CHECK_VALID_LOSSLESS": "true"},
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            m = client.get("/manifest.json").json()
            assert m["allowDownloads"] == 0
            assert m["checkValidLossless"] == 1


def test_t1_c5_dash_uncached_404(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get(f"/dash/{'a' * 64}.mpd")
        assert resp.status_code == 404
        assert resp.json().get("code") == "not_found"


def test_t2_c1_search_query_length_bva(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        ok = client.get("/search", params={"q": "x" * 200})
        assert ok.status_code == 200
        too_long = client.get("/search", params={"q": "x" * 201})
        assert too_long.status_code == 400
        assert too_long.json().get("code") == "invalid_query"


def test_t2_c2_search_limit_bva_and_invalid_type(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for lim in ("0", "-1", "51", "999"):
            resp = client.get("/search", params={"q": "test", "limit": lim})
            assert resp.status_code == 200
        bad = client.get("/search", params={"q": "test", "limit": "abc"})
        assert bad.status_code == 400
        assert bad.json().get("code") == "invalid_request"


def test_t2_c3_stream_id_validation_bva(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for invalid_id in ("not-an-id", "qobuz:abc", "tidal:", "1" * 25):
            resp = client.get(f"/stream/{invalid_id}")
            assert resp.status_code == 404, (invalid_id, resp.status_code)
            assert resp.json().get("code") == "not_found"
        for valid_id in ("5966783", "qobuz:5966783", "tidal:123456"):
            resp = client.get(f"/stream/{valid_id}")
            assert resp.status_code in (404, 503), (valid_id, resp.status_code)
            assert "error" in resp.json()


def test_t2_c4_dash_id_validation_bva(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for bad_id in ("A" * 16, "a" * 15, "a" * 65, "z" * 16):
            resp = client.get(f"/dash/{bad_id}.mpd")
            assert resp.status_code == 404
            assert resp.json().get("code") == "not_found"


def test_t2_c5_rate_limiting_enforcement() -> None:
    with IsolatedUvicornServer(
        {"RATE_LIMIT_PER_MINUTE": "1"},
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            first = client.get("/search", params={"q": "one"})
            assert first.status_code == 200
            second = client.get("/search", params={"q": "two"})
            assert second.status_code == 429
            assert int(second.headers.get("retry-after", "0")) >= 1
            assert second.json().get("code") == "rate_limited"
            assert client.get("/manifest.json").status_code == 200
            assert client.get("/healthz").status_code == 200


def test_t2_c6_addon_auth_enforcement() -> None:
    with IsolatedUvicornServer(
        {"ADDON_AUTH_ENABLED": "true", "ADDON_HOSTS": "127.0.0.1"},
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            manifest = client.get("/manifest.json")
            assert manifest.status_code == 200
            assert manifest.json()["bitchordAuth"] is True
            assert client.get("/healthz").status_code == 200

            for path in ("/search?q=test", "/stream/123"):
                resp = client.get(path)
                assert resp.status_code == 401
                assert resp.headers.get("www-authenticate", "").startswith("BitChord")
                assert resp.json().get("code") == "missing"


# ── Feature Area D: Public Telemetry, Overview & SVG Badge ──────────────────


def test_t1_d1_public_stats_baseline_schema(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/api/public-stats")
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"
        data = resp.json()
        assert data["status"] in ("operational", "degraded", "outage")
        assert isinstance(data["status_reasons"], list)
        assert isinstance(data["primary_online"], bool)
        assert isinstance(data["failover_active"], bool)
        for key in ("name", "version", "url", "display_url", "path_secret", "uptime_seconds"):
            assert key in data["addon"]
        for key in ("active_now", "active_streamers_5m", "active_1h", "active_24h", "total_all_time"):
            assert key in data["users"]
        for key in ("total_requests", "monitor_requests", "total_searches", "total_streams", "streams_served"):
            assert key in data["traffic"]
        assert "qobuz" in data["services"] and "tidal" in data["services"]
        assert len(data["timeline_24h"]) == 24


def test_t1_d2_public_stats_m1_enriched_fields(std_server: IsolatedUvicornServer) -> None:
    """M1/PROJECT.md Contract: /api/public-stats includes timeline_30d, incidents, telemetry, artwork_cache, capabilities."""
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        data = client.get("/api/public-stats").json()
        assert "timeline_30d" in data
        t30 = data["timeline_30d"]
        if isinstance(t30, dict):
            days = t30.get("days", [])
            assert len(days) == 30
            assert days[0] and isinstance(days[0], dict)
            assert {"requests", "ok", "errors", "status"} <= set(days[0].keys())
        else:
            assert isinstance(t30, list) and len(t30) == 30
            assert t30[0] and isinstance(t30[0], dict)
            assert {"requests", "ok", "errors", "status"} <= set(t30[0].keys())
            assert isinstance(data.get("uptime_30d_percent"), (int, float))
            assert data["uptime_30d_percent"] > 0

        assert isinstance(data["incidents"], list)
        assert len(data["incidents"]) == 2

        telem = data["telemetry"]
        for key in ("rss_mb", "cpu_cores", "load_1m", "load_5m", "load_15m", "arch"):
            assert key in telem
        assert ("python" in telem) or ("python_version" in telem)
        assert isinstance(telem["cpu_cores"], int) and telem["cpu_cores"] >= 1
        assert isinstance(telem["rss_mb"], (int, float)) and telem["rss_mb"] > 0

        art_cache = data["artwork_cache"]
        for key in ("files", "bytes", "megabytes", "max_files"):
            assert key in art_cache
        assert art_cache["files"] >= 3

        caps = data["capabilities"]
        for key in ("public_plays", "addon_auth_enabled", "path_secret", "primary_provider", "quality_options"):
            assert key in caps


def test_t1_d3_overview_bootstrap(std_server: IsolatedUvicornServer) -> None:
    """M1/PROJECT.md Contract: GET /api/overview returns unified {stats, support, plays} bootstrap."""
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/api/overview")
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"
        data = resp.json()
        assert "stats" in data and "services" in data["stats"]
        for key in ("timeline_30d", "telemetry", "capabilities"):
            assert key in data["stats"], f"overview stats missing {key}"
        assert "support" in data and data["support"]["demo"] is True
        assert len(data["support"]["assets"]) == 3
        assert data["plays"] is not None and data["plays"]["totals"]["tracks"] == 8
        assert len(data["plays"]["top"]) == 8
        assert len(data["plays"]["recent"]) == 5
        assert len(data["plays"]["timeline"]) == 24


def test_t1_d4_status_badge_svg(std_server: IsolatedUvicornServer) -> None:
    """M1/PROJECT.md Contract: GET /badge.svg returns crisp SVG status badge."""
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/badge.svg")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert "max-age=30" in resp.headers.get("cache-control", "")
        assert resp.headers.get("x-content-type-options") == "nosniff"
        body = resp.text
        assert body.startswith("<svg ")
        root = ET.fromstring(body)
        assert root.tag.endswith("svg")
        assert root.attrib.get("role") == "img"
        assert 'role="img"' in body
        assert "<title>" in body
        assert "CONFIH" in body
        assert any(word in body for word in ("OPERATIONAL", "FAILOVER", "DEGRADED", "OFFLINE"))


def test_t1_d5_monitor_isolation_invariant(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        before = client.get("/api/public-stats").json()
        for path in ("/status", "/healthz", "/plays", "/api/public-stats"):
            client.get(path)
        after = client.get("/api/public-stats").json()
        assert after["traffic"]["total_requests"] == before["traffic"]["total_requests"]
        assert after["users"]["total_all_time"] == before["users"]["total_all_time"]
        assert after["traffic"]["monitor_requests"] > before["traffic"]["monitor_requests"]


def test_t2_d1_overview_with_public_plays_disabled(noplays_server: IsolatedUvicornServer) -> None:
    """M1/PROJECT.md Contract: GET /api/overview returns 200 with plays=None when PUBLIC_PLAYS=false."""
    with httpx.Client(base_url=noplays_server.base_url, timeout=5.0) as client:
        resp = client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["plays"] is None
        assert data["stats"]["capabilities"]["public_plays"] is False


def test_t2_d2_badge_svg_unconfigured_outage(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/badge.svg")
        assert resp.status_code == 200
        body = resp.text
        assert "OFFLINE" in body
        assert "#ff6f52" in body
        assert 'height="24"' in body


def test_t2_d3_public_stats_masks_path_secret(secret_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=secret_server.base_url, timeout=5.0) as client:
        resp = client.get("/api/public-stats")
        assert resp.status_code == 200
        assert DEFAULT_PATH_SECRET not in resp.text
        assert resp.json()["addon"]["path_secret"] is True


def test_t2_d4_public_stats_local_origin_fallback(std_server: IsolatedUvicornServer) -> None:
    """M1/PROJECT.md Feature 7: empty PUBLIC_BASE_URL falls back to active request origin."""
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        data = client.get("/api/public-stats").json()
        assert f"127.0.0.1:{std_server.port}" in data["addon"]["display_url"]


def test_t2_d5_public_stats_privacy_invariants(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        raw = client.get("/api/public-stats").text
        # No raw loopback/private client IPs leaked in users/incidents/telemetry
        assert "10.0.0." not in raw
        assert "192.168." not in raw
        data = httpx.Response(200, text=raw).json()
        assert len(data.get("incidents", [])) <= 25


# ── Feature Area E: Public Plays Feed & Search/Tier Filtering ───────────────


def test_t1_e1_plays_empty_data_dir() -> None:
    with IsolatedUvicornServer(seed_plays=False, seed_artwork=False, seed_stats=False) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            resp = client.get("/api/plays")
            assert resp.status_code == 200
            assert resp.headers.get("cache-control") == "no-store"
            data = resp.json()
            assert data["totals"]["plays"] == 0
            assert data["totals"]["tracks"] == 0
            assert data["totals"]["listeners"] == 0
            assert data["top"] == []
            assert data["recent"] == []
            assert len(data["timeline"]) == 24


def test_t1_e2_plays_seeded_aggregation(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        data = client.get("/api/plays").json()
        assert data["totals"]["plays"] == std_server.seed_summary["total_plays"]
        assert data["totals"]["tracks"] == 8
        assert data["totals"]["listeners"] > 0
        assert len(data["top"]) == 8
        assert len(data["recent"]) == 5
        top_plays = [row["plays"] for row in data["top"]]
        assert top_plays == sorted(top_plays, reverse=True)
        recent_at = [row["at"] for row in data["recent"]]
        assert recent_at == sorted(recent_at, reverse=True)
        assert len(data["timeline"]) == 24
        assert {"hour", "total", "tiers"} <= set(data["timeline"][0].keys())


def test_t1_e3_plays_first_party_artwork_rewriting(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        data = client.get("/api/plays").json()
        art_urls = []
        for row in data["top"] + data["recent"]:
            art = row.get("artwork")
            assert art is None or art.startswith("/art/"), art
            assert "qobuz.com" not in (art or "")
            assert "tidal.com" not in (art or "")
            if art:
                art_urls.append(art)
        assert len(art_urls) >= 1
        img_resp = client.get(art_urls[0])
        assert img_resp.status_code == 200
        assert img_resp.headers["content-type"].startswith("image/")


def _filtered_rows(payload: dict) -> list[dict]:
    if "search_results" in payload:
        return list(payload["search_results"])
    return list(payload["top"])


def test_t1_e4_plays_search_query(std_server: IsolatedUvicornServer) -> None:
    """M1/PROJECT.md Contract: GET /api/plays?q=daft filters matching tracks."""
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/api/plays", params={"q": "daft"})
        assert resp.status_code == 200
        data = resp.json()
        assert "totals" in data and "timeline" in data
        rows = _filtered_rows(data)
        titles = {r["title"] for r in rows}
        assert titles == {"Instant Crush"}
        recent_titles = {r["title"] for r in data.get("recent", [])}
        assert recent_titles == {"Instant Crush"}


def test_t1_e5_plays_tier_filter(std_server: IsolatedUvicornServer) -> None:
    """M1/PROJECT.md Contract: GET /api/plays?tier=dolby_atmos filters tracks matching tier."""
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/api/plays", params={"tier": "dolby_atmos"})
        assert resp.status_code == 200
        data = resp.json()
        rows = _filtered_rows(data)
        titles = {r["title"] for r in rows}
        assert titles == {"Bad Guy", "Money"}
        recent_rows = data.get("recent", [])
        assert len(recent_rows) >= 1
        assert all(r["tier"] == "dolby_atmos" for r in recent_rows)
        assert {r["title"] for r in recent_rows} == {"Bad Guy"}


def test_t2_e1_plays_empty_and_whitespace_query(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        baseline = client.get("/api/plays").json()
        for q in ("", "   "):
            resp = client.get("/api/plays", params={"q": q})
            assert resp.status_code == 200
            data = resp.json()
            assert data["totals"] == baseline["totals"]
            assert len(data["top"]) == len(baseline["top"])


def test_t2_e2_plays_no_match_query(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/api/plays", params={"q": "no-match-zzzz-9999"})
        assert resp.status_code == 200
        data = resp.json()
        assert _filtered_rows(data) == []
        assert "totals" in data and "timeline" in data


def test_t2_e3_plays_special_chars_and_xss(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for q in ("<script>alert(1)</script>", 'Daft & Punk "\'+*[]'):
            resp = client.get("/api/plays", params={"q": q})
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("application/json")
            assert resp.headers.get("x-content-type-options") == "nosniff"
            data = resp.json()
            assert _filtered_rows(data) == []


def test_t2_e4_plays_limit_bva(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for lim in ("0", "-5", "1"):
            resp = client.get("/api/plays", params={"limit": lim})
            assert resp.status_code == 200
            assert len(_filtered_rows(resp.json())) == 1
        for lim in ("50", "999"):
            resp = client.get("/api/plays", params={"limit": lim})
            assert resp.status_code == 200
            assert len(_filtered_rows(resp.json())) == 8


def test_t2_e5_plays_invalid_limit_type(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/api/plays", params={"limit": "abc"})
        assert resp.status_code == 400
        assert resp.json().get("code") == "invalid_request"


def test_t2_e6_plays_unknown_tier(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/api/plays", params={"tier": "invalid_tier_xyz"})
        assert resp.status_code == 200
        data = resp.json()
        assert "totals" in data
        assert _filtered_rows(data) == []
        assert data.get("recent", []) == []


def test_t2_e7_plays_404_when_disabled(noplays_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=noplays_server.base_url, timeout=5.0) as client:
        for params in ({}, {"q": "daft"}):
            resp = client.get("/api/plays", params=params)
            assert resp.status_code == 404
            assert resp.json().get("code") == "not_found"


# ── Feature Area F: Public Support & Donations API ──────────────────────────


def test_t1_f1_support_default_empty(noplays_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=noplays_server.base_url, timeout=5.0) as client:
        resp = client.get("/api/support")
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.json() == {"demo": False, "assets": []}


def test_t1_f2_support_demo_mode(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        data = client.get("/api/support").json()
        assert data["demo"] is True
        assert len(data["assets"]) == 3
        for item in data["assets"]:
            assert {"id", "name", "network", "symbol", "address"} <= set(item.keys())


def test_t1_f3_support_btc_only() -> None:
    with IsolatedUvicornServer({"DONATE_BTC": "bc1qtestonlyaddress123"}, seed_plays=False, seed_artwork=False, seed_stats=False) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            data = client.get("/api/support").json()
            assert data["demo"] is False
            assert len(data["assets"]) == 1
            assert data["assets"][0]["id"] == "btc"
            assert data["assets"][0]["symbol"] == "BTC"
            assert data["assets"][0]["address"] == "bc1qtestonlyaddress123"


def test_t1_f4_support_all_three_order() -> None:
    with IsolatedUvicornServer(
        {
            "DONATE_BTC": "bc1q_all_1",
            "DONATE_ETH": "0x_all_2",
            "DONATE_USDC_ERC20": "0x_all_3",
        },
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            data = client.get("/api/support").json()
            assert data["demo"] is False
            assert [a["id"] for a in data["assets"]] == ["btc", "eth", "usdc"]


def test_t1_f5_support_whitespace_trimming() -> None:
    with IsolatedUvicornServer({"DONATE_ETH": "  0xabc123  "}, seed_plays=False, seed_artwork=False, seed_stats=False) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            data = client.get("/api/support").json()
            assert data["assets"][0]["address"] == "0xabc123"


def test_t2_f1_support_real_beats_demo() -> None:
    with IsolatedUvicornServer(
        {"DONATE_BTC": "bc1qrealaddress", "DONATE_DEMO": "true"},
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            data = client.get("/api/support").json()
            assert data["demo"] is False
            assert len(data["assets"]) == 1
            assert data["assets"][0]["address"] == "bc1qrealaddress"


def test_t2_f2_support_whitespace_only_disabled() -> None:
    with IsolatedUvicornServer(
        {"DONATE_BTC": "   ", "DONATE_DEMO": "false"},
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            assert client.get("/api/support").json() == {"demo": False, "assets": []}


def test_t2_f3_support_whitespace_only_falls_back_to_demo() -> None:
    with IsolatedUvicornServer(
        {"DONATE_BTC": "   ", "DONATE_DEMO": "true"},
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            data = client.get("/api/support").json()
            assert data["demo"] is True
            assert len(data["assets"]) == 3


def test_t2_f4_support_usdc_network_label() -> None:
    with IsolatedUvicornServer({"DONATE_USDC_ERC20": "0xusdc999"}, seed_plays=False, seed_artwork=False, seed_stats=False) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            data = client.get("/api/support").json()
            assert len(data["assets"]) == 1
            assert data["assets"][0]["id"] == "usdc"
            assert data["assets"][0]["network"] == "Ethereum (ERC-20)"


def test_t2_f5_support_disallowed_method(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.post("/api/support")
        assert resp.status_code == 405
        assert resp.json().get("code") == "405"


# ── Feature Area G: Health, Readiness & Stats ───────────────────────────────


def test_t1_g1_healthz_unconfigured(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["overall_status"] == "outage"
        assert data["path_secret"] is False
        assert data["auth_enabled"] is False
        assert data["qobuz"]["configured"] is False


def test_t1_g2_readyz_unconfigured(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.headers.get("cache-control") == "no-store"
        data = resp.json()
        assert data["ready"] is False
        assert data["configured"] is False
        assert data["tidal_ready"] is False


def test_t1_g3_stats_hidden_without_token(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        resp = client.get("/stats")
        assert resp.status_code == 404
        assert resp.json().get("code") == "not_found"


def test_t1_g4_stats_authorized() -> None:
    with IsolatedUvicornServer({"STATS_TOKEN": DEFAULT_STATS_TOKEN}, seed_plays=False, seed_artwork=False, seed_stats=True) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            resp = client.get("/stats", headers={"X-Stats-Token": DEFAULT_STATS_TOKEN})
            assert resp.status_code == 200
            assert resp.headers.get("cache-control") == "no-store"
            data = resp.json()
            for key in ("requests", "by_endpoint", "statuses", "users", "cache", "providers"):
                assert key in data


def test_t1_g5_secret_healthz_and_readyz_both_paths(secret_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=secret_server.base_url, timeout=5.0) as client:
        for path in ("/healthz", f"/s/{DEFAULT_PATH_SECRET}/healthz"):
            r = client.get(path)
            assert r.status_code == 200
            assert r.json()["path_secret"] is True
        for path in ("/readyz", f"/s/{DEFAULT_PATH_SECRET}/readyz"):
            r = client.get(path)
            assert r.status_code == 503


def test_t2_g1_stats_missing_header() -> None:
    with IsolatedUvicornServer({"STATS_TOKEN": DEFAULT_STATS_TOKEN}, seed_plays=False, seed_artwork=False, seed_stats=False) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            resp = client.get("/stats")
            assert resp.status_code == 401
            assert resp.json().get("code") == "unauthorized"


def test_t2_g2_stats_wrong_token() -> None:
    wrong = DEFAULT_STATS_TOKEN[:-1] + ("0" if DEFAULT_STATS_TOKEN[-1] != "0" else "1")
    with IsolatedUvicornServer({"STATS_TOKEN": DEFAULT_STATS_TOKEN}, seed_plays=False, seed_artwork=False, seed_stats=False) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            resp = client.get("/stats", headers={"X-Stats-Token": wrong})
            assert resp.status_code == 401
            assert resp.json().get("code") == "unauthorized"


def test_t2_g3_stats_query_param_rejected() -> None:
    with IsolatedUvicornServer({"STATS_TOKEN": DEFAULT_STATS_TOKEN}, seed_plays=False, seed_artwork=False, seed_stats=False) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            resp = client.get("/stats", params={"token": DEFAULT_STATS_TOKEN})
            assert resp.status_code == 401


def test_t2_g4_stats_prefixed_with_addon_secret() -> None:
    with IsolatedUvicornServer(
        {"ADDON_PATH_SECRET": DEFAULT_PATH_SECRET, "STATS_TOKEN": DEFAULT_STATS_TOKEN},
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            assert client.get("/stats", headers={"X-Stats-Token": DEFAULT_STATS_TOKEN}).status_code == 404
            ok = client.get(
                f"/s/{DEFAULT_PATH_SECRET}/stats",
                headers={"X-Stats-Token": DEFAULT_STATS_TOKEN},
            )
            assert ok.status_code == 200


def test_t2_g5_readyz_when_qobuz_configured_loopback_safe() -> None:
    """Loopback-only QOBUZ_BASE_URL verifies /readyz -> 200 when Qobuz credentials are configured."""
    with IsolatedUvicornServer(
        {
            "PRIMARY_PROVIDER": "qobuz",
            "QOBUZ_BASE_URL": "http://127.0.0.1:9/api.json/0.2",
            "QOBUZ_USER_AUTH_TOKEN": "e2e_dummy_token",
            "QOBUZ_USER_ID": "12345",
            "QOBUZ_APP_ID": "123456",
            "QOBUZ_APP_SECRET": "0123456789abcdef0123456789abcdef",
        },
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            resp = client.get("/readyz")
            assert resp.status_code == 200
            data = resp.json()
            assert data["ready"] is True
            assert data["configured"] is True


# ── Feature Area H: Access Modes, Admin/Vault Security & Data Isolation ────


def test_t1_h1_secret_prefix_addon_vs_root_public(secret_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=secret_server.base_url, timeout=5.0) as client:
        for path in (
            f"/s/{DEFAULT_PATH_SECRET}/manifest.json",
            f"/s/{DEFAULT_PATH_SECRET}/search?q=test",
            f"/s/{DEFAULT_PATH_SECRET}/",
            "/status",
            "/plays",
            "/support",
            "/about",
            "/api/public-stats",
        ):
            assert client.get(path).status_code == 200, path


def test_t1_h2_internal_admin_summary() -> None:
    with IsolatedUvicornServer({"STATS_TOKEN": DEFAULT_STATS_TOKEN}, seed_plays=False, seed_artwork=False, seed_stats=False) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            resp = client.get("/internal/admin/summary", headers={"X-Admin-Token": DEFAULT_STATS_TOKEN})
            assert resp.status_code == 200
            data = resp.json()
            for key in ("primary_provider", "qobuz_accounts", "tidal_accounts", "vault_url"):
                assert key in data


def test_t1_h3_vault_api_auth_headers() -> None:
    with IsolatedUvicornServer({"STATS_TOKEN": DEFAULT_STATS_TOKEN}, seed_plays=False, seed_artwork=False, seed_stats=False) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            r1 = client.get("/admin/api/summary", headers={"X-Vault-Key": DEFAULT_STATS_TOKEN})
            assert r1.status_code == 200
            r2 = client.get(
                "/admin/api/summary",
                headers={"Authorization": f"Bearer {DEFAULT_STATS_TOKEN}"},
            )
            assert r2.status_code == 200


def test_t1_h4_admin_path_secret_custom_vault() -> None:
    with IsolatedUvicornServer(
        {"ADMIN_PATH_SECRET": DEFAULT_ADMIN_SECRET, "STATS_TOKEN": DEFAULT_STATS_TOKEN},
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            html_resp = client.get(f"/{DEFAULT_ADMIN_SECRET}")
            assert html_resp.status_code == 200
            assert html_resp.headers["content-type"].startswith("text/html")
            api_resp = client.get(
                f"/{DEFAULT_ADMIN_SECRET}/api/summary",
                headers={"X-Vault-Key": DEFAULT_STATS_TOKEN},
            )
            assert api_resp.status_code == 200


def test_t1_h5_data_dir_isolation(std_server: IsolatedUvicornServer) -> None:
    assert std_server.data_dir is not None
    assert (std_server.data_dir / "plays.json").is_file()
    assert (std_server.data_dir / "stats.json").is_file()
    assert std_server.data_dir != (REPO_ROOT / "data")
    # Ensure repo root never got polluted with test sentinel files
    repo_art = REPO_ROOT / "data" / "artwork"
    for key in std_server.seed_summary["artwork_keys"]:
        assert not (repo_art / f"{key}.img").exists()


def test_t2_h1_secret_blocks_unprefixed_addon_routes(secret_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=secret_server.base_url, timeout=5.0) as client:
        for path in ("/", "/manifest.json", "/search?q=test", "/stream/123", f"/dash/{'a' * 16}.mpd"):
            assert client.get(path).status_code == 404, path


@pytest.mark.parametrize(
    "bad_env",
    [
        {"ADDON_PATH_SECRET": "tooshort"},
        {"STATS_TOKEN": "has space 12345678"},
        {"ADMIN_PATH_SECRET": "bad/char!0123456789"},
    ],
)
def test_t2_h2_invalid_secret_boot_rejection(bad_env: dict[str, str]) -> None:
    proc = probe_boot_failure(bad_env)
    assert proc.returncode != 0


def test_t2_h3_cloudflare_header_blocks_internal_admin() -> None:
    with IsolatedUvicornServer({"STATS_TOKEN": DEFAULT_STATS_TOKEN}, seed_plays=False, seed_artwork=False, seed_stats=False) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            for cf_header in (
                {"CF-Ray": "e2e-ray-1"},
                {"CF-Connecting-IP": "1.2.3.4"},
                {"CF-Visitor": '{"scheme":"https"}'},
            ):
                resp = client.get(
                    "/internal/admin/summary",
                    headers={"X-Admin-Token": DEFAULT_STATS_TOKEN, **cf_header},
                )
                assert resp.status_code == 404


def test_t2_h4_admin_path_secret_hides_default_paths() -> None:
    with IsolatedUvicornServer(
        {"ADMIN_PATH_SECRET": DEFAULT_ADMIN_SECRET, "STATS_TOKEN": DEFAULT_STATS_TOKEN},
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            for path in ("/admin", "/vault", "/admin/api/summary"):
                assert client.get(path, headers={"X-Vault-Key": DEFAULT_STATS_TOKEN}).status_code == 404


def test_t2_h5_vault_bruteforce_rate_limit() -> None:
    with IsolatedUvicornServer({"STATS_TOKEN": DEFAULT_STATS_TOKEN}, seed_plays=False, seed_artwork=False, seed_stats=False) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            for idx in range(20):
                resp = client.get("/admin/api/summary", headers={"X-Vault-Key": "wrong_key_0123456789"})
                assert resp.status_code == 401, f"attempt {idx + 1} returned {resp.status_code}"
            blocked = client.get("/admin/api/summary", headers={"X-Vault-Key": "wrong_key_0123456789"})
            assert blocked.status_code == 429
            assert int(blocked.headers.get("retry-after", "0")) >= 1


def test_t2_h6_empty_stats_token_hides_all_admin(std_server: IsolatedUvicornServer) -> None:
    with httpx.Client(base_url=std_server.base_url, timeout=5.0) as client:
        for path in ("/internal/admin/summary", "/admin/api/summary", "/vault/api/summary"):
            assert client.get(path).status_code == 404


def test_t2_h7_addon_auth_without_hosts_boot_rejection() -> None:
    proc = probe_boot_failure(
        {"ADDON_AUTH_ENABLED": "true", "ADDON_HOSTS": "", "PUBLIC_BASE_URL": ""}
    )
    assert proc.returncode != 0
