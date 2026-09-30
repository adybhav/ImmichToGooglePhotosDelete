from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .immich import ImmichClient, build_assets, normalize_api_url, parse_path_maps
from .matching import match_items
from .report import build_report, write_report
from .takeout import scan_takeout
from .trash import load_candidates, open_sign_in_browser, trash_candidates


def note(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def positive_workers(value: str) -> int:
    try:
        workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers must be a positive integer") from exc
    if workers < 1:
        raise argparse.ArgumentTypeError("workers must be a positive integer")
    return workers


def _api_key(value: str | None) -> str:
    key = value or os.getenv("IMMICH_API_KEY")
    if key:
        return key
    if sys.stdin.isatty():
        return getpass.getpass("Immich API key: ")
    raise ValueError("Provide --immich-api-key or set IMMICH_API_KEY")


def create_scan_report(
    args: argparse.Namespace,
    progress: Callable[[str], None] = note,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    takeout = Path(args.takeout).expanduser().resolve()
    report_path = Path(args.output).expanduser().resolve()
    path_maps = parse_path_maps(args.path_map)
    storage = Path(args.immich_storage).expanduser().resolve() if args.immich_storage else None
    if storage and not storage.is_dir():
        raise ValueError(f"Immich storage directory does not exist: {storage}")
    client = ImmichClient(
        args.immich_url,
        _api_key(args.immich_api_key),
        verify_ssl=not args.insecure,
        timeout=args.timeout,
    )
    progress("Connecting to Immich...")
    version = client.server_version()
    progress(f"Connected to Immich {version}; fetching assets visible to this API key...")
    raw_assets = client.list_assets(progress)
    progress(f"Checking Immich originals with {args.workers} workers...")
    assets, immich_stats = build_assets(
        raw_assets, storage_root=storage, path_maps=path_maps, progress=progress,
        workers=args.workers, stop_requested=stop_requested,
    )
    progress(f"Scanning and hashing Google Takeout media with {args.workers} workers (read-only)...")
    google_items, google_stats = scan_takeout(
        takeout, progress, workers=args.workers, stop_requested=stop_requested
    )
    progress("Comparing content hashes and metadata...")
    matches = match_items(google_items, assets, time_tolerance_seconds=args.time_tolerance)
    report = build_report(
        matches,
        takeout_root=str(takeout),
        immich_url=normalize_api_url(args.immich_url),
        immich_version=version,
        google_stats=google_stats,
        immich_stats=immich_stats,
    )
    progress("Writing JSON and CSV reports...")
    json_file, csv_file = write_report(report, report_path)
    if immich_stats["unresolved_external_assets"]:
        progress(
            "WARNING: Some external-library assets have non-content checksums. Add --immich-storage "
            "or --path-map to prove those matches byte-for-byte."
        )
    return report, json_file, csv_file


def run_scan(args: argparse.Namespace) -> int:
    report, json_file, csv_file = create_scan_report(args)
    summary = report["summary"]
    print(json.dumps(summary, indent=2))
    print(f"\nJSON report: {json_file}\nCSV review sheet: {csv_file}")
    return 0


def run_summary(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    print(json.dumps(report.get("summary", {}), indent=2))
    return 0


def run_web(args: argparse.Namespace) -> int:
    from .webui import launch_web_ui

    return launch_web_ui(args)


def run_auth(args: argparse.Namespace) -> int:
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    open_sign_in_browser(profile_dir, args.browser_channel)
    print(
        "Regular Chrome/Edge opened with the app's dedicated profile. Sign in to Google Photos "
        "and verify the intended account, then close that browser window before a trash run."
    )
    print(f"Profile: {profile_dir}")
    return 0


def run_trash(args: argparse.Namespace) -> int:
    report_path = Path(args.report).expanduser().resolve()
    selection_path = report_path.with_name("review-selection.json")
    if args.execute and selection_path.is_file() and not args.ignore_review_selection:
        try:
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
            current_digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot check the saved review selection: {exc}") from exc
        if not isinstance(selection, dict):
            raise ValueError("Saved review selection has an invalid format")
        size_filter = selection.get("size_filter", {"enabled": True})
        if not isinstance(size_filter, dict):
            raise ValueError("Saved media size filter has an invalid format")
        if selection.get("report_digest") == current_digest and (
            selection.get("excluded_folders") or selection.get("deselected_ids")
            or size_filter.get("enabled") is True
        ):
            raise ValueError(
                "This report has saved UI exclusions or a media size filter. Run trash from the web UI, or explicitly "
                "add --ignore-review-selection to bypass them."
            )
    candidates = load_candidates(report_path, workers=args.workers, progress=note)
    if args.limit is not None:
        candidate_attempts = min(args.limit, max(0, len(candidates) - args.start_at))
    else:
        candidate_attempts = max(0, len(candidates) - args.start_at)
    print(
        f"Report contains {len(candidates)} exact-hash, URL-backed trash candidates; "
        f"this run would attempt {candidate_attempts}."
    )
    if not args.execute:
        print("Dry run only. No browser opened and nothing was trashed. Add --execute to proceed.")
        return 0
    phrase = f"TRASH {candidate_attempts} GOOGLE PHOTOS ITEMS"
    entered = input(f"Type exactly {phrase!r} to continue: ")
    if entered != phrase:
        raise ValueError("Confirmation did not match; nothing was changed")
    result_path = Path(args.result).expanduser().resolve()
    result = trash_candidates(
        candidates,
        profile_dir=Path(args.profile_dir).expanduser().resolve(),
        result_path=result_path,
        button_pattern=args.trash_button_pattern,
        confirm_pattern=args.confirm_button_pattern,
        browser_channel=args.browser_channel,
        start_at=args.start_at,
        limit=args.limit,
        progress=note,
    )
    trashed = sum(1 for item in result["results"] if item["status"] == "trashed")
    failed = result["attempted"] - trashed
    print(f"Trashed: {trashed}; failed/skipped: {failed}; log: {result_path}")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="immich-gphotos-dedupe",
        description="Audit Google Photos Takeout media against Immich, then optionally trash proven duplicates.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Read, hash, compare, and write JSON/CSV reports; deletes nothing")
    scan.add_argument("--takeout", required=True, help="Extracted Google Takeout directory")
    scan.add_argument("--immich-url", required=True, help="Immich URL, with or without /api")
    scan.add_argument("--immich-api-key", help="Immich user API key (prefer IMMICH_API_KEY env var)")
    scan.add_argument(
        "--immich-storage",
        help="Local directory corresponding to Immich's /usr/src/app/upload (needed for external libraries)",
    )
    scan.add_argument(
        "--path-map",
        action="append",
        default=[],
        metavar="REMOTE=LOCAL",
        help="Map an Immich/container path prefix to local storage; repeatable",
    )
    scan.add_argument("--output", default="reports/duplication-report.json", help="JSON report path")
    scan.add_argument("--time-tolerance", type=int, default=2, help="Metadata timestamp tolerance in seconds")
    scan.add_argument(
        "--workers", type=positive_workers, default=4,
        help="Concurrent file hash workers (default: 4; use 1 for sequential hashing)",
    )
    scan.add_argument("--timeout", type=float, default=30.0, help="Immich HTTP timeout in seconds")
    scan.add_argument("--insecure", action="store_true", help="Disable TLS verification for a trusted local Immich")
    scan.set_defaults(func=run_scan)

    summary = sub.add_parser("summary", help="Print the summary from a prior JSON report")
    summary.add_argument("--report", required=True)
    summary.set_defaults(func=run_summary)

    web = sub.add_parser("web", help="Open a local browser UI for scanning, review, and guarded trash runs")
    web.add_argument("--takeout", help="Pre-fill the extracted Google Photos Takeout folder")
    web.add_argument("--immich-url", help="Pre-fill the Immich URL")
    web.add_argument("--path-map", action="append", help="Pre-fill a container-to-local path mapping; repeatable")
    web.add_argument("--output", help="Report path (default: reports/duplication-report.json)")
    web.add_argument("--workers", type=positive_workers, help="Concurrent hash workers (default: 4)")
    web.add_argument("--browser-channel", choices=("auto", "chrome", "msedge", "chromium"),
                     help="Browser for Google sign-in and trash (default: auto)")
    web.add_argument("--port", type=int, default=8765, help="Localhost port (default: 8765)")
    web.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser")
    web.set_defaults(func=run_web)

    auth = sub.add_parser("auth", help="Sign in to Google Photos in a regular browser before trashing")
    auth.add_argument("--profile-dir", default=".gphotos-browser-profile")
    auth.add_argument(
        "--browser-channel", choices=("auto", "chrome", "msedge"), default="auto",
        help="Installed Chrome or Edge to use for human sign-in",
    )
    auth.set_defaults(func=run_auth)

    trash = sub.add_parser(
        "trash", help="Move exact-hash matches to Google Photos trash through a visible signed-in browser"
    )
    trash.add_argument("--report", required=True)
    trash.add_argument("--execute", action="store_true", help="Actually operate the browser; default is dry-run")
    trash.add_argument(
        "--ignore-review-selection", action="store_true",
        help="Allow CLI execution despite saved web UI exclusions (bypasses them)",
    )
    trash.add_argument("--start-at", type=int, default=0, help="Zero-based candidate offset for resuming")
    trash.add_argument("--limit", type=int, help="Maximum items to attempt (recommended for the first run)")
    trash.add_argument(
        "--workers", type=positive_workers, default=4,
        help="Concurrent Takeout evidence hash workers before the browser opens (default: 4)",
    )
    trash.add_argument("--profile-dir", default=".gphotos-browser-profile")
    trash.add_argument("--result", default="reports/trash-results.json")
    trash.add_argument(
        "--browser-channel", choices=("auto", "chrome", "msedge", "chromium"), default="auto",
        help="Auto-detect Chrome/Edge; chromium requires a Playwright browser install",
    )
    trash.add_argument("--trash-button-pattern", default=r"^(Move to trash|Trash)$")
    trash.add_argument("--confirm-button-pattern", default=r"^(Move to trash|Trash)$")
    trash.set_defaults(func=run_trash)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args))
    except KeyboardInterrupt:
        note("Cancelled.")
        return 130
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        note(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
