from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from .models import GoogleItem, ImmichAsset, Match
from .util import parse_timestamp


COPY_SUFFIX = re.compile(r"(?:[ _-](?:copy|edited|edit)|\s*\(\d+\))$", re.IGNORECASE)


def normalized_stem(filename: str) -> str:
    stem = unicodedata.normalize("NFKC", Path(filename).stem).casefold().strip()
    return COPY_SUFFIX.sub("", stem)


def _seconds_apart(left: str | None, right: str | None) -> float | None:
    left_dt, right_dt = parse_timestamp(left), parse_timestamp(right)
    if not left_dt or not right_dt:
        return None
    return abs((left_dt - right_dt).total_seconds())


def _metadata_score(google: GoogleItem, immich: ImmichAsset, tolerance: int) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if google.media_type != immich.media_type:
        return 0, ["media type differs"]
    score += 5
    reasons.append("same media type")
    if google.filename.casefold() == immich.filename.casefold():
        score += 45
        reasons.append("same filename")
    elif normalized_stem(google.filename) == normalized_stem(immich.filename):
        score += 35
        reasons.append("same normalized filename stem")
    else:
        return score, reasons
    delta = _seconds_apart(google.taken_at, immich.file_created_at)
    if delta is not None and delta <= tolerance:
        score += 40
        reasons.append(f"capture times differ by {delta:.3f}s")
    elif delta is not None and delta <= 60:
        score += 25
        reasons.append(f"capture times differ by {delta:.3f}s")
    if immich.local_size is not None:
        if google.size == immich.local_size:
            score += 20
            reasons.append("same byte size")
        elif max(google.size, immich.local_size) and abs(google.size - immich.local_size) / max(
            google.size, immich.local_size
        ) <= 0.01:
            score += 10
            reasons.append("byte sizes within 1%")
    return score, reasons


def match_items(
    google_items: list[GoogleItem],
    immich_assets: list[ImmichAsset],
    *,
    time_tolerance_seconds: int = 2,
) -> list[Match]:
    by_hash: dict[str, list[ImmichAsset]] = defaultdict(list)
    by_stem: dict[tuple[str, str], list[ImmichAsset]] = defaultdict(list)
    for asset in immich_assets:
        if asset.checksum_reliable and asset.checksum_sha1:
            by_hash[asset.checksum_sha1].append(asset)
        by_stem[(asset.media_type, normalized_stem(asset.filename))].append(asset)

    matches: list[Match] = []
    for google in google_items:
        exact = by_hash.get(google.sha1, [])
        if exact:
            ordered = sorted(exact, key=lambda asset: (asset.is_offline, asset.id))
            chosen = ordered[0]
            alternatives = [asset.id for asset in ordered[1:]]
            eligible = bool(google.google_url) and not chosen.is_offline
            reason = "SHA-1 of the Google Takeout file equals a reliable Immich content checksum"
            if not google.google_url:
                reason += "; Takeout sidecar has no usable Google Photos URL"
            if chosen.is_offline:
                reason += "; matching Immich asset is offline"
            matches.append(
                Match(
                    google=google,
                    confidence="exact_hash",
                    reason=reason,
                    immich=chosen,
                    score=100,
                    deletion_eligible=eligible,
                    alternatives=alternatives,
                )
            )
            continue

        candidates = by_stem.get((google.media_type, normalized_stem(google.filename)), [])
        scored = []
        for asset in candidates:
            score, reasons = _metadata_score(google, asset, time_tolerance_seconds)
            if score >= 75:
                scored.append((score, asset, reasons))
        scored.sort(key=lambda item: (-item[0], item[1].id))
        if scored:
            best_score, chosen, reasons = scored[0]
            tied = [asset.id for score, asset, _ in scored[1:] if score == best_score]
            confidence = "ambiguous_metadata" if tied else "strong_metadata"
            reason = "; ".join(reasons) + "; content hash was not proven"
            matches.append(
                Match(
                    google=google,
                    confidence=confidence,
                    reason=reason,
                    immich=chosen,
                    score=best_score,
                    deletion_eligible=False,
                    alternatives=tied,
                )
            )
        else:
            matches.append(
                Match(
                    google=google,
                    confidence="unmatched",
                    reason="No reliable content-hash or strong metadata match was found",
                )
            )
    return matches
