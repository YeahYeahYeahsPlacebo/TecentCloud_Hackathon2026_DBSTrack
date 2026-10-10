"""
Skeleton of the /api/execute gateway (CONTRACT.md §4).

The AI drafts; the gateway executes. Money moves only here, and only after
every check in ``CHECKS`` has passed, in order. The first check that fails
decides the reason code — a request never gets two reasons.

**The trust boundary.** The gateway loads the draft *it* stored, hashes *that*
copy, and executes *that* copy. It never reads a draft body out of the request:
check 1 refuses any body carrying one. See CONTRACT.md §4 "POST /api/execute"
and §2 "Frontend never builds a draft".

ES256 verification is real (:func:`backend.gateway.webauthn.verify_es256`) and
is the default. It stays injectable so tests can use a fixed key pair without
doing cryptography. Everything else — request shape, draft state, base64url
framing, the challenge binding, idempotency, verdict and policy lookups — is
pure deterministic logic.

``backend/ledger`` is wired: append-only, ``InMemoryLedger`` by default, swap
in ``FileLedger`` when the result must survive a restart. ``backend/audit`` is
an optional callable invoked once per outcome.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, NamedTuple, Optional, Protocol

import jsonschema
from jsonschema import Draft202012Validator

from backend.bank import BankError
from backend.canonical import canonical_bytes, draft_hash
from backend.gateway import reason_codes as R
from backend.gateway.errors import Rejection, rejected, rejection_response
from backend.gateway.webauthn import verify_es256
from backend.ledger import InMemoryLedger, Ledger
from backend.models.draft import TransactionDraft

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contract" / "draft.schema.json"

# The only fields /api/execute accepts. A `draft` body is not one of them.
REQUEST_FIELDS = frozenset({"draft_id", "credential_id", "idempotency_key", "assertion"})
REQUIRED_REQUEST_FIELDS = frozenset({"draft_id", "credential_id", "idempotency_key"})
ASSERTION_FIELDS = frozenset(
    {"credential_id", "authenticator_data", "client_data_json", "signature"}
)

_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")

FLAG_USER_PRESENT = 0x01
FLAG_USER_VERIFIED = 0x04
# rpIdHash (32 bytes) + flags (1) + signCount (4).
MIN_AUTHENTICATOR_DATA_LEN = 37

WEB_AUTHN_GET = "webauthn.get"
VALIDATOR_PASS = "pass"
POLICY_ALLOW = "allow"
POLICY_BLOCK = "block"


# ── Pluggable pieces ──────────────────────────────────────────────────


class DraftStore(Protocol):
    """The server's own drafts, by id. The gateway never accepts one from a client."""

    def get(self, draft_id: str) -> Optional[Any]: ...


@dataclass(frozen=True)
class PolicyDecision:
    """What the policy engine decided. Pure deterministic input, no LLM."""

    decision: str
    reason: Optional[str] = None


# ── Small helpers ─────────────────────────────────────────────────────


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    """Decode base64url without padding, rejecting anything else."""
    if type(value) is not str or not _B64URL_RE.fullmatch(value):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@lru_cache(maxsize=1)
def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _transfer_spec(draft: Mapping[str, Any]) -> Optional[dict]:
    """The arguments for ``Bank.transfer``, or None if the draft names no
    complete transfer (unresolved payee, equity intent, missing amount).

    Returns None rather than raising: a draft the bank cannot service is not a
    gateway error, it is one the earlier checks already dealt with.
    """
    payee = draft.get("payee")
    if not isinstance(payee, Mapping):
        return None
    destination = payee.get("id")
    source = draft.get("source_account")
    amount = draft.get("amount")
    if not isinstance(amount, Mapping):
        return None
    value = amount.get("value")
    currency = amount.get("currency")
    if not source or not destination or value is None or not currency:
        return None
    return {
        "source": source,
        "destination": destination,
        "amount": str(value),
        "currency": currency,
    }


# ── Context threaded through the checks ───────────────────────────────


@dataclass
class ExecuteContext:
    """What the checks know. Each check may read what earlier checks produced."""

    request: dict
    now: datetime

    # Filled in as the checks run, in order.
    stored: Any = None                      # 3  raw value from the draft store
    draft: Optional[TransactionDraft] = None  # 4  validated model
    draft_dict: Optional[dict] = None       # 4  the hashed object
    draft_hash_hex: Optional[str] = None    # 8  our hash of our stored copy
    challenge: Optional[str] = None         # 8  what the challenge must equal
    assertion: Optional[dict] = None        # 7
    authenticator_data: bytes = b""         # 7
    client_data_raw: bytes = b""            # 7
    client_data: Optional[dict] = None      # 7
    signature: bytes = b""                  # 7

    # Set by check 10 to short-circuit: same key + same draft returns the
    # original result instead of executing again.
    final_result: Optional[dict] = None


# ── The twelve checks ─────────────────────────────────────────────────


def _check_request_shape(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """1. Only the four allowed fields; the first three must be present."""
    if not isinstance(ctx.request, dict):
        return rejected(R.MALFORMED_REQUEST, "request body must be a JSON object")
    unknown = sorted(set(ctx.request) - REQUEST_FIELDS)
    if unknown:
        # This is where a client-supplied `draft` body dies. The gateway does
        # not read it, merge it or swap it in — it refuses the request.
        return rejected(
            R.MALFORMED_REQUEST,
            f"unexpected field(s): {unknown}; only {sorted(REQUEST_FIELDS)} are accepted",
        )
    missing = sorted(REQUIRED_REQUEST_FIELDS - set(ctx.request))
    if missing:
        return rejected(R.MALFORMED_REQUEST, f"missing field(s): {missing}")
    return None


def _check_assertion_present(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """2. An assertion must be supplied."""
    if "assertion" not in ctx.request:
        return rejected(R.MISSING_SIGNATURE, "no assertion supplied")
    return None


def _check_known_draft(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """3. draft_id must name a draft the server stored."""
    ctx.stored = gw.draft_store.get(ctx.request["draft_id"])
    if ctx.stored is None:
        return rejected(
            R.UNKNOWN_DRAFT, f"no stored draft with id {ctx.request['draft_id']!r}"
        )
    return None


def _check_schema_valid(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """4. The stored draft validates against the schema and canonicalises.

    Canonicalisation is exercised here too: a lone UTF-16 surrogate or a float
    would otherwise blow up at check 8. CONTRACT.md §9 requires it be reported
    as ``schema_invalid``, never as a 500.
    """
    stored = ctx.stored
    if isinstance(stored, TransactionDraft):
        dumped = stored.model_dump(mode="json", exclude_none=True)
    elif isinstance(stored, dict):
        try:
            dumped = TransactionDraft.model_validate(stored).model_dump(
                mode="json", exclude_none=True
            )
        except Exception as exc:  # pydantic raises ValidationError
            return rejected(R.SCHEMA_INVALID, f"stored draft fails the model: {exc}")
    else:
        return rejected(
            R.SCHEMA_INVALID,
            f"stored draft is {type(stored).__name__}, not a draft object",
        )

    try:
        Draft202012Validator(_load_schema()).validate(dumped)
    except jsonschema.ValidationError as exc:
        return rejected(R.SCHEMA_INVALID, f"stored draft fails the schema: {exc.message}")

    try:
        canonical_bytes(dumped)
    except (ValueError, TypeError) as exc:
        return rejected(R.SCHEMA_INVALID, f"stored draft cannot be canonicalised: {exc}")

    ctx.draft = (
        stored if isinstance(stored, TransactionDraft) else TransactionDraft.model_validate(dumped)
    )
    ctx.draft_dict = dumped
    return None


def _check_unresolved_empty(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """5. unresolved must be empty, regardless of any frontend check."""
    assert ctx.draft is not None
    if ctx.draft.unresolved:
        return rejected(
            R.UNRESOLVED_FIELDS, f"stored draft has unresolved fields: {ctx.draft.unresolved}"
        )
    return None


def _check_not_expired(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """6. Now must be before expires_at."""
    assert ctx.draft is not None
    if ctx.now >= ctx.draft.expires_at:
        return rejected(
            R.EXPIRED,
            f"draft expired at {ctx.draft.expires_at.isoformat()}, now is {ctx.now.isoformat()}",
        )
    return None


def _check_assertion_format(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """7. Exactly four base64url fields; client_data_json is JSON; ids agree."""
    assertion = ctx.request["assertion"]
    if not isinstance(assertion, dict):
        return rejected(R.MALFORMED_SIGNATURE, "assertion must be an object")
    if set(assertion) != ASSERTION_FIELDS:
        return rejected(
            R.MALFORMED_SIGNATURE,
            f"assertion must have exactly {sorted(ASSERTION_FIELDS)}",
        )

    try:
        authenticator_data = b64url_decode(assertion["authenticator_data"])
        client_data_raw = b64url_decode(assertion["client_data_json"])
        signature = b64url_decode(assertion["signature"])
        b64url_decode(assertion["credential_id"])
    except ValueError as exc:
        return rejected(R.MALFORMED_SIGNATURE, f"assertion value is not base64url: {exc}")

    try:
        client_data = json.loads(client_data_raw)
    except ValueError as exc:
        return rejected(R.MALFORMED_SIGNATURE, f"client_data_json is not JSON: {exc}")
    if not isinstance(client_data, dict):
        return rejected(R.MALFORMED_SIGNATURE, "client_data_json must be an object")

    if assertion["credential_id"] != ctx.request["credential_id"]:
        return rejected(
            R.MALFORMED_SIGNATURE, "assertion.credential_id differs from credential_id"
        )
    if len(authenticator_data) < MIN_AUTHENTICATOR_DATA_LEN:
        return rejected(
            R.MALFORMED_SIGNATURE,
            f"authenticator_data is {len(authenticator_data)} bytes, need at least "
            f"{MIN_AUTHENTICATOR_DATA_LEN}",
        )

    ctx.assertion = assertion
    ctx.authenticator_data = authenticator_data
    ctx.client_data_raw = client_data_raw
    ctx.client_data = client_data
    ctx.signature = signature
    return None


def _check_challenge_binding(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """8. The challenge must equal OUR hash of OUR stored draft.

    This is the lock that makes the signature mean anything. The authenticator
    signed *some* challenge; only by recomputing the hash of the draft we are
    about to execute do we prove it signed *this* one.
    """
    assert ctx.draft_dict is not None
    ctx.draft_hash_hex = draft_hash(ctx.draft_dict)
    ctx.challenge = b64url_encode(bytes.fromhex(ctx.draft_hash_hex))
    if ctx.client_data.get("challenge") != ctx.challenge:
        return rejected(
            R.HASH_MISMATCH,
            "the challenge in client_data_json does not equal the hash of the stored draft",
        )
    return None


def _check_signature_valid(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """9. WebAuthn framing plus the ES256 signature over authData ‖ SHA256(cdj)."""
    assert ctx.client_data is not None
    if ctx.client_data.get("type") != WEB_AUTHN_GET:
        return rejected(R.SIGNATURE_INVALID, f"type must be {WEB_AUTHN_GET!r}")
    if ctx.client_data.get("origin") != gw.origin:
        return rejected(R.SIGNATURE_INVALID, f"origin must be {gw.origin!r}")
    if ctx.authenticator_data[:32] != hashlib.sha256(gw.rp_id.encode()).digest():
        return rejected(R.SIGNATURE_INVALID, "rpIdHash does not match")
    if not ctx.authenticator_data[32] & FLAG_USER_VERIFIED:
        return rejected(R.SIGNATURE_INVALID, "user-verified flag is not set")

    public_key = gw.credentials.get(ctx.request["credential_id"])
    if public_key is None:
        return rejected(R.SIGNATURE_INVALID, "credential is not registered")

    signed = ctx.authenticator_data + hashlib.sha256(ctx.client_data_raw).digest()
    if not gw.verify_ecdsa(public_key, signed, ctx.signature):
        return rejected(R.SIGNATURE_INVALID, "ES256 signature does not verify")
    return None


def _check_not_replayed(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """10. idempotency_key must not have been used for a different draft.

    Same key + same draft short-circuits to the original result (idempotent
    retry). Same key + different draft is a replay.
    """
    assert ctx.draft is not None
    key = ctx.request["idempotency_key"]
    prior = gw.idempotency.get(key)
    if prior is None:
        return None
    prior_draft_id, prior_result = prior
    if prior_draft_id != ctx.draft.id:
        return rejected(
            R.REPLAYED,
            f"idempotency_key {key!r} was already used for draft {prior_draft_id!r}",
        )
    ctx.final_result = prior_result
    return None


def _check_validator_passed(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """11. The validator verdict for this draft hash must be "pass".

    Fail-closed: a draft hash with no recorded verdict is not executed.
    """
    verdict = gw.verdicts.get(ctx.draft_hash_hex)
    if verdict != VALIDATOR_PASS:
        return rejected(
            R.VALIDATOR_FROZEN,
            f"validator verdict for this draft hash is {verdict!r}, not {VALIDATOR_PASS!r}",
        )
    return None


def _check_policy_allowed(gw: "ExecuteGateway", ctx: ExecuteContext) -> Optional[Rejection]:
    """12. The policy decision for this draft hash must be "allow".

    A "block" decision is terminal. Anything else needs step-up authentication,
    which is not yet defined (CONTRACT.md §8 open question 8) and therefore
    currently fails closed as ``step_up_required``.
    """
    assert ctx.draft_dict is not None
    outcome = gw.policy(ctx.draft_dict, ctx.draft_hash_hex)
    if outcome.decision == POLICY_ALLOW:
        return None
    if outcome.decision == POLICY_BLOCK:
        return rejected(R.POLICY_BLOCKED, outcome.reason or "policy blocked this draft")
    return rejected(
        R.STEP_UP_REQUIRED,
        outcome.reason or f"policy decision {outcome.decision!r} requires step-up",
    )


class Check(NamedTuple):
    """One numbered gate. ``reason_code`` is what it returns when it fails."""

    step: int
    name: str
    reason_code: str
    fn: Callable[["ExecuteGateway", ExecuteContext], Optional[Rejection]]


# The order is the contract (CONTRACT.md §4). tests/test_gateway_skeleton.py
# asserts this lines up with reason_codes.EXECUTE_REASON_CODES.
CHECKS: tuple[Check, ...] = (
    Check(1, "request_shape", R.MALFORMED_REQUEST, _check_request_shape),
    Check(2, "assertion_present", R.MISSING_SIGNATURE, _check_assertion_present),
    Check(3, "known_draft", R.UNKNOWN_DRAFT, _check_known_draft),
    Check(4, "schema_valid", R.SCHEMA_INVALID, _check_schema_valid),
    Check(5, "unresolved_empty", R.UNRESOLVED_FIELDS, _check_unresolved_empty),
    Check(6, "not_expired", R.EXPIRED, _check_not_expired),
    Check(7, "assertion_format", R.MALFORMED_SIGNATURE, _check_assertion_format),
    Check(8, "challenge_binding", R.HASH_MISMATCH, _check_challenge_binding),
    Check(9, "signature_valid", R.SIGNATURE_INVALID, _check_signature_valid),
    Check(10, "not_replayed", R.REPLAYED, _check_not_replayed),
    Check(11, "validator_passed", R.VALIDATOR_FROZEN, _check_validator_passed),
    Check(12, "policy_allowed", R.POLICY_BLOCKED, _check_policy_allowed),
)


# ── The gateway ───────────────────────────────────────────────────────


class ExecuteGateway:
    """Runs the twelve checks in order, then posts to the ledger."""

    def __init__(
        self,
        *,
        draft_store: Any,
        credentials: Optional[Mapping[str, Any]] = None,
        verify_ecdsa: Callable[[Any, bytes, bytes], bool] = verify_es256,
        idempotency: Optional[MutableMapping[str, tuple[str, dict]]] = None,
        verdicts: Optional[Mapping[str, str]] = None,
        policy: Optional[Callable[[dict, str], PolicyDecision]] = None,
        ledger: Optional[Ledger] = None,
        audit: Optional[Callable[[str, dict], None]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        rp_id: str = "localhost",
        origin: str = "http://localhost:8000",
        bank: Optional[Any] = None,
    ) -> None:
        # A Mapping is used as-is, by reference: a dict store stays live, so a
        # draft added after construction is still visible to the gateway.
        self.draft_store = draft_store
        # By reference, so a credential registered after construction is
        # usable. (Copying here silently broke registration in the API layer.)
        self.credentials: MutableMapping[str, Any] = (
            credentials if credentials is not None else {}
        )
        self.verify_ecdsa = verify_ecdsa
        self.idempotency: MutableMapping[str, tuple[str, dict]] = (
            idempotency if idempotency is not None else {}
        )
        # Held by reference like the draft store, so a caller can inject a
        # mapping with its own lookup policy instead of a plain dict.
        self.verdicts: Mapping[str, str] = verdicts if verdicts is not None else {}
        self.policy = policy or (lambda draft, digest: PolicyDecision(POLICY_ALLOW))
        self.ledger: Ledger = ledger if ledger is not None else InMemoryLedger()
        self.audit = audit
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.rp_id = rp_id
        self.origin = origin
        # Optional mock bank. None means "no money moves" — every check still
        # runs, the ledger still records, but no balance changes.
        self.bank = bank

    def execute(self, request: dict) -> dict:
        """Run every check in order. Return the first rejection, or the result."""
        ctx = ExecuteContext(request=request, now=self.clock())

        for check in CHECKS:
            rejection = check.fn(self, ctx)
            if rejection is not None:
                return rejection.to_response()
            if ctx.final_result is not None:
                # Check 10 recognised an idempotent retry.
                return ctx.final_result

        return self._commit(ctx)

    def _commit(self, ctx: ExecuteContext) -> dict:
        """All twelve checks passed. Move the money, then record the result.

        Order matters: the bank moves the money *before* the ledger writes the
        row. A transaction that is recorded but never moved is a lie the audit
        log cannot detect; a transfer that moved but was never recorded is at
        least recoverable from the accounts.
        """
        assert ctx.draft is not None and ctx.draft_dict is not None
        key = ctx.request["idempotency_key"]

        # Policy already blocked an unaffordable draft before signing. This
        # second check covers the gap between then and now: the balance can
        # move, so the transfer itself can still fail. When it does, nothing
        # is recorded — no ledger row, no audit entry, no money moved.
        if self.bank is not None:
            spec = _transfer_spec(ctx.draft_dict)
            if spec is not None:
                try:
                    self.bank.transfer(**spec)
                except BankError as exc:
                    return rejection_response(R.POLICY_BLOCKED, str(exc))

        transaction_id = self.ledger.post(ctx.draft_dict, idempotency_key=key)
        result = {
            "result": "executed",
            "transaction_id": transaction_id,
            "idempotency_key": key,
            "executed_at": ctx.now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.idempotency[key] = (ctx.draft.id, result)
        if self.audit is not None:
            self.audit(ctx.draft_hash_hex or "", dict(result))
        return result
