"""What `/api/webauthn/register` accepts — and whether it actually works.

Written to answer a direct question from the frontend: *can a browser create
and register a usable credential, and is there a test path that returns a
validator pass without the unvalidated bypass?*

The answers this file pins down:

1. **A PEM SubjectPublicKeyInfo string works**, and so does base64 or
   base64url of the same DER — which is what a browser's ``getPublicKey()``
   gives you once it is stringified. Both are proven end to end: register,
   challenge, sign, execute, and the transaction actually posts.

2. **A COSE_Key map written as JSON does not work**, and cannot be made to
   work over JSON: COSE keys are integers (``-1``, ``-2``, ``-3``) and JSON
   object keys can only be strings. The endpoint now refuses it with
   ``malformed_request`` instead of returning ``registered`` and failing
   later with ``signature_invalid`` — a failure that reads like a signing
   bug in the frontend rather than a registration bug.

3. **The credential id is the browser's, not the server's.**
   ``navigator.credentials.create()`` mints the id (``rawId``), and it is that
   id which comes back inside the assertion and which ``allowCredentials`` has
   to name. The server used to invent its own, which left the frontend holding
   an id no authenticator would ever produce. ``/api/webauthn/register`` now
   takes ``credential_id`` — base64url of ``rawId`` — and stores the key under
   it. Every happy-path test here sends one, because a test that lets the
   server pick the id does not cover the browser at all.

4. **The validator is orthogonal to all of this.** Whether a challenge is
   issued at all depends on the verdict; see
   ``tests/test_validator_integration.py`` for that side.
"""

from __future__ import annotations

import base64
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from backend.gateway import reason_codes as R  # noqa: E402
from backend.gateway.api import GatewayAPI  # noqa: E402
from backend.gateway.webauthn import public_key_to_pem  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402
from backend.parser.interface import ParseResult  # noqa: E402
from gateway_fakes import new_key, passing_validator, sign_assertion  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "drafts"
TRANSCRIPT = "Send fifty dollars to John Smith."


def live(draft: TransactionDraft, *, minutes_alive: int = 1, window: int = 5) -> TransactionDraft:
    """Re-stamp a frozen fixture onto the real wall clock.

    The fixtures are dated 2026-10-03, so an un-stamped draft reads as
    expired and every challenge comes back `expired` — the test would then
    fail for a reason that has nothing to do with registration.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    data = draft.model_dump(mode="json", exclude_none=True)
    data["created_at"] = now - timedelta(minutes=minutes_alive)
    data["expires_at"] = now + timedelta(minutes=window)
    return TransactionDraft.model_validate(data)


class FakeParser:
    """One fixed draft, so a test is about registration, not parsing."""

    def __init__(self, *items) -> None:
        self.items = list(items) or [
            live(
                TransactionDraft.model_validate_json(
                    (FIXTURES / "clean_transfer.json").read_text()
                )
            )
        ]

    def __call__(self, transcript: str) -> ParseResult:
        return ParseResult(items=self.items, intents_detected=len(self.items))


def make_api() -> GatewayAPI:
    """A gateway with a passing validator, so a challenge can be issued.

    The validator is injected rather than configured: how a verdict is
    reached is Member 1's side, and it is not what this file is testing.
    """
    return GatewayAPI(parser=FakeParser(), validator=passing_validator())


def der_of(public_key: ec.EllipticCurvePublicKey) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


# Stand-in for the ArrayBuffer a real navigator.credentials.create() returns.
BROWSER_RAW_ID = bytes(range(32))


def raw_id(value: bytes = BROWSER_RAW_ID, *, padded: bool = False) -> str:
    """base64url of a rawId, padded or not — both reach the server in practice."""
    encoded = base64.urlsafe_b64encode(value).decode("ascii")
    return encoded if padded else encoded.rstrip("=")


def register_then_execute(
    api: GatewayAPI, private_key: ec.EllipticCurvePrivateKey, encode
) -> dict:
    """Register ``encode(private_key's public half)``, then sign with it.

    The key that signs must be the other half of the key that was registered,
    which is the whole property under test: a key stored wrong cannot produce
    a signature that verifies.

    The credential id is sent by the client, the way a browser does — that is
    the property the second half of this file is about.

    Returns the ``/api/execute`` response, so a caller can assert either
    "it posted" or "it was refused, and here is why".
    """
    registration = api.register(
        {
            "user_id": "user-001",
            "credential_id": raw_id(),
            "public_key": encode(private_key.public_key()),
        }
    )
    if registration.get("status") != "registered":
        return registration

    credential_id = registration["credential_id"]
    draft_id = api.message({"transcript": TRANSCRIPT})["items"][0]["draft"]["id"]
    challenge = api.challenge({"draft_id": draft_id})["challenge"]

    # Sign with the registered key's other half. A key that was stored
    # wrong cannot produce a signature that verifies, which is exactly the
    # failure these tests are about.
    return api.execute(
        {
            "draft_id": draft_id,
            "credential_id": credential_id,
            "idempotency_key": "idem-register-1",
            "assertion": sign_assertion(private_key, credential_id, challenge),
        }
    )


# ── What works ────────────────────────────────────────────────────────


def test_a_pem_public_key_registers_and_can_sign():
    """The documented form, and what the rest of the suite uses."""
    api = make_api()
    private_key = new_key()
    registration = api.register(
        {
            "user_id": "u",
            "credential_id": raw_id(),
            "public_key": public_key_to_pem(private_key.public_key()),
        }
    )
    assert registration["status"] == "registered"

    credential_id = registration["credential_id"]
    draft_id = api.message({"transcript": TRANSCRIPT})["items"][0]["draft"]["id"]
    challenge = api.challenge({"draft_id": draft_id})["challenge"]

    result = api.execute(
        {
            "draft_id": draft_id,
            "credential_id": credential_id,
            "idempotency_key": "idem-pem",
            "assertion": sign_assertion(private_key, credential_id, challenge),
        }
    )
    assert result["result"] == "executed", result


def test_a_base64_der_public_key_registers_and_can_sign():
    """The browser form: `getPublicKey()` returns a DER ArrayBuffer, and
    base64 of it is the shortest thing JSON can carry."""
    private_key = new_key()

    def encode(public_key):
        return base64.b64encode(der_of(public_key)).decode("ascii")

    assert register_then_execute(make_api(), private_key, encode)["result"] == "executed"


def test_a_base64url_der_without_padding_registers_and_can_sign():
    """Same, base64url and unpadded — what `btoa`-free frontend code tends
    to produce, and what the rest of this codebase already emits."""
    private_key = new_key()

    def encode(public_key):
        return base64.urlsafe_b64encode(der_of(public_key)).decode("ascii").rstrip("=")

    assert register_then_execute(make_api(), private_key, encode)["result"] == "executed"


# ── What does not, and says so ────────────────────────────────────────


def test_a_cose_key_written_as_json_is_refused_not_registered():
    """The trap this file exists for.

    COSE_Key uses integer labels, so `{"-2": ...}` — the only shape JSON can
    carry — does not match. It used to be stored anyway and reported
    "registered"; the user would then hit `signature_invalid` at execute and
    reasonably conclude their signing code was wrong.
    """
    api = make_api()
    response = api.register(
        {
            "user_id": "u",
            "public_key": {"1": 2, "3": -7, "-1": 1, "-2": "aa" * 32, "-3": "bb" * 32},
        }
    )
    assert response["reason_code"] == R.MALFORMED_REQUEST
    assert "COSE_Key" in response["message"]
    # Nothing was stored under a credential id.
    assert "credential_id" not in response


def test_an_unparseable_public_key_is_refused():
    api = make_api()
    response = api.register({"user_id": "u", "public_key": "not-a-key"})
    assert response["reason_code"] == R.MALFORMED_REQUEST


def test_a_non_es256_key_is_refused():
    """ES256 only: P-384 would weaken nothing on its own, but the server
    must not be talked into a curve it did not agree to."""
    api = make_api()
    other = ec.generate_private_key(ec.SECP384R1()).public_key()
    pem = other.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    response = api.register({"user_id": "u", "public_key": pem})
    assert response["reason_code"] == R.MALFORMED_REQUEST


def test_registering_with_no_public_key_still_reports_registered():
    """Unchanged, and deliberately still allowed: the placeholder path.

    It cannot sign anything — execute will refuse with `signature_invalid` —
    but nothing in the flow requires a key until /api/execute, and refusing
    here would break the shape CONTRACT.md §4 describes.
    """
    api = make_api()
    assert api.register({"user_id": "u"})["status"] == "registered"


# ── The credential id belongs to the browser ──────────────────────────


def test_the_browser_credential_id_is_the_one_stored_and_returned():
    """The mismatch this section exists for.

    `navigator.credentials.create()` makes the id. If the server answered
    with an id of its own, the frontend would hold one no authenticator
    would ever put in an assertion.
    """
    api = make_api()
    sent = raw_id()
    registration = api.register(
        {
            "user_id": "u",
            "credential_id": sent,
            "public_key": public_key_to_pem(new_key().public_key()),
        }
    )
    assert registration["credential_id"] == sent
    assert registration["credential_id_source"] == "client"
    assert sent in api.credentials


def test_the_challenge_offers_the_browser_credential_id():
    """`allow_credentials` must name the id the browser can actually use."""
    api = make_api()
    sent = raw_id()
    api.register(
        {
            "user_id": "u",
            "credential_id": sent,
            "public_key": public_key_to_pem(new_key().public_key()),
        }
    )
    draft_id = api.message({"transcript": TRANSCRIPT})["items"][0]["draft"]["id"]
    assert api.challenge({"draft_id": draft_id})["allow_credentials"] == [sent]


def test_a_browser_registration_signs_end_to_end():
    """rawId + base64url DER public key: register, challenge, sign, execute."""
    private_key = new_key()

    def encode(public_key):
        return base64.urlsafe_b64encode(der_of(public_key)).decode("ascii").rstrip("=")

    result = register_then_execute(make_api(), private_key, encode)
    assert result["result"] == "executed", result


def test_a_padded_credential_id_is_accepted_and_canonicalised():
    """`btoa`-style encoders pad. The server stores the unpadded spelling so
    registration, allow_credentials and /api/execute all agree on one string
    — otherwise execute says "not registered" for a credential that is."""
    api = make_api()
    registration = api.register(
        {
            "user_id": "u",
            "credential_id": raw_id(padded=True),
            "public_key": public_key_to_pem(new_key().public_key()),
        }
    )
    assert registration["credential_id"] == raw_id()
    assert registration["credential_id"] in api.credentials


def test_re_registering_a_credential_id_with_a_different_key_is_refused():
    """Silently swapping the key under an enrolled id would let one
    registration overwrite another's credential."""
    api = make_api()
    sent = raw_id()
    api.register(
        {
            "user_id": "u",
            "credential_id": sent,
            "public_key": public_key_to_pem(new_key().public_key()),
        }
    )
    response = api.register(
        {
            "user_id": "u",
            "credential_id": sent,
            "public_key": public_key_to_pem(new_key().public_key()),
        }
    )
    assert response["reason_code"] == R.MALFORMED_REQUEST
    assert "different public key" in response["message"]


def test_re_registering_the_same_key_is_allowed():
    """Idempotent: enrolling the same credential twice is not a conflict."""
    api = make_api()
    sent = raw_id()
    body = {
        "user_id": "u",
        "credential_id": sent,
        "public_key": public_key_to_pem(new_key().public_key()),
    }
    assert api.register(body)["status"] == "registered"
    assert api.register(body)["status"] == "registered"


def test_a_credential_id_that_is_not_base64url_is_refused():
    """Standard base64 is not base64url: '+' and '/' do not survive."""
    api = make_api()
    response = api.register(
        {
            "user_id": "u",
            "credential_id": "not base64url!",
            "public_key": public_key_to_pem(new_key().public_key()),
        }
    )
    assert response["reason_code"] == R.MALFORMED_REQUEST
    assert "rawId" in response["message"]


def test_an_empty_credential_id_is_refused():
    api = make_api()
    response = api.register(
        {"user_id": "u", "credential_id": "", "public_key": public_key_to_pem(new_key().public_key())}
    )
    assert response["reason_code"] == R.MALFORMED_REQUEST


def test_an_oversized_credential_id_is_refused():
    """WebAuthn caps a credential id at 1023 bytes."""
    api = make_api()
    response = api.register(
        {
            "user_id": "u",
            "credential_id": raw_id(bytes(1024)),
            "public_key": public_key_to_pem(new_key().public_key()),
        }
    )
    assert response["reason_code"] == R.MALFORMED_REQUEST
    assert "1023" in response["message"]


def test_a_server_generated_id_is_flagged_and_never_offered_to_a_browser():
    """No credential_id sent: the server still answers, but says so — and a
    key-less registration is left out of allow_credentials, because offering
    it makes navigator.credentials.get() fail with NotAllowedError for a
    reason the frontend cannot see."""
    api = make_api()
    registration = api.register({"user_id": "u"})
    assert registration["credential_id_source"] == "server"

    api.register(
        {
            "user_id": "u",
            "public_key": public_key_to_pem(new_key().public_key()),
        }
    )
    draft_id = api.message({"transcript": TRANSCRIPT})["items"][0]["draft"]["id"]
    assert api.challenge({"draft_id": draft_id})["allow_credentials"] == []
