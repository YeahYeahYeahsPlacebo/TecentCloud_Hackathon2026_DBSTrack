"""
Pure clarification functions.

No storage, no HTTP.  Member 3's service stores state and calls these.

Two functions:

1. :func:`resolve_payee` — resolve a payee candidate pick on an existing
   draft (CONTRACT.md §4, first form).  Returns ``(draft, record)``; the
   :class:`ClarificationRecord` must be passed to the validator.

2. :func:`resolve_question` — resolve a free-text answer to a
   :class:`~backend.parser.interface.ClarifyingQuestion` (CONTRACT.md §4,
   second form).  Uses a narrow LLM prompt that extracts just the asked
   field, with the answer isolated between ``<<<ANSWER>>>`` and
   ``<<<END>>>`` markers.

Both functions return a **new** draft — the input is never modified.

Single-use enforcement (marking a ``question_id`` as consumed, refusing
re-use) is the **server's** job — it holds storage.  These pure functions
do not and cannot enforce it.  See :class:`PendingQuestion` docstring.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

from backend.models.draft import (
    IntentType,
    Money,
    OrderType,
    Payee,
    PayeeCandidate,
    TransactionDraft,
    _check_money,
)
from backend.llm.text import sanitise
from backend.parser.amount_rules import (
    has_excess_decimals,
    is_negative_amount,
    is_negative_value,
    resolve_currency,
)
from backend.parser.directory import (
    PayeeDirectory,
    match_account_suffix,
    match_payees,
    resolve_ticker,
)
from backend.parser.speech_norm import normalise, spoken_amount
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

# Maximum clarification-answer length.  Longer answers are rejected with
# InvalidClarificationAnswer before anything else looks at them.
_MAX_ANSWER_LEN = 200

# "... to Bob": naming a recipient inside an answer for another field.
_TO_RECIPIENT_RE = re.compile(r"\bto\s+[^\W\d_]", re.IGNORECASE)

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
) -> Tuple[TransactionDraft, ClarificationRecord]:
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
        A tuple ``(draft, record)``:

        - ``draft`` is a **new** :class:`TransactionDraft` with
          a new ``id``, ``nonce``, ``created_at``, ``expires_at``
          (5-minute window), ``payee`` filled from the **directory**
          record, ``"payee"`` removed from ``unresolved``,
          ``payee_candidates`` omitted, and the same ``transcript``,
          ``amount``, ``source_account``, ``intent_type`` and other
          fields as the input.
        - ``record`` is the :class:`ClarificationRecord` for the pick:
          ``question_id`` is the stored draft's id (no separate question
          was issued), ``field`` is ``"payee"``, ``original_transcript``
          is the stored draft's transcript, ``answer`` is ``answer_id``
          and ``answer_kind`` is ``"candidate_id"``.

          **The record is required when the draft is validated.** Pass
          it to ``validator.validate(draft, transcript,
          clarification=record)``: the validator checks the draft's
          payee against the picked id's directory record, and refuses a
          draft whose transcript names more than one payee when no pick
          is supplied.

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
    record = next(
        (p for p in directory.payees() if p.id == answer_id), None
    )
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

    clarification = ClarificationRecord(
        question_id=stored_draft.id,
        field="payee",
        original_transcript=stored_draft.transcript,
        answer=answer_id,
        answer_kind="candidate_id",
    )
    return TransactionDraft.model_validate(data), clarification


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

    The answer is placed between <<<ANSWER>>> and <<<END>>> markers.
    *transcript* and *answer* are the sanitised texts produced once in
    :func:`resolve_question`, so they contain no markers.
    """
    template = _load_prompt_template()
    return (
        template
        .replace("<<<FIELD>>>", field)
        .replace("<<<TRANSCRIPT>>>", transcript)
        .replace("<<<USER_ANSWER>>>", answer)
    )


def _extract_amount_from_answer(answer: str) -> str | None:
    """Try to extract a monetary amount from a free-text answer.

    Returns a decimal string like "50.00" or None.  Digits win over word
    numbers: "50.50" is a number the user actually said, while "fifty" is
    a word this table has to look up.
    """
    lower = answer.lower().strip()

    # Direct money pattern: "$50.00", "50", "50.5".  Thousands separators
    # are allowed; anything with more than two decimals has already been
    # refused upstream (has_excess_decimals).
    m = re.search(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?![\d.])", lower)
    if m:
        whole = m.group(1).replace(",", "")
        cents = (m.group(2) or "").ljust(2, "0")
        return f"{int(whole)}.{cents}"

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
    directory: Any = None,
) -> bool:
    """Return True if the answer tries to change a field other than ``field``.

    Heuristic: looks for keywords associated with other fields.  For a
    non-payee question, naming a recipient ("to Bob") or any word of a
    saved payee's name also counts, because the payee is already fixed.
    """
    lower = answer.lower()

    # If we're asking about amount but the answer mentions payee names.
    if field != "payee":
        payee_keywords = ("payee", "account", "send to", "recipient")
        for kw in payee_keywords:
            if kw in lower:
                return True
        if _TO_RECIPIENT_RE.search(answer):
            return True
        if directory is not None:
            words = set(re.findall(r"[^\W\d_]+", lower))
            for p in directory.payees():
                if words & set(p.display_name.lower().split()):
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


@dataclass(frozen=True)
class _AnswerContext:
    """What an answer looks like once the code-side checks have passed.

    Both resolvers — the model-backed :func:`resolve_question` and the
    code-only :func:`resolve_answer_rules` — start from this, so the rules
    a valid answer has to obey live in one place rather than being copied
    into each backend.
    """

    field: str
    clean_answer: str
    clean_transcript: str
    partial: dict
    reading: Any = None


def _prepare_answer(
    pending: PendingQuestion,
    answer: str,
    directory: Any,
) -> tuple[Optional[_AnswerContext], Optional[Tuple[ParseResult, ClarificationRecord]]]:
    """Run the checks a free-text answer must pass before anything reads it.

    Returns ``(context, None)`` when the answer survived the checks, or
    ``(None, result)`` when the checks settled the question on their own —
    a re-ask for a vague amount, or a payee named by account suffix.

    These checks are shared by both resolvers on purpose: a rule the model
    path enforces has to hold when no model is involved too, or the two
    backends would disagree about what counts as a valid answer.
    """
    # ── Check: pending must not be expired ───────────────────────────
    now = datetime.now(timezone.utc)
    if _is_expired(pending.expires_at, now):
        raise InvalidClarificationAnswer(
            f"question {pending.question_id!r} has expired"
        )

    # ── 1. Length limit, on the answer exactly as received ───────────
    # Rejects before anything else reads the answer.  Sanitising only
    # ever shortens text, so the cleaned answer is within the limit too.
    if len(answer) > _MAX_ANSWER_LEN:
        raise InvalidClarificationAnswer(
            f"answer exceeds {_MAX_ANSWER_LEN} characters"
        )

    # ── 2. Sanitise once ─────────────────────────────────────────────
    # The cleaned texts are used for every check below and, on the model
    # path, for the prompt.  The raw answer is kept only in the audit
    # record.
    clean_answer = sanitise(answer)
    clean_transcript = sanitise(pending.original_transcript)

    if not clean_answer.strip():
        raise InvalidClarificationAnswer("answer is empty")

    # ── 3. Second request / change to an already-resolved field ──────
    if _detect_second_request(clean_answer):
        raise InvalidClarificationAnswer(
            "answer contains a second request; "
            "only the asked field may be answered"
        )

    if _detect_field_change_attempt(pending.field, clean_answer, directory):
        raise InvalidClarificationAnswer(
            f"answer attempts to change a field other than "
            f"{pending.field!r}, which is already resolved"
        )

    partial = dict(pending.partial)
    field = pending.field

    # A negative amount is asked again, never drafted and never sent
    # to the model (whose reply could drop the sign).
    if field == "amount" and is_negative_amount(clean_answer):
        return None, _reask(
            pending, answer, partial,
            "The amount can't be negative. How much would you like to send?",
        )

    # More than two decimal places is asked again, never rounded and
    # never sent to the model (which might round it).
    if field == "amount" and has_excess_decimals(clean_answer):
        return None, _reask(
            pending, answer, partial,
            "Please give the amount with at most two decimal places. "
            "How much would you like to send?",
        )

    # An answer that is only a masked-account suffix ("account ending
    # 8892") is resolved by code against the candidate list (the
    # directory), without the model.  It must select exactly one payee.
    if field == "payee":
        by_suffix = match_account_suffix(clean_answer, directory)
        if by_suffix is not None:
            if len(by_suffix) != 1:
                raise InvalidClarificationAnswer(
                    "account suffix matches "
                    f"{len(by_suffix)} candidate payees, expected exactly 1"
                )
            only = by_suffix[0]
            partial["payee"] = {
                "id": only.id,
                "display_name": only.display_name,
                "masked_account": only.masked_account,
            }
            return None, _finish(pending, answer, partial)

    # R3: a spoken amount is read by code.  "a few hundred", "some",
    # "about fifty" are never a number, whatever the model would say; an
    # exact phrase ("a grand") fixes the value and the model only
    # cross-checks it.  The answer is read after fillers are removed.
    reading = None
    if field == "amount":
        reading = spoken_amount(normalise(clean_answer).text)
        if reading.status in ("vague", "multiple"):
            return None, _reask(
                pending, answer, partial,
                'I need an exact amount, for example "fifty dollars". '
                "How much would you like to send?",
            )

    return (
        _AnswerContext(
            field=field,
            clean_answer=clean_answer,
            clean_transcript=clean_transcript,
            partial=partial,
            reading=reading,
        ),
        None,
    )


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
    context, settled = _prepare_answer(pending, answer, directory)
    if settled is not None:
        return settled

    field = context.field
    clean_answer = context.clean_answer
    clean_transcript = context.clean_transcript
    partial = context.partial
    answer_reading = context.reading

    # ── 4. Build the prompt and call the model ───────────────────────
    prompt = _build_answer_prompt(field, clean_transcript, clean_answer)
    reply = client.ask(prompt)
    reply_data = _parse_model_reply(reply.text)

    # ── Handle rejection from the model ──────────────────────────────
    if reply_data.get("rejected"):
        raise InvalidClarificationAnswer(
            f"answer rejected: {reply_data.get('reason', 'unknown')}"
        )

    # ── Handle unclear answer → new question for the same field ──────
    code_reads_amount = (
        answer_reading is not None and answer_reading.status == "exact"
    )
    if not reply_data.get("resolved") and not code_reads_amount:
        return _reask(pending, answer, partial, f"Could you clarify the {field}?")

    # ── Field resolved → build the draft ─────────────────────────────
    if field == "amount":
        amount_value = reply_data.get("amount_value")
        amount_text = reply_data.get("amount_text", "")

        # amount_text must appear in the (sanitised) answer.
        if amount_text and amount_text.lower() not in clean_answer.lower():
            raise InvalidClarificationAnswer(
                f"amount_text {amount_text!r} does not appear in the answer"
            )

        if is_negative_value(amount_value):
            return _reask(
                pending, answer, partial,
                "The amount can't be negative. How much would you like to send?",
            )

        if code_reads_amount:
            # Two readings of the same words must agree; if not, ask.
            if (
                amount_value
                and _MONEY_RE.fullmatch(amount_value)
                and amount_value != answer_reading.value
            ):
                return _reask(
                    pending, answer, partial,
                    "I could not be sure of the amount. "
                    "How much would you like to send?",
                )
            amount_value = answer_reading.value

        # amount_value must match the money pattern.
        if not amount_value or not _MONEY_RE.fullmatch(amount_value):
            raise InvalidClarificationAnswer(
                f"amount_value {amount_value!r} does not match the money pattern"
            )

        # Currency rule (amount_rules): the user's own words across the
        # transcript and the answer; never the model's.
        currency = resolve_currency(clean_transcript, clean_answer)
        if currency is None:
            return _reask(
                pending, answer, partial,
                "Which currency did you mean? How much would you like to send?",
            )

        partial["amount"] = {"value": amount_value, "currency": currency}

    elif field == "payee":
        payee_mention = reply_data.get("payee_mention", "")
        if not payee_mention:
            raise InvalidClarificationAnswer(
                "payee_mention is empty in the model reply"
            )

        # Name AND any account digits the user said must agree.
        matches = match_payees(payee_mention, directory, context=clean_answer)
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

        symbol = resolve_ticker(ticker_mention, directory)
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
    return _finish(pending, answer, partial)


def _read_amount(context: _AnswerContext) -> Optional[str]:
    """Read the amount out of an answer with code only (no model).

    A spoken number the code can read exactly wins — "fifty" is 50.00 by
    :func:`spoken_amount`, the same rule the parser uses.  Otherwise the
    digits the user typed are used, padded to two decimals.  None means
    neither gave a number, which is when the caller asks again instead of
    guessing.
    """
    reading = context.reading
    if reading is not None and reading.status == "exact":
        return reading.value
    return _extract_amount_from_answer(context.clean_answer)


def _read_order_type(answer: str) -> Optional[str]:
    """Read ``"market"``/``"limit"`` out of an answer, or None."""
    lower = answer.lower()
    if "limit" in lower:
        return "limit"
    if "market" in lower:
        return "market"
    return None


def resolve_answer_rules(
    pending: PendingQuestion,
    answer: str,
    directory: Any,
) -> Tuple[ParseResult, ClarificationRecord]:
    """Resolve a free-text answer with code only — no model, no client.

    The second form of ``/api/clarify`` (CONTRACT.md §4) run the way the
    stub parser runs the first: the shared checks of
    :func:`_prepare_answer`, then the field is read out of the answer by
    the same code the parser already trusts — :func:`spoken_amount` for a
    spoken number, :func:`match_payees` for a name, :func:`resolve_ticker`
    for a ticker.

    This is a **deterministic** resolver, not a stand-in for the model: it
    reads only what the user actually said and asks again when the answer
    does not name a value, so it cannot invent one. It is what makes the
    free-text form work on a server with no ADP keys, where the only
    answer it could give before was a 500.

    Raises:
        InvalidClarificationAnswer: the same cases as
            :func:`resolve_question`, plus a payee or ticker the directory
            does not know.
    """
    context, settled = _prepare_answer(pending, answer, directory)
    if settled is not None:
        return settled

    field = context.field
    clean_answer = context.clean_answer
    partial = context.partial

    if field == "amount":
        value = _read_amount(context)
        if value is None:
            return _reask(pending, answer, partial, f"Could you clarify the {field}?")

        # Currency rule (amount_rules): the user's own words across the
        # transcript and the answer, never a guess.
        currency = resolve_currency(context.clean_transcript, clean_answer)
        if currency is None:
            return _reask(
                pending, answer, partial,
                "Which currency did you mean? How much would you like to send?",
            )
        partial["amount"] = {"value": value, "currency": currency}

    elif field == "payee":
        matches = match_payees(clean_answer, directory, context=clean_answer)
        if not matches:
            raise InvalidClarificationAnswer(
                f"payee {clean_answer!r} not found in directory"
            )
        if len(matches) > 1:
            raise InvalidClarificationAnswer(
                f"payee {clean_answer!r} is ambiguous: {len(matches)} matches"
            )
        payee = matches[0]
        partial["payee"] = {
            "id": payee.id,
            "display_name": payee.display_name,
            "masked_account": payee.masked_account,
        }

    elif field == "ticker":
        symbol = resolve_ticker(clean_answer, directory)
        if symbol is None:
            raise InvalidClarificationAnswer(
                f"ticker {clean_answer!r} not found in directory"
            )
        partial["ticker"] = symbol

    elif field == "order_type":
        order_type = _read_order_type(clean_answer)
        if order_type is None:
            raise InvalidClarificationAnswer(
                f"order_type {clean_answer!r} is not valid"
            )
        partial["order_type"] = order_type

    else:
        raise InvalidClarificationAnswer(
            f"field {field!r} is not supported for clarification"
        )

    return _finish(pending, answer, partial)


def _finish(
    pending: PendingQuestion,
    answer: str,
    partial: dict[str, Any],
) -> Tuple[ParseResult, ClarificationRecord]:
    """Build the draft from the resolved partial and the audit record."""
    transcript = pending.original_transcript
    draft = _build_draft_from_partial(partial, transcript)
    result = ParseResult(
        items=[draft],
        intents_detected=1,
        pending={},
    )
    record = ClarificationRecord(
        question_id=pending.question_id,
        field=pending.field,
        original_transcript=transcript,
        answer=answer,
    )
    return result, record


def _reask(
    pending: PendingQuestion,
    answer: str,
    partial: dict[str, Any],
    question: str,
) -> Tuple[ParseResult, ClarificationRecord]:
    """Return a fresh question for the same field (no draft)."""
    field = pending.field
    qid = _fresh_question_id(field)
    created, expires = _fresh_timestamps()
    new_pending = PendingQuestion(
        question_id=qid,
        field=field,
        original_transcript=pending.original_transcript,
        partial=partial,
        created_at=created,
        expires_at=expires,
    )
    result = ParseResult(
        items=[ClarifyingQuestion(question_id=qid, field=field, question=question)],
        intents_detected=1,
        pending={qid: new_pending},
    )
    record = ClarificationRecord(
        question_id=pending.question_id,
        field=field,
        original_transcript=pending.original_transcript,
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
        currency = partial.get("amount", {}).get("currency") or resolve_currency(
            sanitise(transcript)
        )
        if currency is None:
            raise InvalidClarificationAnswer(
                "cannot build equity draft: currency is unclear"
            )
        data["amount"] = {"value": amount_value, "currency": currency}
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
