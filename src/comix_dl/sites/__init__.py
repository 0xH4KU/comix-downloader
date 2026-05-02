"""Site adapter registry.

The registry is the framework-side discovery point for site adapters.
A reference adapter (``comix_to``) is registered here for the current
runtime; future forks replace it with their own adapter without
touching the framework.

Wave 1 supports exactly one active adapter at a time. The registry
infrastructure is multi-adapter ready so that future extensions
(fallback adapters, mirrored sources) do not require redesign.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from comix_dl.core.errors import ConfigurationError

if TYPE_CHECKING:
    from comix_dl.sites.base import SiteAdapter


_REGISTRY: dict[str, SiteAdapter] = {}


def register(adapter: SiteAdapter) -> None:
    """Register a site adapter under its declared ``name``.

    Re-registering the same name overrides the previous entry. This is
    intentional so that tests can inject mock adapters without manual
    cleanup; production code should not rely on it.
    """
    _REGISTRY[adapter.name] = adapter


def unregister(name: str) -> None:
    """Remove an adapter from the registry. No-op if absent."""
    _REGISTRY.pop(name, None)


def get_for_url(url: str) -> SiteAdapter | None:
    """Return the first registered adapter that claims *url*.

    Returns ``None`` when no adapter recognises the URL; callers
    typically fall back to :func:`get_active` or surface a CLI error.
    """
    for adapter in _REGISTRY.values():
        if adapter.matches_url(url):
            return adapter
    return None


def get_by_name(name: str) -> SiteAdapter:
    """Return an adapter by its declared name.

    Raises:
        ConfigurationError: if no adapter is registered under *name*.
    """
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ConfigurationError(f"Unknown site adapter: {name!r}") from exc


def get_active() -> SiteAdapter:
    """Return the adapter to use for the current run.

    When exactly one adapter is registered, it is returned automatically.
    With multiple adapters, callers must select explicitly via
    :func:`get_by_name` or :func:`get_for_url`; this guard prevents
    silent ambiguity.
    """
    if not _REGISTRY:
        raise ConfigurationError(
            "No site adapter is registered. "
            "Ensure a site module (e.g. comix_dl.sites.comix_to) was imported.",
        )
    if len(_REGISTRY) > 1:
        raise ConfigurationError(
            f"Multiple site adapters registered ({sorted(_REGISTRY)}); "
            "explicit selection is required via --site or get_by_name().",
        )
    return next(iter(_REGISTRY.values()))


def all_adapters() -> tuple[SiteAdapter, ...]:
    """Return every registered adapter in registration order."""
    return tuple(_REGISTRY.values())


def clear() -> None:
    """Drop every registered adapter. Intended for tests only."""
    _REGISTRY.clear()


__all__ = [
    "all_adapters",
    "clear",
    "get_active",
    "get_by_name",
    "get_for_url",
    "register",
    "unregister",
]
