"""Mock bank wired into the gateway: money actually moves, and a draft the
account cannot cover is refused before the user ever signs it.

These are the tests that make Phase 1's "first filmable demo" honest. Without
them, `/api/execute` returns ``executed`` and no balance anywhere changes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.bank import Account, Bank, seed_bank  # noqa: E402
from backend.gateway.api import GatewayAPI, _UNSET  # noqa: E402
from backend.gateway.execute import (  # noqa: E402
    FLAG_USER_PRESENT,
    FLAG_USER_VERIFIED,
    b64url_encode,
)
from backend.gateway.webauthn import der_to_raw_signature, public_key_to_pem  # noqa: E402
from backend.models.draft import Money, Payee, TransactionDraft  # noqa: E402
from backend.parser.interface import ParseResult  # noqa: E402
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


def make_api(*items, bank=_UNSET, now=None):
    clean = load("clean_transfer.json")
    at = now or clean.created_at + timedelta(minutes=1)
    return GatewayAPI(
        parser=FakeParser(*items) if items else FakeParser(clean),
        clock=lambda: at,
        bank=bank,
    )


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


def full_run(api, private_key):
    """message -> challenge -> signed execute. Returns the execute response."""
    clean = load("clean_transfer.json")
    message = api.message({"transcript": "Send fifty dollars to John Smith."})
    draft_id = message["items"][0]["draft"]["id"]
    credential_id = api.register(
        {"user_id": "user-001", "public_key": public_key_to_pem(private_key.public_key())}
    )["credential_id"]
    challenge = api.challenge({"draft_id": draft_id, "credential_id": credential_id})[
        "challenge"
    ]
    return api.execute(
        {
            "draft_id": draft_id,
            "credential_id": credential_id,
            "idempotency_key": "key-1",
            "assertion": sign_assertion(private_key, credential_id, challenge),
        }
    )


# ── Money actually moves ───────────────────────────────────────────────


def test_executing_a_signed_draft_moves_the_money():
    """The demo's whole point: sign, then balances change."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    api = make_api()

    before = api.bank.balance("acct-001-2345")
    assert api.bank.balance("payee-001") == Decimal("0.00")

    result = full_run(api, private_key)
    assert result["result"] == "executed", result

    # Fifty dollars left the account and arrived at John Smith.
    assert api.bank.balance("acct-001-2345") == before - Decimal("50.00")
    assert api.bank.balance("payee-001") == Decimal("50.00")


def test_the_ledger_and_the_bank_agree():
    """The ledger says what happened; the bank says the result. Both, or the
    books and the balances drift apart."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    api = make_api()
    assert full_run(api, private_key)["result"] == "executed"

    assert len(api.ledger.entries) == 1
    posted = api.ledger.entries[0]["draft"]
    assert posted["payee"]["id"] == "payee-001"
    assert posted["amount"]["value"] == "50.00"
    assert api.bank.balance("payee-001") == Decimal("50.00")


def test_a_default_api_has_a_seeded_bank():
    """Someone constructing GatewayAPI for a demo should not have to know the
    bank exists."""
    api = make_api()
    assert api.bank is not None
    assert api.bank.balance("acct-001-2345") == Decimal("10000.00")


# ── Insufficient funds ─────────────────────────────────────────────────


def poor_bank() -> Bank:
    """A bank where the account cannot cover the fifty-dollar draft."""
    return Bank(
        [
            Account("acct-001-2345", "My Account", "****2345", Decimal("10.00"), "SGD"),
            Account("payee-001", "John Smith", "****1234", Decimal("0.00"), "SGD"),
        ]
    )


def test_policy_blocks_an_unaffordable_draft_before_signing():
    """The user should never be asked to sign a draft the bank would refuse.
    This is checked in policy (step 12), so it happens before WebAuthn."""
    api = make_api(bank=poor_bank())
    response = api.message({"transcript": "Send fifty dollars to John Smith."})
    decision = response["items"][0]["policy"]
    assert decision["decision"] == "block"
    assert "insufficient funds" in decision["reason"]


def test_an_unaffordable_draft_is_never_issued_a_challenge():
    """No challenge means nothing to sign — the draft dies before the
    authenticator is involved."""
    clean = load("clean_transfer.json")
    api = make_api(bank=poor_bank())
    message = api.message({"transcript": "Send fifty dollars to John Smith."})
    draft_id = message["items"][0]["draft"]["id"]

    private_key = ec.generate_private_key(ec.SECP256R1())
    credential_id = api.register(
        {"user_id": "u", "public_key": public_key_to_pem(private_key.public_key())}
    )["credential_id"]

    assert api.challenge({"draft_id": draft_id, "credential_id": credential_id})[
        "result"
    ] == "rejected"


def test_executing_an_unaffordable_draft_moves_nothing():
    """Defence in depth: if a signed draft somehow reaches execute with an
    account that cannot cover it, no money moves and nothing is recorded."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    api = make_api(bank=poor_bank())

    clean = load("clean_transfer.json")
    message = api.message({"transcript": "Send fifty dollars to John Smith."})
    draft_id = message["items"][0]["draft"]["id"]
    credential_id = api.register(
        {"user_id": "u", "public_key": public_key_to_pem(private_key.public_key())}
    )["credential_id"]

    # Bypass the challenge gate and sign the hash directly, so the request
    # arrives at execute with a valid signature over the right draft.
    from backend.canonical import draft_hash

    stored = api.store[draft_id]
    digest = draft_hash(stored.model_dump(mode="json", exclude_none=True))
    challenge = b64url_encode(bytes.fromhex(digest))

    result = api.execute(
        {
            "draft_id": draft_id,
            "credential_id": credential_id,
            "idempotency_key": "key-1",
            "assertion": sign_assertion(private_key, credential_id, challenge),
        }
    )

    assert result["result"] == "rejected"
    assert result["reason_code"] == "policy_blocked"
    assert api.bank.balance("acct-001-2345") == Decimal("10.00"), "money moved anyway"
    assert api.bank.balance("payee-001") == Decimal("0.00")
    assert api.ledger.entries == [], "recorded a transfer that never happened"


def test_a_bank_with_no_money_still_runs_every_check():
    """``bank=None`` means no balances change; it must not break execution."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    api = make_api(bank=None)
    assert full_run(api, private_key)["result"] == "executed"
    assert api.bank is None


# ── Repeat transfers ───────────────────────────────────────────────────


def test_two_transfers_to_two_different_johns_land_in_different_accounts():
    """The ambiguity is resolved by which payee id the user picked."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    api = make_api()

    for index, (payee_id, amount) in enumerate(
        (("payee-001", "50.00"), ("payee-102", "25.00"))
    ):
        clean = load("clean_transfer.json")
        targeted = clean.model_copy(
            update={
                "id": f"33333333-3333-3333-3333-33333333333{index}",
                "nonce": f"nonce-{index}",
                "payee": Payee(
                    id=payee_id,
                    display_name="John Smith",
                    masked_account=clean.payee.masked_account,
                ),
                "amount": Money(value=amount, currency="SGD"),
            }
        )
        api.messages.parser = FakeParser(targeted)
        message = api.message({"transcript": "x"})
        draft_id = message["items"][0]["draft"]["id"]
        credential_id = api.register(
            {"user_id": "u", "public_key": public_key_to_pem(private_key.public_key())}
        )["credential_id"]
        challenge = api.challenge(
            {"draft_id": draft_id, "credential_id": credential_id}
        )["challenge"]
        result = api.execute(
            {
                "draft_id": draft_id,
                "credential_id": credential_id,
                "idempotency_key": f"key-{payee_id}",
                "assertion": sign_assertion(private_key, credential_id, challenge),
            }
        )
        assert result["result"] == "executed", result

    assert api.bank.balance("payee-001") == Decimal("50.00")
    assert api.bank.balance("payee-102") == Decimal("25.00")
    assert api.bank.balance("acct-001-2345") == Decimal("10000.00") - Decimal("75.00")
