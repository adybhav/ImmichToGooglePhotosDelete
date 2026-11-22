import argparse
import hashlib
import json
import threading
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pytest

from immich_google_dedupe.webui import LocalApp, WebError, candidate_id, launch_web_ui, make_handler


def test_lan_page_requires_access_code_cookie_and_api_token(tmp_path: Path):
    args = argparse.Namespace(
        takeout="", immich_url="http://localhost:2283", path_map=[],
        output=str(tmp_path / "missing-report.json"), workers=4, browser_channel="auto",
        settings_path=tmp_path / "settings.json",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(LocalApp(args), "private-token", require_access_link=True,
                     access_code="entry-code"),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        browser = build_opener(HTTPCookieProcessor(CookieJar()))
        login = browser.open(base + "/")
        assert login.status == 200
        assert b"Local review access" in login.read()
        with pytest.raises(HTTPError) as denied:
            browser.open(Request(base + "/?access=entry-code", headers={"Host": "other.example"}))
        assert denied.value.code == 403
        with pytest.raises(HTTPError) as denied:
            browser.open(Request(base + "/api/state", headers={"X-Session-Token": "private-token"}))
        assert denied.value.code == 403
        with pytest.raises(HTTPError) as denied:
            browser.open(Request(base + "/access", data=urlencode({"code": "wrong"}).encode()))
        assert denied.value.code == 403
        page = browser.open(Request(base + "/access", data=urlencode({"code": "entry-code"}).encode()))
        assert page.status == 200
        assert page.headers["Referrer-Policy"] == "no-referrer"
        assert b"const TOKEN" in page.read()
        state = browser.open(Request(base + "/api/state", headers={"X-Session-Token": "private-token"}))
        assert json.load(state)["report"]["exists"] is False
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_one_ui_switches_between_isolated_account_workspaces(tmp_path: Path):
    apps = {}
    for name in ("july20", "july21"):
        workspace = tmp_path / name
        workspace.mkdir()
        args = argparse.Namespace(
            takeout="", immich_url="http://localhost:2283", path_map=[],
            output=str(workspace / "reports" / "duplication-report.json"),
            workers=4, browser_channel="auto",
            settings_path=workspace / ".immich-gphotos-settings.json",
        )
        apps[name] = LocalApp(args)
        assert apps[name].workspace == workspace
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(apps["july20"], "private-token", account_apps=apps),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    browser = build_opener(HTTPCookieProcessor(CookieJar()))
    headers = {"X-Session-Token": "private-token"}
    try:
        first = json.load(browser.open(Request(base + "/api/state", headers=headers)))
        assert first["account"] == "july20"
        assert first["accounts"] == ["july20", "july21"]
        assert "july20" in first["report"]["path"]
        browser.open(Request(
            base + "/api/account/select", data=json.dumps({"account": "july21"}).encode(),
            headers={**headers, "Content-Type": "application/json"},
        ))
        second = json.load(browser.open(Request(base + "/api/state", headers=headers)))
        assert second["account"] == "july21"
        assert "july21" in second["report"]["path"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("host", ["0.0.0.0", "8.8.8.8", "example.com"])
def test_web_ui_rejects_nonlocal_bind_addresses(host: str):
    with pytest.raises(ValueError, match="--host"):
        launch_web_ui(argparse.Namespace(host=host, port=8765))


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


def test_scanned_media_view_includes_unmatched_without_selecting_it(tmp_path: Path):
    media = tmp_path / "unmatched.jpg"
    media.write_bytes(b"image")
    report_path = tmp_path / "duplication-report.json"
    report_path.write_text(json.dumps({
        "schema_version": 1,
        "inputs": {"takeout_root": str(tmp_path)},
        "matches": [{
            "confidence": "unmatched", "deletion_eligible": False,
            "google": {
                "path": str(media), "relative_path": "Album/unmatched.jpg",
                "filename": "unmatched.jpg", "size": 5, "media_type": "image",
                "sha1": hashlib.sha1(b"image").hexdigest(), "google_url": None,
            },
            "immich": None,
        }],
    }), encoding="utf-8")
    args = argparse.Namespace(
        takeout=str(tmp_path), immich_url="http://localhost:2283", path_map=[],
        output=str(report_path), workers=4, browser_channel="chrome",
    )
    app = LocalApp(args)
    assert app.candidate_page()["total"] == 0
    page = app.candidate_page(view="scanned")
    assert page["total"] == 1
    assert page["items"][0]["candidate"] is False
    assert page["items"][0]["selected"] is False
    assert page["items"][0]["confidence"] == "unmatched"
    assert app.state()["scan_folders"][0]["name"] == "Album"
    with pytest.raises(WebError, match="Unknown candidate"):
        app.set_selection({"type": "candidate", "id": page["items"][0]["id"], "selected": True})


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
