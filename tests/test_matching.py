from immich_google_dedupe.matching import match_items
from immich_google_dedupe.models import GoogleItem, ImmichAsset


def google(**overrides):
    values = dict(
        path="/takeout/a.jpg",
        relative_path="a.jpg",
        filename="a.jpg",
        size=3,
        media_type="image",
        sha1="a" * 40,
        sha256="b" * 64,
        taken_at="2024-01-01T00:00:00Z",
        google_url="https://photos.google.com/photo/abc",
    )
    values.update(overrides)
    return GoogleItem(**values)


def immich(**overrides):
    values = dict(
        id="asset-1",
        filename="a.jpg",
        original_path="upload/a.jpg",
        media_type="image",
        file_created_at="2024-01-01T00:00:00Z",
        width=100,
        height=100,
        checksum_sha1="a" * 40,
        checksum_reliable=True,
        is_external=False,
        is_offline=False,
        local_size=3,
    )
    values.update(overrides)
    return ImmichAsset(**values)


def test_exact_hash_is_eligible():
    result = match_items([google()], [immich()])[0]
    assert result.confidence == "exact_hash"
    assert result.deletion_eligible is True


def test_exact_without_url_is_not_eligible():
    result = match_items([google(google_url=None)], [immich()])[0]
    assert result.confidence == "exact_hash"
    assert result.deletion_eligible is False


def test_unreliable_external_hash_is_not_exact():
    result = match_items(
        [google()],
        [immich(checksum_reliable=False, is_external=True, local_size=None)],
    )[0]
    assert result.confidence == "strong_metadata"
    assert result.deletion_eligible is False


def test_metadata_only_never_auto_eligible():
    result = match_items(
        [google(sha1="c" * 40)],
        [immich(checksum_sha1="d" * 40)],
    )[0]
    assert result.confidence == "strong_metadata"
    assert result.deletion_eligible is False


def test_different_name_and_hash_is_unmatched():
    result = match_items(
        [google(filename="other.jpg", sha1="c" * 40)],
        [immich(checksum_sha1="d" * 40)],
    )[0]
    assert result.confidence == "unmatched"


def test_offline_exact_asset_is_not_eligible():
    result = match_items([google()], [immich(is_offline=True)])[0]
    assert result.confidence == "exact_hash"
    assert result.deletion_eligible is False


def test_online_exact_asset_is_preferred_over_offline_copy():
    result = match_items(
        [google()],
        [immich(id="offline", is_offline=True), immich(id="online", is_offline=False)],
    )[0]
    assert result.immich.id == "online"
    assert result.deletion_eligible is True
