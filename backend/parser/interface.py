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
"""

from __future__ import annotations

from typing import List, Union

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


class ParseResult(BaseModel):
    """
    The return type of :func:`parse`.

    Attributes:
        items: A list whose elements are each a
            :class:`TransactionDraft` or a :class:`ClarifyingQuestion`.
        intents_detected: The number of distinct intents the parser found
            in the transcript.

    Rule: ``len(items)`` must equal ``intents_detected``.  A parser that
    detects more intents than it returns items for has silently dropped
    an intent.
    """

    model_config = ConfigDict(extra="forbid")

    items: List[ParseItem]
    intents_detected: int


class ParserCannotHandle(Exception):
    """
    Raised when the stub parser does not recognise the input pattern,
    or when the transcript describes more than one transaction.

    A real (LLM) parser would attempt to parse anything, but the stub only
    handles a small fixed set of single-intent sentences.  Raising here is
    better than returning a malformed or guessed draft, and better than
    silently dropping one of two detected intents.
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
