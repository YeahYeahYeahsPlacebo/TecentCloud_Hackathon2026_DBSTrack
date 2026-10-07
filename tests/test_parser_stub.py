"""
Tests for the throwaway stub parser (backend/parser/stub.py).

Every draft the stub returns must validate against the JSON Schema and the
Pydantic model.  Unrecognized transcripts must raise, and ambiguous amounts
must return a ClarifyingQuestion — never a draft with a guessed number.

The new ``parse()`` contract returns a ``ParseResult`` with ``items``
(a list of drafts or clarifying questions) and ``intents_detected`` (an
int).  ``len(items)`` must equal ``intents_detected`` at all times.
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
    ParseResult,
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


def assert_items_match_intents(result: ParseResult) -> None:
    """Every parser test calls this after a successful parse().

    Asserts ``len(result.items) == result.intents_detected``.
    """
    assert len(result.items) == result.intents_detected, (
        f"len(items)={len(result.items)} but intents_detected="
        f"{result.intents_detected} — an intent was silently dropped"
    )


def _single_item(result: ParseResult) -> object:
    """Return the single item from a one-intent ParseResult."""
    assert_items_match_intents(result)
    assert len(result.items) == 1, (
        f"expected 1 item, got {len(result.items)}"
    )
    return result.items[0]


# ── Pattern 1: clean transfer ────────────────────────────────────────


def test_clean_transfer_returns_draft():
    """'Send fifty dollars to John Smith.' → clean transfer draft."""
    result = parse("Send fifty dollars to John Smith.")
    item = _single_item(result)
    assert isinstance(item, TransactionDraft)

    assert item.intent_type.value == "transfer"
    assert item.amount.value == "50.00"
    assert item.amount.currency == "SGD"
    assert item.source_account == "acct-001-2345"
    assert item.unresolved == []
    assert item.payee is not None
    assert item.payee.id == "payee-001"
    assert item.payee.display_name == "John Smith"
    assert item.payee.masked_account == "****1234"


def test_clean_transfer_validates_against_schema():
    result = parse("Send fifty dollars to John Smith.")
    item = _single_item(result)
    assert isinstance(item, TransactionDraft)
    _validate_schema(item)


def test_clean_transfer_validates_against_pydantic():
    result = parse("Send fifty dollars to John Smith.")
    item = _single_item(result)
    assert isinstance(item, TransactionDraft)
    _validate_pydantic(item)


# ── Pattern 2: ambiguous payee ────────────────────────────────────────


def test_ambiguous_payee_returns_draft_with_unresolved():
    """'Send fifty dollars to John.' → ambiguous payee draft."""
    result = parse("Send fifty dollars to John.")
    item = _single_item(result)
    assert isinstance(item, TransactionDraft)

    assert item.intent_type.value == "transfer"
    assert item.unresolved == ["payee"]
    assert item.payee is None
    assert item.payee_candidates is not None
    assert len(item.payee_candidates) == 2

    ids = [c.id for c in item.payee_candidates]
    assert "payee-101" in ids
    assert "payee-102" in ids

    names = [c.display_name for c in item.payee_candidates]
    assert "John Doe" in names
    assert "John Smith" in names

    masked = [c.masked_account for c in item.payee_candidates]
    assert "****4521" in masked
    assert "****8892" in masked


def test_ambiguous_payee_validates_against_schema():
    result = parse("Send fifty dollars to John.")
    item = _single_item(result)
    assert isinstance(item, TransactionDraft)
    _validate_schema(item)


def test_ambiguous_payee_validates_against_pydantic():
    result = parse("Send fifty dollars to John.")
    item = _single_item(result)
    assert isinstance(item, TransactionDraft)
    _validate_pydantic(item)


# ── Pattern 3: equity purchase ───────────────────────────────────────


def test_equity_purchase_returns_draft():
    """Anything mentioning shares/stock/buy → equity purchase draft."""
    result = parse("Buy one thousand dollars of DBS shares at market price.")
    item = _single_item(result)
    assert isinstance(item, TransactionDraft)

    assert item.intent_type.value == "equity_purchase"
    assert item.source_account == "acct-001-2345"
    assert item.ticker == "D05.SI"
    assert item.order_type.value == "market"
    assert item.unresolved == []


def test_equity_purchase_with_dollar_amount():
    """'$1000.00 of shares' → equity purchase with that amount."""
    result = parse("Buy $1000.00 of DBS shares.")
    item = _single_item(result)
    assert isinstance(item, TransactionDraft)
    assert item.intent_type.value == "equity_purchase"
    assert item.amount.value == "1000.00"


def test_equity_purchase_validates_against_schema():
    result = parse("Buy one thousand dollars of DBS shares at market price.")
    item = _single_item(result)
    assert isinstance(item, TransactionDraft)
    _validate_schema(item)


def test_equity_purchase_validates_against_pydantic():
    result = parse("Buy one thousand dollars of DBS shares at market price.")
    item = _single_item(result)
    assert isinstance(item, TransactionDraft)
    _validate_pydantic(item)


# ── Pattern 4: ambiguous amount → ClarifyingQuestion ─────────────────


def test_ambiguous_amount_returns_clarifying_question():
    """An amount the stub cannot parse → ClarifyingQuestion, never a draft."""
    result = parse("Buy a lot of shares.")
    item = _single_item(result)
    assert isinstance(item, ClarifyingQuestion)
    assert item.field == "amount"
    assert item.question  # non-empty
    assert item.question_id  # non-empty


def test_ambiguous_amount_never_returns_draft():
    """The result item must be a ClarifyingQuestion, not a TransactionDraft."""
    result = parse("Buy a lot of shares.")
    item = _single_item(result)
    assert not isinstance(item, TransactionDraft)


def test_ambiguous_amount_for_transfer_returns_clarifying_question():
    """'Send some money to John Smith' → ClarifyingQuestion for amount."""
    result = parse("Send some money to John Smith.")
    item = _single_item(result)
    assert isinstance(item, ClarifyingQuestion)
    assert item.field == "amount"


# ── Pattern 5: unrecognized → raise ──────────────────────────────────


def test_unrecognized_raises():
    """An unrecognized sentence raises rather than returning a draft."""
    with pytest.raises(ParserCannotHandle):
        parse("What's the weather today?")


def test_unrecognized_raises_not_result():
    """Ensure no result is returned for unrecognized input."""
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
    r1 = parse("Send fifty dollars to John Smith.")
    r2 = parse("Send fifty dollars to John Smith.")
    d1 = _single_item(r1)
    d2 = _single_item(r2)
    assert isinstance(d1, TransactionDraft)
    assert isinstance(d2, TransactionDraft)
    assert d1.id != d2.id
    assert d1.nonce != d2.nonce


# ── Every draft from every pattern validates ─────────────────────────


SUPPORTED_DRAFT_TRANSCRIPTS = [
    "Send fifty dollars to John Smith.",
    "Send fifty dollars to John.",
    "Buy one thousand dollars of DBS shares at market price.",
    "Buy $50.00 of shares.",
]

SUPPORTED_QUESTION_TRANSCRIPTS = [
    "Buy a lot of shares.",
    "Send some money to John Smith.",
]

ALL_SUPPORTED_TRANSCRIPTS = SUPPORTED_DRAFT_TRANSCRIPTS + SUPPORTED_QUESTION_TRANSCRIPTS


@pytest.mark.parametrize("transcript", SUPPORTED_DRAFT_TRANSCRIPTS)
def test_every_draft_validates_against_schema(transcript: str):
    result = parse(transcript)
    item = _single_item(result)
    assert isinstance(item, TransactionDraft), (
        f"expected draft for {transcript!r}, got {type(item).__name__}"
    )
    _validate_schema(item)


@pytest.mark.parametrize("transcript", SUPPORTED_DRAFT_TRANSCRIPTS)
def test_every_draft_validates_against_pydantic(transcript: str):
    result = parse(transcript)
    item = _single_item(result)
    assert isinstance(item, TransactionDraft)
    _validate_pydantic(item)


# ── ParseResult contract: len(items) == intents_detected ─────────────


@pytest.mark.parametrize("transcript", ALL_SUPPORTED_TRANSCRIPTS)
def test_items_match_intents_detected(transcript: str):
    """The helper holds for every supported sentence."""
    result = parse(transcript)
    assert_items_match_intents(result)


# ── ClarifyingQuestion has question_id and no draft ──────────────────


@pytest.mark.parametrize("transcript", SUPPORTED_QUESTION_TRANSCRIPTS)
def test_clarifying_question_has_question_id(transcript: str):
    """A ClarifyingQuestion must have a non-empty question_id."""
    result = parse(transcript)
    item = _single_item(result)
    assert isinstance(item, ClarifyingQuestion)
    assert item.question_id
    assert item.field
    assert item.question


@pytest.mark.parametrize("transcript", SUPPORTED_QUESTION_TRANSCRIPTS)
def test_clarifying_question_has_no_draft(transcript: str):
    """A question item never carries a draft."""
    result = parse(transcript)
    item = _single_item(result)
    assert isinstance(item, ClarifyingQuestion)
    assert not isinstance(item, TransactionDraft)


def test_question_id_format():
    """question_id should look like 'q-<field>-<8 hex>'."""
    result = parse("Buy a lot of shares.")
    item = _single_item(result)
    assert isinstance(item, ClarifyingQuestion)
    assert item.question_id.startswith("q-amount-")
    # 8 hex chars after the last dash
    suffix = item.question_id.rsplit("-", 1)[-1]
    assert len(suffix) == 8
    int(suffix, 16)  # valid hex


# ── Multi-intent sentences must raise ─────────────────────────────────


MULTI_INTENT_TRANSCRIPTS = [
    # ── Two bug sentences that previously returned a partial draft ──
    "Send fifty dollars to John Smith and buy one thousand dollars of DBS shares.",
    "Send fifty dollars to John Smith, also pay my bill.",
    # ── Original multi-intent sentences ──
    "Send fifty dollars to John Smith then buy fifty dollars of shares.",
    "Send fifty dollars to John Smith and then buy shares.",
    "Send $50.00 to John Smith, then send $100.00 to Jane.",
    "Buy $50.00 of shares and then buy $100.00 of shares.",
    "Transfer one thousand dollars to Jane, then buy five hundred dollars of ES3 shares.",
    # ── Three new: word amounts joined by "and" ──
    "Send fifty dollars and send one thousand dollars to John Smith.",
    # ── "also" connector ──
    "Buy fifty dollars of shares, also sell one hundred dollars of stock.",
    # ── Two verbs, no amounts ──
    "Pay my bill and send money to Jane.",
]


@pytest.mark.parametrize("transcript", MULTI_INTENT_TRANSCRIPTS)
def test_multi_intent_raises(transcript: str):
    """A multi-intent sentence must raise, never partially draft."""
    with pytest.raises(ParserCannotHandle) as exc_info:
        parse(transcript)
    assert "one request at a time" in str(exc_info.value).lower()


@pytest.mark.parametrize("transcript", MULTI_INTENT_TRANSCRIPTS)
def test_multi_intent_never_returns_result(transcript: str):
    """A multi-intent sentence must not return a ParseResult at all."""
    try:
        result = parse(transcript)
    except ParserCannotHandle:
        return
    pytest.fail(
        f"expected ParserCannotHandle for {transcript!r}, "
        f"got ParseResult with {len(result.items)} item(s)"
    )
