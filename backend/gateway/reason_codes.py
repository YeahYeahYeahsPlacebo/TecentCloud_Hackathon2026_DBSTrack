"""
Rejection reason codes (CONTRACT.md §5).

Every rejection carries exactly one code, and only the FIRST failing check's
code is returned. The order in which those codes may be produced is fixed by
CONTRACT.md §4 and is asserted by tests/test_gateway_skeleton.py against
``backend/gateway/execute.py``.
"""

from __future__ import annotations

# ── /api/execute ──
MALFORMED_REQUEST = "malformed_request"
MISSING_SIGNATURE = "missing_signature"
MALFORMED_SIGNATURE = "malformed_signature"
UNKNOWN_DRAFT = "unknown_draft"
HASH_MISMATCH = "hash_mismatch"
SIGNATURE_INVALID = "signature_invalid"
EXPIRED = "expired"
REPLAYED = "replayed"
UNRESOLVED_FIELDS = "unresolved_fields"
POLICY_BLOCKED = "policy_blocked"
VALIDATOR_FROZEN = "validator_frozen"
STEP_UP_REQUIRED = "step_up_required"
SCHEMA_INVALID = "schema_invalid"

# ── /api/clarify ──
INVALID_CLARIFICATION_ANSWER = "invalid_clarification_answer"

# ── /api/message ──
# CONTRACT.md §8 open question 4 is Resolved: before phase 5, a transcript
# that describes more than one transaction is refused with nothing drafted at
# all. Both codes below were added to the §5 table when that was decided, and
# are now part of the contract.
MULTI_INTENT_UNSUPPORTED = "multi_intent_unsupported"

# The same ParserCannotHandle covers "the stub does not recognise this
# sentence", which is a different failure from "two intents". It gets its own
# code so an eval table can tell the two apart.
PARSER_CANNOT_HANDLE = "parser_cannot_handle"

# ── 500s: our bug, not the user's request ──
# CONTRACT.md §5 fixes the shape of every error response — result,
# reason_code and message — and does not exempt the 500s. A 500 is never a
# rejection (nothing about the request was judged), so it carries
# result:"error" with the same three fields. One code covers all of them: the
# client cannot act on the difference, and splitting it would put our internal
# failure modes into the shared contract table. The ``bug`` tag in the body
# says which one it was.
INTERNAL_ERROR = "internal_error"

ALL_REASON_CODES: frozenset[str] = frozenset(
    {
        MALFORMED_REQUEST,
        MISSING_SIGNATURE,
        MALFORMED_SIGNATURE,
        UNKNOWN_DRAFT,
        HASH_MISMATCH,
        SIGNATURE_INVALID,
        EXPIRED,
        REPLAYED,
        UNRESOLVED_FIELDS,
        POLICY_BLOCKED,
        VALIDATOR_FROZEN,
        STEP_UP_REQUIRED,
        SCHEMA_INVALID,
        INVALID_CLARIFICATION_ANSWER,
        MULTI_INTENT_UNSUPPORTED,
        PARSER_CANNOT_HANDLE,
        INTERNAL_ERROR,
    }
)

# The reason codes /api/execute may return, in check order (CONTRACT.md §4).
# Index i holds the code produced by check i+1. Step 12 can yield either
# POLICY_BLOCKED or STEP_UP_REQUIRED; POLICY_BLOCKED stands in for the pair.
#
# This tuple is the contract. backend/gateway/execute.py must line up with it
# exactly — tests/test_gateway_skeleton.py asserts that it does.
EXECUTE_REASON_CODES: tuple[str, ...] = (
    MALFORMED_REQUEST,       # 1  request shape
    MISSING_SIGNATURE,       # 2  assertion present
    UNKNOWN_DRAFT,           # 3  draft_id names a stored draft
    SCHEMA_INVALID,          # 4  stored draft validates against the schema
    UNRESOLVED_FIELDS,       # 5  stored draft's unresolved is empty
    EXPIRED,                 # 6  now < expires_at
    MALFORMED_SIGNATURE,     # 7  assertion format
    HASH_MISMATCH,           # 8  challenge == our hash of our stored draft
    SIGNATURE_INVALID,       # 9  WebAuthn / ES256 verification
    REPLAYED,                # 10 idempotency_key not reused for another draft
    VALIDATOR_FROZEN,        # 11 validator verdict is "pass"
    POLICY_BLOCKED,          # 12 policy decision is "allow" (or step-up met)
)

# Step 12's second possible outcome; it shares step 12 with POLICY_BLOCKED.
STEP_12_REASON_CODES: frozenset[str] = frozenset({POLICY_BLOCKED, STEP_UP_REQUIRED})
