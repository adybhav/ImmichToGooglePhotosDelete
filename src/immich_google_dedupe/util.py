from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar
from urllib.parse import urlparse


MEDIA_EXTENSIONS = {
    ".3gp", ".arw", ".avi", ".bmp", ".cr2", ".dng", ".gif", ".heic",
    ".heif", ".jpeg", ".jpg", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".nef", ".orf", ".png", ".raf", ".rw2",
    ".tif", ".tiff", ".webm", ".webp",
}

Input = TypeVar("Input")
Output = TypeVar("Output")


def ordered_parallel_map(
    function: Callable[[Input], Output], items: Iterable[Input], workers: int
) -> Iterator[Output]:
    """Process a bounded number of items concurrently and yield input order."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if workers == 1:
        yield from map(function, items)
        return

    source = iter(items)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending: deque[Future[Output]] = deque()

        def submit_next() -> bool:
            try:
                item = next(source)
            except StopIteration:
                return False
            pending.append(executor.submit(function, item))
            return True

        for _ in range(workers * 2):
            if not submit_next():
                break
        while pending:
            yield pending.popleft().result()
            submit_next()


def media_kind(path_or_mime: str) -> str:
    value = path_or_mime.lower()
    if value.startswith("video/") or Path(value).suffix in {
        ".3gp", ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".webm"
    }:
        return "video"
    return "image"


def hash_file(
    path: Path,
    algorithms: Iterable[str] = ("sha1", "sha256"),
    *,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, str]:
    hashers = {name: hashlib.new(name) for name in algorithms}
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            if stop_requested and stop_requested():
                raise RuntimeError("Hashing stopped")
            for hasher in hashers.values():
                hasher.update(chunk)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def is_google_photo_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname in {"photos.google.com", "photos.app.goo.gl"}


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
