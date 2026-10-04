# CONTRACT.md

The inter-member agreement for the Direct Conversational Transaction
Agent (DCTA). This document defines the three lanes, the data contract,
the API, and the rejection model. It links to — but does not duplicate —
the schema, fixtures, and hashing rule.

---

## 1. Overview

There are three lanes:

| Member | Lane | Owns |
|---|---|---|
| Member 1 | AI layer | `backend/parser`, `backend/validator` |
| Member 2 | Frontend | `frontend/` |
| Member 3 | Backend services | `backend/gateway`, `backend/ledger`, `backend/audit`, `backend/policy` |

**The one rule that matters:** the generative layer (Member 1) produces
drafts; it never executes funds. It has zero write access to the ledger.
Money moves only through the gateway (Member 3), which rejects any
request lacking a valid cryptographic signature bound to the exact draft
the user approved.

The flow is:

1. User speaks or types text.
2. The parser (Member 1) produces a **draft**.
3. The validator (Member 1) compares the transcript to the draft.
4. The policy engine (Member 3) decides whether the draft may proceed.
5. The frontend (Member 2) displays the draft and collects a signature.
6. The gateway (Member 3) verifies the signature and executes — or
   rejects with a reason code.

---

## 2. The draft

The draft is the core data contract. It is defined in:

- **JSON Schema:** [`contract/draft.schema.json`](contract/draft.schema.json)
- **Pydantic model:** [`backend/models/draft.py`](backend/models/draft.py)
- **Fixtures:** [`fixtures/drafts/`](fixtures/drafts/)

This document does not duplicate the schema. Always refer to the schema
file as the source of truth.

### `unresolved`

An array of field names the parser could not resolve. If non-empty, the
draft **cannot be signed or executed**. The gateway rejects it with
`unresolved_fields` regardless of any frontend check.

Example: `["payee"]` means the parser could not determine which payee
the user meant. The user must clarify before the draft can proceed.

### `payee_candidates`

When `"payee"` is in `unresolved`, the draft may include
`payee_candidates`: an array of `{id, display_name, masked_account}`
objects, each fully populated (all fields required and non-null). The
frontend presents these to the user for selection. Once the user
selects one, a `/api/clarify` call resolves the ambiguity and the
parser returns an updated draft with `payee` populated and `"payee"`
removed from `unresolved`.

See [`fixtures/drafts/ambiguous_payee.json`](fixtures/drafts/ambiguous_payee.json)
for a working example.

### Frontend never builds a draft

The frontend (Member 2) **never constructs or edits a draft**. It
receives the draft from the server, displays it, and sends back a
signature. The signature is over the canonical hash of the exact bytes
the server returned. The frontend may not add, remove, or modify fields.

---

## 3. Hashing

The signature is over the SHA-256 of the canonical JSON serialisation
of the draft. The canonicalisation rule is defined in:

- **Rule:** [`contract/CANONICAL_HASH.md`](contract/CANONICAL_HASH.md)
- **Python reference:** `backend/canonical.py`
- **JavaScript mirror:** `frontend/js/canonical.js`
- **Test vectors:** [`contract/test_vectors.json`](contract/test_vectors.json)

Every implementation — Python or JavaScript — must produce the hashes
listed in `contract/test_vectors.json`. The test suite
(`tests/test_canonical.py` and `frontend/js/canonical.test.js`) enforces
this. If an implementation disagrees with the vectors, it is wrong.

Key points (see `CANONICAL_HASH.md` for the full rule):

- Keys sorted by Unicode **code point** at every nesting level.
- No whitespace between tokens.
- Non-ASCII characters are not escaped (emitted as raw UTF-8).
- Floats are rejected, not formatted.
- `signature` and `confidence` are excluded from the hash.
- All other fields are included — nothing can be smuggled in unsigned.

---

## 4. API endpoints

All endpoints accept and return JSON. Examples below are built from the
real fixtures in `fixtures/drafts/`.

### POST /api/message

User text in. Returns the draft, `unresolved` fields, the validator
verdict, and the policy decision.

**Request:**

```json
{
  "transcript": "Send fifty dollars to John Smith."
}
```

**Response (200):** (built from `clean_transfer.json`)

```json
{
  "draft": {
    "id": "11111111-1111-1111-1111-111111111111",
    "created_at": "2026-10-03T10:00:00Z",
    "expires_at": "2026-10-03T10:05:00Z",
    "nonce": "nonce-clean-transfer-001",
    "intent_type": "transfer",
    "source_account": "acct-001-2345",
    "amount": {
      "value": "50.00",
      "currency": "SGD"
    },
    "confidence": 0.95,
    "unresolved": [],
    "transcript": "Send fifty dollars to John Smith.",
    "payee": {
      "id": "payee-001",
      "display_name": "John Smith",
      "masked_account": "****1234"
    }
  },
  "unresolved": [],
  "validator": {
    "verdict": "pass",
    "discrepancies": []
  },
  "policy": {
    "decision": "allow",
    "reason": null
  }
}
```

**Response (200, ambiguous payee):** (built from `ambiguous_payee.json`)

```json
{
  "draft": {
    "id": "22222222-2222-2222-2222-222222222222",
    "created_at": "2026-10-03T10:00:00Z",
    "expires_at": "2026-10-03T10:05:00Z",
    "nonce": "nonce-ambiguous-payee-001",
    "intent_type": "transfer",
    "source_account": "acct-001-2345",
    "amount": {
      "value": "50.00",
      "currency": "SGD"
    },
    "confidence": 0.4,
    "unresolved": ["payee"],
    "transcript": "Send fifty dollars to John.",
    "payee": null,
    "payee_candidates": [
      {
        "id": "payee-101",
        "display_name": "John Doe",
        "masked_account": "****4521"
      },
      {
        "id": "payee-102",
        "display_name": "John Smith",
        "masked_account": "****8892"
      }
    ]
  },
  "unresolved": ["payee"],
  "validator": {
    "verdict": "pass",
    "discrepancies": []
  },
  "policy": {
    "decision": "hold",
    "reason": "unresolved_fields"
  }
}
```

### POST /api/clarify

A field name plus the user's answer. Returns the updated draft.

**Request:**

```json
{
  "draft_id": "22222222-2222-2222-2222-222222222222",
  "field": "payee",
  "answer": "payee-102"
}
```

**Response (200):**

```json
{
  "draft": {
    "id": "22222222-2222-2222-2222-222222222222",
    "created_at": "2026-10-03T10:00:00Z",
    "expires_at": "2026-10-03T10:05:00Z",
    "nonce": "nonce-ambiguous-payee-001",
    "intent_type": "transfer",
    "source_account": "acct-001-2345",
    "amount": {
      "value": "50.00",
      "currency": "SGD"
    },
    "confidence": 0.9,
    "unresolved": [],
    "transcript": "Send fifty dollars to John.",
    "payee": {
      "id": "payee-102",
      "display_name": "John Smith",
      "masked_account": "****8892"
    }
  },
  "unresolved": [],
  "validator": {
    "verdict": "pass",
    "discrepancies": []
  },
  "policy": {
    "decision": "allow",
    "reason": null
  }
}
```

### POST /api/execute

A draft, a signature, and an idempotency key. Returns the result or a
rejection.

**Request:**

```json
{
  "draft": {
    "id": "11111111-1111-1111-1111-111111111111",
    "created_at": "2026-10-03T10:00:00Z",
    "expires_at": "2026-10-03T10:05:00Z",
    "nonce": "nonce-clean-transfer-001",
    "intent_type": "transfer",
    "source_account": "acct-001-2345",
    "amount": {
      "value": "50.00",
      "currency": "SGD"
    },
    "confidence": 0.95,
    "unresolved": [],
    "transcript": "Send fifty dollars to John Smith.",
    "payee": {
      "id": "payee-001",
      "display_name": "John Smith",
      "masked_account": "****1234"
    }
  },
  "signature": "base64-encoded-signature-over-canonical-hash",
  "idempotency_key": "user-supplied-key-001"
}
```

**Response (200):**

```json
{
  "result": "executed",
  "transaction_id": "tx-20261003-000001",
  "idempotency_key": "user-supplied-key-001",
  "executed_at": "2026-10-03T10:01:00Z"
}
```

**Response (422, rejected):**

```json
{
  "result": "rejected",
  "reason_code": "hash_mismatch",
  "message": "The signature does not match the canonical hash of the draft."
}
```

### GET /api/audit/verify

Whether the audit chain is intact, and the first broken row if not.

**Response (200, intact):**

```json
{
  "intact": true,
  "broken_row": null,
  "rows_checked": 42
}
```

**Response (200, broken):**

```json
{
  "intact": false,
  "broken_row": {
    "row_id": 17,
    "expected_hash": "a1b2c3d4...",
    "actual_hash": "e5f6g7h8..."
  },
  "rows_checked": 42
}
```

### POST /api/webauthn/register (placeholder)

WebAuthn registration. To be implemented by Member 2 (frontend) and
Member 3 (gateway).

**Request (placeholder):**

```json
{
  "user_id": "user-001",
  "display_name": "John Smith"
}
```

**Response (placeholder):**

```json
{
  "status": "registered",
  "credential_id": "base64-encoded-credential-id"
}
```

### POST /api/webauthn/challenge (placeholder)

WebAuthn challenge for signing. To be implemented by Member 2 (frontend)
and Member 3 (gateway).

**Request (placeholder):**

```json
{
  "user_id": "user-001",
  "draft_hash": "49435db8b277e1a6a9b0db0585c3d0a452c197955776e1b5ab42c09288be2415"
}
```

**Response (placeholder):**

```json
{
  "challenge": "base64-encoded-challenge",
  "rp_id": "dcta.local"
}
```

---

## 5. Error response shape and rejection reason codes

### Standard error response

Every error uses the same shape:

```json
{
  "result": "rejected",
  "reason_code": "hash_mismatch",
  "message": "Human-readable explanation of what went wrong."
}
```

The `reason_code` is machine-readable. The `message` is for the
developer log, not necessarily for the user.

### Rejection reason codes

| Code | Triggered when |
|---|---|
| `missing_signature` | `/api/execute` is called without a `signature` field. |
| `malformed_signature` | The `signature` field is present but not valid base64 or not the expected format. |
| `hash_mismatch` | The signature is valid in format but was computed over a different draft hash — the draft was tampered with or is stale. |
| `signature_invalid` | The signature does not verify against the user's registered public key. |
| `expired` | The draft's `expires_at` timestamp is in the past. |
| `replayed` | The `idempotency_key` has already been used for a different draft. |
| `unresolved_fields` | The draft's `unresolved` array is non-empty. The gateway rejects this regardless of any frontend check. |
| `policy_blocked` | The policy engine determined the draft violates a rule (e.g. over per-transaction limit). |
| `validator_frozen` | The validator returned a verdict of `frozen` — the transcript does not match the draft. |
| `step_up_required` | Additional authentication (e.g. biometric or 2FA) is required for this transaction. |
| `schema_invalid` | The draft does not validate against `contract/draft.schema.json`. |

---

## 6. Folder ownership

| Folder | Member | Responsibility |
|---|---|---|
| `backend/parser/` | Member 1 | Parse user text into drafts; manage `unresolved` fields. |
| `backend/validator/` | Member 1 | Read-only comparison of transcript vs. draft; no tools, no ledger access. |
| `frontend/` | Member 2 | Display drafts, collect signatures, voice input. Never build or edit a draft. |
| `backend/gateway/` | Member 3 | Verify signatures, enforce idempotency, reject or execute. |
| `backend/ledger/` | Member 3 | The ledger. Only the gateway writes to it. |
| `backend/audit/` | Member 3 | Hash-chained audit log with `verify_chain()`. |
| `backend/policy/` | Member 3 | Pure deterministic policy engine. No LLM. |
| `contract/` | Shared | Schema, hashing rule, test vectors. Changes require agreement. |
| `fixtures/` | Shared | Test fixtures. Changes require agreement. |
| `tests/` | Shared | Test suite. |

Do not edit another member's folder without asking.

---

## 7. Test expectations

Each member must be able to demonstrate the following:

### Member 1 (AI layer)

- Every fixture in `fixtures/drafts/` validates against both the JSON
  Schema and the Pydantic model.
- A draft with a float amount is rejected.
- A draft with a missing required field is rejected.
- A draft with an unknown `intent_type` is rejected.
- A transfer with `payee: null` and empty `unresolved` is rejected.
- A payee object with a null `id` is rejected.
- The parser does not import the ledger module (enforced by a test).
- The validator is read-only and has no tools.
- The validator runs on a different model from the parser.

### Member 2 (frontend)

- The frontend displays a draft received from the server without
  modifying it.
- The frontend sends back a signature over the canonical hash of the
  exact draft bytes it received.
- The frontend never constructs a draft locally.
- The canonical JavaScript implementation
  (`frontend/js/canonical.js`) passes all vectors in
  `contract/test_vectors.json`.

### Member 3 (backend services)

- A draft with a non-empty `unresolved` list and an otherwise valid
  signature is rejected with `unresolved_fields` and the ledger is
  untouched.
- A signature for draft A is rejected for draft B (`hash_mismatch`).
- An expired draft is rejected with `expired`.
- A replayed `idempotency_key` is rejected with `replayed`.
- The policy engine is pure deterministic code — no LLM anywhere.
- Execution is idempotent on a caller-supplied key.
- The audit log is hash-chained; `verify_chain()` detects tampering.
- The canonical Python implementation (`backend/canonical.py`) passes
  all vectors in `contract/test_vectors.json`.

---

## 8. Open questions

These are ambiguous points I noticed. They need a decision from the
team, not a silent assumption:

1. **Signature algorithm.** The contract says "cryptographic signature"
   but does not specify the algorithm. WebAuthn typically uses
   ES256 or RS256. Which one? This affects the `/api/webauthn/*`
   endpoints and the `signature_invalid` verification path.

2. **`idempotency_key` scope.** Is the key unique per user, per
   session, or globally? If user A and user B both submit the same
   key, should the second be rejected as `replayed`? The contract
   says "caller-supplied" but does not define the scope.

3. **Draft expiry window.** The fixtures use a 5-minute window
   (`expires_at` is 5 minutes after `created_at`). Is this the
   intended policy, or just a fixture convention? The policy engine
   needs a concrete number.

4. **`multi_step.json` semantics.** The transcript describes two
   steps (transfer, then buy shares with half), but the fixture
   represents only the first step (the transfer). How are multi-step
   drafts modelled — as a sequence of separate drafts, or as a single
   draft with sub-steps? The schema does not currently support
   sub-steps.

5. **`confidence` exclusion from the hash.** `confidence` is excluded
   from the canonical hash because it is a float and is diagnostic
   only. But if the parser re-runs and produces a different confidence
   for the same draft, the hash stays the same. Is this intended?
   (I believe yes, since confidence is not shown to the user, but it
   should be confirmed.)

6. **`payee_candidates` after clarification.** When the user selects
   a candidate and `/api/clarify` returns an updated draft, should
   `payee_candidates` be removed (set to null/absent) or kept for
   audit? The schema allows it to be absent, but the contract does
   not say.

7. **Validator verdict values.** The examples use `"pass"` and the
   reason-code table mentions `"frozen"`. What are all the possible
   verdict values? Is there a `"warn"` state that still allows
   execution?

8. **Policy `decision` values.** The examples use `"allow"` and
   `"hold"`. Is `"block"` different from `"hold"`? What are all the
   possible policy decisions, and which map to which reason codes?
