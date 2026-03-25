"""Tests for the download history module."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from comix_dl.history import (
    MAX_ENTRIES,
    HistoryEntry,
    HistoryRepository,
    clear_history,
    list_history,
    record_download,
)


@pytest.fixture(autouse=True)
def _history_in_tmp(tmp_path):
    """Redirect history storage to a temp directory for each test."""
    with (
        patch("comix_dl.history._HISTORY_DIR", tmp_path),
        patch("comix_dl.history._HISTORY_FILE", tmp_path / "history.json"),
    ):
        yield


class TestRecordDownload:
    def test_record_creates_file(self, tmp_path):
        record_download("Manga A", 5, "pdf", total_size_bytes=1024)
        entries = list_history()
        assert len(entries) == 1
        assert entries[0].title == "Manga A"
        assert entries[0].chapters_count == 5
        assert entries[0].format == "pdf"
        assert entries[0].total_size_bytes == 1024

    def test_record_appends(self, tmp_path):
        record_download("Manga A", 1, "pdf")
        record_download("Manga B", 2, "cbz")
        entries = list_history()
        assert len(entries) == 2
        # list_history returns newest first
        assert entries[0].title == "Manga B"
        assert entries[1].title == "Manga A"

    def test_record_with_stats(self, tmp_path):
        record_download("Manga A", 10, "both", completed=7, partial=1, failed=1, skipped=1)
        entry = list_history()[0]
        assert entry.completed == 7
        assert entry.partial == 1
        assert entry.failed == 1
        assert entry.skipped == 1

    def test_repository_records_download(self, tmp_path):
        repository = HistoryRepository(tmp_path / "history.json")

        repository.record_download("Manga Repo", 3, "cbz")

        entries = repository.list_entries()
        assert len(entries) == 1
        assert entries[0].title == "Manga Repo"


class TestAutoTrim:
    def test_auto_trim_at_max(self, tmp_path):
        for i in range(MAX_ENTRIES + 50):
            record_download(f"Manga {i}", 1, "pdf")
        entries = list_history()
        assert len(entries) == MAX_ENTRIES
        # Newest should be present
        assert entries[0].title == f"Manga {MAX_ENTRIES + 49}"


class TestListHistory:
    def test_empty_when_no_file(self, tmp_path):
        entries = list_history()
        assert entries == []

    def test_handles_corrupt_json(self, tmp_path):
        (tmp_path / "history.json").write_text("not valid json")
        entries = list_history()
        assert entries == []

    def test_handles_wrong_type(self, tmp_path):
        (tmp_path / "history.json").write_text('"just a string"')
        entries = list_history()
        assert entries == []

    def test_skips_malformed_entries(self, tmp_path):
        data = [
            {"title": "Good", "timestamp": "2024-01-01", "chapters_count": 1, "format": "pdf"},
            {"bad_field": "only"},
        ]
        (tmp_path / "history.json").write_text(json.dumps(data))
        entries = list_history()
        assert len(entries) == 1
        assert entries[0].title == "Good"


class TestClearHistory:
    def test_clear_removes_file(self, tmp_path):
        record_download("Manga A", 1, "pdf")
        assert (tmp_path / "history.json").exists()
        clear_history()
        assert not (tmp_path / "history.json").exists()

    def test_clear_no_file_is_noop(self, tmp_path):
        # Should not raise
        clear_history()


class TestHistoryEntry:
    def test_dataclass_fields(self):
        entry = HistoryEntry(
            timestamp="2024-01-01T00:00:00",
            title="Test",
            chapters_count=5,
            format="pdf",
            total_size_bytes=2048,
            completed=4,
            partial=1,
            failed=1,
            skipped=0,
        )
        assert entry.title == "Test"
        assert entry.total_size_bytes == 2048
