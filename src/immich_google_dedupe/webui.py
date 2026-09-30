"""Local-only browser interface for scanning and reviewing trash candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import secrets
import threading
import uuid
import webbrowser
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .trash import is_declared_candidate, load_candidates, open_sign_in_browser, trash_candidates
from .util import atomic_write_json


class WebError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def candidate_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def parse_media_size_mib(value: Any) -> tuple[float, int]:
    try:
        mib = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise WebError("Media size must be a number of MiB") from exc
    if not mib.is_finite() or mib < 0 or mib > 100_000:
        raise WebError("Choose a media size between 0 and 100,000 MiB")
    return float(mib), int(mib * 1024 * 1024)


def takeout_folders(match: dict[str, Any]) -> tuple[str, ...]:
    google = match["google"]
    paths = [google["relative_path"], *(google.get("repeated_takeout_paths") or [])]
    folders = []
    for path in paths:
        parent = PurePosixPath(str(path).replace("\\", "/")).parent.as_posix()
        folders.append("(root)" if parent == "." else parent)
    return tuple(dict.fromkeys(folders))


class LocalApp:
    def __init__(self, args: argparse.Namespace):
        self.lock = threading.RLock()
        self.config = {
            "takeout": args.takeout,
            "immich_url": args.immich_url,
            "path_map": list(args.path_map),
            "output": str(Path(args.output).expanduser().resolve()),
            "workers": args.workers,
            "browser_channel": args.browser_channel,
        }
        self.report_path = Path(self.config["output"])
        self.report: dict[str, Any] | None = None
        self.report_digest = ""
        self.candidates: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.folder_index: dict[str, set[str]] = {}
        self.excluded_folders: set[str] = set()
        self.deselected_ids: set[str] = set()
        self.completed_ids: set[str] = set()
        self.size_filter_enabled = True
        self.minimum_media_mib = 2.0
        self.minimum_media_bytes = 2 * 1024 * 1024
        self.revision = 0
        self.events: deque[dict[str, Any]] = deque(maxlen=1500)
        self.event_id = 0
        self.job: dict[str, Any] = {"status": "idle", "kind": None}
        self.job_thread: threading.Thread | None = None
        self.login_event = threading.Event()
        self.stop_event = threading.Event()
        self.auth_process = None
        if self.report_path.is_file():
            self.load_report()

    def log(self, message: str, level: str = "info") -> None:
        with self.lock:
            self.event_id += 1
            self.events.append({
                "id": self.event_id,
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": message,
                "level": level,
            })

    def _selection_path(self) -> Path:
        return self.report_path.with_name("review-selection.json")

    def _save_selection(self) -> None:
        atomic_write_json(self._selection_path(), {
            "version": 1,
            "report_digest": self.report_digest,
            "excluded_folders": sorted(self.excluded_folders),
            "deselected_ids": sorted(self.deselected_ids),
            "size_filter": {
                "enabled": self.size_filter_enabled,
                "minimum_mib": self.minimum_media_mib,
            },
        })

    def load_report(self) -> None:
        raw = self.report_path.read_bytes()
        report = json.loads(raw)
        if report.get("schema_version") != 1 or not isinstance(report.get("matches"), list):
            raise WebError("The report has an unsupported format")
        digest = hashlib.sha256(raw).hexdigest()
        candidates = []
        by_id = {}
        folder_index: dict[str, set[str]] = defaultdict(set)
        for match in report["matches"]:
            if not is_declared_candidate(match):
                continue
            item_id = candidate_id(match["google"]["google_url"])
            record = {"id": item_id, "match": match, "folders": takeout_folders(match)}
            candidates.append(record)
            by_id[item_id] = record
            for folder in record["folders"]:
                folder_index[folder].add(item_id)

        excluded_folders: set[str] = set()
        deselected_ids: set[str] = set()
        size_filter_enabled = True
        minimum_media_mib, minimum_media_bytes = parse_media_size_mib(2)
        selection_path = self._selection_path()
        if selection_path.is_file():
            try:
                saved = json.loads(selection_path.read_text(encoding="utf-8"))
                if saved.get("report_digest") == digest:
                    excluded_folders = set(saved.get("excluded_folders") or []) & set(folder_index)
                    deselected_ids = set(saved.get("deselected_ids") or []) & set(by_id)
                    size_filter = saved.get("size_filter")
                    if isinstance(size_filter, dict):
                        minimum_media_mib, minimum_media_bytes = parse_media_size_mib(
                            size_filter.get("minimum_mib", 2)
                        )
                        size_filter_enabled = size_filter.get("enabled") is True
            except (OSError, ValueError, TypeError):
                pass

        completed_ids = set()
        result_paths = [self.report_path.with_name("trash-results.json")]
        result_paths.extend(sorted(self.report_path.parent.glob("trash-run-*.json")))
        for result_path in result_paths:
            if not result_path.is_file():
                continue
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                for entry in result.get("results", []):
                    if entry.get("status") == "trashed" and isinstance(entry.get("url"), str):
                        completed_ids.add(candidate_id(entry["url"]))
            except (OSError, ValueError, TypeError):
                continue

        with self.lock:
            self.report = report
            self.report_digest = digest
            self.candidates = candidates
            self.by_id = by_id
            self.folder_index = dict(folder_index)
            self.excluded_folders = excluded_folders
            self.deselected_ids = deselected_ids
            self.completed_ids = completed_ids & set(by_id)
            self.size_filter_enabled = size_filter_enabled
            self.minimum_media_mib = minimum_media_mib
            self.minimum_media_bytes = minimum_media_bytes
            self.revision += 1
        self.log(f"Loaded report: {len(candidates)} exact-hash candidates with Google Photos URLs.")

    def _is_size_excluded(self, record: dict[str, Any]) -> bool:
        if not self.size_filter_enabled:
            return False
        size = record["match"]["google"].get("size")
        return type(size) is not int or size <= self.minimum_media_bytes

    def _is_selected(self, record: dict[str, Any]) -> bool:
        return (
            record["id"] not in self.completed_ids
            and record["id"] not in self.deselected_ids
            and not any(folder in self.excluded_folders for folder in record["folders"])
            and not self._is_size_excluded(record)
        )

    def state(self, after: int = 0) -> dict[str, Any]:
        with self.lock:
            pending = sum(self._is_selected(record) for record in self.candidates)
            kept_by_size = sum(
                self._is_size_excluded(record)
                and record["id"] not in self.completed_ids
                and record["id"] not in self.deselected_ids
                and not any(folder in self.excluded_folders for folder in record["folders"])
                for record in self.candidates
            )
            folders = [
                {
                    "name": name,
                    "count": len(ids),
                    "excluded": name in self.excluded_folders,
                }
                for name, ids in sorted(self.folder_index.items(), key=lambda item: item[0].casefold())
            ]
            return {
                "config": dict(self.config),
                "size_filter": {
                    "enabled": self.size_filter_enabled,
                    "minimum_mib": self.minimum_media_mib,
                },
                "report": {
                    "exists": self.report is not None,
                    "path": str(self.report_path),
                    "generated_at": self.report.get("generated_at") if self.report else None,
                    "summary": self.report.get("summary", {}) if self.report else {},
                    "immich_scan": self.report.get("immich_scan", {}) if self.report else {},
                    "google_scan": self.report.get("google_scan", {}) if self.report else {},
                    "revision": self.revision,
                },
                "counts": {
                    "eligible": len(self.candidates),
                    "selected": pending,
                    "excluded": len(self.candidates) - pending - len(self.completed_ids),
                    "completed": len(self.completed_ids),
                    "kept_by_size": kept_by_size,
                },
                "folders": folders,
                "job": dict(self.job),
                "events": [event for event in self.events if event["id"] > after],
                "last_event_id": self.event_id,
            }

    def candidate_page(
        self, *, offset: int = 0, limit: int = 24, query: str = "", folder: str = "", view: str = "all"
    ) -> dict[str, Any]:
        if offset < 0 or limit < 1 or limit > 48:
            raise WebError("Invalid page size or offset")
        if view not in {"all", "selected", "excluded", "completed"}:
            raise WebError("Invalid review filter")
        needle = query.casefold().strip()
        with self.lock:
            rows = []
            for record in self.candidates:
                match = record["match"]
                google = match["google"]
                selected = self._is_selected(record)
                completed = record["id"] in self.completed_ids
                if folder and folder not in record["folders"]:
                    continue
                if needle and needle not in google["relative_path"].casefold() and needle not in str(
                    (match.get("immich") or {}).get("filename") or ""
                ).casefold():
                    continue
                if view == "selected" and not selected:
                    continue
                if view == "excluded" and (selected or completed):
                    continue
                if view == "completed" and not completed:
                    continue
                rows.append(record)
            page = []
            for record in rows[offset : offset + limit]:
                match = record["match"]
                google = match["google"]
                source = Path(google["path"])
                try:
                    preview = (
                        source.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
                        and source.is_file() and source.stat().st_size <= 25 * 1024 * 1024
                    )
                except OSError:
                    preview = False
                page.append({
                    "id": record["id"],
                    "filename": google["filename"],
                    "relative_path": google["relative_path"],
                    "folders": record["folders"],
                    "size": google["size"],
                    "media_type": google["media_type"],
                    "google_url": google["google_url"],
                    "immich_filename": (match.get("immich") or {}).get("filename"),
                    "selected": self._is_selected(record),
                    "completed": record["id"] in self.completed_ids,
                    "album_excluded": any(f in self.excluded_folders for f in record["folders"]),
                    "size_excluded": self._is_size_excluded(record),
                    "preview": preview,
                })
            return {"total": len(rows), "offset": offset, "limit": limit, "items": page}

    def set_selection(self, payload: dict[str, Any]) -> None:
        with self.lock:
            if self.job.get("status") == "running":
                raise WebError("Wait for the current job before changing selections", 409)
            if self.report is None:
                raise WebError("Build a report before reviewing candidates")
            if payload.get("type") == "folder":
                name = str(payload.get("name") or "")
                if name not in self.folder_index:
                    raise WebError("Unknown Takeout folder")
                if payload.get("excluded") is True:
                    self.excluded_folders.add(name)
                elif payload.get("excluded") is False:
                    self.excluded_folders.discard(name)
                else:
                    raise WebError("Expected excluded: true or false")
            elif payload.get("type") == "candidate":
                item_id = str(payload.get("id") or "")
                if item_id not in self.by_id:
                    raise WebError("Unknown candidate")
                if item_id in self.completed_ids:
                    raise WebError("This item was already recorded as trashed")
                if payload.get("selected") is False:
                    self.deselected_ids.add(item_id)
                elif payload.get("selected") is True:
                    self.deselected_ids.discard(item_id)
                else:
                    raise WebError("Expected selected: true or false")
            elif payload.get("type") == "size_filter":
                if type(payload.get("enabled")) is not bool:
                    raise WebError("Expected enabled: true or false")
                minimum_mib, minimum_bytes = parse_media_size_mib(payload.get("minimum_mib"))
                self.size_filter_enabled = payload["enabled"]
                self.minimum_media_mib = minimum_mib
                self.minimum_media_bytes = minimum_bytes
            else:
                raise WebError("Unknown selection action")
            self._save_selection()
            self.revision += 1

    def _begin_job(self, kind: str, **details: Any) -> None:
        if self.job.get("status") == "running":
            raise WebError("A job is already running", 409)
        self.stop_event = threading.Event()
        self.login_event = threading.Event()
        self.job = {
            "kind": kind, "status": "running", "waiting_login": False,
            "started_at": datetime.now(timezone.utc).isoformat(), **details,
        }
        self.revision += 1

    def _finish_job(self, status: str, message: str) -> None:
        with self.lock:
            self.job["status"] = status
            self.job["waiting_login"] = False
            self.job["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.revision += 1
        self.log(message, "error" if status == "failed" else "success" if status == "done" else "info")

    def start_scan(self, payload: dict[str, Any]) -> None:
        key = str(payload.get("api_key") or os.getenv("IMMICH_API_KEY") or "").strip()
        if not key:
            raise WebError("Enter an Immich API key to build the report")
        takeout = str(payload.get("takeout") or "").strip()
        immich_url = str(payload.get("immich_url") or "").strip()
        output = str(payload.get("output") or "").strip()
        path_map = str(payload.get("path_map") or "").strip()
        try:
            workers = int(payload.get("workers"))
        except (TypeError, ValueError) as exc:
            raise WebError("Workers must be a positive integer") from exc
        if workers < 1 or workers > 32:
            raise WebError("Choose between 1 and 32 workers")
        if not Path(takeout).is_dir():
            raise WebError("Takeout folder does not exist")
        if not immich_url.startswith(("http://", "https://")):
            raise WebError("Immich URL must begin with http:// or https://")
        from .immich import parse_path_maps

        if path_map:
            parse_path_maps([path_map])
        if not output:
            raise WebError("Choose a report path")
        with self.lock:
            self._begin_job("scan")
            self.config.update({
                "takeout": takeout, "immich_url": immich_url,
                "path_map": [path_map] if path_map else [],
                "output": str(Path(output).expanduser().resolve()), "workers": workers,
            })

        def work() -> None:
            self.log("Starting read-only duplication scan.")
            try:
                from .cli import create_scan_report

                def scan_progress(message: str) -> None:
                    if self.stop_event.is_set():
                        raise RuntimeError("Scan stopped")
                    self.log(message)

                scan_args = argparse.Namespace(
                    takeout=takeout, immich_url=immich_url, output=output,
                    path_map=[path_map] if path_map else [], immich_storage=None,
                    immich_api_key=key, workers=workers, time_tolerance=2,
                    timeout=30.0, insecure=False,
                )
                report, json_path, csv_path = create_scan_report(
                    scan_args, scan_progress, stop_requested=self.stop_event.is_set
                )
                with self.lock:
                    self.report_path = json_path
                self.load_report()
                self._finish_job(
                    "done", f"Scan complete: {report['summary']['exact_hash_matches']} exact matches. "
                    f"Reports: {json_path} and {csv_path}"
                )
            except Exception as exc:
                if self.stop_event.is_set():
                    self._finish_job("cancelled", "Scan stopped. No new report was written.")
                else:
                    self._finish_job("failed", f"Scan failed: {exc}")

        self.job_thread = threading.Thread(target=work, name="dedupe-scan", daemon=True)
        self.job_thread.start()

    def _wait_for_login(self) -> bool:
        with self.lock:
            self.job["waiting_login"] = True
            self.revision += 1
        self.log("Automated browser is open. Verify the already signed-in Google account, then click Continue in this UI.")
        while not self.login_event.wait(0.5):
            if self.stop_event.is_set():
                return False
        return not self.stop_event.is_set()

    def _on_trash_result(self, entry: dict[str, Any]) -> None:
        if entry["status"] == "trashed":
            with self.lock:
                self.completed_ids.add(candidate_id(entry["url"]))
                self.revision += 1
        self.log(
            f"{entry['status'].capitalize()}: {entry['relative_path']} — {entry['detail']}",
            "success" if entry["status"] == "trashed" else "error",
        )

    def start_trash(self, payload: dict[str, Any]) -> None:
        scope = payload.get("scope")
        if scope not in {"one", "all"}:
            raise WebError("Choose one item or all selected items")
        with self.lock:
            if self.report is None:
                raise WebError("Build a report before moving anything to trash")
            if self.auth_process is not None and self.auth_process.poll() is None:
                raise WebError(
                    "The separate Google sign-in browser is still open. Close its entire window, "
                    "then retry the trash test.", 409
                )
            selected = [record for record in self.candidates if self._is_selected(record)]
            if scope == "one":
                selected = selected[:1]
            if not selected:
                raise WebError("No selected candidates remain")
            phrase = f"TRASH {len(selected)} GOOGLE PHOTOS ITEMS"
            if payload.get("confirmation") != phrase:
                raise WebError(f"Type exactly: {phrase}")
            self._begin_job("trash", scope=scope, planned=len(selected))
            selected_urls = {record["match"]["google"]["google_url"] for record in selected}
            report_path = self.report_path
            report_digest = self.report_digest
            workers = int(self.config["workers"])
            browser_channel = str(self.config["browser_channel"])

        def work() -> None:
            self.log(f"Verifying {len(selected_urls)} selected Takeout evidence files before opening Chrome.")
            try:
                if hashlib.sha256(report_path.read_bytes()).hexdigest() != report_digest:
                    raise WebError("The report changed since review. Reload the UI and review it again")
                verified = load_candidates(
                    report_path, selected_urls=selected_urls, workers=workers, progress=self.log,
                    should_stop=self.stop_event.is_set,
                )
                if hashlib.sha256(report_path.read_bytes()).hexdigest() != report_digest:
                    raise WebError("The report changed during verification. Review it again")
                if len(verified) != len(selected_urls):
                    raise WebError("The report changed or a selected item failed its eligibility check")
                if self.stop_event.is_set():
                    self._finish_job("cancelled", "Trash run stopped before opening Chrome.")
                    return
                result_path = report_path.with_name(f"trash-run-{uuid.uuid4().hex}.json")
                result = trash_candidates(
                    verified,
                    profile_dir=Path.cwd() / ".gphotos-browser-profile",
                    result_path=result_path,
                    button_pattern=r"^(Move to trash|Trash)$",
                    confirm_pattern=r"^(Move to trash|Trash)$",
                    browser_channel=browser_channel,
                    progress=self.log,
                    login_ready=self._wait_for_login,
                    on_result=self._on_trash_result,
                    should_stop=self.stop_event.is_set,
                )
                failed = sum(entry["status"] != "trashed" for entry in result["results"])
                if self.stop_event.is_set():
                    status = "cancelled"
                elif failed:
                    status = "failed"
                else:
                    status = "done"
                self._finish_job(
                    status, f"Trash run finished: {result['attempted'] - failed} moved, "
                    f"{failed} failed. Log: {result_path}"
                )
            except Exception as exc:
                if self.stop_event.is_set():
                    self._finish_job("cancelled", "Trash run stopped before the next item.")
                else:
                    self._finish_job("failed", f"Trash run failed: {exc}")

        self.job_thread = threading.Thread(target=work, name="dedupe-trash", daemon=False)
        self.job_thread.start()

    def confirm_login(self) -> None:
        with self.lock:
            if self.job.get("kind") != "trash" or not self.job.get("waiting_login"):
                raise WebError("No browser sign-in is waiting", 409)
            self.job["waiting_login"] = False
            self.revision += 1
        self.login_event.set()
        self.log("Google account verification acknowledged. Starting selected items.")

    def open_sign_in(self) -> None:
        with self.lock:
            if self.job.get("status") == "running":
                raise WebError("Wait for the current job before opening the sign-in browser", 409)
            if self.auth_process is not None and self.auth_process.poll() is None:
                raise WebError("The sign-in browser is already open", 409)
            browser_channel = str(self.config["browser_channel"])
            profile_dir = Path.cwd() / ".gphotos-browser-profile"
            self.auth_process = open_sign_in_browser(profile_dir, browser_channel)
        self.log(
            "Regular Chrome/Edge opened with the dedicated profile. Sign in, verify the account, "
            "then close the entire sign-in window before starting a trash run."
        )

    def stop(self) -> None:
        with self.lock:
            if self.job.get("status") != "running":
                raise WebError("No job is active", 409)
            kind = self.job.get("kind")
        self.stop_event.set()
        self.login_event.set()
        self.log(
            "Stop requested. Hashing will stop shortly."
            if kind == "scan" else
            "Stop requested. The current browser action may finish; no next item will start."
        )


def make_handler(app: LocalApp, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ImmichDedupeLocalUI/1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _json(self, data: Any, status: int = 200) -> None:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _authorized(self, query: dict[str, list[str]] | None = None) -> bool:
            host = self.headers.get("Host", "").split(":", 1)[0]
            provided = self.headers.get("X-Session-Token", "")
            if query is not None:
                provided = query.get("token", [""])[0]
            return host in {"127.0.0.1", "localhost"} and secrets.compare_digest(provided, token)

        def _file(self, path: Path, content_type: str, *, download: bool = False) -> None:
            size = path.stat().st_size
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "private, no-store")
                if download:
                    self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
                self.end_headers()
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                host = self.headers.get("Host", "").split(":", 1)[0]
                if host not in {"127.0.0.1", "localhost"}:
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                source = Path(__file__).with_name("webui.html").read_text(encoding="utf-8")
                payload = source.replace("__APP_TOKEN_JSON__", json.dumps(token)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
                self.end_headers()
                self.wfile.write(payload)
                return
            if not self._authorized(query if parsed.path.startswith("/api/preview/") or parsed.path == "/api/download" else None):
                self._json({"error": "Unauthorized local request"}, 403)
                return
            try:
                if parsed.path == "/api/state":
                    self._json(app.state(after=int(query.get("after", ["0"])[0])))
                elif parsed.path == "/api/candidates":
                    self._json(app.candidate_page(
                        offset=int(query.get("offset", ["0"])[0]),
                        limit=int(query.get("limit", ["24"])[0]),
                        query=query.get("query", [""])[0],
                        folder=query.get("folder", [""])[0],
                        view=query.get("view", ["all"])[0],
                    ))
                elif parsed.path.startswith("/api/preview/"):
                    item_id = parsed.path.removeprefix("/api/preview/")
                    with app.lock:
                        record = app.by_id.get(item_id)
                        root = Path((app.report or {}).get("inputs", {}).get("takeout_root") or "")
                    if record is None:
                        raise WebError("Unknown image", 404)
                    source = Path(record["match"]["google"]["path"]).resolve()
                    if (
                        not source.is_file() or not source.is_relative_to(root.resolve())
                        or source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
                        or source.stat().st_size > 25 * 1024 * 1024
                    ):
                        raise WebError("Preview unavailable", 404)
                    self._file(source, mimetypes.guess_type(source.name)[0] or "image/jpeg")
                elif parsed.path == "/api/download":
                    kind = query.get("kind", [""])[0]
                    if kind not in {"json", "csv"}:
                        raise WebError("Unknown report format")
                    path = app.report_path if kind == "json" else app.report_path.with_suffix(".csv")
                    if not path.is_file():
                        raise WebError("Report file is not available", 404)
                    self._file(path, "application/json" if kind == "json" else "text/csv", download=True)
                else:
                    self._json({"error": "Not found"}, 404)
            except (ValueError, TypeError, WebError, OSError) as exc:
                self._json({"error": str(exc)}, exc.status if isinstance(exc, WebError) else 400)

        def do_POST(self) -> None:
            if not self._authorized() or "application/json" not in self.headers.get("Content-Type", ""):
                self._json({"error": "Unauthorized local request"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 128 * 1024:
                    raise WebError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise WebError("Expected a JSON object")
                path = urlsplit(self.path).path
                if path == "/api/scan":
                    app.start_scan(payload)
                elif path == "/api/selection":
                    app.set_selection(payload)
                elif path == "/api/trash":
                    app.start_trash(payload)
                elif path == "/api/login-ready":
                    app.confirm_login()
                elif path == "/api/open-sign-in":
                    app.open_sign_in()
                elif path == "/api/stop":
                    app.stop()
                else:
                    raise WebError("Not found", 404)
                self._json({"ok": True})
            except (ValueError, TypeError, WebError, OSError) as exc:
                self._json({"error": str(exc)}, exc.status if isinstance(exc, WebError) else 400)

    return Handler


def launch_web_ui(args: argparse.Namespace) -> int:
    if args.port < 0 or args.port > 65535:
        raise ValueError("Port must be between 0 and 65535")
    app = LocalApp(args)
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app, token))
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Local review UI: {url}", flush=True)
    print("Press Ctrl+C here to close the UI.", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        app.stop_event.set()
        app.login_event.set()
        if app.job_thread and app.job_thread.is_alive() and app.job.get("kind") == "trash":
            print("Waiting for the current browser action to finish...", flush=True)
            app.job_thread.join(timeout=90)
    finally:
        server.server_close()
    return 0
