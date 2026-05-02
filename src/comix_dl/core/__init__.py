"""Framework core — site-agnostic infrastructure.

Modules under :mod:`comix_dl.core` implement the parts of the project
that should remain valid across forks: CLI plumbing, application use
cases, the download / convert / persistence layers, the structured
logging helpers, and the engine boundary that talks to a remote site.

Site-specific behaviour (URL parsing, API schemas, request signing,
deduplication rules) lives under :mod:`comix_dl.sites` instead. When
the reference site is replaced, only that subpackage and a handful of
metadata files (``pyproject.toml``, README, install scripts) need to
move; the modules under this package are intended to be reused as-is.
"""
