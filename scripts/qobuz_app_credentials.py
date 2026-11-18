#!/usr/bin/env python3
"""Find the current Qobuz web app id/secret pair.

Qobuz's web player ships an app id and a per-timezone obfuscated secret in its
bundle. When track/getFileUrl starts answering "Invalid Request Signature", the
pair rotated. This script downloads the bundle, decodes the secrets, and tests
each one by signing a harmless getFileUrl request (no account token involved).

    python3 scripts/qobuz_app_credentials.py
    python3 scripts/qobuz_app_credentials.py --proxy http://127.0.0.1:8080

The addon only needs QOBUZ_APP_ID and QOBUZ_APP_SECRET; the pair supplied for
this deployment normally keeps working for a long time.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys
import time
from dataclasses import dataclass

import httpx

API = "https://www.qobuz.com/api.json/0.2"
PLAY = "https://play.qobuz.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

_BUNDLE_RE = re.compile(r'<script src="(/resources/[^"]*bundle\.js)"></script>')
_PAIR_RE = re.compile(
    r'production:\{api:\{appId:"(?P<app_id>\d+)",appSecret:"(?P<secret>[^"]+)"'
)
_SEED_RE = re.compile(
    r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.utimezone\.(?P<timezone>[a-z]+)\)'
)
_INFO_RE = re.compile(
    r'name:"\w+/(?P<timezone>[A-Za-z]+)",info:"(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"'
)


@dataclass(frozen=True)
class Candidate:
    app_id: str
    secret: str
    source: str


def _get(client: httpx.Client, url: str) -> str:
    response = client.get(url, headers={"User-Agent": UA})
    response.raise_for_status()
    return response.text


def _scrape(client: httpx.Client) -> list[Candidate]:
    page = _get(client, f"{PLAY}/login")
    match = _BUNDLE_RE.search(page)
    if not match:
        page = _get(client, f"{PLAY}/")
        match = _BUNDLE_RE.search(page)
    if not match:
        raise RuntimeError("could not find the Qobuz web bundle on play.qobuz.com")

    bundle_url = PLAY + match.group(1)
    bundle = _get(client, bundle_url)
    print(f"bundle: {bundle_url} ({len(bundle):,} bytes)")

    candidates: list[Candidate] = []
    pair = _PAIR_RE.search(bundle)
    if pair:
        candidates.append(Candidate(pair.group("app_id"), pair.group("secret"), "literal"))

    seeds: dict[str, list[str]] = {}
    for seed_match in _SEED_RE.finditer(bundle):
        seeds.setdefault(seed_match.group("timezone"), []).append(seed_match.group("seed"))
    infos: dict[str, tuple[str, str]] = {}
    for info_match in _INFO_RE.finditer(bundle):
        infos[info_match.group("timezone").lower()] = (
            info_match.group("info"),
            info_match.group("extras"),
        )

    # The original obfuscation rotates the seed list by moving the second
    # timezone to the front before concatenating seed + info + extras.
    pairs = list(seeds.items())
    if len(pairs) >= 2:
        reordered = [pairs[1], *pairs[:1], *pairs[2:]]
    else:
        reordered = pairs
    for timezone, parts in reordered:
        extra = infos.get(timezone)
        if not extra:
            continue
        combined = "".join(parts + list(extra))[:-44]
        try:
            secret = base64.b64decode(combined).decode("ascii")
        except Exception:
            continue
        if re.fullmatch(r"[0-9a-zA-Z]{32}", secret):
            candidates.append(Candidate(pair.group("app_id") if pair else "", secret, timezone))

    deduped: dict[tuple[str, str], Candidate] = {}
    for candidate in candidates:
        if candidate.app_id and candidate.secret:
            deduped[(candidate.app_id, candidate.secret)] = candidate
    return list(deduped.values())


def _signature(secret: str) -> tuple[str, str]:
    timestamp = str(int(time.time()))
    signature = hashlib.md5(
        f"trackgetFileUrlformat_id27intentstreamtrack_id1{timestamp}{secret}".encode()
    ).hexdigest()
    return timestamp, signature


def _works(client: httpx.Client, candidate: Candidate) -> tuple[bool, str]:
    timestamp, signature = _signature(candidate.secret)
    response = client.get(
        f"{API}/track/getFileUrl",
        params={
            "format_id": 27,
            "intent": "stream",
            "track_id": 1,
            "request_ts": timestamp,
            "request_sig": signature,
            "app_id": candidate.app_id,
        },
        headers={"User-Agent": UA, "X-App-Id": candidate.app_id},
    )
    text = response.text[:300].replace("\n", " ")
    if response.status_code == 200:
        return True, f"HTTP 200 {text[:120]}"
    if "signature" in text.lower():
        return False, f"HTTP {response.status_code} {text}"
    if "app_id" in text.lower() or "app id" in text.lower():
        return False, f"HTTP {response.status_code} {text}"
    # Some other error (unknown track, geo, auth) still means the signature
    # was accepted; the API rejects a bad one with a signature message.
    if response.status_code in (401, 403, 404):
        return True, f"HTTP {response.status_code} {text[:120]}"
    return False, f"HTTP {response.status_code} {text}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", help="HTTP/SOCKS proxy for the Qobuz website")
    args = parser.parse_args()

    kwargs: dict[str, object] = {
        "headers": {"User-Agent": UA},
        "timeout": 30,
        "follow_redirects": True,
    }
    if args.proxy:
        kwargs["proxy"] = args.proxy

    with httpx.Client(**kwargs) as client:
        try:
            candidates = _scrape(client)
        except Exception as exc:
            print(f"scrape failed: {exc}", file=sys.stderr)
            return 1

        if not candidates:
            print("no app id/secret candidates decoded", file=sys.stderr)
            return 1

        print(f"testing {len(candidates)} candidate(s)...")
        working: list[Candidate] = []
        for candidate in candidates:
            ok, detail = _works(client, candidate)
            print(
                f"  {'OK  ' if ok else 'FAIL'} {candidate.app_id}/{candidate.secret[:6]}…"
                f" ({candidate.source}) {detail}"
            )
            if ok:
                working.append(candidate)

        if not working:
            print("none of the candidates produced a valid signature", file=sys.stderr)
            return 1

        chosen = working[0]
        print()
        print("Working credentials:")
        print(f"QOBUZ_APP_ID={chosen.app_id}")
        print(f"QOBUZ_APP_SECRET={chosen.secret}")
        if len(working) > 1:
            print(f"(other working pairs: {len(working) - 1})")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
