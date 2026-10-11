"""
The HTTP layer: `backend/gateway/server.py`.

`GatewayAPI` is a plain Python object, so the whole gateway can be tested
without a server. This file tests the one thing that cannot be: the
translation from dict to status code, and from our own bugs to a 500.

It also asserts the endpoints the frontend actually calls exist. frontend/
app.js does `fetch("/api/message")` — before this module existed, that was a
404 and no part of the demo could run.
"""

import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.canonical import draft_hash  # noqa: E402
from backend.gateway.api import GatewayAPI  # noqa: E402
from backend.gateway.message import (  # noqa: E402
    ParserContractViolation,
    ServerNotConfigured,
)
from backend.gateway.server import create_app  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402
from backend.parser.directory import FixtureDirectory  # noqa: E402
from backend.parser.interface import ParseResult, ParserCannotHandle  # noqa: E402
from backend.validator.interface import ValidatorVerdict  # noqa: E402

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient


def load(name: str) -> TransactionDraft:
    return TransactionDraft.model_validate_json(
        (ROOT / "fixtures" / "drafts" / name).read_text()
    )


class PassingValidator:
    def validate(self, draft, transcript, clarification=None) -> ValidatorVerdict:
        return ValidatorVerdict(
            verdict="pass",
            discrepancies=[],
            reason="ok",
            draft_hash=draft_hash(draft.model_dump(mode="json", exclude_none=True)),
        )


class FakeParser:
    def __init__(self, *items, raises=None):
        self.items = list(items)
        self.raises = raises

    def __call__(self, transcript: str) -> ParseResult:
        if self.raises is not None:
            raise self.raises
        return ParseResult(items=self.items, intents_detected=len(self.items))


def client(*items, parser=None, validator=None, frontend=False):
    clean = load("clean_transfer.json")
    api = GatewayAPI(
        parser=parser if parser is not None else FakeParser(*(items or (clean,))),
        clock=lambda: clean.created_at + timedelta(minutes=1),
        validator=validator if validator is not None else PassingValidator(),
        directory=FixtureDirectory(),
    )
    return TestClient(create_app(api, serve_frontend=frontend))


# ── The routes the frontend calls ─────────────────────────────────────


def test_every_endpoint_the_frontend_calls_exists():
    """frontend/app.js fetches /api/message; /api/execute needs the others."""
    app = create_app(client().app.state.gateway, serve_frontend=False)
    paths = {(route.path, tuple(sorted(getattr(route, "methods", ()) or ()))) for route in app.routes}
    assert ("/api/message", ("POST",)) in paths
    assert ("/api/clarify", ("POST",)) in paths
    assert ("/api/webauthn/challenge", ("POST",)) in paths
    assert ("/api/webauthn/register", ("POST",)) in paths
    assert ("/api/execute", ("POST",)) in paths
    assert ("/api/audit/verify", ("GET",)) in paths


def test_a_draft_is_returned_with_200_and_the_body_untouched():
    response = client().post("/api/message", json={"transcript": "Send fifty dollars to John Smith."})
    assert response.status_code == 200
    body = response.json()
    assert body["intents_detected"] == 1
    assert body["items"][0]["kind"] == "draft"


# ── Rejection -> 422, body unchanged ──────────────────────────────────


def test_a_rejection_is_422_and_carries_the_gateway_body_verbatim():
    response = client().post("/api/webauthn/challenge", json={"draft_id": "nope"})
    assert response.status_code == 422
    body = response.json()
    assert body["result"] == "rejected"
    assert body["reason_code"] == "unknown_draft"
    assert "message" in body


def test_an_empty_execute_body_is_malformed_request_not_a_server_error():
    response = client().post("/api/execute", json={})
    assert response.status_code == 422
    assert response.json()["reason_code"] == "malformed_request"


def test_a_body_that_is_not_json_reaches_step_1_instead_of_crashing():
    """The gateway owns the malformed_request decision, so the HTTP layer
    must not answer first with a 500 from its own JSON parser."""
    response = client().post(
        "/api/message",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["reason_code"] == "malformed_request"


# ── Our bugs are 500s, not the user's 422s ────────────────────────────


def test_a_parser_that_drops_an_intent_is_a_500_not_a_rejection():
    """CONTRACT.md §4: there is no reason code for 'we lost one of your
    intents'. Silently returning the shorter list would be worse — and a 422
    would tell the user their request was refused when it was not."""

    def broken(transcript):
        return ParseResult(items=[], intents_detected=2)

    c = client(parser=FakeParser())
    c.app.state.gateway.messages.parser = broken
    response = c.post("/api/message", json={"transcript": "hi"})
    assert response.status_code == 500
    assert response.json()["bug"] == "parser"


def test_a_misconfigured_server_is_a_500_not_a_rejection():
    """No reason code exists for 'we are not wired up', and blaming the
    user's answer for our missing directory would be worse. The draft is
    stored first so the request gets past unknown_draft to the real problem."""
    c = client()
    c.app.state.gateway.store["d-1"] = load("ambiguous_payee.json")
    c.app.state.gateway.messages.directory = None

    response = c.post(
        "/api/clarify", json={"draft_id": "d-1", "field": "payee", "answer": "payee-102"}
    )
    assert response.status_code == 500
    assert response.json()["bug"] == "wiring"


# ── Multi-intent is refused, not half-drafted ─────────────────────────


def test_two_intents_in_one_sentence_are_refused_with_nothing_drafted():
    """CONTRACT.md §8 open question 4 (Resolved): before phase 5, refuse."""
    exc = ParserCannotHandle("refusing to drop one of two intents")
    exc.reason = "model: 2 intents"
    c = client(parser=FakeParser(raises=exc))
    response = c.post("/api/message", json={"transcript": "Send 50 to John and 20 to Jane"})
    assert response.status_code == 422
    assert response.json()["reason_code"] == "multi_intent_unsupported"
    assert c.app.state.gateway.store == {}  # nothing was drafted


def test_a_sentence_the_parser_does_not_know_gets_its_own_code():
    """Same exception, different cause. An eval table should be able to tell
    'two intents' from 'the stub does not know this one'."""
    exc = ParserCannotHandle("no pattern matched")
    exc.reason = "keyword guard"
    c = client(parser=FakeParser(raises=exc))
    response = c.post("/api/message", json={"transcript": "asdfghjkl"})
    assert response.status_code == 422
    assert response.json()["reason_code"] == "parser_cannot_handle"


# ── /api/health says what is actually wired ───────────────────────────


def test_health_reports_the_real_assembly_not_a_hardcoded_ok():
    body = client().get("/api/health").json()
    assert body["ok"] is True
    assert body["validator"] == "PassingValidator"
    assert body["parser"] == "FakeParser"
    assert body["bank"].endswith("accounts")


# ── Frontend, same origin ─────────────────────────────────────────────


def test_the_frontend_is_served_from_the_same_origin():
    """app.js uses a relative fetch("/api/message"), so a different origin
    would fail on CORS before it failed on anything else."""
    response = client(frontend=True).get("/")
    assert response.status_code == 200
    assert "html" in response.headers["content-type"]


def test_the_frontend_mount_does_not_shadow_the_api():
    c = client(frontend=True)
    assert c.get("/api/health").status_code == 200
    assert c.get("/").status_code == 200
