"""Tests for GitHub release update checks."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.error import URLError

from comix_dl.core import update_check

if TYPE_CHECKING:
    from pathlib import Path


def test_update_check_module_exists() -> None:
    assert importlib.util.find_spec("comix_dl.core.update_check") is not None


def test_is_newer_version_ignores_v_prefix() -> None:
    assert update_check.is_newer_version("0.4.1", "v0.4.2") is True


def test_is_newer_version_rejects_same_or_older_versions() -> None:
    assert update_check.is_newer_version("0.4.1", "v0.4.1") is False
    assert update_check.is_newer_version("0.4.1", "v0.4.0") is False


def test_is_newer_version_rejects_prerelease_as_update_for_stable() -> None:
    assert update_check.is_newer_version("0.4.1", "v0.4.2rc1") is False


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_fetch_latest_release_returns_release_metadata() -> None:
    requests: list[object] = []

    def opener(url: object, timeout: float | None = None) -> _Response:
        requests.append((url, timeout))
        return _Response({
            "tag_name": "v0.4.2",
            "html_url": "https://github.com/0xH4KU/comix-downloader/releases/tag/v0.4.2",
            "draft": False,
            "prerelease": False,
        })

    release = update_check.fetch_latest_release(opener=opener, timeout=1.5)

    assert release is not None
    assert release.version == "v0.4.2"
    assert release.url.endswith("/v0.4.2")
    assert requests


def test_fetch_latest_release_returns_none_on_network_error() -> None:
    def opener(_url: object, _timeout: float | None = None) -> _Response:
        raise URLError("offline")

    assert update_check.fetch_latest_release(opener=opener) is None


def test_check_for_update_uses_fresh_cache_without_network(tmp_path: Path) -> None:
    cache_file = tmp_path / "update-check.json"
    cache_file.write_text(json.dumps({
        "checked_at": "2026-05-31T00:00:00+00:00",
        "latest_version": "v0.4.2",
        "release_url": "https://github.com/0xH4KU/comix-downloader/releases/tag/v0.4.2",
    }))

    def fetcher() -> update_check.ReleaseInfo | None:
        raise AssertionError("fresh cache should skip network")

    update = update_check.check_for_update(
        current_version="0.4.1",
        cache_file=cache_file,
        now=datetime(2026, 5, 31, 1, tzinfo=UTC),
        fetcher=fetcher,
    )

    assert update is not None
    assert update.latest_version == "v0.4.2"
    assert update.current_version == "0.4.1"
    assert update.release_url.endswith("/v0.4.2")


def test_check_for_update_refreshes_expired_cache(tmp_path: Path) -> None:
    cache_file = tmp_path / "update-check.json"
    cache_file.write_text(json.dumps({
        "checked_at": "2026-05-30T00:00:00+00:00",
        "latest_version": "v0.4.1",
        "release_url": "https://github.com/0xH4KU/comix-downloader/releases/tag/v0.4.1",
    }))
    calls = 0

    def fetcher() -> update_check.ReleaseInfo | None:
        nonlocal calls
        calls += 1
        return update_check.ReleaseInfo(
            version="v0.4.2",
            url="https://github.com/0xH4KU/comix-downloader/releases/tag/v0.4.2",
        )

    update = update_check.check_for_update(
        current_version="0.4.1",
        cache_file=cache_file,
        now=datetime(2026, 5, 31, 1, tzinfo=UTC),
        max_age=timedelta(hours=24),
        fetcher=fetcher,
    )

    assert calls == 1
    assert update is not None
    assert update.latest_version == "v0.4.2"


def test_check_for_update_caches_same_version_response(tmp_path: Path) -> None:
    cache_file = tmp_path / "update-check.json"

    def fetcher() -> update_check.ReleaseInfo | None:
        return update_check.ReleaseInfo(
            version="v0.4.1",
            url="https://github.com/0xH4KU/comix-downloader/releases/tag/v0.4.1",
        )

    update = update_check.check_for_update(
        current_version="0.4.1",
        cache_file=cache_file,
        now=datetime(2026, 5, 31, 1, tzinfo=UTC),
        fetcher=fetcher,
    )

    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    assert update is None
    assert cached["latest_version"] == "v0.4.1"


def test_check_for_update_caches_failed_response(tmp_path: Path) -> None:
    cache_file = tmp_path / "update-check.json"

    update = update_check.check_for_update(
        current_version="0.4.1",
        cache_file=cache_file,
        now=datetime(2026, 5, 31, 1, tzinfo=UTC),
        fetcher=lambda: None,
    )

    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    assert update is None
    assert cached["checked_at"] == "2026-05-31T01:00:00+00:00"
    assert "latest_version" not in cached


def test_check_for_update_uses_fresh_empty_cache_without_network(tmp_path: Path) -> None:
    cache_file = tmp_path / "update-check.json"
    cache_file.write_text(json.dumps({"checked_at": "2026-05-31T00:00:00+00:00"}))

    def fetcher() -> update_check.ReleaseInfo | None:
        raise AssertionError("fresh empty cache should skip network")

    update = update_check.check_for_update(
        current_version="0.4.1",
        cache_file=cache_file,
        now=datetime(2026, 5, 31, 1, tzinfo=UTC),
        fetcher=fetcher,
    )

    assert update is None
