"""
The review gaps in the gateway, one test each.

Member 1's review listed ten things the gateway got wrong. Each test below
names the bug it locks down, so a revert fails here rather than in review:

1. ``build_api()`` never passed ``client=`` — free-text ``/api/clarify`` 500s.
2. An empty ``draft_hash`` was trusted (see test_validator_integration.py).
3. ``/api/webauthn/challenge`` issued a challenge for a draft with no verdict.
4. 500s carried ``detail`` only, not the ``reason_code``/``message`` §5 wants.
5. The payee form of ``/api/clarify`` was not single-use.
6. ``self.pending`` kept expired questions forever.
7. A non-string ``draft_id`` was a 500 instead of ``malformed_request``.
8. The question form's audit row did not carry the original transcript.

The parser, the directory and the validator are injected: ``backend/parser``
and ``backend/validator`` belong to Member 1, and none of these tests depend
on what they decide internally.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.gateway.api import GatewayAPI  # noqa: E402
from backend.gateway import reason_codes as R  # noqa: E402
from backend.gateway.errors import error_response  # noqa: E402
from backend.gateway.server import build_api, create_app  # noqa: E402
from backend.llm.fake_client import FakeChatClient  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402
from backend.parser.directory import FixtureDirectory  # noqa: E402
from backend.parser.interface import (  # noqa: E402
    ClarifyingQuestion,
    ParseResult,
    PendingQuestion,
)

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from gateway_fakes import passing_validator  # noqa: E402

FIXTURES = ROOT / "fixtures" / "drafts"

# The six variables backend/llm/config.py requires.
LLM_ENV = (
    "PARSER_APPKEY",
    "VALIDATOR_APPKEY",
    "PARSER_MODEL_LABEL",
    "VALIDATOR_MODEL_LABEL",
    "ADP_ENDPOINT",
    "ADP_TIMEOUT_SECONDS",
)


def load(name: str) -> TransactionDraft:
    return TransactionDraft.model_validate_json((FIXTURES / name).read_text())


def live(draft: TransactionDraft, *, minutes_alive: int = 1, window: int = 5):
    """Re-stamp a frozen fixture onto the real wall clock.

    Member 1's ``resolve_payee`` checks expiry against ``datetime.now()``,
    not against the gateway's injected clock, so the 2026-10-03 fixtures read
    as expired and every clarification test would pass for the wrong reason.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    data = draft.model_dump(mode="json", exclude_none=True)
    data["created_at"] = now - timedelta(minutes=minutes_alive)
    data["expires_at"] = now + timedelta(minutes=window)
    return TransactionDraft.model_validate(data)


class FakeParser:
    def __init__(self, *items) -> None:
        self.items = list(items)

    def __call__(self, transcript: str) -> ParseResult:
        return ParseResult(items=self.items, intents_detected=len(self.items))


def make_api(*items, now=None, validator=None, client=None, parser=None):
    clean = load("clean_transfer.json")
    at = now or (clean.created_at + timedelta(minutes=1))
    return GatewayAPI(
        parser=parser if parser is not None else FakeParser(*(items or (clean,))),
        clock=lambda: at,
        validator=validator if validator is not None else passing_validator(),
        directory=FixtureDirectory(),
        client=client,
    )


def http_client(*items, raise_server_exceptions=True, **kwargs):
    """``raise_server_exceptions=False`` for the tests that want to read a 500
    body: starlette re-raises after its 500 handler so servers can log it,
    and the test client turns that into an exception by default."""
    return TestClient(
        create_app(make_api(*items, **kwargs), serve_frontend=False),
        raise_server_exceptions=raise_server_exceptions,
    )


# ── 1. The chat client reaches /api/clarify ───────────────────────────


def test_build_api_passes_a_chat_client_through(monkeypatch):
    """The free-text form calls a model, so with no client it could only 500.
    Wiring the client is what makes that endpoint answerable at all."""
    monkeypatch.setenv("PARSER_APPKEY", "parser-key")
    monkeypatch.setenv("VALIDATOR_APPKEY", "validator-key")
    monkeypatch.setenv("PARSER_MODEL_LABEL", "parser-model")
    monkeypatch.setenv("VALIDATOR_MODEL_LABEL", "validator-model")

    api = build_api()
    assert api.client is not None
    assert type(api.client).__name__ == "AdpChatClient"
    # The answer-extraction prompt is parser-side work, so it uses the
    # parser's application, never the validator's (CONTEXT.md constraint 4).
    # _app_key is private; this is the only place that can see it.
    assert api.client._app_key == "parser-key"


def test_with_no_keys_there_is_no_client_and_health_says_so(monkeypatch):
    """No keys, no model, no invented answer: the client stays None and
    /api/health reports it rather than letting the demo look wired."""
    for name in LLM_ENV:
        monkeypatch.delenv(name, raising=False)

    api = build_api()
    assert api.client is None
    assert http_client().get("/api/health").json()["client"] is None


def test_dcta_fake_client_is_an_explicit_opt_in(monkeypatch):
    """Scripted replies are for exercising wiring, so they are asked for —
    never a silent fallback when the real keys are missing."""
    for name in LLM_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DCTA_FAKE_CLIENT", "1")
    assert type(build_api().client).__name__ == "FakeChatClient"

    monkeypatch.setenv("DCTA_NO_CLIENT", "1")
    assert build_api().client is None


# ── 3. A challenge needs a recorded pass, not merely no objection ──────


def test_a_challenge_is_refused_when_no_verdict_was_recorded():
    """A draft the server never validated has no verdict. Refusing it at
    execute (check 11) but issuing a challenge anyway spends the user's
    biometric on a signature /api/execute is going to reject."""
    api = make_api()
    draft_id = api.message({"transcript": "hi"})["items"][0]["draft"]["id"]

    api.verdicts.clear()  # as if the validator had never run
    response = api.challenge({"draft_id": draft_id})
    assert response["reason_code"] == R.VALIDATOR_FROZEN
    assert "no validator verdict" in response["message"]


def test_a_recorded_pass_still_gets_a_challenge():
    """The gate above must not become 'never issue one'."""
    api = make_api()
    draft_id = api.message({"transcript": "hi"})["items"][0]["draft"]["id"]
    assert api.verdicts
    assert "challenge" in api.challenge({"draft_id": draft_id})


# ── 4. A 500 carries the contract's three fields ──────────────────────


def test_a_500_carries_result_reason_code_and_message():
    """§5 fixes the shape of every error response and does not exempt ours.
    A bare `detail` left the client with nothing to branch on."""
    c = http_client()
    c.app.state.gateway.store["d-1"] = load("ambiguous_payee.json")
    c.app.state.gateway.messages.directory = None  # -> ServerNotConfigured

    response = c.post(
        "/api/clarify",
        json={"draft_id": "d-1", "field": "payee", "answer": "payee-102"},
    )
    assert response.status_code == 500
    body = response.json()
    assert body["result"] == "error"  # not "rejected": this is our failure
    assert body["reason_code"] == R.INTERNAL_ERROR
    assert body["message"]
    assert body["bug"] == "wiring"


def test_a_parser_contract_violation_carries_the_same_shape():
    def broken(transcript):
        return ParseResult(items=[], intents_detected=2)

    c = http_client()
    c.app.state.gateway.messages.parser = broken
    body = c.post("/api/message", json={"transcript": "hi"}).json()
    assert body["result"] == "error"
    assert body["reason_code"] == R.INTERNAL_ERROR
    assert body["bug"] == "parser"


def test_an_unhandled_exception_still_carries_the_shape():
    """'Every error' means the ones we did not predict too. The type stays in
    `message` — hiding it would make the least expected case the hardest to
    debug."""

    def boom(transcript):
        raise ValueError("kaboom")

    c = http_client(raise_server_exceptions=False)
    c.app.state.gateway.messages.parser = boom
    response = c.post("/api/message", json={"transcript": "hi"})
    assert response.status_code == 500
    body = response.json()
    assert body["reason_code"] == R.INTERNAL_ERROR
    assert "ValueError" in body["message"]
    assert body["bug"] == "unhandled"


def test_error_response_is_the_one_place_the_500_shape_is_written():
    assert error_response("boom", bug="wiring") == {
        "result": "error",
        "reason_code": "internal_error",
        "message": "boom",
        "bug": "wiring",
    }


# ── 5. The payee form is single-use ───────────────────────────────────


def test_a_draft_can_only_be_clarified_once():
    """Picking a candidate twice would produce two resolved drafts from one
    ambiguity. The ambiguous draft stays stored and unresolved — that is what
    makes it unexecutable — but it cannot be picked again."""
    ambiguous = live(load("ambiguous_payee.json"))
    api = make_api(ambiguous)
    draft_id = api.message({"transcript": "Send fifty dollars to John."})["items"][0][
        "draft"
    ]["id"]

    first = api.clarify({"draft_id": draft_id, "field": "payee", "answer": "payee-102"})
    new_id = first["items"][0]["draft"]["id"]

    again = api.clarify({"draft_id": draft_id, "field": "payee", "answer": "payee-102"})
    assert again["reason_code"] == R.INVALID_CLARIFICATION_ANSWER
    assert "already been clarified" in again["message"]
    assert new_id in again["message"]  # and it says which draft to use now

    # The original is still the unexecutable draft it always was.
    assert api.store[draft_id].unresolved == ["payee"]
    assert api.challenge({"draft_id": draft_id})["reason_code"] == R.UNRESOLVED_FIELDS


def test_a_refused_answer_does_not_consume_the_draft():
    """Single-use must not become single-try: an answer that was refused
    leaves the question open, or a bad click locks the user out."""
    ambiguous = live(load("ambiguous_payee.json"))
    api = make_api(ambiguous)
    draft_id = api.message({"transcript": "Send fifty dollars to John."})["items"][0][
        "draft"
    ]["id"]

    refused = api.clarify({"draft_id": draft_id, "field": "payee", "answer": "payee-999"})
    assert refused["reason_code"] == R.INVALID_CLARIFICATION_ANSWER

    accepted = api.clarify({"draft_id": draft_id, "field": "payee", "answer": "payee-102"})
    assert accepted["items"][0]["draft"]["payee"]["id"] == "payee-102"


# ── 6. Expired pending questions are forgotten ─────────────────────────


def pending_question(field="amount", transcript="Send some money to John Smith."):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return PendingQuestion(
        question_id=f"q-{field}-001",
        field=field,
        original_transcript=transcript,
        partial={
            "intent_type": "transfer",
            "source_account": "acct-001-2345",
            "payee": {
                "id": "payee-001",
                "display_name": "John Smith",
                "masked_account": "****1234",
            },
        },
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )


def asking_parser(pending):
    class _Asking:
        def __call__(self, transcript):
            return ParseResult(
                items=[
                    ClarifyingQuestion(
                        question_id=pending.question_id,
                        field=pending.field,
                        question=f"What {pending.field}?",
                    )
                ],
                intents_detected=1,
                pending={pending.question_id: pending},
            )

    return _Asking()


def amount_reply():
    return json.dumps({"resolved": True, "amount_text": "fifty dollars", "amount_value": "50.00"})


def test_a_pending_question_is_forgotten_when_it_expires():
    """A question expires with the draft it came from (§4, second form).
    Left in place it is answerable for as long as the server lives."""
    pending = pending_question()
    api = make_api(
        parser=asking_parser(pending), client=FakeChatClient([amount_reply()])
    )
    api.message({"transcript": pending.original_transcript})
    assert pending.question_id in api.messages.pending

    # A still-live question survives a pass that prunes, so this is not
    # "forget everything on every call".
    api.clarify({"question_id": "q-never-issued", "answer": "fifty dollars"})
    assert pending.question_id in api.messages.pending

    api.messages.clock = lambda: pending.expires_at + timedelta(seconds=1)
    response = api.clarify(
        {"question_id": pending.question_id, "answer": "fifty dollars"}
    )
    assert response["reason_code"] == R.INVALID_CLARIFICATION_ANSWER
    assert pending.question_id not in api.messages.pending


# ── 7. A non-string draft_id is malformed_request, not a 500 ───────────


def test_a_non_string_draft_id_is_malformed_request_not_a_500():
    """A dict is unhashable, so the store lookup raised and the user's
    malformed body came back as our server error."""
    api = make_api()

    for body in (
        {"draft_id": {"a": 1}, "field": "payee", "answer": "payee-102"},
        {"draft_id": ["d-1"], "field": "payee", "answer": "payee-102"},
        {"draft_id": 1, "field": "payee", "answer": "payee-102"},
    ):
        assert api.clarify(body)["reason_code"] == R.MALFORMED_REQUEST

    assert api.challenge({"draft_id": {"a": 1}})["reason_code"] == R.MALFORMED_REQUEST
    assert (
        api.execute(
            {
                "draft_id": {"a": 1},
                "credential_id": "c",
                "idempotency_key": "k",
                "assertion": {},
            }
        )["reason_code"]
        == R.MALFORMED_REQUEST
    )


def test_a_non_string_question_id_is_malformed_request_too():
    api = make_api()
    response = api.clarify({"question_id": {"a": 1}, "answer": "fifty"})
    assert response["reason_code"] == R.MALFORMED_REQUEST
    assert (
        api.clarify({"question_id": "q-amount-001", "answer": {"a": 1}})["reason_code"]
        == R.MALFORMED_REQUEST
    )


def test_a_missing_draft_id_is_still_missing_fields_not_a_type_error():
    """The type check must not swallow the 'missing' case with a worse
    message."""
    response = make_api().clarify({"field": "payee", "answer": "payee-102"})
    assert response["reason_code"] == R.MALFORMED_REQUEST
    assert "missing field" in response["message"]


# ── 8. The question form's audit row names the transcript ──────────────


def test_the_question_form_audit_row_carries_the_original_transcript():
    """§4, second form: the server stores the transcript and the answer
    separately and records both. An audit row with an answer and no
    transcript cannot be read later."""
    pending = pending_question()
    api = make_api(
        parser=asking_parser(pending), client=FakeChatClient([amount_reply()])
    )
    api.message({"transcript": pending.original_transcript})
    api.clarify({"question_id": pending.question_id, "answer": "fifty dollars"})

    row = api.audit.rows[-1]
    assert row.event == "clarify"
    assert row.payload["transcript"] == pending.original_transcript
    assert row.payload["answer"] == "fifty dollars"
    # Stored separately, never merged.
    assert "fifty" not in row.payload["transcript"]


def test_the_payee_form_audit_row_carries_the_transcript_too():
    """Same reasoning as the question form: "payee-102" in an audit row names
    nobody on its own. §4 asks for the transcript and the answer to be stored
    separately, on both forms of /api/clarify."""
    ambiguous = live(load("ambiguous_payee.json"))
    api = make_api(ambiguous)
    draft_id = api.message({"transcript": "Send fifty dollars to John."})["items"][0][
        "draft"
    ]["id"]
    api.clarify({"draft_id": draft_id, "field": "payee", "answer": "payee-102"})

    row = api.audit.rows[-1]
    assert row.event == "clarify"
    assert row.payload["transcript"] == ambiguous.transcript
    assert row.payload["answer"] == "payee-102"
    assert row.payload["from_draft_id"] == draft_id
    # The answer is never merged into the transcript.
    assert "payee-102" not in row.payload["transcript"]
