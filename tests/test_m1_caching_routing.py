"""Tests for M1 Feature 7: asset hashing, static caching, _public_display_url, _endpoint_name, sitemap/robots."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.dashboard import _asset_version
from app.main import _endpoint_name, _public_display_url, _valid_host, create_app

from .conftest import FakeQobuz, make_settings


def test_asset_version_hashes_extended_types_deterministically(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "shader.glsl").write_text("void main() {}", "utf-8")
    (tmp_path / "b" / "shader.glsl").write_text("void main() { gl_FragColor=vec4(1.0); }", "utf-8")
    (tmp_path / "app.mjs").write_text("export const x = 1;", "utf-8")
    (tmp_path / "scene.json").write_text('{"ok": true}', "utf-8")
    (tmp_path / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "font.woff2").write_bytes(b"wOF2")
    (tmp_path / "OFL.txt").write_text("ignored license", "utf-8")

    v1 = _asset_version(tmp_path)
    v2 = _asset_version(tmp_path)
    assert v1 == v2
    assert len(v1) == 10

    # Changing an ignored .txt file does not change the hash
    (tmp_path / "OFL.txt").write_text("changed license", "utf-8")
    assert _asset_version(tmp_path) == v1

    # Changing .glsl, .mjs, .json, .png, or .woff2 changes the hash
    for rel, new_bytes in (
        ("a/shader.glsl", b"void main() { discard; }"),
        ("app.mjs", b"export const x = 2;"),
        ("scene.json", b'{"ok": false}'),
        ("icon.png", b"\x89PNG\r\n\x1a\nmodified"),
        ("font.woff2", b"wOF2modified"),
    ):
        prev = _asset_version(tmp_path)
        (tmp_path / rel).write_bytes(new_bytes)
        assert _asset_version(tmp_path) != prev


@pytest.mark.asyncio
async def test_assets_cache_control_headers(http: httpx.AsyncClient) -> None:
    ok_resp = await http.get("/assets/confih.css")
    assert ok_resp.status_code == 200
    assert (
        ok_resp.headers["cache-control"]
        == "public, max-age=300, stale-while-revalidate=86400"
    )

    missing_resp = await http.get("/assets/does-not-exist.js")
    assert missing_resp.status_code == 404
    assert "max-age=300" not in missing_resp.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_public_display_url_priority_proxy_gating_and_secret_stripping(
    tmp_path: Path,
) -> None:
    # 1. Fallback when request=None and public_base_url=""
    s_default = make_settings(tmp_path / "d")
    assert _public_display_url(s_default) == "https://bitchord.confih.tech"

    # 2. Configured public_base_url strips /s/<secret> when addon_path_secret is set
    secret = "secret0123456789"
    s_cfg = make_settings(
        tmp_path / "cfg",
        public_base_url=f"https://gw.example.com/s/{secret}",
        addon_path_secret=secret,
    )
    assert _public_display_url(s_cfg) == "https://gw.example.com"

    # 3. Dynamic request origin when public_base_url="" and trust_proxy_headers=False
    app_untrusted = create_app(
        make_settings(tmp_path / "untrusted", trust_proxy_headers=False),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app_untrusted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_untrusted),
            base_url="http://127.0.0.1:8899",
        ) as client:
            resp = await client.get(
                "/api/public-stats",
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "spoofed.example.com",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["addon"]["display_url"] == "http://127.0.0.1:8899"
            plays_resp = await client.get("/api/plays")
            assert plays_resp.json()["url"] == "http://127.0.0.1:8899"

    # 4. Forwarded headers honored only when trust_proxy_headers=True and host is valid
    app_trusted = create_app(
        make_settings(tmp_path / "trusted", trust_proxy_headers=True),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app_trusted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_trusted),
            base_url="http://127.0.0.1:8899",
        ) as client:
            good = await client.get(
                "/api/public-stats",
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "gw.example.com",
                },
            )
            assert good.json()["addon"]["display_url"] == "https://gw.example.com"

            for bad_host in ("evil.com/path", "user@evil.com", "evil.com<script>"):
                bad = await client.get(
                    "/api/public-stats",
                    headers={
                        "X-Forwarded-Proto": "https",
                        "X-Forwarded-Host": bad_host,
                    },
                )
                assert bad.json()["addon"]["display_url"] == "https://127.0.0.1:8899"


def test_valid_host_validation_accepts_safe_hosts_and_rejects_malformed() -> None:
    for good in (
        "127.0.0.1",
        "127.0.0.1:8899",
        "localhost",
        "localhost:8899",
        "testserver",
        "bitchord.confih.tech",
        "Bitchord.Confih.Tech",
        "[::1]",
        "[::1]:8899",
        "[2001:db8::1]:443",
        "[2001:DB8::1]",
        "[2001:db8:0:0::1]",
        "edge.confih.tech",
        f"{'a' * 63}.com",
    ):
        assert _valid_host(good) == good, good

    for bad in (
        "[invalid",
        "[[::1]]",
        "[::1]extra",
        "evil.com:abc",
        "evil.com:99999",
        "evil.com:0",
        "evil.com:",
        "[::1]:",
        "[127.0.0.1]",
        "a:b:c",
        "evil.com/path",
        "user@evil.com",
        "evil.com<script>",
        "-bad",
        "bad-",
        "---",
        "...",
        ".evil.com",
        "a..b.com",
        "evil.com.",
        "-bad.com",
        "bad-.com",
        "a.-b.com",
        "a.b-.com",
        "999.999.999.999",
        "12345",
        "foo.123",
        f"{'a' * 64}.com",
        "a." + "b" * 64 + ".com",
        "a." * 128 + "com",
        "",
    ):
        assert _valid_host(bad) == "", bad


@pytest.mark.asyncio
async def test_proxy_forwarded_host_malformed_never_500s_and_allows_ipv6(
    tmp_path: Path,
) -> None:
    app_trusted = create_app(
        make_settings(tmp_path / "trusted_adv", trust_proxy_headers=True),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app_trusted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_trusted),
            base_url="http://127.0.0.1:8899",
        ) as client:
            for bad_host in (
                "[invalid",
                "[[::1]]",
                "[::1]extra",
                "evil.com:abc",
                "evil.com:99999",
                "a:b:c",
                "-bad",
                "bad-",
                "...",
                ".evil.com",
                "a..b.com",
                "evil.com.",
                "999.999.999.999",
            ):
                for ep in (
                    "/robots.txt",
                    "/sitemap.xml",
                    "/api/public-stats",
                    "/api/plays",
                    "/api/overview",
                ):
                    resp = await client.get(ep, headers={"X-Forwarded-Host": bad_host})
                    assert resp.status_code == 200, (bad_host, ep)
                    assert bad_host not in resp.text
                    assert "127.0.0.1:8899" in resp.text

            ipv6_resp = await client.get(
                "/api/public-stats",
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "[::1]:8899"},
            )
            assert ipv6_resp.status_code == 200
            assert ipv6_resp.json()["addon"]["display_url"] == "https://[::1]:8899"


@pytest.mark.asyncio
async def test_endpoint_name_classifies_all_monitor_routes_without_inflating_addon_totals(
    tmp_path: Path,
) -> None:
    assert _endpoint_name("/api/overview", "") == "overview"
    assert _endpoint_name("/badge.svg", "") == "badge"
    assert _endpoint_name("/api/events", "") == "other"
    assert _endpoint_name("/s/abc", "/s/abc") == "root"

    token = "t" * 32
    settings = make_settings(tmp_path, stats_token=token)
    app = create_app(settings, transport=httpx.MockTransport(FakeQobuz()))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            for path in (
                "/plays",
                "/api/plays",
                "/support",
                "/api/support",
                "/about",
                "/art/missing0000000000",
                "/assets/confih.css",
                "/badge.svg",
                "/api/overview",
            ):
                await client.get(path)

            stats_resp = await client.get("/stats", headers={"X-Stats-Token": token})
            assert stats_resp.status_code == 200
            body = stats_resp.json()
            assert body["total_requests_all_time"] == 0
            ep = body["by_endpoint"]
            for key in (
                "plays_page",
                "api_plays",
                "support_page",
                "api_support",
                "about_page",
                "artwork",
                "assets",
                "badge",
                "overview",
            ):
                assert ep[key] == 1, key


@pytest.mark.asyncio
async def test_sitemap_and_robots_respect_origin_public_plays_and_path_secret(
    tmp_path: Path,
) -> None:
    # 1. Public plays enabled, no secret
    app1 = create_app(
        make_settings(tmp_path / "a", public_plays=True),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app1):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app1),
            base_url="http://127.0.0.1:8899",
        ) as client:
            sm = await client.get("/sitemap.xml")
            assert sm.status_code == 200
            # Only indexable pages are listed. /plays and /support answer with
            # X-Robots-Tag: noindex, so they must not appear here.
            for loc in (
                "http://127.0.0.1:8899/",
                "http://127.0.0.1:8899/manifest.json",
                "http://127.0.0.1:8899/status",
                "http://127.0.0.1:8899/about",
            ):
                assert f"<loc>{loc}</loc>" in sm.text
            assert "/plays</loc>" not in sm.text
            assert "/support</loc>" not in sm.text

            rb = await client.get("/robots.txt")
            assert rb.status_code == 200
            assert "Allow: /manifest.json" in rb.text
            assert "Sitemap: http://127.0.0.1:8899/sitemap.xml" in rb.text

    # 2. Public plays disabled + path secret active
    secret = "s" * 24
    app2 = create_app(
        make_settings(
            tmp_path / "b",
            public_plays=False,
            addon_path_secret=secret,
        ),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app2):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2),
            base_url="http://127.0.0.1:8899",
        ) as client:
            sm = await client.get("/sitemap.xml")
            assert secret not in sm.text
            assert "<loc>http://127.0.0.1:8899/</loc>" not in sm.text
            assert "/manifest.json" not in sm.text
            assert "/plays" not in sm.text
            assert "<loc>http://127.0.0.1:8899/status</loc>" in sm.text

            rb = await client.get("/robots.txt")
            assert secret not in rb.text
            assert "Allow: /manifest.json" not in rb.text
            assert "Disallow: /plays" in rb.text
            assert "Disallow: /admin" in rb.text
            assert "Disallow: /vault" in rb.text


@pytest.mark.asyncio
async def test_dash_base_does_not_double_path_secret_and_honors_forwarded_proto(
    tmp_path: Path,
) -> None:
    from tests.test_tidal_failover import DualMockBackend

    secret = "myaddonsecret123456"
    admin_tok = "t" * 32
    fake_q = FakeQobuz()
    dual = DualMockBackend(fake_q)
    dual.tidal_dash = True

    # 1. PUBLIC_BASE_URL includes /s/<secret>
    settings1 = make_settings(
        tmp_path / "a",
        stats_token=admin_tok,
        addon_path_secret=secret,
        public_base_url=f"https://gw.example.com/s/{secret}",
    )
    app1 = create_app(settings1, transport=httpx.MockTransport(dual))
    async with LifespanManager(app1):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app1),
            base_url="http://testserver",
        ) as client:
            await client.post(
                "/internal/admin/tidal/add",
                headers={"X-Admin-Token": admin_tok},
                json={"access_token": "tidal-tok-123"},
            )
            st = await client.get(
                f"/s/{secret}/stream/tidal:77889900",
                params={"quality": "max"},
            )
            assert st.status_code == 200
            mpd_url = st.json()["url"]
            assert mpd_url.startswith(f"https://gw.example.com/s/{secret}/dash/")
            assert f"/s/{secret}/s/{secret}/" not in mpd_url
            mpd_path = mpd_url.removeprefix("https://gw.example.com")
            assert (await client.get(mpd_path)).status_code == 200

    # 2. PUBLIC_BASE_URL empty behind HTTPS reverse proxy
    settings2 = make_settings(
        tmp_path / "b",
        stats_token=admin_tok,
        addon_path_secret=secret,
        public_base_url="",
        trust_proxy_headers=True,
    )
    app2 = create_app(settings2, transport=httpx.MockTransport(dual))
    async with LifespanManager(app2):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2),
            base_url="http://127.0.0.1:8899",
        ) as client:
            await client.post(
                "/internal/admin/tidal/add",
                headers={"X-Admin-Token": admin_tok},
                json={"access_token": "tidal-tok-123"},
            )
            st = await client.get(
                f"/s/{secret}/stream/tidal:77889900",
                params={"quality": "max"},
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "gw.example.com",
                },
            )
            assert st.status_code == 200
            assert st.json()["url"].startswith(f"https://gw.example.com/s/{secret}/dash/")


@pytest.mark.asyncio
async def test_client_ip_ignores_spoofed_cf_connecting_ip_when_trust_cf_headers_false(
    tmp_path: Path,
) -> None:
    token = "vault_tok_0123456789abcdef01234567"
    app = create_app(
        make_settings(
            tmp_path / "caddy",
            stats_token=token,
            trust_proxy_headers=True,
            trust_cf_headers=False,
        ),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            for _ in range(20):
                r = await client.get(
                    "/admin/api/summary",
                    headers={"X-Vault-Key": "wrong", "X-Forwarded-For": "203.0.113.5"},
                )
                assert r.status_code == 401

            r21 = await client.get(
                "/admin/api/summary",
                headers={"X-Vault-Key": "wrong", "X-Forwarded-For": "203.0.113.5"},
            )
            assert r21.status_code == 429

            # Spoofing CF-Connecting-IP (even with CF-Ray) must NOT bypass X-Forwarded-For limit
            r22 = await client.get(
                "/admin/api/summary",
                headers={
                    "X-Vault-Key": "wrong",
                    "X-Forwarded-For": "203.0.113.5",
                    "CF-Connecting-IP": "198.51.100.99",
                    "CF-Ray": "spoofed-ray",
                },
            )
            assert r22.status_code == 429

    # When trust_cf_headers=True and CF-Ray is present, CF-Connecting-IP is honored
    app_cf = create_app(
        make_settings(
            tmp_path / "cf",
            stats_token=token,
            trust_proxy_headers=True,
            trust_cf_headers=True,
        ),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app_cf):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_cf),
            base_url="http://127.0.0.1:8899",
        ) as client:
            for _ in range(21):
                await client.get(
                    "/admin/api/summary",
                    headers={
                        "X-Vault-Key": "wrong",
                        "X-Forwarded-For": "203.0.113.5",
                        "CF-Connecting-IP": "198.51.100.10",
                        "CF-Ray": "cf-real-ray",
                    },
                )
            # Same XFF, different CF-Connecting-IP with CF-Ray is treated as distinct CF client
            r_other_cf = await client.get(
                "/admin/api/summary",
                headers={
                    "X-Vault-Key": "wrong",
                    "X-Forwarded-For": "203.0.113.5",
                    "CF-Connecting-IP": "198.51.100.11",
                    "CF-Ray": "cf-real-ray",
                },
            )
            assert r_other_cf.status_code == 401


@pytest.mark.asyncio
async def test_root_vault_api_unmounted_and_runtime_secret_redaction(
    tmp_path: Path,
) -> None:
    import logging

    from app.logging_setup import RedactionFilter, register_secrets

    filt = RedactionFilter(("initial-secret-value",))
    runtime_tok = "dyn-qobuz-runtime-secret-998877"
    register_secrets([runtime_tok])
    rec = logging.LogRecord(
        name="confih.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Upstream call carried %s",
        args=(runtime_tok,),
        exc_info=None,
    )
    assert filt.filter(rec) is True
    assert runtime_tok not in rec.getMessage()
    assert "***" in rec.getMessage()

    token = "v" * 32
    app = create_app(
        make_settings(tmp_path / "scoped", stats_token=token),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            # Root /api/summary must NOT be mounted when ADMIN_PATH_SECRET is empty
            root_res = await client.get("/api/summary", headers={"X-Vault-Key": token})
            assert root_res.status_code == 404

            # Scoped /admin/api/summary works with valid X-Vault-Key
            admin_res = await client.get(
                "/admin/api/summary", headers={"X-Vault-Key": token}
            )
            assert admin_res.status_code == 200
