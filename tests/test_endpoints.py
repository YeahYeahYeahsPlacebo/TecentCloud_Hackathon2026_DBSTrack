"""
The rest of the gateway surface: `/api/message`, `/api/clarify`,
`/api/webauthn/*`, `/api/audit/verify` — plus one end-to-end run from user
text to a posted transaction with a real ES256 signature.

The parser is injected, never imported: `backend/parser` belongs to Member 1.
These tests use a fake parser so they do not break when the real one changes.
"""

import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.gateway.api import GatewayAPI  # noqa: E402
from backend.gateway.execute import (  # noqa: E402
    FLAG_USER_PRESENT,
    FLAG_USER_VERIFIED,
    b64url_encode,
)
from backend.gateway.message import CLARIFIED_CONFIDENCE, ParserContractViolation  # noqa: E402
from backend.gateway.webauthn import der_to_raw_signature, public_key_to_pem  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402
from backend.parser.interface import ClarifyingQuestion, ParseResult  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

FIXTURES = ROOT / "fixtures" / "drafts"
RP_ID = "localhost"
ORIGIN = "http://localhost:8000"


def load(name: str) -> TransactionDraft:
    return TransactionDraft.model_validate_json((FIXTURES / name).read_text(encoding="utf-8"))


class FakeParser:
    """Returns fixed items and counts them honestly."""

    def __init__(self, *items) -> None:
        self.items = list(items)

    def __call__(self, transcript: str) -> ParseResult:
        return ParseResult(items=self.items, intents_detected=len(self.items))


def clean_and_now():
    clean = load("clean_transfer.json")
    return clean, clean.created_at + timedelta(minutes=1)


def make_api(*items, now=None, known_payees=()):
    clean, default_now = clean_and_now()
    return GatewayAPI(
        parser=FakeParser(*items) if items else FakeParser(clean),
        clock=lambda: now or default_now,
        known_payees=known_payees,
    )


# ── POST /api/message ─────────────────────────────────────────────────


def test_message_returns_a_draft_item_with_validator_and_policy_attached():
    response = make_api().message({"transcript": "Send fifty dollars to John Smith."})
    assert response["intents_detected"] == 1
    item = response["items"][0]
    assert item["kind"] == "draft"
    assert item["validator"] == {"verdict": "pass", "discrepancies": []}
    assert item["policy"] == {"decision": "allow", "reason": None}


def test_message_stores_the_draft_so_execute_can_find_it():
    api = make_api()
    draft_id = api.message({"transcript": "hi"})["items"][0]["draft"]["id"]
    assert draft_id in api.store


def test_message_records_the_verdict_so_check_eleven_can_pass():
    """Without this, /api/execute would fail closed on every draft the server
    itself just offered."""
    api = make_api()
    response = api.message({"transcript": "hi"})
    from backend.canonical import draft_hash

    digest = draft_hash(response["items"][0]["draft"])
    assert api.verdicts[digest] == "pass"


def test_a_question_item_carries_no_validator_and_no_policy():
    """CONTRACT.md §4: the server attaches those to draft items only."""
    question = ClarifyingQuestion(
        question_id="q-amount-001", field="amount", question="How much would you like to send?"
    )
    response = make_api(question).message({"transcript": "Send money to John Smith."})
    item = response["items"][0]
    assert item["kind"] == "question"
    assert item["question_id"] == "q-amount-001"
    assert "validator" not in item and "policy" not in item and "draft" not in item


def test_message_rejects_an_empty_transcript():
    response = make_api().message({"transcript": "   "})
    assert response["result"] == "rejected"
    assert response["reason_code"] == "malformed_request"


def test_a_parser_that_drops_an_intent_is_a_bug_not_a_rejection():
    """len(items) must equal intents_detected. There is no reason code for this,
    so it must fail loudly rather than show the user a short list."""

    class DroppingParser:
        def __call__(self, transcript):
            return ParseResult(items=[load("clean_transfer.json")], intents_detected=2)

    api = GatewayAPI(parser=DroppingParser(), clock=lambda: clean_and_now()[1])
    try:
        api.message({"transcript": "Send fifty to John and twenty to Jane."})
    except ParserContractViolation as exc:
        assert "2 intent" in str(exc)
    else:
        raise AssertionError("expected ParserContractViolation")


# ── POST /api/clarify ─────────────────────────────────────────────────


def clarify_request(api, draft_id, answer):
    return api.clarify({"draft_id": draft_id, "field": "payee", "answer": answer})


def test_clarify_produces_a_new_draft_with_a_new_id_and_nonce():
    ambiguous = load("ambiguous_payee.json")
    api = make_api(ambiguous, now=ambiguous.created_at + timedelta(minutes=1))
    original_id = api.message({"transcript": "Send fifty dollars to John."})["items"][0]["draft"]["id"]

    response = clarify_request(api, original_id, "payee-102")
    resolved = response["items"][0]["draft"]
    assert resolved["id"] != original_id
    assert resolved["nonce"] != ambiguous.nonce
    assert resolved["payee"] == {
        "id": "payee-102",
        "display_name": "John Smith",
        "masked_account": "****8892",
    }
    assert resolved["unresolved"] == []
    assert "payee_candidates" not in resolved
    assert resolved["confidence"] == CLARIFIED_CONFIDENCE


def test_clarify_leaves_the_original_draft_unresolved_and_unusable():
    """CONTRACT.md §4: the old id stays unresolved and can never be executed."""
    ambiguous = load("ambiguous_payee.json")
    api = make_api(ambiguous, now=ambiguous.created_at + timedelta(minutes=1))
    original_id = api.message({"transcript": "Send fifty dollars to John."})["items"][0]["draft"]["id"]

    clarify_request(api, original_id, "payee-102")
    assert api.store[original_id].unresolved == ["payee"]
    assert api.challenge({"draft_id": original_id})["reason_code"] == "unresolved_fields"


def test_clarify_rejects_an_answer_that_was_not_offered():
    ambiguous = load("ambiguous_payee.json")
    api = make_api(ambiguous, now=ambiguous.created_at + timedelta(minutes=1))
    draft_id = api.message({"transcript": "Send fifty dollars to John."})["items"][0]["draft"]["id"]

    response = clarify_request(api, draft_id, "payee-999")
    assert response["reason_code"] == "invalid_clarification_answer"
    assert api.store[draft_id].unresolved == ["payee"]  # draft unchanged
    assert len(api.store) == 1  # nothing new was stored


def test_clarify_rejects_a_field_that_is_not_unresolved():
    api = make_api()
    draft_id = api.message({"transcript": "hi"})["items"][0]["draft"]["id"]
    response = api.clarify({"draft_id": draft_id, "field": "payee", "answer": "payee-102"})
    assert response["reason_code"] == "invalid_clarification_answer"


def test_clarify_rejects_a_field_that_cannot_be_clarified_in_place():
    """CONTRACT.md §2: only `payee` may be unresolved inside an existing draft."""
    draft = load("clean_transfer.json")
    draft.unresolved = ["amount"]
    api = make_api(draft)
    draft_id = api.message({"transcript": "hi"})["items"][0]["draft"]["id"]
    response = api.clarify({"draft_id": draft_id, "field": "amount", "answer": "10.00"})
    assert response["reason_code"] == "invalid_clarification_answer"


def test_clarify_rejects_an_unknown_draft():
    response = make_api().clarify(
        {"draft_id": "99999999-9999-9999-9999-999999999999", "field": "payee", "answer": "x"}
    )
    assert response["reason_code"] == "unknown_draft"


def test_the_question_form_of_clarify_is_refused_not_half_implemented():
    """CONTRACT.md §4 marks it proposed and not committed."""
    response = make_api().clarify({"question_id": "q-amount-001", "answer": "fifty"})
    assert response["reason_code"] == "invalid_clarification_answer"


# ── POST /api/webauthn/challenge ──────────────────────────────────────


def test_challenge_is_the_base64url_of_the_raw_hash_not_of_the_hex():
    api = make_api()
    draft_id = api.message({"transcript": "hi"})["items"][0]["draft"]["id"]
    response = api.challenge({"draft_id": draft_id})
    assert response["challenge"] == b64url_encode(bytes.fromhex(response["draft_hash"]))
    assert len(response["draft_hash"]) == 64
    assert response["rp_id"] == RP_ID
    assert response["user_verification"] == "required"
    assert response["allow_credentials"] == []


def test_challenge_refuses_an_unresolved_draft():
    ambiguous = load("ambiguous_payee.json")
    api = make_api(ambiguous, now=ambiguous.created_at + timedelta(minutes=1))
    draft_id = api.message({"transcript": "hi"})["items"][0]["draft"]["id"]
    assert api.challenge({"draft_id": draft_id})["reason_code"] == "unresolved_fields"


def test_challenge_refuses_an_expired_draft():
    clean, _ = clean_and_now()
    api = make_api(now=clean.expires_at)
    draft_id = api.message({"transcript": "hi"})["items"][0]["draft"]["id"]
    assert api.challenge({"draft_id": draft_id})["reason_code"] == "expired"


def test_challenge_refuses_an_unknown_draft():
    assert make_api().challenge({"draft_id": "nope"})["reason_code"] == "unknown_draft"


# ── POST /api/webauthn/register ───────────────────────────────────────


def test_register_issues_a_credential_and_keeps_the_key():
    api = make_api()
    private_key = ec.generate_private_key(ec.SECP256R1())
    response = api.register({"user_id": "user-001", "public_key": public_key_to_pem(private_key.public_key())})
    assert response["status"] == "registered"
    assert response["credential_id"] in api.credentials
    assert api.credentials[response["credential_id"]] == public_key_to_pem(private_key.public_key())


def test_register_requires_a_user_id():
    assert make_api().register({})["reason_code"] == "malformed_request"


# ── GET /api/audit/verify ─────────────────────────────────────────────


def test_audit_verify_reports_an_intact_chain_and_counts_rows():
    api = make_api()
    assert api.audit_verify() == {"intact": True, "broken_row": None, "rows_checked": 0}
    api.message({"transcript": "hi"})
    assert api.audit_verify()["rows_checked"] == 1
    assert api.audit_verify()["intact"]


# ── End to end ────────────────────────────────────────────────────────


def sign_assertion(private_key, credential_id, challenge):
    client_data = json.dumps(
        {"type": "webauthn.get", "challenge": challenge, "origin": ORIGIN}
    ).encode()
    authenticator_data = (
        hashlib.sha256(RP_ID.encode()).digest()
        + bytes([FLAG_USER_PRESENT | FLAG_USER_VERIFIED])
        + (1).to_bytes(4, "big")
    )
    signed = authenticator_data + hashlib.sha256(client_data).digest()
    return {
        "credential_id": credential_id,
        "authenticator_data": b64url_encode(authenticator_data),
        "client_data_json": b64url_encode(client_data),
        "signature": b64url_encode(
            der_to_raw_signature(private_key.sign(signed, ec.ECDSA(hashes.SHA256())))
        ),
    }


def test_user_text_to_posted_transaction_with_a_real_signature():
    private_key = ec.generate_private_key(ec.SECP256R1())
    api = make_api()
    credential_id = api.register(
        {"user_id": "user-001", "public_key": public_key_to_pem(private_key.public_key())}
    )["credential_id"]

    draft_id = api.message({"transcript": "Send fifty dollars to John Smith."})["items"][0]["draft"]["id"]
    challenge = api.challenge({"draft_id": draft_id})["challenge"]

    result = api.execute(
        {
            "draft_id": draft_id,
            "credential_id": credential_id,
            "idempotency_key": "idem-1",
            "assertion": sign_assertion(private_key, credential_id, challenge),
        }
    )
    assert result["result"] == "executed", result
    assert len(api.ledger.entries) == 1
    assert api.ledger.entries[0]["draft"]["id"] == draft_id
    assert api.audit_verify()["intact"]


def test_a_replayed_idempotency_key_does_not_post_twice():
    private_key = ec.generate_private_key(ec.SECP256R1())
    api = make_api()
    credential_id = api.register(
        {"user_id": "user-001", "public_key": public_key_to_pem(private_key.public_key())}
    )["credential_id"]
    draft_id = api.message({"transcript": "hi"})["items"][0]["draft"]["id"]
    challenge = api.challenge({"draft_id": draft_id})["challenge"]
    body = {
        "draft_id": draft_id,
        "credential_id": credential_id,
        "idempotency_key": "idem-1",
        "assertion": sign_assertion(private_key, credential_id, challenge),
    }
    first = api.execute(body)
    again = api.execute(body)
    assert first == again
    assert len(api.ledger.entries) == 1


def test_a_signature_for_another_draft_is_rejected_end_to_end():
    """The whole point of the design: a signature over draft A is worthless
    for draft B, even with the same key."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    clean, now = clean_and_now()
    # A second, different draft: same shape, different id and nonce, so a
    # different hash and therefore a different challenge.
    other = TransactionDraft.model_validate(
        {
            **clean.model_dump(mode="json", exclude_none=True),
            "id": "88888888-8888-8888-8888-888888888888",
            "nonce": "nonce-other-001",
        }
    )
    api = GatewayAPI(parser=FakeParser(clean, other), clock=lambda: now)
    credential_id = api.register(
        {"user_id": "user-001", "public_key": public_key_to_pem(private_key.public_key())}
    )["credential_id"]
    items = api.message({"transcript": "hi"})["items"]
    target_id = items[0]["draft"]["id"]
    other_id = items[1]["draft"]["id"]

    other_challenge = api.challenge({"draft_id": other_id})["challenge"]
    result = api.execute(
        {
            "draft_id": target_id,
            "credential_id": credential_id,
            "idempotency_key": "idem-1",
            "assertion": sign_assertion(private_key, credential_id, other_challenge),
        }
    )
    assert result["reason_code"] == "hash_mismatch"
    assert api.ledger.entries == []
