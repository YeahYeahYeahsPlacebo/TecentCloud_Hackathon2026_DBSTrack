"""
LLM-based validation agent.

DESIGN RULE: the validator never sees the draft in its prompt. The model
independently extracts what the user asked for from the transcript, and
CODE compares that extraction with the draft. The model never edits,
repairs or suggests changes to a draft.

The validator:
  0. Sanitises the transcript and any clarification answer once
     (backend.llm.text.sanitise) and uses that text for everything
     below.  A transcript over 500 characters, a negative amount, or an
     amount with more than two decimal places freezes without calling
     the model.
  1. Sends the transcript (isolated between markers) to the VALIDATOR
     ADP application (Tencent Hy3), never the parser's.
  2. Validates the model's JSON reply with a strict Pydantic model
     (extra fields forbidden). On malformed/invalid JSON, retries once,
     then fails closed.
  3. Code compares the extraction with the draft:
     - more than one intent -> freeze ("multiple requests")
     - intent_type differs -> discrepancy "intent_type"
     - amount_value differs from draft.amount.value, or is negative
       -> "amount"
     - currency (product rule in backend.parser.amount_rules, never
       the model's currency field) unclear or different -> "amount"
     - payee: match_payees (name AND any account digits said) must
       include the draft's payee id, else "payee".  A transcript that
       fits several payees needs a payee pick on record (the
       ClarificationRecord from resolve_payee); without one -> "payee".
       With a pick, the draft's payee must be the directory record for
       the picked id, which must exist in the directory -> else "payee"
     - source account: must match the draft's if mentioned
     - ticker and order_type for equity drafts
  4. Any discrepancy -> freeze, listing the fields.
  5. Fail closed: ModelCallError, invalid JSON after retry, extra fields,
     or zero intents -> freeze with reason
     "validator_unavailable: <redacted cause>", e.g.
     "validator_unavailable: ModelCallError (ADP returned HTTP 429)" or
     "validator_unavailable: invalid JSON after retry".

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
from backend.llm.failure import describe_failure
from backend.llm.interface import ChatClient, ModelCallError
from backend.llm.text import sanitise
from backend.models.draft import TransactionDraft
from backend.parser.amount_rules import (
    has_excess_decimals,
    is_negative_amount,
    is_negative_value,
    resolve_currency,
)
from backend.parser.directory import PayeeDirectory, match_payees, resolve_ticker
from backend.parser.interface import ClarificationRecord
from backend.validator.interface import ValidatorVerdict

# ── Prompt loading ────────────────────────────────────────────────────

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "verify_v1.txt"

# Verdict reason prefix when the model could not answer (fail closed).
# The full reason is "validator_unavailable: <redacted cause>".
UNAVAILABLE = "validator_unavailable"

# Maximum transcript length, measured on the sanitised transcript.
# Longer transcripts freeze with reason "transcript too long".
_MAX_TRANSCRIPT_LEN = 500

# Markers that delimit user-supplied text so the model treats it as data
# (CONTEXT.md constraint 5).  User text has them removed by
# :func:`backend.llm.text.sanitise` before it reaches _build_prompt.
_ANSWER_MARKER_OPEN = "<<<ANSWER>>>"
_ANSWER_MARKER_CLOSE = "<<<END>>>"
_FIELD_MARKER = "<<<FIELD>>>"

# Template placeholders, filled in a single pass so user text that
# happens to contain "{answer_block}" is never expanded.
_PLACEHOLDER_RE = re.compile(r"\{transcript\}|\{answer_block\}")

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


def _build_prompt(
    clean_transcript: str,
    clarification: Optional[ClarificationRecord] = None,
    clean_answer: Optional[str] = None,
) -> str:
    """Load the prompt template and insert the transcript.

    *clean_transcript* and *clean_answer* are the sanitised texts
    produced once in :meth:`LlmValidator.validate`; the same texts are
    used for every check.

    If a ClarificationRecord is given, the user's answer goes in a
    SEPARATE <<<ANSWER>>> ... <<<END>>> block, labelled with the field it
    answered. Never merged with the transcript.
    """
    template = _PROMPT_PATH.read_text(encoding="utf-8")

    if clarification is not None:
        if clean_answer is None:
            clean_answer = sanitise(clarification.answer)
        answer_block = (
            f"\nClarification answer for field <<<FIELD>>>{clarification.field}<<<FIELD>>>:\n"
            f"{_ANSWER_MARKER_OPEN}\n"
            f"{clean_answer}\n"
            f"{_ANSWER_MARKER_CLOSE}\n"
        )
    else:
        answer_block = ""

    values = {"{transcript}": clean_transcript, "{answer_block}": answer_block}
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(0)], template)


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


# ── Payee pick (CONTRACT.md §4, first form of /api/clarify) ──────────


def _payee_pick(
    clarification: Optional[ClarificationRecord],
) -> Optional[ClarificationRecord]:
    """Return *clarification* if it records a payee candidate pick."""
    if (
        clarification is not None
        and clarification.field == "payee"
        and clarification.answer_kind == "candidate_id"
    ):
        return clarification
    return None


def _transcript_names(record: Any, transcript: str) -> bool:
    """True if a word of the transcript is the first or last name of *record*.

    Deterministic and model-free: the fallback when the model gave no
    usable payee mention for a payee pick.
    """
    words = record.display_name.lower().split()
    if not words:
        return False
    tokens = set(re.findall(r"[^\W\d_]+", transcript.lower()))
    return bool(tokens & {words[0], words[-1]})


def _check_payee_pick(
    draft: TransactionDraft,
    pick: ClarificationRecord,
    mention: Optional[str],
    matches: list[Any],
    directory: PayeeDirectory,
    transcript: str,
) -> list[str]:
    """Reasons the draft's payee disagrees with a payee pick (empty = fine).

    1. The picked id must exist in the directory; otherwise freeze.
    2. The draft's payee must be exactly the directory record for that
       id (id, display name and masked account).
    3. The transcript must be consistent with the picked payee: the
       model's payee mention must match it, or -- if the model gave no
       usable mention (none, or it copied the opaque id) -- a name
       component of the picked payee must appear in the transcript.
    """
    picked = next((p for p in directory.payees() if p.id == pick.answer), None)
    if picked is None:
        return [
            f"The payee that was picked ({pick.answer!r}) is not in the "
            f"directory."
        ]

    reasons: list[str] = []
    drafted = draft.payee
    if drafted is None or (
        drafted.id, drafted.display_name, drafted.masked_account
    ) != (picked.id, picked.display_name, picked.masked_account):
        named = drafted.display_name if drafted is not None else "no payee"
        reasons.append(
            f"The payee in the draft ({named}) is not the payee that was "
            f"picked ({picked.display_name})."
        )

    usable = mention is not None and mention.strip().lower() != pick.answer.strip().lower()
    if usable:
        if picked.id not in {m.id for m in matches}:
            reasons.append(
                f"The payee that was picked ({picked.display_name}) does "
                f"not match who was said ({mention})."
            )
    elif not _transcript_names(picked, transcript):
        reasons.append(
            f"The transcript does not name the payee that was picked "
            f"({picked.display_name})."
        )
    return reasons


# ── Comparison logic ─────────────────────────────────────────────────


def _compare(
    draft: TransactionDraft,
    extraction: _ExtractionResult,
    directory: PayeeDirectory,
    user_texts: tuple[str, ...] = (),
    clarification: Optional[ClarificationRecord] = None,
) -> tuple[list[str], str, bool]:
    """Compare the model's extraction with the draft.

    *user_texts* are the sanitised transcript (first) and, if any, the
    clarification answer; the currency rule is applied to them.
    *clarification* is the record for a clarified draft; for a payee
    pick it is what the draft's payee is checked against (see
    :func:`_check_payee_pick`).

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
    if is_negative_value(intent.amount_value):
        discrepancies.append("amount")
        reasons.append(
            f"The amount that was said ({intent.amount_value}) is negative."
        )
    elif intent.amount_value is not None:
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

    # ── currency (product rule in amount_rules, never the model's) ─
    said_currency = resolve_currency(*user_texts)
    if said_currency is None:
        if "amount" not in discrepancies:
            discrepancies.append("amount")
        reasons.append(
            "The currency that was said is unclear or not supported."
        )
    elif said_currency != draft.amount.currency:
        if "amount" not in discrepancies:
            discrepancies.append("amount")
        reasons.append(
            f"The currency in the draft ({draft.amount.currency}) does not "
            f"match what was said ({said_currency})."
        )

    # ── payee (skip if payee is unresolved) ─────────────────────
    if "payee" not in draft.unresolved:
        pick = _payee_pick(clarification)
        said = "\n".join(user_texts)
        matches = (
            # Same rule as the parser: name AND account digits.  The
            # clue may sit after the mention in the user's own text.
            match_payees(intent.payee_mention, directory, context=said)
            if intent.payee_mention is not None
            else []
        )
        match_ids = {m.id for m in matches}

        if pick is not None:
            # The user picked a candidate in the UI: check the draft
            # against THAT payee's directory record as well as the
            # transcript.
            problems = _check_payee_pick(
                draft, pick, intent.payee_mention, matches, directory,
                user_texts[0] if user_texts else "",
            )
            if problems:
                discrepancies.append("payee")
                reasons.extend(problems)
        elif intent.payee_mention is not None:
            if draft.payee is not None and draft.payee.id not in match_ids:
                discrepancies.append("payee")
                payee_name = (
                    draft.payee.display_name if draft.payee else "unknown"
                )
                reasons.append(
                    f"The payee in the draft ({payee_name}) does not "
                    f"match who was said ({intent.payee_mention})."
                )
            elif draft.payee is not None and len(match_ids) > 1:
                # The transcript fits several payees, so only a pick can
                # say which one the user meant.  A resolved draft with no
                # pick on record has no evidence of that choice: refuse
                # it rather than pass because the name happens to fit.
                discrepancies.append("payee")
                reasons.append(
                    f"The transcript names more than one payee "
                    f"({intent.payee_mention!r} fits {len(match_ids)}) and "
                    f"no payee pick was recorded for the draft's payee "
                    f"({draft.payee.display_name})."
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


def _entry_freeze(
    clean_transcript: str,
    amount_text: str,
    h: str,
) -> Optional[ValidatorVerdict]:
    """The checks that run before anything is extracted, for every backend.

    Shared by :class:`LlmValidator` and
    :class:`~backend.validator.stub_validator.StubValidator` so a check
    cannot hold for one backend and not the other: a transcript longer
    than the limit, an amount that is negative, and an amount with more
    than two decimal places are all refused here, before either the
    model or the code reading ever sees the text.

    Returns the freeze verdict, or None when the entry checks pass.
    """
    if len(clean_transcript) > _MAX_TRANSCRIPT_LEN:
        return ValidatorVerdict(
            verdict="freeze",
            discrepancies=[],
            reason="transcript too long",
            draft_hash=h,
        )

    if is_negative_amount(amount_text):
        return ValidatorVerdict(
            verdict="freeze",
            discrepancies=["amount"],
            reason="The amount that was said is negative.",
            draft_hash=h,
        )

    if has_excess_decimals(amount_text):
        return ValidatorVerdict(
            verdict="freeze",
            discrepancies=["amount"],
            reason=(
                "The amount that was said has more than two decimal "
                "places and cannot be rounded."
            ),
            draft_hash=h,
        )

    return None


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
        with reason ``"validator_unavailable: <redacted cause>"``.
        """
        h = draft_hash(draft.model_dump(mode="json", exclude_none=True))

        # ── Sanitise once, at entry ──────────────────────────────
        # The cleaned transcript and answer are used for the prompt,
        # the length check, the negative-amount check and the
        # currency rule.
        clean_transcript = sanitise(transcript or "")
        clean_answer = (
            sanitise(clarification.answer) if clarification is not None else None
        )

        # ── Entry checks, shared with the stub backend ───────────
        # The amount comes from the answer when the amount was the
        # clarified field, otherwise from the transcript.
        amount_text = (
            clean_answer
            if clarification is not None and clarification.field == "amount"
            else clean_transcript
        )
        early = _entry_freeze(clean_transcript, amount_text, h)
        if early is not None:
            return early

        # ── Ask the model to extract ─────────────────────────────
        prompt = _build_prompt(clean_transcript, clarification, clean_answer)

        try:
            extraction = self._extract(prompt)
        except (ModelCallError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            # Fail closed, and say why (redacted: no key, no bodies).
            return ValidatorVerdict(
                verdict="freeze",
                discrepancies=[],
                reason=f"{UNAVAILABLE}: {describe_failure(exc)}",
                draft_hash=h,
            )

        # ── Code compares the extraction with the draft ─────────
        user_texts = (clean_transcript,) + (
            (clean_answer,) if clean_answer is not None else ()
        )
        discrepancies, reason, should_freeze = _compare(
            draft, extraction, self._directory, user_texts, clarification
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
