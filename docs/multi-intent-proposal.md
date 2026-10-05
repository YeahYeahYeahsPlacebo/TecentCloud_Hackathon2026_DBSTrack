# Proposal: Multi-Intent Requests (Transaction Groups)

**Status:** Accepted in part. See Decisions below. This document
does not modify `CONTRACT.md`, `contract/draft.schema.json`,
`fixtures/`, `tests/`, `backend/`, or `frontend/`. It exists only to
frame a decision for the team.

**Scope:** Multi-intent user requests — a single user utterance that
describes two or more distinct transactions, where later transactions
may depend on earlier ones.

**Author:** Member 2 (frontend).

**Related:** `CONTRACT.md` §8 open question 4 (`multi_step.json`
semantics); `CONTEXT.md` hard constraints 1, 6, 10 and the core rule
("the generative AI is a generator of drafts, never an executor of
funds"); `fixtures/drafts/multi_step.json`.

---

## 0. Decisions (team review, agreed in part)

The team has agreed the following design directions. These are
**agreed directions, not completed implementations** — none of the
features below have been built yet. Phase 1 may return a list
containing a single item; full multi-intent group execution is
deferred to Phase 5, after the working single-intent demo.

**Item 1 — Modelling style (§8 item 1).** We accept separate
per-transaction drafts plus a group metadata object as the future
modelling approach. The existing draft schema
(`draft.schema.json`), the canonical hash, and the per-draft WebAuthn
approach remain unchanged. No sub-steps or nested intents are added
to the draft.

**Item 8 — Parser coverage ownership (§8 item 8).** Member 1 owns
parser coverage. The parser interface will return an items list plus
an intent count, with a test that compares them. This closes the
whole-request coverage gap (§5.2) at the parser level for v1.

**Item 11 — Validator behaviour (§8 item 11).** The validator remains
read-only and checks each draft individually. It does not reason
about the group as a whole in v1.

**Item 14 — Endpoint surface (§8 item 14).** Items/group information
will be returned inline from `/api/message` rather than through a
separate endpoint for v1. No new `GET /api/group/{group_id}` or
`POST /api/group/clarify` endpoint in Phase 1.

**Item 16 — Fixture migration (§8 item 16).** Use option (b) now:
correct the `multi_step.json` transcript to describe one intent
honestly, so the fixture no longer claims to represent two actions
while only modelling one. Revisit option (a) — splitting into a
group fixture under `fixtures/groups/` — in Phase 5 when the group
model is actually built.

**Deferred to Phase 5 (after the working single-intent demo):**

All other group modelling, including but not limited to:
- Group storage and the `transaction_group` schema/object (§3.1)
- Inter-step dependencies (`depends_on`) and ordering enforcement
  (§6)
- Failure rules (`on_failure`, `on_earlier_failure`, `pause_and_ask`,
  `abort_group`, `continue`, `mark_partial`) (§4)
- Combined balance checks and the pre-execution check (§4.1)
- New gateway enforcement and reason codes
  (`group_paused_pre_execution`, `dependency_not_satisfied`,
  `earlier_step_failed`) (§6.1)
- Dependent-amount lifecycle (§5.3)
- Group-level signing (§7)

These remain design directions in this proposal. They are not
implemented, and no contract, schema, fixture, test, backend, or
frontend changes for them have been made. Member 1 will handle the
parser, contract, and fixture changes in their own work.

---

## 1. The current gap

`fixtures/drafts/multi_step.json` carries this transcript:

> "Transfer one thousand dollars to Jane, then buy five hundred dollars
> of ES3 shares at market price with half of it."

The fixture, however, represents only the first action — a transfer:

- `intent_type`: `transfer`
- `amount.value`: `"1000.00"`
- `payee.display_name`: `"Jane Tan"`
- `unresolved`: `[]`

The second action ("buy five hundred dollars of ES3 shares at market
price with half of it") is **dropped entirely**. There is no
`equity_purchase` draft, no `ticker`, no `notional_amount`, no
`order_type`.

What is and is not ambiguous here needs care:

- **The amount is not automatically unknown.** The user explicitly
  says "buy five hundred dollars", so `amount.value: "500.00"` is a
  stated figure, not a guess. "Half of it" may refer to the previously
  mentioned SGD 1,000, but that does not by itself make the SGD 500
  amount unresolved — it makes the *relationship* between the two
  transactions unclear.
- **The source of funds and the intended relationship are unclear.**
  The transfer credits Jane's account, not the user's. It is not
  stated whether the share purchase should debit the same source
  account (`acct-001-2345`), whether "half of it" is meant to tie the
  purchase to the transfer's proceeds (which would be unusual, since
  the money left for Jane), or whether these are simply two
  independent expenditures uttered in one breath. The system should
  ask a **targeted question** about the share purchase's source
  account and about whether the user intends the two transactions to
  be linked or separate.
- **Do not silently assume the share purchase is funded by the money
  sent to Jane.** That is one reading, but it is not the only one, and
  guessing it would be exactly the kind of silent resolution hard
  constraint 6 forbids.

A signable second draft cannot exist until that clarification is
given. The genuinely outcome-dependent case — "use whatever is left
after the first payment" — is different and is handled separately in
§5.3: there the amount truly is not knowable at parse time and must
stay unresolved until the earlier step's actual result is known, then
requires a new draft and a fresh authorisation.

This is inconsistent with the contract in two ways:

1. **Drops a user-expressed intent silently.** The parser captured only
   one of two actions the user described. `CONTEXT.md` hard constraint 6
   says: *"When uncertain, the parser puts the field in `unresolved` and
   asks; it never guesses."* The current fixture does the opposite: it
   silently omits the second action rather than surfacing the
   uncertainty. An empty `unresolved` here is therefore misleading — it
   asserts "nothing is unresolved" while half the user's request is
   unmodelled.

2. **No way to express order or dependency.** Even if the parser
   produced two drafts, the schema has no concept of "B runs only after
   A succeeds" or "B depends on A". Each draft today is an independent
   unit: independent `id`, independent `nonce`, independent
   `expires_at`, independent signature, independent `idempotency_key`.
   There is no link between them, and the gateway has no record that
   one draft must (or must not) precede another.

`CONTRACT.md` §8 open question 4 already flags this and lists two
candidate modelling styles — a sequence of separate drafts, or a single
draft with sub-steps — and notes that the schema does not currently
support sub-steps. This proposal picks the first style and adds the
missing concept: a **transaction group** that ties the drafts together
without merging them.

---

## 2. Design principle

Keep the existing draft as the atomic, signable, executable unit. Do
**not** add sub-steps, nested intents, or composite fields to
`draft.schema.json`. A draft continues to describe exactly one
transaction (`transfer`, `bill_payment`, or `equity_purchase`), with
exactly one signature over its own canonical hash.

Introduce a **separate, group-level structure** that:

- lists the drafts in execution order,
- records dependencies between them,
- records what happens when an earlier draft fails,
- is itself **not signable and not executable**. It is metadata that
  the frontend displays and that the gateway reads to sequence
  executions.

This proposal is designed to fit inside the existing constraints. The
following are the relevant ones from `CONTEXT.md`; nothing here claims
to already be enforced where the current implementation does not:

- The **core rule** says the generative AI drafts, it never executes
  funds, and has zero write access to the ledger. The group structure
  is metadata only; it does not move money. Money still moves only
  through the gateway.
- Hard constraint **1** says parsing, validation and policy run
  server-side only, and the client never builds the draft. The group
  is produced server-side by the parser; the frontend only displays it
  and drives per-step signing. No client-built drafts.
- Hard constraint **6** says when uncertain the parser puts the field
  in `unresolved` and asks; it never guesses. The multi-intent analogue
  is: when the parser cannot tie an uttered intent to a concrete draft
  (or cannot resolve a field within one), it must surface that, not
  silently drop it. See §1 and §5.
- Hard constraint **10** says a draft with a non-empty `unresolved`
  list can never be signed or executed, and the gateway rejects it
  independent of any frontend check. This stays per-draft under this
  proposal.
- Hard constraint **3** says a signature for draft A must be rejected
  for draft B. Per-step signing (§5) keeps one signature bound to one
  draft's canonical hash. Group-level signing (§7) is explicitly
  deferred and would require a new canonical object and challenge type.

What this proposal adds on top of the current implementation is the
group itself and the server-side enforcement of ordering and
dependencies. Those are **not** present today and are called out as new
work in §6. The hashes, the WebAuthn flow, and the per-draft signature
binding are reused unchanged.

---

## 3. Proposed shape (illustrative, not normative)

This is a sketch to make the discussion concrete. It is **not** a
schema. If the team agrees, the real schema would live in
`contract/transaction_group.schema.json` (shared, requires agreement
per `CONTRACT.md` §6) and the Pydantic model in
`backend/models/transaction_group.py` (Member 3's lane, since it is
backend storage).

### 3.1 A transaction group

To illustrate a **realistic, unambiguous dependency**, consider:

> "Transfer SGD 100 to Mom, then pay my SGD 30 electricity bill from
> the same account."

The account balance is only SGD 70. Both transactions name a concrete
amount and the same source account, so neither amount is unresolved at
parse time. The problem is that the **combined request cannot be
satisfied** as stated: SGD 100 + SGD 30 = SGD 130 against a SGD 70
balance. (This example is invented for the proposal; it is not a
fixture.)

```json
{
  "group_id": "tg-mom-bill-0001",
  "created_at": "2026-10-03T09:00:00Z",
  "expires_at": "2026-10-03T09:05:00Z",
  "user_id": "user-001",
  "transcript": "Transfer SGD 100 to Mom, then pay my SGD 30 electricity bill from the same account.",
  "steps": [
    {
      "order": 1,
      "draft_id": "55555555-5555-5555-5555-555555555555",
      "intent_type": "transfer",
      "depends_on": [],
      "on_failure": "pause_and_ask"
    },
    {
      "order": 2,
      "draft_id": "66666666-6666-6666-6666-666666666666",
      "intent_type": "bill_payment",
      "depends_on": [1],
      "on_failure": "pause_and_ask"
    }
  ],
  "on_earlier_failure": "pause_and_ask",
  "pre_execution_check": {
    "status": "insufficient_balance_for_group",
    "combined_amount": "130.00",
    "available_balance": "70.00",
    "currency": "SGD",
    "message": "The two payments total SGD 130.00 but the account has SGD 70.00. The first transfer cannot be completed and both payments cannot be made as requested.",
    "options_presented": [
      "change_funding_account",
      "revise_an_amount",
      "choose_one_transaction"
    ]
  },
  "status": "paused_pre_execution"
}
```

Two things to call out about the `pre_execution_check` block:

- It is run by the **server**, before any step is signed or executed.
  It must explain that the first transfer cannot be completed and
  that both payments cannot be made as requested, and it must pause
  both transactions. It must **not** automatically reduce the SGD 100
  transfer, and it must **not** execute the SGD 30 bill while the
  user is deciding. The frontend can render the options, but the
  pause and the balance check are server-side (§6).
- `options_presented` is illustrative. The real set and their
  semantics are a team decision (§8). The key invariant is that the
  server holds the group in `paused_pre_execution` until the user
  picks one; no draft in the group is signable in that state.
```

Notes on the fields (all provisional):

- `group_id` — server-generated, unique per group. Not a draft id; not
  signable.
- `created_at` / `expires_at` — group-level window. The group expires
  when the **latest** step's draft would expire, or at its own
  `expires_at`, whichever is earlier. (Decision needed — see §8.)
- `transcript` — the full user utterance, kept here so the group has
  its own auditable record and so the whole-request coverage check
  (§5) can run against it. Each step's draft keeps its own
  `transcript` too (the slice the parser used for that step). The
  validator continues to compare each draft's transcript slice to that
  draft.
- `steps[]` — ordered list. Each step references a stored `draft_id`
  and never embeds a draft body (mirrors `/api/execute`'s rule: ids,
  not bodies).
- `depends_on` — list of `order` values that must reach a terminal
  `executed` state before this step may be submitted to the gateway.
  Empty for the first step. **This is a security rule, not a UX hint:
  the server must store and enforce it (§6).**
- `on_failure` (per step) and `on_earlier_failure` (group default) —
  what happens when a step fails, or when the pre-execution check
  fails. See §4.
- `pre_execution_check` — a server-side, pre-signing check over the
  group as a whole (combined balance, combined limits, etc.). When it
  fails, the group is held in `paused_pre_execution` and no step is
  signable. See §4.
- `status` — group-level rollup: `pending`, `in_progress`,
  `paused_pre_execution`, `completed`, `aborted`,
  `partially_completed`. (Decision needed on the exact set — see §8.)

### 3.2 Drafts for the Mom + electricity-bill example

Both drafts debit `acct-001-2345` (the account with only SGD 70):

```json
{
  "id": "55555555-5555-5555-5555-555555555555",
  "created_at": "2026-10-03T09:00:00Z",
  "expires_at": "2026-10-03T09:05:00Z",
  "nonce": "nonce-mom-bill-transfer-001",
  "intent_type": "transfer",
  "source_account": "acct-001-2345",
  "amount": { "value": "100.00", "currency": "SGD" },
  "confidence": 0.93,
  "unresolved": [],
  "transcript": "Transfer SGD 100 to Mom.",
  "payee": {
    "id": "payee-MOM-001",
    "display_name": "Mom",
    "masked_account": "****2222"
  }
}
```

```json
{
  "id": "66666666-6666-6666-6666-666666666666",
  "created_at": "2026-10-03T09:00:00Z",
  "expires_at": "2026-10-03T09:05:00Z",
  "nonce": "nonce-mom-bill-bill-002",
  "intent_type": "bill_payment",
  "source_account": "acct-001-2345",
  "amount": { "value": "30.00", "currency": "SGD" },
  "confidence": 0.91,
  "unresolved": [],
  "transcript": "Pay my SGD 30 electricity bill from the same account.",
  "payee": {
    "id": "payee-SP-001",
    "display_name": "SP Services Ltd",
    "masked_account": "****0001"
  }
}
```

Two things to call out:

- Each draft is independently valid against `draft.schema.json`. The
  group adds **no** new fields to either draft. `additionalProperties:
  false` on the draft schema is unchanged.
- The dependency is **not** modelled inside either draft. It lives
  only in the group's `depends_on: [1]` on step 2. The drafts
  themselves remain self-contained and re-signable in isolation; the
  ordering is a server-side rule on the group, not a field on a draft.

### 3.3 The `multi_step` example: second step stays blocked on source/relationship

The original `fixtures/drafts/multi_step.json` transcript is
**ambiguous** about the second step's source of funds and its
relationship to the first step:

> "Transfer one thousand dollars to Jane, then buy five hundred dollars
> of ES3 shares at market price with half of it."

As explained in §1, the amount "five hundred dollars" is stated by the
user and is **not** automatically unresolved. What is unresolved is
the source account for the share purchase and whether the two
transactions are linked or separate. Under this proposal the parser
must **not** guess a source account, and must **not** assume the share
purchase is funded by the money sent to Jane. The group would carry
step 2 with:

- `unresolved` containing `source_account` (the parser could not
  determine which account the share purchase should debit, or whether
  it is the same `acct-001-2345` as the transfer),
- **no** stored second draft yet, because a signable draft cannot
  exist with `source_account` unresolved, and
- a targeted clarification request surfaced to the user — e.g. "Which
  account should the ES3 share purchase debit, and should it go ahead
  even if the transfer to Jane fails?" — before any second draft is
  produced.

Only after the user clarifies the source account (and the
relationship, which determines the failure policy) can a concrete
step-2 draft be created, displayed, and signed. The genuinely
outcome-dependent amount case ("use whatever is left after the first
payment") is different and is covered in §5.3.

---

## 4. Failure semantics and pre-execution checks

This proposal distinguishes three failure points, because they need
different handling:

1. **Pre-execution check failure** — the server inspects the group as
   a whole, before any step is signed, and finds it cannot be
   satisfied as requested (e.g. combined amounts exceed the available
   balance). This is the Mom + electricity-bill case in §3.1.
2. **Step failure at execution** — a step's `/api/execute` returns
   anything other than `result: "executed"`. This is the runtime
   failure case.
3. **Outcome-dependent amounts** — a later step's amount cannot be
   known until an earlier step's actual result is known. This is the
   "use whatever is left" case, handled in §5.3.

### 4.1 Pre-execution check (case 1)

Before any step in the group is offered for signing, the server runs a
**pre-execution check** over the group as a whole: combined amounts
against available balance, combined amounts against aggregate limits,
and any other group-level invariant. If the check fails, the server:

- holds the group in `paused_pre_execution`,
- explains in plain terms what cannot be done — for the Mom + bill
  example, that the first transfer cannot be completed and that both
  payments cannot be made as requested,
- presents the user with choices (change the funding account, revise
  an amount, or choose one transaction), and
- **must not** automatically reduce the SGD 100 transfer and **must
  not** execute the SGD 30 bill while the user is deciding.

The frontend can render the choices, but the pause and the balance
check are server-side. The frontend cannot enforce balance checks as
the only security control — a direct `/api/execute` against either
draft must still be blocked while the group is
`paused_pre_execution` (§6).

### 4.2 "then" expresses order, not a failure policy

"Then" tells the system the order the user wants. It does **not**, by
itself, tell the system what the user wants if the first transaction
**fails**. The AI must not invent a failure preference. Concretely, for
the Mom + bill example, "then" means the bill comes after the
transfer; it does not mean "pay the bill even if the transfer fails".

Two sub-cases:

- **Pre-execution check passes, first transaction later fails
  unexpectedly.** The server pauses the following transaction and asks
  the user what to do. It does not silently continue and does not
  silently abort. The user's answer becomes a recorded instruction the
  server enforces for this group, subject to the usual balance,
  validation, policy, and authorisation checks on the second
  transaction.
- **User explicitly says to continue the second transaction regardless
  of the first.** That is a different, explicit instruction. The
  server must record it on the group (provisional field:
  `on_earlier_failure: "continue"`) and enforce it — but the second
  transaction still has to pass its own balance, validation, policy,
  and authorisation checks. "Continue regardless" does not waive
  those.

### 4.3 Step failure at execution (case 2)

When a step's `/api/execute` returns anything other than
`result: "executed"`, the group must decide what to do. Provisional
`on_failure` / `on_earlier_failure` values:

| value | Meaning |
|---|---|
| `pause_and_ask` | No later step is submitted. The server pauses the following step and asks the user what to do. Already-executed earlier steps are **not** rolled back. This is the default when the user has not stated a failure preference. |
| `abort_group` | No later step is submitted. The group's `status` becomes `aborted`. Use only when the user has explicitly said to abort on failure, or when policy forces it. |
| `continue` | Later steps are still submitted. The failed step's outcome is recorded on the group. Use only when the user has explicitly said to continue regardless (see §4.2). |
| `mark_partial` | Stop submitting later steps, but allow the user to resume the remaining steps in a new session. (Decision needed — see §8.) |

For the Mom + bill example the natural default is `on_failure:
"pause_and_ask"` and `on_earlier_failure: "pause_and_ask"`: if the
transfer to Mom fails or is rejected, the bill payment is paused and
the user is asked what to do. The `multi_step` example (§3.3) is not
assigned a failure policy here, because its step 2 cannot even be
drafted until the source account and relationship are clarified —
there is nothing yet to fail.

**Compensation / rollback is explicitly out of scope for v1.** "Pause"
and "abort" both mean "stop submitting later steps", not "reverse an
executed transfer". The audit log already records each executed step
independently and is hash-chained, so a partial group leaves a
complete, verifiable trail.

---

## 5. Signing model for v1: sign each draft separately

For the first version, the frontend signs **each step's draft
independently**, reusing the existing per-draft challenge, hash, and
WebAuthn flow. The flow below assumes the group has passed its
pre-execution check (§4.1) and every step has a concrete, resolvable
draft. A group in `paused_pre_execution` is not signable at all; a
step still blocked on clarification (like the `multi_step` second step
in §3.3) is not signable until clarification produces a real draft for
it.

1. The server's pre-execution check (§4.1) passes, and the user
   approves the group as a whole in the UI (the frontend renders all
   signable steps before signing starts, and surfaces any step that is
   still awaiting clarification).
2. For each step in order that has an empty `unresolved`:
   - The frontend calls `POST /api/webauthn/challenge` with that
     step's `draft_id`.
   - The frontend recomputes the draft hash with
     `frontend/js/canonical.js` and checks it against the returned
     `draft_hash`, exactly as today.
   - The frontend calls `navigator.credentials.get()` with
     `userVerification: "required"`, ES256.
   - The frontend calls `POST /api/execute` with that step's
     `draft_id`, `credential_id`, `idempotency_key`, and `assertion`.
   - If `result: "executed"`, proceed to the next step.
   - If `result: "rejected"`, apply the group's `on_failure` policy
     (§4.3). With the default `pause_and_ask`, the server pauses the
     following step and asks the user what to do; the frontend does
     not silently continue or silently abort.

### 5.1 What is reused unchanged, and what is new

**Reused unchanged** (the existing per-draft machinery):

- `draft.schema.json` — no new draft fields. Each step's draft is a
  plain `TransactionDraft`.
- `draftHashAsync(draft)` / `frontend/js/canonical.js` — the canonical
  hash and the test vectors are reused as-is. No new canonical object
  in v1.
- `POST /api/webauthn/challenge` — returns the hash of one stored
  draft. Called once per step.
- WebAuthn `navigator.credentials.get()` with `userVerification:
  "required"`, ES256. One assertion per step, bound to that step's
  draft hash.
- `POST /api/execute` — takes a `draft_id` plus an assertion, loads its
  own stored draft, verifies the assertion against that draft's hash,
  and executes. Called once per step. The existing 12-check order
  (`CONTRACT.md` §4) runs unchanged per step.

**New (not present today, and required for v1):**

- A stored transaction-group object (§3.1) with ordered `steps` and
  `depends_on`.
- **Server-side enforcement of `depends_on`** (§6). This is the part
  the previous draft of this proposal wrongly claimed needed no
  endpoint changes; it does.
- A **whole-request coverage check** (§5.2).
- Handling of **dependent amounts** that cannot be resolved until an
  earlier step's actual result is known (§5.3).

### 5.2 Whole-request coverage check

Per-draft validation alone cannot catch an omitted second intent. The
validator (`backend/validator/`, Member 1) compares each draft's
transcript *slice* to that draft — so if the parser only ever
produced one draft for a two-intent utterance, every per-draft
validator verdict can be `pass` and the second intent is still
silently absent.

To close that gap, the system needs a **coverage check over the full
original transcript**: every intent the parser detected in the full
utterance must correspond to either (a) a concrete draft in the
group, or (b) an explicit clarification request for a field that
blocked a draft. If an uttered intent has neither, the group is
rejected with a new reason (provisional name: `incomplete_coverage`)
before any step is signable.

This check is server-side (consistent with `CONTEXT.md` hard
constraint 1) and runs before the group is offered for signing. It is
**not** something the frontend can be trusted to enforce on its own,
for the same reason the `unresolved` check is not: a client can be
bypassed. The exact home for this check is a decision (§8) — it
plausibly belongs to the parser or validator (Member 1), since it is
reasoning over the transcript.

### 5.3 Dependent amounts

Some amounts cannot be known at parse time because they are defined
relative to the **actual result** of an earlier transaction — e.g.
"buy shares with whatever is left after the first payment", or "invest
the remaining balance". These must stay unresolved until the earlier
step's executed result is known.

Rule (proposed):

- A step whose `amount` (or `notional_amount`, for equity) depends on
  an earlier step's result is produced with that field in `unresolved`,
  and therefore cannot be signed or executed (hard constraint 10).
- When the earlier step reaches a terminal `executed` state, the
  server computes the concrete amount, and produces a **new draft** for
  the dependent step with a new `id`, new `nonce`, and a resolved
  `amount`.
- The frontend must **display the new draft** and **collect a fresh
  signature** over its hash before calling `/api/execute` for it. The
  earlier signature does not cover the new amount and must not be
  reused. (This follows directly from hard constraint 3: a signature
  for draft A must be rejected for draft B.)

Concretely: the `multi_step` example is **not** a dependent-amount
case, because the user explicitly states "five hundred dollars". What
is unresolved there is the source account and the relationship (§3.3),
not the amount. A genuine dependent-amount case is a different
utterance, e.g. "Transfer SGD 1,000 to Jane, then buy ES3 shares with
**whatever is left** in the same account." There the share-purchase
amount is not knowable until the transfer has executed and the
remaining balance is known. The step-2 draft is not created with a
guessed amount; it is created only once the real derived amount is
known, and the user authorises that exact amount.

The `idempotency_key` per step remains caller-supplied and is now
**scoped to the step within the group** — the team still needs to
resolve `CONTRACT.md` §8 open question 2 (key scope: per user, per
session, or global) before this is fully specified.

---

## 6. Server-side enforcement (balance, dependencies, failure rules)

The frontend can show the user the choices — the pre-execution
explanation, the pause prompt, the options to change account or
revise an amount. What the frontend **cannot** do is enforce any of
the following as the only security control:

- **balance checks** (combined amounts vs. available balance),
- **transaction dependencies** (step 2 must not run before step 1 has
  succeeded), or
- **failure rules** (`pause_and_ask`, `abort_group`, `continue`).

Each of these must be enforced by the server, **including when someone
calls `/api/execute` directly** with a valid assertion for a draft that
belongs to a group. The frontend refusing to call `/api/execute` is a
UX convenience, not a security boundary — exactly as the frontend
refusing to sign a draft with unresolved fields is not what stops that
draft from executing (hard constraint 10 says the gateway rejects
regardless of any frontend check).

A previous draft of this proposal claimed multi-step support needs "no
changes to the existing challenge, signature, or execute endpoints".
That is only true for the per-draft crypto: the hash, the WebAuthn
challenge, and the signature binding are reused verbatim. It is **not**
true for balance, ordering, dependencies, and failure rules. Those are
security rules, and the server has to store and enforce them.

### 6.1 What the gateway must enforce, on top of the existing checks

Today `/api/execute` takes a `draft_id` and an assertion, loads its
stored draft, and runs the 12-check order in `CONTRACT.md` §4. Under
this proposal, when a draft belongs to a group, the gateway must
additionally be able to reject a direct call in at least these cases:

1. **Group is `paused_pre_execution`.** The pre-execution check (§4.1)
   failed and the user has not yet chosen a resolution. A direct
   `/api/execute` against any step's draft must be rejected. Provisional
   reason code: `group_paused_pre_execution`.
2. **Dependency not satisfied.** The step's `depends_on` lists an
   `order` that has not reached a terminal `executed` state in the
   server's own group record. A direct `/api/execute` for step 2 while
   step 1 has not succeeded must be rejected. Provisional reason code:
   `dependency_not_satisfied`.
3. **Failure rule violated.** The group's `on_failure` /
   `on_earlier_failure` is `pause_and_ask` or `abort_group`, an earlier
   step failed, and the user has not recorded a `continue` instruction.
   A direct `/api/execute` for the following step must be rejected.
   Provisional reason code: `earlier_step_failed`.

### 6.2 Position in the check order is a team decision

This proposal deliberately does **not** fix the exact position of the
new checks among the gateway's existing 12. The existing checks have a
defined order (`CONTRACT.md` §4) and a "first failure wins" rule, so
where these new checks sit changes which reason code a caller sees
first. That is a team decision (§8), not something the frontend
proposal should pin down. The only invariant this proposal asserts is
that the new checks must exist somewhere in the gateway path, so that
a direct call cannot bypass them.

### 6.3 What this requires that does not exist today

- The group must be **stored server-side** and referenceable from a
  stored draft (or the gateway must be able to look up "which group,
  which step, which dependencies, what status" from a `draft_id`).
- The gateway must record each step's terminal state (`executed` /
  `rejected` / `expired`) in the group record, atomically with the
  step's execution, so a later call sees a consistent state. This
  touches the gateway (Member 3) and the audit log (the audit row for
  step 1 is what proves step 1 executed; the group record should
  reference that audit row, not duplicate it).
- New reason codes (`group_paused_pre_execution`,
  `dependency_not_satisfied`, `earlier_step_failed` above) are added to
  the rejection table in `CONTRACT.md` §5. These are `CONTRACT.md`
  changes and require team agreement.

### 6.4 What this does **not** require

- No change to the per-draft canonical hash or to
  `frontend/js/canonical.js`.
- No change to the WebAuthn challenge shape or to
  `navigator.credentials.get()` usage. One assertion per step, bound
  to that step's draft hash, unchanged.
- No change to the existing 12 gateway checks themselves. The new
  checks are **additional**, not a modification of checks 1–12; only
  their ordering relative to the existing 12 is undecided (§6.2).

---

## 7. Alternative: group-level signing (deferred)

A second, more ergonomic model would let the user sign the **entire
group** with one WebAuthn assertion. This is listed as an alternative
only; it is **not** recommended for v1 because it requires design work
the team has not done, and it touches the signature binding rule
(`CONTEXT.md` hard constraint 3) which we should not relax lightly.

Sketch of what it would require:

- A canonical serialisation of the **group** (sorted list of step
  draft hashes, in order, plus the group's own fields). This is a new
  canonical object — `contract/CANONICAL_HASH.md` would need a second
  rule.
- A new challenge type whose challenge is the hash of that group
  canonical object, not of a single draft.
- A gateway path that verifies one assertion against the group hash
  and then executes each step's stored draft in order, re-checking
  each stored draft's hash against the value pinned in the group.
- New reason codes for group-level failures (e.g. `group_hash_mismatch`,
  `step_unresolved`, `dependency_failed`).
- A decision on whether a group-level signature still requires each
  step's `unresolved` to be empty (almost certainly yes), and whether
  step drafts may be individually re-signed after a group signature
  exists (probably no).

Because each step is a real money movement, per-step signing gives
stronger user intent evidence and a simpler audit story. Group-level
signing trades one assertion for lower friction, at the cost of new
canonical, challenge, and verification machinery. **Recommendation:
ship per-step signing first; revisit group-level signing only if the
UX cost of N assertions proves unacceptable.**

---

## 8. Decisions the team must make

This proposal deliberately does not answer the following. Each is a
team decision, not a frontend assumption.

1. **Modelling style.** Confirm "sequence of independent drafts +
   a group metadata object" over "single draft with sub-steps". (The
   latter would force `draft.schema.json` to grow nested intents and
   break `additionalProperties: false` + the one-signature-per-draft
   invariant.)

2. **Who owns the group model.** The group is server-stored metadata
   that the gateway reads. Per `CONTRACT.md` §6, storage and execution
   belong to Member 3. Member 1 (parser) would produce the group when
   it detects multiple intents in one transcript. Member 2 (frontend)
   only displays it and drives per-step signing. Confirm this split.

3. **Failure and pause semantics.** §4 introduces three failure points
   (pre-execution check, step failure, outcome-dependent amounts) and
   a default `pause_and_ask` policy. Decide the exact set of
   `on_failure` / `on_earlier_failure` values, the exact set of group
   `status` values (including `paused_pre_execution`), and whether
   `mark_partial` / resume-across-sessions is in v1 or out. Decide
   when `pause_and_ask` is allowed to transition to `abort_group`
   (e.g. on a timeout) without user input.

4. **Compensation / rollback.** Confirm that v1 does **not** attempt
   to reverse already-executed earlier steps when a later step fails.
   If compensation is ever in scope, it is a ledger concern (Member 3)
   and a regulatory question, not a frontend one.

5. **Expiry window.** Does the group have its own `expires_at`
   independent of its step drafts', or is it derived (e.g. the latest
   step's `expires_at`)? This interacts with `CONTRACT.md` §8 open
   question 3 (draft expiry window) and needs a concrete number.

6. **`idempotency_key` scope.** Resolve `CONTRACT.md` §8 open question
   2 before group execution is fully specified. Per-step keys within a
   group need an unambiguous scope.

7. **`unresolved` at group level.** If step 2's payee (or source
   account, or amount) is ambiguous, the group must surface that step 2
   is blocked on clarification before step 1 can be signed (or after,
   depending on UX). Decide whether `/api/clarify` is called per-step
   or whether the group exposes a "steps needing clarification" view.

8. **Whole-request coverage check ownership.** The coverage check in
   §5.2 reasons over the full transcript to detect an omitted intent.
   Decide which lane owns it: most likely Member 1 (parser or
   validator), since it is transcript reasoning, but it could be a new
   shared contract check. Decide the reason code name
   (`incomplete_coverage` is provisional) and whether it is a
   `/api/message`-time rejection or a group-level rejection.

9. **Dependent-amount lifecycle.** §5.3 says a dependent amount
   (genuinely outcome-dependent, like "whatever is left") stays
   unresolved and produces a new draft once the earlier step's result
   is known. Decide: does the new draft replace the blocked step in the
   same group, or start a new group? Does the new draft's `transcript`
   re-quote the original clause, or quote the prior step's result? Who
   computes the derived amount — the parser, the gateway, or a new
   service? Note this is distinct from the `multi_step` case, where
   the amount is stated and only the source/relationship is unclear
   (§3.3).

10. **Pre-execution check ownership and scope.** §4.1 introduces a
    server-side pre-execution check over the group as a whole
    (combined balance, combined limits). Decide which lane owns it:
    it reads account balances, so it likely involves the ledger
    (Member 3) or a new read-only balance service, but it is triggered
    by group metadata the parser (Member 1) produces. Decide what it
    covers — balance only, or also aggregate policy limits — and how
    it relates to the per-draft policy decision already in
    `CONTRACT.md` §4. Decide the reason code name
    (`group_paused_pre_execution` is provisional).

11. **Validator behaviour across steps.** The validator
    (`backend/validator/`, Member 1) is read-only and compares each
    draft's transcript slice to that draft. Confirm it does **not** need
    to reason about the group as a whole; the coverage check (§5.2) is
    a separate concern. If the validator must reason about the group,
    that is new behaviour for Member 1 to scope.

12. **Policy across steps.** Today policy is per-draft
    (`CONTRACT.md` §4 `/api/message` response includes a per-draft
    `policy.decision`). Decide whether policy also emits a group-level
    decision (e.g. "this group's combined value exceeds a daily
    aggregate limit even though each step is under the per-transaction
    limit"). This may interact with the `over_limit.json` fixture's
    scenario.

13. **Audit.** Confirm that each step's execution is audited exactly
    as a single-draft execution is today (one hash-chained row per
    step), and that the group itself is auditable as a single row
    recording the group id, the ordered step ids, and the final
    group status. Decide whether the group audit row references the
    per-step audit rows by hash (recommended) or duplicates their
    outcome.

14. **Endpoint surface.** Decide whether the group is returned inline
    by `POST /api/message` (alongside the per-step drafts) or via a
    new `GET /api/group/{group_id}` endpoint. The frontend prefers
    inline for the initial render, but a fetch endpoint is cleaner
    for resume-after-failure flows. Decide whether a new
    `POST /api/group/clarify` is needed or whether `/api/clarify` is
    extended to take a `group_id`.

15. **Gateway check ordering.** §6.1 lists three new server-side
    enforcements (`group_paused_pre_execution`,
    `dependency_not_satisfied`, `earlier_step_failed`) and §6.2
    deliberately leaves their position among the existing 12 checks
    open. Decide the exact position of each in the check order, the
    interaction with `unresolved_fields` (a step with unresolved
    fields is not even signable, so dependency ordering only matters
    for signable steps), and whether these are `CONTRACT.md` §5
    additions requiring team agreement.

16. **Fixture migration.** If the team accepts this proposal,
    `fixtures/drafts/multi_step.json` should either (a) be split into
    a draft fixture (the transfer) plus a group fixture under a new
    `fixtures/groups/` directory that records the blocked second step
    awaiting clarification, or (b) be marked as a single-intent
    fixture by editing the transcript. Option (a) is preferred because
    it preserves the multi-intent test case and demonstrates the
    blocked-step behaviour. Either way, this is a `fixtures/` change
    and requires team agreement per `CONTRACT.md` §6.

---

## 9. What this proposal does **not** change

- `CONTRACT.md` — untouched.
- `contract/draft.schema.json` — untouched. No new draft fields.
- `contract/CANONICAL_HASH.md` and `contract/test_vectors.json` —
  untouched. No new canonical object in v1 (per-step signing reuses
  the existing draft hash).
- `fixtures/` — untouched. The split of `multi_step.json` is a
  follow-up decision (§8 item 16), not part of this proposal.
- `tests/` — untouched.
- `backend/` — untouched. Parser, validator, gateway, ledger, audit,
  policy all untouched.
- `frontend/` — untouched. No frontend code in this proposal.

This document is the only file added on the `proposal/multi-intent`
branch.
