"""Wire models for the BitChord HTTP addon contract.

Only the fields BitChord reads are modelled here. Extra fields are allowed on
the Qobuz side; they never reach these models unchanged. The names are
camelCase because that is the contract, not a style choice.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AddonQualityOption(BaseModel):
    value: str
    label: str


class AddonSetting(BaseModel):
    key: str = "quality"
    type: str = "select"
    label: str = "Audio quality"
    default: str = "max"
    options: list[AddonQualityOption] = Field(default_factory=list)


class AddonManifest(BaseModel):
    id: str
    name: str
    version: str
    resources: list[str] = Field(default_factory=lambda: ["search", "stream"])
    allowDownloads: int = 1
    checkValidLossless: int = 0
    bitchordAuth: bool = False
    settings: list[AddonSetting] = Field(default_factory=list)


class AddonTrack(BaseModel):
    id: str
    title: str
    artist: str = ""
    album: str = ""
    duration: int | float | None = None
    artworkURL: str | None = None
    albumArtworkURL: str | None = None
    format: str = ""
    audioQuality: str = ""
    audioMode: str | None = None
    audioModes: list[str] = Field(default_factory=list)
    streamURL: str | None = None
    # Not read by BitChord today, but honest catalogue data and useful in
    # logs / future matching.
    isrc: str | None = None


class AddonSearchResponse(BaseModel):
    tracks: list[AddonTrack] = Field(default_factory=list)


class AddonStream(BaseModel):
    url: str
    format: str = ""
    quality: str = ""
    codec: str = ""
    container: str = ""
    manifest: str = "none"
    encrypted: bool = False
    sampleRate: int | None = None
    bitDepth: int | None = None
    bitrate: int | None = None
    isAtmos: bool = False
    audioMode: str | None = None
    error: str | None = None


class AddonError(BaseModel):
    error: str
    code: str = "error"
    message: str | None = None
