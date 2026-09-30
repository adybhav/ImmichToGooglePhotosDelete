from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .util import atomic_write_json, hash_file, is_google_photo_url, ordered_parallel_map


def resolve_browser_channel(requested: str) -> str:
    if requested != "auto":
        return requested
    if sys.platform == "win32":
        local = Path(os.getenv("LOCALAPPDATA", ""))
        program_files = [Path(os.getenv("PROGRAMFILES", "")), Path(os.getenv("PROGRAMFILES(X86)", ""))]
        chrome = [root / "Google/Chrome/Application/chrome.exe" for root in [local, *program_files]]
        edge = [root / "Microsoft/Edge/Application/msedge.exe" for root in [local, *program_files]]
        if shutil.which("chrome") or any(path.is_file() for path in chrome):
            return "chrome"
        if shutil.which("msedge") or any(path.is_file() for path in edge):
            return "msedge"
    return "chrome"


def browser_executable(channel: str) -> Path:
    """Locate the installed browser for a human sign-in, without Playwright."""
    resolved = resolve_browser_channel(channel)
    if resolved not in {"chrome", "msedge"}:
        raise ValueError("Use Chrome or Edge for the separate Google sign-in step")
    binary = "chrome.exe" if resolved == "chrome" else "msedge.exe"
    if sys.platform == "win32":
        roots = [
            Path(value) for value in (
                os.getenv("LOCALAPPDATA"), os.getenv("PROGRAMFILES"),
                os.getenv("PROGRAMFILES(X86)"),
            ) if value
        ]
        relative = Path("Google/Chrome/Application" if resolved == "chrome" else "Microsoft/Edge/Application")
        for root in roots:
            path = root / relative / binary
            if path.is_file():
                return path
    names = ("google-chrome", "google-chrome-stable", "chrome") if resolved == "chrome" else ("microsoft-edge", "microsoft-edge-stable", "msedge")
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    raise ValueError(f"Could not find installed {resolved}; select an installed Chrome or Edge browser")


def open_sign_in_browser(profile_dir: Path, browser_channel: str) -> subprocess.Popen[bytes]:
    """Open the dedicated profile in a normal browser so Google can accept human sign-in."""
    executable = browser_executable(browser_channel)
    profile_dir = profile_dir.expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([
        str(executable), f"--user-data-dir={profile_dir}", "--new-window",
        "--no-first-run", "--no-default-browser-check",
        "https://photos.google.com/",
    ], close_fds=True)


def click_trash_confirmation(page: Any, pattern: re.Pattern[str]) -> None:
    """Click the visible popup text button, never the identically named toolbar icon."""
    # Accessible names are whitespace-normalized; has_text tests raw textContent.
    # Remove only outer anchors so a label with surrounding layout whitespace matches.
    text_source = pattern.pattern
    if text_source.startswith("^"):
        text_source = text_source[1:]
    if text_source.endswith("$") and not text_source.endswith(r"\$"):
        text_source = text_source[:-1]
    text_pattern = re.compile(text_source, pattern.flags)
    confirm = page.get_by_role("button", name=pattern).filter(
        has_text=text_pattern, visible=True
    )
    confirm.wait_for(state="visible", timeout=8_000)
    if confirm.count() != 1:
        raise RuntimeError("Expected exactly one visible text-labeled trash confirmation button")
    confirm.click(timeout=8_000)
    confirm.wait_for(state="hidden", timeout=8_000)


def is_declared_candidate(match: dict[str, Any]) -> bool:
    google = match.get("google") or {}
    immich = match.get("immich") or {}
    url = google.get("google_url")
    return (
        match.get("confidence") == "exact_hash"
        and match.get("deletion_eligible") is True
        and is_google_photo_url(url)
        and immich.get("checksum_reliable") is True
        and not immich.get("is_offline")
        and isinstance(google.get("sha1"), str)
        and google.get("sha1") == immich.get("checksum_sha1")
    )


def load_candidates(
    report_path: Path,
    *,
    selected_urls: set[str] | None = None,
    workers: int = 1,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read report: {exc}") from exc
    if report.get("schema_version") != 1 or not isinstance(report.get("matches"), list):
        raise ValueError("Unsupported or invalid report file")
    declared = [
        match for match in report["matches"]
        if is_declared_candidate(match)
        and (selected_urls is None or match["google"]["google_url"] in selected_urls)
    ]

    def verify(match: dict[str, Any]) -> dict[str, Any]:
        if should_stop and should_stop():
            raise RuntimeError("Evidence verification stopped")
        google = match["google"]
        source = Path(str(google.get("path") or ""))
        if not source.is_file():
            raise ValueError(
                f"Refusing trash run because a Takeout evidence file is missing: {source}"
            )
        current_sha1 = hash_file(source, ("sha1",), stop_requested=should_stop)["sha1"]
        if current_sha1 != google["sha1"]:
            raise ValueError(
                f"Refusing trash run because a Takeout evidence file changed since the scan: {source}"
            )
        return match

    candidates = []
    for index, match in enumerate(ordered_parallel_map(verify, declared, workers), start=1):
        candidates.append(match)
        if progress and (index == 1 or index % 100 == 0 or index == len(declared)):
            progress(f"Verified Takeout evidence {index}/{len(declared)}")
    return candidates


def trash_candidates(
    candidates: list[dict[str, Any]],
    *,
    profile_dir: Path,
    result_path: Path,
    button_pattern: str,
    confirm_pattern: str,
    browser_channel: str,
    start_at: int = 0,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
    login_ready: Callable[[], bool] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Browser support is not installed. Run: pip install -e .[browser]") from exc

    if start_at < 0 or (limit is not None and limit < 0):
        raise ValueError("start-at and limit must not be negative")
    selected = candidates[start_at : start_at + limit if limit is not None else None]
    results: list[dict[str, Any]] = []
    profile_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        launch_args: dict[str, Any] = {"headless": False, "viewport": {"width": 1280, "height": 900}}
        resolved_channel = resolve_browser_channel(browser_channel)
        if resolved_channel != "chromium":
            launch_args["channel"] = resolved_channel
        try:
            context = playwright.chromium.launch_persistent_context(str(profile_dir), **launch_args)
        except Exception as exc:
            if "Opening in existing browser session" in str(exc):
                raise RuntimeError(
                    "The dedicated sign-in Chrome/Edge window is still open. Close that entire "
                    "window, wait a few seconds, then retry the trash test. No items were changed."
                ) from exc
            raise RuntimeError(
                "Could not open the dedicated browser profile. Close its regular Chrome/Edge "
                "sign-in window before starting the trash run. "
                f"Browser error: {str(exc).splitlines()[0]}"
            ) from exc
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://photos.google.com/", wait_until="domcontentloaded")
            if login_ready is None:
                input("Verify the signed-in Google account in the browser, then press Enter here: ")
            elif not login_ready():
                if progress:
                    progress("Trash run cancelled before any item was changed.")
                return {"candidate_count": len(candidates), "attempted": 0, "results": []}
            page.goto("https://photos.google.com/", wait_until="domcontentloaded")
            if urlsplit(page.url).hostname != "photos.google.com":
                raise RuntimeError(
                    "Google Photos is still asking for sign-in. Close this browser, use the "
                    "separate Sign in button (or the auth command) in regular Chrome/Edge, "
                    "then close that window and retry. No items were changed."
                )
            trash_regex = re.compile(button_pattern, re.IGNORECASE)
            confirm_regex = re.compile(confirm_pattern, re.IGNORECASE)
            for offset, match in enumerate(selected, start=start_at + 1):
                if should_stop and should_stop():
                    if progress:
                        progress("Trash run stopped before the next item.")
                    break
                google = match["google"]
                entry = {
                    "index": offset,
                    "relative_path": google["relative_path"],
                    "url": google["google_url"],
                    "status": "failed",
                    "detail": "",
                }
                phase = "opening the Google Photos item"
                try:
                    if progress:
                        progress(f"Trashing {offset}/{len(candidates)}: {google['relative_path']}")
                    page.goto(google["google_url"], wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_timeout(1200)
                    phase = "clicking the photo toolbar trash button"
                    button = page.get_by_role("button", name=trash_regex).first
                    button.wait_for(state="visible", timeout=8_000)
                    button.click()
                    phase = "clicking the confirmation popup button"
                    click_trash_confirmation(page, confirm_regex)
                    entry["status"] = "trashed"
                    entry["detail"] = "Google Photos UI confirmed the move-to-trash action"
                except PlaywrightTimeoutError as exc:
                    entry["detail"] = f"Timed out while {phase}; no successful move was recorded"
                    entry["technical_detail"] = str(exc)
                    screenshot = result_path.with_name(f"trash-failure-{offset}.png")
                    try:
                        page.screenshot(path=str(screenshot), full_page=False)
                        entry["screenshot"] = str(screenshot)
                    except Exception as screenshot_exc:
                        entry["detail"] += f"; screenshot failed: {screenshot_exc}"
                except Exception as exc:  # Browser errors must be logged without losing completed work.
                    entry["detail"] = f"Browser action failed: {type(exc).__name__}: {exc}"
                results.append(entry)
                atomic_write_json(
                    result_path,
                    {
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "candidate_count": len(candidates),
                        "results": results,
                    },
                )
                if on_result:
                    on_result(entry)
        finally:
            context.close()
    return {"candidate_count": len(candidates), "attempted": len(results), "results": results}
