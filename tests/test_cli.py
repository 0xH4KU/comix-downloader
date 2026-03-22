"""Tests for comix_dl.cli — argument parsing and chapter selection."""

from __future__ import annotations

import pytest

from comix_dl.cli import _build_parser, _parse_chapter_selection
from comix_dl.comix_service import ChapterInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chapters(n: int) -> list[ChapterInfo]:
    """Create a list of n test chapters."""
    return [
        ChapterInfo(title=f"Chapter {i}", chapter_id=i * 100, number=float(i))
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# _parse_chapter_selection
# ---------------------------------------------------------------------------

class TestParseChapterSelection:
    def test_all_returns_all(self):
        chapters = _make_chapters(5)
        result = _parse_chapter_selection("all", chapters)
        assert len(result) == 5

    def test_all_case_insensitive(self):
        chapters = _make_chapters(5)
        assert len(_parse_chapter_selection("ALL", chapters)) == 5
        assert len(_parse_chapter_selection("All", chapters)) == 5

    def test_single_number(self):
        chapters = _make_chapters(10)
        result = _parse_chapter_selection("3", chapters)
        assert len(result) == 1
        assert result[0].title == "Chapter 3"

    def test_range(self):
        chapters = _make_chapters(10)
        result = _parse_chapter_selection("2-5", chapters)
        assert len(result) == 4
        assert result[0].title == "Chapter 2"
        assert result[-1].title == "Chapter 5"

    def test_comma_separated(self):
        chapters = _make_chapters(10)
        result = _parse_chapter_selection("1,3,5", chapters)
        assert len(result) == 3
        assert [ch.title for ch in result] == ["Chapter 1", "Chapter 3", "Chapter 5"]

    def test_mixed_range_and_singles(self):
        chapters = _make_chapters(10)
        result = _parse_chapter_selection("1,3-5,8", chapters)
        assert len(result) == 5
        titles = [ch.title for ch in result]
        assert "Chapter 1" in titles
        assert "Chapter 3" in titles
        assert "Chapter 5" in titles
        assert "Chapter 8" in titles

    def test_out_of_bounds_ignored(self):
        chapters = _make_chapters(5)
        result = _parse_chapter_selection("0,6,100", chapters)
        assert result == []

    def test_negative_index_ignored(self):
        chapters = _make_chapters(5)
        result = _parse_chapter_selection("-1", chapters)
        assert result == []

    def test_invalid_input_returns_empty(self):
        chapters = _make_chapters(5)
        assert _parse_chapter_selection("abc", chapters) == []
        assert _parse_chapter_selection("", chapters) == []

    def test_duplicate_indices_deduplicated(self):
        chapters = _make_chapters(5)
        result = _parse_chapter_selection("1,1,1", chapters)
        assert len(result) == 1

    def test_whitespace_handling(self):
        chapters = _make_chapters(5)
        result = _parse_chapter_selection(" 1 , 3 ", chapters)
        assert len(result) == 2

    def test_range_with_spaces(self):
        chapters = _make_chapters(10)
        result = _parse_chapter_selection(" 2 - 4 ", chapters)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# _build_parser
# ---------------------------------------------------------------------------

class TestBuildParser:
    def test_search_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["search", "one piece"])
        assert args.command == "search"
        assert args.query == "one piece"

    def test_download_subcommand_defaults(self):
        parser = _build_parser()
        args = parser.parse_args(["download", "https://comix.to/manga/test"])
        assert args.command == "download"
        assert args.url == "https://comix.to/manga/test"
        assert args.chapters == "all"
        assert args.format is None
        assert args.output is None

    def test_download_with_options(self):
        parser = _build_parser()
        args = parser.parse_args([
            "download", "test-manga",
            "-c", "1-5",
            "-f", "cbz",
            "-o", "/tmp/output",
        ])
        assert args.chapters == "1-5"
        assert args.format == "cbz"
        assert args.output == "/tmp/output"

    def test_doctor_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["doctor"])
        assert args.command == "doctor"

    def test_settings_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["settings"])
        assert args.command == "settings"

    def test_no_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_version_flag(self):
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])
        assert exc_info.value.code == 0

    def test_debug_flag(self):
        parser = _build_parser()
        args = parser.parse_args(["--debug", "search", "test"])
        assert args.debug is True

    def test_format_choices_enforced(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["download", "test", "-f", "epub"])
