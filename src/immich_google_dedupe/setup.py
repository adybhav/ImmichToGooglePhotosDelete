"""Read-only setup discovery for the local web UI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .immich import ImmichClient, parse_path_maps, resolve_local_path


def local_bind_source(source: str) -> str | None:
    """Return a Docker bind source only when this Python process can read it."""
    path = Path(source)
    if path.is_dir():
        return str(path.resolve())
    if sys.platform == "win32":
        match = re.fullmatch(r"/mnt/([a-zA-Z])/(.+)", source)
        if match:
            windows_path = Path(f"{match[1].upper()}:/{match[2]}")
            if windows_path.is_dir():
                return str(windows_path.resolve())
    return None


def _docker(*arguments: str) -> str:
    if not shutil.which("docker"):
        raise RuntimeError("Docker CLI is not installed or is not on PATH; enter the URL and mappings manually.")
    try:
        result = subprocess.run(
            ["docker", *arguments], capture_output=True, text=True, timeout=8, check=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Docker discovery could not finish: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Docker discovery failed: {exc.stderr.strip()[:300]}") from exc
    return result.stdout.strip()


def discover_immich() -> list[dict[str, Any]]:
    """Inspect only names, published ports, and mounts; never read container environment."""
    rows = _docker("ps", "--format", "{{json .}}")
    found = []
    for line in rows.splitlines():
        row = json.loads(line)
        identity = f"{row.get('Names', '')} {row.get('Image', '')}".lower()
        if "immich" not in identity or "server" not in identity:
            continue
        if "machine-learning" in identity or "machine_learning" in identity:
            continue
        container_id = str(row.get("ID") or "")
        if not container_id:
            continue
        mounts = json.loads(_docker("inspect", container_id, "--format", "{{json .Mounts}}")) or []
        ports = json.loads(_docker("inspect", container_id, "--format", "{{json .NetworkSettings.Ports}}")) or {}
        bindings = ports.get("2283/tcp") or []
        published = next((str(item.get("HostPort")) for item in bindings if item.get("HostPort")), None)
        suggestions = []
        for mount in mounts:
            if mount.get("Type") != "bind":
                continue
            remote = str(mount.get("Destination") or "")
            source = str(mount.get("Source") or "")
            local = local_bind_source(source)
            suggestions.append({
                "container_path": remote,
                "docker_source": source,
                "local_path": local,
                "mapping": f"{remote}={local}" if remote and local else None,
            })
        found.append({
            "name": row.get("Names") or container_id,
            "url": f"http://localhost:{published}" if published else None,
            "mounts": suggestions,
        })
    return found


def check_immich_setup(url: str, api_key: str, path_map: list[str]) -> dict[str, Any]:
    """Check API access and a small asset sample without hashing or changing media."""
    maps = parse_path_maps(path_map)
    client = ImmichClient(url, api_key, timeout=8)
    try:
        version = client.server_version()
        raw = client._request(
            "POST", "search/metadata",
            json={"page": 1, "size": 100, "withExif": False, "withDeleted": False},
        ).json()
    finally:
        client.session.close()
    assets = raw.get("assets", {}).get("items", [])
    if not isinstance(assets, list):
        raise RuntimeError("Immich returned an unexpected asset list")
    examples = []
    external = 0
    readable_external = 0
    readable = 0
    for asset in assets:
        original = str(asset.get("originalPath") or "")
        local = resolve_local_path(original, None, maps)
        is_external = bool(asset.get("libraryId"))
        external += is_external
        readable += local is not None
        readable_external += is_external and local is not None
        if len(examples) < 5 and (is_external or local is not None):
            examples.append({"immich_path": original, "local_path": str(local) if local else None,
                             "external": is_external})
    return {
        "version": version, "sampled": len(assets), "readable": readable,
        "external": external, "readable_external": readable_external,
        "examples": examples,
    }
