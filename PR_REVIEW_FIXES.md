# Member 3 — the ten review items, in the order you listed them

Branch: `feature/member3-review-fixes` (based on `origin/main` @ `90dde2c`).
`CONTRACT.md` is the only shared file touched, and only in two places — see
"Needs your sign-off" before merging.

Every item has a test; every test was mutation-checked (breaking the fix makes
it fail). 856 pass, 0 fail.

## The list

| # | Item | Where | What changed |
|---|---|---|---|
| 1 | Free-text `/api/clarify` 500s — `build_api()` never passed `client=` | `server.py` | New `_chat_client()`: builds the ADP client from the **parser's** AppKey (answer-extraction is parser-side work, not validator-side). No keys → `None`, and the endpoint stays a loud 500 rather than inventing an answer. `DCTA_FAKE_CLIENT=1` opts into the scripted client; `DCTA_NO_CLIENT=1` forces none. `/api/health` now reports `client`. |
| 2 | Empty `draft_hash` was trusted | `message.py` `_bind_verdict` | A verdict with an empty or missing `draft_hash` is now forced to `freeze` with a reason, same as a mismatched one. Previously `if bound and bound != digest` — an empty hash skipped the check entirely, so a validator could hand back an unattributable `pass`. |
| 3 | Challenge issued without a verdict | `credential.py` | The gate was `verdict is not None and verdict != "pass"` — a draft with **no** recorded verdict got a challenge that check 11 was always going to refuse. Now `verdict != "pass"`, with a distinct message for "no verdict recorded". |
| 4 | 500s had no `reason_code` | `errors.py`, `server.py`, `reason_codes.py` | New `error_response()` builds `{"result": "error", "reason_code": "internal_error", "message": ..., "bug": ...}`, and the three handlers use it. Also added an `Exception` handler so the ones we did not predict carry the shape too (type name in `message`, so it stays debuggable). `result` is `"error"`, not `"rejected"`: a 500 is our failure, not a judgement on the request. |
| 5 | Payee form not single-use | `message.py` | New `self.clarified` map (draft id → resolved draft id). A second pick on the same draft is `invalid_clarification_answer` and names the draft to use instead. **A refused answer does not consume it** — otherwise a bad click locks the user out. |
| 6 | `self.pending` never pruned | `message.py` | `_prune_pending()` runs at the top of `clarify()` and drops questions past `expires_at`. A still-live question survives the pass (there is a test for that, so pruning is not "forget everything"). |
| 7 | Non-string `draft_id` → 500 | `message.py`, `credential.py`, `execute.py` | `_require_strings()` in `/api/clarify`; the same guard in `/api/webauthn/challenge`; and `draft_id`/`credential_id`/`idempotency_key` in `/api/execute` check 1. All `malformed_request`. A dict is unhashable, which is why these were 500s. |
| 8 | Question form's audit row had no transcript | `message.py` | The `clarify` row now carries `transcript` alongside `answer` — CONTRACT.md §4 second form wants both stored separately. |
| 9 | Stale type hint at `message.py:151` | `message.py` | `Optional[Callable[[Mapping, str], Mapping]]` → `Optional[Any]`, with a note that `validate()` is the interface and `__call__` is deliberately refused. |
| 10 | Out-of-date comment at `reason_codes.py:31-35` | `reason_codes.py` | It said the two `/api/message` codes were "NOT yet in the §5 table". They have been since §8 q4 was decided. |

## Needs your sign-off (CONTRACT.md) — both approved

1. **§5, new row + a note:** 500s now carry `result`/`reason_code`/`message`
   like everything else, with one new code `internal_error`. The note explains
   why `result` is `"error"` rather than `"rejected"` — a client that branches
   on "rejected" would otherwise show the user an error that is not theirs.
   One code, not three: splitting our own failure modes into the shared table
   puts them in the client's branch logic. `bug` (`parser` / `wiring` /
   `validator` / `unhandled`) stays a non-contract extra for the log.
2. **§4 `POST /api/webauthn/challenge`, one blockquote:** it now also refuses
   `validator_frozen` (including "no verdict recorded") and `policy_blocked`.
   Worded as Member 1 asked — *"refuses anything other than a recorded pass,
   including no verdict"* — and stated as **the same condition as check 11**
   rather than a list of cases, so the two gates cannot drift apart.

## Deliberately not done

- **`_clarify_payee` is untouched.** You said you are making a one-line change
  there, so the single-use rule (item 5) and the type guard (item 7) live in
  the dispatcher: `clarify()` → `_clarify_once()` → `_clarify_payee()`. No edit
  to the function body, so your PR cannot collide with mine.
- SQLite, `idempotency_key` scope, the validator's `warn` verdict: unchanged,
  still open from the earlier audit.

## Follow-ups, after Member 1's review (second commit)

- **payee-102 is "John Lee".** The directory is the source of truth, so
  `backend/bank/bank.py` is aligned: payee-102 is now John Lee, and the comment
  says why the ambiguity is "which John", not "one person with two accounts".
  A new test asserts the seed names match `FixtureDirectory`, so the next time
  either side moves, the suite says so instead of the receipt silently naming a
  different person than the candidate the user picked.
  `fixtures/drafts/ambiguous_payee.json` said John Smith at the time; PR #24
  aligned it and `tests/test_fixture_directory_parity.py` now pins it, so the
  fixture, the directory, the stub and the bank all say John Lee.
- **The payee form's audit row now carries `transcript` too** (item 8, both
  forms). It is one added line inside `_clarify_payee`, as agreed.
- `internal_error` approved as a single code; no change needed.

## Verification

```
857 passed in 10.06s          # on origin/main, whole suite
```

Each fix was then broken on purpose and the suite re-run — every one fails when
its fix is removed, so none of these tests can pass vacuously:

```
1 client not passed          -> 2 failed
3 challenge verdict gate     -> 1 failed
4 500 reason_code            -> 1 failed
4 unhandled handler          -> 1 failed
5 single-use                 -> 1 failed
5 consume on success         -> 1 failed
6 prune pending              -> 1 failed
7 clarify type check         -> 1 failed
7 challenge type check       -> 1 failed
7 execute type check         -> 1 failed
8 audit transcript           -> 1 failed
2 empty hash trusted         -> 2 failed

bank name drifts back        -> 1 failed
bank: two Johns collapse     -> 2 failed
payee audit transcript       -> 1 failed
```

## Merge order

No conflicts expected with `feature/frontend-shell` — this branch touches no
frontend file. It is already based on `main` including PR #17 (the
`_clarify_payee` change), so that one is settled. If main moves again before
this merges, rebase rather than merge — the only shared file touched here is
`CONTRACT.md`, and both edits are in distinct paragraphs.
