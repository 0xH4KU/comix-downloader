"""Tests for structured logging helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from comix_dl.logging_utils import StructuredFormatter, log_context


def test_log_context_normalizes_common_values() -> None:
    context = log_context(path=Path("/tmp/example"), elapsed=1.23456, status="ok")

    assert context == {
        "context": {
            "path": "/tmp/example",
            "elapsed": 1.235,
            "status": "ok",
        }
    }


def test_structured_formatter_appends_json_context() -> None:
    formatter = StructuredFormatter("%(levelname)s:%(name)s:%(message)s")
    record = logging.LogRecord(
        name="comix_dl.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="download_batch_finished",
        args=(),
        exc_info=None,
    )
    record.context = {"series": "Series A", "status": "ok", "bytes": 2048}

    formatted = formatter.format(record)

    assert formatted == (
        'INFO:comix_dl.test:download_batch_finished {"bytes": 2048, "series": "Series A", "status": "ok"}'
    )
