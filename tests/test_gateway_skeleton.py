"""
Tests for the /api/execute gateway skeleton (backend/gateway/execute.py).

The point of this file is that the check ORDER is asserted, not merely
documented. CONTRACT.md §4 fixes the order and requires that every rejection
carry exactly one reason code — the code of the first check that failed. If
someone reorders CHECKS, these tests fail.

ES256 is not implemented, so `verify_ecdsa` is injected as a stub here. Every
other check runs for real.
"""

import copy
import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.canonical import draft_hash  # noqa: E402
from backend.gateway import reason_codes as R  # noqa: E402
from backend.gateway.errors import Rejection  # noqa: E402
from backend.gateway.execute import (  # noqa: E402
    CHECKS,
    FLAG_USER_PRESENT,
    FLAG_USER_VERIFIED,
    InMemoryLedger,
    PolicyDecision,
    ExecuteGateway,
    b64url_encode,
)
from backend.models.draft import TransactionDraft  # noqa: E402

FIXTURES_DIR = ROOT / "fixtures" / "drafts"
RP_ID = "localhost"
ORIGIN = "http://localhost:8000"
CREDENTIAL_ID = b64url_encode(b"credential-001")
GOOD_SIGNATURE = b"stub-valid-ecdsa-signature"


# ── Helpers ───────────────────────────────────────────────────────────


def load_draft(name: str) -> TransactionDraft:
    return TransactionDraft.model_validate_json((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def dump(draft: TransactionDraft) -> dict:
    """The hashed object."""
    return draft.model_dump(mode="json", exclude_none=True)


def challenge_for(draft: TransactionDraft) -> str:
    return b64url_encode(bytes.fromhex(draft_hash(dump(draft))))


def make_assertion(
    challenge: str,
    *,
    origin: str = ORIGIN,
    type_: str = "webauthn.get",
    flags: int = FLAG_USER_PRESENT | FLAG_USER_VERIFIED,
    signature: bytes = GOOD_SIGNATURE,
) -> dict:
    client_data = json.dumps({"type": type_, "challenge": challenge, "origin": origin}).encode()
    authenticator_data = (
        hashlib.sha256(RP_ID.encode()).digest() + bytes([flags]) + (1).to_bytes(4, "big")
    )
    return {
        "credential_id": CREDENTIAL_ID,
        "authenticator_data": b64url_encode(authenticator_data),
        "client_data_json": b64url_encode(client_data),
        "signature": b64url_encode(signature),
    }


def request_for(draft_id: str, assertion: dict, key: str = "idem-001") -> dict:
    return {
        "draft_id": draft_id,
        "credential_id": CREDENTIAL_ID,
        "idempotency_key": key,
        "assertion": assertion,
    }


def build(
    *,
    drafts: dict | None = None,
    verdicts: dict | None = None,
    policy=None,
    now=None,
    credentials: dict | None = None,
    verify=None,
    idempotency: dict | None = None,
    ledger=None,
):
    """A gateway wired to the clean fixture, with sane defaults."""
    clean = load_draft("clean_transfer.json")
    store: dict = {clean.id: clean}
    store.update(drafts or {})
    if now is None:
        now = clean.created_at + timedelta(minutes=1)
    if verdicts is None:
        verdicts = {
            draft_hash(dump(d)): "pass" for d in store.values() if isinstance(d, TransactionDraft)
        }
    return ExecuteGateway(
        draft_store=store,
        credentials=credentials if credentials is not None else {CREDENTIAL_ID: "stub-public-key"},
        verify_ecdsa=verify
        if verify is not None
        else (lambda pk, signed, sig: sig == GOOD_SIGNATURE),
        idempotency=idempotency,
        verdicts=verdicts,
        policy=policy if policy is not None else (lambda d, h: PolicyDecision("allow")),
        ledger=ledger if ledger is not None else InMemoryLedger(),
        clock=lambda: now,
    )


def only_code(response: dict) -> str:
    assert response["result"] == "rejected", response
    return response["reason_code"]


# ── The order is the contract ─────────────────────────────────────────


def test_check_order_matches_the_contract_table():
    """CHECKS must line up with CONTRACT.md §4 step-for-step."""
    assert [c.step for c in CHECKS] == list(range(1, 13))
    assert [c.reason_code for c in CHECKS] == list(R.EXECUTE_REASON_CODES)
    assert [c.name for c in CHECKS] == [
        "request_shape",
        "assertion_present",
        "known_draft",
        "schema_valid",
        "unresolved_empty",
        "not_expired",
        "assertion_format",
        "challenge_binding",
        "signature_valid",
        "not_replayed",
        "validator_passed",
        "policy_allowed",
    ]


def test_every_check_reason_code_is_a_known_code():
    for c in CHECKS:
        assert c.reason_code in R.ALL_REASON_CODES


def test_rejection_rejects_unknown_reason_codes():
    with pytest.raises(ValueError, match="unknown reason_code"):
        Rejection(reason_code="not_a_real_code")


# ── One broken thing at a time yields that step's code ────────────────


def test_step_1_extra_field_is_malformed_request():
    gw = build()
    clean = load_draft("clean_transfer.json")
    request = request_for(clean.id, make_assertion(challenge_for(clean)))
    request["draft"] = dump(clean)
    assert only_code(gw.execute(request)) == R.MALFORMED_REQUEST
    assert gw.ledger.entries == []


def test_step_2_missing_assertion_is_missing_signature():
    gw = build()
    clean = load_draft("clean_transfer.json")
    request = request_for(clean.id, make_assertion(challenge_for(clean)))
    del request["assertion"]
    assert only_code(gw.execute(request)) == R.MISSING_SIGNATURE


def test_step_3_unknown_draft_id_is_unknown_draft():
    gw = build()
    clean = load_draft("clean_transfer.json")
    request = request_for(
        "99999999-9999-9999-9999-999999999999", make_assertion(challenge_for(clean))
    )
    assert only_code(gw.execute(request)) == R.UNKNOWN_DRAFT


def test_step_4_stored_draft_failing_the_schema_is_schema_invalid():
    broken_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    gw = build(drafts={broken_id: {"id": broken_id, "intent_type": "not-an-intent"}})
    clean = load_draft("clean_transfer.json")
    assert only_code(gw.execute(request_for(broken_id, make_assertion(challenge_for(clean))))) == (
        R.SCHEMA_INVALID
    )


def test_step_5_unresolved_stored_draft_is_rejected_even_with_a_valid_assertion():
    ambiguous = load_draft("ambiguous_payee.json")
    gw = build(drafts={ambiguous.id: ambiguous})
    assert only_code(
        gw.execute(request_for(ambiguous.id, make_assertion(challenge_for(ambiguous))))
    ) == R.UNRESOLVED_FIELDS
    assert gw.ledger.entries == []


def test_step_6_expired_draft_is_expired():
    clean = load_draft("clean_transfer.json")
    gw = build(now=clean.expires_at)
    assert only_code(
        gw.execute(request_for(clean.id, make_assertion(challenge_for(clean))))
    ) == R.EXPIRED


def test_step_7_malformed_assertion_is_malformed_signature():
    gw = build()
    clean = load_draft("clean_transfer.json")
    for bad in [
        {**make_assertion(challenge_for(clean)), "signature": "not base64url!"},
        {k: v for k, v in make_assertion(challenge_for(clean)).items() if k != "signature"},
        {
            **make_assertion(challenge_for(clean)),
            "credential_id": b64url_encode(b"some-other-credential"),
        },
    ]:
        assert only_code(gw.execute(request_for(clean.id, bad))) == R.MALFORMED_SIGNATURE


def test_step_8_signature_for_a_different_draft_is_hash_mismatch():
    """The core binding rule: a signature for draft A dies on stored draft B."""
    gw = build()
    clean = load_draft("clean_transfer.json")
    other = load_draft("ambiguous_payee.json")
    assert challenge_for(clean) != challenge_for(other)
    assert only_code(
        gw.execute(request_for(clean.id, make_assertion(challenge_for(other))))
    ) == R.HASH_MISMATCH
    assert gw.ledger.entries == []


def test_step_9_signature_invalid_covers_origin_type_uv_and_unregistered():
    gw = build()
    clean = load_draft("clean_transfer.json")
    challenge = challenge_for(clean)
    for bad in [
        make_assertion(challenge, signature=b"forged"),
        make_assertion(challenge, origin="https://evil.example"),
        make_assertion(challenge, type_="webauthn.create"),
        make_assertion(challenge, flags=FLAG_USER_PRESENT),  # present, not verified
    ]:
        assert only_code(gw.execute(request_for(clean.id, bad))) == R.SIGNATURE_INVALID

    unregistered = build(credentials={})
    assert only_code(
        unregistered.execute(request_for(clean.id, make_assertion(challenge)))
    ) == R.SIGNATURE_INVALID
    assert gw.ledger.entries == []


def test_step_11_non_pass_verdict_is_validator_frozen():
    clean = load_draft("clean_transfer.json")
    gw = build(verdicts={draft_hash(dump(clean)): "frozen"})
    assert only_code(
        gw.execute(request_for(clean.id, make_assertion(challenge_for(clean))))
    ) == R.VALIDATOR_FROZEN


def test_step_11_missing_verdict_fails_closed():
    clean = load_draft("clean_transfer.json")
    gw = build(verdicts={})
    assert only_code(
        gw.execute(request_for(clean.id, make_assertion(challenge_for(clean))))
    ) == R.VALIDATOR_FROZEN
    assert gw.ledger.entries == []


def test_step_12_block_is_policy_blocked_and_hold_is_step_up_required():
    clean = load_draft("clean_transfer.json")
    blocked = build(policy=lambda d, h: PolicyDecision("block", "over per-transaction limit"))
    assert only_code(
        blocked.execute(request_for(clean.id, make_assertion(challenge_for(clean))))
    ) == R.POLICY_BLOCKED

    held = build(policy=lambda d, h: PolicyDecision("hold", "new payee"))
    assert only_code(
        held.execute(request_for(clean.id, make_assertion(challenge_for(clean))))
    ) == R.STEP_UP_REQUIRED
    assert held.ledger.entries == []


# ── ES256 is genuinely not implemented ────────────────────────────────


def test_default_verify_ecdsa_raises_not_implemented():
    """Guard against anyone assuming the skeleton already verifies signatures."""
    clean = load_draft("clean_transfer.json")
    gw = ExecuteGateway(
        draft_store={clean.id: clean},
        credentials={CREDENTIAL_ID: "stub-public-key"},
        verdicts={draft_hash(dump(clean)): "pass"},
        clock=lambda: clean.created_at + timedelta(minutes=1),
    )
    with pytest.raises(NotImplementedError, match="ES256"):
        gw.execute(request_for(clean.id, make_assertion(challenge_for(clean))))


# ── Happy path and the trust boundary ─────────────────────────────────


def test_valid_request_executes_the_stored_copy():
    clean = load_draft("clean_transfer.json")
    gw = build()
    result = gw.execute(request_for(clean.id, make_assertion(challenge_for(clean))))
    assert result["result"] == "executed"
    assert result["idempotency_key"] == "idem-001"
    assert gw.ledger.entries[0]["draft"] == dump(clean)


def test_a_client_supplied_draft_body_is_refused_and_the_stored_copy_survives():
    """The draft-swap attack: show one thing, sign it, submit another."""
    clean = load_draft("clean_transfer.json")
    gw = build()
    request = request_for(clean.id, make_assertion(challenge_for(clean)))
    tampered = copy.deepcopy(dump(clean))
    tampered["payee"] = {
        "id": "payee-666",
        "display_name": "Mallory",
        "masked_account": "****6666",
    }
    request["draft"] = tampered

    assert only_code(gw.execute(request)) == R.MALFORMED_REQUEST
    assert gw.ledger.entries == []
    # The stored draft was never touched.
    assert gw.draft_store.get(clean.id).payee.id == "payee-001"


def test_a_user_signed_hash_of_a_client_built_draft_is_hash_mismatch():
    """A tampered page gets the user to sign Mallory's draft; we hash our own."""
    clean = load_draft("clean_transfer.json")
    gw = build()
    forged = TransactionDraft.model_validate(
        {
            **copy.deepcopy(dump(clean)),
            "payee": {"id": "payee-666", "display_name": "Mallory", "masked_account": "****6666"},
        }
    )
    assert challenge_for(forged) != challenge_for(clean)
    assert only_code(
        gw.execute(request_for(clean.id, make_assertion(challenge_for(forged))))
    ) == R.HASH_MISMATCH
    assert gw.ledger.entries == []


# ── Idempotency ───────────────────────────────────────────────────────


def test_same_key_same_draft_returns_the_original_result():
    clean = load_draft("clean_transfer.json")
    gw = build()
    request = request_for(clean.id, make_assertion(challenge_for(clean)), key="k1")
    first = gw.execute(request)
    again = gw.execute(request)
    assert first == again
    assert len(gw.ledger.entries) == 1


def test_same_key_different_draft_is_replayed():
    clean = load_draft("clean_transfer.json")
    other = TransactionDraft.model_validate(
        {
            **dump(clean),
            "id": "66666666-6666-6666-6666-666666666666",
            "nonce": "nonce-other",
        }
    )
    gw = build(drafts={other.id: other})
    assert gw.execute(
        request_for(clean.id, make_assertion(challenge_for(clean)), key="k1")
    )["result"] == "executed"
    assert only_code(
        gw.execute(request_for(other.id, make_assertion(challenge_for(other)), key="k1"))
    ) == R.REPLAYED
    assert len(gw.ledger.entries) == 1


# ── Ordering: earlier codes win over later ones ───────────────────────


def test_an_expired_draft_reports_expired_not_hash_mismatch():
    """Everything is wrong here; only the first failure is reported."""
    clean = load_draft("clean_transfer.json")
    other = load_draft("ambiguous_payee.json")
    gw = build(now=clean.expires_at)
    # Wrong challenge (step 8) AND expired (step 6): step 6 must win.
    assert only_code(
        gw.execute(request_for(clean.id, make_assertion(challenge_for(other))))
    ) == R.EXPIRED


def test_unresolved_beats_expired():
    ambiguous = load_draft("ambiguous_payee.json")
    gw = build(drafts={ambiguous.id: ambiguous}, now=ambiguous.expires_at)
    assert only_code(
        gw.execute(request_for(ambiguous.id, make_assertion(challenge_for(ambiguous))))
    ) == R.UNRESOLVED_FIELDS


def test_rejection_response_shape():
    clean = load_draft("clean_transfer.json")
    gw = build(now=clean.expires_at)
    response = gw.execute(request_for(clean.id, make_assertion(challenge_for(clean))))
    assert set(response) == {"result", "reason_code", "message"}
    assert response["result"] == "rejected"
    assert response["message"]
