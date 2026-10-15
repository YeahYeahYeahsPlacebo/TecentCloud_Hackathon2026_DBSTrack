"""
The dotenv guard in conftest.py.

Tests never read the real project ``.env``, with or without a path.  The
live scripts under ``scripts/`` are the only place that reads it.

The guard is what makes "no keys are configured" testable at all: without
it, ``build_api()`` passes the project ``.env`` path to ``load_dotenv``
explicitly (``backend/gateway/server.py``), which puts back any variable
a test just deleted.  That bug is machine-dependent — it only shows up
for a developer who has a ``.env`` — so it is worth pinning down here.

Nothing in this file reads, writes or prints the real file's contents: it
asserts on **whether a variable is set**, never on a value.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conftest import PROJECT_ENV, _is_project_env  # noqa: E402

pytest.importorskip("dotenv")

from backend.llm import config as llm_config  # noqa: E402

# A variable that exists in the real .env.  Only ever checked for
# presence, never for its value.
A_REAL_ENV_VAR = "PARSER_APPKEY"

SENTINEL = "DCTA_DOTENV_GUARD_SENTINEL"


def _loader():
    """The guarded ``load_dotenv`` the production code would call."""
    return llm_config.load_dotenv


# ── The project .env is inert, even when asked for by name ────────────


def test_loading_the_project_env_explicitly_changes_nothing(monkeypatch):
    """The exact call production makes (server.py passes this path) must
    leave os.environ as the test arranged it."""
    monkeypatch.setenv(SENTINEL, "untouched")
    monkeypatch.delenv(A_REAL_ENV_VAR, raising=False)
    before = dict(os.environ)

    result = _loader()(str(PROJECT_ENV))

    # The deleted variable stays deleted: nothing came back from the file.
    assert A_REAL_ENV_VAR not in os.environ
    assert os.environ.get(SENTINEL) == "untouched"
    assert dict(os.environ) == before
    # dotenv's own answer for "nothing was loaded".
    assert result is False


@pytest.mark.skipif(not PROJECT_ENV.exists(), reason="no .env on this machine")
def test_the_guard_matters_only_because_the_file_is_really_there(monkeypatch):
    """Guards against a false pass: if the real file were missing, the
    test above would pass for the wrong reason."""
    monkeypatch.delenv(A_REAL_ENV_VAR, raising=False)
    _loader()(str(PROJECT_ENV))
    assert A_REAL_ENV_VAR not in os.environ


@pytest.mark.parametrize("as_path_object", [False, True])
def test_the_path_may_be_a_string_or_a_Path(monkeypatch, as_path_object):
    monkeypatch.delenv(A_REAL_ENV_VAR, raising=False)
    _loader()(PROJECT_ENV if as_path_object else str(PROJECT_ENV))
    assert A_REAL_ENV_VAR not in os.environ


def test_a_relative_path_to_the_project_env_is_the_same_file(monkeypatch):
    """``Path(...).resolve()`` is what makes this the same file, so a
    relative path cannot sneak past the comparison."""
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv(A_REAL_ENV_VAR, raising=False)
    _loader()(".env")
    assert A_REAL_ENV_VAR not in os.environ


def test_keyword_forms_are_guarded_too(monkeypatch):
    monkeypatch.delenv(A_REAL_ENV_VAR, raising=False)
    _loader()(dotenv_path=str(PROJECT_ENV))
    assert A_REAL_ENV_VAR not in os.environ


# ── Any other explicit path still loads ───────────────────────────────


def test_a_tmp_path_env_file_still_loads(tmp_path, monkeypatch):
    """A test writing its own .env must still be able to read it back."""
    name = "DCTA_GUARD_TMP_VAR"
    monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{name}=from-tmp-file\n", encoding="utf-8")

    result = _loader()(str(env_file))

    assert os.environ[name] == "from-tmp-file"
    assert result is True


def test_a_tmp_path_env_sets_each_of_its_variables(tmp_path, monkeypatch):
    for name in ("DCTA_GUARD_A", "DCTA_GUARD_B"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / "other.env"
    env_file.write_text("DCTA_GUARD_A=a\nDCTA_GUARD_B=b\n", encoding="utf-8")

    _loader()(str(env_file))

    assert (os.environ["DCTA_GUARD_A"], os.environ["DCTA_GUARD_B"]) == ("a", "b")


def test_a_nonexistent_path_is_simply_not_loaded(tmp_path):
    assert _loader()(str(tmp_path / "nope.env")) is False


def test_a_tmp_file_named_env_elsewhere_is_not_the_project_env(tmp_path, monkeypatch):
    """Only the real path is inert — not every file called ``.env``."""
    name = "DCTA_GUARD_DECOY"
    monkeypatch.delenv(name, raising=False)
    decoy = tmp_path / ".env"
    decoy.write_text(f"{name}=decoy\n", encoding="utf-8")

    _loader()(str(decoy))

    assert os.environ[name] == "decoy"


# ── No path at all still raises ───────────────────────────────────────


def test_calling_the_loader_with_no_path_raises():
    with pytest.raises(RuntimeError, match="without an explicit path"):
        _loader()()


def test_an_empty_path_counts_as_no_path():
    for empty in ("", None):
        with pytest.raises(RuntimeError, match="without an explicit path"):
            _loader()(empty)


def test_the_raise_happens_before_anything_is_read(monkeypatch):
    monkeypatch.delenv(A_REAL_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError):
        _loader()()
    assert A_REAL_ENV_VAR not in os.environ


# ── The predicate itself ──────────────────────────────────────────────


def test_is_project_env_recognises_the_file(monkeypatch):
    monkeypatch.chdir(ROOT)
    assert _is_project_env(str(PROJECT_ENV))
    assert _is_project_env(PROJECT_ENV)
    assert _is_project_env(".env")


@pytest.mark.parametrize("other", [
    "/tmp/.env",
    "env",
    ".env.example",
    "backend/.env",
])
def test_is_project_env_rejects_other_paths(other):
    assert not _is_project_env(other)


@pytest.mark.parametrize("junk", [None, 0, b"", object()])
def test_is_project_env_survives_junk(junk):
    """An unresolvable argument is not the project .env, and must not
    raise out of the guard."""
    assert not _is_project_env(junk)


# ── The guard is wired into the production call path ──────────────────


def test_load_llm_settings_with_the_project_env_sees_no_keys(monkeypatch):
    """The whole point: this is what ``_chat_client()`` calls, and with the
    variables deleted it must fail closed rather than read the file."""
    for name in (
        "PARSER_APPKEY",
        "VALIDATOR_APPKEY",
        "PARSER_MODEL_LABEL",
        "VALIDATOR_MODEL_LABEL",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="Missing required environment variable"):
        llm_config.load_llm_settings(env_path=str(PROJECT_ENV))


def test_the_guard_is_active_for_every_test_without_being_requested():
    """It is autouse: no test has to remember to ask for it."""
    assert _loader() is not getattr(llm_config, "_real_load_dotenv", None)
    assert _loader().__name__ == "_guarded_load_dotenv"
