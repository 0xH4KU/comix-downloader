"""Tests for comix_dl.cli.display helpers."""

from __future__ import annotations

from comix_dl.cli.display import console, print_dedup_report
from comix_dl.comix_service import DedupDecision


class TestDedupDisplay:
    def test_print_dedup_report_includes_reason_kept_and_dropped(self):
        decisions = [
            DedupDecision(
                chapter_number="5",
                reason="same-language duplicate; kept the variant with the highest page count",
                kept=("Chapter 5 [en, 25p, id=200]",),
                dropped=("Chapter 5 [en, 10p, id=100]",),
            )
        ]

        with console.capture() as capture:
            print_dedup_report(decisions)

        output = capture.get()
        assert "Dedup decisions" in output
        assert "highest page count" in output
        assert "id=200" in output
        assert "id=100" in output
