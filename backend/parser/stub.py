"""
Throwaway stub parser.

This is NOT the real AI parser.  It handles a small number of fixed
patterns with plain string matching so that Member 2 (frontend) and
Member 3 (backend) can build against real draft objects now.

The stub handles **one intent per call**.  If the transcript contains
more than one intent (for example " then ", " and then ", or two
separate amounts), it raises :class:`ParserCannotHandle` with a clear
message asking the user to send one request at a time.  It must never
return one item for a multi-intent sentence, because that would silently
drop an intent (violating the ``len(items) == intents_detected`` rule).

Patterns handled:

* "Send fifty dollars to John Smith."
    → clean transfer draft (matches fixtures/drafts/clean_transfer.json)
* "Send fifty dollars to John."
    → ambiguous payee draft (matches fixtures/drafts/ambiguous_payee.json)
* Anything mentioning shares/stock/buy
    → equity purchase draft (matches fixtures/drafts/equity_purchase.json shape)
* Amount the stub cannot parse with confidence
    → ClarifyingQuestion for "amount" (never a draft with a guessed value)
* Multi-intent sentence (more than one action verb, more than one
  amount mention, or a sequencing/addition connector such as
  " then ", " and then ", " also ", " plus ")
    → raise ParserCannotHandle (multi-intent not supported)
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
    PendingQuestion,
)

# ── Multi-intent detection ─────────────────────────────────────────────
# The stub handles one intent per call.  _is_multi_intent checks three
# signals; ANY one is sufficient to decline:
#
#   1. More than one action verb (send, transfer, pay, buy, sell,
#      invest, move, top up).
#   2. More than one amount mention — counting both digit amounts
#      ($50, 50.00) and word amounts ("fifty dollars",
#      "one thousand dollars").  Every occurrence of "dollar"/"dollars"
#      or a "$" amount is one mention.
#   3. A sequencing or addition connector: " then ", " and then ",
#      " also ", " after that ", " afterwards ", " as well ", " plus ".
#
# It is better to refuse a valid single-intent sentence than to return
# a partial draft for a multi-intent one.

_MULTI_INTENT_PHRASES = (
    " then ",
    " and then ",
    " also ",
    " after that ",
    " afterwards ",
    " as well ",
    " plus ",
)

# Action verbs that indicate a financial transaction.  Each occurrence
# of any of these (as a whole word) is one potential intent.
_ACTION_VERBS = (
    "send",
    "transfer",
    "pay",
    "buy",
    "sell",
    "invest",
    "move",
    "top up",
)

# Digit amounts: "$50.00", "$1000.00", or bare "50.00".
_DIGIT_AMOUNT_RE = re.compile(r"(?:\$|\b)(\d+\.\d{2})\b", re.ASCII)

# Word amounts: "fifty dollars", "one thousand dollars", "$50".
# Count every occurrence of "dollar" or "dollars" as one amount mention.
_DOLLAR_WORD_RE = re.compile(r"\bdollars?\b", re.IGNORECASE)
_DOLLAR_SIGN_RE = re.compile(r"\$", re.ASCII)

# ── Known amounts ──────────────────────────────────────────────────────
# The stub recognises a small set of word-numbers and "$XX.XX" patterns.
_AMOUNT_WORDS = {
    "fifty": "50.00",
    "one thousand": "1000.00",
}

_DOLLAR_RE = re.compile(r"\$(\d+\.\d{2})", re.ASCII)


def _count_action_verbs(lower: str) -> int:
    """Count occurrences of action verbs (whole-word matches)."""
    count = 0
    for verb in _ACTION_VERBS:
        count += len(re.findall(rf"\b{re.escape(verb)}\b", lower))
    return count


def _count_amount_mentions(lower: str) -> int:
    """Count amount mentions: digit amounts + word amounts.

    Digit amounts: "$50.00", "50.00".
    Word amounts: every occurrence of "dollar"/"dollars" or a "$" sign.
    """
    # Each "$" amount is one mention.
    dollar_sign_amounts = len(_DIGIT_AMOUNT_RE.findall(lower))

    # Each "dollar"/"dollars" word is one mention (covers "fifty dollars",
    # "one thousand dollars", etc.).
    dollar_words = len(_DOLLAR_WORD_RE.findall(lower))

    # A bare "$" not followed by digits (e.g. "fifty $") is rare but
    # count it via _DOLLAR_SIGN_RE minus those already captured.
    bare_dollar_signs = len(_DOLLAR_SIGN_RE.findall(lower))

    # Take the larger of digit-amount count and bare-dollar-sign count
    # to avoid double counting "$50" as both a sign and a digit amount.
    digit_mentions = max(dollar_sign_amounts, bare_dollar_signs)

    return digit_mentions + dollar_words


def _is_multi_intent(transcript: str) -> bool:
    """Return True if the transcript likely describes more than one intent.

    Checks three signals; ANY one is sufficient:
      1. More than one action verb.
      2. More than one amount mention.
      3. A sequencing or addition connector.
    """
    lower = transcript.lower()

    # ── Signal 1: more than one action verb ─────────────────────────
    if _count_action_verbs(lower) > 1:
        return True

    # ── Signal 2: more than one amount mention ──────────────────────
    if _count_amount_mentions(lower) > 1:
        return True

    # ── Signal 3: sequencing / addition connector ───────────────────
    for phrase in _MULTI_INTENT_PHRASES:
        if phrase in lower:
            return True

    return False


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


def _fresh_question_id(field: str) -> str:
    """Generate a short unique question id, e.g. 'q-amount-a1b2c3d4'."""
    return f"q-{field}-{uuid.uuid4().hex[:8]}"


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


def _clarifying_question(
    field: str,
    question: str,
    transcript: str,
    partial: dict | None = None,
) -> tuple[ClarifyingQuestion, PendingQuestion]:
    """Build a ClarifyingQuestion and its server-only PendingQuestion.

    ``partial`` carries the fields already resolved from the transcript
    (e.g. ``intent_type``, ``source_account``, ``payee``).  The answer
    to the question fills the remaining field so a draft can be produced.
    """
    qid = _fresh_question_id(field)
    created, expires = _fresh_timestamps()
    question_item = ClarifyingQuestion(
        question_id=qid,
        field=field,
        question=question,
    )
    pending = PendingQuestion(
        question_id=qid,
        field=field,
        original_transcript=transcript,
        partial=partial or {},
        created_at=created,
        expires_at=expires,
    )
    return question_item, pending


def _question_result(
    field: str,
    question: str,
    transcript: str,
    partial: dict | None = None,
) -> ParseResult:
    """Build a one-item ParseResult with a ClarifyingQuestion and its pending."""
    item, pending = _clarifying_question(field, question, transcript, partial)
    return ParseResult(
        items=[item],
        intents_detected=1,
        pending={item.question_id: pending},
    )


def parse(transcript: str) -> ParseResult:
    """
    Parse a user transcript into a :class:`ParseResult`.

    The stub handles one intent per call.  A multi-intent transcript
    raises :class:`ParserCannotHandle` rather than silently dropping
    one of the intents.

    See module docstring for the patterns handled.
    """
    if not transcript or not transcript.strip():
        raise ParserCannotHandle("empty transcript")

    # ── Multi-intent guard ───────────────────────────────────────────
    # The stub handles one intent only.  If the transcript describes
    # more than one, decline so no intent is silently dropped.
    if _is_multi_intent(transcript):
        raise ParserCannotHandle(
            "Multiple intents detected in the transcript. "
            "Please send one request at a time."
        )

    lower = transcript.lower().strip()

    # ── Equity / shares / stock / buy ────────────────────────────────
    if any(kw in lower for kw in ("share", "stock", "buy")):
        amount = _parse_amount(transcript)
        if amount is None:
            return _question_result(
                "amount",
                "How much would you like to invest?",
                transcript,
                partial={
                    "intent_type": "equity_purchase",
                    "source_account": "acct-001-2345",
                    "ticker": "D05.SI",
                    "order_type": "market",
                },
            )
        return ParseResult(
            items=[_equity_purchase(transcript, amount)],
            intents_detected=1,
        )

    # ── Transfer patterns ────────────────────────────────────────────
    if "send" in lower and ("dollar" in lower or "money" in lower):
        amount = _parse_amount(transcript)
        if amount is None:
            # Per CONTRACT.md §2, amount cannot be in unresolved.
            # The parser must ask before producing any draft.
            return _question_result(
                "amount",
                "How much would you like to send?",
                transcript,
                partial={
                    "intent_type": "transfer",
                    "source_account": "acct-001-2345",
                    "payee": {
                        "id": "payee-001",
                        "display_name": "John Smith",
                        "masked_account": "****1234",
                    } if "john smith" in lower else None,
                },
            )

        # "John Smith" (full name) → clean transfer
        if "john smith" in lower:
            return ParseResult(
                items=[_clean_transfer(transcript)],
                intents_detected=1,
            )

        # "John" alone (ambiguous — two saved Johns)
        if "john" in lower and "john smith" not in lower:
            return ParseResult(
                items=[_ambiguous_payee(transcript)],
                intents_detected=1,
            )

    # ── Unrecognized ─────────────────────────────────────────────────
    raise ParserCannotHandle(
        f"stub parser cannot handle this transcript: {transcript!r}"
    )
