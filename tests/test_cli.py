"""Tests for comix_dl.cli — argument parsing and chapter selection."""

from __future__ import annotations

import logging
import signal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from comix_dl import cli as cli_module
from comix_dl.cli import _build_parser, _parse_chapter_selection
from comix_dl.comix_service import ChapterInfo
from comix_dl.settings import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chapters(n: int) -> list[ChapterInfo]:
    """Create a list of n test chapters."""
    return [
        ChapterInfo(title=f"Chapter {i}", chapter_id=i * 100, number=str(i))
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

    # -- New subcommands & flags ------------------------------------------------

    def test_quiet_flag(self):
        parser = _build_parser()
        args = parser.parse_args(["-q", "search", "test"])
        assert args.quiet is True

    def test_quiet_flag_long(self):
        parser = _build_parser()
        args = parser.parse_args(["--quiet", "doctor"])
        assert args.quiet is True

    def test_info_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["info", "https://comix.to/manga/test"])
        assert args.command == "info"
        assert args.url == "https://comix.to/manga/test"

    def test_list_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["list"])
        assert args.command == "list"

    def test_clean_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["clean"])
        assert args.command == "clean"
        assert args.force is False

    def test_clean_with_force(self):
        parser = _build_parser()
        args = parser.parse_args(["clean", "--force"])
        assert args.force is True

    def test_history_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["history"])
        assert args.command == "history"
        assert args.action is None

    def test_history_clear(self):
        parser = _build_parser()
        args = parser.parse_args(["history", "clear"])
        assert args.command == "history"
        assert args.action == "clear"

    def test_download_no_optimize(self):
        parser = _build_parser()
        args = parser.parse_args(["download", "test", "--no-optimize"])
        assert args.no_optimize is True

    def test_download_default_optimize(self):
        parser = _build_parser()
        args = parser.parse_args(["download", "test"])
        assert args.no_optimize is False


def test_main_treats_bare_positional_arg_as_search(monkeypatch: pytest.MonkeyPatch):
    recorded: dict[str, object] = {}

    def run_async(coro: object) -> int:
        recorded["coro"] = coro
        return 17

    monkeypatch.setattr(cli_module, "_build_parser", lambda: SimpleNamespace(parse_args=lambda: None))
    monkeypatch.setattr(cli_module, "configure_logging", lambda level: recorded.setdefault("level", level))
    monkeypatch.setattr(cli_module, "flow_search", lambda query: ("search", query))
    monkeypatch.setattr(cli_module, "_run_async", run_async)
    monkeypatch.setattr(cli_module.sys, "argv", ["comix-dl", "one-piece"])

    assert cli_module.main() == 17
    assert recorded["level"] == logging.INFO
    assert recorded["coro"] == ("search", "one-piece")


def test_main_dispatches_subcommands_and_menu(monkeypatch: pytest.MonkeyPatch):
    recorded: list[object] = []
    args_queue = [
        SimpleNamespace(command="search", query="naruto", debug=False, quiet=False),
        SimpleNamespace(
            command="download",
            url="series-a",
            chapters="1-2",
            format="pdf",
            output="/tmp/out",
            no_optimize=True,
            debug=True,
            quiet=True,
        ),
        SimpleNamespace(command="info", url="series-a", debug=False, quiet=False),
        SimpleNamespace(command="list", debug=False, quiet=False),
        SimpleNamespace(command="clean", force=True, debug=False, quiet=True),
        SimpleNamespace(command="history", action="clear", debug=False, quiet=False),
        SimpleNamespace(command="doctor", debug=False, quiet=False),
        SimpleNamespace(command="settings", debug=False, quiet=False),
        SimpleNamespace(command=None, debug=False, quiet=False),
    ]

    class _Parser:
        def parse_args(self) -> object:
            return args_queue.pop(0)

    monkeypatch.setattr(cli_module, "_build_parser", lambda: _Parser())
    monkeypatch.setattr(cli_module.sys, "argv", ["comix-dl"])
    monkeypatch.setattr(cli_module, "configure_logging", lambda level: recorded.append(("log", level)))
    monkeypatch.setattr(cli_module, "flow_search", lambda query, quiet=False: ("search", query, quiet))
    monkeypatch.setattr(
        cli_module,
        "flow_noninteractive_download",
        lambda url, chapters, fmt, output, optimize=None, quiet=False: (
            "download",
            url,
            chapters,
            fmt,
            output,
            optimize,
            quiet,
        ),
    )
    monkeypatch.setattr(cli_module, "flow_info", lambda url: ("info", url))
    monkeypatch.setattr(cli_module, "_run_async", lambda coro: recorded.append(("async", coro)) or 10)
    monkeypatch.setattr(cli_module, "flow_list", lambda: 21)
    monkeypatch.setattr(
        cli_module,
        "flow_clean",
        lambda *, force=False, auto_confirm=False: ("clean", force, auto_confirm),
    )
    monkeypatch.setattr(cli_module, "flow_history", lambda action=None: ("history", action))
    monkeypatch.setattr(cli_module, "run_doctor", lambda: 24)
    monkeypatch.setattr(cli_module, "flow_settings", lambda: recorded.append(("settings",)))
    monkeypatch.setattr(cli_module, "_main_menu", lambda: 25)
    monkeypatch.setattr(cli_module.console, "quiet", False)

    assert cli_module.main() == 10
    assert cli_module.main() == 10
    assert cli_module.console.quiet is True
    assert cli_module.main() == 10
    assert cli_module.main() == 21
    assert cli_module.main() == ("clean", True, True)
    assert cli_module.main() == ("history", "clear")
    assert cli_module.main() == 24
    assert cli_module.main() == 0
    assert cli_module.main() == 25

    assert ("log", logging.DEBUG) in recorded
    assert ("async", ("search", "naruto", False)) in recorded
    assert ("async", ("download", "series-a", "1-2", "pdf", "/tmp/out", False, True)) in recorded
    assert ("async", ("info", "series-a")) in recorded
    assert ("settings",) in recorded


def test_run_async_sets_shutdown_flag_on_sigint_and_restores_signal(monkeypatch: pytest.MonkeyPatch):
    printed: list[str] = []
    signal_calls: list[tuple[signal.Signals, object]] = []

    class _Loop:
        def __init__(self) -> None:
            self.closed = False

        def run_until_complete(self, coro: object) -> int:
            del coro
            handler = signal_calls[0][1]
            assert callable(handler)
            handler()
            return 12

        def close(self) -> None:
            self.closed = True

    loop = _Loop()

    monkeypatch.setattr(cli_module.asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(
        cli_module.signal,
        "signal",
        lambda sig, handler: signal_calls.append((sig, handler)),
    )
    monkeypatch.setattr(cli_module.console, "print", lambda message: printed.append(str(message)))

    assert cli_module._run_async(object()) == 12
    assert cli_module._shutdown_requested is True
    assert loop.closed is True
    assert signal_calls[0][0] == signal.SIGINT
    assert signal_calls[-1] == (signal.SIGINT, signal.SIG_DFL)
    assert "Ctrl+C" in printed[0]


def test_run_async_returns_130_on_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch):
    printed: list[str] = []

    class _Loop:
        def __init__(self) -> None:
            self.closed = False

        def run_until_complete(self, coro: object) -> int:
            del coro
            raise KeyboardInterrupt

        def close(self) -> None:
            self.closed = True

    loop = _Loop()

    monkeypatch.setattr(cli_module.asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(cli_module.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module.console, "print", lambda message: printed.append(str(message)))

    assert cli_module._run_async(object()) == 130
    assert loop.closed is True
    assert "Interrupted." in printed[0]


def test_main_returns_130_on_keyboard_interrupt_from_main_menu(monkeypatch: pytest.MonkeyPatch):
    printed: list[str] = []

    monkeypatch.setattr(
        cli_module,
        "_build_parser",
        lambda: SimpleNamespace(parse_args=lambda: SimpleNamespace(command=None, debug=False, quiet=False)),
    )
    monkeypatch.setattr(cli_module.sys, "argv", ["comix-dl"])
    monkeypatch.setattr(cli_module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli_module, "_main_menu", MagicMock(side_effect=KeyboardInterrupt))
    monkeypatch.setattr(cli_module.console, "print", lambda message: printed.append(str(message)))

    assert cli_module.main() == 130
    assert "Interrupted." in printed[0]


def test_shutdown_loop_runs_async_cleanup_hooks() -> None:
    class _Loop:
        def __init__(self) -> None:
            self.closed = False
            self.shutdown_asyncgens_called = False
            self.shutdown_default_executor_called = False

        def run_until_complete(self, coro: object) -> object:
            import asyncio

            return asyncio.run(coro)  # type: ignore[arg-type]

        async def shutdown_asyncgens(self) -> None:
            self.shutdown_asyncgens_called = True

        async def shutdown_default_executor(self) -> None:
            self.shutdown_default_executor_called = True

        def close(self) -> None:
            self.closed = True

    loop = _Loop()

    cli_module._shutdown_loop(loop)  # type: ignore[arg-type]

    assert loop.shutdown_asyncgens_called is True
    assert loop.shutdown_default_executor_called is True
    assert loop.closed is True


def test_main_menu_invokes_each_action_then_exits(monkeypatch: pytest.MonkeyPatch):
    calls: list[object] = []

    monkeypatch.setattr(
        cli_module,
        "SettingsRepository",
        lambda: SimpleNamespace(load=lambda: Settings(output_dir="/tmp/out", default_format="pdf")),
    )
    monkeypatch.setattr(
        cli_module.Prompt,
        "ask",
        MagicMock(
            side_effect=[
                "1",
                "  search me  ",
                "2",
                "  series-a  ",
                "3",
                "4",
                "5",
                "6",
                "q",
            ]
        ),
    )
    monkeypatch.setattr(cli_module, "flow_search", lambda query: ("search", query))
    monkeypatch.setattr(cli_module, "flow_url_download", lambda url: ("url", url))
    monkeypatch.setattr(cli_module, "_run_async", lambda coro: calls.append(("async", coro)) or 0)
    monkeypatch.setattr(cli_module, "flow_list", lambda: calls.append(("list",)))
    monkeypatch.setattr(cli_module, "flow_history", lambda: calls.append(("history",)))
    monkeypatch.setattr(cli_module, "flow_settings", lambda: calls.append(("settings",)))
    monkeypatch.setattr(cli_module, "run_doctor", lambda: calls.append(("doctor",)))

    assert cli_module._main_menu() == 0
    assert ("async", ("search", "search me")) in calls
    assert ("async", ("url", "series-a")) in calls
    assert ("list",) in calls
    assert ("history",) in calls
    assert ("settings",) in calls
    assert ("doctor",) in calls
