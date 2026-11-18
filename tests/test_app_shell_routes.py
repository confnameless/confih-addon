"""Tests for M2 unified APP_SHELL_HTML, route rendering, secrecy, and admin vault isolation."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.dashboard import ASSET_VERSION
from app.main import create_app

from .conftest import FakeQobuz, make_settings


class _ShellInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.html_attrs: dict[str, str | None] = {}
        self.body_attrs: dict[str, str | None] = {}
        self.nav_links: list[dict[str, str | None]] = []
        self.brand_href: str | None = None
        self.sections: dict[str, dict[str, str | None]] = {}
        self.covers_block_attrs: dict[str, str | None] = {}
        self.boot_text = ""
        self.inline_script_nonces: list[str] = []
        self._in_nav = False
        self._in_boot = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if "id" in attr_map and attr_map["id"]:
            self.ids.append(attr_map["id"])
        if tag == "html":
            self.html_attrs = attr_map
        elif tag == "body":
            self.body_attrs = attr_map
        elif tag == "nav" and "site-nav" in (attr_map.get("class") or ""):
            self._in_nav = True
        elif tag == "a" and self._in_nav:
            self.nav_links.append(attr_map)
        elif tag == "a" and "brand" in (attr_map.get("class") or ""):
            self.brand_href = attr_map.get("href")
        elif tag == "section" and attr_map.get("id") in (
            "view-status",
            "view-plays",
            "view-about",
            "view-support",
        ):
            self.sections[attr_map["id"]] = attr_map
        elif tag == "div" and attr_map.get("id") == "support-covers-block":
            self.covers_block_attrs = attr_map
        elif tag == "script" and attr_map.get("id") == "confih-boot":
            self._in_boot = True
        elif tag == "script" and not attr_map.get("src"):
            # Inline boot script: it must present the nonce the CSP names.
            self.inline_script_nonces.append(attr_map.get("nonce") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav":
            self._in_nav = False
        elif tag == "script" and self._in_boot:
            self._in_boot = False

    def handle_data(self, data: str) -> None:
        if self._in_boot:
            self.boot_text += data


def _parse(body: str) -> _ShellInspector:
    parser = _ShellInspector()
    parser.feed(body)
    return parser


@pytest.mark.asyncio
async def test_unified_shell_renders_all_six_public_routes(tmp_path: Path) -> None:
    app = create_app(
        make_settings(tmp_path),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            expected_routes = [
                ("/", "status", "status", "status"),
                ("/status", "status", "status", "status"),
                ("/monitor", "status", "status", "status"),
                ("/plays", "plays", "plays", "plays"),
                ("/plays/", "plays", "plays", "plays"),
                ("/about", "about", "about", "about"),
                ("/about/", "about", "about", "about"),
                ("/support", "support", "support", "support"),
                ("/support/", "support", "support", "support"),
            ]
            for path, html_view, active_view, nav_key in expected_routes:
                resp = await client.get(path, headers={"Accept": "text/html"})
                assert resp.status_code == 200, path
                assert "text/html" in resp.headers["content-type"]
                csp = resp.headers.get("content-security-policy", "")
                assert "'self'" in csp
                for forbidden_origin in ("cloudflareinsights.com", "http://", "https://"):
                    assert forbidden_origin not in csp, f"{forbidden_origin} leaked in CSP on {path}"
                assert resp.headers.get("x-content-type-options") == "nosniff"
                assert resp.headers.get("x-frame-options") == "DENY"

                parsed = _parse(resp.text)
                assert parsed.html_attrs.get("data-initial-view") == html_view, path
                assert parsed.body_attrs.get("data-initial-view") == active_view, path
                assert len(parsed.ids) == len(set(parsed.ids)), f"duplicate IDs on {path}"
                assert parsed.brand_href == "/"
                assert len(parsed.nav_links) == 4

                current_links = [
                    link for link in parsed.nav_links if link.get("aria-current") == "page"
                ]
                assert len(current_links) == 1
                assert current_links[0].get("data-nav-view") == nav_key

                for view_id, view_name in (
                    ("view-status", "status"),
                    ("view-plays", "plays"),
                    ("view-about", "about"),
                    ("view-support", "support"),
                ):
                    assert view_id in parsed.sections
                    is_hidden = "hidden" in parsed.sections[view_id]
                    assert is_hidden is (view_name != active_view), (path, view_id)

                boot = json.loads(parsed.boot_text)
                assert boot["initial_view"] == html_view
                assert boot["public_plays"] is True
                assert boot["asset_version"] == ASSET_VERSION

                assert 'id="qr-img"' not in resp.text
                assert 'id="qr-loading"' not in resp.text
                assert 'id="qr-svg-slot"' in resp.text
                assert "?v=3100e775e8" not in resp.text

                for asset in (
                    "confih.css",
                    "content.css",
                    "monitor.css",
                    "plays.css",
                    "support.css",
                    "about.css",
                    "theme.js",
                    "site.js",
                    "spatial-gl.js",
                    "router.js",
                    "monitor.js",
                    "support.js",
                    "confih-mark.svg",
                    "confih-logo.svg",
                    "og-card.png",
                    "inter-latin.woff2",
                ):
                    assert f"{asset}?v={ASSET_VERSION}" in resp.text, (path, asset)

                # The plays bundle is fetched on intent, never on first paint,
                # unless the page *is* the plays view.
                plays_bundle = f"plays.js?v={ASSET_VERSION}"
                if active_view == "plays":
                    assert plays_bundle in resp.text, path
                else:
                    assert plays_bundle not in resp.text, path

                # Every inline script must carry the nonce the CSP names,
                # because script-src no longer allows 'unsafe-inline'.
                nonce = parsed.inline_script_nonces
                assert nonce, path
                assert len(set(nonce)) == 1, path
                script_src = csp.split("script-src", 1)[1].split(";", 1)[0]
                assert f"'nonce-{nonce[0]}'" in script_src, (path, script_src)
                assert "'unsafe-inline'" not in script_src, (path, script_src)


@pytest.mark.asyncio
async def test_public_plays_false_omits_plays_nav_footer_and_returns_404(
    tmp_path: Path,
) -> None:
    app = create_app(
        make_settings(tmp_path, public_plays=False),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            for disabled_path in ("/plays", "/plays/", "/api/plays"):
                r404 = await client.get(disabled_path)
                assert r404.status_code == 404, disabled_path

            for path in ("/", "/status", "/monitor", "/about", "/support"):
                resp = await client.get(path, headers={"Accept": "text/html"})
                assert resp.status_code == 200, path
                assert 'href="/plays"' not in resp.text
                assert 'href="/api/plays"' not in resp.text
                assert 'id="view-plays"' not in resp.text
                assert "/assets/plays.js" not in resp.text
                parsed = _parse(resp.text)
                assert len(parsed.nav_links) == 3
                boot = json.loads(parsed.boot_text)
                assert boot["public_plays"] is False
                assert boot["capabilities"]["public_plays"] is False


@pytest.mark.asyncio
async def test_root_content_negotiation_html_json_and_head(tmp_path: Path) -> None:
    app = create_app(
        make_settings(tmp_path),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            html_resp = await client.get("/", headers={"Accept": "text/html"})
            assert html_resp.status_code == 200
            assert "Confih Hi-Res Gateway" in html_resp.text
            assert 'data-initial-view="status"' in html_resp.text

            json_resp = await client.get("/", headers={"Accept": "application/json"})
            assert json_resp.status_code == 200
            assert json_resp.json()["manifest"] == "/manifest.json"
            assert json_resp.json()["resources"] == ["search", "stream"]

            bc_resp = await client.get(
                "/",
                headers={"Accept": "text/html", "User-Agent": "BitChord/2.0"},
            )
            assert bc_resp.status_code == 200
            assert bc_resp.json()["manifest"] == "/manifest.json"

            head_html = await client.head("/", headers={"Accept": "text/html"})
            assert head_html.status_code == 200
            assert "text/html" in head_html.headers["content-type"]

            head_json = await client.head("/", headers={"Accept": "application/json"})
            assert head_json.status_code == 200
            assert "application/json" in head_json.headers["content-type"]


@pytest.mark.asyncio
async def test_dynamic_origin_stamping_and_proxy_sanitization(tmp_path: Path) -> None:
    app = create_app(
        make_settings(tmp_path / "plain", trust_proxy_headers=False),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            resp = await client.get("/", headers={"Accept": "text/html"})
            assert resp.status_code == 200
            assert "bitchord.confih.tech" not in resp.text
            assert "http://127.0.0.1:8899" in resp.text
            parsed = _parse(resp.text)
            boot = json.loads(parsed.boot_text)
            assert boot["public_url"] == "http://127.0.0.1:8899"

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
                "/",
                headers={
                    "Accept": "text/html",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "gw.example.com",
                },
            )
            assert good.status_code == 200
            assert "https://gw.example.com" in good.text

            evil = await client.get(
                "/",
                headers={
                    "Accept": "text/html",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": 'evil.com"><script>alert(1)</script>',
                },
            )
            assert evil.status_code == 200
            assert "alert(1)" not in evil.text
            assert "evil.com" not in evil.text


@pytest.mark.asyncio
async def test_addon_path_secret_root_relative_links_and_non_leakage(
    tmp_path: Path,
) -> None:
    secret = "topsecretpath9876"
    app = create_app(
        make_settings(tmp_path, addon_path_secret=secret),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            s_html = await client.get(f"/s/{secret}/", headers={"Accept": "text/html"})
            assert s_html.status_code == 200
            assert secret not in s_html.text

            s_json = await client.get(f"/s/{secret}/", headers={"Accept": "application/json"})
            assert s_json.status_code == 200
            assert s_json.json()["manifest"] == f"/s/{secret}/manifest.json"

            for path in ("/status", "/plays", "/about", "/support"):
                resp = await client.get(path, headers={"Accept": "text/html"})
                assert resp.status_code == 200, path
                assert secret not in resp.text
                assert "Private path hidden" in resp.text
                parsed = _parse(resp.text)
                assert parsed.brand_href == "/status"
                boot = json.loads(parsed.boot_text)
                assert boot["addon_base_prefix"] == ""
                assert boot["capabilities"]["path_secret"] is True


@pytest.mark.asyncio
async def test_admin_vault_isolation_theme_js_and_hooks(tmp_path: Path) -> None:
    app = create_app(
        make_settings(tmp_path / "open"),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            for path in ("/admin", "/vault"):
                resp = await client.get(path)
                assert resp.status_code == 200
                csp = resp.headers.get("content-security-policy", "")
                assert "'self'" in csp
                for forbidden_origin in ("cloudflareinsights.com", "http://", "https://"):
                    assert forbidden_origin not in csp, f"{forbidden_origin} leaked in CSP on {path}"
                assert "noindex, nofollow" in resp.headers.get("x-robots-tag", "")
                assert '<meta name="robots" content="noindex, nofollow"' in resp.text
                for asset in (
                    "theme.js",
                    "site.js",
                    "admin.js",
                    "confih.css",
                    "admin.css",
                    "inter-latin.woff2",
                    "confih-logo.svg",
                ):
                    assert f"{asset}?v={ASSET_VERSION}" in resp.text
                for forbidden in (
                    "router.js",
                    "spatial-gl.js",
                    "monitor.js",
                    "plays.js",
                    "support.js",
                    "confih-boot",
                    'class="site-nav"',
                ):
                    assert forbidden not in resp.text
                for hook in (
                    "login-box",
                    "vault-form",
                    "vault-key",
                    "toggle-pwd-btn",
                    "unlock-btn",
                    "login-err",
                    "admin-console",
                    "btn-refresh",
                    "btn-clear-cache",
                    "btn-lock",
                    "btn-pri-qobuz",
                    "btn-pri-tidal",
                    "btn-pri-both",
                    "qobuz-list",
                    "tidal-list",
                    "btn-qobuz-add",
                    "btn-tidal-device",
                    "btn-tidal-add",
                    "toast",
                ):
                    assert f'id="{hook}"' in resp.text

    admin_secret = "vaultsecret123456"
    app_secret = create_app(
        make_settings(tmp_path / "secret", admin_path_secret=admin_secret),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app_secret):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_secret),
            base_url="http://127.0.0.1:8899",
        ) as client:
            assert (await client.get("/admin")).status_code == 404
            assert (await client.get("/vault")).status_code == 404
            assert (await client.get(f"/{admin_secret}")).status_code == 200


@pytest.mark.asyncio
async def test_donate_configured_flag_controls_covers_block(tmp_path: Path) -> None:
    app_empty = create_app(
        make_settings(tmp_path / "empty"),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app_empty):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_empty),
            base_url="http://127.0.0.1:8899",
        ) as client:
            resp = await client.get("/support")
            parsed = _parse(resp.text)
            boot = json.loads(parsed.boot_text)
            assert boot["donate_configured"] is False
            assert "hidden" in parsed.covers_block_attrs

    app_btc = create_app(
        make_settings(tmp_path / "btc", donate_btc="bc1qexample"),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app_btc):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_btc),
            base_url="http://127.0.0.1:8899",
        ) as client:
            resp = await client.get("/support")
            parsed = _parse(resp.text)
            boot = json.loads(parsed.boot_text)
            assert boot["donate_configured"] is True
            assert "hidden" not in parsed.covers_block_attrs
            assert re.search(r"<\s*title\s*>", resp.text) is not None


def test_nat64_ssrf_guard_allows_public_and_blocks_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    from app.artwork import _host_is_public

    allowed = ("tidal.com",)

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::6356:aff3", 0, 0, 0))],
    )
    assert _host_is_public("resources.tidal.com", allowed) is True

    for private_nat64 in ("64:ff9b::127.0.0.1", "64:ff9b::10.0.0.1", "64:ff9b::169.254.169.254"):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *a, addr=private_nat64, **k: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, 0, 0, 0))],
        )
        assert _host_is_public("resources.tidal.com", allowed) is False


@pytest.mark.asyncio
async def test_admin_path_secret_is_redacted_from_logs(tmp_path: Path) -> None:
    import io
    import logging

    admin_secret = "vault_secret_slug_987654"
    app = create_app(
        make_settings(tmp_path, admin_path_secret=admin_secret),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        root_logger = logging.getLogger()
        assert root_logger.handlers
        handler = root_logger.handlers[0]
        buf = io.StringIO()
        orig_stream = handler.stream
        handler.stream = buf
        try:
            logging.getLogger("addon").warning(
                "Unhandled error while serving /%s/api/summary",
                admin_secret,
            )
        finally:
            handler.stream = orig_stream
        logged = buf.getvalue()
        assert admin_secret not in logged
        assert "***" in logged


@pytest.mark.asyncio
async def test_exception_tracebacks_redact_admin_and_addon_path_secrets(
    tmp_path: Path,
) -> None:
    import io
    import logging

    admin_secret = "admin_slug_leak_test_9988"
    addon_secret = "addon_path_leak_test_7766"
    qobuz_token = "qobuz_secret_tok_5544"
    app = create_app(
        make_settings(
            tmp_path,
            admin_path_secret=admin_secret,
            addon_path_secret=addon_secret,
            qobuz_user_auth_token=qobuz_token,
        ),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        root_logger = logging.getLogger()
        assert root_logger.handlers
        handler = root_logger.handlers[0]
        buf = io.StringIO()
        orig_stream = handler.stream
        handler.stream = buf
        captured_exc_info = None
        try:
            try:
                raise ValueError(
                    f"Failed /{admin_secret}/api /s/{addon_secret}/stream?user_auth_token={qobuz_token}"
                )
            except ValueError as exc:
                captured_exc_info = (type(exc), exc, exc.__traceback__)
                logging.getLogger("addon").exception(
                    "Unhandled error while serving /%s/api",
                    admin_secret,
                )
        finally:
            handler.stream = orig_stream

        logged = buf.getvalue()
        assert admin_secret not in logged
        assert addon_secret not in logged
        assert qobuz_token not in logged
        assert "Traceback" in logged
        assert "ValueError:" in logged
        assert logged.count("***") >= 4
        assert captured_exc_info is not None

        record = logging.LogRecord(
            name="addon",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Failure on /%s/api",
            args=(admin_secret,),
            exc_info=captured_exc_info,
        )
        assert handler.formatter is not None
        formatted = handler.formatter.format(record)
        assert record.exc_info is captured_exc_info
        assert admin_secret not in formatted
        assert addon_secret not in formatted
        assert qobuz_token not in formatted
        assert admin_secret not in (record.exc_text or "")
        assert addon_secret not in (record.exc_text or "")


def test_render_app_shell_single_pass_substitution_prevents_token_leak() -> None:
    from app.dashboard import render_app_shell

    rendered = render_app_shell(addon_name="Confih {{boot_json}} Test")
    assert "Confih {{boot_json}} Test" in rendered
    assert rendered.count('{"initial_view":') == 1


@pytest.mark.asyncio
async def test_admin_and_vault_post_routes_reject_malformed_or_invalid_json_with_400(
    tmp_path: Path,
) -> None:
    token = "admin_post_val_0123456789abcdef"
    app = create_app(
        make_settings(tmp_path, stats_token=token),
        transport=httpx.MockTransport(FakeQobuz()),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8899",
        ) as client:
            for base, hdrs in (
                ("/internal/admin", {"X-Admin-Token": token}),
                ("/admin/api", {"X-Vault-Key": token}),
            ):
                for suffix in ("/qobuz/add", "/tidal/add", "/routing"):
                    url = f"{base}{suffix}"
                    for raw_body in (b"{bad_json", b"[]", b"null", b'"string"'):
                        resp = await client.post(
                            url,
                            headers={**hdrs, "Content-Type": "application/json"},
                            content=raw_body,
                        )
                        assert resp.status_code == 400, (url, raw_body, resp.text)
                        assert resp.json()["error"] == "invalid_request"

                # Invalid primary_provider type or value
                bad_type = await client.post(
                    f"{base}/routing",
                    headers=hdrs,
                    json={"primary_provider": 123},
                )
                assert bad_type.status_code == 400
                bad_val = await client.post(
                    f"{base}/routing",
                    headers=hdrs,
                    json={"primary_provider": "spotify"},
                )
                assert bad_val.status_code == 400

                # Empty Qobuz add payload must not create an unconfigured entry
                empty_qobuz = await client.post(
                    f"{base}/qobuz/add",
                    headers=hdrs,
                    json={},
                )
                assert empty_qobuz.status_code == 400

