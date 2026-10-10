"""
Pytest configuration shared across all tests.

Includes a network guard: if any test opens a real outbound TCP
connection, the run fails.  The guard patches ``socket.socket.connect``
to raise ``ConnectionRefusedError``.  Tests that need HTTP use
``httpx.MockTransport`` (which calls the handler directly, never real
sockets) or the ``FakeChatClient``.

To temporarily disable the guard (e.g. for manual smoke testing), set
the environment variable ``DCTA_ALLOW_NETWORK=1`` before running pytest.

Also includes a dotenv guard: ``load_dotenv()`` called without an
explicit ``env_path`` will fail the test.  Tests must never read the
real ``.env`` file.
"""

from __future__ import annotations

import os
import socket

import pytest

_BLOCKED_MSG = (
    "Network access blocked during tests. "
    "Use httpx.MockTransport or FakeChatClient instead. "
    "Set DCTA_ALLOW_NETWORK=1 to allow."
)


@pytest.fixture(autouse=True, scope="session")
def block_network() -> None:
    """Block real outbound TCP connections for the entire test session."""
    if os.environ.get("DCTA_ALLOW_NETWORK"):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _blocked_connect(self, address):  # type: ignore[no-untyped-def]
        raise ConnectionRefusedError(_BLOCKED_MSG)

    def _blocked_connect_ex(self, address):  # type: ignore[no-untyped-def]
        import errno

        return errno.ECONNREFUSED

    socket.socket.connect = _blocked_connect  # type: ignore[method-assign, assignment]
    socket.socket.connect_ex = _blocked_connect_ex  # type: ignore[method-assign, assignment]


@pytest.fixture(autouse=True)
def block_load_dotenv_without_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that calls ``load_dotenv()`` without an explicit path.

    ``load_dotenv()`` with no arguments searches the current directory
    for a ``.env`` file.  In the test environment that would load the
    real ``.env`` (containing real AppKeys), which tests must never do.

    This fixture patches the ``load_dotenv`` reference in
    ``backend.llm.config`` so that calling it without an ``env_path``
    argument raises ``RuntimeError``.  Calls with an explicit
    ``env_path`` are allowed (though tests should generally use
    ``monkeypatch.setenv`` instead).
    """
    try:
        from backend.llm import config as _config
    except ImportError:
        return

    if _config.load_dotenv is None:
        return

    _real_load_dotenv = _config.load_dotenv

    def _guarded_load_dotenv(*args: object, **kwargs: object) -> None:
        # load_dotenv signature: (filename=None, ...) — the first
        # positional arg or "filename" kwarg is the env path.
        env_path = args[0] if args else kwargs.get("filename")
        if not env_path:
            raise RuntimeError(
                "load_dotenv() called without an explicit path during tests. "
                "Use monkeypatch.setenv instead, or pass env_path explicitly."
            )
        _real_load_dotenv(env_path)  # type: ignore[arg-type]

    monkeypatch.setattr(_config, "load_dotenv", _guarded_load_dotenv)
