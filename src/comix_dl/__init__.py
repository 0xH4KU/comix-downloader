"""comix-downloader — a focused comix.to manga downloader."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("comix-downloader")
except PackageNotFoundError:
    # Fallback for editable installs or running from source
    __version__ = "0.3.5"
