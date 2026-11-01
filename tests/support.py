"""Shared test-only fixtures for isolated sessions and loopback integration.

No imports from the legacy aeon_mud_client compatibility module.
"""
from __future__ import annotations

import socket
import threading
from unittest import mock

from aeon_mud.profiles import Profile
from aeon_mud.session import SessionController
from aeon_mud.telnet import TelnetCodec


def make_profile(name="tapestries", host="tapestries.fur.com", **overrides):
    """A plaintext test profile with individually overridable settings."""
    settings = {"name": name, "host": host, "allow_plaintext": True}
    settings.update(overrides)
    return Profile(**settings)


def make_controller(profile=None):
    """Create a session with no network I/O, using mocks at its boundaries."""
    profile = profile if profile is not None else make_profile(sanity_timeout=0.0)
    transport, transcript, ui = mock.Mock(), mock.Mock(), mock.Mock()
    controller = SessionController(profile, transport, TelnetCodec(), transcript, ui, None)
    return controller, transport, transcript


class LocalServer:
    """Deterministic TCP loopback server that propagates worker failures."""

    def __init__(self, handler, count=1):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(count)
        self.listener.settimeout(3)
        self.port = self.listener.getsockname()[1]
        self.handler = handler
        self.count = count
        self.errors = []
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.listener.close()
        self.thread.join(timeout=4)
        if self.thread.is_alive():
            raise AssertionError("local server did not finish")
        if self.errors:
            raise self.errors[0]

    def serve(self):
        try:
            for index in range(self.count):
                with self.listener.accept()[0] as peer:
                    peer.settimeout(2)
                    self.handler(peer, index)
        except Exception as exc:
            self.errors.append(exc)
