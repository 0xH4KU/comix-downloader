"""Structured logging helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path


def _normalize_value(value: object) -> object:
    """Normalize common values into JSON-safe logging fields."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, tuple):
        return [_normalize_value(item) for item in value]
    return value


def log_context(**fields: object) -> dict[str, dict[str, object]]:
    """Build a normalized structured context payload for ``logging.extra``."""
    context = {
        key: _normalize_value(value)
        for key, value in fields.items()
        if value is not None
    }
    return {"context": context}


class StructuredFormatter(logging.Formatter):
    """Formatter that appends JSON context when available."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        context = getattr(record, "context", None)
        if not isinstance(context, dict) or not context:
            return message
        return f"{message} {json.dumps(context, ensure_ascii=False, sort_keys=True)}"


def configure_logging(level: int) -> None:
    """Install a structured formatter on the root logger."""
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter("%(levelname)s:%(name)s:%(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)
