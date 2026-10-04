"""
Throwaway stub parser.

This is NOT the real AI parser.  It handles a small number of fixed
patterns with plain string matching so that Member 2 (frontend) and
Member 3 (backend) can build against real draft objects now.

Patterns handled:

* "Send fifty dollars to John Smith."
    → clean transfer draft (matches fixtures/drafts/clean_transfer.json)
* "Send fifty dollars to John."
    → ambiguous payee draft (matches fixtures/drafts/ambiguous_payee.json)
* Anything mentioning shares/stock/buy
    → equity purchase draft (matches fixtures/drafts/equity_purchase.json shape)
* Amount the stub cannot parse with confidence
    → ClarifyingQuestion for "amount" (never a draft with a guessed value)
* Anything else
    → raise ParserCannotHandle

All drafts returned are valid against contract/draft.schema.json and
backend/models/draft.py.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from backend.models.draft import (
    Money,
    Payee,
    PayeeCandidate,
    TransactionDraft,
)
from backend.parser.interface import (
    ClarifyingQuestion,
    ParserCannotHandle,
    ParseResult,
)

# ── Known amounts ──────────────────────────────────────────────────────
# The stub recognises a small set of word-numbers and "$XX.XX" patterns.
_AMOUNT_WORDS = {
    "fifty": "50.00",
    "one thousand": "1000.00",
}

_DOLLAR_RE = re.compile(r"\$(\d+\.\d{2})", re.ASCII)


def _parse_amount(transcript: str) -> str | None:
    """Return a decimal-string amount or ``None`` if not confident."""
    lower = transcript.lower()

    # "$50.00" style
    m = _DOLLAR_RE.search(lower)
    if m:
        return m.group(1)

    # "fifty dollars" style
    for word, value in _AMOUNT_WORDS.items():
        if word in lower and "dollar" in lower:
            return value

    return None


def _fresh_id() -> str:
    return str(uuid.uuid4())


def _fresh_nonce() -> str:
    return uuid.uuid4().hex


def _fresh_timestamps() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now, now + timedelta(minutes=5)


def _clean_transfer(transcript: str) -> TransactionDraft:
    """Match fixtures/drafts/clean_transfer.json (field values, not id/timestamps)."""
    created, expires = _fresh_timestamps()
    return TransactionDraft(
        id=_fresh_id(),
        created_at=created,
        expires_at=expires,
        nonce=_fresh_nonce(),
        intent_type="transfer",
        source_account="acct-001-2345",
        amount=Money(value="50.00", currency="SGD"),
        confidence=0.95,
        unresolved=[],
        transcript=transcript,
        payee=Payee(
            id="payee-001",
            display_name="John Smith",
            masked_account="****1234",
        ),
    )


def _ambiguous_payee(transcript: str) -> TransactionDraft:
    """Match fixtures/drafts/ambiguous_payee.json (field values, not id/timestamps)."""
    created, expires = _fresh_timestamps()
    return TransactionDraft(
        id=_fresh_id(),
        created_at=created,
        expires_at=expires,
        nonce=_fresh_nonce(),
        intent_type="transfer",
        source_account="acct-001-2345",
        amount=Money(value="50.00", currency="SGD"),
        confidence=0.4,
        unresolved=["payee"],
        transcript=transcript,
        payee_candidates=[
            PayeeCandidate(
                id="payee-101",
                display_name="John Doe",
                masked_account="****4521",
            ),
            PayeeCandidate(
                id="payee-102",
                display_name="John Smith",
                masked_account="****8892",
            ),
        ],
    )


def _equity_purchase(transcript: str, amount_value: str) -> TransactionDraft:
    """Match fixtures/drafts/equity_purchase.json shape."""
    created, expires = _fresh_timestamps()
    return TransactionDraft(
        id=_fresh_id(),
        created_at=created,
        expires_at=expires,
        nonce=_fresh_nonce(),
        intent_type="equity_purchase",
        source_account="acct-001-2345",
        amount=Money(value=amount_value, currency="SGD"),
        confidence=0.92,
        unresolved=[],
        transcript=transcript,
        ticker="D05.SI",
        notional_amount=amount_value,
        order_type="market",
    )


def parse(transcript: str) -> ParseResult:
    """
    Parse a user transcript into a draft or a clarifying question.

    See module docstring for the patterns handled.
    """
    if not transcript or not transcript.strip():
        raise ParserCannotHandle("empty transcript")

    lower = transcript.lower().strip()

    # ── Equity / shares / stock / buy ────────────────────────────────
    if any(kw in lower for kw in ("share", "stock", "buy")):
        amount = _parse_amount(transcript)
        if amount is None:
            return ClarifyingQuestion(
                field="amount",
                question="How much would you like to invest?",
            )
        return _equity_purchase(transcript, amount)

    # ── Transfer patterns ────────────────────────────────────────────
    if "send" in lower and ("dollar" in lower or "money" in lower):
        amount = _parse_amount(transcript)
        if amount is None:
            # Per CONTRACT.md §2, amount cannot be in unresolved.
            # The parser must ask before producing any draft.
            return ClarifyingQuestion(
                field="amount",
                question="How much would you like to send?",
            )

        # "John Smith" (full name) → clean transfer
        if "john smith" in lower:
            return _clean_transfer(transcript)

        # "John" alone (ambiguous — two saved Johns)
        if "john" in lower and "john smith" not in lower:
            return _ambiguous_payee(transcript)

    # ── Unrecognized ─────────────────────────────────────────────────
    raise ParserCannotHandle(
        f"stub parser cannot handle this transcript: {transcript!r}"
    )
