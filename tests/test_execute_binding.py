"""
Design-level tests for /api/execute (CONTRACT.md §4, "POST /api/execute").

These run against the real gateway, ``backend.gateway.execute.ExecuteGateway``.
They used to drive a ``StubGateway`` reference model while backend/gateway was
still empty; CONTRACT.md §7 asks for the real one once it exists, and the stub
has been retired rather than left to drift beside it.

What is real here: draft hashing (backend.canonical), client_data_json
parsing, the challenge comparison, the rpIdHash / user-verified flag checks on
authenticator_data, and the twelve-check order itself. What is stubbed: the
ECDSA signature check, via an injected ``verify_ecdsa`` callable.

Scope: checks 1-10, the binding rules — that a client-supplied draft body can
never be swapped in, and that a signature over draft A is rejected for draft B.
Checks 11 (validator) and 12 (policy) are exercised in
test_gateway_skeleton.py; see ``Verdicts`` below.
"""

import base64
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.canonical import draft_hash  # noqa: E402
from backend.gateway.execute import ExecuteGateway  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402

RP_ID = "localhost"
ORIGIN = "http://localhost:8000"
REQUEST_FIELDS = {"draft_id", "credential_id", "idempotency_key", "assertion"}
ASSERTION_FIELDS = {"credential_id", "authenticator_data", "client_data_json", "signature"}
FLAG_USER_PRESENT = 0x01
FLAG_USER_VERIFIED = 0x04


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    if type(s) is not str or not re.fullmatch(r"[A-Za-z0-9_-]*", s):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def challenge_for(draft: TransactionDraft) -> str:
    """The WebAuthn challenge IS the draft hash: base64url of the raw 32 bytes."""
    hashed = draft_hash(draft.model_dump(mode="json", exclude_none=True))
    return b64url_encode(bytes.fromhex(hashed))


def assert_rejected(result: dict, code: str) -> None:
    """CONTRACT.md §5: rejections carry result and reason_code (plus a message).

    Compares the two fields the client branches on rather than the whole dict,
    so the developer-facing message can change without breaking these.
    """
    assert result["result"] == "rejected", result
    assert result["reason_code"] == code, result


def ledger_drafts(gw: ExecuteGateway) -> list[dict]:
    """The drafts the gateway actually posted, in order."""
    return [entry["draft"] for entry in gw.ledger.entries]


class Verdicts(dict):
    """Validator verdicts by draft hash, defaulting to "pass".

    These tests cover checks 1-10. The validator gate is asserted to fail
    closed in test_gateway_skeleton.py; defaulting to pass here keeps that
    concern out of every binding test instead of making each one register a
    verdict for every draft it happens to build.
    """

    def get(self, key, default="pass"):
        return dict.get(self, key, default)


# ── Helpers that play the browser + authenticator ─────────────────────

CREDENTIAL_ID = b64url_encode(b"credential-001")
GOOD_SIGNATURE = b"stub-valid-ecdsa-signature"


def fake_verify_ecdsa(public_key, signed: bytes, signature: bytes) -> bool:
    return signature == GOOD_SIGNATURE


def make_assertion(challenge: str, *, origin=ORIGIN, type_="webauthn.get", flags=FLAG_USER_PRESENT | FLAG_USER_VERIFIED,
                   signature=GOOD_SIGNATURE) -> dict:
    client_data = json.dumps({"type": type_, "challenge": challenge, "origin": origin}).encode()
    authenticator_data = hashlib.sha256(RP_ID.encode()).digest() + bytes([flags]) + (1).to_bytes(4, "big")
    return {
        "credential_id": CREDENTIAL_ID,
        "authenticator_data": b64url_encode(authenticator_data),
        "client_data_json": b64url_encode(client_data),
        "signature": b64url_encode(signature),
    }


def load_draft(name: str) -> TransactionDraft:
    return TransactionDraft.model_validate_json((ROOT / "fixtures" / "drafts" / name).read_text(encoding="utf-8"))


def make_gateway() -> tuple[ExecuteGateway, TransactionDraft]:
    stored = load_draft("clean_transfer.json")
    ambiguous = load_draft("ambiguous_payee.json")
    gw = ExecuteGateway(
        draft_store={stored.id: stored, ambiguous.id: ambiguous},
        credentials={CREDENTIAL_ID: "stub-public-key"},
        verify_ecdsa=fake_verify_ecdsa,
        verdicts=Verdicts(),
        clock=lambda: stored.created_at + timedelta(minutes=1),
    )
    return gw, stored


def request_for(draft_id: str, assertion: dict, key: str = "idem-001") -> dict:
    return {"draft_id": draft_id, "credential_id": CREDENTIAL_ID, "idempotency_key": key, "assertion": assertion}


def tampered_copy(stored: TransactionDraft) -> dict:
    body = stored.model_dump(mode="json", exclude_none=True)
    body = copy.deepcopy(body)
    body["payee"] = {"id": "payee-666", "display_name": "Mallory", "masked_account": "****6666"}
    return body


# ── The binding rules ─────────────────────────────────────────────────


def test_stored_draft_with_matching_challenge_executes_the_stored_copy():
    gw, stored = make_gateway()
    result = gw.execute(request_for(stored.id, make_assertion(challenge_for(stored))))
    assert result["result"] == "executed"
    assert ledger_drafts(gw) == [stored.model_dump(mode="json", exclude_none=True)]


def test_request_carrying_a_different_draft_body_is_rejected_not_swapped_in():
    gw, stored = make_gateway()
    request = request_for(stored.id, make_assertion(challenge_for(stored)))
    request["draft"] = tampered_copy(stored)
    assert_rejected(gw.execute(request), "malformed_request")
    assert ledger_drafts(gw) == []
    assert gw.draft_store[stored.id].payee.id == "payee-001"  # stored copy untouched


def test_user_signing_a_client_built_draft_is_rejected_as_hash_mismatch():
    """A tampered page shows Mallory, the user approves, the authenticator signs
    the hash of the client-built draft. The gateway hashes its own copy instead."""
    gw, stored = make_gateway()
    forged = TransactionDraft.model_validate(tampered_copy(stored))
    assert challenge_for(forged) != challenge_for(stored)
    assert_rejected(
        gw.execute(request_for(stored.id, make_assertion(challenge_for(forged)))),
        "hash_mismatch",
    )
    assert ledger_drafts(gw) == []


def test_unknown_draft_id_is_rejected():
    gw, stored = make_gateway()
    request = request_for("99999999-9999-9999-9999-999999999999", make_assertion(challenge_for(stored)))
    assert_rejected(gw.execute(request), "unknown_draft")


def test_unresolved_stored_draft_is_rejected_even_with_a_valid_assertion():
    gw, _ = make_gateway()
    ambiguous = gw.draft_store["22222222-2222-2222-2222-222222222222"]
    assert_rejected(
        gw.execute(request_for(ambiguous.id, make_assertion(challenge_for(ambiguous)))),
        "unresolved_fields",
    )
    assert ledger_drafts(gw) == []


def test_expired_draft_is_rejected():
    gw, stored = make_gateway()
    gw.clock = lambda: stored.expires_at
    assert_rejected(
        gw.execute(request_for(stored.id, make_assertion(challenge_for(stored)))),
        "expired",
    )


def test_missing_and_malformed_assertions():
    gw, stored = make_gateway()
    request = request_for(stored.id, make_assertion(challenge_for(stored)))
    del request["assertion"]
    assert_rejected(gw.execute(request), "missing_signature")

    bad = make_assertion(challenge_for(stored))
    bad["signature"] = "not base64url!"
    assert_rejected(gw.execute(request_for(stored.id, bad)), "malformed_signature")


def test_signature_invalid_covers_bad_signature_origin_type_and_missing_uv():
    gw, stored = make_gateway()
    challenge = challenge_for(stored)
    for assertion in [
        make_assertion(challenge, signature=b"forged"),
        make_assertion(challenge, origin="https://evil.example"),
        make_assertion(challenge, type_="webauthn.create"),
        make_assertion(challenge, flags=FLAG_USER_PRESENT),  # user present but not verified
    ]:
        assert_rejected(gw.execute(request_for(stored.id, assertion)), "signature_invalid")
    assert ledger_drafts(gw) == []


def test_idempotency_key_replays_the_same_result_and_rejects_a_different_draft():
    gw, stored = make_gateway()
    other = TransactionDraft.model_validate(
        {**stored.model_dump(mode="json", exclude_none=True), "id": "66666666-6666-6666-6666-666666666666", "nonce": "nonce-other"}
    )
    gw.draft_store[other.id] = other

    first = gw.execute(request_for(stored.id, make_assertion(challenge_for(stored)), key="k1"))
    again = gw.execute(request_for(stored.id, make_assertion(challenge_for(stored)), key="k1"))
    assert first == again and len(ledger_drafts(gw)) == 1
    assert_rejected(
        gw.execute(request_for(other.id, make_assertion(challenge_for(other)), key="k1")),
        "replayed",
    )
    assert len(ledger_drafts(gw)) == 1
