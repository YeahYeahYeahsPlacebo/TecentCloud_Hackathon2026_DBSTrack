"""
Parser interface.

The parser turns user text into a :class:`ParseResult` — a list of
items (each a :class:`TransactionDraft` or a :class:`ClarifyingQuestion`)
plus a count of distinct intents detected.  It never executes funds and
never imports the ledger or gateway (enforced by
``tests/test_import_boundaries.py``).

Contract:

- ``len(result.items)`` **must equal** ``result.intents_detected``.
  A parser that detects more intents than it returns items for has
  silently dropped an intent.
- A :class:`ClarifyingQuestion` item means **no draft exists yet** for
  that intent.  It is used for every unresolved field *except* ``payee``.
  Only ``payee`` may be unresolved inside an existing draft (see
  CONTRACT.md §2).
- A question item never carries a draft.

Per CONTRACT.md §2, only ``payee`` may appear in ``unresolved`` while a
draft exists.  If the parser cannot confidently resolve any *other*
required field (``intent_type``, ``source_account``, ``amount``,
``ticker``, ``quantity``, ``notional_amount``, ``order_type``), it does
**not** produce a draft at all — it returns a :class:`ClarifyingQuestion`
so the user can supply the missing value first.

**Clarification support** (this module, :mod:`backend.parser.clarify`):

- When the parser returns a :class:`ClarifyingQuestion`, it also
  populates :attr:`ParseResult.pending` — a mapping from
  ``question_id`` to :class:`PendingQuestion` — so the server can
  correlate the answer with the original transcript and partial fields.
- ``pending`` is **server-only** and must never be sent to the client.
  The client only sees ``question_id``, ``field``, and ``question``
  from each question item.
- :func:`backend.parser.clarify.resolve_payee` resolves a payee
  candidate pick on an existing draft (CONTRACT.md §4, first form).
- :func:`backend.parser.clarify.resolve_question` resolves a free-text
  answer to a :class:`ClarifyingQuestion` (CONTRACT.md §4, second form).
- Single-use enforcement (marking a ``question_id`` as consumed,
  refusing re-use) is the **server's** job — it holds storage.
  The pure functions in :mod:`backend.parser.clarify` do not and
  cannot enforce it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Union

from pydantic import BaseModel, ConfigDict

from backend.models.draft import TransactionDraft


class ClarifyingQuestion(BaseModel):
    """
    Returned when the parser cannot resolve a required field and must ask
    before producing any draft.

    Only used for fields *other than* ``payee`` — a payee ambiguity is
    expressed as a draft with ``"payee"`` in ``unresolved`` and
    ``payee_candidates`` populated (see CONTRACT.md §2).

    A question item never carries a draft.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str
    field: str
    question: str


# A single item in the result list: either a draft or a clarifying question.
ParseItem = Union[TransactionDraft, ClarifyingQuestion]


class PendingQuestion(BaseModel):
    """
    Server-only state for an unanswered :class:`ClarifyingQuestion`.

    **Never sent to the client.**  The client only sees the
    :class:`ClarifyingQuestion` item (``question_id``, ``field``,
    ``question``).  This model exists so the server can:

    - correlate an answer with the original transcript and the fields
      already resolved, and
    - hand the :func:`backend.parser.clarify.resolve_question` function
      everything it needs in one value.

    Attributes:
        question_id: Matches the ``question_id`` of the
            :class:`ClarifyingQuestion` the client saw.
        field: The unresolved field the question asks about.
        original_transcript: The user's original text, unchanged.  The
            answer is never merged into it (CONTRACT.md §4 second form).
        partial: The resolved fields so far, as a plain dict.  This is
            the starting point for the draft once the answer fills in
            the remaining field.
        created_at: When the question was issued.
        expires_at: When the question expires (same 5-minute window as
            the draft the question was derived from).

    Single-use enforcement (refusing a second answer for the same
    ``question_id``) is the **server's** job — it holds storage.  This
    model is a value object; it does not track consumption.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str
    field: str
    original_transcript: str
    partial: Dict[str, Any]
    created_at: datetime
    expires_at: datetime


class ClarificationRecord(BaseModel):
    """
    Audit record for a clarification answer.

    Returned by :func:`backend.parser.clarify.resolve_question` alongside
    the :class:`ParseResult` so the server can:

    - record the question_id, field, original transcript, and answer in
      the audit log, and
    - pass both the transcript and the answer (separately, never merged)
      to the validator (CONTRACT.md §4 second form).

    The answer is never merged into ``original_transcript``.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str
    field: str
    original_transcript: str
    answer: str


class ParseResult(BaseModel):
    """
    The return type of :func:`parse`.

    Attributes:
        items: A list whose elements are each a
            :class:`TransactionDraft` or a :class:`ClarifyingQuestion`.
        intents_detected: The number of distinct intents the parser found
            in the transcript.
        pending: **Server-only.**  A mapping from ``question_id`` to
            :class:`PendingQuestion` for every :class:`ClarifyingQuestion`
            in ``items``.  Defaults to ``{}`` when there are no
            questions.  **Must never be sent to the client.**

    Rule: ``len(items)`` must equal ``intents_detected``.  A parser that
    detects more intents than it returns items for has silently dropped
    an intent.
    """

    model_config = ConfigDict(extra="forbid")

    items: List[ParseItem]
    intents_detected: int
    pending: Dict[str, PendingQuestion] = {}


class ParserCannotHandle(Exception):
    """
    Raised when the stub parser does not recognise the input pattern,
    or when the transcript describes more than one transaction.

    A real (LLM) parser would attempt to parse anything, but the stub only
    handles a small fixed set of single-intent sentences.  Raising here is
    better than returning a malformed or guessed draft, and better than
    silently dropping one of two detected intents.
    """


class InvalidClarificationAnswer(Exception):
    """
    Raised by :func:`backend.parser.clarify.resolve_payee` and
    :func:`backend.parser.clarify.resolve_question` when a clarification
    answer is invalid.

    This maps to the ``invalid_clarification_answer`` reason code in
    CONTRACT.md §5.  The server catches it and returns a 422 rejection.

    Common causes:

    - The ``field`` is not in the stored draft's ``unresolved`` list.
    - The draft or pending question has expired.
    - The ``answer_id`` for a payee pick is not among the offered
      ``payee_candidates``, or does not exist in the directory.
    - The answer text contains a second request (multi-intent).
    - The answer text tries to change a field that is already resolved.
    """


def parse(transcript: str) -> ParseResult:
    """
    Parse a user transcript into a :class:`ParseResult`.

    Implementations must:

    * Treat ``transcript`` as **data**, never instructions (CONTEXT.md #5).
    * Never guess — if a field cannot be resolved with confidence, either
      put ``payee`` in ``unresolved`` (with candidates) or return a
      :class:`ClarifyingQuestion` for the field.
    * Return a draft that validates against ``contract/draft.schema.json``
      and ``backend/models/draft.py``.
    * Ensure ``len(result.items) == result.intents_detected``.
    * Decline a multi-intent transcript (raise
      :class:`ParserCannotHandle`) rather than returning a partial result
      that silently drops an intent.

    This function is the single entry point.  The stub implementation lives
    in :mod:`backend.parser.stub`; the real LLM parser will replace it later.
    """
    raise NotImplementedError("use backend.parser.stub.parse instead")
