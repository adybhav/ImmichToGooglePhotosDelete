import hashlib
import json
from pathlib import Path

import pytest

from immich_google_dedupe.takeout import scan_takeout


@pytest.mark.parametrize("workers", [1, 4])
def test_scan_takeout_associates_sidecar(tmp_path: Path, workers: int):
    album = tmp_path / "Takeout" / "Google Photos" / "Album"
    album.mkdir(parents=True)
    media = album / "IMG_0001.jpg"
    media.write_bytes(b"not really a jpeg")
    (album / "IMG_0001.jpg.json").write_text(
        json.dumps(
            {
                "title": "IMG_0001.jpg",
                "url": "https://photos.google.com/photo/example",
                "photoTakenTime": {"timestamp": "1700000000"},
            }
        ),
        encoding="utf-8",
    )
    items, stats = scan_takeout(tmp_path, workers=workers)
    assert len(items) == 1
    assert items[0].sha1 == hashlib.sha1(b"not really a jpeg").hexdigest()
    assert items[0].google_url == "https://photos.google.com/photo/example"
    assert items[0].taken_at == "2023-11-14T22:13:20Z"
    assert stats["associated_sidecars"] == 1


def test_scan_takeout_rejects_non_google_url(tmp_path: Path):
    media = tmp_path / "x.png"
    media.write_bytes(b"png")
    (tmp_path / "x.png.json").write_text(
        json.dumps({"title": "x.png", "url": "https://evil.example/photo/1"}), encoding="utf-8"
    )
    items, _ = scan_takeout(tmp_path)
    assert items[0].google_url is None


def test_scan_takeout_includes_raw_files(tmp_path: Path):
    (tmp_path / "camera.dng").write_bytes(b"raw dng")
    (tmp_path / "camera.cr2").write_bytes(b"raw cr2")
    items, stats = scan_takeout(tmp_path, workers=2)
    assert [item.filename for item in items] == ["camera.cr2", "camera.dng"]
    assert stats["exported_media_files"] == 2


@pytest.mark.parametrize("workers", [1, 4])
def test_scan_takeout_collapses_repeated_cloud_url(tmp_path: Path, workers: int):
    for album_name in ("Album A", "Album B"):
        album = tmp_path / album_name
        album.mkdir()
        media = album / "same.jpg"
        media.write_bytes(b"same bytes")
        (album / "same.jpg.json").write_text(
            json.dumps(
                {
                    "title": "same.jpg",
                    "url": "https://photos.google.com/photo/one-item",
                    "photoTakenTime": {"timestamp": "1700000000"},
                }
            ),
            encoding="utf-8",
        )
    items, stats = scan_takeout(tmp_path, workers=workers)
    assert len(items) == 1
    assert len(items[0].repeated_takeout_paths) == 1
    assert stats["exported_media_files"] == 2
    assert stats["unique_google_items"] == 1
    assert stats["repeated_url_exports_collapsed"] == 1
