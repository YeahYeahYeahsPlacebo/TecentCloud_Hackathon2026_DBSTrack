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

- **The parser** is injected. ``backend/parser`` belongs to Member 1; this
  module only consumes its result.
- **The validator** is injected and **required**. There is no default that can
  produce ``pass``: a validator that has not been configured returns
  ``freeze`` / ``validator_not_configured`` (``UnconfiguredValidator``), so an
  unconfigured server refuses to execute rather than waving drafts through.
  ``DCTA_ALLOW_UNVALIDATED=true`` opts into a pass, explicitly, for local dev.

A validator must expose ``validate(draft, transcript, clarification=None)``.
Anything else is a wiring error and fails loudly at construction — see
:func:`require_validator`.
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
from backend.parser.clarify import resolve_payee, resolve_question
from backend.parser.interface import InvalidClarificationAnswer, ParserCannotHandle, ParseResult
from backend.parser.independent import independent_transfer_clauses, independent_draft_matches
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


def _cannot_handle_code(exc: Exception) -> str:
    """Pick the reason code for a :class:`ParserCannotHandle`.

    ``exc.reason`` is informational ("model: 2 intents", "keyword guard"), so
    it cannot be sent as a code. Anything that names a second intent is the
    phase-5 refusal; everything else is "the parser does not know this one".
    """
    reason = getattr(exc, "reason", "") or ""
    lowered = reason.lower()
    if "intent" in lowered:
        return R.MULTI_INTENT_UNSUPPORTED
    return R.PARSER_CANNOT_HANDLE


class ParserContractViolation(RuntimeError):
    """The parser returned fewer items than the intents it detected.

    That means an intent was silently dropped. It is a bug, not a rejection —
    there is no reason code for it, and the safe response is to fail loudly
    rather than to show the user a shorter list than their request described.
    """


class ValidatorUnusable(RuntimeError):
    """The injected validator does not have the required interface.

    Raised at construction rather than at request time: a misconfigured server
    should refuse to start, not serve requests and fail on the first one. It
    also replaces what used to be a callable fallback — a validator without
    ``validate()`` can no longer be invoked at all, because the only thing
    such a call could produce is either a crash or an unchecked ``pass``.
    """


class ServerNotConfigured(RuntimeError):
    """The server cannot answer this request because it is not wired up.

    Distinct from a rejection: a rejection means *the user's request* was
    refused and gets a reason code (CONTRACT.md §5). This means *we* are
    missing something — a payee directory, a chat client — so there is no
    reason code that describes it and no safe way to answer. Like
    :class:`ParserContractViolation`, it is a 500, not a 422.
    """


def require_validator(validator: Any) -> Any:
    """Return *validator* if it can validate a draft, else raise.

    The interface (``backend/validator/interface.py``) is::

        validator.validate(draft: TransactionDraft, transcript: str,
                           clarification: ClarificationRecord | None = None)
            -> ValidatorVerdict

    Its ``__call__`` is deliberately disabled, so duck-typing on ``validate``
    is the only correct way to detect it.
    """
    if validator is None:
        raise ValidatorUnusable(
            "no validator was injected. Build one with "
            "backend.validator.factory.get_validator() — there is no "
            "default that returns pass."
        )
    if not callable(getattr(validator, "validate", None)):
        raise ValidatorUnusable(
            f"validator {type(validator).__name__} has no callable validate(). "
            "Expected validate(draft, transcript, clarification=None) -> "
            "ValidatorVerdict. A plain callable is not accepted: it cannot "
            "carry the ClarificationRecord, and there is no safe way to call it."
        )
    return validator


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
        # Not ``Callable[[...], ...]``: the validator is an object exposing
        # ``validate()``, and its ``__call__`` is deliberately disabled, so
        # typing it as a callable described a shape that is now refused.
        # ``Any`` is honest — see :func:`require_validator` for the interface
        # that is actually checked.
        validator: Optional[Any] = None,
        policy: Optional[PolicyEngine] = None,
        audit: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
        window: timedelta = DRAFT_WINDOW,
        ledger: Optional[Any] = None,
        known_payees: Any = (),
        verdicts: Optional[MutableMapping[str, str]] = None,
        directory: Optional[Any] = None,
        client: Optional[Any] = None,
        answer_resolver: Optional[Any] = None,
    ) -> None:
        """Build the service.

        ``answer_resolver`` decides how the free-text form of
        ``/api/clarify`` reads an answer. ``None`` means "use the model",
        which needs ``client``; pass
        :func:`backend.parser.clarify.resolve_answer_rules` to read the
        answer with code instead, which needs no client at all.
        """
        self.parser = parser
        self.store = store
        self.verdicts = verdicts
        # No default that can return "pass". A missing or wrongly shaped
        # validator is a wiring error and is caught here, at construction.
        self.validator = require_validator(validator)
        self.policy = policy or PolicyEngine()
        self.audit = audit
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.window = window
        self.ledger = ledger
        self.known_payees = set(known_payees)
        # For /api/clarify: resolve_payee needs the directory to fetch the
        # authoritative payee record, and resolve_question needs a chat client
        # for the narrow answer-extraction prompt.
        self.directory = directory
        self.client = client
        self.answer_resolver = answer_resolver
        # Server-only, keyed by question_id. A PendingQuestion carries the
        # original transcript and the partial draft — it is never serialised
        # into any response. Consumed once: answering deletes it, and letting
        # it expire deletes it too (see _prune_pending).
        self.pending: dict[str, Any] = {}
        # Server-only: draft id -> the id of the draft its payee was resolved
        # into. A clarification is single-use in both directions — the
        # ambiguous draft stays stored (and stays unresolved, so it can never
        # be executed), but it cannot be picked a second time.
        self.clarified: dict[str, str] = {}

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
        self, draft: TransactionDraft, clarification: Optional[Any] = None
    ) -> dict:
        """Store the draft and fold the server-side verdict and decision in.

        ``clarification`` is the ``ClarificationRecord`` returned alongside a
        resolved question. It goes to the validator so it can compare the
        answer against the draft — the answer is never merged into the
        transcript.
        """
        item = self._prepare(draft, clarification)
        self._publish(draft, item)
        return item

    def _publish(self, draft: TransactionDraft, item: dict) -> None:
        self.store[draft.id] = draft
        if self.verdicts is not None:
            self.verdicts[draft_hash(item["draft"])] = str(item["validator"].get("verdict", ""))

    def _prepare(self, draft: TransactionDraft, clarification: Optional[Any] = None) -> dict:
        """Validate and evaluate policy without publishing draft/verdict state."""
        dumped = dump(draft)
        digest = draft_hash(dumped)
        verdict = self._run_validator(draft, dumped, digest, clarification=clarification)
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

    def _run_validator(
        self,
        draft: TransactionDraft,
        dumped: Mapping[str, Any],
        digest: str,
        clarification: Optional[Any] = None,
    ) -> dict:
        """Run the validator and return a plain dict for the response.

        The only supported shape is the one ``backend/validator`` exposes::

            validator.validate(draft, transcript, clarification=...)
                -> ValidatorVerdict

        Its ``__call__`` is deliberately disabled and raises, so it must be
        called as a method. A validator without ``validate()`` is refused
        (``ValidatorUnusable``) rather than invoked — there is no callable
        fallback, because such a call could only crash or produce an unchecked
        ``pass``.

        The verdict string is what check 11 reads, and anything other than
        ``"pass"`` is rejected there, so a validator that fails to produce a
        verdict fails closed rather than passing silently.
        """
        validator = self.validator

        if not callable(getattr(validator, "validate", None)):
            # Not reachable via the constructor, which rejects such a validator
            # up front. Kept as a hard error rather than a fallback: the only
            # thing a callable-form call could produce is a crash or an
            # unchecked pass, and neither is acceptable here.
            raise ValidatorUnusable(
                f"validator {type(validator).__name__} has no validate() method; "
                "refusing to produce a verdict. Fix the wiring — this path "
                "fails closed and never passes."
            )

        result = validator.validate(draft, draft.transcript, clarification=clarification)
        payload = (
            result.model_dump() if hasattr(result, "model_dump") else dict(result)
        )
        return self._bind_verdict(payload, digest)

    @staticmethod
    def _bind_verdict(payload: Mapping[str, Any], digest: str) -> dict:
        """Refuse a verdict that was produced for a different draft.

        ``ValidatorVerdict`` carries the hash of the draft it actually
        checked. That exists so the gateway can bind the verdict to that exact
        draft (CONTRACT.md §4, check 11). If it disagrees with the hash we
        computed, the verdict is about something else — treat it as a freeze
        rather than recording a pass we cannot attribute.
        """
        bound = payload.get("draft_hash")
        if not isinstance(bound, str) or not bound:
            # An empty or missing hash binds to nothing. Accepting it here
            # would mean recording a verdict — possibly a "pass" — that no
            # one can attribute to a draft, which is exactly what check 11
            # exists to prevent. A verdict that cannot name its draft has not
            # been proven to be about this one.
            return {
                **dict(payload),
                "verdict": "freeze",
                "reason": (
                    "verdict carries no draft_hash, so it cannot be bound to "
                    f"the stored draft {digest}"
                ),
            }
        if bound != digest:
            return {
                **dict(payload),
                "verdict": "freeze",
                "reason": (
                    f"verdict was produced for draft {bound}, "
                    f"not the stored draft {digest}"
                ),
            }
        return dict(payload)

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

        try:
            clauses = independent_transfer_clauses(transcript, self.directory)
            if clauses is None:
                result = self.parser(transcript)
            else:
                # Parse both before storing either. Do not silently drop an
                # intent, accept a model-created split, or turn this into a group.
                results = [self.parser(clause) for clause in clauses]
                for clause, child in zip(clauses, results):
                    if child.intents_detected != 1 or len(child.items) != 1:
                        raise ParserContractViolation("each independent clause must produce exactly one item")
                    if not isinstance(child.items[0], TransactionDraft) or child.items[0].intent_type != "transfer":
                        raise ParserCannotHandle(
                            "Please send these requests separately; each independent clause must produce a transfer draft.",
                            reason="multi-intent unsupported",
                        )
                    if child.pending or not independent_draft_matches(clause, child.items[0], self.directory):
                        raise ParserContractViolation("independent draft must match its verbatim source clause and offered payees")
                result = ParseResult(items=[child.items[0] for child in results], intents_detected=2)
        except ParserCannotHandle as exc:
            # Outside the narrow independent-transfer exception, before phase 5
            # a transcript describing more than one transaction is refused with
            # nothing drafted. The parser raises rather than dropping one of
            # two intents; the gateway is what turns that into a rejection,
            # because "reject with a reason code" is a gateway job.
            return rejection_response(_cannot_handle_code(exc), str(exc) or "parser cannot handle this transcript")

        if len(result.items) != result.intents_detected:
            raise ParserContractViolation(
                f"parser returned {len(result.items)} item(s) but detected "
                f"{result.intents_detected} intent(s)"
            )

        # Server-only. ParseResult.pending is a mapping of question_id ->
        # PendingQuestion; the client sees only the ClarifyingQuestion.
        for question_id, pending_state in (result.pending or {}).items():
            self.pending[question_id] = pending_state

        if clauses is not None:
            return self._independent_response(result.items, transcript)

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

    def _independent_response(self, drafts: list, transcript: str) -> dict:
        # No draft or passing verdict becomes visible until BOTH validators,
        # BOTH policy evaluations and the original-transcript audit succeed.
        ids = [d.id for d in drafts]
        nonces = [d.nonce for d in drafts]
        digests = [draft_hash(dump(d)) for d in drafts]
        if (len(set(ids)) != 2 or len(set(nonces)) != 2 or len(set(digests)) != 2
                or any(key in self.store for key in ids)
                or (self.verdicts is not None and any(key in self.verdicts for key in digests))):
            raise ParserContractViolation("independent drafts must have fresh distinct identities")
        items = [self._prepare(draft) for draft in drafts]
        self._record("message", {"transcript": transcript, "intents_detected": 2, "drafts": ids})
        try:
            for draft, item in zip(drafts, items):
                self._publish(draft, item)
        except Exception:
            # Also fail closed for a store implementation that raises during
            # publication. Only this request's fresh keys may be removed.
            for key in ids:
                self.store.pop(key, None)
            if self.verdicts is not None:
                for key in digests:
                    self.verdicts.pop(key, None)
            raise
        return {"intents_detected": 2, "items": items,
                "request_mode": "independent_transfers", "source_transcript": transcript}

    # ── POST /api/clarify ─────────────────────────────────────────────

    def clarify(self, request: Mapping[str, Any]) -> dict:
        """Two forms (CONTRACT.md §4), both owned by ``backend/parser``.

        1. ``{question_id, answer}`` — a free-text reply to a
           ``ClarifyingQuestion``. Calls ``resolve_question``.
        2. ``{draft_id, field, answer}`` — picking one of a draft's
           ``payee_candidates``. Calls ``resolve_payee``.

        Both may raise ``InvalidClarificationAnswer``, which is a 422 with
        ``invalid_clarification_answer`` — not a 500. Neither modifies the
        stored draft: each returns a new one.

        Both are single-use, and the checks that make them so live here in the
        dispatcher rather than inside either form: the rule is about server
        state, and keeping it in one place means it holds whichever way the
        forms below are written.
        """
        if not isinstance(request, Mapping):
            return rejection_response(R.MALFORMED_REQUEST, "request body must be a JSON object")

        # A question that has expired is gone before either form looks for it,
        # so a late answer cannot resurrect it.
        self._prune_pending()

        if "question_id" in request:
            return self._clarify_question(request)
        return self._clarify_once(request)

    def _prune_pending(self) -> None:
        """Forget pending questions whose window has closed.

        A ``PendingQuestion`` expires with the draft it was derived from
        (CONTRACT.md §4, second form). Without this the map only ever grows,
        and a question the server would itself refuse as expired stays live
        long enough to be replayed.
        """
        now = self.clock()
        for question_id, pending_state in list(self.pending.items()):
            expires_at = getattr(pending_state, "expires_at", None)
            if expires_at is not None and now >= expires_at:
                self.pending.pop(question_id, None)

    @staticmethod
    def _require_strings(request: Mapping[str, Any], *fields: str) -> Optional[dict]:
        """Reject any of *fields* that was supplied but is not a string.

        A dict or a list is unhashable, so using one as a ``draft_id`` or
        ``question_id`` raises inside the store lookup and turns a malformed
        body into a 500. The contract already has a code for this shape of
        mistake: ``malformed_request``.
        """
        for field in fields:
            if field in request and not isinstance(request[field], str):
                return rejection_response(R.MALFORMED_REQUEST, f"{field!r} must be a string")
        return None

    def _clarify_once(self, request: Mapping[str, Any]) -> dict:
        """The payee form, at most once per draft (CONTRACT.md §4).

        Picking a candidate twice would produce two drafts from one
        ambiguity, so the ambiguous draft is marked once its payee is
        resolved. It stays stored and stays unresolved — that is what makes
        it unexecutable — but it cannot be picked again.

        A refused answer does not consume anything: the user has to be able
        to answer again with a candidate that was actually offered.
        """
        if "draft_id" not in request:
            # Not supplied at all: the form below names the missing field,
            # which is a better message than anything this check could give.
            return self._clarify_payee(request)

        shaped = self._require_strings(request, "draft_id")
        if shaped is not None:
            return shaped
        draft_id = request["draft_id"]

        if draft_id in self.clarified:
            replacement = self.clarified[draft_id]
            return rejection_response(
                R.INVALID_CLARIFICATION_ANSWER,
                f"draft {draft_id!r} has already been clarified"
                + (
                    f" into draft {replacement!r}; clarify that one"
                    if replacement
                    else " and cannot be clarified twice"
                ),
            )

        result = self._clarify_payee(request)
        if result.get("result") != "rejected":
            items = result.get("items") or []
            resolved_id = ""
            if items and isinstance(items[0], Mapping):
                resolved_id = str((items[0].get("draft") or {}).get("id", ""))
            self.clarified[draft_id] = resolved_id
        return result

    # ── Form 1: answering a clarifying question ────────────────────────

    def _clarify_question(self, request: Mapping[str, Any]) -> dict:
        missing = [f for f in ("question_id", "answer") if f not in request]
        if missing:
            return rejection_response(R.MALFORMED_REQUEST, f"missing field(s): {missing}")

        shaped = self._require_strings(request, "question_id", "answer")
        if shaped is not None:
            return shaped

        question_id = request["question_id"]
        pending_state = self.pending.get(question_id)
        if pending_state is None:
            return rejection_response(
                R.INVALID_CLARIFICATION_ANSWER,
                f"no pending question with id {question_id!r} — it was never "
                f"issued, already answered, or expired",
            )

        if self.directory is None:
            # Not the user's fault, so not a rejection: there is no reason
            # code for "the server cannot answer this". Fail loudly instead
            # of returning a 422 that blames the answer.
            raise ServerNotConfigured(
                "cannot resolve a free-text answer: 'directory' is not "
                "configured on this server. Pass directory= when building "
                "the gateway."
            )

        # Two ways to read an answer: through a model (needs client=), or
        # with code (answer_resolver=). With neither, the answer cannot be
        # read, which is a wiring gap and stays a loud 500 — never a guess.
        resolver = self.answer_resolver
        if resolver is None and self.client is None:
            raise ServerNotConfigured(
                "cannot resolve a free-text answer: no chat client and no "
                "answer resolver are configured on this server. Pass client= "
                "for the model path, or answer_resolver=resolve_answer_rules "
                "to read answers with code and no model."
            )

        try:
            if resolver is not None:
                parsed, record = resolver(
                    pending_state, request["answer"], self.directory
                )
            else:
                parsed, record = resolve_question(
                    pending_state, request["answer"], self.client, self.directory
                )
        except InvalidClarificationAnswer as exc:
            return rejection_response(R.INVALID_CLARIFICATION_ANSWER, str(exc))

        # Single use: the question cannot be answered twice, and a replayed
        # answer finds nothing to resolve.
        self.pending.pop(question_id, None)

        self._record(
            "clarify",
            {
                "question_id": record.question_id,
                "field": record.field,
                "answer": record.answer,
                # CONTRACT.md §4, second form: the server stores the original
                # transcript and the answer separately and records both in
                # the audit log. Without this the log holds an answer with
                # nothing it was an answer to.
                "transcript": record.original_transcript,
            },
        )

        items = []
        for item in parsed.items:
            if isinstance(item, TransactionDraft):
                items.append(self._attach(item, clarification=record))
            else:
                items.append(self._question_item(item))
        # A follow-up question carries its own pending state.
        for new_id, new_pending in (parsed.pending or {}).items():
            self.pending[new_id] = new_pending

        return {"intents_detected": parsed.intents_detected, "items": items}

    # ── Form 2: picking a payee candidate ──────────────────────────────

    def _clarify_payee(self, request: Mapping[str, Any]) -> dict:
        missing = [f for f in ("draft_id", "field", "answer") if f not in request]
        if missing:
            return rejection_response(R.MALFORMED_REQUEST, f"missing field(s): {missing}")

        stored = self.store.get(request["draft_id"])
        if stored is None:
            return rejection_response(
                R.UNKNOWN_DRAFT, f"no stored draft with id {request['draft_id']!r}"
            )

        field = request["field"]
        if field != RESOLVABLE_IN_PLACE:
            # CONTRACT.md §2: only payee may be unresolved while a draft exists.
            return rejection_response(
                R.INVALID_CLARIFICATION_ANSWER,
                f"{field!r} cannot be clarified on an existing draft; "
                f"only {RESOLVABLE_IN_PLACE!r} can",
            )

        if self.directory is None:
            # Same reasoning as the question form: the answer may be perfectly
            # valid, we just cannot check it against anything. Not a 422.
            raise ServerNotConfigured(
                "cannot resolve a payee: no directory is configured on this "
                "server. Pass directory= when building the gateway — the "
                "candidate fields on the draft are not authoritative."
            )

        try:
            resolved, record = resolve_payee(stored, request["answer"], self.directory)
        except InvalidClarificationAnswer as exc:
            return rejection_response(R.INVALID_CLARIFICATION_ANSWER, str(exc))

        # The parser is not involved in a clarification, so the server raises
        # the confidence itself (CONTRACT.md §4: 0.4 -> 0.9). Not hashed.
        resolved = resolved.model_copy(update={"confidence": CLARIFIED_CONFIDENCE})

        self._record(
            "clarify",
            {
                "from_draft_id": stored.id,
                "to_draft_id": resolved.id,
                "field": field,
                "answer": request["answer"],
                # Same reason as the question form: an answer id on its own
                # names nobody. The audit row has to say which transcript it
                # was an answer to, or the log holds "payee-102" with nothing
                # to interpret it against.
                "transcript": stored.transcript,
            },
        )
        return {"intents_detected": 1, "items": [self._attach(resolved, clarification=record)]}
