from __future__ import annotations

import hashlib
from unittest.mock import patch

import httpx
import pytest

from app.qobuz import QobuzClient
from app.quality import normalize_quality, search_labels, stream_meta

from .conftest import FakeQobuz, make_settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "max"),
        ("", "max"),
        ("max", "max"),
        ("best", "max"),
        ("LOSSLESS", "lossless"),
        ("FLAC", "lossless"),
        ("Hi-Res", "hires"),
        ("hi_res", "hires"),
        ("HIGH", "high"),
        ("mp3-320", "high"),
        ("LOW", "low"),
        ("something else", "max"),
    ],
)
def test_normalize_quality(value: str | None, expected: str) -> None:
    assert normalize_quality(value) == expected


def test_stream_meta_hires() -> None:
    meta = stream_meta(27, bit_depth=24, sampling_rate_khz=192.0)
    assert meta.fmt == "flac"
    assert meta.codec == "flac"
    assert meta.container == "flac"
    assert meta.sample_rate == 192_000
    assert meta.bit_depth == 24
    assert meta.quality == "Hi-Res · 24-bit / 192 kHz"


def test_stream_meta_cd() -> None:
    meta = stream_meta(6, bit_depth=16, sampling_rate_khz=44.1)
    assert meta.quality == "Lossless · 16-bit / 44.1 kHz"
    assert meta.sample_rate == 44_100


def test_stream_meta_explicit_cd_under_hires_format_is_lossless() -> None:
    # The plan was 24/192; Qobuz explicitly answered with CD. Do not repeat
    # the plan's label over the actual fields.
    meta = stream_meta(27, bit_depth=16, sampling_rate_khz=44.1)
    assert meta.quality == "Lossless · 16-bit / 44.1 kHz"
    assert "Hi-Res" not in meta.quality


def test_stream_meta_mp3() -> None:
    meta = stream_meta(5, mime_type="audio/mpeg")
    assert meta.fmt == "mp3"
    assert meta.bitrate == 320_000
    assert meta.sample_rate == 44_100


def test_search_labels_never_overpromise() -> None:
    assert search_labels(hires_streamable=True, requested_quality="max").audio_quality == (
        "HI_RES_LOSSLESS"
    )
    assert search_labels(hires_streamable=False, requested_quality="max").audio_quality == (
        "LOSSLESS"
    )
    assert search_labels(hires_streamable=True, requested_quality="lossless").audio_quality == (
        "LOSSLESS"
    )
    assert search_labels(hires_streamable=True, requested_quality="high").audio_quality == (
        "MP3_320"
    )


@pytest.mark.asyncio
async def test_token_file_recovers_a_stale_environment_token(tmp_path) -> None:
    settings = make_settings(tmp_path, qobuz_user_auth_token="stale-token")
    (tmp_path / "qobuz-token.json").write_text(
        '{"user_auth_token": "fresh-file-token", "user_id": "14889875"}',
        encoding="utf-8",
    )
    fake = FakeQobuz()
    fake.rejected_tokens.add("stale-token")
    client = QobuzClient(settings, transport=httpx.MockTransport(fake))
    await client.start()
    try:
        await client.ensure_token()
        assert client.health_snapshot()["token_ok"] is True
    finally:
        await client.stop()

    token_logins = [
        call
        for call in fake.calls
        if call.url.path.endswith("/user/login")
        and call.url.params.get("user_auth_token") == "fresh-file-token"
    ]
    assert token_logins, "the token file should have been tried after the env token"


@pytest.mark.asyncio
async def test_get_file_url_signature_uses_integer_seconds(tmp_path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeQobuz()
    client = QobuzClient(settings, transport=httpx.MockTransport(fake))
    await client.start()
    try:
        with patch("app.qobuz.time.time", return_value=1_700_000_000.987):
            await client.get_file_url("123", 27)
    finally:
        await client.stop()

    request = next(call for call in fake.calls if call.url.path.endswith("/track/getFileUrl"))
    assert request.url.params["request_ts"] == "1700000000"
    expected = hashlib.md5(
        (
            "trackgetFileUrlformat_id27intentstreamtrack_id123"
            f"1700000000{settings.qobuz_app_secret}"
        ).encode()
    ).hexdigest()
    assert request.url.params["request_sig"] == expected
