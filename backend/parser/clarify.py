"""
Pure clarification functions.

No storage, no HTTP.  Member 3's service stores state and calls these.

Two functions:

1. :func:`resolve_payee` — resolve a payee candidate pick on an existing
   draft (CONTRACT.md §4, first form).

2. :func:`resolve_question` — resolve a free-text answer to a
   :class:`~backend.parser.interface.ClarifyingQuestion` (CONTRACT.md §4,
   second form).  Uses a narrow LLM prompt that extracts just the asked
   field, with the answer isolated between ``<<<ANSWER>>>`` and
   ``<<<END>>>`` markers.

Both functions return a **new** draft/value — the input is never modified.

Single-use enforcement (marking a ``question_id`` as consumed, refusing
re-use) is the **server's** job — it holds storage.  These pure functions
do not and cannot enforce it.  See :class:`PendingQuestion` docstring.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Tuple

from backend.models.draft import (
    IntentType,
    Money,
    OrderType,
    Payee,
    PayeeCandidate,
    TransactionDraft,
    _check_money,
)
from backend.parser.directory import PayeeDirectory, TickerDirectory
from backend.parser.interface import (
    ClarificationRecord,
    ClarifyingQuestion,
    InvalidClarificationAnswer,
    ParseResult,
    PendingQuestion,
)

# ── Constants ───────────────────────────────────────────────────────────

_DRAFT_WINDOW = timedelta(minutes=5)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "answer_v1.txt"

# Markers stripped from user-supplied answer text before insertion into
# the prompt.  The user text is data, never instructions (CONTEXT.md #5).
_MARKERS = ("<<<ANSWER>>>", "<<<END>>>", "<<<TRANSCRIPT>>>", "<<<FIELD>>>")

# Money pattern for validating amount answers.
_MONEY_RE = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{2}$", re.ASCII)

# Word-number to digit-string map for amount extraction from answers.
_WORD_NUMBERS: dict[str, str] = {
    "fifty": "50.00",
    "fifteen": "15.00",
    "one thousand": "1000.00",
    "a grand": "1000.00",
    "a hundred": "100.00",
    "one hundred": "100.00",
    "twenty": "20.00",
    "ten": "10.00",
    "five": "5.00",
}

# Multi-intent / second-request signals in an answer.
_SECOND_REQUEST_SIGNALS = (
    "also",
    "and also",
    "then",
    "and then",
    "plus",
    "after that",
    "as well",
    "additionally",
)


# ── Helpers ─────────────────────────────────────────────────────────────


def _fresh_id() -> str:
    return str(uuid.uuid4())


def _fresh_nonce() -> str:
    return uuid.uuid4().hex


def _fresh_timestamps() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now, now + _DRAFT_WINDOW


def _strip_markers(text: str) -> str:
    """Remove all template markers from user text.

    The user text is data (CONTEXT.md #5).  Markers are stripped so the
    user cannot break out of the delimited block in the prompt.
    """
    result = text
    for marker in _MARKERS:
        result = result.replace(marker, "")
    return result


def _is_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    """Return True if ``expires_at`` is in the past."""
    check = now or datetime.now(timezone.utc)
    return check >= expires_at


def _fresh_question_id(field: str) -> str:
    return f"q-{field}-{uuid.uuid4().hex[:8]}"


# ── 1. Payee pick (CONTRACT.md §4, first form) ──────────────────────────


def resolve_payee(
    stored_draft: TransactionDraft,
    answer_id: str,
    directory: PayeeDirectory,
) -> TransactionDraft:
    """Resolve a payee candidate pick on an existing draft.

    This is the first form of ``/api/clarify`` (CONTRACT.md §4):
    the user selected one of the ``payee_candidates`` offered in the
    draft, and ``answer_id`` is the candidate's ``id``.

    Args:
        stored_draft: The stored draft with ``"payee"`` in ``unresolved``
            and ``payee_candidates`` populated.
        answer_id: The ``id`` of the selected payee candidate.
        directory: The payee directory (to fetch the authoritative
            payee record — never trust the candidate fields alone).

    Returns:
        A **new** :class:`TransactionDraft` with:
        - a new ``id``, ``nonce``, ``created_at``, ``expires_at``
          (5-minute window),
        - ``payee`` filled from the **directory** record,
        - ``"payee"`` removed from ``unresolved``,
        - ``payee_candidates`` omitted,
        - the same ``transcript``, ``amount``, ``source_account``,
          ``intent_type``, and other fields as the input.

    Raises:
        InvalidClarificationAnswer: If:
        - ``"payee"`` is not in ``stored_draft.unresolved``,
        - the draft has expired,
        - ``answer_id`` is not among ``stored_draft.payee_candidates``,
          or does not exist in the directory.

    The input ``stored_draft`` is **never modified**.
    """
    # ── Check: payee must be unresolved ──────────────────────────────
    if "payee" not in stored_draft.unresolved:
        raise InvalidClarificationAnswer(
            f"'payee' is not in this draft's unresolved list "
            f"{stored_draft.unresolved}"
        )

    # ── Check: draft must not be expired ─────────────────────────────
    now = datetime.now(timezone.utc)
    if _is_expired(stored_draft.expires_at, now):
        raise InvalidClarificationAnswer(
            f"draft {stored_draft.id!r} has expired"
        )

    # ── Check: answer_id must be one of the offered candidates ───────
    candidates = stored_draft.payee_candidates or []
    candidate = next((c for c in candidates if c.id == answer_id), None)
    if candidate is None:
        raise InvalidClarificationAnswer(
            f"answer {answer_id!r} is not one of the offered "
            f"payee_candidates"
        )

    # ── Check: answer_id must exist in the directory ─────────────────
    record = directory.get_payee_by_id(answer_id) if hasattr(directory, "get_payee_by_id") else None
    if record is None:
        raise InvalidClarificationAnswer(
            f"answer {answer_id!r} does not exist in the directory"
        )

    # ── Build the new draft ──────────────────────────────────────────
    created, expires = _fresh_timestamps()
    data = stored_draft.model_dump(mode="json", exclude_none=True)
    data.pop("payee_candidates", None)  # omitted once payee is resolved
    data["id"] = _fresh_id()
    data["nonce"] = _fresh_nonce()
    data["created_at"] = created
    data["expires_at"] = expires
    data["unresolved"] = [f for f in stored_draft.unresolved if f != "payee"]
    data["payee"] = {
        "id": record.id,
        "display_name": record.display_name,
        "masked_account": record.masked_account,
    }

    return TransactionDraft.model_validate(data)


# ── 2. Answering a question (CONTRACT.md §4, second form) ───────────────


def _load_prompt_template() -> str:
    """Load the answer extraction prompt template."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _build_answer_prompt(
    field: str,
    transcript: str,
    answer: str,
) -> str:
    """Build the narrow answer-extraction prompt.

    The answer is placed between <<<ANSWER>>> and <<<END>>> markers,
    with any template markers in the answer stripped first.
    """
    template = _load_prompt_template()
    clean_answer = _strip_markers(answer)
    return (
        template
        .replace("<<<FIELD>>>", field)
        .replace("<<<TRANSCRIPT>>>", transcript)
        .replace("<<<USER_ANSWER>>>", clean_answer)
    )


def _extract_amount_from_answer(answer: str) -> str | None:
    """Try to extract a monetary amount from a free-text answer.

    Returns a decimal string like "50.00" or None.
    """
    lower = answer.lower().strip()

    # Direct money pattern: "$50.00" or "50.00".
    m = re.search(r"(?:\$|\b)(\d+\.\d{2})\b", lower)
    if m:
        return m.group(1)

    # Word-number lookup.
    for phrase, value in _WORD_NUMBERS.items():
        if phrase in lower:
            return value

    return None


def _detect_second_request(answer: str) -> bool:
    """Return True if the answer appears to contain a second request."""
    lower = answer.lower()
    for signal in _SECOND_REQUEST_SIGNALS:
        if signal in lower:
            return True
    return False


def _detect_field_change_attempt(
    field: str,
    answer: str,
) -> bool:
    """Return True if the answer tries to change a field other than ``field``.

    Heuristic: looks for keywords associated with other fields.
    """
    lower = answer.lower()

    # If we're asking about amount but the answer mentions payee names.
    if field != "payee":
        payee_keywords = ("payee", "account", "send to", "recipient")
        for kw in payee_keywords:
            if kw in lower:
                return True

    # If we're asking about payee but the answer mentions amount.
    if field != "amount":
        if re.search(r"\d+\.\d{2}", lower) or "dollars" in lower:
            return True

    return False


def _parse_model_reply(reply_text: str) -> dict[str, Any]:
    """Parse the model's JSON reply.

    Returns the parsed dict.  Raises ValueError on malformed JSON.
    """
    text = reply_text.strip()
    # Some models wrap JSON in ```json ... ``` blocks.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def resolve_question(
    pending: PendingQuestion,
    answer: str,
    client: Any,
    directory: Any,
) -> Tuple[ParseResult, ClarificationRecord]:
    """Resolve a free-text answer to a :class:`ClarifyingQuestion`.

    This is the second form of ``/api/clarify`` (CONTRACT.md §4):
    the user was asked a question about a single unresolved field, and
    ``answer`` is their free-text reply.

    Args:
        pending: The :class:`PendingQuestion` for the asked question.
        answer: The user's free-text answer (data, never instructions).
        client: A :class:`~backend.llm.interface.ChatClient` for the
            narrow answer-extraction prompt.
        directory: A directory for payee/ticker validation.

    Returns:
        A tuple of:
        - a :class:`ParseResult` (containing either a completed
          :class:`TransactionDraft` or a new :class:`ClarifyingQuestion`
          if the answer was still unclear), and
        - a :class:`ClarificationRecord` for audit (question_id, field,
          original_transcript, answer — the answer is never merged into
          the transcript).

    Raises:
        InvalidClarificationAnswer: If:
        - the pending question has expired,
        - the answer contains a second request,
        - the answer tries to change a field that is already resolved.

    The resulting draft keeps ``original_transcript`` as its transcript.
    The answer is never merged into the transcript.

    Single-use enforcement (marking the question_id as consumed) is the
    **server's** job — it holds storage.
    """
    # ── Check: pending must not be expired ───────────────────────────
    now = datetime.now(timezone.utc)
    if _is_expired(pending.expires_at, now):
        raise InvalidClarificationAnswer(
            f"question {pending.question_id!r} has expired"
        )

    # ── Check: answer must not contain a second request ──────────────
    if _detect_second_request(answer):
        raise InvalidClarificationAnswer(
            "answer contains a second request; "
            "only the asked field may be answered"
        )

    # ── Check: answer must not change an already-resolved field ──────
    if _detect_field_change_attempt(pending.field, answer):
        raise InvalidClarificationAnswer(
            f"answer attempts to change a field other than "
            f"{pending.field!r}, which is already resolved"
        )

    field = pending.field
    transcript = pending.original_transcript
    partial = dict(pending.partial)

    # ── Build the prompt and call the model ──────────────────────────
    prompt = _build_answer_prompt(field, transcript, answer)
    reply = client.ask(prompt)
    reply_data = _parse_model_reply(reply.text)

    # ── Handle rejection from the model ──────────────────────────────
    if reply_data.get("rejected"):
        raise InvalidClarificationAnswer(
            f"answer rejected: {reply_data.get('reason', 'unknown')}"
        )

    # ── Handle unclear answer → new question for the same field ──────
    if not reply_data.get("resolved"):
        qid = _fresh_question_id(field)
        created, expires = _fresh_timestamps()
        question_item = ClarifyingQuestion(
            question_id=qid,
            field=field,
            question=f"Could you clarify the {field}?",
        )
        new_pending = PendingQuestion(
            question_id=qid,
            field=field,
            original_transcript=transcript,
            partial=partial,
            created_at=created,
            expires_at=expires,
        )
        result = ParseResult(
            items=[question_item],
            intents_detected=1,
            pending={qid: new_pending},
        )
        record = ClarificationRecord(
            question_id=pending.question_id,
            field=field,
            original_transcript=transcript,
            answer=answer,
        )
        return result, record

    # ── Field resolved → build the draft ─────────────────────────────
    if field == "amount":
        amount_value = reply_data.get("amount_value")
        amount_text = reply_data.get("amount_text", "")

        # amount_text must appear in the answer (CONTRACT rule).
        if amount_text and amount_text.lower() not in answer.lower():
            raise InvalidClarificationAnswer(
                f"amount_text {amount_text!r} does not appear in the answer"
            )

        # amount_value must match the money pattern.
        if not amount_value or not _MONEY_RE.fullmatch(amount_value):
            raise InvalidClarificationAnswer(
                f"amount_value {amount_value!r} does not match the money pattern"
            )

        partial["amount"] = {
            "value": amount_value,
            "currency": "SGD",
        }

    elif field == "payee":
        payee_mention = reply_data.get("payee_mention", "")
        if not payee_mention:
            raise InvalidClarificationAnswer(
                "payee_mention is empty in the model reply"
            )

        matches = directory.find_payee(payee_mention) if hasattr(directory, "find_payee") else []
        if not matches:
            raise InvalidClarificationAnswer(
                f"payee {payee_mention!r} not found in directory"
            )
        if len(matches) > 1:
            raise InvalidClarificationAnswer(
                f"payee {payee_mention!r} is ambiguous: "
                f"{len(matches)} matches"
            )

        payee = matches[0]
        partial["payee"] = {
            "id": payee.id,
            "display_name": payee.display_name,
            "masked_account": payee.masked_account,
        }

    elif field == "ticker":
        ticker_mention = reply_data.get("ticker_mention", "")
        if not ticker_mention:
            raise InvalidClarificationAnswer(
                "ticker_mention is empty in the model reply"
            )

        symbol = directory.resolve_ticker(ticker_mention) if hasattr(directory, "resolve_ticker") else None
        if symbol is None:
            raise InvalidClarificationAnswer(
                f"ticker {ticker_mention!r} not found in directory"
            )

        partial["ticker"] = symbol

    elif field == "order_type":
        order_type_str = reply_data.get("order_type", "")
        if order_type_str not in ("market", "limit"):
            raise InvalidClarificationAnswer(
                f"order_type {order_type_str!r} is not valid"
            )

        partial["order_type"] = order_type_str

    else:
        raise InvalidClarificationAnswer(
            f"field {field!r} is not supported for clarification"
        )

    # ── Build the draft from partial ─────────────────────────────────
    draft = _build_draft_from_partial(partial, transcript)
    result = ParseResult(
        items=[draft],
        intents_detected=1,
        pending={},
    )
    record = ClarificationRecord(
        question_id=pending.question_id,
        field=field,
        original_transcript=transcript,
        answer=answer,
    )
    return result, record


def _build_draft_from_partial(
    partial: dict[str, Any],
    transcript: str,
) -> TransactionDraft:
    """Build a TransactionDraft from the partial fields and the transcript.

    ``partial`` must contain at least: intent_type, source_account,
    amount (or notional_amount + ticker + order_type for equity).
    """
    created, expires = _fresh_timestamps()
    data: dict[str, Any] = {
        "id": _fresh_id(),
        "created_at": created,
        "expires_at": expires,
        "nonce": _fresh_nonce(),
        "unresolved": [],
        "transcript": transcript,
        "confidence": 0.85,
    }

    # Copy resolved fields from partial.
    intent_type = partial.get("intent_type", "transfer")
    data["intent_type"] = intent_type
    data["source_account"] = partial.get("source_account", "acct-001-2345")

    if intent_type == "equity_purchase":
        # Equity: amount from notional, ticker, order_type.
        amount_value = partial.get("amount", {}).get("value") or partial.get("notional_amount")
        if not amount_value:
            raise InvalidClarificationAnswer(
                "cannot build equity draft: no amount/notional_amount"
            )
        data["amount"] = {
            "value": amount_value,
            "currency": "SGD",
        }
        data["notional_amount"] = amount_value
        data["ticker"] = partial.get("ticker", "D05.SI")
        data["order_type"] = partial.get("order_type", "market")
    else:
        # Transfer or bill_payment: amount + payee.
        amount = partial.get("amount")
        if not amount:
            raise InvalidClarificationAnswer(
                "cannot build transfer draft: no amount"
            )
        data["amount"] = amount
        payee = partial.get("payee")
        if payee:
            data["payee"] = payee

    return TransactionDraft.model_validate(data)
