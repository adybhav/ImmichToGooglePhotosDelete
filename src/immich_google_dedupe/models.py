from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class GoogleItem:
    path: str
    relative_path: str
    filename: str
    size: int
    media_type: str
    sha1: str
    sha256: str
    taken_at: str | None = None
    google_url: str | None = None
    sidecar_path: str | None = None
    repeated_takeout_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ImmichAsset:
    id: str
    filename: str
    original_path: str
    media_type: str
    file_created_at: str | None
    width: int | None
    height: int | None
    checksum_sha1: str | None
    checksum_reliable: bool
    is_external: bool
    is_offline: bool
    local_path: str | None = None
    local_size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Match:
    google: GoogleItem
    confidence: str
    reason: str
    immich: ImmichAsset | None = None
    score: int | None = None
    deletion_eligible: bool = False
    alternatives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return value
