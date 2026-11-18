#!/usr/bin/env python3
"""Write a synthetic ``plays.json`` for a *test* instance.

The public /plays page renders whatever ``PlayLedger`` persisted, so reviewing it
against real traffic means either waiting for someone to stream something or
polluting the production ledger. This seeds a self-contained ledger instead.

    python3 scripts/seed_demo_plays.py --out /data/plays.json

It writes the same schema ``PlayLedger.save()`` produces (see app/plays.py), so
the server loads it normally on startup and treats it as its own history.

The newest play is deliberately a few seconds old so the "Latest stream" plate
shows its live state (green dot, faster platter spin) right after boot.

Nothing here touches a provider: no credentials, no network, no artwork fetches —
covers render as letter tiles.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

# Mirrors plays.TIER_ORDER. Kept local so this runs without the app installed.
TIERS = ("dolby_atmos", "hires_24bit", "lossless_16bit", "high_320")

# Demo cover art, resolved once from the live providers as `/art/{key}` sources.
# Baking them in keeps the seeder offline and deterministic; the artwork cache
# downloads each one server-side on first use, so nothing hotlinks at page load.
ARTWORK: dict[str, str] = {
    "childishgambinoredbone": "https://static.qobuz.com/images/covers/ka/4k/yprhjr5as4kka_600.jpg",
    "frankoceannights": "https://static.qobuz.com/images/covers/la/bx/it2hq7tg2bxla_600.jpg",
    "kendricklamaralright": "https://static.qobuz.com/images/covers/92/89/0060254728992_600.jpg",
    "massiveattackteardrop": "https://static.qobuz.com/images/covers/53/99/0724384559953_600.jpg",
    "aphextwinwindowlicker": "https://static.qobuz.com/images/covers/32/05/0801061910532_600.jpg",
    "radioheadmotionpicturesoundtrack": "https://static.qobuz.com/images/covers/63/82/0634904078263_600.jpg",
    "tychoawalk": "https://static.qobuz.com/images/covers/20/45/0804297814520_600.jpg",
    "petitbiscuitsunsetlover": "https://static.qobuz.com/images/covers/74/75/3614978217574_600.jpg",
    "caribouodessa": "https://static.qobuz.com/images/covers/aa/uk/q83z4rz6hukaa_600.jpg",
    "littledragonritualunion": "https://static.qobuz.com/images/covers/xg/el/yly9qoz7telxg_600.jpg",
    "m83midnightcity": "https://static.qobuz.com/images/covers/rb/gf/kzq0n3vmfgfrb_600.jpg",
    "odeszabloom": "https://static.qobuz.com/images/covers/91/91/5021392959191_600.jpg",
    "jamiexxloudplaces": "https://static.qobuz.com/images/covers/65/22/0889030012265_600.jpg",
    "boardsofcanadaroygbiv": "https://static.qobuz.com/images/covers/35/55/0801061005535_600.jpg",
}

# (title, artist, album) — short, recognisable, and easy to tell apart on screen.
TRACKS: list[tuple[str, str, str]] = [
    ("Redbone", "Childish Gambino", "Awaken, My Love!"),
    ("Nights", "Frank Ocean", "Blonde"),
    ("Alright", "Kendrick Lamar", "To Pimp a Butterfly"),
    ("Teardrop", "Massive Attack", "Mezzanine"),
    ("Windowlicker", "Aphex Twin", "Windowlicker"),
    ("Motion Picture Soundtrack", "Radiohead", "Kid A"),
    ("A Walk", "Tycho", "Dive"),
    ("Sunset Lover", "Petit Biscuit", "Presence"),
    ("Odessa", "Caribou", "Swim"),
    ("Ritual Union", "Little Dragon", "Ritual Union"),
    ("Midnight City", "M83", "Hurry Up, We're Dreaming"),
    ("Bloom", "ODESZA", "In Return"),
    ("Loud Places", "Jamie xx", "In Colour"),
    ("Roygbiv", "Boards of Canada", "Music Has the Right to Children"),
]


def _slug(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def build(*, plays: int, latest_age: int, seed: int) -> dict:
    rng = random.Random(seed)
    now = int(time.time())
    tracks: dict[str, dict] = {}
    hourly: dict[int, dict[str, int]] = {}
    recent: list[dict] = []

    # Spread plays over the last 24 hours; the newest one is a few seconds old.
    for index in range(plays):
        # newest play is `latest_age` seconds old; the rest spread backwards
        offset = latest_age if index == 0 else int((index / max(1, plays - 1)) * 23.5 * 3600) + latest_age
        at = now - offset
        at -= at % 60  # the ledger rounds to the minute
        title, artist, album = rng.choice(TRACKS)
        track_id = _slug(f"{artist}-{title}")
        tier = rng.choices(TIERS, weights=(10, 26, 34, 30))[0]
        listeners = rng.randint(1, 4)

        track = tracks.setdefault(
            track_id,
            {
                "id": track_id,
                "title": title,
                "artist": artist,
                "album": album,
                "artwork": ARTWORK.get(track_id),
                "plays": 0,
                "quality": {key: 0 for key in TIERS},
                "listeners": 0,
                "first_seen": at,
                "last_seen": at,
            },
        )
        track["plays"] += 1
        track["quality"][tier] += 1
        track["listeners"] += listeners
        track["first_seen"] = min(track["first_seen"], at)
        track["last_seen"] = max(track["last_seen"], at)

        hour = at - (at % 3600)
        bucket = hourly.setdefault(hour, {})
        bucket[tier] = bucket.get(tier, 0) + 1

        if len(recent) < 40:
            recent.append(
                {
                    "id": track_id,
                    "title": title,
                    "artist": artist,
                    "artwork": track["artwork"],
                    "tier": tier,
                    "at": at,
                }
            )

    recent.sort(key=lambda row: row["at"], reverse=True)
    ordered_hours = [
        {"hour": hour, "tiers": {k: v for k, v in tiers.items() if v}}
        for hour, tiers in sorted(hourly.items())
    ]
    total = sum(track["plays"] for track in tracks.values())
    return {
        "total": total,
        "tracks": list(tracks.values()),
        "recent": recent,
        "hourly": ordered_hours,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="/data/plays.json", help="target path (default: /data/plays.json)")
    parser.add_argument("--plays", type=int, default=180, help="how many plays to record (default: 180)")
    parser.add_argument(
        "--latest-age",
        type=int,
        default=20,
        help="seconds since the newest play, so the plate shows its live state (default: 20)",
    )
    parser.add_argument("--seed", type=int, default=7, help="random seed for repeatable output")
    args = parser.parse_args()

    payload = build(plays=max(1, args.plays), latest_age=max(0, args.latest_age), seed=args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), "utf-8")
    tmp.replace(out)
    print(
        f"seeded {payload['total']} plays, {len(payload['tracks'])} tracks, "
        f"{len(payload['hourly'])} hourly buckets -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
