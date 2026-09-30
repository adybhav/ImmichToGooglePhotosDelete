import hashlib
import json
import re
import argparse
from pathlib import Path

import pytest

from immich_google_dedupe import trash
from immich_google_dedupe.trash import load_candidates, resolve_browser_channel


def report_for(path: Path, *, google_sha1: str, immich_sha1: str | None = None):
    return {
        "schema_version": 1,
        "matches": [
            {
                "confidence": "exact_hash",
                "deletion_eligible": True,
                "google": {
                    "path": str(path),
                    "relative_path": path.name,
                    "sha1": google_sha1,
                    "google_url": "https://photos.google.com/photo/abc",
                },
                "immich": {
                    "checksum_sha1": immich_sha1 or google_sha1,
                    "checksum_reliable": True,
                    "is_offline": False,
                },
            }
        ],
    }


def test_load_candidates_rehashes_evidence(tmp_path: Path):
    media = tmp_path / "a.jpg"
    media.write_bytes(b"hello")
    sha1 = hashlib.sha1(b"hello").hexdigest()
    report = tmp_path / "report.json"
    report.write_text(json.dumps(report_for(media, google_sha1=sha1)), encoding="utf-8")
    assert len(load_candidates(report)) == 1


def test_load_candidates_rejects_changed_evidence(tmp_path: Path):
    media = tmp_path / "a.jpg"
    media.write_bytes(b"changed")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(report_for(media, google_sha1="a" * 40)), encoding="utf-8")
    with pytest.raises(ValueError, match="changed since the scan"):
        load_candidates(report)


def test_load_candidates_skips_tampered_hash_pair(tmp_path: Path):
    media = tmp_path / "a.jpg"
    media.write_bytes(b"hello")
    sha1 = hashlib.sha1(b"hello").hexdigest()
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(report_for(media, google_sha1=sha1, immich_sha1="b" * 40)), encoding="utf-8"
    )
    assert load_candidates(report) == []


def test_load_candidates_filters_before_parallel_evidence_check(tmp_path: Path):
    media = tmp_path / "a.jpg"
    media.write_bytes(b"hello")
    sha1 = hashlib.sha1(b"hello").hexdigest()
    report = tmp_path / "report.json"
    selected = report_for(media, google_sha1=sha1)["matches"][0]
    excluded = json.loads(json.dumps(selected))
    excluded["google"]["google_url"] = "https://photos.google.com/photo/other"
    excluded["google"]["path"] = str(tmp_path / "missing.jpg")
    report.write_text(json.dumps({"schema_version": 1, "matches": [selected, excluded]}), encoding="utf-8")
    assert len(load_candidates(
        report, selected_urls={"https://photos.google.com/photo/abc"}, workers=4
    )) == 1


def test_explicit_browser_channel_is_preserved():
    assert resolve_browser_channel("msedge") == "msedge"


def test_human_sign_in_opens_regular_browser_with_dedicated_profile(tmp_path: Path, monkeypatch):
    browser = tmp_path / "chrome.exe"
    calls = []
    monkeypatch.setattr(trash, "browser_executable", lambda channel: browser)
    monkeypatch.setattr(trash.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)))
    profile = tmp_path / "private-profile"

    trash.open_sign_in_browser(profile, "chrome")

    assert profile.is_dir()
    assert calls == [([
        str(browser), f"--user-data-dir={profile}", "--new-window",
        "--no-first-run", "--no-default-browser-check",
        "https://photos.google.com/",
    ], {"close_fds": True})]


def test_human_sign_in_rejects_playwright_only_chromium():
    with pytest.raises(ValueError, match="Chrome or Edge"):
        trash.browser_executable("chromium")


def test_trash_run_stops_if_google_redirects_to_sign_in(tmp_path: Path, monkeypatch):
    class FakePage:
        url = "https://accounts.google.com/v3/signin"

        def goto(self, *_args, **_kwargs):
            pass

        def get_by_role(self, *_args, **_kwargs):
            raise AssertionError("Trash controls must not be touched while signed out")

    class FakeContext:
        pages = [FakePage()]
        closed = False

        def close(self):
            self.closed = True

    context = FakeContext()

    class FakeChromium:
        def launch_persistent_context(self, *_args, **_kwargs):
            return context

    class FakePlaywright:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr("playwright.sync_api.sync_playwright", FakePlaywright)
    match = {"google": {"relative_path": "one.jpg", "google_url": "https://photos.google.com/photo/one"}}
    result_path = tmp_path / "results.json"

    with pytest.raises(RuntimeError, match="still asking for sign-in"):
        trash.trash_candidates(
            [match], profile_dir=tmp_path / "profile", result_path=result_path,
            button_pattern="Trash", confirm_pattern="Trash", browser_channel="chrome",
            login_ready=lambda: True,
        )

    assert context.closed
    assert not result_path.exists()


def test_confirmation_click_skips_covered_toolbar_icon():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as manager:
        try:
            browser = manager.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"Chrome browser unavailable for popup test: {exc}")
        try:
            page = browser.new_page()
            page.set_content("""
                <style>
                  #toolbar { position:fixed; right:10px; top:10px }
                  #overlay { position:fixed; inset:0; background:#0004 }
                  #dialog { position:fixed; right:0; top:20px; background:white; padding:25px }
                </style>
                <div id="overlay"><div id="dialog" role="dialog">
                  Remove from your Google Account?
                  <button id="confirm" onclick="window.confirmed=true; document.getElementById('overlay').remove()">
                    Move to trash
                  </button>
                </div></div>
                <button id="toolbar" aria-label="Move to trash">🗑</button>
            """)
            pattern = re.compile(r"^(Move to trash|Trash)$", re.IGNORECASE)
            assert page.get_by_role("button", name=pattern).last.get_attribute("id") == "toolbar"

            trash.click_trash_confirmation(page, pattern)

            assert page.evaluate("window.confirmed") is True
            assert page.locator("#overlay").count() == 0
        finally:
            browser.close()


def test_cli_execute_does_not_bypass_saved_size_rule(tmp_path: Path):
    from immich_google_dedupe.cli import run_trash

    report = tmp_path / "duplication-report.json"
    report.write_text(json.dumps({"schema_version": 1, "matches": []}), encoding="utf-8")
    (tmp_path / "review-selection.json").write_text(json.dumps({
        "report_digest": hashlib.sha256(report.read_bytes()).hexdigest(),
        "excluded_folders": [], "deselected_ids": [],
        "size_filter": {"enabled": True, "minimum_mib": 2},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="media size filter"):
        run_trash(argparse.Namespace(
            report=str(report), execute=True, ignore_review_selection=False,
        ))
