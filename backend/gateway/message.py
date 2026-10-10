"""
`/api/message` and `/api/clarify` (CONTRACT.md §4).

Both return the same shape — ``intents_detected`` plus ``items`` — so the
client has one response shape across both endpoints. Each item is either:

``{"kind": "draft", "draft": {...}, "validator": {...}, "policy": {...}}``
    The server has stored this draft. ``validator`` and ``policy`` are attached
    **by the server**, never by the parser (CONTRACT.md §4).
``{"kind": "question", "question_id": ..., "field": ..., "question": ...}``
    No draft exists yet for that intent.

Two things this module deliberately does not own:

- **The parser** is injected. ``backend/parser`` belongs to Member 1 and the
  real LLM implementation lands there; this module only consumes its result.
- **The validator** is injected and defaults to a placeholder that returns
  ``pass``. ``backend/validator`` belongs to Member 1 and is still empty, so a
  real verdict cannot be produced yet. The placeholder is explicit rather than
  silently optimistic — see :func:`placeholder_validator`.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, MutableMapping, Optional

from backend.canonical import draft_hash
from backend.gateway import reason_codes as R
from backend.gateway.errors import rejection_response
from backend.models.draft import TransactionDraft
from backend.policy import PolicyEngine

# CONTRACT.md §8 open question 3: the fixtures use 5 minutes, but the intended
# policy window has not been decided. This is a starting point.
DRAFT_WINDOW = timedelta(minutes=5)

# The /api/clarify example moves confidence from 0.4 to 0.9 once the payee is
# resolved. The parser is not involved in a clarification, so the server sets
# it. Not covered by the hash (CONTRACT.md §9).
CLARIFIED_CONFIDENCE = 0.9

# Only payee may stay unresolved inside an existing draft (CONTRACT.md §2).
RESOLVABLE_IN_PLACE = "payee"


class ParserContractViolation(RuntimeError):
    """The parser returned fewer items than the intents it detected.

    That means an intent was silently dropped. It is a bug, not a rejection —
    there is no reason code for it, and the safe response is to fail loudly
    rather than to show the user a shorter list than their request described.
    """


def placeholder_validator(draft: Mapping[str, Any], transcript: str) -> dict:
    """Stands in for Member 1's validator until backend/validator lands.

    Returns ``pass`` with no discrepancies, which is what every example in
    CONTRACT.md §4 shows. It is a placeholder: it has not checked anything.
    """
    return {"verdict": "pass", "discrepancies": []}


def dump(draft: TransactionDraft) -> dict:
    """The hashed object (contract/CANONICAL_HASH.md)."""
    return draft.model_dump(mode="json", exclude_none=True)


def _now_whole_seconds(clock: Callable[[], datetime]) -> datetime:
    """Timestamps must be whole seconds and UTC (backend/models/draft.py)."""
    return clock().astimezone(timezone.utc).replace(microsecond=0)


def _new_nonce() -> str:
    return f"nonce-{secrets.token_hex(8)}"


class MessageService:
    """Turns user text into stored drafts with verdicts and policy decisions."""

    def __init__(
        self,
        *,
        parser: Callable[[str], Any],
        store: MutableMapping[str, Any],
        validator: Optional[Callable[[Mapping[str, Any], str], Mapping[str, Any]]] = None,
        policy: Optional[PolicyEngine] = None,
        audit: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
        window: timedelta = DRAFT_WINDOW,
        ledger: Optional[Any] = None,
        known_payees: Any = (),
        verdicts: Optional[MutableMapping[str, str]] = None,
    ) -> None:
        self.parser = parser
        self.store = store
        self.verdicts = verdicts
        self.validator = validator or placeholder_validator
        self.policy = policy or PolicyEngine()
        self.audit = audit
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.window = window
        self.ledger = ledger
        self.known_payees = set(known_payees)

    # ── What the policy engine is allowed to see ──────────────────────

    def _posted(self) -> list[Mapping[str, Any]]:
        return list(self.ledger.entries) if self.ledger is not None else []

    def _known(self) -> set:
        """Payees this account has already paid, plus any pre-declared."""
        known = set(self.known_payees)
        for entry in self._posted():
            payee = (entry.get("draft") or {}).get("payee")
            if isinstance(payee, Mapping) and payee.get("id"):
                known.add(payee["id"])
        return known

    def _attach(
        self, draft: TransactionDraft
    ) -> dict:
        """Store the draft and fold the server-side verdict and decision in."""
        self.store[draft.id] = draft
        dumped = dump(draft)
        digest = draft_hash(dumped)
        verdict = dict(self.validator(dumped, draft.transcript))
        if self.verdicts is not None:
            # Recorded by hash so /api/execute check 11 can find it. Without
            # this the gateway's fail-closed default would reject everything
            # the server itself just offered to the user.
            self.verdicts[digest] = str(verdict.get("verdict", ""))
        decision = self.policy.evaluate(
            dumped,
            digest,
            posted=self._posted(),
            known_payees=self._known(),
        )
        return {
            "kind": "draft",
            "draft": dumped,
            "validator": verdict,
            "policy": decision.to_response(),
        }

    def _question_item(self, question: Any) -> dict:
        return {
            "kind": "question",
            "question_id": getattr(question, "question_id"),
            "field": getattr(question, "field"),
            "question": getattr(question, "question"),
        }

    def _record(self, event: str, payload: Mapping[str, Any]) -> None:
        if self.audit is not None:
            self.audit.append(event, payload)

    # ── POST /api/message ─────────────────────────────────────────────

    def message(self, request: Mapping[str, Any]) -> dict:
        transcript = request.get("transcript") if isinstance(request, Mapping) else None
        if not isinstance(transcript, str) or not transcript.strip():
            return rejection_response(R.MALFORMED_REQUEST, "'transcript' must be a non-empty string")

        result = self.parser(transcript)
        if len(result.items) != result.intents_detected:
            raise ParserContractViolation(
                f"parser returned {len(result.items)} item(s) but detected "
                f"{result.intents_detected} intent(s)"
            )

        items = []
        for item in result.items:
            if isinstance(item, TransactionDraft):
                items.append(self._attach(item))
            else:
                items.append(self._question_item(item))

        self._record(
            "message",
            {
                "transcript": transcript,
                "intents_detected": result.intents_detected,
                "drafts": [i["draft"]["id"] for i in items if i["kind"] == "draft"],
            },
        )
        return {"intents_detected": result.intents_detected, "items": items}

    # ── POST /api/clarify ─────────────────────────────────────────────

    def clarify(self, request: Mapping[str, Any]) -> dict:
        if not isinstance(request, Mapping):
            return rejection_response(R.MALFORMED_REQUEST, "request body must be a JSON object")

        # The question form is proposed but not committed (CONTRACT.md §4),
        # so it is refused rather than half-implemented.
        if "question_id" in request:
            return rejection_response(
                R.INVALID_CLARIFICATION_ANSWER,
                "the question form of /api/clarify is proposed but not committed",
            )

        missing = [f for f in ("draft_id", "field", "answer") if f not in request]
        if missing:
            return rejection_response(R.MALFORMED_REQUEST, f"missing field(s): {missing}")

        stored = self.store.get(request["draft_id"])
        if stored is None:
            return rejection_response(
                R.UNKNOWN_DRAFT, f"no stored draft with id {request['draft_id']!r}"
            )

        field = request["field"]
        if field not in stored.unresolved:
            return rejection_response(
                R.INVALID_CLARIFICATION_ANSWER,
                f"{field!r} is not in this draft's unresolved list {stored.unresolved}",
            )
        if field != RESOLVABLE_IN_PLACE:
            # CONTRACT.md §2: only payee may be unresolved while a draft exists.
            return rejection_response(
                R.INVALID_CLARIFICATION_ANSWER,
                f"{field!r} cannot be clarified on an existing draft; "
                f"only {RESOLVABLE_IN_PLACE!r} can",
            )

        answer = request["answer"]
        candidate = next(
            (c for c in (stored.payee_candidates or []) if c.id == answer), None
        )
        if candidate is None:
            return rejection_response(
                R.INVALID_CLARIFICATION_ANSWER,
                f"answer {answer!r} is not one of the offered payee_candidates",
            )

        # A clarification produces a NEW draft with a NEW id and a NEW nonce.
        # The old draft is never modified in place, so the old id can never be
        # executed — the gateway loads by id.
        now = _now_whole_seconds(self.clock)
        data = dump(stored)
        data.pop("payee_candidates", None)  # omitted once the payee is resolved
        data.update(
            {
                "id": str(uuid.uuid4()),
                "nonce": _new_nonce(),
                "created_at": now,
                "expires_at": now + self.window,
                "unresolved": [f for f in stored.unresolved if f != field],
                "payee": {
                    "id": candidate.id,
                    "display_name": candidate.display_name,
                    "masked_account": candidate.masked_account,
                },
                "confidence": CLARIFIED_CONFIDENCE,
            }
        )
        resolved = TransactionDraft.model_validate(data)

        self._record(
            "clarify",
            {
                "from_draft_id": stored.id,
                "to_draft_id": resolved.id,
                "field": field,
                "answer": answer,
            },
        )
        return {"intents_detected": 1, "items": [self._attach(resolved)]}
