import base64
import hashlib
from pathlib import Path

import pytest

from immich_google_dedupe.immich import build_assets, decode_sha1, parse_path_maps, resolve_local_path


def test_decode_sha1_base64():
    raw = hashlib.sha1(b"hello").digest()
    assert decode_sha1(base64.b64encode(raw).decode()) == raw.hex()


def test_resolve_storage_upload_prefix(tmp_path: Path):
    target = tmp_path / "library" / "user" / "a.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"hello")
    resolved = resolve_local_path("/usr/src/app/upload/library/user/a.jpg", tmp_path, [])
    assert resolved == target.resolve()


@pytest.mark.parametrize("workers", [1, 4])
def test_external_asset_gets_real_local_hash(tmp_path: Path, workers: int):
    target = tmp_path / "photos" / "a.jpg"
    target.parent.mkdir()
    target.write_bytes(b"hello")
    raw = [
        {
            "id": "id",
            "libraryId": "library",
            "originalPath": "/external/a.jpg",
            "originalFileName": "a.jpg",
            "type": "IMAGE",
            "fileCreatedAt": "2024-01-01T00:00:00Z",
            "checksum": base64.b64encode(b"x" * 20).decode(),
        }
    ]
    assets, stats = build_assets(raw, path_maps=[("/external", target.parent)], workers=workers)
    assert assets[0].checksum_sha1 == hashlib.sha1(b"hello").hexdigest()
    assert assets[0].checksum_reliable is True
    assert stats["locally_hashed"] == 1


def test_bad_path_map_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        parse_path_maps(["missing-separator"])
