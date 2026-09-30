from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Match
from .util import atomic_write_json


def build_report(
    matches: list[Match],
    *,
    takeout_root: str,
    immich_url: str,
    immich_version: str,
    google_stats: dict[str, int],
    immich_stats: dict[str, int],
) -> dict[str, Any]:
    counts = Counter(match.confidence for match in matches)
    exact_bytes = sum(match.google.size for match in matches if match.confidence == "exact_hash")
    eligible = sum(1 for match in matches if match.deletion_eligible)
    summary = {
        "google_items": len(matches),
        "immich_assets": immich_stats["assets"],
        "exact_hash_matches": counts["exact_hash"],
        "strong_metadata_matches_review_only": counts["strong_metadata"],
        "ambiguous_metadata_matches_review_only": counts["ambiguous_metadata"],
        "unmatched": counts["unmatched"],
        "exact_duplicate_bytes": exact_bytes,
        "exact_duplicate_gib": round(exact_bytes / (1024**3), 3),
        "trash_eligible_with_url": eligible,
        "exact_duplication_percent": round(100 * counts["exact_hash"] / len(matches), 2) if matches else 0.0,
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "inputs": {
            "takeout_root": takeout_root,
            "immich_url": immich_url,
            "immich_version": immich_version,
        },
        "safety": {
            "scan_deleted_nothing": True,
            "automatic_trash_threshold": "exact_hash only",
            "metadata_matches_are_review_only": True,
        },
        "google_scan": google_stats,
        "immich_scan": immich_stats,
        "summary": summary,
        "matches": [match.to_dict() for match in matches],
    }


def write_report(report: dict[str, Any], json_path: Path) -> tuple[Path, Path]:
    json_path = json_path.expanduser().resolve()
    atomic_write_json(json_path, report)
    csv_path = json_path.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "confidence", "deletion_eligible", "score", "google_relative_path", "google_size",
                "google_taken_at", "google_url", "google_sha1", "immich_id", "immich_filename",
                "immich_original_path", "reason",
            ],
        )
        writer.writeheader()
        for match in report["matches"]:
            google = match["google"]
            immich = match.get("immich") or {}
            writer.writerow(
                {
                    "confidence": match["confidence"],
                    "deletion_eligible": match["deletion_eligible"],
                    "score": match.get("score"),
                    "google_relative_path": google["relative_path"],
                    "google_size": google["size"],
                    "google_taken_at": google.get("taken_at"),
                    "google_url": google.get("google_url"),
                    "google_sha1": google["sha1"],
                    "immich_id": immich.get("id"),
                    "immich_filename": immich.get("filename"),
                    "immich_original_path": immich.get("original_path"),
                    "reason": match["reason"],
                }
            )
    return json_path, csv_path
