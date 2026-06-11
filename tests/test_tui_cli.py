"""Tests for the optional full-screen TUI CLI entry point."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from comix_dl.core import cli as cli_module
from comix_dl.core.cli import _build_parser


def test_parser_accepts_tui_subcommand() -> None:
    parser = _build_parser()
    args = parser.parse_args(["tui"])

    assert args.command == "tui"


def test_tui_is_not_treated_as_bare_search(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[object] = []

    class _Parser:
        def parse_args(self) -> object:
            return SimpleNamespace(command="tui", debug=False, quiet=False, mirror=None)

    monkeypatch.setattr(cli_module, "_build_parser", lambda: _Parser())
    monkeypatch.setattr(cli_module.sys, "argv", ["comix-dl", "tui"])
    monkeypatch.setattr(cli_module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli_module, "_maybe_print_update_notice", lambda: recorded.append("update"))
    monkeypatch.setattr(cli_module, "run_tui", lambda *, mirror=None: recorded.append(("tui", mirror)) or 44)

    assert cli_module.main() == 44
    assert recorded == ["update", ("tui", None)]


def test_tui_receives_mirror_override(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[object] = []

    class _Parser:
        def parse_args(self) -> object:
            return SimpleNamespace(
                command="tui",
                debug=False,
                quiet=True,
                mirror="https://comix.cc",
            )

    monkeypatch.setattr(cli_module, "_build_parser", lambda: _Parser())
    monkeypatch.setattr(cli_module.sys, "argv", ["comix-dl", "--quiet", "--mirror", "https://comix.cc", "tui"])
    monkeypatch.setattr(cli_module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli_module, "_maybe_print_update_notice", lambda: recorded.append("update"))
    monkeypatch.setattr(cli_module, "run_tui", lambda *, mirror=None: recorded.append(("tui", mirror)) or 0)

    assert cli_module.main() == 0
    assert recorded == [("tui", "https://comix.cc")]
