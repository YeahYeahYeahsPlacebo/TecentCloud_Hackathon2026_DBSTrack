"""
`/api/webauthn/challenge` and `/api/webauthn/register` (CONTRACT.md §4).

**There is no separate random challenge: the challenge IS the draft hash** —
base64url of the raw 32-byte SHA-256, not of the 64-character hex string. The
draft's ``nonce`` is what makes it unpredictable, which is why a draft must
carry a fresh nonce and why a clarification mints a new one.

The server refuses to issue a challenge for a draft it did not store, that has
unresolved fields, that has expired, or that the validator has not passed —
there is nothing worth signing in those cases, and handing out a challenge
would only spend the user's biometric on something ``/api/execute`` is going
to refuse.

**Attestation is not verified.** Registration accepts an already-extracted
public key. Verifying the authenticator's attestation statement against a
metadata service is a separate piece of work and is not even sketched here;
until it exists, registration is a placeholder (as CONTRACT.md §4 says).

**The credential id is the browser's, not ours.** ``navigator.credentials.create()``
mints the credential id (``rawId``); the server has no business inventing a
different one, because the id is what the authenticator puts in the assertion
and what ``allowCredentials`` has to name. So ``/api/webauthn/register`` takes
``credential_id`` — base64url of ``rawId`` — and stores the public key under
*that* key. Only when a client sends no id at all does the server generate one
(the placeholder path, which cannot sign anything and is therefore never
offered back to a browser).
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, MutableMapping, Optional

from cryptography.hazmat.primitives import serialization

from backend.canonical import draft_hash
from backend.gateway import reason_codes as R
from backend.gateway.errors import rejection_response
from backend.gateway.execute import b64url_encode
from backend.gateway.webauthn import coerce_es256_public_key
from backend.models.draft import TransactionDraft

USER_VERIFICATION = "required"

# A registration with no public key: it holds the id down but cannot sign.
PLACEHOLDER_CREDENTIAL = "unregistered-placeholder"

# WebAuthn caps a credential id at 1023 bytes (WebAuthn L2 §5.4.3, "The
# credential id... MUST NOT exceed 1023 bytes").
MAX_CREDENTIAL_ID_BYTES = 1023

# base64url, with the '=' padding a frontend's btoa()-style encoder may add.
_CREDENTIAL_ID_RE = re.compile(r"[A-Za-z0-9_-]+=*")


def dump(draft: Any) -> dict:
    """The hashed object, whether the store holds a model or a dict."""
    if isinstance(draft, TransactionDraft):
        return draft.model_dump(mode="json", exclude_none=True)
    return TransactionDraft.model_validate(draft).model_dump(mode="json", exclude_none=True)


def canonical_credential_id(value: str) -> str:
    """The one spelling of a credential id this system uses: unpadded base64url.

    A browser's ``rawId`` is an ``ArrayBuffer``, so how it arrives depends on
    how the frontend stringifies it — padded or not, and occasionally still in
    standard base64 (``+``/``/``). This accepts the padded form and re-encodes
    it unpadded, so the id the server stores, the id it hands back in
    ``allow_credentials``, and the id ``/api/execute`` looks up are always the
    same string. Anything else is refused: an id that is stored under one
    spelling and sent under another reads as "credential is not registered" at
    execute, which looks like a server bug and is really an encoding one.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("credential_id must be a non-empty string")
    stripped = value.strip()
    if not _CREDENTIAL_ID_RE.fullmatch(stripped):
        raise ValueError(
            "credential_id must be base64url of the credential's rawId "
            "(A-Z, a-z, 0-9, '-', '_', optional '=' padding)"
        )
    try:
        raw = base64.urlsafe_b64decode(stripped + "=" * (-len(stripped) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"credential_id is not decodable base64url: {exc}") from None
    if not raw:
        raise ValueError("credential_id decodes to zero bytes")
    if len(raw) > MAX_CREDENTIAL_ID_BYTES:
        raise ValueError(
            f"credential_id is {len(raw)} bytes; WebAuthn allows at most "
            f"{MAX_CREDENTIAL_ID_BYTES}"
        )
    return b64url_encode(raw)


def _public_key_bytes(value: Any) -> Optional[bytes]:
    """A comparable form of a stored credential, or None if it is unusable.

    The same key can arrive as PEM in one registration and as base64 DER in
    another, so comparing the inputs would call them different. Comparing the
    DER does not.
    """
    try:
        key = coerce_es256_public_key(value)
    except (ValueError, TypeError):
        return None
    return key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


class CredentialService:
    """Issues challenges for stored drafts and registers credentials."""

    def __init__(
        self,
        *,
        store: Mapping[str, Any],
        credentials: MutableMapping[str, Any],
        rp_id: str = "localhost",
        audit: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
        policy: Optional[Callable[[dict, str], Any]] = None,
        verdicts: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.store = store
        self.credentials = credentials
        self.rp_id = rp_id
        self.audit = audit
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.policy = policy
        self.verdicts: Mapping[str, str] = verdicts if verdicts is not None else {}
        # Credential ids this server invented. They are real entries in
        # `credentials` — /api/execute accepts them — but no authenticator will
        # ever put one in an assertion, so they are kept out of
        # allow_credentials.
        self._generated_ids: set[str] = set()

    # ── POST /api/webauthn/challenge ──────────────────────────────────

    def challenge(self, request: Mapping[str, Any]) -> dict:
        if not isinstance(request, Mapping) or "draft_id" not in request:
            return rejection_response(R.MALFORMED_REQUEST, "'draft_id' is required")

        draft_id = request["draft_id"]
        if not isinstance(draft_id, str):
            # Unhashable otherwise: the lookup would raise and a malformed
            # body would come back as a 500.
            return rejection_response(R.MALFORMED_REQUEST, "'draft_id' must be a string")

        stored = self.store.get(draft_id)
        if stored is None:
            return rejection_response(
                R.UNKNOWN_DRAFT, f"no stored draft with id {draft_id!r}"
            )

        draft = (
            stored
            if isinstance(stored, TransactionDraft)
            else TransactionDraft.model_validate(stored)
        )
        if draft.unresolved:
            return rejection_response(
                R.UNRESOLVED_FIELDS,
                f"stored draft has unresolved fields: {draft.unresolved}",
            )
        if self.clock() >= draft.expires_at:
            return rejection_response(
                R.EXPIRED, f"draft expired at {draft.expires_at.isoformat()}"
            )

        dumped = dump(draft)
        digest = draft_hash(dumped)

        # Refuse anything other than a recorded pass — including *no* verdict.
        # This is deliberately the same condition as check 11, not a similar
        # one: if check 11 would refuse at execute, this endpoint refuses at
        # challenge, and the two must not be able to drift apart. (Agreed
        # with Member 1; CONTRACT.md §4 words it the same way.)
        #
        # A challenge is the server saying "I am willing to let you sign
        # this". Issuing one for a draft check 11 or 12 will refuse only
        # spends the user's biometric on something that cannot succeed.
        verdict = self.verdicts.get(digest)
        if verdict != "pass":
            message = (
                "no validator verdict is recorded for this draft; the server "
                "never checked this copy, so there is nothing to sign"
                if verdict is None
                else f"validator verdict for this draft is {verdict!r}, not 'pass'"
            )
            return rejection_response(R.VALIDATOR_FROZEN, message)

        if self.policy is not None:
            decision = self.policy(dumped, digest)
            if getattr(decision, "decision", None) == "block":
                return rejection_response(
                    R.POLICY_BLOCKED, getattr(decision, "reason", "blocked by policy")
                )

        return {
            "draft_id": draft.id,
            "draft_hash": digest,
            "challenge": b64url_encode(bytes.fromhex(digest)),
            "rp_id": self.rp_id,
            "user_verification": USER_VERIFICATION,
            "allow_credentials": self._signable_credential_ids(),
        }

    def _signable_credential_ids(self) -> list[str]:
        """The credential ids a browser can actually sign with.

        Two kinds of entry cannot be used by a browser, and naming either in
        ``allowCredentials`` only makes ``navigator.credentials.get()`` fail
        with ``NotAllowedError`` — an error whose cause the frontend cannot
        see. Both are left out:

        * a registration that carried no public key (it cannot sign at all);
        * an id this server generated itself, because no authenticator will
          ever put an id it did not mint into an assertion.
        """
        usable = []
        for credential_id in sorted(self.credentials):
            if credential_id in self._generated_ids:
                continue
            if _public_key_bytes(self.credentials[credential_id]) is None:
                continue
            usable.append(credential_id)
        return usable

    # ── POST /api/webauthn/register ───────────────────────────────────

    def register(self, request: Mapping[str, Any]) -> dict:
        """Placeholder registration.

        Accepts the browser's credential id (base64url of ``rawId``) and an
        optional already-extracted public key, and stores the key under that
        id. It does **not** verify an attestation statement, so a client can
        register any key pair it likes — fine for a hackathon prototype, not
        fine for production.
        """
        if not isinstance(request, Mapping) or "user_id" not in request:
            return rejection_response(R.MALFORMED_REQUEST, "'user_id' is required")

        # The id is the authenticator's, not ours: it is what comes back in
        # the assertion and what allowCredentials has to name.
        requested_id = request.get("credential_id")
        if requested_id is None:
            credential_id = b64url_encode(secrets.token_bytes(16))
            self._generated_ids.add(credential_id)
            id_source = "server"
        else:
            try:
                credential_id = canonical_credential_id(requested_id)
            except ValueError as exc:
                return rejection_response(
                    R.MALFORMED_REQUEST,
                    f"unusable credential_id: {exc}. Send base64url of "
                    "attestationResponse.credential.rawId — the id the browser "
                    "made, not one of your own.",
                )
            # A client claiming an id we once generated makes it theirs.
            self._generated_ids.discard(credential_id)
            id_source = "client"

        public_key = request.get("public_key")
        if public_key is not None:
            # Refuse a key we will never be able to verify a signature against.
            # Storing one anyway reports "registered" and then fails much later
            # with signature_invalid, which reads like a signing bug on the
            # client rather than a registration bug.
            try:
                coerce_es256_public_key(public_key)
            except (ValueError, TypeError) as exc:
                return rejection_response(
                    R.MALFORMED_REQUEST,
                    f"unusable public_key: {exc}. Send either a PEM "
                    "SubjectPublicKeyInfo string or base64/base64url of the DER. "
                    "A COSE_Key map written as JSON will not work: COSE keys are "
                    "integers (-1, -2, -3) and JSON keys can only be strings.",
                )

        # Re-registering an id the server already knows is only harmless when
        # it is the same key. A different key under an id the client believes
        # is already enrolled would silently replace it.
        if public_key is not None and credential_id in self.credentials:
            stored = _public_key_bytes(self.credentials[credential_id])
            offered = _public_key_bytes(public_key)
            if stored is not None and stored != offered:
                return rejection_response(
                    R.MALFORMED_REQUEST,
                    f"credential_id {credential_id} is already registered with a "
                    "different public key; register a new credential instead of "
                    "replacing this one's key",
                )

        if public_key is not None:
            self.credentials[credential_id] = public_key
        elif credential_id not in self.credentials:
            # Placeholder: holds no key, so it is never offered to a browser.
            self.credentials[credential_id] = PLACEHOLDER_CREDENTIAL

        if self.audit is not None:
            self.audit.append(
                "webauthn_register",
                {
                    "user_id": request.get("user_id"),
                    "credential_id": credential_id,
                    "credential_id_source": id_source,
                },
            )
        return {
            "status": "registered",
            "credential_id": credential_id,
            # "server" means the client did not send one: that id is ours, and
            # no browser can produce an assertion with it.
            "credential_id_source": id_source,
        }
