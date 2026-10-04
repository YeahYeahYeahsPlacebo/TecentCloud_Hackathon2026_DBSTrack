"""
Parser interface.

The parser turns user text into either a :class:`TransactionDraft` or a
:class:`ClarifyingQuestion`.  It never executes funds and never imports the
ledger or gateway (enforced by ``tests/test_import_boundaries.py``).

Per CONTRACT.md §2, only ``payee`` may appear in ``unresolved`` while a
draft exists.  If the parser cannot confidently resolve any *other*
required field (``intent_type``, ``source_account``, ``amount``,
``ticker``, ``quantity``, ``notional_amount``, ``order_type``), it does
**not** produce a draft at all — it returns a :class:`ClarifyingQuestion`
so the user can supply the missing value first.
"""

from __future__ import annotations

from typing import Union

from pydantic import BaseModel, ConfigDict

from backend.models.draft import TransactionDraft


class ClarifyingQuestion(BaseModel):
    """
    Returned when the parser cannot resolve a required field and must ask
    before producing any draft.

    Only used for fields *other than* ``payee`` — a payee ambiguity is
    expressed as a draft with ``"payee"`` in ``unresolved`` and
    ``payee_candidates`` populated (see CONTRACT.md §2).
    """

    model_config = ConfigDict(extra="forbid")

    field: str
    question: str


# The return type of ``parse``.
ParseResult = Union[TransactionDraft, ClarifyingQuestion]


class ParserCannotHandle(Exception):
    """
    Raised when the stub parser does not recognise the input pattern.

    A real (LLM) parser would attempt to parse anything, but the stub only
    handles a small fixed set of sentences.  Raising here is better than
    returning a malformed or guessed draft.
    """


def parse(transcript: str) -> ParseResult:
    """
    Parse a user transcript into a draft or a clarifying question.

    Implementations must:

    * Treat ``transcript`` as **data**, never instructions (CONTEXT.md #5).
    * Never guess — if a field cannot be resolved with confidence, either
      put ``payee`` in ``unresolved`` (with candidates) or return a
      :class:`ClarifyingQuestion` for the field.
    * Return a draft that validates against ``contract/draft.schema.json``
      and ``backend/models/draft.py``.

    This function is the single entry point.  The stub implementation lives
    in :mod:`backend.parser.stub`; the real LLM parser will replace it later.
    """
    raise NotImplementedError("use backend.parser.stub.parse instead")
