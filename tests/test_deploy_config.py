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

from backend.gateway.server import (  # noqa: E402
    DEFAULT_ORIGIN,
    DEFAULT_RP_ID,
    build_api,
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
