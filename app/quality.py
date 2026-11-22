"""Quality negotiation between BitChord and Qobuz.

BitChord sends one of its own tiers (LOSSLESS/HIGH/LOW) or, because this addon
declares a quality picker, one of the values below. Qobuz then has four format
ids: 5 (MP3 320), 6 (FLAC 16/44.1), 7 (FLAC up to 24/96) and 27 (FLAC up to
24/192). We always ask for the requested rung and accept whatever Qobuz
actually returns, labelling the response from the returned format id rather
than the request. That is what keeps a CD-only track from being advertised as
hi-res.
"""

from __future__ import annotations

from dataclasses import dataclass

# Values are deliberately lower-case and match what the manifest sends.
# This is the production list: one "best" rung that covers hi-res and Atmos.
# Do not add a separate hi-res entry — BitChord stores the listener's pick per
# addon, and a list that changes under them silently moves what they get.
QUALITY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("max", "Hi-Res & Dolby Atmos (24-bit / 192 kHz FLAC)"),
    ("lossless", "CD Lossless (16-bit / 44.1 kHz FLAC)"),
    ("high", "High Fidelity (320 kbps AAC / MP3)"),
    ("low", "Data Saver (96–128 kbps)"),
)

# Qobuz format ids, best first.
FORMAT_PLANS: dict[str, tuple[int, ...]] = {
    "max": (27, 7, 6, 5),
    "hires": (27, 7, 6, 5),
    "atmos": (27, 7, 6, 5),
    "lossless": (6, 5),
    "high": (5,),
    "low": (5,),
}

FORMAT_IDS = {5: "MP3 320", 6: "FLAC CD", 7: "FLAC Hi-Res 96", 27: "FLAC Hi-Res 192"}


def normalize_quality(value: str | None) -> str:
    """Map a BitChord tier or picker value onto one of our plans.

    `hires` is still accepted as a request value for older clients and direct
    API calls, but it is not a manifest rung: it means the same thing as
    `max`, which is what the picker offers.
    """
    text = (value or "").strip().lower()
    if not text:
        return "max"
    if text in {
        "max", "best", "auto", "maximum",
        "hires", "hi-res", "hi_res", "high-res", "highres", "hi res",
        "atmos", "spatial", "dolby", "dolby_atmos", "dolby-atmos",
    }:
        return "max"
    if text in {"lossless", "flac", "cd", "cd-quality", "quality"}:
        return "lossless"
    if text in {"high", "320", "mp3", "mp3-320", "mp3_320", "aac"}:
        return "high"
    if text in {"low", "128", "96", "min", "minimum", "saver", "data-saver"}:
        return "low"
    return "max"


def format_plan(quality: str) -> tuple[int, ...]:
    return FORMAT_PLANS.get(normalize_quality(quality), FORMAT_PLANS["max"])


@dataclass(frozen=True)
class StreamMeta:
    fmt: str
    codec: str
    container: str
    quality: str
    sample_rate: int | None
    bit_depth: int | None
    bitrate: int | None
    is_atmos: bool = False

    def as_addon_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "format": self.fmt,
            "quality": self.quality,
            "codec": self.codec,
            "container": self.container,
            "manifest": "none",
            "encrypted": False,
            "isAtmos": self.is_atmos,
        }
        if self.is_atmos:
            fields["audioMode"] = "DOLBY_ATMOS"
        if self.sample_rate:
            fields["sampleRate"] = self.sample_rate
        if self.bit_depth:
            fields["bitDepth"] = self.bit_depth
        if self.bitrate:
            fields["bitrate"] = self.bitrate
        return fields


def stream_meta(
    format_id: int,
    *,
    bit_depth: int | None = None,
    sampling_rate_khz: float | None = None,
    mime_type: str | None = None,
    is_atmos: bool = False,
) -> StreamMeta:
    """Describe the rendition Qobuz actually returned."""
    mime = (mime_type or "").lower()
    is_mp3 = format_id == 5 or "mpeg" in mime or "mp3" in mime
    sample_rate = _rate_hz(sampling_rate_khz)
    if bit_depth is None:
        bit_depth = 16 if format_id in (5, 6) else 24 if format_id in (7, 27) else None

    if is_atmos or any(x in mime for x in ("atmos", "eac3", "ec-3", "ac-4", "ac4")):
        codec = "ac4" if any(x in mime for x in ("ac-4", "ac4")) else "eac3"
        return StreamMeta(
            fmt=codec,
            codec=codec,
            container="m4a",
            quality="Dolby Atmos",
            sample_rate=sample_rate or 48_000,
            bit_depth=bit_depth or 16,
            bitrate=768_000 if codec == "eac3" else 448_000,
            is_atmos=True,
        )

    if is_mp3:
        return StreamMeta(
            fmt="mp3",
            codec="mp3",
            container="mp3",
            quality=_quality_label("MP3", 16, sample_rate, fallback="MP3 320 kbps"),
            sample_rate=sample_rate or 44_100,
            bit_depth=16,
            bitrate=320_000,
            is_atmos=False,
        )

    # A format id is a plan ("up to 24/192"), not proof of what arrived. If
    # Qobuz explicitly reports 16-bit / 44.1 kHz under a hi-res plan, call it
    # lossless. When the fields are absent, the inferred 24-bit above makes
    # format 7/27 hi-res, which is the usual case.
    is_hires = (bit_depth or 0) >= 24 or (sample_rate or 0) > 48_000
    label = "Hi-Res" if is_hires else "Lossless"
    return StreamMeta(
        fmt="flac",
        codec="flac",
        container="flac",
        quality=_quality_label(label, bit_depth, sample_rate),
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        bitrate=None,
        is_atmos=False,
    )


def _rate_hz(khz: float | None) -> int | None:
    if not khz or khz <= 0:
        return None
    # Qobuz states kHz (44.1, 96, 192). A client sending Hz is tolerated.
    return int(round(khz * 1000)) if khz < 1000 else int(round(khz))


def _quality_label(
    prefix: str,
    bit_depth: int | None,
    sample_rate: int | None,
    fallback: str | None = None,
) -> str:
    if bit_depth and sample_rate:
        khz = sample_rate / 1000
        khz_text = f"{khz:g}"
        return f"{prefix} · {bit_depth}-bit / {khz_text} kHz"
    return fallback or prefix


@dataclass(frozen=True)
class SearchLabels:
    fmt: str
    audio_quality: str


def search_labels(
    *,
    hires_streamable: bool,
    requested_quality: str,
    is_atmos: bool = False,
) -> SearchLabels:
    """What a search row may honestly claim at the tier being asked for.

    A catalog row says the *recording* is available in hi-res, not that this
    request will receive it. When BitChord is asking on behalf of a metered
    download, show it the MP3 it will actually get so ranking is honest.
    """
    quality = normalize_quality(requested_quality)
    if quality in {"high", "low"}:
        return SearchLabels(fmt="mp3", audio_quality="MP3_320")
    raw_req = (requested_quality or "").strip().lower()
    if is_atmos or raw_req in ("atmos", "spatial", "dolby", "dolby_atmos", "dolby-atmos"):
        return SearchLabels(fmt="eac3", audio_quality="DOLBY_ATMOS")
    if quality == "lossless":
        return SearchLabels(fmt="flac", audio_quality="LOSSLESS")
    if hires_streamable:
        return SearchLabels(fmt="flac", audio_quality="HI_RES_LOSSLESS")
    return SearchLabels(fmt="flac", audio_quality="LOSSLESS")
