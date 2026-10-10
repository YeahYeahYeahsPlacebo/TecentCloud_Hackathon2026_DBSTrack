"""
LLM-based validation agent.

DESIGN RULE: the validator never sees the draft in its prompt. The model
independently extracts what the user asked for from the transcript, and
CODE compares that extraction with the draft. The model never edits,
repairs or suggests changes to a draft.

The validator:
  1. Sends the transcript (isolated between markers) to the VALIDATOR
     ADP application (Tencent Hy3), never the parser's.
  2. Validates the model's JSON reply with a strict Pydantic model
     (extra fields forbidden). On malformed/invalid JSON, retries once,
     then fails closed.
  3. Code compares the extraction with the draft:
     - more than one intent -> freeze ("multiple requests")
     - intent_type differs -> discrepancy "intent_type"
     - amount_value differs from draft.amount.value -> "amount"
     - payee: match_payees must include the draft's payee id, else "payee"
     - source account: must match the draft's if mentioned
     - ticker and order_type for equity drafts
  4. Any discrepancy -> freeze, listing the fields.
  5. Fail closed: ModelCallError, invalid JSON after retry, extra fields,
     or zero intents -> freeze with reason "validator_unavailable".

This module contains no ledger or gateway imports (CONTEXT.md
constraint 2, enforced by tests/test_import_boundaries.py).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from backend.canonical import draft_hash
from backend.llm.interface import ChatClient, ModelCallError
from backend.models.draft import TransactionDraft
from backend.parser.directory import PayeeDirectory, match_payees, resolve_ticker
from backend.parser.interface import ClarificationRecord
from backend.validator.interface import ValidatorVerdict

# ── Prompt loading ────────────────────────────────────────────────────

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "verify_v1.txt"

# Markers that delimit user-supplied text so the model treats it as data
# (CONTEXT.md constraint 5).
_TRANSCRIPT_MARKER_OPEN = "<<<TRANSCRIPT>>>"
_TRANSCRIPT_MARKER_CLOSE = "<<<END>>>"
_ANSWER_MARKER_OPEN = "<<<ANSWER>>>"
_ANSWER_MARKER_CLOSE = "<<<END>>>"
_FIELD_MARKER = "<<<FIELD>>>"

_ALL_MARKERS = (
    _TRANSCRIPT_MARKER_OPEN,
    _TRANSCRIPT_MARKER_CLOSE,
    _ANSWER_MARKER_OPEN,
    _ANSWER_MARKER_CLOSE,
    _FIELD_MARKER,
)

# ── Strict extraction model (extra fields forbidden) ─────────────────


class _ExtractedIntent(BaseModel):
    """One intent extracted by the validator model."""

    model_config = ConfigDict(extra="forbid")

    intent_type: str
    payee_mention: Optional[str] = None
    amount_value: Optional[str] = None
    currency: Optional[str] = None
    source_account_mention: Optional[str] = None
    ticker_mention: Optional[str] = None
    order_type: Optional[str] = None


class _ExtractionResult(BaseModel):
    """Top-level JSON object returned by the validator model."""

    model_config = ConfigDict(extra="forbid")

    intents: list[_ExtractedIntent]


# ── Helpers ───────────────────────────────────────────────────────────


def _strip_markers(text: str) -> str:
    """Remove all template markers from user text.

    The user text is data (CONTEXT.md constraint 5). Markers are stripped
    so the user cannot break out of the delimited block in the prompt.
    """
    result = text
    for marker in _ALL_MARKERS:
        result = result.replace(marker, "")
    return result


def _build_prompt(
    transcript: str,
    clarification: Optional[ClarificationRecord] = None,
) -> str:
    """Load the prompt template and insert the sanitised transcript.

    If a ClarificationRecord is given, the user's answer goes in a
    SEPARATE <<<ANSWER>>> ... <<<END>>> block, labelled with the field it
    answered. Never merged with the transcript.
    """
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    safe_transcript = _strip_markers(transcript)

    if clarification is not None:
        safe_answer = _strip_markers(clarification.answer)
        answer_block = (
            f"\nClarification answer for field <<<FIELD>>>{clarification.field}<<<FIELD>>>:\n"
            f"{_ANSWER_MARKER_OPEN}\n"
            f"{safe_answer}\n"
            f"{_ANSWER_MARKER_CLOSE}\n"
        )
    else:
        answer_block = ""

    return (
        template
        .replace("{transcript}", safe_transcript)
        .replace("{answer_block}", answer_block)
    )


def _parse_reply(text: str) -> _ExtractionResult:
    """Parse the model's reply text into an _ExtractionResult.

    Strips any markdown code fences, then parses JSON and validates
    against the strict Pydantic model (extra fields forbidden).
    """
    cleaned = text.strip()

    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    data = json.loads(cleaned)
    return _ExtractionResult.model_validate(data)


# ── Comparison logic ─────────────────────────────────────────────────


def _compare(
    draft: TransactionDraft,
    extraction: _ExtractionResult,
    directory: PayeeDirectory,
) -> tuple[list[str], str, bool]:
    """Compare the model's extraction with the draft.

    Returns ``(discrepancies, reason, should_freeze)``.

    - ``discrepancies`` is a list of contract field names that
      disagree (only ``intent_type``, ``amount``, ``payee``,
      ``source_account``, ``ticker``, ``order_type``).
    - ``reason`` is a plain-language sentence.
    - ``should_freeze`` is True when the verdict should be ``freeze``
      regardless of whether ``discrepancies`` is non-empty (e.g. the
      transcript contains more than one request, or the model returned
      zero intents).
    """
    # ── More than one intent -> freeze ───────────────────────────
    if len(extraction.intents) > 1:
        return [], "The transcript contains more than one request.", True

    # ── Zero intents -> fail closed ───────────────────────────────
    if len(extraction.intents) == 0:
        return [], "The model extracted no intents.", True

    intent = extraction.intents[0]
    discrepancies: list[str] = []
    reasons: list[str] = []

    # ── intent_type ──────────────────────────────────────────────
    if intent.intent_type != draft.intent_type.value:
        discrepancies.append("intent_type")
        reasons.append(
            f"The intent in the draft ({draft.intent_type.value}) does not "
            f"match what was said ({intent.intent_type})."
        )

    # ── amount ──────────────────────────────────────────────────
    if intent.amount_value is not None:
        if intent.amount_value != draft.amount.value:
            discrepancies.append("amount")
            reasons.append(
                f"The amount in the draft ({draft.amount.value}) does not "
                f"match what was said ({intent.amount_value})."
            )
    else:
        # The model could not extract an amount from the transcript.
        # The draft has an amount, so it is unverified — the transcript
        # does not support the amount in the draft.
        discrepancies.append("amount")
        reasons.append(
            f"The transcript does not mention an amount but the draft has "
            f"{draft.amount.value}."
        )

    # ── payee (skip if payee is unresolved) ─────────────────────
    if "payee" not in draft.unresolved:
        if intent.payee_mention is not None:
            matches = match_payees(intent.payee_mention, directory)
            match_ids = {m.id for m in matches}
            if draft.payee is not None and draft.payee.id not in match_ids:
                discrepancies.append("payee")
                payee_name = (
                    draft.payee.display_name if draft.payee else "unknown"
                )
                reasons.append(
                    f"The payee in the draft ({payee_name}) does not "
                    f"match who was said ({intent.payee_mention})."
                )
        elif draft.payee is not None:
            # The draft has a payee but the model found none.
            discrepancies.append("payee")
            reasons.append(
                f"The transcript does not mention a payee but the draft "
                f"names {draft.payee.display_name}."
            )

    # ── source account ───────────────────────────────────────────
    if intent.source_account_mention is not None:
        # If mentioned, it must match the draft's.
        if intent.source_account_mention != draft.source_account:
            discrepancies.append("source_account")
            reasons.append(
                f"The source account in the draft ({draft.source_account}) "
                f"does not match what was said ({intent.source_account_mention})."
            )
    else:
        # If not mentioned, the draft must use the default.
        default_acct = directory.default_source_account()
        if draft.source_account != default_acct:
            discrepancies.append("source_account")
            reasons.append(
                f"The transcript does not mention a source account but the "
                f"draft uses {draft.source_account} instead of the default "
                f"({default_acct})."
            )

    # ── ticker and order_type for equity drafts ─────────────────
    if draft.intent_type.value == "equity_purchase":
        if intent.ticker_mention is not None:
            resolved = resolve_ticker(intent.ticker_mention, directory)
            if resolved is not None and resolved != draft.ticker:
                discrepancies.append("ticker")
                reasons.append(
                    f"The ticker in the draft ({draft.ticker}) does not "
                    f"match what was said ({resolved})."
                )
        if intent.order_type is not None:
            if intent.order_type != draft.order_type.value:
                discrepancies.append("order_type")
                reasons.append(
                    f"The order type in the draft ({draft.order_type.value}) "
                    f"does not match what was said ({intent.order_type})."
                )

    reason = " ".join(reasons) if reasons else "The draft matches the transcript."
    return discrepancies, reason, bool(discrepancies)


# ── LlmValidator ─────────────────────────────────────────────────────


class LlmValidator:
    """LLM-backed validator implementing the Validator protocol.

    The validator takes a :class:`ChatClient` (for the VALIDATOR ADP
    application) and a :class:`PayeeDirectory` in its constructor. It
    sends the transcript to the model, validates the reply with a strict
    Pydantic model, then code compares the extraction with the draft.

    The model never sees the draft. It independently extracts what the
    user asked for, and code compares.
    """

    def __init__(
        self,
        client: ChatClient,
        directory: PayeeDirectory,
    ) -> None:
        self._client = client
        self._directory = directory

    def validate(
        self,
        draft: TransactionDraft,
        transcript: str,
        clarification: Optional[ClarificationRecord] = None,
    ) -> ValidatorVerdict:
        """Validate a draft against the transcript.

        Returns a :class:`ValidatorVerdict` with ``verdict`` ``"pass"`` or
        ``"freeze"``.  Fail closed: any model error results in a freeze
        with reason ``"validator_unavailable"``.
        """
        h = draft_hash(draft.model_dump(mode="json", exclude_none=True))

        # ── Ask the model to extract ─────────────────────────────
        prompt = _build_prompt(transcript, clarification)

        try:
            extraction = self._extract(prompt)
        except (ModelCallError, json.JSONDecodeError, ValidationError, ValueError):
            return ValidatorVerdict(
                verdict="freeze",
                discrepancies=[],
                reason="validator_unavailable",
                draft_hash=h,
            )

        # ── Code compares the extraction with the draft ─────────
        discrepancies, reason, should_freeze = _compare(
            draft, extraction, self._directory
        )

        if should_freeze:
            return ValidatorVerdict(
                verdict="freeze",
                discrepancies=discrepancies,
                reason=reason,
                draft_hash=h,
            )

        return ValidatorVerdict(
            verdict="pass",
            discrepancies=[],
            reason=reason,
            draft_hash=h,
        )

    def _extract(self, prompt: str) -> _ExtractionResult:
        """Send the prompt to the model and validate the reply.

        Retries once on malformed/invalid JSON, then raises.
        """
        for attempt in range(2):
            reply = self._client.ask(prompt)
            try:
                return _parse_reply(reply.text)
            except (json.JSONDecodeError, ValidationError, ValueError):
                if attempt == 0:
                    continue
                raise

        # Unreachable: the loop either returns or raises.
        raise ValueError("extraction failed unexpectedly")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """The callable form is intentionally disabled.

        ``__call__`` would drop the ``ClarificationRecord`` that
        ``validate()`` needs to pass through.  Call ``validate()``
        directly instead.
        """
        raise TypeError(
            "Call validate(draft, transcript, clarification=record); "
            "the callable form drops the ClarificationRecord."
        )
