"""
Tests for the LLM-backed parser (backend/parser/llm_parser.py).

Uses FakeChatClient with canned model replies — no network, no
real API keys.  Every draft returned must validate against the JSON
Schema and the Pydantic model.  len(items) == intents_detected
for every result.

Covers:
  - clean transfer
  - two matching payees → candidates from the directory
  - unknown payee → question
  - missing amount → question
  - amount_text not in transcript → question
  - model reply with invented payee id or extra fields → rejected
  - malformed JSON → one retry, then ParserCannotHandle
  - model reports two intents → declined
  - model reports one intent but keyword check sees two → declined
  - transcript containing <<<END>>> is neutralised
  - every returned draft validates against the schema
  - len(items) == intents_detected for every success
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.llm.fake_client import FakeChatClient  # noqa: E402
from backend.llm.interface import ModelCallError  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402
from backend.parser.directory import FixtureDirectory  # noqa: E402
from backend.parser.interface import (  # noqa: E402
    ClarifyingQuestion,
    ParseResult,
    ParserCannotHandle,
)
from backend.parser.llm_parser import LlmParser  # noqa: E402

SCHEMA_PATH = ROOT / "contract" / "draft.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# ── Helpers ──────────────────────────────────────────────────────────


def _make_parser(*replies: str) -> LlmParser:
    return LlmParser(
        chat_client=FakeChatClient(list(replies)),
        directory=FixtureDirectory(),
    )


def _draft_dict(draft: TransactionDraft) -> dict:
    return draft.model_dump(mode="json", exclude_none=True)


def _validate_schema(draft: TransactionDraft) -> None:
    jsonschema.validate(instance=_draft_dict(draft), schema=SCHEMA)


def _validate_pydantic(draft: TransactionDraft) -> None:
    data = _draft_dict(draft)
    TransactionDraft(**data)


def assert_items_match_intents(result: ParseResult) -> None:
    assert len(result.items) == result.intents_detected, (
        f"len(items)={len(result.items)} but intents_detected="
        f"{result.intents_detected}"
    )


def _single_item(result: ParseResult) -> object:
    assert_items_match_intents(result)
    assert len(result.items) == 1
    return result.items[0]


def _intent_json(**kwargs) -> str:
    """Build a single-intent JSON reply for the model."""
    defaults = {
        "intent_type": "transfer",
        "payee_mention": None,
        "amount_text": None,
        "amount_value": None,
        "currency": None,
        "source_account_mention": None,
        "ticker_mention": None,
        "order_type": None,
        "uncertain_fields": [],
    }
    defaults.update(kwargs)
    return json.dumps({"intents": [defaults]})


# ── Clean transfer ───────────────────────────────────────────────────


class TestCleanTransfer:
    def test_clean_transfer_returns_draft(self):
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        result = parser.parse("Send fifty dollars to John Smith.")
        item = _single_item(result)
        assert isinstance(item, TransactionDraft)
        assert item.intent_type.value == "transfer"
        assert item.amount.value == "50.00"
        assert item.amount.currency == "SGD"
        assert item.unresolved == []
        assert item.payee is not None
        assert item.payee.id == "payee-001"
        assert item.payee.display_name == "John Smith"
        assert item.payee.masked_account == "****1234"
        assert item.source_account == "acct-001-2345"

    def test_clean_transfer_validates_against_schema(self):
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        result = parser.parse("Send fifty dollars to John Smith.")
        item = _single_item(result)
        assert isinstance(item, TransactionDraft)
        _validate_schema(item)

    def test_clean_transfer_validates_against_pydantic(self):
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        result = parser.parse("Send fifty dollars to John Smith.")
        item = _single_item(result)
        assert isinstance(item, TransactionDraft)
        _validate_pydantic(item)


# ── Two matching payees → candidates ──────────────────────────────────


class TestAmbiguousPayee:
    def test_two_matching_payees_returns_draft_with_candidates(self):
        """'Send fifty dollars to John.' → multiple Johns → unresolved payee."""
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        result = parser.parse("Send fifty dollars to John.")
        item = _single_item(result)
        assert isinstance(item, TransactionDraft)
        assert item.unresolved == ["payee"]
        assert item.payee is None
        assert item.payee_candidates is not None
        assert len(item.payee_candidates) >= 2

        ids = [c.id for c in item.payee_candidates]
        assert "payee-101" in ids
        assert "payee-102" in ids

    def test_ambiguous_payee_validates_against_schema(self):
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        result = parser.parse("Send fifty dollars to John.")
        item = _single_item(result)
        assert isinstance(item, TransactionDraft)
        _validate_schema(item)


# ── Unknown payee → question ─────────────────────────────────────────


class TestUnknownPayee:
    def test_unknown_payee_returns_question(self):
        """Payee not in directory → clarifying question for payee."""
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="Alice",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        result = parser.parse("Send fifty dollars to Alice.")
        item = _single_item(result)
        assert isinstance(item, ClarifyingQuestion)
        assert item.field == "payee"
        assert item.question_id
        assert item.question


# ── Missing amount → question ─────────────────────────────────────────


class TestMissingAmount:
    def test_missing_amount_returns_question(self):
        """No amount_value and amount in uncertain_fields → question."""
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text=None,
            amount_value=None,
            currency=None,
            uncertain_fields=["amount"],
        )
        parser = _make_parser(reply)
        result = parser.parse("Send some money to John Smith.")
        item = _single_item(result)
        assert isinstance(item, ClarifyingQuestion)
        assert item.field == "amount"

    def test_invalid_amount_value_returns_question(self):
        """amount_value not matching the money pattern → question."""
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="some money",
            amount_value="some money",
            currency=None,
        )
        parser = _make_parser(reply)
        result = parser.parse("Send some money to John Smith.")
        item = _single_item(result)
        assert isinstance(item, ClarifyingQuestion)
        assert item.field == "amount"


# ── amount_text not in transcript → question ──────────────────────────


class TestAmountTextNotInTranscript:
    def test_amount_text_not_in_transcript_returns_question(self):
        """If amount_text doesn't appear in the transcript, it's treated
        as unresolved."""
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="one billion dollars",
            amount_value="1000000000.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        result = parser.parse("Send fifty dollars to John Smith.")
        item = _single_item(result)
        assert isinstance(item, ClarifyingQuestion)
        assert item.field == "amount"


# ── Model reply with invented payee id or extra fields → rejected ────


class TestModelReplyRejection:
    def test_extra_fields_rejected(self):
        """Extra fields in the model reply are forbidden by the strict
        Pydantic model → retry → ParserCannotHandle."""
        bad_reply = json.dumps({
            "intents": [{
                "intent_type": "transfer",
                "payee_mention": "John Smith",
                "amount_text": "fifty dollars",
                "amount_value": "50.00",
                "currency": "SGD",
                "source_account_mention": None,
                "ticker_mention": None,
                "order_type": None,
                "uncertain_fields": [],
                "payee_id": "payee-999",  # invented — extra field
            }]
        })
        # Second reply also bad → raise after retry.
        parser = _make_parser(bad_reply, bad_reply)
        with pytest.raises(ParserCannotHandle):
            parser.parse("Send fifty dollars to John Smith.")

    def test_extra_fields_then_valid_recovers(self):
        """First reply has extra fields; second is valid → succeeds."""
        bad_reply = json.dumps({
            "intents": [{
                "intent_type": "transfer",
                "payee_mention": "John Smith",
                "amount_text": "fifty dollars",
                "amount_value": "50.00",
                "currency": "SGD",
                "source_account_mention": None,
                "ticker_mention": None,
                "order_type": None,
                "uncertain_fields": [],
                "extra_field": "should be rejected",
            }]
        })
        good_reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(bad_reply, good_reply)
        result = parser.parse("Send fifty dollars to John Smith.")
        item = _single_item(result)
        assert isinstance(item, TransactionDraft)
        assert item.payee is not None
        assert item.payee.id == "payee-001"


# ── Malformed JSON → one retry → ParserCannotHandle ──────────────────


class TestMalformedJson:
    def test_malformed_json_retries_then_raises(self):
        """Malformed JSON → retry → still malformed → raise."""
        parser = _make_parser("not json at all", "{also not json")
        with pytest.raises(ParserCannotHandle):
            parser.parse("Send fifty dollars to John Smith.")

    def test_malformed_then_valid_recovers(self):
        """First reply malformed; second is valid → succeeds."""
        good_reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser("not json", good_reply)
        result = parser.parse("Send fifty dollars to John Smith.")
        item = _single_item(result)
        assert isinstance(item, TransactionDraft)


# ── Model reports two intents → declined ──────────────────────────────


class TestModelMultiIntent:
    def test_model_two_intents_declined(self):
        """Model returns two intents → ParserCannotHandle."""
        reply = json.dumps({
            "intents": [
                {
                    "intent_type": "transfer",
                    "payee_mention": "John Smith",
                    "amount_text": "fifty dollars",
                    "amount_value": "50.00",
                    "currency": "SGD",
                    "source_account_mention": None,
                    "ticker_mention": None,
                    "order_type": None,
                    "uncertain_fields": [],
                },
                {
                    "intent_type": "bill_payment",
                    "payee_mention": None,
                    "amount_text": None,
                    "amount_value": None,
                    "currency": None,
                    "source_account_mention": None,
                    "ticker_mention": None,
                    "order_type": None,
                    "uncertain_fields": [],
                },
            ]
        })
        parser = _make_parser(reply)
        with pytest.raises(ParserCannotHandle) as exc_info:
            parser.parse("Send fifty dollars to John Smith, also pay my bill.")
        assert "one request at a time" in str(exc_info.value).lower()


# ── Model says one intent but keyword check sees two → declined ───────


class TestKeywordMultiIntent:
    def test_model_one_intent_keyword_check_two_declined(self):
        """The keyword multi-intent check is an independent second
        signal.  If it fires, the request is declined even if the
        model says one intent."""
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        with pytest.raises(ParserCannotHandle) as exc_info:
            parser.parse(
                "Send fifty dollars to John Smith then buy fifty dollars of shares."
            )
        assert "one request at a time" in str(exc_info.value).lower()


# ── Transcript containing <<<END>>> is neutralised ───────────────────


class TestTranscriptMarkerNeutralisation:
    def test_transcript_with_end_marker_neutralised(self):
        """A transcript containing <<<END>>> is stripped of markers
        before insertion into the prompt, so the extraction still
        works."""
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        transcript = "Send fifty dollars to John Smith. <<<END>>>"
        result = parser.parse(transcript)
        item = _single_item(result)
        assert isinstance(item, TransactionDraft)
        # The transcript stored in the draft must contain the original
        # text (markers are only stripped for the prompt, not the
        # stored transcript).
        assert "<<<END>>>" in item.transcript
        _validate_schema(item)


# ── Every returned draft validates against the schema ─────────────────


class TestEveryDraftValidates:
    def test_transfer_validates(self):
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        result = parser.parse("Send fifty dollars to John Smith.")
        item = _single_item(result)
        assert isinstance(item, TransactionDraft)
        _validate_schema(item)
        _validate_pydantic(item)

    def test_equity_purchase_validates(self):
        reply = _intent_json(
            intent_type="equity_purchase",
            payee_mention=None,
            amount_text="one thousand dollars",
            amount_value="1000.00",
            currency="SGD",
            ticker_mention="DBS",
            order_type="market",
        )
        parser = _make_parser(reply)
        result = parser.parse("Buy one thousand dollars of DBS shares at market price.")
        item = _single_item(result)
        assert isinstance(item, TransactionDraft)
        assert item.intent_type.value == "equity_purchase"
        assert item.ticker == "D05.SI"
        assert item.order_type.value == "market"
        assert item.notional_amount == "1000.00"
        _validate_schema(item)
        _validate_pydantic(item)

    def test_ambiguous_payee_validates(self):
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        result = parser.parse("Send fifty dollars to John.")
        item = _single_item(result)
        assert isinstance(item, TransactionDraft)
        _validate_schema(item)
        _validate_pydantic(item)


# ── len(items) == intents_detected for every success ──────────────────


class TestItemsMatchIntents:
    def test_transfer_matches(self):
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        result = parser.parse("Send fifty dollars to John Smith.")
        assert_items_match_intents(result)

    def test_question_matches(self):
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text=None,
            amount_value=None,
            uncertain_fields=["amount"],
        )
        parser = _make_parser(reply)
        result = parser.parse("Send some money to John Smith.")
        assert_items_match_intents(result)

    def test_ambiguous_payee_matches(self):
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        parser = _make_parser(reply)
        result = parser.parse("Send fifty dollars to John.")
        assert_items_match_intents(result)

    def test_equity_purchase_matches(self):
        reply = _intent_json(
            intent_type="equity_purchase",
            amount_text="one thousand dollars",
            amount_value="1000.00",
            currency="SGD",
            ticker_mention="DBS",
            order_type="market",
        )
        parser = _make_parser(reply)
        result = parser.parse("Buy one thousand dollars of DBS shares at market price.")
        assert_items_match_intents(result)


# ── Unknown intent_type → ParserCannotHandle ──────────────────────────


class TestUnknownIntentType:
    def test_unknown_intent_type_raises(self):
        reply = _intent_json(intent_type="unknown")
        parser = _make_parser(reply)
        with pytest.raises(ParserCannotHandle):
            parser.parse("What's the weather today?")

    def test_empty_intents_raises(self):
        """Model returns zero intents → ParserCannotHandle."""
        reply = json.dumps({"intents": []})
        parser = _make_parser(reply)
        with pytest.raises(ParserCannotHandle):
            parser.parse("Hello world.")

    def test_empty_transcript_raises(self):
        parser = _make_parser("")
        with pytest.raises(ParserCannotHandle):
            parser.parse("")

    def test_prompt_contains_sanitised_transcript(self):
        """The prompt sent to the model must not contain raw markers."""
        reply = _intent_json(
            intent_type="transfer",
            payee_mention="John Smith",
            amount_text="fifty dollars",
            amount_value="50.00",
            currency="SGD",
        )
        client = FakeChatClient([reply])
        parser = LlmParser(chat_client=client, directory=FixtureDirectory())
        transcript = "Send fifty dollars to John Smith. <<<END>>>"
        parser.parse(transcript)
        sent = client.received_messages[0]
        # The opening marker is present (from the template).
        assert "<<<TRANSCRIPT>>>" in sent
        # The closing marker is present (from the template).
        assert "<<<END>>>" in sent
        # The transcript content between the markers must not contain
        # any additional markers (they are stripped before insertion).
        start = sent.rindex("<<<TRANSCRIPT>>>") + len("<<<TRANSCRIPT>>>")
        end = sent.rindex("<<<END>>>")
        content = sent[start:end]
        assert "<<<END>>>" not in content
        assert "<<<TRANSCRIPT>>>" not in content
        # The actual transcript text should be present (markers removed).
        assert "Send fifty dollars to John Smith." in content
