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
from backend.models.draft import (
    Money,
    Payee,
    PayeeCandidate,
    TransactionDraft,
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
)

# ── Prompt loading ────────────────────────────────────────────────────

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "extract_v1.txt"

# Markers that delimit the transcript so the model treats it as data
# (CONTEXT.md constraint 5).
_TRANSCRIPT_MARKER_OPEN = "<<<TRANSCRIPT>>>"
_TRANSCRIPT_MARKER_CLOSE = "<<<END>>>"

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


# ── Transcript sanitisation (constraint 5) ──────────────────────────


def _strip_markers(transcript: str) -> str:
    """Remove any occurrence of the transcript markers from *transcript*.

    This prevents a malicious transcript from closing the delimited
    block early and injecting instructions.
    """
    cleaned = transcript.replace(_TRANSCRIPT_MARKER_OPEN, "")
    cleaned = cleaned.replace(_TRANSCRIPT_MARKER_CLOSE, "")
    return cleaned


def _build_prompt(transcript: str) -> str:
    """Load the prompt template and insert the sanitised transcript."""
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    safe = _strip_markers(transcript)
    return template.replace("{transcript}", safe)


# ── Stub keyword multi-intent check (imported as second signal) ─────

from backend.parser.stub import _is_multi_intent  # noqa: E402


# ── Transcript verification ──────────────────────────────────────────


def _in_transcript(mention: str | None, transcript: str) -> bool:
    """Check that *mention* appears in *transcript* (case-insensitive)."""
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
        if not transcript or not transcript.strip():
            raise ParserCannotHandle("empty transcript", reason="empty transcript")

        # ── 1. Ask the model to extract ────────────────────────────
        extraction = self._extract(transcript)

        # ── 2. Multi-intent guard (two signals) ───────────────────
        if len(extraction.intents) > 1:
            raise ParserCannotHandle(
                "Multiple intents detected in the transcript. "
                "Please send one request at a time.",
                reason=f"model: {len(extraction.intents)} intents",
            )

        # Independent second signal: keyword-based check from stub.
        if _is_multi_intent(transcript):
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
        return self._build_result(transcript, intent)

    def _extract(self, transcript: str) -> _ExtractionResult:
        """Send the prompt to the model and validate the reply.

        Retries once on malformed/invalid JSON, then raises
        :class:`ParserCannotHandle`.
        """
        prompt = _build_prompt(transcript)

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
        intent: _ExtractedIntent,
    ) -> ParseResult:
        """Build a :class:`ParseResult` from the extracted intent.

        Code — not the model — resolves payees, validates amounts,
        and sets all draft fields.
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
        for field_name, value in mentions.items():
            if value and not _in_transcript(value, transcript):
                transcript_failures.add(field_name)

        # ── Amount resolution ──────────────────────────────────────
        uncertain = set(intent.uncertain_fields)
        amount_value = intent.amount_value

        # If amount_text fails the transcript check, treat as unresolved.
        if "amount_text" in transcript_failures:
            return ParseResult(
                items=[_make_question("amount",
                    "How much would you like to send?")],
                intents_detected=1,
            )

        # If amount is in uncertain_fields or missing, ask.
        if "amount" in uncertain or not amount_value:
            return ParseResult(
                items=[_make_question("amount",
                    "How much would you like to send?")],
                intents_detected=1,
            )

        # Validate the money pattern.
        if not _is_valid_money(amount_value):
            return ParseResult(
                items=[_make_question("amount",
                    "How much would you like to send?")],
                intents_detected=1,
            )

        currency = intent.currency or "SGD"

        # ── Payee resolution (transfer / bill_payment) ─────────────
        intent_type = intent.intent_type
        payee: Payee | None = None
        payee_candidates: list[PayeeCandidate] | None = None
        unresolved: list[str] = []

        if intent_type in ("transfer", "bill_payment"):
            # If payee_mention fails the transcript check, ask.
            if "payee_mention" in transcript_failures:
                return ParseResult(
                    items=[_make_question("payee",
                        "Who would you like to send to?")],
                    intents_detected=1,
                )

            matches = match_payees(intent.payee_mention, self._directory)

            if len(matches) == 0:
                # Zero matches → ask the user.
                return ParseResult(
                    items=[_make_question("payee",
                        "Who would you like to send the money to?")],
                    intents_detected=1,
                )

            if len(matches) >= 2:
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

        # ── Equity fields ──────────────────────────────────────────
        ticker: str | None = None
        order_type: str | None = intent.order_type
        notional_amount: str | None = None

        if intent_type == "equity_purchase":
            # Ticker resolved only through the directory.
            if "ticker_mention" in transcript_failures:
                return ParseResult(
                    items=[_make_question("ticker",
                        "Which stock would you like to buy?")],
                    intents_detected=1,
                )

            ticker = resolve_ticker(intent.ticker_mention, self._directory)
            if ticker is None:
                return ParseResult(
                    items=[_make_question("ticker",
                        "Which stock would you like to buy?")],
                    intents_detected=1,
                )

            if not order_type:
                return ParseResult(
                    items=[_make_question("order_type",
                        "Market or limit order?")],
                    intents_detected=1,
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
