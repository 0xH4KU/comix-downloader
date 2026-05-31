"""Shared fixtures for browser engine tests."""

from __future__ import annotations

import asyncio
import socket

import pytest

from comix_dl.core.config import AppConfig, BrowserConfig, DownloadConfig


def _make_config(
    *,
    browser: BrowserConfig | None = None,
    download: DownloadConfig | None = None,
) -> AppConfig:
    return AppConfig(
        browser=browser or BrowserConfig(),
        download=download or DownloadConfig(),
    )


def _can_bind_localhost() -> bool:
    """Return whether this environment allows binding localhost TCP sockets."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", 0))
        except OSError:
            return False
    return True


LOCALHOST_SKIP = pytest.mark.skipif(
    not _can_bind_localhost(),
    reason="Environment blocks binding localhost TCP sockets",
)


async def _hang(*_args: object, **_kwargs: object) -> object:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")
