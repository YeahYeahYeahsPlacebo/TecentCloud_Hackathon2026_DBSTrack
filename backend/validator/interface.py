"""
Validator interface: data models and protocol.

The validator independently extracts the user's intent from the
transcript and compares it with the compiled draft.  It never sees the
draft in its prompt (CONTEXT.md constraint 4).
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from backend.canonical import draft_hash
from backend.models.draft import TransactionDraft
from backend.parser.interface import ClarificationRecord


class ValidatorVerdict(BaseModel):
    """The result of validating a draft against a transcript.

    Attributes:
        verdict: ``"pass"`` if the extraction matches the draft,
            ``"freeze"`` if they disagree or the validator could not
            produce a result.
        discrepancies: Field names that disagreed between the model's
            extraction and the draft (e.g. ``["amount"]``).  Empty when
            the verdict is ``"pass"``.
        reason: A plain-language sentence explaining the verdict (e.g.
            ``"The amount in the draft (500.00) does not match what was
            said (50.00)."``).  ``"validator_unavailable"`` when the
            model failed and the validator could not run.
        draft_hash: The SHA-256 hex digest of the draft that was
            checked, so the gateway can bind this verdict to that exact
            draft (CONTRACT.md §4, check 11).
    """

    model_config = ConfigDict(extra="forbid")

    verdict: str
    discrepancies: list[str] = []
    reason: str
    draft_hash: str


@runtime_checkable
class Validator(Protocol):
    """Protocol for a validation agent.

    The validator compares the transcript to the draft.  It never sees
    the draft in its prompt — the model extracts independently, and code
    compares.
    """

    def validate(
        self,
        draft: TransactionDraft,
        transcript: str,
        clarification: Optional[ClarificationRecord] = None,
    ) -> ValidatorVerdict: ...
