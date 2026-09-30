import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from immich_google_dedupe.webui import LocalApp, WebError, candidate_id


def test_album_exclusion_covers_repeated_takeout_paths_and_persists(tmp_path: Path):
    media = tmp_path / "photo.jpg"
    media.write_bytes(b"photo")
    sha1 = hashlib.sha1(b"photo").hexdigest()
    url = "https://photos.google.com/photo/one"
    report_path = tmp_path / "duplication-report.json"
    report_path.write_text(json.dumps({
        "schema_version": 1,
        "summary": {"google_items": 1, "exact_hash_matches": 1},
        "matches": [{
            "confidence": "exact_hash", "deletion_eligible": True,
            "google": {
                "path": str(media), "relative_path": "Album A/photo.jpg",
                "repeated_takeout_paths": ["Album B/photo.jpg"],
                "filename": "photo.jpg", "size": 5, "media_type": "image",
                "sha1": sha1, "google_url": url,
            },
            "immich": {"checksum_sha1": sha1, "checksum_reliable": True, "is_offline": False},
        }],
    }), encoding="utf-8")
    args = argparse.Namespace(
        takeout=str(tmp_path), immich_url="http://localhost:2283", path_map=[],
        output=str(report_path), workers=4, browser_channel="chrome",
    )
    app = LocalApp(args)
    app.set_selection({"type": "size_filter", "enabled": False, "minimum_mib": 2})
    assert app.state()["counts"]["selected"] == 1
    app.auth_process = SimpleNamespace(poll=lambda: None)
    with pytest.raises(WebError, match="still open"):
        app.start_trash({"scope": "one", "confirmation": "TRASH 1 GOOGLE PHOTOS ITEMS"})
    assert app.state()["job"]["status"] == "idle"
    app.auth_process = None
    app.set_selection({"type": "folder", "name": "Album B", "excluded": True})
    assert app.state()["counts"]["selected"] == 0
    assert app.candidate_page()["items"][0]["album_excluded"] is True
    assert LocalApp(args).state()["counts"]["selected"] == 0
    assert candidate_id(url) in app.by_id

    (tmp_path / "trash-run-example.json").write_text(json.dumps({
        "results": [{"url": url, "status": "trashed"}]
    }), encoding="utf-8")
    resumed = LocalApp(args)
    assert resumed.state()["counts"]["completed"] == 1
    assert resumed.state()["counts"]["selected"] == 0


def test_media_size_rule_covers_images_and_videos_and_persists(tmp_path: Path):
    matches = []
    for name, media_type, size in (
        ("at-limit.jpg", "image", 2 * 1024 * 1024),
        ("over-limit.jpg", "image", 2 * 1024 * 1024 + 1),
        ("short-video.mp4", "video", 2 * 1024 * 1024),
    ):
        media = tmp_path / name
        media.write_bytes(b"x")
        sha1 = hashlib.sha1(b"x").hexdigest()
        matches.append({
            "confidence": "exact_hash", "deletion_eligible": True,
            "google": {
                "path": str(media), "relative_path": name, "filename": name,
                "size": size, "media_type": media_type,
                "sha1": sha1, "google_url": f"https://photos.google.com/photo/{name}",
            },
            "immich": {"checksum_sha1": sha1, "checksum_reliable": True, "is_offline": False},
        })
    report_path = tmp_path / "duplication-report.json"
    report_path.write_text(json.dumps({"schema_version": 1, "matches": matches}), encoding="utf-8")
    args = argparse.Namespace(
        takeout=str(tmp_path), immich_url="http://localhost:2283", path_map=[],
        output=str(report_path), workers=4, browser_channel="chrome",
    )
    app = LocalApp(args)
    assert app.state()["counts"]["selected"] == 1
    assert app.state()["counts"]["kept_by_size"] == 2
    assert app.candidate_page()["items"][0]["size_excluded"] is True
    assert app.candidate_page()["items"][2]["size_excluded"] is True

    app.set_selection({"type": "size_filter", "enabled": True, "minimum_mib": 500})
    assert app.state()["counts"]["selected"] == 0
    assert app.state()["counts"]["kept_by_size"] == 3
    app.set_selection({"type": "size_filter", "enabled": False, "minimum_mib": 3})
    assert app.state()["counts"]["selected"] == 3
    assert LocalApp(args).state()["size_filter"] == {"enabled": False, "minimum_mib": 3.0}
