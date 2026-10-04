"""
Design-level tests for /api/execute (CONTRACT.md §4, "POST /api/execute").

`StubGateway` below is NOT the gateway. It is a small reference model of the
contract's check order, run against an in-memory stub store, so the binding
rules can be tested before backend/gateway exists. Member 3 should point these
tests at the real gateway once it exists and keep them passing.

What is real here: draft hashing (backend.canonical), client_data_json
parsing, the challenge comparison, and the rpIdHash / user-verified flag
checks on authenticator_data. What is stubbed: the ECDSA signature check
itself, via an injected `verify_ecdsa` callable.
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


def rejected(code: str) -> dict:
    return {"result": "rejected", "reason_code": code}


class StubGateway:
    """Reference model of the /api/execute check order in CONTRACT.md."""

    def __init__(self, store: dict, credentials: dict, verify_ecdsa, now: datetime):
        self.store = store              # draft_id -> TransactionDraft (the server's own copy)
        self.credentials = credentials  # credential_id -> public key (opaque here)
        self.verify_ecdsa = verify_ecdsa
        self.now = now
        self.ledger: list[dict] = []
        self.idempotency: dict[str, tuple[str, dict]] = {}

    def execute(self, request: dict) -> dict:
        # 1. Request shape. A `draft` body (or any unknown field) is refused,
        #    never used: the gateway only ever executes its own stored copy.
        if not isinstance(request, dict) or set(request) - REQUEST_FIELDS:
            return rejected("malformed_request")
        if not {"draft_id", "credential_id", "idempotency_key"} <= set(request):
            return rejected("malformed_request")
        if "assertion" not in request:
            return rejected("missing_signature")

        # 2. Load the server's own copy.
        draft = self.store.get(request["draft_id"])
        if draft is None:
            return rejected("unknown_draft")

        # 3. Draft state, independent of any frontend check.
        if draft.unresolved:
            return rejected("unresolved_fields")
        if self.now >= draft.expires_at:
            return rejected("expired")

        # 4. Assertion format.
        assertion = request["assertion"]
        if not isinstance(assertion, dict) or set(assertion) != ASSERTION_FIELDS:
            return rejected("malformed_signature")
        try:
            authenticator_data = b64url_decode(assertion["authenticator_data"])
            client_data_raw = b64url_decode(assertion["client_data_json"])
            signature = b64url_decode(assertion["signature"])
            b64url_decode(assertion["credential_id"])
            client_data = json.loads(client_data_raw)
        except ValueError:
            return rejected("malformed_signature")
        if assertion["credential_id"] != request["credential_id"] or len(authenticator_data) < 37:
            return rejected("malformed_signature")

        # 5. Challenge binding: the challenge must be OUR hash of OUR stored draft.
        if client_data.get("challenge") != challenge_for(draft):
            return rejected("hash_mismatch")

        # 6. Everything else about the assertion.
        if client_data.get("type") != "webauthn.get" or client_data.get("origin") != ORIGIN:
            return rejected("signature_invalid")
        if authenticator_data[:32] != hashlib.sha256(RP_ID.encode()).digest():
            return rejected("signature_invalid")
        if not authenticator_data[32] & FLAG_USER_VERIFIED:
            return rejected("signature_invalid")
        public_key = self.credentials.get(request["credential_id"])
        signed = authenticator_data + hashlib.sha256(client_data_raw).digest()
        if public_key is None or not self.verify_ecdsa(public_key, signed, signature):
            return rejected("signature_invalid")

        # 7. Idempotency on the caller-supplied key.
        key = request["idempotency_key"]
        if key in self.idempotency:
            prior_draft_id, prior_result = self.idempotency[key]
            return prior_result if prior_draft_id == draft.id else rejected("replayed")

        # 8. Execute the stored draft. (Validator and policy are out of scope here.)
        self.ledger.append(draft.model_dump(mode="json", exclude_none=True))
        result = {"result": "executed", "transaction_id": f"tx-{len(self.ledger):06d}", "idempotency_key": key}
        self.idempotency[key] = (draft.id, result)
        return result


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


def make_gateway() -> tuple[StubGateway, TransactionDraft]:
    stored = load_draft("clean_transfer.json")
    ambiguous = load_draft("ambiguous_payee.json")
    gw = StubGateway(
        store={stored.id: stored, ambiguous.id: ambiguous},
        credentials={CREDENTIAL_ID: "stub-public-key"},
        verify_ecdsa=fake_verify_ecdsa,
        now=stored.created_at + timedelta(minutes=1),
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
    assert gw.ledger == [stored.model_dump(mode="json", exclude_none=True)]


def test_request_carrying_a_different_draft_body_is_rejected_not_swapped_in():
    gw, stored = make_gateway()
    request = request_for(stored.id, make_assertion(challenge_for(stored)))
    request["draft"] = tampered_copy(stored)
    assert gw.execute(request) == rejected("malformed_request")
    assert gw.ledger == []
    assert gw.store[stored.id].payee.id == "payee-001"  # stored copy untouched


def test_user_signing_a_client_built_draft_is_rejected_as_hash_mismatch():
    """A tampered page shows Mallory, the user approves, the authenticator signs
    the hash of the client-built draft. The gateway hashes its own copy instead."""
    gw, stored = make_gateway()
    forged = TransactionDraft.model_validate(tampered_copy(stored))
    assert challenge_for(forged) != challenge_for(stored)
    assert gw.execute(request_for(stored.id, make_assertion(challenge_for(forged)))) == rejected("hash_mismatch")
    assert gw.ledger == []


def test_unknown_draft_id_is_rejected():
    gw, stored = make_gateway()
    request = request_for("99999999-9999-9999-9999-999999999999", make_assertion(challenge_for(stored)))
    assert gw.execute(request) == rejected("unknown_draft")


def test_unresolved_stored_draft_is_rejected_even_with_a_valid_assertion():
    gw, _ = make_gateway()
    ambiguous = gw.store["22222222-2222-2222-2222-222222222222"]
    assert gw.execute(request_for(ambiguous.id, make_assertion(challenge_for(ambiguous)))) == rejected("unresolved_fields")
    assert gw.ledger == []


def test_expired_draft_is_rejected():
    gw, stored = make_gateway()
    gw.now = stored.expires_at
    assert gw.execute(request_for(stored.id, make_assertion(challenge_for(stored)))) == rejected("expired")


def test_missing_and_malformed_assertions():
    gw, stored = make_gateway()
    request = request_for(stored.id, make_assertion(challenge_for(stored)))
    del request["assertion"]
    assert gw.execute(request) == rejected("missing_signature")

    bad = make_assertion(challenge_for(stored))
    bad["signature"] = "not base64url!"
    assert gw.execute(request_for(stored.id, bad)) == rejected("malformed_signature")


def test_signature_invalid_covers_bad_signature_origin_type_and_missing_uv():
    gw, stored = make_gateway()
    challenge = challenge_for(stored)
    for assertion in [
        make_assertion(challenge, signature=b"forged"),
        make_assertion(challenge, origin="https://evil.example"),
        make_assertion(challenge, type_="webauthn.create"),
        make_assertion(challenge, flags=FLAG_USER_PRESENT),  # user present but not verified
    ]:
        assert gw.execute(request_for(stored.id, assertion)) == rejected("signature_invalid")
    assert gw.ledger == []


def test_idempotency_key_replays_the_same_result_and_rejects_a_different_draft():
    gw, stored = make_gateway()
    other = TransactionDraft.model_validate(
        {**stored.model_dump(mode="json", exclude_none=True), "id": "66666666-6666-6666-6666-666666666666", "nonce": "nonce-other"}
    )
    gw.store[other.id] = other

    first = gw.execute(request_for(stored.id, make_assertion(challenge_for(stored)), key="k1"))
    again = gw.execute(request_for(stored.id, make_assertion(challenge_for(stored)), key="k1"))
    assert first == again and len(gw.ledger) == 1
    assert gw.execute(request_for(other.id, make_assertion(challenge_for(other)), key="k1")) == rejected("replayed")
    assert len(gw.ledger) == 1
