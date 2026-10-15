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

Speech-style input is handled the same way the LLM parser handles it
(see :mod:`backend.parser.speech_norm`): fillers are removed for matching,
spoken amounts ("a grand", "fifteen") are read by code, a missing "to" is
tolerated, and the draft's ``transcript`` is always the exact submitted
text.  Payees are matched through the directory, never by substring.

Patterns handled:

* "Send fifty dollars to John Smith."  (or "send fifty dollars john smith",
  "uh send a grand to jane lah", "SEND FIFTY DOLLARS TO JANE")
    -> clean transfer draft to the one directory payee the words resolve to
* "Send fifty dollars to John."
    -> ambiguous payee draft (matches fixtures/drafts/ambiguous_payee.json
       in shape).  The stub always offers the two demo Johns, payee-101 and
       payee-102, which tests/test_frontend_compat.py relies on.
* A recipient that matches no payee ("mum", "my wife", "Bob")
    -> ClarifyingQuestion for "payee" (never a guessed draft)
* Anything mentioning shares/stock/buy
    -> equity purchase draft (matches fixtures/drafts/equity_purchase.json shape)
* An amount that is missing, vague ("some", "a few hundred"), negative,
  over-precise or ambiguous
    -> ClarifyingQuestion for "amount" (never a draft with a guessed value)
* Multi-intent sentence (more than one action verb, more than one
  amount mention, or a sequencing/addition connector such as
  " then ", " and then ", " also ", " plus ")
    -> raise ParserCannotHandle (multi-intent not supported)
* Anything else
    -> raise ParserCannotHandle

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
from backend.llm.text import sanitise
from backend.parser.amount_rules import (
    has_excess_decimals,
    is_negative_amount,
    resolve_currency,
)
from backend.parser.directory import FixtureDirectory, match_payees
from backend.parser.speech_norm import (
    directory_name_words,
    normalise,
    payee_phrase,
    person_reference,
    spoken_amount,
)
from backend.parser.interface import (
    ClarifyingQuestion,
    ParserCannotHandle,
    ParseResult,
    PendingQuestion,
)

# Maximum transcript length.  Mirrors the LLM parser's limit.
_MAX_TRANSCRIPT_LEN = 500

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
# A digit amount immediately followed by "dollar(s)": one amount, not two.
_DIGIT_AMOUNT_DOLLARS_RE = re.compile(r"\d+\.\d{2}\s*dollars?\b", re.ASCII)

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
    # "one thousand dollars", etc.) -- except one that directly follows
    # a digit amount ("50.50 dollars"), which is the SAME amount and was
    # already counted above.  Two separate amounts still count twice.
    dollar_words = len(_DOLLAR_WORD_RE.findall(lower))
    dollar_words -= len(_DIGIT_AMOUNT_DOLLARS_RE.findall(lower))

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


def _transfer(
    transcript: str,
    payee: object,
    amount: str,
    currency: str,
    source_account: str,
) -> TransactionDraft:
    """A transfer to one directory payee (shape of clean_transfer.json).

    The payee fields come from the directory record, never from the text.
    """
    created, expires = _fresh_timestamps()
    return TransactionDraft(
        id=_fresh_id(),
        created_at=created,
        expires_at=expires,
        nonce=_fresh_nonce(),
        intent_type="transfer",
        source_account=source_account,
        amount=Money(value=amount, currency=currency),
        confidence=0.95,
        unresolved=[],
        transcript=transcript,
        payee=Payee(
            id=payee.id,
            display_name=payee.display_name,
            masked_account=payee.masked_account,
        ),
    )


# The payee ids the stub offers for an ambiguous "John".
_AMBIGUOUS_JOHN_IDS = ("payee-101", "payee-102")


def _candidates_from_directory(ids: tuple[str, ...]) -> list[PayeeCandidate]:
    """Build ``payee_candidates`` for *ids* from the payee directory.

    Names and masked accounts are never written out here: the directory is
    the source of truth (CONTRACT.md §4), and ``resolve_payee`` fills the
    resolved payee from it.  A candidate labelled differently from the
    payee it resolves to would show the user one name and execute another.
    """
    by_id = {p.id: p for p in FixtureDirectory().payees()}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise LookupError(f"stub offers payee id(s) the directory lacks: {missing}")
    return [
        PayeeCandidate(
            id=by_id[i].id,
            display_name=by_id[i].display_name,
            masked_account=by_id[i].masked_account,
        )
        for i in ids
    ]


def _ambiguous_payee(
    transcript: str, amount: str = "50.00", currency: str = "SGD"
) -> TransactionDraft:
    """The ambiguous-payee draft (shape of fixtures/drafts/ambiguous_payee.json).

    Only the shape matches the fixture.  The candidate names come from the
    directory, so payee-102 is "John Lee" here — the same name the shared
    fixture carries since PR #24 aligned it.  tests/test_fixture_directory_parity.py
    is what keeps the two in step from now on.
    """
    created, expires = _fresh_timestamps()
    return TransactionDraft(
        id=_fresh_id(),
        created_at=created,
        expires_at=expires,
        nonce=_fresh_nonce(),
        intent_type="transfer",
        source_account="acct-001-2345",
        amount=Money(value=amount, currency=currency),
        confidence=0.4,
        unresolved=["payee"],
        transcript=transcript,
        payee_candidates=_candidates_from_directory(_AMBIGUOUS_JOHN_IDS),
    )


def _equity_purchase(
    transcript: str, amount_value: str, currency: str = "SGD"
) -> TransactionDraft:
    """Match fixtures/drafts/equity_purchase.json shape."""
    created, expires = _fresh_timestamps()
    return TransactionDraft(
        id=_fresh_id(),
        created_at=created,
        expires_at=expires,
        nonce=_fresh_nonce(),
        intent_type="equity_purchase",
        source_account="acct-001-2345",
        amount=Money(value=amount_value, currency=currency),
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


_TRANSFER_VERB_RE = re.compile(r"\b(?:send|transfer|pay|give|wire)\b")
_MONEY_WORD_RE = re.compile(r"\b(?:money|cash|dollars?|bucks?)\b")


def _amount_question(text: str, reading) -> str:
    """The question to ask when the amount cannot be drafted."""
    if is_negative_amount(text):
        return "The amount can't be negative. How much would you like to send?"
    if has_excess_decimals(text):
        return (
            "Please give the amount with at most two decimal places. "
            "How much would you like to send?"
        )
    if reading.status == "vague":
        return (
            'I need an exact amount, for example "fifty dollars". '
            "How much would you like to send?"
        )
    if reading.status == "multiple":
        return "I heard more than one number. How much would you like to send?"
    return "How much would you like to send?"


def parse(transcript: str) -> ParseResult:
    """
    Parse a user transcript into a :class:`ParseResult`.

    The stub handles one intent per call.  A multi-intent transcript
    raises :class:`ParserCannotHandle` rather than silently dropping
    one of the intents.

    The draft's ``transcript`` is always *transcript* exactly as
    submitted; the normalised text is used only for matching (R1).

    See module docstring for the patterns handled.
    """
    # Sanitise, then normalise (fillers out).  ``text`` is internal only.
    clean = sanitise(transcript or "")
    if not clean.strip():
        raise ParserCannotHandle("empty transcript")

    # ── Length limit ────────────────────────────────────────────────
    if len(clean) > _MAX_TRANSCRIPT_LEN:
        raise ParserCannotHandle("request too long", reason="request too long")

    directory = FixtureDirectory()
    text = normalise(clean, protected_words=directory_name_words(directory)).text
    if not text.strip():
        raise ParserCannotHandle("empty transcript")

    # ── Multi-intent guard ───────────────────────────────────────────
    # The stub handles one intent only.  If the transcript describes
    # more than one, decline so no intent is silently dropped.
    if _is_multi_intent(text):
        raise ParserCannotHandle(
            "Multiple intents detected in the transcript. "
            "Please send one request at a time."
        )

    lower = text.lower().strip()
    source_account = directory.default_source_account()

    # R3: the amount is read by code.  Only an exact, positive,
    # two-decimal amount in a clear currency is usable; anything else is
    # asked about, never guessed.
    reading = spoken_amount(text)
    currency = resolve_currency(text)
    amount: str | None = None
    if (
        reading.status == "exact"
        and currency is not None
        and not is_negative_amount(text)
        and not has_excess_decimals(text)
    ):
        amount = reading.value

    # ── Equity / shares / stock / buy ────────────────────────────────
    if any(kw in lower for kw in ("share", "stock", "buy")):
        if amount is None:
            return _question_result(
                "amount",
                "How much would you like to invest?",
                transcript,
                partial={
                    "intent_type": "equity_purchase",
                    "source_account": source_account,
                    "ticker": "D05.SI",
                    "order_type": "market",
                },
            )
        return ParseResult(
            items=[_equity_purchase(transcript, amount, currency)],
            intents_detected=1,
        )

    # ── Transfer patterns ────────────────────────────────────────────
    if _TRANSFER_VERB_RE.search(lower) and (
        reading.status != "none" or _MONEY_WORD_RE.search(lower)
    ):
        # R5: the recipient, with or without a spoken "to".  R2: it must
        # resolve through the directory; a relation ("mum") never does.
        phrase = payee_phrase(text)
        matches = match_payees(phrase, directory, context=text) if phrase else []

        partial: dict = {"intent_type": "transfer", "source_account": source_account}
        if len(matches) == 1:
            partial["payee"] = {
                "id": matches[0].id,
                "display_name": matches[0].display_name,
                "masked_account": matches[0].masked_account,
            }

        if amount is None:
            # Per CONTRACT.md §2, amount cannot be in unresolved.  The
            # parser must ask before producing any draft.  Unresolved
            # fields are omitted (never None) -- the PendingQuestion shape.
            return _question_result(
                "amount",
                _amount_question(text, reading),
                transcript,
                partial=partial,
            )

        if len(matches) == 1:
            return ParseResult(
                items=[_transfer(transcript, matches[0], amount, currency, source_account)],
                intents_detected=1,
            )

        if len(matches) >= 2:
            # Ambiguous ("John"): the stub always offers the two demo Johns.
            return ParseResult(
                items=[_ambiguous_payee(transcript, amount, currency)],
                intents_detected=1,
            )

        # No directory payee matches ("mum", "my wife", "Bob", nothing).
        partial["amount"] = {"value": amount, "currency": currency}
        reference = person_reference(phrase)
        question = (
            f'I can\'t tell who "{reference}" is. Please name one of your saved payees.'
            if reference
            else "Who would you like to send the money to?"
        )
        return _question_result("payee", question, transcript, partial=partial)

    # ── Unrecognized ─────────────────────────────────────────────────
    raise ParserCannotHandle(
        f"stub parser cannot handle this transcript: {transcript!r}"
    )
