"""
LLM-based parser implementing the parse(transcript) -> ParseResult interface.

DESIGN RULE: the model only extracts; code builds the draft.  The model
never supplies payee ids, masked account numbers, or draft fields
directly.  Code resolves the model's *_mention fields against a
:class:`~backend.parser.directory.PayeeDirectory`, validates amounts
against the money pattern, and sets confidence, id, nonce, and
timestamps.

The parser:
  1. Sends the transcript (isolated between markers) to the LLM.
  2. Validates the model's JSON reply with a strict Pydantic model
     (extra fields forbidden).  On malformed/invalid JSON, retries
     once, then raises :class:`ParserCannotHandle`.
  3. Runs the stub's keyword multi-intent check as an independent
     second signal.  If either the model or the keyword check says
     multi-intent, the request is declined.
  4. Code builds the :class:`TransactionDraft` or
     :class:`ClarifyingQuestion`.
  5. Every draft validates against the schema and the Pydantic model
     before it is returned.

This module contains no ledger or gateway imports (CONTEXT.md
constraint 2, enforced by ``tests/test_import_boundaries.py``).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from backend.llm.interface import ChatClient
from backend.llm.text import sanitise
from backend.models.draft import (
    Money,
    Payee,
    PayeeCandidate,
    TransactionDraft,
)
from backend.parser.amount_rules import (
    has_excess_decimals,
    is_negative_amount,
    is_negative_value,
    resolve_currency,
)
from backend.parser.directory import (
    PayeeDirectory,
    PayeeRecord,
    match_payees,
    resolve_ticker,
)
from backend.parser.interface import (
    ClarifyingQuestion,
    ParserCannotHandle,
    ParseResult,
    PendingQuestion,
)
from backend.parser.speech_norm import (
    directory_name_words,
    normalise,
    payee_phrase,
    person_reference,
    spoken_amount,
)

# ── Prompt loading ────────────────────────────────────────────────────

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "extract_v1.txt"

# Maximum transcript length, measured on the sanitised transcript.
# Longer transcripts are declined before the model is called.
_MAX_TRANSCRIPT_LEN = 500

# ── Strict extraction model (extra fields forbidden) ─────────────────


class _ExtractedIntent(BaseModel):
    """One intent extracted by the model."""

    model_config = ConfigDict(extra="forbid")

    intent_type: str
    payee_mention: Optional[str] = None
    amount_text: Optional[str] = None
    amount_value: Optional[str] = None
    currency: Optional[str] = None
    source_account_mention: Optional[str] = None
    ticker_mention: Optional[str] = None
    order_type: Optional[str] = None
    uncertain_fields: list[str] = []


class _ExtractionResult(BaseModel):
    """Top-level JSON object returned by the model."""

    model_config = ConfigDict(extra="forbid")

    intents: list[_ExtractedIntent]


# ── Money validation (code, not the model) ───────────────────────────

from backend.models.draft import MONEY_PATTERN  # noqa: E402

_MONEY_RE = re.compile(MONEY_PATTERN, re.ASCII)


def _is_valid_money(value: str | None) -> bool:
    """Return True if *value* matches the money pattern."""
    if value is None:
        return False
    return bool(_MONEY_RE.fullmatch(value))


# ── Helpers (shared with stub.py conventions) ───────────────────────


def _fresh_id() -> str:
    return str(uuid.uuid4())


def _fresh_nonce() -> str:
    return uuid.uuid4().hex


def _fresh_question_id(field: str) -> str:
    return f"q-{field}-{uuid.uuid4().hex[:8]}"


def _fresh_timestamps() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now, now + timedelta(minutes=5)


# ── Prompt building (constraint 5) ──────────────────────────────────


def _build_prompt(clean_transcript: str) -> str:
    """Load the prompt template and insert the transcript.

    *clean_transcript* is the output of :func:`sanitise`, produced once
    in :meth:`LlmParser.parse`; the same text is used for every check.
    """
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{transcript}", clean_transcript)


# ── Stub keyword multi-intent check (imported as second signal) ─────

from backend.parser.stub import _is_multi_intent  # noqa: E402


# ── Transcript verification ──────────────────────────────────────────


def _in_transcript(mention: str | None, transcript: str) -> bool:
    """Check that *mention* appears in the sanitised *transcript*
    (case-insensitive)."""
    if not mention or not mention.strip():
        return False
    return mention.strip().lower() in transcript.lower()


# ── Intent type validation ───────────────────────────────────────────

_VALID_INTENT_TYPES = {"transfer", "bill_payment", "equity_purchase"}


# ── LlmParser ────────────────────────────────────────────────────────


class LlmParser:
    """LLM-backed parser implementing the ``parse(transcript)`` contract.

    The parser takes a :class:`ChatClient` (real or fake) and a
    :class:`PayeeDirectory` in its constructor.  It sends the
    transcript to the model, validates the reply with a strict Pydantic
    model, then code builds the draft — the model never supplies
    ids, account numbers, or tickers directly.
    """

    def __init__(
        self,
        chat_client: ChatClient,
        directory: PayeeDirectory,
    ) -> None:
        self._client = chat_client
        self._directory = directory

    def parse(self, transcript: str) -> ParseResult:
        """Parse a transcript into a :class:`ParseResult`.

        Raises :class:`ParserCannotHandle` on empty transcripts,
        multi-intent requests, malformed model replies (after one
        retry), or unknown intent types.
        """
        # ── 0. Sanitise once, at entry ─────────────────────────────
        # ``clean`` is used for the prompt and for every check below
        # (emptiness, length, verbatim mentions, multi-intent keywords,
        # currency, negative amount).  The raw ``transcript`` is only
        # stored, unchanged, in the draft / pending question.
        clean = sanitise(transcript or "")
        if not clean.strip():
            raise ParserCannotHandle("empty transcript", reason="empty transcript")

        if len(clean) > _MAX_TRANSCRIPT_LEN:
            raise ParserCannotHandle(
                "request too long",
                reason="request too long",
            )

        # ── 0b. Speech-style normalisation (speech_norm) ───────────
        # Fillers ("uh", "lah", ...) come out; nothing else changes.  The
        # result, ``work``, is what the model sees and what every check
        # below runs on, so the model and the code always read the same
        # words.  It is internal only: the draft's transcript stays the
        # exact submitted text (R1), and the rules applied are not stored
        # (the draft schema has no field for them, R7).
        norm = normalise(clean, protected_words=directory_name_words(self._directory))
        work = norm.text
        if not work.strip():
            # Nothing but fillers ("uh um lah").
            raise ParserCannotHandle("empty transcript", reason="empty transcript")

        # ── 1. Ask the model to extract ────────────────────────────
        extraction = self._extract(work)

        # ── 2. Multi-intent guard (two signals) ───────────────────
        if len(extraction.intents) > 1:
            raise ParserCannotHandle(
                "Multiple intents detected in the transcript. "
                "Please send one request at a time.",
                reason=f"model: {len(extraction.intents)} intents",
            )

        # Independent second signal: keyword-based check from stub.
        if _is_multi_intent(work):
            raise ParserCannotHandle(
                "Multiple intents detected in the transcript. "
                "Please send one request at a time.",
                reason="keyword guard",
            )

        # ── 3. Zero intents or unknown intent type ─────────────────
        if len(extraction.intents) == 0:
            raise ParserCannotHandle(
                "no intents detected",
                reason="invalid model output",
            )

        intent = extraction.intents[0]
        if intent.intent_type not in _VALID_INTENT_TYPES:
            raise ParserCannotHandle(
                f"unknown intent_type: {intent.intent_type!r}",
                reason="invalid model output",
            )

        # ── 4. Code builds the draft ───────────────────────────────
        return self._build_result(transcript, work, intent, sanitised=clean)

    def _extract(self, clean_transcript: str) -> _ExtractionResult:
        """Send the prompt to the model and validate the reply.

        Retries once on malformed/invalid JSON, then raises
        :class:`ParserCannotHandle`.
        """
        prompt = _build_prompt(clean_transcript)

        for attempt in range(2):
            reply = self._client.ask(prompt)
            try:
                return _parse_extraction_reply(reply.text)
            except (json.JSONDecodeError, ValidationError, ValueError):
                if attempt == 0:
                    continue
                raise ParserCannotHandle(
                    "model reply did not match the extraction schema "
                    "after one retry",
                    reason="invalid model output",
                )

        # Unreachable: the loop either returns or raises.
        raise ParserCannotHandle(
            "extraction failed unexpectedly",
            reason="invalid model output",
        )

    def _build_result(
        self,
        transcript: str,
        clean: str,
        intent: _ExtractedIntent,
        sanitised: str | None = None,
    ) -> ParseResult:
        """Build a :class:`ParseResult` from the extracted intent.

        Code — not the model — resolves payees, validates amounts,
        and sets all draft fields.  Every check uses ``clean``, the
        sanitised and speech-normalised working text the model saw; the
        raw ``transcript`` is only stored, unchanged (R1).

        Every :class:`ClarifyingQuestion` returned also populates
        :attr:`ParseResult.pending` with a :class:`PendingQuestion`
        carrying the original transcript and the fields resolved so
        far, so the server can correlate an answer with the original
        request (CONTRACT.md §4, second form).

        The ``partial`` dict shape is identical to the stub parser's
        (see :class:`PendingQuestion` docstring for the shape contract).
        Unresolved fields are **omitted**, never ``None``.
        """
        # ── Transcript verification for *_mention fields ──────────
        mentions = {
            "payee_mention": intent.payee_mention,
            "amount_text": intent.amount_text,
            "source_account_mention": intent.source_account_mention,
            "ticker_mention": intent.ticker_mention,
        }
        # Build a set of fields that fail the transcript check.
        transcript_failures: set[str] = set()
        # A mention must be the user's own words.  The model saw ``clean``
        # (fillers removed), but a mention that quotes the sanitised
        # original instead ("Jane lah") is still the user's words; the
        # directory match strips the particle.  Anything in neither text
        # is invented and is refused.
        for field_name, value in mentions.items():
            if value and not (
                _in_transcript(value, clean)
                or (sanitised is not None and _in_transcript(value, sanitised))
            ):
                transcript_failures.add(field_name)

        # ── Amount resolution ──────────────────────────────────────
        uncertain = set(intent.uncertain_fields)
        amount_value = intent.amount_value
        intent_type = intent.intent_type

        # Build the partial dict (fields resolved so far) for any
        # ClarifyingQuestion we might return.
        partial: dict[str, Any] = {
            "intent_type": intent_type,
            "source_account": self._directory.default_source_account(),
        }

        # ── Payee resolution (transfer / bill_payment) ─────────────
        # Resolve the payee into partial, but do NOT return a question
        # yet.  We want the partial to carry the amount too (if
        # resolved) before we ask about the payee, so that
        # resolve_question can build a valid transfer draft from
        # partial + the payee answer.
        payee: Payee | None = None
        payee_candidates: list[PayeeCandidate] | None = None
        unresolved: list[str] = []
        need_payee_question = False
        payee_question = "Who would you like to send the money to?"

        if intent_type in ("transfer", "bill_payment"):
            # If payee_mention fails the transcript check, mark for asking.
            if "payee_mention" in transcript_failures:
                need_payee_question = True
            else:
                mention = intent.payee_mention
                if not (mention and mention.strip()):
                    # R5: the model found no recipient.  A spoken "to" is
                    # often missing ("send fifty dollars john smith"), so
                    # take the words left after the verb and the amount.
                    # They are only a candidate: the directory must
                    # resolve them to exactly one payee, else we ask.
                    mention = payee_phrase(clean)

                # R2: "mum", "my wife", ... are relations, not names.  They
                # reach a draft only if the directory itself matches the
                # words; nothing maps a relation to a payee.
                reference = person_reference(mention)

                # The account clue can sit after the mention in the
                # transcript (the model often returns just the name), so
                # the working text is the context.
                matches = (
                    match_payees(mention, self._directory, context=clean)
                    if mention
                    else []
                )

                if len(matches) == 0:
                    # Zero matches → mark for asking.
                    need_payee_question = True
                    if reference:
                        payee_question = (
                            f'I can\'t tell who "{reference}" is. '
                            f"Please name one of your saved payees."
                        )
                elif len(matches) >= 2:
                    # Ambiguous → draft with payee in unresolved.
                    unresolved.append("payee")
                    payee_candidates = [
                        PayeeCandidate(
                            id=m.id,
                            display_name=m.display_name,
                            masked_account=m.masked_account,
                        )
                        for m in matches
                    ]
                else:
                    # Exactly one match → resolved payee.
                    m = matches[0]
                    payee = Payee(
                        id=m.id,
                        display_name=m.display_name,
                        masked_account=m.masked_account,
                    )
                    partial["payee"] = {
                        "id": m.id,
                        "display_name": m.display_name,
                        "masked_account": m.masked_account,
                    }

        # ── Amount checks ──────────────────────────────────────────
        # Resolve the amount into partial if it's valid.  If not, mark
        # for asking — but don't return yet, so the partial carries the
        # payee (if resolved) before we ask about the amount.
        # The currency comes from the user's words by the product rule
        # in amount_rules — never from the model's ``currency`` field.
        need_amount_question = False
        amount_question = "How much would you like to send?"
        currency = resolve_currency(clean)
        # R3: money words are read by code, deterministically.  The
        # model's amount_value is only a cross-check against that reading.
        reading = spoken_amount(clean)

        if is_negative_amount(clean) or is_negative_value(amount_value):
            # Never drop a sign: a negative amount is asked again.
            need_amount_question = True
            amount_question = (
                "The amount can't be negative. "
                "How much would you like to send?"
            )
        elif has_excess_decimals(clean):
            # Never round: "50.555" is asked again, whatever the model
            # returned for it.
            need_amount_question = True
            amount_question = (
                "Please give the amount with at most two decimal places. "
                "How much would you like to send?"
            )
        elif reading.status == "vague":
            # "some", "a few hundred", "about fifty", "two fifty": never a
            # number, whatever the model returned for it.
            need_amount_question = True
            amount_question = (
                "I need an exact amount, for example \"fifty dollars\". "
                "How much would you like to send?"
            )
        elif reading.status == "multiple":
            need_amount_question = True
            amount_question = (
                "I heard more than one number. "
                "How much would you like to send?"
            )
        elif "amount_text" in transcript_failures:
            need_amount_question = True
        elif (
            reading.status == "exact"
            and amount_value
            and _is_valid_money(amount_value)
            and amount_value != reading.value
        ):
            # Two readings of the same words disagree: ask, do not pick.
            need_amount_question = True
            amount_question = (
                "I could not be sure of the amount. "
                "How much would you like to send?"
            )
        elif reading.status == "exact" and currency is None:
            need_amount_question = True
            amount_question = (
                "Which currency did you mean? "
                "How much would you like to send?"
            )
        elif reading.status == "exact":
            amount_value = reading.value
            partial["amount"] = {"value": amount_value, "currency": currency}
        elif "amount" in uncertain or not amount_value:
            need_amount_question = True
        elif not _is_valid_money(amount_value):
            need_amount_question = True
        elif currency is None:
            need_amount_question = True
            amount_question = (
                "Which currency did you mean? "
                "How much would you like to send?"
            )
        else:
            partial["amount"] = {"value": amount_value, "currency": currency}

        # ── Ask in priority order: amount, then payee ──────────────
        # Amount has priority because no draft can be produced without
        # it (CONTRACT.md §2: "amount cannot be in unresolved").
        if need_amount_question:
            return _make_question_result(
                "amount", amount_question,
                transcript, partial,
            )

        if need_payee_question:
            return _make_question_result(
                "payee", payee_question,
                transcript, partial,
            )

        # ── Equity fields ──────────────────────────────────────────
        ticker: str | None = None
        order_type: str | None = intent.order_type
        notional_amount: str | None = None
        need_ticker_question = False
        need_order_type_question = False

        if intent_type == "equity_purchase":
            # Add order_type to partial before the ticker check so the
            # partial carries it when we ask for the ticker.
            if order_type:
                partial["order_type"] = order_type
            else:
                need_order_type_question = True

            # Ticker resolved only through the directory.
            if "ticker_mention" in transcript_failures:
                need_ticker_question = True
            else:
                ticker = resolve_ticker(intent.ticker_mention, self._directory)
                if ticker is None:
                    need_ticker_question = True
                else:
                    partial["ticker"] = ticker

            # Ask in priority order: ticker, then order_type.
            if need_ticker_question:
                return _make_question_result(
                    "ticker", "Which stock would you like to buy?",
                    transcript, partial,
                )

            if need_order_type_question:
                return _make_question_result(
                    "order_type", "Market or limit order?",
                    transcript, partial,
                )

            notional_amount = amount_value

        # ── Source account (product rule, not a guess) ─────────────
        source_account = self._directory.default_source_account()

        # ── Confidence (code sets, not model) ──────────────────────
        confidence = self._compute_confidence(intent, unresolved)

        # ── Build the draft ────────────────────────────────────────
        # Optional fields are omitted (not None) per the contract rule.
        created, expires = _fresh_timestamps()
        kwargs: dict[str, Any] = dict(
            id=_fresh_id(),
            created_at=created,
            expires_at=expires,
            nonce=_fresh_nonce(),
            intent_type=intent_type,
            source_account=source_account,
            amount=Money(value=amount_value, currency=currency),
            confidence=confidence,
            unresolved=unresolved,
            transcript=transcript,
        )
        if payee is not None:
            kwargs["payee"] = payee
        if payee_candidates is not None:
            kwargs["payee_candidates"] = payee_candidates
        if ticker is not None:
            kwargs["ticker"] = ticker
        if notional_amount is not None:
            kwargs["notional_amount"] = notional_amount
        if order_type is not None:
            kwargs["order_type"] = order_type

        draft = TransactionDraft(**kwargs)

        return ParseResult(items=[draft], intents_detected=1)

    def _compute_confidence(
        self,
        intent: _ExtractedIntent,
        unresolved: list[str],
    ) -> float:
        """Compute a confidence score.

        Lower confidence when fields are uncertain or unresolved.
        """
        if unresolved:
            return 0.4
        if intent.uncertain_fields:
            return 0.6
        return 0.9


# ── Module-level helpers ─────────────────────────────────────────────


def _make_question(field: str, question: str) -> ClarifyingQuestion:
    return ClarifyingQuestion(
        question_id=_fresh_question_id(field),
        field=field,
        question=question,
    )


def _make_question_result(
    field: str,
    question: str,
    transcript: str,
    partial: dict[str, Any] | None = None,
) -> ParseResult:
    """Build a one-item ParseResult with a ClarifyingQuestion and its pending."""
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
    return ParseResult(
        items=[question_item],
        intents_detected=1,
        pending={qid: pending},
    )


def _parse_extraction_reply(text: str) -> _ExtractionResult:
    """Parse the model's reply text into an :class:`_ExtractionResult`.

    Strips any markdown code fences, then parses JSON and validates
    against the strict Pydantic model (extra fields forbidden).
    """
    cleaned = text.strip()

    # Strip markdown code fences if present.
    if cleaned.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1:]
        # Remove closing fence
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    data = json.loads(cleaned)
    return _ExtractionResult.model_validate(data)
