"""Shared fixtures: a fake Qobuz API and the addon app on httpx ASGI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.config import Settings
from app.main import create_app

SECRET = "0123456789abcdef0123456789abcdef"

TRACK: dict[str, Any] = {
    "id": 5966783,
    "title": "One More Time",
    "version": None,
    "performer": {"name": "Daft Punk"},
    "album": {
        "title": "Discovery",
        "artist": {"name": "Daft Punk"},
        "image": {"large": "https://static.qobuz.com/images/covers/cover_600.jpg"},
    },
    "duration": 320,
    "streamable": True,
    "hires_streamable": True,
    "maximum_bit_depth": 24,
    "maximum_sampling_rate": 96.0,
    "isrc": "GBDUW0000059",
}

CD_TRACK: dict[str, Any] = {
    **TRACK,
    "id": 42,
    "title": "A CD-only track",
    "hires_streamable": False,
    "maximum_bit_depth": 16,
    "maximum_sampling_rate": 44.1,
}

STREAM_192: dict[str, Any] = {
    "url": "https://cdn.example/audio/one-more-time.flac?range=0-100",
    "format_id": 27,
    "mime_type": "audio/flac",
    "bit_depth": 24,
    "sampling_rate": 192.0,
}

STREAM_96: dict[str, Any] = {
    "url": "https://cdn.example/audio/one-more-time-96.flac?range=0-100",
    "format_id": 7,
    "mime_type": "audio/flac",
    "bit_depth": 24,
    "sampling_rate": 96.0,
}

STREAM_CD: dict[str, Any] = {
    "url": "https://cdn.example/audio/one-more-time.flac?range=0-100",
    "format_id": 6,
    "mime_type": "audio/flac",
    "bit_depth": 16,
    "sampling_rate": 44.1,
}

STREAM_MP3: dict[str, Any] = {
    "url": "https://cdn.example/audio/one-more-time.mp3?range=0-100",
    "format_id": 5,
    "mime_type": "audio/mpeg",
}


class FakeQobuz:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.search_items: list[dict[str, Any]] = [TRACK]
        self.stream_data: dict[int, dict[str, Any]] = {
            27: STREAM_192,
            7: STREAM_96,
            6: STREAM_CD,
            5: STREAM_MP3,
        }
        self.stream_errors: dict[int, tuple[int, str]] = {}
        self.geo_blocked = False
        self.sample = False
        self.auth_error = False
        self.login_fails = False
        self.rejected_tokens: set[str] = set()

    def count(self, suffix: str) -> int:
        return sum(1 for call in self.calls if call.url.path.endswith(suffix))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path

        if path.endswith("/user/login"):
            if self.login_fails:
                return httpx.Response(
                    401,
                    json={
                        "status": "error",
                        "code": 401,
                        "message": "Invalid username/email and password combination",
                    },
                )
            token = request.url.params.get("user_auth_token")
            if token and token in self.rejected_tokens:
                return httpx.Response(
                    401,
                    json={
                        "status": "error",
                        "code": 401,
                        "message": "Invalid username/email and password combination",
                    },
                )
            return httpx.Response(
                200,
                json={"user_auth_token": "fresh-token", "user": {"id": 14889875}},
            )

        if path.endswith("/favorite/getUserFavorites"):
            return httpx.Response(200, json={"tracks": {"items": [], "total": 0}})

        if path.endswith("/track/search"):
            query = request.url.params.get("query", "")
            limit = int(request.url.params.get("limit", "25"))
            if query == "daft punk" and limit == 5:
                items = [] if self.geo_blocked else [TRACK]
                return httpx.Response(
                    200, json={"tracks": {"items": items, "total": len(items)}}
                )
            if self.geo_blocked:
                return httpx.Response(
                    200, json={"tracks": {"items": [], "total": 0}}
                )
            if self.search_items:
                return httpx.Response(
                    200,
                    json={
                        "tracks": {
                            "items": self.search_items,
                            "total": len(self.search_items),
                        }
                    },
                )
            return httpx.Response(200, json={"tracks": {"items": [], "total": 0}})

        if path.endswith("/track/getFileUrl"):
            if self.auth_error:
                return httpx.Response(
                    200,
                    json={
                        "url": "https://cdn.example/sample.flac?range=20-30",
                        "format_id": 27,
                        "restrictions": [{"code": "UserUnauthenticated"}],
                    },
                )
            if self.sample:
                return httpx.Response(
                    200,
                    json={
                        "url": "https://cdn.example/sample.flac?range=20-30",
                        "format_id": 27,
                        "sample": True,
                        "mime_type": "audio/flac",
                    },
                )
            format_id = int(request.url.params.get("format_id", "27"))
            if format_id in self.stream_errors:
                status, message = self.stream_errors[format_id]
                return httpx.Response(
                    status,
                    json={"status": "error", "code": status, "message": message},
                )
            data = self.stream_data.get(format_id)
            if data is None:
                return httpx.Response(
                    404,
                    json={"status": "error", "code": 404, "message": "No such format"},
                )
            return httpx.Response(200, json=data)

        return httpx.Response(
            404, json={"status": "error", "code": 404, "message": "Unknown endpoint"}
        )


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "addon_id": "test.qobuz",
        "addon_name": "Qobuz Test",
        "addon_version": "1.0.0",
        "public_base_url": "",
        "addon_path_secret": "",
        "addon_auth_enabled": False,
        "addon_hosts": "",
        "qobuz_app_id": "312369995",
        "qobuz_app_secret": SECRET,
        "qobuz_user_auth_token": "token",
        "qobuz_user_id": "14889875",
        "qobuz_self_check": False,
        "qobuz_token_check_interval_seconds": 0,
        "qobuz_rate_limit_per_second": 0,
        "rate_limit_per_minute": 0,
        "data_dir": tmp_path,
        "log_level": "WARNING",
        "enable_docs": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def fake() -> FakeQobuz:
    return FakeQobuz()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def app(settings: Settings, fake: FakeQobuz):
    application = create_app(settings, transport=httpx.MockTransport(fake))
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def http(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client
