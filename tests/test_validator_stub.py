"""The deterministic validator: ``VALIDATOR_BACKEND=stub``.

What this backend has to prove is narrower than the LLM one, and the
tests say what: it is not a second opinion (it re-reads the transcript
with the parser, so a mistake it shares with the parser is invisible to
it).  What it must do is refuse a draft that no longer matches what the
user said — which ``DCTA_ALLOW_UNVALIDATED=true`` currently waves
through — and give the same kind of reason the model path gives.
"""

from __future__ import annotations

import pytest

from backend.parser.directory import FixtureDirectory
from backend.parser.interface import ClarificationRecord
from backend.parser.stub import parse
from backend.models.draft import TransactionDraft
from backend.validator.factory import get_validator
from backend.validator.interface import ValidatorVerdict
from backend.validator.stub_validator import StubValidator
from backend.validator.llm_validator import _MAX_TRANSCRIPT_LEN

TRANSCRIPT = "Send fifty dollars to John Smith."
DIRECTORY = FixtureDirectory()


def _draft(
    transcript: str = TRANSCRIPT,
    *,
    payee_id: str = "payee-001",
    amount: str = "50.00",
    **overrides,
) -> TransactionDraft:
    """A transfer draft naming *payee_id*, as the parser would make it."""
    record = next(p for p in DIRECTORY.payees() if p.id == payee_id)
    data = {
        "id": "11111111-1111-1111-1111-111111111111",
        "created_at": "2026-10-03T10:00:00Z",
        "expires_at": "2026-10-03T10:05:00Z",
        "nonce": "nonce-stub-001",
        "intent_type": "transfer",
        "source_account": "acct-001-2345",
        "amount": {"value": amount, "currency": "SGD"},
        "confidence": 0.95,
        "unresolved": [],
        "transcript": transcript,
        "payee": {
            "id": record.id,
            "display_name": record.display_name,
            "masked_account": record.masked_account,
        },
    }
    data.update(overrides)
    return TransactionDraft.model_validate(data)


def _validator() -> StubValidator:
    return StubValidator(directory=FixtureDirectory())


# ── The happy path ────────────────────────────────────────────────────


def test_a_draft_that_matches_the_transcript_passes():
    verdict = _validator().validate(_draft(), TRANSCRIPT)

    assert verdict.verdict == "pass"
    assert verdict.discrepancies == []
    # Says what happened, and does not claim a model looked at it.
    assert verdict.reason == "The draft matches the transcript."


def test_the_verdict_is_bound_to_the_draft_that_was_checked():
    """The gateway keys the execute check off this hash (check 11)."""
    verdict = _validator().validate(_draft(), TRANSCRIPT)

    assert verdict.verdict == "pass"
    assert verdict.draft_hash == _validator().validate(_draft(), TRANSCRIPT).draft_hash
    assert (
        _validator().validate(_draft(amount="500.00"), TRANSCRIPT).draft_hash
        != verdict.draft_hash
    )


# ── What it is for: a draft that no longer matches ────────────────────


def test_a_tampered_amount_freezes():
    """The case the dev bypass waves through."""
    verdict = _validator().validate(_draft(amount="500.00"), TRANSCRIPT)

    assert verdict.verdict == "freeze"
    assert verdict.discrepancies == ["amount"]
    assert "500.00" in verdict.reason and "50.00" in verdict.reason


def test_a_swapped_payee_freezes():
    """Said John Lee, drafted John Smith."""
    verdict = _validator().validate(
        _draft("Send fifty dollars to John Lee.", payee_id="payee-001"),
        "Send fifty dollars to John Lee.",
    )

    assert verdict.verdict == "freeze"
    assert "payee" in verdict.discrepancies


def test_a_transcript_the_parser_cannot_read_freezes_instead_of_crashing():
    """ParserCannotHandle must not reach the caller as an exception."""
    verdict = _validator().validate(_draft("Transfer to John Smith."), "Transfer to John Smith.")

    assert verdict.verdict == "freeze"
    assert verdict.reason


def test_a_currency_that_drifted_freezes():
    draft = _draft()
    draft.amount.currency = "USD"

    verdict = _validator().validate(draft, TRANSCRIPT)

    assert verdict.verdict == "freeze"
    assert "amount" in verdict.discrepancies


# ── Clarified drafts ──────────────────────────────────────────────────


def _pick(
    field: str,
    answer: str,
    transcript: str,
    kind: str = "text",
) -> ClarificationRecord:
    return ClarificationRecord(
        question_id="q-stub-1",
        field=field,
        answer=answer,
        answer_kind=kind,
        original_transcript=transcript,
    )


def test_a_payee_pick_is_checked_against_the_directory_record():
    """Picked John Lee out of the John candidates: passes."""
    draft = _draft("Pay John two hundred dollars.", payee_id="payee-102", amount="200.00")
    record = _pick("payee", "payee-102", "Pay John two hundred dollars.", "candidate_id")

    verdict = _validator().validate(
        draft, "Pay John two hundred dollars.", clarification=record
    )

    assert verdict.verdict == "pass", verdict.reason


def test_a_payee_pick_the_transcript_does_not_name_freezes():
    """Picked John Smith, who the transcript never mentions."""
    draft = _draft("Pay John two hundred dollars.", payee_id="payee-001", amount="200.00")
    record = _pick("payee", "payee-001", "Pay John two hundred dollars.", "candidate_id")

    verdict = _validator().validate(
        draft, "Pay John two hundred dollars.", clarification=record
    )

    assert verdict.verdict == "freeze"
    assert "payee" in verdict.discrepancies


def test_a_free_text_payee_answer_is_read_from_the_answer():
    """"mum" is in the transcript, the name is in the answer."""
    from backend.parser.clarify import resolve_answer_rules

    pending = parse("Send fifty dollars to mum.").pending
    question = pending[list(pending)[0]]
    result, record = resolve_answer_rules(question, "John Lee", FixtureDirectory())

    verdict = _validator().validate(
        result.items[0], "Send fifty dollars to mum.", clarification=record
    )

    assert verdict.verdict == "pass", verdict.reason


def test_a_free_text_amount_answer_is_read_from_the_answer():
    """The transcript says no amount; the answer does."""
    from backend.parser.clarify import resolve_answer_rules

    pending = parse("Buy shares.").pending
    question = pending[list(pending)[0]]
    result, record = resolve_answer_rules(question, "fifty dollars", FixtureDirectory())

    verdict = _validator().validate(result.items[0], "Buy shares.", clarification=record)

    assert verdict.verdict == "pass", verdict.reason


# ── Entry checks, shared with the LLM backend ─────────────────────────


def test_a_transcript_over_the_limit_freezes_without_reading_it():
    long_transcript = "a" * (_MAX_TRANSCRIPT_LEN + 1)

    verdict = _validator().validate(_draft(long_transcript), long_transcript)

    assert verdict.verdict == "freeze"
    assert verdict.reason == "transcript too long"


def test_a_negative_amount_freezes():
    record = _pick("amount", "-50", TRANSCRIPT)

    verdict = _validator().validate(_draft(), TRANSCRIPT, clarification=record)

    assert verdict.verdict == "freeze"
    assert verdict.discrepancies == ["amount"]


def test_more_than_two_decimals_freezes():
    record = _pick("amount", "50.1234", TRANSCRIPT)

    verdict = _validator().validate(_draft(), TRANSCRIPT, clarification=record)

    assert verdict.verdict == "freeze"
    assert verdict.discrepancies == ["amount"]


# ── Equity fields _compare cannot re-derive ───────────────────────────


def test_a_ticker_that_drifted_freezes():
    said = "Buy one thousand dollars of DBS shares at market price."
    draft = TransactionDraft.model_validate({
        "id": "33333333-3333-3333-3333-333333333333",
        "created_at": "2026-10-03T11:00:00Z",
        "expires_at": "2026-10-03T11:05:00Z",
        "nonce": "nonce-stub-eq",
        "intent_type": "equity_purchase",
        "source_account": "acct-001-2345",
        "amount": {"value": "1000.00", "currency": "SGD"},
        "confidence": 0.92,
        "unresolved": [],
        "transcript": said,
        "ticker": "U11.SI",
        "notional_amount": "1000.00",
        "order_type": "market",
    })

    verdict = _validator().validate(draft, said)

    assert verdict.verdict == "freeze"
    assert "ticker" in verdict.discrepancies


# ── Shape ─────────────────────────────────────────────────────────────


def test_the_callable_form_is_disabled():
    with pytest.raises(TypeError, match="ClarificationRecord"):
        _validator()(_draft(), TRANSCRIPT)


def test_the_factory_returns_it_when_the_backend_is_stub(monkeypatch):
    monkeypatch.setenv("VALIDATOR_BACKEND", "stub")
    assert isinstance(get_validator(), StubValidator)


def test_an_unknown_backend_still_refuses_to_pass(monkeypatch):
    monkeypatch.setenv("VALIDATOR_BACKEND", "bogus")
    validator = get_validator()
    assert type(validator).__name__ == "UnconfiguredValidator"


def test_it_never_returns_a_verdict_it_cannot_justify():
    """Every freeze names a field, or says why there is no field to name."""
    for transcript in [TRANSCRIPT, "Pay John two hundred dollars."]:
        verdict = _validator().validate(_draft(transcript), transcript)
        assert isinstance(verdict, ValidatorVerdict)
        if verdict.verdict == "freeze":
            assert verdict.reason
