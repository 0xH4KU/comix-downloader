"""Tests for comix_dl.cdp_browser — port detection utilities."""

from __future__ import annotations

import socket

import pytest

from comix_dl.cdp_browser import _find_free_port, _is_port_in_use


def _can_bind_localhost() -> bool:
    """Return whether this environment allows binding localhost TCP sockets."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", 0))
        except OSError:
            return False
    return True


pytestmark = pytest.mark.skipif(
    not _can_bind_localhost(),
    reason="Environment blocks binding localhost TCP sockets",
)


class TestFindFreePort:
    def test_returns_valid_port(self):
        port = _find_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_returned_port_is_available(self):
        port = _find_free_port()
        # We should be able to bind to the returned port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))  # should not raise

    def test_returns_different_ports(self):
        """Multiple calls should generally return different ports."""
        ports = {_find_free_port() for _ in range(5)}
        # At least 2 different ports (very unlikely to get the same 5 times)
        assert len(ports) >= 2


class TestIsPortInUse:
    def test_unused_port(self):
        # Bind to a port, get its number, close it, then check
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # Port is now released
        assert _is_port_in_use(port) is False

    def test_used_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            # Port is in use while socket is open
            assert _is_port_in_use(port) is True
