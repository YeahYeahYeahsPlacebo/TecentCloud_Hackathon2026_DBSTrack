"""Deployment configuration: rp_id and origin are settings, not constants.

WebAuthn only works on the host it was registered for, and
``/api/execute`` compares the origin in ``clientDataJSON`` to the one the
server was configured with — literally, including the port. Hard-coded to
localhost, the server works on one machine and fails everywhere else with
``signature_invalid``, which looks like a broken authenticator rather than
a deployment that forgot to say where it is.

These tests exist because the two defaults are also the two values every
test relies on: if reading the environment ever breaks local development,
it should break here, loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from backend.gateway.server import (  # noqa: E402
    DEFAULT_ORIGIN,
    DEFAULT_RP_ID,
    build_api,
    create_app,
)


def test_with_nothing_set_the_server_stays_on_localhost(monkeypatch):
    monkeypatch.delenv("DCTA_RP_ID", raising=False)
    monkeypatch.delenv("DCTA_ORIGIN", raising=False)

    api = build_api()

    assert api.webauthn.rp_id == DEFAULT_RP_ID
    assert api.executor.rp_id == DEFAULT_RP_ID
    assert api.executor.origin == DEFAULT_ORIGIN


def test_a_deployed_host_and_origin_are_used(monkeypatch):
    """The whole point: the same code on a real host."""
    monkeypatch.setenv("DCTA_RP_ID", "dcta.example.com")
    monkeypatch.setenv("DCTA_ORIGIN", "https://dcta.example.com")

    api = build_api()

    assert api.webauthn.rp_id == "dcta.example.com"
    assert api.executor.rp_id == "dcta.example.com"
    assert api.executor.origin == "https://dcta.example.com"


def test_an_empty_value_is_unset_not_an_empty_origin(monkeypatch):
    """``DCTA_ORIGIN=`` in a shell must not silently mean "no origin
    matches", which is every signature failing for an invisible reason."""
    monkeypatch.setenv("DCTA_RP_ID", "   ")
    monkeypatch.setenv("DCTA_ORIGIN", "")

    api = build_api()

    assert api.webauthn.rp_id == DEFAULT_RP_ID
    assert api.executor.origin == DEFAULT_ORIGIN


def test_auto_binds_webauthn_to_the_host_the_request_came_in_on(monkeypatch):
    """A deployment does not know its own domain when the app is built.

    ``auto`` reads it off the request instead, which is what makes a first
    deploy work before anyone has seen the domain. The proxy headers are
    what a platform in front of the app actually sends.
    """
    monkeypatch.setenv("DCTA_RP_ID", "auto")
    monkeypatch.setenv("DCTA_ORIGIN", "auto")

    api = build_api()
    app = create_app(api, serve_frontend=False)
    client = TestClient(app)

    health = client.get(
        "/api/health",
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "dcta.example.com"},
    ).json()

    assert health["rp_id"] == "dcta.example.com"
    assert health["origin"] == "https://dcta.example.com"


def test_auto_falls_back_to_the_host_header(monkeypatch):
    """A host header is enough when no proxy sets a forwarded one."""
    monkeypatch.setenv("DCTA_RP_ID", "auto")
    monkeypatch.setenv("DCTA_ORIGIN", "auto")

    api = build_api()
    client = TestClient(create_app(api, serve_frontend=False))

    health = client.get("/api/health", headers={"host": "pay.internal:8000"}).json()

    assert health["rp_id"] == "pay.internal"
    assert health["origin"] == "http://pay.internal:8000"


def test_auto_leaves_an_explicit_setting_alone(monkeypatch):
    """Only what is set to auto is derived. Naming the rpId but letting the
    origin follow the request — or the other way round — has to work, or
    auto is not composable with a domain that is partly known."""
    monkeypatch.setenv("DCTA_RP_ID", "dcta.example.com")
    monkeypatch.setenv("DCTA_ORIGIN", "auto")

    api = build_api()
    client = TestClient(create_app(api, serve_frontend=False))

    health = client.get("/api/health", headers={"host": "other.example.com"}).json()

    assert health["rp_id"] == "dcta.example.com"
    assert health["origin"] == "http://other.example.com"


def test_without_auto_a_request_on_another_host_changes_nothing(monkeypatch):
    """Auto is opt-in: a configured server ignores the host header, so a
    stray Host cannot move the origin a signature is checked against."""
    monkeypatch.delenv("DCTA_RP_ID", raising=False)
    monkeypatch.delenv("DCTA_ORIGIN", raising=False)

    api = build_api()
    client = TestClient(create_app(api, serve_frontend=False))

    health = client.get("/api/health", headers={"host": "evil.example.com"}).json()

    assert health["rp_id"] == DEFAULT_RP_ID
    assert health["origin"] == DEFAULT_ORIGIN


def test_the_challenge_reports_the_configured_rp_id(monkeypatch):
    """The browser is told which rpId to create/assert against, so it has
    to be the configured one and not a constant baked into the module."""
    monkeypatch.setenv("DCTA_RP_ID", "dcta.example.com")
    monkeypatch.setenv("DCTA_ORIGIN", "https://dcta.example.com")
    # Without a verdict the challenge is refused before rp_id is ever built,
    # so this is the one test here that needs the dev pass.
    monkeypatch.setenv("DCTA_ALLOW_UNVALIDATED", "true")

    api = build_api()
    draft_id = api.message({"transcript": "Send fifty dollars to John Smith."})[
        "items"
    ][0]["draft"]["id"]

    challenge = api.challenge({"draft_id": draft_id})

    assert challenge["rp_id"] == "dcta.example.com"
