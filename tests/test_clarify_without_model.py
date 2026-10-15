"""The free-text form of ``/api/clarify``, on a server with no model.

``/api/clarify`` has two forms. The candidate-pick form never needed a
model, but the free-text form asked one to pull a single field out of an
answer — so on a server with no ADP keys it could only answer with a 500.
That is the form a demo needs most: "who is mum?" has no candidate list
to pick from, so the pick form cannot be used instead.

``resolve_answer_rules`` reads the answer with the same code the stub
parser already trusts (``spoken_amount``, ``match_payees``), which is the
trade-off the stub parser already made: deterministic, offline, and it
asks again rather than guessing. These tests hold it to that, and hold
both backends to the same checks — they now share them, and a check that
only held when a model was involved would be worth nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.gateway.api import GatewayAPI  # noqa: E402
from backend.gateway.message import ServerNotConfigured  # noqa: E402
from backend.gateway.server import build_api, create_app  # noqa: E402
from backend.gateway.webauthn import public_key_to_pem  # noqa: E402
from backend.parser.clarify import resolve_answer_rules  # noqa: E402
from backend.parser.directory import FixtureDirectory  # noqa: E402
from backend.parser.interface import (  # noqa: E402
    ClarifyingQuestion,
    InvalidClarificationAnswer,
    TransactionDraft,
)
from backend.parser.stub import parse  # noqa: E402
from gateway_fakes import (  # noqa: E402
    new_key,
    passing_validator,
    sign_assertion,
)

DIRECTORY = FixtureDirectory()


def pending_for(transcript: str):
    """The PendingQuestion the stub parser leaves behind for *transcript*."""
    result = parse(transcript)
    question_ids = list(result.pending)
    assert len(question_ids) == 1, transcript
    return result.pending[question_ids[0]]


def answer(pending, text: str):
    return resolve_answer_rules(pending, text, DIRECTORY)


def draft_of(result) -> TransactionDraft:
    assert len(result.items) == 1
    assert isinstance(result.items[0], TransactionDraft)
    return result.items[0]


# ── Reading an answer with code ─────────────────────────────────────────


def test_a_named_payee_is_resolved_with_no_model_involved():
    """'mum' cannot be matched, so the stub asks. Naming a payee answers it."""
    pending = pending_for("Send fifty dollars to mum.")

    draft = draft_of(answer(pending, "John Lee")[0])

    assert draft.payee.id == "payee-102"
    assert draft.payee.display_name == "John Lee"
    # The amount was never in question, so it is untouched.
    assert draft.amount.value == "50.00"


def test_a_spoken_amount_is_read_by_the_parser_rule_not_a_guess():
    """R3 holds without a model: 'fifty' is 50.00 because the code says so."""
    pending = pending_for("Send money to John Smith.")

    draft = draft_of(answer(pending, "fifty dollars")[0])

    assert draft.amount.value == "50.00"
    assert draft.amount.currency == "SGD"


def test_digits_the_user_typed_are_padded_to_two_decimals():
    pending = pending_for("Send money to John Smith.")

    assert draft_of(answer(pending, "1250.5 SGD")[0]).amount.value == "1250.50"
    assert draft_of(answer(pending, "80")[0]).amount.value == "80.00"
    assert draft_of(answer(pending, "$1,200.00")[0]).amount.value == "1200.00"


def test_digits_are_read_out_of_an_answer_the_speech_rules_cannot_speak():
    """The fallback underneath the spoken-number rule: digits the user
    typed, padded to the two decimals the money pattern requires."""
    from backend.parser.clarify import _extract_amount_from_answer

    assert _extract_amount_from_answer("$50.00") == "50.00"
    assert _extract_amount_from_answer("1250.5") == "1250.50"
    assert _extract_amount_from_answer("80") == "80.00"
    assert _extract_amount_from_answer("1,200.00") == "1200.00"
    assert _extract_amount_from_answer("fifty") == "50.00"
    assert _extract_amount_from_answer("not sure") is None


def test_a_vague_amount_is_asked_again_and_drafts_nothing():
    """'some' is not a number. Guessing one would be worse than asking."""
    pending = pending_for("Send money to John Smith.")

    result, _ = answer(pending, "some")

    assert isinstance(result.items[0], ClarifyingQuestion)
    assert result.items[0].field == "amount"


def test_an_answer_with_no_number_in_it_at_all_is_asked_again():
    """The check that has no model behind it: nothing here can read a
    number, so nothing here may invent one."""
    pending = pending_for("Send money to John Smith.")

    result, _ = answer(pending, "not sure")

    assert isinstance(result.items[0], ClarifyingQuestion)
    assert result.items[0].field == "amount"


def test_a_payee_the_directory_does_not_know_is_refused():
    """Not 'resolved to someone similar' — refused, with a reason code."""
    pending = pending_for("Send fifty dollars to mum.")

    with pytest.raises(InvalidClarificationAnswer) as exc:
        answer(pending, "Bob")

    assert "not found in directory" in str(exc.value)


def test_an_ambiguous_payee_is_refused_rather_than_picked():
    """'John' is two payees. Picking one would be the bug this prevents."""
    pending = pending_for("Send fifty dollars to mum.")

    with pytest.raises(InvalidClarificationAnswer) as exc:
        answer(pending, "John")

    assert "ambiguous" in str(exc.value)


def test_a_negative_amount_is_asked_again_without_a_model():
    """The sign is never drafted, with or without a model to cross-check."""
    pending = pending_for("Send money to John Smith.")

    result, _ = answer(pending, "minus fifty dollars")

    assert isinstance(result.items[0], ClarifyingQuestion)
    assert "negative" in result.items[0].question.lower()


def test_an_answer_that_changes_another_field_is_refused():
    """Shared with the model path on purpose: asking for the amount does
    not licence a new payee."""
    pending = pending_for("Send money to John Smith.")

    with pytest.raises(InvalidClarificationAnswer) as exc:
        answer(pending, "fifty dollars to Jane")

    assert "already resolved" in str(exc.value)


def test_an_account_suffix_still_selects_a_payee_by_code():
    """The suffix rule was always code, so it must survive with no model."""
    pending = pending_for("Send fifty dollars to mum.")

    draft = draft_of(answer(pending, "account ending 8892")[0])

    assert draft.payee.id == "payee-102"


# ── The gateway, with no client ─────────────────────────────────────────


def make_service(**kwargs) -> GatewayAPI:
    return GatewayAPI(
        parser=parse,
        validator=passing_validator(),
        directory=DIRECTORY,
        **kwargs,
    )


def test_the_question_form_works_without_a_chat_client():
    api = make_service(client=None, answer_resolver=resolve_answer_rules)

    question = api.message({"transcript": "Send fifty dollars to mum."})["items"][0]
    clarified = api.clarify(
        {"question_id": question["question_id"], "answer": "John Lee"}
    )

    assert clarified["items"][0]["kind"] == "draft"
    assert clarified["items"][0]["draft"]["payee"]["id"] == "payee-102"


def test_without_a_client_or_a_resolver_it_still_fails_loud():
    """No way to read the answer is a wiring gap, not a rejected answer:
    still a 500, never a guess and never a 422 blaming the user."""
    api = make_service(client=None)

    question = api.message({"transcript": "Send fifty dollars to mum."})["items"][0]

    with pytest.raises(ServerNotConfigured):
        api.clarify({"question_id": question["question_id"], "answer": "John Lee"})


def test_the_audit_row_says_what_the_answer_was_given_for():
    """An answer on its own names nobody; the row has to carry the
    transcript it answered."""
    api = make_service(client=None, answer_resolver=resolve_answer_rules)

    question = api.message({"transcript": "Send fifty dollars to mum."})["items"][0]
    api.clarify({"question_id": question["question_id"], "answer": "John Lee"})

    row = api.audit.rows[-1]
    assert row.event == "clarify"
    assert row.payload["transcript"] == "Send fifty dollars to mum."
    assert row.payload["answer"] == "John Lee"


def test_a_resolved_question_still_cannot_be_answered_twice():
    """Single-use is server state; the code path cannot opt out of it."""
    api = make_service(client=None, answer_resolver=resolve_answer_rules)

    question = api.message({"transcript": "Send fifty dollars to mum."})["items"][0]
    api.clarify({"question_id": question["question_id"], "answer": "John Lee"})
    again = api.clarify({"question_id": question["question_id"], "answer": "Jane"})

    assert again["result"] == "rejected"
    assert again["reason_code"] == "invalid_clarification_answer"


def test_a_full_run_from_question_to_signed_execution_needs_no_model():
    """The whole gate, offline: ask, answer, sign, execute."""
    api = make_service(client=None, answer_resolver=resolve_answer_rules)
    private_key = new_key()

    question = api.message({"transcript": "Send fifty dollars to mum."})["items"][0]
    draft_id = api.clarify(
        {"question_id": question["question_id"], "answer": "John Lee"}
    )["items"][0]["draft"]["id"]

    credential_id = api.register(
        {
            "user_id": "user-001",
            "public_key": public_key_to_pem(private_key.public_key()),
        }
    )["credential_id"]
    challenge = api.challenge(
        {"draft_id": draft_id, "credential_id": credential_id}
    )["challenge"]

    result = api.execute(
        {
            "draft_id": draft_id,
            "credential_id": credential_id,
            "idempotency_key": "key-1",
            "assertion": sign_assertion(private_key, credential_id, challenge),
        }
    )

    assert result["result"] == "executed", result


# ── Which backend the server picked ─────────────────────────────────────


def test_with_no_chat_client_the_server_reads_answers_by_rule(monkeypatch):
    monkeypatch.setenv("DCTA_NO_CLIENT", "1")
    monkeypatch.delenv("DCTA_ANSWER_BACKEND", raising=False)

    api = build_api()

    assert api.answer_resolver is not None
    assert api.client is None


def test_answer_backend_model_requires_the_client_and_says_so(monkeypatch):
    """Opting into the model without keys is still a loud 500 — the
    endpoint must not quietly fall back to rules when told not to."""
    monkeypatch.setenv("DCTA_NO_CLIENT", "1")
    monkeypatch.setenv("DCTA_ANSWER_BACKEND", "model")

    api = build_api()

    assert api.answer_resolver is None

    question = api.message({"transcript": "Send fifty dollars to mum."})["items"][0]
    with pytest.raises(ServerNotConfigured):
        api.clarify({"question_id": question["question_id"], "answer": "John Lee"})


def test_answer_backend_rules_can_be_forced_even_with_a_client(monkeypatch):
    monkeypatch.setenv("DCTA_FAKE_CLIENT", "1")
    monkeypatch.setenv("DCTA_ANSWER_BACKEND", "rules")

    api = build_api()

    assert api.client is not None
    assert api.answer_resolver is not None


def test_an_unknown_answer_backend_is_refused_at_startup(monkeypatch):
    """A typo in the setting must not silently mean 'model'."""
    monkeypatch.setenv("DCTA_ANSWER_BACKEND", "llm")

    with pytest.raises(RuntimeError):
        build_api()


def test_health_reports_which_backend_reads_answers(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DCTA_NO_CLIENT", "1")
    monkeypatch.delenv("DCTA_ANSWER_BACKEND", raising=False)
    rules = TestClient(create_app(build_api(), serve_frontend=False)).get(
        "/api/health"
    ).json()
    assert rules["answer_backend"] == "rules"
    assert rules["client"] is None

    monkeypatch.setenv("DCTA_ANSWER_BACKEND", "model")
    model = TestClient(create_app(build_api(), serve_frontend=False)).get(
        "/api/health"
    ).json()
    assert model["answer_backend"] == "model"
