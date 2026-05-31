"""GitHub release update checks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from urllib.request import Request, urlopen

from comix_dl.core.fileio import atomic_write_text

_STABLE_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_LATEST_RELEASE_URL = "https://api.github.com/repos/0xH4KU/comix-downloader/releases/latest"
_DEFAULT_TIMEOUT = 2.0
_DEFAULT_MAX_AGE = timedelta(hours=24)
_UPDATE_CACHE_FILE = Path.home() / ".config" / "comix-dl" / "update-check.json"


class _Response(Protocol):
    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> object: ...

    def read(self) -> bytes: ...


class _Opener(Protocol):
    def __call__(self, url: Request, timeout: float | None = None) -> _Response: ...


@dataclass(frozen=True)
class ReleaseInfo:
    """Latest GitHub release metadata."""

    version: str
    url: str


@dataclass(frozen=True)
class UpdateInfo:
    """Available update metadata."""

    current_version: str
    latest_version: str
    release_url: str


class _Fetcher(Protocol):
    def __call__(self) -> ReleaseInfo | None: ...


@dataclass(frozen=True)
class _CachedCheck:
    release: ReleaseInfo | None


def _parse_stable_version(value: str) -> tuple[int, int, int] | None:
    match = _STABLE_VERSION_RE.match(value.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer_version(current_version: str, candidate_version: str) -> bool:
    """Return whether *candidate_version* is a newer stable version."""
    current = _parse_stable_version(current_version)
    candidate = _parse_stable_version(candidate_version)
    if current is None or candidate is None:
        return False
    return candidate > current


def _open_url(url: Request, timeout: float | None = None) -> _Response:
    return cast("_Response", urlopen(url, timeout=timeout))


def fetch_latest_release(
    *,
    opener: _Opener = _open_url,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ReleaseInfo | None:
    """Fetch latest GitHub release metadata, returning ``None`` on failure."""
    request = Request(
        _LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "comix-downloader-update-check",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    if data.get("draft") is True or data.get("prerelease") is True:
        return None

    tag_name = data.get("tag_name")
    html_url = data.get("html_url")
    if not isinstance(tag_name, str) or not isinstance(html_url, str):
        return None
    return ReleaseInfo(version=tag_name, url=html_url)


def check_for_update(
    current_version: str,
    *,
    cache_file: Path = _UPDATE_CACHE_FILE,
    now: datetime | None = None,
    max_age: timedelta = _DEFAULT_MAX_AGE,
    fetcher: _Fetcher = fetch_latest_release,
) -> UpdateInfo | None:
    """Return available update metadata, throttled by a small JSON cache."""
    checked_at = now or datetime.now(UTC)
    cached = _load_fresh_cached_check(cache_file, now=checked_at, max_age=max_age)
    if cached is None:
        release = fetcher()
        _save_cached_check(cache_file, release, checked_at)
    else:
        release = cached.release

    if release is None or not is_newer_version(current_version, release.version):
        return None
    return UpdateInfo(
        current_version=current_version,
        latest_version=release.version,
        release_url=release.url,
    )


def _load_fresh_cached_check(cache_file: Path, *, now: datetime, max_age: timedelta) -> _CachedCheck | None:
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    checked_at_raw = data.get("checked_at")
    if not isinstance(checked_at_raw, str):
        return None

    try:
        checked_at = datetime.fromisoformat(checked_at_raw)
    except ValueError:
        return None
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    if now - checked_at >= max_age:
        return None

    latest_version = data.get("latest_version")
    release_url = data.get("release_url")
    if latest_version is None and release_url is None:
        return _CachedCheck(release=None)
    if not isinstance(latest_version, str) or not isinstance(release_url, str):
        return None
    return _CachedCheck(release=ReleaseInfo(version=latest_version, url=release_url))


def _save_cached_check(cache_file: Path, release: ReleaseInfo | None, checked_at: datetime) -> None:
    payload = {
        "checked_at": checked_at.astimezone(UTC).isoformat(),
    }
    if release is not None:
        payload["latest_version"] = release.version
        payload["release_url"] = release.url
    try:
        atomic_write_text(cache_file, json.dumps(payload, indent=2) + "\n")
    except Exception:
        return
