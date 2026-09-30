import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from immich_google_dedupe import setup
from immich_google_dedupe.webui import LocalApp, WebError, setup_values


def test_setup_values_accepts_multiple_linux_mappings_and_rejects_missing_takeout(tmp_path: Path):
    first = tmp_path / "library"
    second = tmp_path / "external"
    first.mkdir()
    second.mkdir()
    payload = {
        "takeout": str(tmp_path), "immich_url": "http://localhost:2283",
        "path_map": f"/data={first}\n/external={second}\n", "output": str(tmp_path / "report.json"),
        "workers": "4",
    }
    assert setup_values(payload, require_takeout=True)["path_map"] == [f"/data={first}", f"/external={second}"]
    payload["takeout"] = ""
    with pytest.raises(WebError, match="Takeout folder does not exist"):
        setup_values(payload, require_takeout=True)


def test_local_settings_round_trip_without_api_key(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "settings.json"
    args = argparse.Namespace(
        takeout=None, immich_url=None, path_map=None, output=None, workers=None,
        browser_channel=None, settings_path=settings,
    )
    app = LocalApp(args)
    assert app.state()["config"]["path_map"] == []
    app.save_setup({
        "takeout": str(tmp_path), "immich_url": "http://localhost:2283",
        "path_map": [], "output": str(tmp_path / "report.json"), "workers": 3,
        "api_key": "must-not-persist",
    })
    content = settings.read_text(encoding="utf-8")
    assert "must-not-persist" not in content
    assert LocalApp(args).state()["config"]["workers"] == 3


def test_docker_discovery_only_suggests_readable_bind_paths(monkeypatch, tmp_path: Path):
    mount = tmp_path / "library"
    mount.mkdir()
    outputs = {
        ("ps", "--format", "{{json .}}"): "\n".join([
            json.dumps({"ID": "server", "Names": "immich_server", "Image": "ghcr.io/immich-app/immich-server"}),
            json.dumps({"ID": "db", "Names": "immich_postgres", "Image": "postgres"}),
        ]),
        ("inspect", "server", "--format", "{{json .Mounts}}"): json.dumps([
            {"Type": "bind", "Source": str(mount), "Destination": "/data"},
            {"Type": "bind", "Source": "/not/readable", "Destination": "/external"},
            {"Type": "volume", "Source": "/hidden", "Destination": "/other"},
        ]),
        ("inspect", "server", "--format", "{{json .NetworkSettings.Ports}}"): json.dumps({
            "2283/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2284"}]
        }),
    }
    monkeypatch.setattr(setup, "_docker", lambda *arguments: outputs[arguments])
    found = setup.discover_immich()
    assert len(found) == 1
    assert found[0]["url"] == "http://localhost:2284"
    assert found[0]["mounts"][0]["mapping"] == f"/data={mount}"
    assert found[0]["mounts"][1]["mapping"] is None


def test_setup_check_samples_paths_without_hashing(monkeypatch, tmp_path: Path):
    media = tmp_path / "example.jpg"
    media.write_bytes(b"photo")

    class FakeClient:
        def __init__(self, url, key, timeout):
            assert key == "key"
            self.session = SimpleNamespace(close=lambda: None)

        def server_version(self):
            return "2.0.0"

        def _request(self, method, path, **kwargs):
            assert path == "search/metadata"
            assert kwargs["json"]["size"] == 100
            return SimpleNamespace(json=lambda: {"assets": {"items": [
                {"originalPath": "/data/example.jpg", "libraryId": "external"},
                {"originalPath": "/data/missing.jpg", "libraryId": "external"},
            ]}})

    monkeypatch.setattr(setup, "ImmichClient", FakeClient)
    result = setup.check_immich_setup("http://localhost:2283", "key", [f"/data={tmp_path}"])
    assert result["version"] == "2.0.0"
    assert result["sampled"] == 2
    assert result["readable_external"] == 1
    assert result["examples"][0]["local_path"] == str(media)
