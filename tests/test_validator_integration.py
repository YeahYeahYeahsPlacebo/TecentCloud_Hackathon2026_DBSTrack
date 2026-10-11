"""Wiring Member 1's real validator into the gateway.

Until ``backend/validator`` landed, check 11 had never been exercised against
a verdict that was not the placeholder's constant ``"pass"``. These tests put
a real-shaped validator behind the gateway and make sure a ``"freeze"``
verdict actually stops execution — which is the whole point of check 11.

The interface being adapted to (backend/validator/interface.py):

    validator.validate(draft_model, transcript, clarification=None)
        -> ValidatorVerdict   (verdict, discrepancies, reason, draft_hash)

Its ``__call__`` is deliberately disabled and raises, so the gateway must
detect and call ``validate()`` as a method rather than invoking the object.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.canonical import draft_hash  # noqa: E402
from backend.gateway.api import GatewayAPI  # noqa: E402
from backend.gateway.execute import (  # noqa: E402
    FLAG_USER_PRESENT,
    FLAG_USER_VERIFIED,
    b64url_encode,
)
from backend.gateway.webauthn import der_to_raw_signature, public_key_to_pem  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402
from backend.parser.interface import ParseResult  # noqa: E402
from backend.validator.interface import ValidatorVerdict  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "drafts"
ORIGIN = "http://localhost:8000"
RP_ID = "localhost"


def load(name: str) -> TransactionDraft:
    return TransactionDraft.model_validate_json((FIXTURES / name).read_text())


class FakeParser:
    def __init__(self, *items) -> None:
        self.items = list(items)

    def __call__(self, transcript: str) -> ParseResult:
        return ParseResult(items=self.items, intents_detected=len(self.items))


class FakeValidator:
    """Shaped exactly like backend/validator: a ``validate()`` method, a
    disabled ``__call__``, and a ValidatorVerdict carrying the draft hash."""

    def __init__(self, verdict: str = "pass", reason: str = "ok", bad_hash: str = "") -> None:
        self.verdict = verdict
        self.reason = reason
        self.bad_hash = bad_hash
        self.calls: list = []

    def validate(self, draft, transcript, clarification=None) -> ValidatorVerdict:
        self.calls.append((draft, transcript, clarification))
        digest = draft_hash(draft.model_dump(mode="json", exclude_none=True))
        return ValidatorVerdict(
            verdict=self.verdict,
            discrepancies=[] if self.verdict == "pass" else ["amount"],
            reason=self.reason,
            draft_hash=self.bad_hash or digest,
        )

    def __call__(self, *args, **kwargs):
        raise TypeError(
            "Call validate(draft, transcript, clarification=record); "
            "the callable form drops the ClarificationRecord."
        )


def make_api(validator=None):
    clean = load("clean_transfer.json")
    return GatewayAPI(
        parser=FakeParser(clean),
        clock=lambda: clean.created_at + timedelta(minutes=1),
        validator=validator,
    )


def sign_assertion(private_key, credential_id, challenge):
    client_data = json.dumps(
        {"type": "webauthn.get", "challenge": challenge, "origin": ORIGIN}
    ).encode()
    authenticator_data = (
        hashlib_sha256(RP_ID)
        + bytes([FLAG_USER_PRESENT | FLAG_USER_VERIFIED])
        + (1).to_bytes(4, "big")
    )
    signed = authenticator_data + hashlib_sha256_bytes(client_data)
    return {
        "credential_id": credential_id,
        "authenticator_data": b64url_encode(authenticator_data),
        "client_data_json": b64url_encode(client_data),
        "signature": b64url_encode(
            der_to_raw_signature(private_key.sign(signed, ec.ECDSA(hashes.SHA256())))
        ),
    }


import hashlib as _hashlib  # noqa: E402


def hashlib_sha256(text: str) -> bytes:
    return _hashlib.sha256(text.encode()).digest()


def hashlib_sha256_bytes(raw: bytes) -> bytes:
    return _hashlib.sha256(raw).digest()


def execute_draft(api, draft_id):
    private_key = ec.generate_private_key(ec.SECP256R1())
    credential_id = api.register(
        {"user_id": "u", "public_key": public_key_to_pem(private_key.public_key())}
    )["credential_id"]
    stored = api.store[draft_id]
    digest = draft_hash(stored.model_dump(mode="json", exclude_none=True))
    challenge = b64url_encode(bytes.fromhex(digest))
    return api.execute(
        {
            "draft_id": draft_id,
            "credential_id": credential_id,
            "idempotency_key": "key-1",
            "assertion": sign_assertion(private_key, credential_id, challenge),
        }
    )


# ── The real interface is actually used ────────────────────────────────


def test_the_gateway_calls_validate_not_the_disabled_call():
    """Member 1 disabled ``__call__`` on purpose. Invoking the object would
    raise — so if this passes, the gateway took the method path."""
    validator = FakeValidator("pass")
    api = make_api(validator)
    response = api.message({"transcript": "Send fifty dollars to John Smith."})

    assert len(validator.calls) == 1
    draft, transcript, clarification = validator.calls[0]
    assert isinstance(draft, TransactionDraft), "validator wants the model, not a dict"
    assert transcript == "Send fifty dollars to John Smith."
    assert response["items"][0]["validator"]["verdict"] == "pass"


def test_a_real_validator_receives_the_draft_model_not_a_dict():
    """Its signature is ``validate(draft: TransactionDraft, ...)``."""
    validator = FakeValidator("pass")
    make_api(validator).message({"transcript": "x"})
    draft, _, _ = validator.calls[0]
    assert hasattr(draft, "model_dump")


# ── A freeze verdict actually stops execution ──────────────────────────


def test_a_frozen_draft_is_rejected_at_check_eleven():
    """The point of check 11. With the placeholder this could never fail,
    because the placeholder always said pass."""
    api = make_api(FakeValidator("freeze", reason="amount disagrees"))
    message = api.message({"transcript": "Send fifty dollars to John Smith."})
    draft_id = message["items"][0]["draft"]["id"]

    assert message["items"][0]["validator"]["verdict"] == "freeze"

    # It never gets a challenge either: no point spending the user's
    # biometric on a draft check 11 will refuse.
    assert (
        api.challenge({"draft_id": draft_id, "credential_id": "c"})["reason_code"]
        == "validator_frozen"
    )

    result = execute_draft(api, draft_id)
    assert result["result"] == "rejected"
    assert result["reason_code"] == "validator_frozen"


def test_a_frozen_draft_moves_no_money():
    validator = FakeValidator("freeze")
    api = make_api(validator)
    draft_id = api.message({"transcript": "Send fifty dollars to John Smith."})["items"][
        0
    ]["draft"]["id"]

    before = api.bank.balance("acct-001-2345")
    assert execute_draft(api, draft_id)["result"] == "rejected"
    assert api.bank.balance("acct-001-2345") == before
    assert api.ledger.entries == []


def test_a_passing_verdict_still_lets_execution_through():
    """The happy path must not be collateral damage of the freeze path."""
    api = make_api(FakeValidator("pass"))
    draft_id = api.message({"transcript": "Send fifty dollars to John Smith."})["items"][
        0
    ]["draft"]["id"]
    assert execute_draft(api, draft_id)["result"] == "executed"


# ── Verdict binding ────────────────────────────────────────────────────


def test_a_verdict_for_a_different_draft_is_not_trusted():
    """A ValidatorVerdict carries the hash of the draft it checked. One that
    names some other draft must not be recorded as a pass for this one."""
    validator = FakeValidator("pass", bad_hash="a" * 64)
    api = make_api(validator)
    response = api.message({"transcript": "Send fifty dollars to John Smith."})
    item = response["items"][0]

    assert item["validator"]["verdict"] == "freeze"
    assert "not the stored draft" in item["validator"]["reason"]

    draft_id = item["draft"]["id"]
    assert execute_draft(api, draft_id)["reason_code"] == "validator_frozen"


def test_a_verdict_with_no_hash_is_still_accepted():
    """draft_hash is optional in the model; absent means 'not bound', which is
    the validator's choice, not a mismatch."""
    validator = FakeValidator("pass")
    api = make_api(validator)

    class NoHashValidator(FakeValidator):
        def validate(self, draft, transcript, clarification=None):
            return ValidatorVerdict(
                verdict="pass", discrepancies=[], reason="ok", draft_hash=""
            )

    api.messages.validator = NoHashValidator("pass")
    response = api.message({"transcript": "Send fifty dollars to John Smith."})
    assert response["items"][0]["validator"]["verdict"] == "pass"


# ── The old callable form still works ──────────────────────────────────


def test_a_plain_callable_validator_is_still_supported():
    """Tests and the placeholder use ``validator(draft_dict, transcript)``."""
    seen = {}

    def callable_validator(draft_dict, transcript):
        seen["dict"] = draft_dict
        seen["transcript"] = transcript
        return {"verdict": "pass", "discrepancies": []}

    api = make_api(callable_validator)
    response = api.message({"transcript": "Send fifty dollars to John Smith."})
    assert response["items"][0]["validator"]["verdict"] == "pass"
    assert isinstance(seen["dict"], dict)


def test_the_default_is_still_the_explicit_placeholder():
    """Omitting a validator must not quietly become 'always pass' without
    saying so — the placeholder is named and documented as a placeholder."""
    api = make_api()
    response = api.message({"transcript": "Send fifty dollars to John Smith."})
    assert response["items"][0]["validator"]["verdict"] == "pass"
