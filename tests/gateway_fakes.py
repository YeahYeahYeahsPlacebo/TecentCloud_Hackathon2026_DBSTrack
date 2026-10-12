"""Shared fakes for gateway tests.

Shaped like the real ``backend/validator`` interface — a ``validate()`` method
returning a ``ValidatorVerdict`` — because the gateway accepts nothing else.
A plain callable is rejected on purpose (it cannot carry a
ClarificationRecord), so tests must use this rather than a lambda.

Kept out of conftest.py so Member 1's fixtures are untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.canonical import draft_hash  # noqa: E402
from backend.validator.interface import ValidatorVerdict  # noqa: E402


class FakeValidator:
    """A validator that returns a fixed verdict.

    ``validate()`` is the only accepted form; ``__call__`` raises, mirroring
    the real interface's deliberate refusal of the callable form.
    """

    def __init__(
        self,
        verdict: str = "pass",
        reason: str = "ok",
        discrepancies: list | None = None,
        bad_hash: str = "",
    ) -> None:
        self.verdict = verdict
        self.reason = reason
        self.discrepancies = discrepancies if discrepancies is not None else []
        self.bad_hash = bad_hash
        self.calls: list = []

    def validate(self, draft, transcript, clarification=None) -> ValidatorVerdict:
        self.calls.append((draft, transcript, clarification))
        digest = draft_hash(draft.model_dump(mode="json", exclude_none=True))
        return ValidatorVerdict(
            verdict=self.verdict,
            discrepancies=self.discrepancies,
            reason=self.reason,
            draft_hash=self.bad_hash or digest,
        )

    def __call__(self, *args, **kwargs):
        raise TypeError(
            "Call validate(draft, transcript, clarification=record); "
            "the callable form drops the ClarificationRecord."
        )


def passing_validator() -> FakeValidator:
    """The common case: a validator with nothing to object to."""
    return FakeValidator("pass", reason="ok")


def freezing_validator(reason: str = "amount disagrees") -> FakeValidator:
    """A validator that refuses the draft."""
    return FakeValidator("freeze", reason=reason, discrepancies=["amount"])


# ── A real signature, so a test can reach /api/execute ─────────────────
# Every step after 8 needs a signed assertion. Copying this plumbing into a
# second test file would let the copies drift apart, and drift here means a
# test that quietly stops exercising step 9. One copy, here.

ORIGIN = "http://localhost:8000"
RP_ID = "localhost"


def new_key():
    """A fresh P-256 key, which is what WebAuthn ES256 means."""
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


def sign_assertion(private_key, credential_id: str, challenge: str) -> dict:
    """An assertion over ``authenticator_data || SHA256(client_data_json)``."""
    import hashlib
    import json

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    from backend.gateway.execute import (
        FLAG_USER_PRESENT,
        FLAG_USER_VERIFIED,
        b64url_encode,
    )
    from backend.gateway.webauthn import der_to_raw_signature

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


def signed_full_run(api, private_key, idempotency_key: str = "key-1") -> dict:
    """message -> register -> challenge -> signed execute.

    Returns the ``/api/execute`` response, so a caller can assert on the
    reason code rather than on the happy path only.
    """
    from backend.gateway.webauthn import public_key_to_pem

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
            "idempotency_key": idempotency_key,
            "assertion": sign_assertion(private_key, credential_id, challenge),
        }
    )
