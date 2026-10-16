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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.canonical import draft_hash  # noqa: E402
from backend.gateway.api import GatewayAPI  # noqa: E402
from backend.gateway.message import MessageService, dump  # noqa: E402
from backend.gateway.execute import (  # noqa: E402
    FLAG_USER_PRESENT,
    FLAG_USER_VERIFIED,
    b64url_encode,
)
from backend.gateway.message import (  # noqa: E402
    CLARIFIED_CONFIDENCE,
    ParserContractViolation,
    ServerNotConfigured,
    ValidatorUnusable,
    require_validator,
)
from backend.gateway.webauthn import der_to_raw_signature, public_key_to_pem  # noqa: E402
from backend.llm.fake_client import FakeChatClient  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402
from backend.parser.directory import FixtureDirectory  # noqa: E402
from backend.parser.independent import independent_transfer_clauses  # noqa: E402
from backend.parser.interface import (  # noqa: E402
    ClarifyingQuestion,
    ParseResult,
    PendingQuestion,
)
from gateway_fakes import FakeValidator, passing_validator  # noqa: E402
import pytest  # noqa: E402
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


def live(draft, *, minutes_alive=1, window=5):
    """Re-stamp a frozen fixture onto the real wall clock.

    The fixtures are dated 2026-10-03. Member 1's ``resolve_payee`` and
    ``resolve_question`` check expiry against ``datetime.now()``, not against
    the gateway's injected clock, so a frozen fixture reads as expired and
    every clarification test would pass for the wrong reason ("expired"
    instead of the branch the test names). Shifting the window onto now keeps
    the assertion honest.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    data = draft.model_dump(mode="json", exclude_none=True)
    data["created_at"] = now - timedelta(minutes=minutes_alive)
    data["expires_at"] = now + timedelta(minutes=window)
    return TransactionDraft.model_validate(data)


def make_api(
    *items,
    now=None,
    known_payees=(),
    validator=None,
    directory=None,
    client=None,
    parser=None,
):
    clean, default_now = clean_and_now()
    return GatewayAPI(
        parser=parser
        if parser is not None
        else (FakeParser(*items) if items else FakeParser(clean)),
        clock=lambda: now or default_now,
        known_payees=known_payees,
        validator=validator if validator is not None else passing_validator(),
        # /api/clarify resolves payees through the directory, not from the
        # candidate fields on the draft.
        directory=directory if directory is not None else FixtureDirectory(),
        client=client,
    )


# ── POST /api/message ─────────────────────────────────────────────────


def test_message_returns_a_draft_item_with_validator_and_policy_attached():
    response = make_api().message({"transcript": "Send fifty dollars to John Smith."})
    assert response["intents_detected"] == 1
    item = response["items"][0]
    assert item["kind"] == "draft"
    assert item["validator"]["verdict"] == "pass"
    assert item["validator"]["discrepancies"] == []
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


def test_the_gateway_has_no_default_that_can_pass(monkeypatch):
    """The most important item from review: with no VALIDATOR_BACKEND=llm,
    nothing the server does may produce a pass. It freezes and says why."""
    monkeypatch.delenv("VALIDATOR_BACKEND", raising=False)
    monkeypatch.delenv("DCTA_ALLOW_UNVALIDATED", raising=False)
    clean, now = clean_and_now()
    api = GatewayAPI(parser=FakeParser(clean), clock=lambda: now)  # no validator
    draft_id = api.message({"transcript": "hi"})["items"][0]["draft"]["id"]

    item = api.messages.message({"transcript": "hi"})["items"][0]
    assert item["validator"]["verdict"] == "freeze"
    assert item["validator"]["reason"] == "validator_not_configured"
    assert api.challenge({"draft_id": draft_id})["reason_code"] == "validator_frozen"

    # Check 11 is the last line of defence, so prove it holds even against a
    # perfectly valid signature over the right draft: everything before it
    # passes, and the freeze still stops the transaction.
    private_key = ec.generate_private_key(ec.SECP256R1())
    credential_id = api.register(
        {"user_id": "user-001", "public_key": public_key_to_pem(private_key.public_key())}
    )["credential_id"]
    challenge = b64url_encode(
        bytes.fromhex(draft_hash(dump(api.store[draft_id])))
    )
    result = api.execute(
        {
            "draft_id": draft_id,
            "credential_id": credential_id,
            "idempotency_key": "idem-1",
            "assertion": sign_assertion(private_key, credential_id, challenge),
        }
    )
    assert result["reason_code"] == "validator_frozen"
    assert api.ledger.entries == []


def test_DCTA_ALLOW_UNVALIDATED_is_the_only_way_to_get_a_pass(monkeypatch):
    """Opt-in, explicit, and labelled in the verdict — not a silent default."""
    monkeypatch.delenv("VALIDATOR_BACKEND", raising=False)
    monkeypatch.setenv("DCTA_ALLOW_UNVALIDATED", "true")
    clean, now = clean_and_now()
    api = GatewayAPI(parser=FakeParser(clean), clock=lambda: now)
    item = api.message({"transcript": "hi"})["items"][0]
    assert item["validator"]["verdict"] == "pass"
    assert "unvalidated" in item["validator"]["reason"]


def test_a_missing_or_wrongly_shaped_validator_is_refused_at_startup():
    with pytest.raises(ValidatorUnusable):
        require_validator(None)
    # A plain callable cannot carry the ClarificationRecord, so it is refused
    # rather than invoked. There is no callable fallback anywhere.
    with pytest.raises(ValidatorUnusable):
        require_validator(lambda draft, transcript: {"verdict": "pass"})

    class CallableOnly:
        def __call__(self, draft, transcript):
            return {"verdict": "pass"}

    with pytest.raises(ValidatorUnusable):
        MessageService(
            parser=FakeParser(), store={}, validator=CallableOnly()
        )


class DroppingParser:
    """Claims two intents but returns one item: an intent was dropped."""

    def __call__(self, transcript):
        return ParseResult(items=[load("clean_transfer.json")], intents_detected=2)


def test_a_parser_that_drops_an_intent_is_a_bug_not_a_rejection():
    """len(items) must equal intents_detected. There is no reason code for this,
    so it must fail loudly rather than show the user a short list.

    This is the SINGLE-request path, so the sentence must be one that
    independent_transfer_clauses does not split. A two-transfer sentence
    takes the clause path instead and fails earlier, with a different
    message (see the next test)."""
    api = GatewayAPI(parser=DroppingParser(), clock=lambda: clean_and_now()[1])
    try:
        api.message({"transcript": "Send fifty dollars to John Smith."})
    except ParserContractViolation as exc:
        assert "2 intent" in str(exc)
    else:
        raise AssertionError("expected ParserContractViolation")


def test_a_clause_that_drops_an_intent_is_a_bug_and_nothing_is_stored():
    """The same broken parser on a sentence that IS split into two independent
    transfers. Each clause is parsed on its own and must yield exactly one
    item, so the check fires per clause, before the single-path "2 intent"
    check is ever reached -- and before anything is stored or published."""
    transcript = "Send fifty to John and twenty to Jane."
    # Guard against this test silently drifting back onto the single path.
    assert independent_transfer_clauses(transcript, FixtureDirectory()) is not None

    api = GatewayAPI(parser=DroppingParser(), clock=lambda: clean_and_now()[1])
    with pytest.raises(ParserContractViolation) as caught:
        api.message({"transcript": transcript})

    assert str(caught.value) == "each independent clause must produce exactly one item"
    assert "2 intent" not in str(caught.value)

    # Nothing was stored, recorded or left pending.
    assert len(api.store) == 0          # drafts the gateway could later execute
    assert len(api.verdicts) == 0       # validator verdicts, keyed by draft hash
    assert len(api.messages.pending) == 0
    assert len(api.audit.rows) == 0     # no "message" audit row either


# ── POST /api/clarify ─────────────────────────────────────────────────


def clarify_request(api, draft_id, answer):
    return api.clarify({"draft_id": draft_id, "field": "payee", "answer": answer})


def test_clarify_produces_a_new_draft_with_a_new_id_and_nonce():
    ambiguous = live(load("ambiguous_payee.json"))
    api = make_api(ambiguous, now=ambiguous.created_at + timedelta(minutes=1))
    original_id = api.message({"transcript": "Send fifty dollars to John."})["items"][0]["draft"]["id"]

    response = clarify_request(api, original_id, "payee-102")
    resolved = response["items"][0]["draft"]
    assert resolved["id"] != original_id
    assert resolved["nonce"] != ambiguous.nonce
    # The directory is authoritative, not the candidate fields on the draft.
    # Fixture and directory now agree that payee-102 is "John Lee", so this
    # reads the name from the directory rather than hard-coding it: if the
    # two ever diverge again, the directory is what the draft must carry.
    record = next(p for p in FixtureDirectory().payees() if p.id == "payee-102")
    assert resolved["payee"] == {
        "id": "payee-102",
        "display_name": record.display_name,
        "masked_account": record.masked_account,
    }
    assert resolved["unresolved"] == []
    assert "payee_candidates" not in resolved
    assert resolved["confidence"] == CLARIFIED_CONFIDENCE


def test_clarify_leaves_the_original_draft_unresolved_and_unusable():
    """CONTRACT.md §4: the old id stays unresolved and can never be executed."""
    ambiguous = live(load("ambiguous_payee.json"))
    api = make_api(ambiguous, now=ambiguous.created_at + timedelta(minutes=1))
    original_id = api.message({"transcript": "Send fifty dollars to John."})["items"][0]["draft"]["id"]

    clarify_request(api, original_id, "payee-102")
    assert api.store[original_id].unresolved == ["payee"]
    assert api.challenge({"draft_id": original_id})["reason_code"] == "unresolved_fields"


def test_clarify_rejects_an_answer_that_was_not_offered():
    ambiguous = live(load("ambiguous_payee.json"))
    api = make_api(ambiguous, now=ambiguous.created_at + timedelta(minutes=1))
    draft_id = api.message({"transcript": "Send fifty dollars to John."})["items"][0]["draft"]["id"]

    response = clarify_request(api, draft_id, "payee-999")
    assert response["reason_code"] == "invalid_clarification_answer"
    assert "not one of the offered" in response["message"]
    assert api.store[draft_id].unresolved == ["payee"]  # draft unchanged
    assert len(api.store) == 1  # nothing new was stored


def test_clarify_rejects_an_expired_draft():
    """resolve_payee checks expiry itself, against the real clock."""
    ambiguous = live(load("ambiguous_payee.json"), minutes_alive=10, window=-5)
    api = make_api(ambiguous, now=ambiguous.created_at + timedelta(minutes=1))
    draft_id = api.message({"transcript": "Send fifty dollars to John."})["items"][0]["draft"]["id"]
    response = clarify_request(api, draft_id, "payee-102")
    assert response["reason_code"] == "invalid_clarification_answer"
    assert "expired" in response["message"]


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


def test_an_unknown_question_id_is_refused():
    """A question_id the server never issued has no pending state to resolve."""
    response = make_api().clarify({"question_id": "q-amount-001", "answer": "fifty"})
    assert response["reason_code"] == "invalid_clarification_answer"


# ── POST /api/clarify, question form (CONTRACT.md §4, second form) ────


def pending_question(field="amount", transcript="Send some money to John Smith."):
    """A PendingQuestion as Member 1's parser would hand it to us."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return PendingQuestion(
        question_id=f"q-{field}-001",
        field=field,
        original_transcript=transcript,
        partial={
            "intent_type": "transfer",
            "source_account": "acct-001-2345",
            "payee": {
                "id": "payee-001",
                "display_name": "John Smith",
                "masked_account": "****1234",
            },
        },
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )


def asking_parser(pending):
    """A parser that asks one question instead of producing a draft."""

    class _Asking:
        def __call__(self, transcript):
            return ParseResult(
                items=[
                    ClarifyingQuestion(
                        question_id=pending.question_id,
                        field=pending.field,
                        question=f"What {pending.field}?",
                    )
                ],
                intents_detected=1,
                pending={pending.question_id: pending},
            )

    return _Asking()


def amount_reply():
    return json.dumps(
        {"resolved": True, "amount_text": "fifty dollars", "amount_value": "50.00"}
    )


def test_the_question_form_produces_a_draft_and_never_sends_pending():
    pending = pending_question()
    api = make_api(
        parser=asking_parser(pending), client=FakeChatClient([amount_reply()])
    )
    offered = api.message({"transcript": pending.original_transcript})
    item = offered["items"][0]
    assert item["kind"] == "question"
    # PendingQuestion carries the original transcript and the partial draft.
    # It is server-only: nothing here may leak it.
    assert "pending" not in offered and "pending" not in item
    assert set(item) == {"kind", "question_id", "field", "question"}

    answered = api.clarify(
        {"question_id": pending.question_id, "answer": "fifty dollars"}
    )
    draft = answered["items"][0]["draft"]
    assert draft["amount"] == {"value": "50.00", "currency": "SGD"}
    assert draft["payee"]["id"] == "payee-001"
    assert draft["unresolved"] == []
    # CONTRACT.md §4: the answer is never merged into the transcript.
    assert draft["transcript"] == pending.original_transcript
    assert "fifty" not in draft["transcript"]


def test_a_question_id_is_single_use():
    """Answering twice must not work — the second finds nothing to resolve."""
    pending = pending_question()
    api = make_api(
        parser=asking_parser(pending),
        client=FakeChatClient([amount_reply(), amount_reply()]),
    )
    api.message({"transcript": pending.original_transcript})

    first = api.clarify({"question_id": pending.question_id, "answer": "fifty dollars"})
    assert first["items"][0]["kind"] == "draft"
    # Consumed on use, so the id is gone from server state.
    assert pending.question_id not in api.messages.pending

    again = api.clarify({"question_id": pending.question_id, "answer": "fifty dollars"})
    assert again["reason_code"] == "invalid_clarification_answer"
    assert "already answered" in again["message"]


def test_the_clarification_record_reaches_the_validator_separately():
    """validate() gets the answer as its own argument, not inside the
    transcript, so the validator can compare the two."""
    pending = pending_question()
    validator = passing_validator()
    api = make_api(
        parser=asking_parser(pending),
        client=FakeChatClient([amount_reply()]),
        validator=validator,
    )
    api.message({"transcript": pending.original_transcript})
    api.clarify({"question_id": pending.question_id, "answer": "fifty dollars"})

    draft, transcript, record = validator.calls[-1]
    assert transcript == pending.original_transcript
    assert record.question_id == pending.question_id
    assert record.field == "amount"
    assert record.original_transcript == pending.original_transcript
    assert record.answer == "fifty dollars"


def test_a_second_request_inside_an_answer_is_refused():
    pending = pending_question()
    api = make_api(
        parser=asking_parser(pending), client=FakeChatClient([amount_reply()])
    )
    api.message({"transcript": pending.original_transcript})
    response = api.clarify(
        {
            "question_id": pending.question_id,
            "answer": "fifty dollars and also transfer $20 to Jane",
        }
    )
    assert response["reason_code"] == "invalid_clarification_answer"


def test_clarify_without_a_directory_fails_loudly_not_as_a_rejection():
    """No directory means no authoritative payee. That is our problem, not the
    user's, so it must not come back as a 422 that blames the answer."""
    ambiguous = live(load("ambiguous_payee.json"))
    store = {}
    service = MessageService(
        parser=FakeParser(ambiguous),
        store=store,
        validator=passing_validator(),
        directory=None,  # the gateway itself defaults to a real one
    )
    draft_id = service.message({"transcript": "hi"})["items"][0]["draft"]["id"]
    with pytest.raises(ServerNotConfigured):
        service.clarify({"draft_id": draft_id, "field": "payee", "answer": "payee-102"})


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
    # A passing validator: this test is about signature binding, so nothing
    # before check 9 may reject.
    api = GatewayAPI(
        parser=FakeParser(clean, other),
        clock=lambda: now,
        validator=passing_validator(),
    )
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
