"""Security and delivery headers, and the public-JSON rate limit.

These are the properties a reader of the source should be able to trust without
re-deriving them: a nonce-backed script policy, the usual hardening headers,
compression, cacheability, and a ceiling on anonymous JSON.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import create_app

from .conftest import FakeQobuz, make_settings

HTML_ROUTES = ("/", "/status", "/plays", "/about", "/support")


def _app(tmp_dir: Path):
    return create_app(
        make_settings(tmp_dir), transport=httpx.MockTransport(FakeQobuz())
    )


def _nonces(html: str) -> set[str]:
    return set(re.findall(r'<script[^>]*\bnonce="([^"]+)"', html))


def _script_src(csp: str) -> str:
    return csp.split("script-src", 1)[1].split(";", 1)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", HTML_ROUTES)
async def test_html_pages_use_a_nonce_and_hardening_headers(
    path: str, tmp_path: Path
) -> None:
    app = _app(tmp_path)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await client.get(path, headers={"Accept": "text/html"})
    assert resp.status_code == 200, path

    csp = resp.headers["content-security-policy"]
    script_src = _script_src(csp)
    nonce = _nonces(resp.text)
    assert len(nonce) == 1, (path, nonce)
    assert f"'nonce-{nonce.pop()}'" in script_src, (path, script_src)
    # A nonce only helps if the escape hatch is gone.
    assert "'unsafe-inline'" not in script_src, path
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp

    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cross-origin-opener-policy"] == "same-origin"
    assert resp.headers["cross-origin-resource-policy"] == "same-origin"
    assert resp.headers["x-permitted-cross-domain-policies"] == "none"
    assert "camera=()" in resp.headers["permissions-policy"]


@pytest.mark.asyncio
async def test_two_pages_get_different_nonces(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first = await client.get("/about")
            second = await client.get("/about")
    assert _nonces(first.text) != _nonces(second.text)


@pytest.mark.asyncio
async def test_html_is_revalidatable_and_assets_are_immutable(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            page = await client.get("/about")
            # no-cache (not no-store): the back/forward cache stays usable.
            assert page.headers["cache-control"] == "no-cache"
            assert page.headers["etag"].startswith('W/"')

            css = await client.get("/assets/monitor.css")
            assert css.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_page_response_answers_a_matching_etag_with_304() -> None:
    """The revalidation path itself: identical body, no second download."""
    from starlette.requests import Request

    from app.main import _page_response

    scope = {"type": "http", "method": "GET", "path": "/about", "headers": []}
    body = "<html>same</html>"
    first = _page_response(Request(scope), body)
    etag = first.headers["etag"]

    scope_with_inm = {
        **scope,
        "headers": [(b"if-none-match", etag.encode())],
    }
    second = _page_response(Request(scope_with_inm), body)
    assert second.status_code == 304
    assert second.body == b""
    assert second.headers["etag"] == etag

    changed = _page_response(Request(scope), body + "<!-- moved on -->")
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag


@pytest.mark.asyncio
async def test_text_assets_are_compressed_when_the_client_asks(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            plain = await client.get(
                "/assets/monitor.css", headers={"Accept-Encoding": "identity"}
            )
            zipped = await client.get(
                "/assets/monitor.css", headers={"Accept-Encoding": "gzip"}
            )
    assert zipped.headers.get("content-encoding") == "gzip"
    # httpx decodes the body, so compare the wire sizes the header reports.
    assert int(zipped.headers["content-length"]) < int(plain.headers["content-length"])
    assert zipped.content == plain.content


@pytest.mark.asyncio
async def test_public_json_has_a_ceiling_but_allows_the_pages_own_polling(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            # The page polls a few times a minute; that must never be blocked.
            for _ in range(10):
                assert (await client.get("/api/support")).status_code == 200

            statuses: list[int] = []
            for _ in range(260):
                statuses.append((await client.get("/api/support")).status_code)
    assert 429 in statuses, "anonymous JSON is unbounded"
    assert statuses.count(200) <= 240


@pytest.mark.asyncio
async def test_error_bodies_never_echo_internals(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            probes: list[Any] = [
                await client.get("/art/not-a-key"),
                await client.get("/art/" + "a" * 200),
                await client.get("/stream/../../etc/passwd"),
                await client.get("/api/plays?limit=999999"),
                await client.get("/nonexistent"),
            ]
    for resp in probes:
        assert resp.status_code < 500, resp.text
        body = resp.text.lower()
        for leak in ("traceback", "site-packages", "app/main.py", "sqlite", "token"):
            assert leak not in body, (resp.request.url, leak)
