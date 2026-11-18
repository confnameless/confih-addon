#!/usr/bin/env python3
"""End-to-end check of the Qobuz account, geo access and stream signing.

    python3 scripts/qobuz_check.py
    python3 scripts/qobuz_check.py --query "daft punk" --quality max
    python3 scripts/qobuz_check.py --track-id 5966783 --show-url

It performs exactly the calls the addon makes: login/refresh, catalogue search,
signed track/getFileUrl, then a 64-byte ranged request to the Qobuz CDN to
confirm the URL really is FLAC/MP3 and not a 30-second sample. The signed URL
is redacted unless --show-url is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.qobuz import QobuzClient, QobuzError  # noqa: E402
from app.quality import stream_meta  # noqa: E402


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="daft punk", help="search query")
    parser.add_argument(
        "--quality", default="max", help="max, hires, lossless, high or low"
    )
    parser.add_argument("--track-id", help="skip search and use this Qobuz track id")
    parser.add_argument("--limit", type=int, default=5, help="search rows to inspect")
    parser.add_argument("--show-url", action="store_true", help="print the signed URL")
    return parser.parse_args()


async def _first_bytes(url: str, proxy: str, timeout: float) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "follow_redirects": True,
        "timeout": timeout,
    }
    if proxy:
        kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**kwargs) as client:
        async with client.stream(
            "GET", url, headers={"Range": "bytes=0-63", "Accept": "*/*"}
        ) as response:
            data = b""
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) >= 64:
                    break
            magic = "flac" if data.startswith(b"fLaC") else "mp3" if data[:3] == b"ID3" or data[:1] == b"\xff" else "unknown"
            return {
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "content_range": response.headers.get("content-range", ""),
                "magic": magic,
                "first_bytes_hex": data[:16].hex(),
            }


async def _run(args: argparse.Namespace) -> int:
    settings = Settings(
        _env_file=ROOT / ".env",
        qobuz_self_check=False,
        qobuz_token_check_interval_seconds=0,
    )
    if not settings.qobuz_configured:
        print(
            "error: Qobuz is not configured. Set QOBUZ_APP_SECRET and "
            "QOBUZ_USER_AUTH_TOKEN/QOBUZ_USER_ID (or e-mail/password) in .env.",
            file=sys.stderr,
        )
        return 2

    client = QobuzClient(settings)
    await client.start()
    try:
        geo = await client.check_geo(force=True)
        print(f"geo: {'ok' if geo else 'blocked or unknown'} ({geo})")
        if geo is False:
            print(
                "This IP gets an empty Qobuz catalogue. Use a VPS in FR/DE/NL/UK/US "
                "or set QOBUZ_PROXY_URL. Audio itself comes from the CDN and does "
                "not need the proxy.",
                file=sys.stderr,
            )

        if args.track_id:
            track_id = args.track_id
            print(f"track: {track_id} (given)")
        else:
            tracks = await client.search_tracks(args.query, max(1, min(args.limit, 50)))
            print(f"search: {len(tracks)} row(s) for {args.query!r}")
            if not tracks:
                print("no tracks found; check the query, token and geo access.", file=sys.stderr)
                return 1
            for track in tracks[: args.limit]:
                print(
                    f"  {track.id}  {track.artist} — {track.title}"
                    f"  (hires={track.hires_streamable}, max={track.max_bit_depth}/"
                    f"{track.max_sampling_rate_khz} kHz)"
                )
            track_id = tracks[0].id

        resolved = await client.resolve(track_id, args.quality)
        meta = stream_meta(
            resolved.format_id,
            bit_depth=resolved.bit_depth,
            sampling_rate_khz=resolved.sampling_rate_khz,
            mime_type=resolved.mime_type,
        )
        host = urlparse(resolved.url).hostname
        print()
        print(f"format_id: {resolved.format_id} (requested {resolved.requested_format_id})")
        print(f"mime:      {resolved.mime_type or '?'}")
        print(f"quality:   {meta.quality}")
        print(f"codec:     {meta.codec} / {meta.container}")
        print(f"url host:  {host}")
        if args.show_url:
            print(f"url:       {resolved.url}")

        probe = await _first_bytes(
            resolved.url, settings.qobuz_proxy_url, settings.qobuz_timeout_seconds
        )
        print()
        print("CDN probe:")
        print(json.dumps(probe, indent=2))
        if probe["status"] not in (200, 206):
            print("warning: CDN did not return 200/206", file=sys.stderr)
            return 1
        if probe["magic"] == "unknown":
            print("warning: first bytes are neither FLAC nor MP3", file=sys.stderr)
            return 1
        if meta.codec == "flac" and probe["magic"] != "flac":
            print("warning: addon calls it FLAC but the CDN sent something else", file=sys.stderr)
            return 1
        if meta.codec == "mp3" and probe["magic"] != "mp3":
            print("warning: addon calls it MP3 but the CDN sent something else", file=sys.stderr)
            return 1
        print()
        print("OK: account, catalogue, signature and CDN all work.")
        return 0
    except QobuzError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await client.stop()
        print()
        print("health:", json.dumps(client.health_snapshot(), indent=2))


def main() -> int:
    return asyncio.run(_run(_args()))


if __name__ == "__main__":
    raise SystemExit(main())
