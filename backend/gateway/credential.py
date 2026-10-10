"""
`/api/webauthn/challenge` and `/api/webauthn/register` (CONTRACT.md §4).

**There is no separate random challenge: the challenge IS the draft hash** —
base64url of the raw 32-byte SHA-256, not of the 64-character hex string. The
draft's ``nonce`` is what makes it unpredictable, which is why a draft must
carry a fresh nonce and why a clarification mints a new one.

The server refuses to issue a challenge for a draft it did not store, that has
unresolved fields, or that has expired — there is nothing to sign in those
cases, and handing out a challenge would only teach a client that the draft is
worth attacking.

**Attestation is not verified.** Registration accepts an already-extracted
public key. Verifying the authenticator's attestation statement against a
metadata service is a separate piece of work and is not even sketched here;
until it exists, registration is a placeholder (as CONTRACT.md §4 says).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, MutableMapping, Optional

from backend.canonical import draft_hash
from backend.gateway import reason_codes as R
from backend.gateway.errors import rejection_response
from backend.gateway.execute import b64url_encode
from backend.models.draft import TransactionDraft

USER_VERIFICATION = "required"


def dump(draft: Any) -> dict:
    """The hashed object, whether the store holds a model or a dict."""
    if isinstance(draft, TransactionDraft):
        return draft.model_dump(mode="json", exclude_none=True)
    return TransactionDraft.model_validate(draft).model_dump(mode="json", exclude_none=True)


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
    ) -> None:
        self.store = store
        self.credentials = credentials
        self.rp_id = rp_id
        self.audit = audit
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.policy = policy

    # ── POST /api/webauthn/challenge ──────────────────────────────────

    def challenge(self, request: Mapping[str, Any]) -> dict:
        if not isinstance(request, Mapping) or "draft_id" not in request:
            return rejection_response(R.MALFORMED_REQUEST, "'draft_id' is required")

        stored = self.store.get(request["draft_id"])
        if stored is None:
            return rejection_response(
                R.UNKNOWN_DRAFT, f"no stored draft with id {request['draft_id']!r}"
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

        # Do not issue a challenge for a draft that policy has already
        # blocked. A challenge is the server saying "I am willing to let you
        # sign this"; issuing one for a draft step 12 would reject only spends
        # the user's biometric on something that cannot succeed.
        if self.policy is not None:
            dumped = dump(draft)
            decision = self.policy(dumped, draft_hash(dumped))
            if getattr(decision, "decision", None) == "block":
                return rejection_response(
                    R.POLICY_BLOCKED, getattr(decision, "reason", "blocked by policy")
                )

        digest = draft_hash(dump(draft))
        return {
            "draft_id": draft.id,
            "draft_hash": digest,
            "challenge": b64url_encode(bytes.fromhex(digest)),
            "rp_id": self.rp_id,
            "user_verification": USER_VERIFICATION,
            "allow_credentials": sorted(self.credentials),
        }

    # ── POST /api/webauthn/register ───────────────────────────────────

    def register(self, request: Mapping[str, Any]) -> dict:
        """Placeholder registration.

        Accepts an optional already-extracted COSE public key. It does **not**
        verify an attestation statement, so a client can register any key pair
        it likes — fine for a hackathon prototype, not fine for production.
        """
        if not isinstance(request, Mapping) or "user_id" not in request:
            return rejection_response(R.MALFORMED_REQUEST, "'user_id' is required")

        credential_id = b64url_encode(secrets.token_bytes(16))
        public_key = request.get("public_key")
        self.credentials[credential_id] = (
            public_key if public_key is not None else "unregistered-placeholder"
        )

        if self.audit is not None:
            self.audit.append(
                "webauthn_register",
                {"user_id": request.get("user_id"), "credential_id": credential_id},
            )
        return {"status": "registered", "credential_id": credential_id}
