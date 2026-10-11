"""Shared fakes for gateway tests.

Shaped like the real ``backend/validator`` interface — a ``validate()`` method
returning a ``ValidatorVerdict`` — because the gateway accepts nothing else.
A plain callable is rejected on purpose (it cannot carry a
ClarificationRecord), so tests must use this rather than a lambda.

Kept out of conftest.py so Member 1's fixtures are untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.canonical import draft_hash  # noqa: E402
from backend.validator.interface import ValidatorVerdict  # noqa: E402


class FakeValidator:
    """A validator that returns a fixed verdict.

    ``validate()`` is the only accepted form; ``__call__`` raises, mirroring
    the real interface's deliberate refusal of the callable form.
    """

    def __init__(
        self,
        verdict: str = "pass",
        reason: str = "ok",
        discrepancies: list | None = None,
        bad_hash: str = "",
    ) -> None:
        self.verdict = verdict
        self.reason = reason
        self.discrepancies = discrepancies if discrepancies is not None else []
        self.bad_hash = bad_hash
        self.calls: list = []

    def validate(self, draft, transcript, clarification=None) -> ValidatorVerdict:
        self.calls.append((draft, transcript, clarification))
        digest = draft_hash(draft.model_dump(mode="json", exclude_none=True))
        return ValidatorVerdict(
            verdict=self.verdict,
            discrepancies=self.discrepancies,
            reason=self.reason,
            draft_hash=self.bad_hash or digest,
        )

    def __call__(self, *args, **kwargs):
        raise TypeError(
            "Call validate(draft, transcript, clarification=record); "
            "the callable form drops the ClarificationRecord."
        )


def passing_validator() -> FakeValidator:
    """The common case: a validator with nothing to object to."""
    return FakeValidator("pass", reason="ok")


def freezing_validator(reason: str = "amount disagrees") -> FakeValidator:
    """A validator that refuses the draft."""
    return FakeValidator("freeze", reason=reason, discrepancies=["amount"])
