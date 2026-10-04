"""
Tests for the throwaway stub parser (backend/parser/stub.py).

Every draft the stub returns must validate against the JSON Schema and the
Pydantic model.  Unrecognized transcripts must raise, and ambiguous amounts
must return a ClarifyingQuestion — never a draft with a guessed number.
"""

import json
import sys
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.models.draft import TransactionDraft  # noqa: E402
from backend.parser.interface import (  # noqa: E402
    ClarifyingQuestion,
    ParserCannotHandle,
)
from backend.parser.stub import parse  # noqa: E402

SCHEMA_PATH = ROOT / "contract" / "draft.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

# ── Helpers ──────────────────────────────────────────────────────────


def _draft_dict(draft: TransactionDraft) -> dict:
    """Serialise a draft to a plain dict for JSON Schema validation."""
    return draft.model_dump(mode="json", exclude_none=True)


def _validate_schema(draft: TransactionDraft) -> None:
    """Assert the draft validates against the JSON Schema."""
    jsonschema.validate(instance=_draft_dict(draft), schema=SCHEMA)


def _validate_pydantic(draft: TransactionDraft) -> None:
    """Re-validate the draft through the Pydantic model (round-trip)."""
    data = _draft_dict(draft)
    TransactionDraft(**data)


# ── Pattern 1: clean transfer ────────────────────────────────────────


def test_clean_transfer_returns_draft():
    """'Send fifty dollars to John Smith.' → clean transfer draft."""
    result = parse("Send fifty dollars to John Smith.")
    assert isinstance(result, TransactionDraft)

    assert result.intent_type.value == "transfer"
    assert result.amount.value == "50.00"
    assert result.amount.currency == "SGD"
    assert result.source_account == "acct-001-2345"
    assert result.unresolved == []
    assert result.payee is not None
    assert result.payee.id == "payee-001"
    assert result.payee.display_name == "John Smith"
    assert result.payee.masked_account == "****1234"


def test_clean_transfer_validates_against_schema():
    result = parse("Send fifty dollars to John Smith.")
    assert isinstance(result, TransactionDraft)
    _validate_schema(result)


def test_clean_transfer_validates_against_pydantic():
    result = parse("Send fifty dollars to John Smith.")
    assert isinstance(result, TransactionDraft)
    _validate_pydantic(result)


# ── Pattern 2: ambiguous payee ────────────────────────────────────────


def test_ambiguous_payee_returns_draft_with_unresolved():
    """'Send fifty dollars to John.' → ambiguous payee draft."""
    result = parse("Send fifty dollars to John.")
    assert isinstance(result, TransactionDraft)

    assert result.intent_type.value == "transfer"
    assert result.unresolved == ["payee"]
    assert result.payee is None
    assert result.payee_candidates is not None
    assert len(result.payee_candidates) == 2

    ids = [c.id for c in result.payee_candidates]
    assert "payee-101" in ids
    assert "payee-102" in ids

    names = [c.display_name for c in result.payee_candidates]
    assert "John Doe" in names
    assert "John Smith" in names

    masked = [c.masked_account for c in result.payee_candidates]
    assert "****4521" in masked
    assert "****8892" in masked


def test_ambiguous_payee_validates_against_schema():
    result = parse("Send fifty dollars to John.")
    assert isinstance(result, TransactionDraft)
    _validate_schema(result)


def test_ambiguous_payee_validates_against_pydantic():
    result = parse("Send fifty dollars to John.")
    assert isinstance(result, TransactionDraft)
    _validate_pydantic(result)


# ── Pattern 3: equity purchase ───────────────────────────────────────


def test_equity_purchase_returns_draft():
    """Anything mentioning shares/stock/buy → equity purchase draft."""
    result = parse("Buy one thousand dollars of DBS shares at market price.")
    assert isinstance(result, TransactionDraft)

    assert result.intent_type.value == "equity_purchase"
    assert result.source_account == "acct-001-2345"
    assert result.ticker == "D05.SI"
    assert result.order_type.value == "market"
    assert result.unresolved == []


def test_equity_purchase_with_dollar_amount():
    """'$1000.00 of shares' → equity purchase with that amount."""
    result = parse("Buy $1000.00 of DBS shares.")
    assert isinstance(result, TransactionDraft)
    assert result.intent_type.value == "equity_purchase"
    assert result.amount.value == "1000.00"


def test_equity_purchase_validates_against_schema():
    result = parse("Buy one thousand dollars of DBS shares at market price.")
    assert isinstance(result, TransactionDraft)
    _validate_schema(result)


def test_equity_purchase_validates_against_pydantic():
    result = parse("Buy one thousand dollars of DBS shares at market price.")
    assert isinstance(result, TransactionDraft)
    _validate_pydantic(result)


# ── Pattern 4: ambiguous amount → ClarifyingQuestion ─────────────────


def test_ambiguous_amount_returns_clarifying_question():
    """An amount the stub cannot parse → ClarifyingQuestion, never a draft."""
    result = parse("Buy a lot of shares.")
    assert isinstance(result, ClarifyingQuestion)
    assert result.field == "amount"
    assert result.question  # non-empty


def test_ambiguous_amount_never_returns_draft():
    """The result must be a ClarifyingQuestion, not a TransactionDraft."""
    result = parse("Buy a lot of shares.")
    assert not isinstance(result, TransactionDraft)


def test_ambiguous_amount_for_transfer_returns_clarifying_question():
    """'Send some money to John Smith' → ClarifyingQuestion for amount."""
    result = parse("Send some money to John Smith.")
    assert isinstance(result, ClarifyingQuestion)
    assert result.field == "amount"


# ── Pattern 5: unrecognized → raise ──────────────────────────────────


def test_unrecognized_raises():
    """An unrecognized sentence raises rather than returning a draft."""
    with pytest.raises(ParserCannotHandle):
        parse("What's the weather today?")


def test_unrecognized_raises_not_draft():
    """Ensure no draft is returned for unrecognized input."""
    try:
        result = parse("Hello world.")
    except ParserCannotHandle:
        return
    pytest.fail(f"expected ParserCannotHandle, got {type(result).__name__}")


def test_empty_transcript_raises():
    with pytest.raises(ParserCannotHandle):
        parse("")


# ── Fresh id / nonce / timestamps per call ───────────────────────────


def test_fresh_id_per_call():
    """Two calls with the same transcript must produce different ids."""
    d1 = parse("Send fifty dollars to John Smith.")
    d2 = parse("Send fifty dollars to John Smith.")
    assert isinstance(d1, TransactionDraft)
    assert isinstance(d2, TransactionDraft)
    assert d1.id != d2.id
    assert d1.nonce != d2.nonce


# ── Every draft from every pattern validates ─────────────────────────


@pytest.mark.parametrize(
    "transcript",
    [
        "Send fifty dollars to John Smith.",
        "Send fifty dollars to John.",
        "Buy one thousand dollars of DBS shares at market price.",
        "Buy $50.00 of shares.",
    ],
)
def test_every_draft_validates_against_schema(transcript: str):
    result = parse(transcript)
    assert isinstance(result, TransactionDraft), (
        f"expected draft for {transcript!r}, got {type(result).__name__}"
    )
    _validate_schema(result)


@pytest.mark.parametrize(
    "transcript",
    [
        "Send fifty dollars to John Smith.",
        "Send fifty dollars to John.",
        "Buy one thousand dollars of DBS shares at market price.",
        "Buy $50.00 of shares.",
    ],
)
def test_every_draft_validates_against_pydantic(transcript: str):
    result = parse(transcript)
    assert isinstance(result, TransactionDraft)
    _validate_pydantic(result)
