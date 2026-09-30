from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .models import GoogleItem
from .util import (
    MEDIA_EXTENSIONS,
    hash_file,
    isoformat,
    is_google_photo_url,
    media_kind,
    ordered_parallel_map,
    parse_timestamp,
)


def _sidecar_candidates(sidecar: Path, payload: dict[str, Any]) -> list[str]:
    name = sidecar.name
    candidates: list[str] = []
    for suffix in (".supplemental-metadata.json", ".json"):
        if name.lower().endswith(suffix):
            candidates.append(name[: -len(suffix)])
            break
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        candidates.append(Path(title).name)
    return list(dict.fromkeys(candidates))


def _extract_taken_at(payload: dict[str, Any]):
    for key in ("photoTakenTime", "creationTime", "mediaMetadata"):
        value = payload.get(key)
        if isinstance(value, dict):
            for inner in ("timestamp", "formatted", "creationTime"):
                parsed = parse_timestamp(value.get(inner))
                if parsed:
                    return parsed
    return parse_timestamp(payload.get("creationTime"))


def _load_sidecars(root: Path) -> tuple[dict[Path, dict[str, Any]], int]:
    media_by_dir_name: dict[tuple[Path, str], list[Path]] = defaultdict(list)
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
            media_by_dir_name[(path.parent, path.name.casefold())].append(path)

    metadata: dict[Path, dict[str, Any]] = {}
    orphan_count = 0
    for sidecar in root.rglob("*.json"):
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        matches: list[Path] = []
        for candidate in _sidecar_candidates(sidecar, payload):
            matches.extend(media_by_dir_name.get((sidecar.parent, candidate.casefold()), []))
        matches = list(dict.fromkeys(matches))
        if len(matches) == 1:
            # Prefer a sidecar whose own name maps to the media over a title-only duplicate.
            current = metadata.get(matches[0])
            if current is None or sidecar.name.casefold().startswith(matches[0].name.casefold()):
                metadata[matches[0]] = {"payload": payload, "path": sidecar}
        elif not matches and any(key in payload for key in ("photoTakenTime", "url", "title")):
            orphan_count += 1
    return metadata, orphan_count


def scan_takeout(
    root: Path,
    progress: Callable[[str], None] | None = None,
    workers: int = 1,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[list[GoogleItem], dict[str, int]]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Google Takeout path is not a directory: {root}")
    sidecars, orphan_count = _load_sidecars(root)
    media = sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
    )
    def scan_media(path: Path) -> GoogleItem:
        if stop_requested and stop_requested():
            raise RuntimeError("Scan stopped")
        hashes = hash_file(path, stop_requested=stop_requested)
        sidecar = sidecars.get(path)
        payload = sidecar["payload"] if sidecar else {}
        url = payload.get("url")
        if not isinstance(url, str) or not is_google_photo_url(url):
            url = None
        return GoogleItem(
            path=str(path),
            relative_path=str(path.relative_to(root)),
            filename=path.name,
            size=path.stat().st_size,
            media_type=media_kind(path.name),
            sha1=hashes["sha1"],
            sha256=hashes["sha256"],
            taken_at=isoformat(_extract_taken_at(payload)),
            google_url=url,
            sidecar_path=str(sidecar["path"]) if sidecar else None,
        )

    exported_items: list[GoogleItem] = []
    for index, item in enumerate(ordered_parallel_map(scan_media, media, workers), start=1):
        exported_items.append(item)
        if progress and (index == 1 or index % 100 == 0 or index == len(media)):
            progress(f"Hashed Google Takeout media {index}/{len(media)}")
    # Takeout may export one cloud item into more than one album directory. A
    # Google Photos URL identifies the cloud item, so collapse only on that
    # strong identifier. Identical bytes without a URL may be distinct items.
    items: list[GoogleItem] = []
    by_url: dict[str, GoogleItem] = {}
    repeated_url_exports = 0
    for item in exported_items:
        if not item.google_url:
            items.append(item)
            continue
        existing = by_url.get(item.google_url)
        if existing is None:
            by_url[item.google_url] = item
            items.append(item)
            continue
        existing.repeated_takeout_paths.append(item.relative_path)
        repeated_url_exports += 1
    return items, {
        "exported_media_files": len(exported_items),
        "unique_google_items": len(items),
        "repeated_url_exports_collapsed": repeated_url_exports,
        "associated_sidecars": len(sidecars),
        "orphan_sidecars": orphan_count,
        "items_with_google_url": sum(1 for item in items if item.google_url),
    }
