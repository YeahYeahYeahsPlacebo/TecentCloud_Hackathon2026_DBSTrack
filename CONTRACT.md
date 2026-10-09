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
2. The parser (Member 1) returns a **list of drafts and questions**,
   and the server stores each draft.
3. The validator (Member 1) compares the transcript to the draft.
4. The policy engine (Member 3) decides whether the draft may proceed.
5. The frontend (Member 2) displays the draft and collects a WebAuthn
   assertion whose challenge is the draft hash.
6. The gateway (Member 3) loads **its own stored copy** of the draft,
   verifies the assertion against it, and executes — or rejects with a
   reason code.

---

## 2. The draft

The draft is the core data contract. It is defined in:

- **JSON Schema:** [`contract/draft.schema.json`](contract/draft.schema.json)
- **Pydantic model:** [`backend/models/draft.py`](backend/models/draft.py)
- **Fixtures:** [`fixtures/drafts/`](fixtures/drafts/)

This document does not duplicate the schema. Always refer to the schema
file as the source of truth. Optional fields are **omitted, never
null**; both the schema and the model reject an explicit `null`.

### `unresolved`

A **required** array of field names the parser could not resolve. Items
must come from a fixed list (`intent_type`, `source_account`, `amount`,
`payee`, `ticker`, `quantity`, `notional_amount`, `order_type`) with no
duplicates. If non-empty, the draft **cannot be signed or executed**:
`/api/webauthn/challenge` refuses to issue a challenge for it, and the
gateway rejects it with `unresolved_fields` regardless of any frontend
check.

Example: `["payee"]` means the parser could not determine which payee
the user meant. The user must clarify before the draft can proceed.

**Only `payee` may appear in `unresolved` while a draft exists.** If the
parser cannot confidently resolve any other field — `intent_type`,
`source_account`, `amount`, `ticker`, `quantity`, `notional_amount`, or
`order_type` — it does not produce a draft at all. It asks the user a
direct clarifying question for that field first (outside the draft
flow), and only creates the draft once the answer resolves it. This
prevents a guessed or placeholder value from ever sitting in a field
that's simultaneously flagged as uncertain.

### `payee` and `payee_candidates`

- A `payee` and `"payee"` in `unresolved` can never appear together.
- When `"payee"` is in `unresolved`, `payee` is omitted and
  `payee_candidates` is **required with at least 2 entries**, each a
  fully populated `{id, display_name, masked_account}`. The frontend
  presents these for selection; `/api/clarify` resolves the choice.
- When `"payee"` is not in `unresolved`, `payee_candidates` must be
  absent or empty.

See [`fixtures/drafts/ambiguous_payee.json`](fixtures/drafts/ambiguous_payee.json)
for a working example.

### `nonce`

A server-generated random value, unique per draft. It is **anti-replay
only**: it makes two otherwise identical drafts (same user, same words,
same second) hash differently, so the WebAuthn challenge is never
predictable or reused. It is **not** an idempotency mechanism —
idempotency is `idempotency_key` on `/api/execute`, and nothing else.

### Frontend never builds a draft

The frontend (Member 2) **never constructs or edits a draft**. It
receives the draft from the server, displays it, recomputes its hash
with `frontend/js/canonical.js`, checks that hash against the challenge
the server issued, and only then asks the authenticator to sign. The
gateway never trusts any draft body the client sends (see §4).

---

## 3. Hashing

The signature is over the SHA-256 of the canonical JSON serialisation
of the draft. The canonicalisation rule is defined in:

- **Rule:** [`contract/CANONICAL_HASH.md`](contract/CANONICAL_HASH.md)
- **Python reference:** `backend/canonical.py`
- **JavaScript mirror:** `frontend/js/canonical.js`
- **Test vectors:** [`contract/test_vectors.json`](contract/test_vectors.json)

**The hashed object is the server-stored draft as the plain dict
produced by `TransactionDraft.model_dump(mode="json", exclude_none=True)`.**
It contains no `null`, no `Decimal` (money is already a string such as
`"50.00"`), no `datetime` (timestamps are serialised as
`YYYY-MM-DDTHH:MM:SSZ`, so `+00:00` and `Z` never diverge) and no
`Enum` objects. `draft_hash()` raises on anything else. The frontend
gets the same tree by `JSON.parse`-ing the server's response.

Every implementation — Python or JavaScript — must produce the hashes
listed in `contract/test_vectors.json`. The test suite
(`tests/test_canonical.py` and `frontend/js/canonical.test.js`) enforces
this. If an implementation disagrees with the vectors, it is wrong.

Key points (see `CANONICAL_HASH.md` for the full rule):

- Keys sorted by Unicode **code point** at every nesting level.
- No whitespace between tokens.
- Non-ASCII characters are not escaped (emitted as raw UTF-8).
- Control-character escapes use lowercase hex.
- Floats, null and lone UTF-16 surrogates are rejected, not formatted.
- `signature` and `confidence` are excluded from the hash.
- All other fields are included — nothing can be smuggled in unsigned.

---

## 4. API endpoints

All endpoints accept and return JSON. Draft examples below are built
from the real fixtures in `fixtures/drafts/`. All binary WebAuthn values
are **base64url without padding** (RFC 4648 §5).

### POST /api/message

User text in. Returns a list of items — each item is either a `draft`
(which the server has stored, with its validator verdict and policy
decision attached) or a `question` (a clarifying question for an
unresolved field). `unresolved` lives only inside `draft`.

Rules:

- `intents_detected` is the number of intents the parser found in the
  transcript. `len(items)` **must equal** `intents_detected`. A parser
  that detects more intents than it returns items for is a bug (an
  intent was silently dropped), and the parser tests enforce this.
- A `"question"` item means **no draft exists yet** for that intent. It
  is used for every unresolved field **except `payee`**. Only `payee`
  may be unresolved inside an existing draft (see §2). The `question_id`
  is used by `/api/clarify` to correlate the answer with the original
  transcript.
- The `validator` and `policy` objects are attached per draft **by the
  server**, not by the parser. The parser produces drafts and questions;
  the server runs the validator and policy engine over each draft and
  folds the results into the response.

**Request:**

```json
{
  "transcript": "Send fifty dollars to John Smith."
}
```

**Response (200):** (built from `clean_transfer.json`)

```json
{
  "intents_detected": 1,
  "items": [
    {
      "kind": "draft",
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
      "validator": {
        "verdict": "pass",
        "discrepancies": []
      },
      "policy": {
        "decision": "allow",
        "reason": null
      }
    }
  ]
}
```

**Response (200, ambiguous payee):** (built from `ambiguous_payee.json`)

```json
{
  "intents_detected": 1,
  "items": [
    {
      "kind": "draft",
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
      "validator": {
        "verdict": "pass",
        "discrepancies": []
      },
      "policy": {
        "decision": "hold",
        "reason": "unresolved_fields"
      }
    }
  ]
}
```

**Response (200, unresolved amount — question item):**

```json
{
  "intents_detected": 1,
  "items": [
    {
      "kind": "question",
      "question_id": "q-amount-001",
      "field": "amount",
      "question": "How much would you like to send?"
    }
  ]
}
```

### POST /api/clarify

A field name plus the user's answer. Returns a response shaped
exactly like `/api/message` — `intents_detected` and `items` — so the
client has one response shape across both endpoints.

Rules:

- `field` must be in the stored draft's `unresolved` list.
- For `field: "payee"`, `answer` **must equal the `id` of one of the
  stored draft's `payee_candidates`**. Any other value (an id that was
  not offered, free text, a display name) is rejected with
  `invalid_clarification_answer`, and the draft is unchanged.
- The clarified draft is stored as a **new draft with a new `id` and a
  new `nonce`**. The old draft id stays unresolved and can never be
  executed. Because the gateway loads drafts by id (see
  `/api/execute`), stored drafts are never modified in place.
- Once the payee is resolved, `payee_candidates` is omitted.

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
  "intents_detected": 1,
  "items": [
    {
      "kind": "draft",
      "draft": {
        "id": "77777777-7777-7777-7777-777777777777",
        "created_at": "2026-10-03T10:00:30Z",
        "expires_at": "2026-10-03T10:05:30Z",
        "nonce": "nonce-ambiguous-payee-002",
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
      "validator": {
        "verdict": "pass",
        "discrepancies": []
      },
      "policy": {
        "decision": "allow",
        "reason": null
      }
    }
  ]
}
```

**Response (422, answer not offered):**

```json
{
  "result": "rejected",
  "reason_code": "invalid_clarification_answer",
  "message": "answer 'payee-999' is not one of the offered payee_candidates."
}
```

#### Second form: answering a question item (proposed, needs team agreement)

> **Note:** The question-answer form below is **proposed and needs team
> agreement** before implementation. The existing payee form above is
> unchanged and remains the only committed form.

When the server returns a `"question"` item from `/api/message`, the
client answers it here. The request carries the `question_id` (from
the question item) and the user's `answer`. The server re-parses the
original transcript with the answer folded in, and returns a
response shaped exactly like `/api/message`.

Rules (proposed):

- `question_id` must match a question the server issued from a prior
  `/api/message` call. An unknown or already-used id is rejected with
  `invalid_clarification_answer` (see §5).
- A `question_id` is **single-use**: once the server has accepted an
  answer for it, it cannot be reused. It **expires with the draft
  window** (the 5-minute `expires_at` of the draft the question was
  derived from).
- The `answer` is **free text** and therefore **data, never
  instructions** (CONTEXT.md constraint 5). The server isolates it in
  a delimited block before passing it to the parser or validator.
- The server stores the **original transcript and the answer
  separately** and records both in the audit log.
- The validator receives **both** the original transcript and the
  answer — not a merged sentence — and compares the draft against the
  combination.
- The server re-parses the original transcript together with the
  answer. The response has the same shape as `/api/message` —
  `intents_detected` and `items` — so the client can treat it
  uniformly. A subsequent question item may appear if a different
  field is now unresolved.

**Request (proposed):**

```json
{
  "question_id": "q-amount-001",
  "answer": "one thousand dollars"
}
```

**Response (200, proposed):** — same shape as `/api/message`

```json
{
  "intents_detected": 1,
  "items": [
    {
      "kind": "draft",
      "draft": {
        "id": "44444444-4444-4444-4444-444444444444",
        "created_at": "2026-10-03T12:00:00Z",
        "expires_at": "2026-10-03T12:05:00Z",
        "nonce": "nonce-multi-step-001",
        "intent_type": "transfer",
        "source_account": "acct-001-2345",
        "amount": {
          "value": "1000.00",
          "currency": "SGD"
        },
        "confidence": 0.88,
        "unresolved": [],
        "transcript": "Transfer one thousand dollars to Jane.",
        "payee": {
          "id": "payee-002",
          "display_name": "Jane Tan",
          "masked_account": "****5678"
        }
      },
      "validator": {
        "verdict": "pass",
        "discrepancies": []
      },
      "policy": {
        "decision": "allow",
        "reason": null
      }
    }
  ]
}
```

### POST /api/webauthn/register (placeholder)

WebAuthn registration. To be implemented by Member 2 (frontend) and
Member 3 (gateway). The credential must be created with
`userVerification: "required"` and algorithm ES256 (COSE `-7`).

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
  "credential_id": "Y3JlZGVudGlhbC0wMDE"
}
```

### POST /api/webauthn/challenge

Returns the challenge for signing a stored draft. **There is no separate
random challenge: the challenge IS the draft hash** — base64url of the
raw 32-byte SHA-256 (not of the 64-character hex string). The draft's
`nonce` is what makes it unpredictable.

The server refuses (`unknown_draft`, `unresolved_fields`, `expired`) to
issue a challenge for a draft it did not store, that has unresolved
fields, or that has expired.

The frontend must recompute `draftHashAsync(draft)` over the draft it is
displaying and check that it matches `draft_hash` here before calling
`navigator.credentials.get()`. If they differ, it must not sign.

**Request:**

```json
{
  "draft_id": "11111111-1111-1111-1111-111111111111"
}
```

**Response (200):**

```json
{
  "draft_id": "11111111-1111-1111-1111-111111111111",
  "draft_hash": "b5fe8f0c9dd1b16cb1e9e716ded5a998ae3417c7c71bf187de5f73dc95c34777",
  "challenge": "tf6PDJ3RsWyx6ecW3tWpmK40F8fHG_GH3l9z3JXDR3c",
  "rp_id": "localhost",
  "user_verification": "required",
  "allow_credentials": ["Y3JlZGVudGlhbC0wMDE"]
}
```

### POST /api/execute

A **draft id** (never a draft body), the credential id, the WebAuthn
assertion, and an idempotency key. Returns the result or a rejection.

The gateway:

1. Loads **the draft it stored itself** under `draft_id`.
2. Computes its own hash of that stored copy (§3).
3. Checks that hash against the challenge inside the assertion.
4. Executes only that stored copy.

It **never executes a client-supplied draft body**. A request that
carries a `draft` field (or any field not listed below) is rejected with
`malformed_request` — the body is not used, merged or "swapped in".

**Request:**

```json
{
  "draft_id": "11111111-1111-1111-1111-111111111111",
  "credential_id": "Y3JlZGVudGlhbC0wMDE",
  "idempotency_key": "user-supplied-key-001",
  "assertion": {
    "credential_id": "Y3JlZGVudGlhbC0wMDE",
    "authenticator_data": "SZYN5YgOjGh0NBcPZHZgW4_krrmihjLHmVzzuoMdl2MFAAAAAQ",
    "client_data_json": "eyJ0eXBlIjoid2ViYXV0aG4uZ2V0IiwiY2hhbGxlbmdlIjoidGY2UERKM1JzV3l4NmVjVzN0V3BtSzQwRjhmSEdfR0gzbDl6M0pYRFIzYyIsIm9yaWdpbiI6Imh0dHA6Ly9sb2NhbGhvc3Q6ODAwMCJ9",
    "signature": "MEUCIQ...base64url-DER-ECDSA-signature..."
  }
}
```

`client_data_json` above decodes to
`{"type":"webauthn.get","challenge":"tf6PDJ3RsWyx6ecW3tWpmK40F8fHG_GH3l9z3JXDR3c","origin":"http://localhost:8000"}`.
`authenticator_data` is SHA-256(`localhost`) ‖ flags `0x05` (user
present + user verified) ‖ sign count 1. The `signature` value is
illustrative.

**Check order.** The gateway runs these checks in order and returns the
first failure, so every rejection has exactly one reason code:

| # | Check | Reason code on failure |
|---|---|---|
| 1 | Body has only `draft_id`, `credential_id`, `idempotency_key`, `assertion`; the first three are present | `malformed_request` |
| 2 | `assertion` is present | `missing_signature` |
| 3 | `draft_id` names a draft the server stored | `unknown_draft` |
| 4 | Stored draft validates against the schema | `schema_invalid` |
| 5 | Stored draft's `unresolved` is empty | `unresolved_fields` |
| 6 | Now is before the stored draft's `expires_at` | `expired` |
| 7 | Assertion has exactly its 4 fields, all valid base64url; `client_data_json` is JSON; `assertion.credential_id` equals `credential_id` | `malformed_signature` |
| 8 | `challenge` in `client_data_json` equals base64url of the server's recomputed hash of the stored draft | `hash_mismatch` |
| 9 | `type` is `webauthn.get`; `origin` is the expected origin; rpIdHash matches; user-verified flag set; credential is registered; ES256 signature over `authenticator_data ‖ SHA256(client_data_json)` verifies | `signature_invalid` |
| 10 | `idempotency_key` not previously used for a different draft (same key + same draft returns the original result) | `replayed` |
| 11 | Validator verdict for this draft hash is `pass` | `validator_frozen` |
| 12 | Policy decision for this draft hash is `allow` (or step-up satisfied) | `policy_blocked` / `step_up_required` |

`tests/test_execute_binding.py` checks this order against a stub store,
including that a request carrying a different draft body is rejected
and that a user-signed hash of a client-built draft fails with
`hash_mismatch`.

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
  "message": "The challenge in client_data_json does not equal the hash of the stored draft."
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

---

## 5. Error response shape and rejection reason codes

### Standard error response

Every rejection uses the same shape and HTTP status 422:

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
| `malformed_request` | The `/api/execute` body has a field other than `draft_id`, `credential_id`, `idempotency_key`, `assertion` (for example a `draft` body), or is missing one of the first three. |
| `missing_signature` | `/api/execute` is called without an `assertion`. |
| `malformed_signature` | The `assertion` does not have exactly `credential_id`, `authenticator_data`, `client_data_json`, `signature`; a value is not valid base64url; `client_data_json` is not JSON; or `assertion.credential_id` differs from the request's `credential_id`. |
| `unknown_draft` | `draft_id` does not name a draft the server stored. |
| `hash_mismatch` | The challenge embedded in `client_data_json` does not equal the server's recomputed draft hash (base64url of the SHA-256 of the stored draft's canonical bytes). |
| `signature_invalid` | The ECDSA (ES256) signature over `authenticator_data ‖ SHA256(client_data_json)` does not verify against the registered public key for `credential_id`. Also covers: unregistered credential, `type` not `webauthn.get`, wrong `origin`, wrong rpIdHash, or the user-verified (UV) flag not set. |
| `expired` | The stored draft's `expires_at` is not in the future. |
| `replayed` | The `idempotency_key` has already been used for a different draft. |
| `unresolved_fields` | The stored draft's `unresolved` array is non-empty. The gateway rejects this regardless of any frontend check. |
| `policy_blocked` | The policy engine determined the draft violates a rule (e.g. over per-transaction limit). |
| `validator_frozen` | The validator returned a verdict of `frozen` — the transcript does not match the draft. |
| `step_up_required` | Additional authentication (e.g. biometric or 2FA) is required for this transaction. |
| `schema_invalid` | The draft does not validate against `contract/draft.schema.json`. |
| `invalid_clarification_answer` | `/api/clarify` was called with an `answer` that is not the `id` of one of the stored draft's `payee_candidates`, with a `field` that is not in its `unresolved` list, or (in the proposed question form) with a `question_id` that is unknown or already used. |

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
  Schema and the Pydantic model, and the two agree on every negative case
  in `tests/test_draft_contract.py`.
- A draft with a float, integer or non-ASCII-digit amount is rejected.
- A draft with a missing required field (including `unresolved`) is rejected.
- A draft with an unknown `intent_type` is rejected.
- A transfer with no `payee` and empty `unresolved` is rejected.
- A payee object with a null `id` is rejected.
- The parser and validator do not import the ledger or gateway
  (`tests/test_import_boundaries.py`, enforced by AST inspection).
- `/api/clarify` rejects an answer that was not an offered candidate.
- The validator is read-only and has no tools.
- The validator runs on a different model from the parser.
- Every parser test asserts `len(items) == intents_detected`.
- A question item never contains a draft.
- A multi-intent request is declined, never partially drafted.

### Member 2 (frontend)

- The frontend displays a draft received from the server without
  modifying it.
- The frontend recomputes the hash of the displayed draft, checks it
  against the issued challenge, and refuses to sign on mismatch.
- The frontend sends a draft id plus a WebAuthn assertion, never a draft body.
- The frontend never constructs a draft locally.
- The frontend renders both item kinds (`"draft"` and `"question"`)
  from `/api/message` and `/api/clarify` responses.
- After any clarification, the frontend uses the **new draft id** from
  the response, not the old one.
- The canonical JavaScript implementation
  (`frontend/js/canonical.js`) passes all vectors in
  `contract/test_vectors.json`.

### Member 3 (backend services)

- `/api/message` calls `parse()` and attaches `validator` and `policy`
  to draft items only — never to question items.
- `/api/execute` loads its own stored draft and never executes a
  client-supplied body; a request carrying one is rejected
  (`malformed_request`) and the ledger is untouched.
- An unknown `draft_id` is rejected with `unknown_draft`.
- A draft with a non-empty `unresolved` list and an otherwise valid
  assertion is rejected with `unresolved_fields` and the ledger is
  untouched.
- An assertion whose challenge is the hash of draft A is rejected for
  stored draft B (`hash_mismatch`).
- A bad signature, wrong origin, wrong type, or missing UV flag is
  rejected with `signature_invalid`.
- An expired draft is rejected with `expired`.
- A replayed `idempotency_key` is rejected with `replayed`.
- The policy engine is pure deterministic code — no LLM anywhere.
- Execution is idempotent on a caller-supplied key.
- The audit log is hash-chained; `verify_chain()` detects tampering.
- The canonical Python implementation (`backend/canonical.py`) passes
  all vectors in `contract/test_vectors.json`.
- `tests/test_execute_binding.py` passes against the real gateway, not
  only the stub.

---

## 8. Open questions

These are ambiguous points I noticed. They need a decision from the
team, not a silent assumption:

1. ~~**Signature algorithm.**~~ **Resolved:** WebAuthn ES256 (ECDSA
   P-256 with SHA-256, COSE `-7`), user verification required. See §4.

2. **`idempotency_key` scope.** Is the key unique per user, per
   session, or globally? If user A and user B both submit the same
   key, should the second be rejected as `replayed`? The contract
   says "caller-supplied" but does not define the scope.

3. **Draft expiry window.** The fixtures use a 5-minute window
   (`expires_at` is 5 minutes after `created_at`). Is this the
   intended policy, or just a fixture convention? The policy engine
   needs a concrete number.

4. ~~**`multi_step.json` semantics.**~~ **Resolved:** multi-step
   transactions are modelled as **separate drafts plus a group** (not
   sub-steps within a single draft). The parser owns the coverage
   guarantee: `len(items)` in `/api/message` must equal
   `intents_detected` so no intent is silently dropped (see §4,
   `/api/message` rules). The validator stays **per-draft** — each
   draft's transcript is compared independently. Drafts and questions
   are returned **inline** in the `/api/message` `items` array. The
   group model — `depends_on`, failure policies, and a server-side
   balance/limit check — is **deferred to phase 5**. Until phase 5, a
   request describing more than one transaction is **declined** with a
   message asking the user to send one request at a time, and nothing
   is drafted. Reason: executing dependent steps independently, with
   nothing enforcing order, is the risk raised in the multi-intent
   proposal. The exact response for a declined request is an open
   decision (suggest 422 with `reason_code` `"multi_intent_unsupported"`);
   it is not yet added to the reason code table. Until then,
   `multi_step.json` represents a single transfer whose transcript
   matches the draft exactly.

5. ~~**`confidence` exclusion from the hash.**~~ **Moved** to §9
   (Known limitations).

6. ~~**`payee_candidates` after clarification.**~~ **Resolved:** it is
   omitted once the payee is resolved; the schema and model reject it
   otherwise. See §2.

7. **Validator verdict values.** The examples use `"pass"` and the
   reason-code table mentions `"frozen"`. What are all the possible
   verdict values? Is there a `"warn"` state that still allows
   execution?

8. **Policy `decision` values.** The examples use `"allow"` and
   `"hold"`. Is `"block"` different from `"hold"`? What are all the
   possible policy decisions, and which map to which reason codes?

---

## 9. Known limitations

Deliberately not fixed in this pass. Each is a decision, not an oversight.

- **Equity: quantity vs notional.** Both or neither may be set; exactly-one is not enforced.
- **Equity: notional vs amount.** `notional_amount` is not cross-checked against `amount.value`.
- **Equity: limit orders.** `order_type: "limit"` has no limit-price field.
- **Equity: fractional shares.** `quantity` uses the money pattern, so shares are always written with exactly two decimals (`"10.00"`).
- **Unknown or single-match payee.** `"payee"` in `unresolved` requires at least 2 candidates, so a payee with 0 or 1 matches cannot be expressed as a draft yet.
- **Unresolved required fields.** By the rule in §2, only `payee` may
  appear in `unresolved` while a draft exists, but the schema and
  model still accept other values (e.g. `amount`). The rule is
  enforced by the parser, not yet by the schema. Tightening the
  `unresolved` enum to `payee` only is a possible follow-up.
- **`confidence` trust boundary.** It is excluded from the hash, so a client copy can be altered freely. Policy and step-up must read only the server-stored value. This is documented, not tested.
- **`confidence` coercion.** Pydantic accepts `true` and `"0.5"`, which the schema rejects.
- **`id` format.** Pydantic does not check that `id` is a UUID. The schema's `format: uuid` is only enforced when a format checker is enabled, and the tests don't enable one.
- **`expires_at > created_at`.** Pydantic enforces it; JSON Schema cannot compare two fields. There is no maximum window yet (open question 3).
- **Unicode normalisation.** None: NFC `José` and NFD `José` hash differently, and confusable display names are not detected.
- **Lone surrogates in the model.** Pydantic does not reject them in strings; the canonicaliser does (`ValueError`). The gateway must map that to `schema_invalid`, not a 500.
- **Numbers in JavaScript.** JS cannot tell `50.0` from `50` and loses precision above 2^53. No hashed field is currently a number, so any future numeric field must be a string.
- **JS `__proto__` key.** A top-level `__proto__` key is dropped by `canonical.js`. The schema forbids extra keys, so valid drafts can't contain one.
- **Non-plain JS objects.** `canonical.js` serialises a `Date` or `Map` as `{}`. Only reachable without `JSON.parse`.
- **Deep nesting.** Python raises `RecursionError` at roughly 1000 levels; the draft schema is shallow.
- **Duplicate JSON keys.** Both parsers keep the last value; neither rejects duplicates.
- **Stubbed crypto in tests.** `tests/test_execute_binding.py` stubs ECDSA verification. Real WebAuthn verification is untested until the gateway exists.
- **Not yet specified.** WebAuthn `signCount` / clone detection, KYC, and binding validator and policy verdicts to the draft hash in storage.
- **Zero amounts.** `"0.00"` passes the money pattern; policy must reject it.
- **Multi-step group model (deferred to phase 5).** Multi-step
  transactions produce separate drafts returned inline in
  `/api/message` `items` (see §8 open question 4). The group model —
  `depends_on` between drafts, failure policies, and a server-side
  balance/limit check — is **deferred to phase 5**. Until phase 5, a
  request describing more than one transaction is declined with a
  message asking the user to send one request at a time, and nothing
  is drafted. Reason: executing dependent steps independently, with
  nothing enforcing order, is the risk raised in the multi-intent
  proposal. The exact response for a declined request is an open
  decision (suggest 422 with `reason_code`
  `"multi_intent_unsupported"`); it is not yet added to the reason
  code table.
