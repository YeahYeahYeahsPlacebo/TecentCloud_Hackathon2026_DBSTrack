"""
Pytest configuration shared across all tests.

Includes a network guard: if any test opens a real outbound TCP
connection, the run fails.  The guard patches ``socket.socket.connect``
to raise ``ConnectionRefusedError``.  Tests that need HTTP use
``httpx.MockTransport`` (which calls the handler directly, never real
sockets) or the ``FakeChatClient``.

To temporarily disable the guard (e.g. for manual smoke testing), set
the environment variable ``DCTA_ALLOW_NETWORK=1`` before running pytest.

Also includes a dotenv guard: tests never read the real project
``.env``, with or without a path.  Calling ``load_dotenv()`` with no
path fails the test; calling it with the project ``.env`` path — which
production code does deliberately — quietly does nothing.  The live
scripts under ``scripts/`` are the only place that reads the real file.
See :func:`block_project_dotenv`.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

#: The real ``.env``, next to this repo's root (``tests/`` is one level
#: down).  ``backend/gateway/server.py`` and ``backend/parser/factory.py``
#: compute the same path from their own location.  Resolved, so that both
#: sides of the comparison follow a symlink the same way.
PROJECT_ENV = (Path(__file__).resolve().parent.parent / ".env").resolve()

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


def _is_project_env(path: object) -> bool:
    """True if *path* points at the real project ``.env``.

    Compared after ``resolve()``, so an absolute path, a relative one and
    a symlink to the same file all count as the same file.
    """
    try:
        return Path(path).resolve() == PROJECT_ENV  # type: ignore[arg-type]
    except (OSError, RuntimeError, TypeError, ValueError):
        # An unresolvable path is certainly not the project .env.
        return False


@pytest.fixture(autouse=True)
def block_project_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real project ``.env`` out of every test, path or no path.

    Tests never read the real ``.env``.  The live scripts under
    ``scripts/`` are the only place that does — they talk to the real ADP
    applications, which is exactly what the file is for.

    Two ways in, handled differently:

    - **No path.**  ``load_dotenv()`` searches upwards from the working
      directory and would find the real file.  Nothing in this repo calls
      it that way, so a call like that is a mistake: raise and name it.

    - **An explicit path to the project ``.env``.**  This is what
      production code does on purpose — ``backend/gateway/server.py``
      and ``backend/parser/factory.py`` both pass
      ``str(_project_root() / ".env")``.  It is **not** a mistake, so it
      must not raise: ``server.py`` builds its app at import time, and
      raising would break every test that imports it.  Instead the call
      becomes a no-op returning ``False`` — the same answer dotenv gives
      when there is no file — so ``os.environ`` is left exactly as the
      test set it up.

      Without this, a test that deletes the LLM variables with
      ``monkeypatch.delenv`` and then calls ``build_api()`` gets them
      straight back from the file, and "no keys configured" can only be
      tested on a machine that happens to have no ``.env``.

    Any other explicit path still loads normally, so a test can write its
    own ``.env`` under ``tmp_path`` and read it back.

    The patch is applied to the ``load_dotenv`` reference in
    ``backend.llm.config``, which is the only module that imports it.
    """
    try:
        from backend.llm import config as _config
    except ImportError:
        return

    if _config.load_dotenv is None:
        return

    _real_load_dotenv = _config.load_dotenv

    def _guarded_load_dotenv(*args: object, **kwargs: object) -> bool:
        # load_dotenv(dotenv_path=None, ...) — the first positional arg is
        # the path.  "filename" was its name in older python-dotenv.
        if args:
            env_path = args[0]
        else:
            env_path = kwargs.get("dotenv_path") or kwargs.get("filename")

        if not env_path:
            raise RuntimeError(
                "load_dotenv() called without an explicit path during tests. "
                "Use monkeypatch.setenv instead, or pass env_path explicitly."
            )

        if _is_project_env(env_path):
            # Silently do nothing, and say so the way dotenv says it.
            return False

        return bool(_real_load_dotenv(*args, **kwargs))  # type: ignore[arg-type]

    monkeypatch.setattr(_config, "load_dotenv", _guarded_load_dotenv)
