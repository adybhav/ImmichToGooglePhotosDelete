from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from typing import Any, Callable

import requests

from .models import ImmichAsset
from .util import hash_file, media_kind, ordered_parallel_map


def normalize_api_url(url: str) -> str:
    value = url.rstrip("/")
    if not value.endswith("/api"):
        value += "/api"
    return value


def decode_sha1(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) == 40:
        try:
            bytes.fromhex(value)
            return value.lower()
        except ValueError:
            return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw.hex() if len(raw) == 20 else None


def parse_path_maps(values: list[str]) -> list[tuple[str, Path]]:
    mappings: list[tuple[str, Path]] = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"Invalid --path-map {value!r}; expected CONTAINER_PREFIX=LOCAL_DIRECTORY")
        remote, local = value.split("=", 1)
        remote = remote.rstrip("/\\")
        path = Path(local).expanduser().resolve()
        if not remote or not path.is_dir():
            raise ValueError(f"Invalid --path-map {value!r}; local directory does not exist")
        mappings.append((remote.replace("\\", "/"), path))
    return sorted(mappings, key=lambda item: len(item[0]), reverse=True)


def resolve_local_path(
    original_path: str,
    storage_root: Path | None,
    path_maps: list[tuple[str, Path]],
) -> Path | None:
    native = Path(original_path)
    if native.is_file():
        return native.resolve()
    normalized = original_path.replace("\\", "/")
    for remote, local in path_maps:
        if normalized == remote or normalized.startswith(remote + "/"):
            suffix = normalized[len(remote) :].lstrip("/")
            candidate = local.joinpath(*suffix.split("/"))
            return candidate.resolve() if candidate.is_file() else None
    if storage_root:
        root = storage_root.expanduser().resolve()
        relative = normalized.lstrip("/")
        marker = "/upload/"
        if marker in normalized:
            relative = normalized.split(marker, 1)[1]
        elif relative.startswith("upload/"):
            relative = relative[len("upload/") :]
        candidate = root.joinpath(*relative.split("/"))
        return candidate.resolve() if candidate.is_file() else None
    return None


class ImmichClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        verify_ssl: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = normalize_api_url(base_url)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key, "Accept": "application/json"})
        self.session.verify = verify_ssl

    def _request(self, method: str, path: str, **kwargs):
        try:
            response = self.session.request(
                method, f"{self.base_url}/{path.lstrip('/')}", timeout=self.timeout, **kwargs
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            detail = ""
            if getattr(exc, "response", None) is not None:
                detail = f" Response: {exc.response.text[:500]}"
            raise RuntimeError(f"Immich API request failed: {exc}.{detail}") from exc

    def server_version(self) -> str:
        for endpoint in ("server/version", "server/about"):
            try:
                data = self._request("GET", endpoint).json()
                if endpoint.endswith("version"):
                    return ".".join(str(data.get(key, 0)) for key in ("major", "minor", "patch"))
                return str(data.get("version") or data.get("versionUrl") or "unknown")
            except RuntimeError:
                continue
        raise RuntimeError("Connected to the URL, but could not read the Immich server version")

    def list_assets(self, progress: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
        page = 1
        assets: list[dict[str, Any]] = []
        while True:
            response = self._request(
                "POST",
                "search/metadata",
                json={"page": page, "size": 1000, "withExif": True, "withDeleted": False},
            ).json()
            batch = response.get("assets", {}).get("items", [])
            if not isinstance(batch, list):
                raise RuntimeError("Unexpected response from Immich search/metadata")
            assets.extend(batch)
            if progress:
                progress(f"Fetched {len(assets)} Immich assets")
            next_page = response.get("assets", {}).get("nextPage")
            if not batch or next_page in (None, "", 0, False):
                break
            try:
                page = int(next_page)
            except (TypeError, ValueError):
                page += 1
        return assets


def build_assets(
    raw_assets: list[dict[str, Any]],
    *,
    storage_root: Path | None = None,
    path_maps: list[tuple[str, Path]] | None = None,
    progress: Callable[[str], None] | None = None,
    workers: int = 1,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[list[ImmichAsset], dict[str, int]]:
    path_maps = path_maps or []
    assets: list[ImmichAsset] = []
    locally_hashed = 0
    unresolved_external = 0

    def build_asset(raw: dict[str, Any]) -> ImmichAsset:
        if stop_requested and stop_requested():
            raise RuntimeError("Scan stopped")
        original_path = str(raw.get("originalPath") or "")
        is_external = bool(raw.get("libraryId"))
        local_path = resolve_local_path(original_path, storage_root, path_maps)
        checksum = decode_sha1(raw.get("checksum"))
        checksum_reliable = bool(checksum) and not is_external
        local_size = None
        if local_path:
            checksum = hash_file(local_path, ("sha1",), stop_requested=stop_requested)["sha1"]
            checksum_reliable = True
            local_size = local_path.stat().st_size
        filename = str(raw.get("originalFileName") or Path(original_path).name)
        return ImmichAsset(
            id=str(raw.get("id") or ""),
            filename=filename,
            original_path=original_path,
            media_type=media_kind(str(raw.get("originalMimeType") or filename)),
            file_created_at=raw.get("fileCreatedAt"),
            width=_optional_int(raw.get("width")),
            height=_optional_int(raw.get("height")),
            checksum_sha1=checksum,
            checksum_reliable=checksum_reliable,
            is_external=is_external,
            is_offline=bool(raw.get("isOffline")),
            local_path=str(local_path) if local_path else None,
            local_size=local_size,
        )

    for asset in ordered_parallel_map(build_asset, raw_assets, workers):
        assets.append(asset)
        if asset.local_path:
            locally_hashed += 1
            if progress and (locally_hashed == 1 or locally_hashed % 100 == 0):
                progress(f"Hashed {locally_hashed} Immich originals from local storage")
        elif asset.is_external:
            unresolved_external += 1
    return assets, {
        "assets": len(assets),
        "reliable_checksums": sum(1 for asset in assets if asset.checksum_reliable),
        "locally_hashed": locally_hashed,
        "unresolved_external_assets": unresolved_external,
        "offline_assets": sum(1 for asset in assets if asset.is_offline),
    }


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
