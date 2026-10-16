# Member 2 submission verification — 16 October 2026 (Singapore)

## Revision and configuration safety

- Fetched main: `1b4f34399a8467ad05f0fca846edde543cfe8bda`.
- This submission branch starts from that revision. WebAuthn PR #32 remains open and unmerged; its branch was not rewritten.
- Original worktrees, the WebAuthn checkpoint/cleanup and untracked Playwright artifacts were preserved.
- The private repository-root configuration was initially absent. After the owner placed it, it was verified ignored, untracked and owner-only (`600`). All four required parser/validator key and model-label settings were present, with distinct keys and model labels. No secret values are included here or in screenshots.
- The live server loaded the original private file directly into memory without copying it into this worktree. Both real LLM backends were selected before importing the gateway. The no-client, fake-client and unvalidated-bypass switches were removed. Model calls were given enough timeout allowance for the reported latency. Access logging was suppressed.
- Old servers on ports 8877 and 8879 were left untouched. The current live LLM rehearsal is on `http://localhost:8883/`; use `lsof -nP -iTCP:8883 -sTCP:LISTEN` to identify the actual process before stopping anything. No stop command should target an assumed PID.
- The rehearsal uses no bank. No `/api/execute` request was made in this preparation pass.

## First real LLM request

Submitted exactly: `Send fifty dollars to John Smith.`

| Field | Observed result |
|---|---|
| HTTP | 200 |
| Observed response time | approximately 20.8 seconds |
| `intents_detected` | 1 |
| Item kind | `draft` |
| Payee | John Smith, `payee-001`, `****1234` |
| Amount | `50.00 SGD` |
| Validator | `pass` |
| Validator reason | `The draft matches the transcript.` |
| Discrepancies | Empty |
| Policy | `allow` |
| Browser | Same amount, payee, transcript, validator and policy; badge `Draft received`; no page errors |

Health reported parser `method` (the bound LLM parser), validator `LlmValidator`, client `AdpChatClient`, clarification backend `model`, and bank `none`. This is real model-backed draft validation, not a stub or an unvalidated development pass. It is not a transfer or a test of execution authorization.

**Copy-ready Member 1 message:**

> The real AI configuration works on latest main (`1b4f343`). “Send fifty dollars to John Smith.” returned HTTP 200 in about 21 seconds: John Smith ****1234, 50.00 SGD, validator pass with “The draft matches the transcript.”, and policy allow. Chromium displayed the same result. The corrected ambiguous-John flow also resolved John Lee to a new 50.00 SGD draft with real validator pass and policy allow. No transaction was executed.

## Real corrected-message and clarification check

The browser input was first set to “Send fifteen dollars to John.” and deliberately corrected to “Send fifty dollars to John.” before a touch on Send. This checks manual editing through the real LLM API; it is not a claim that headless speech recognition produced either phrase.

- The real server offered John Doe `****4521` and John Lee `****8892`.
- A touch selected John Lee without submitting clarification.
- A separate touch on Confirm selection called the real clarification endpoint.
- HTTP 200 returned a new draft ID with John Lee, `50.00 SGD`, and the exact corrected transcript `Send fifty dollars to John.`.
- The real validator returned `pass`, reason `The draft matches the transcript.`, and policy returned `allow`.
- The browser matched the response and had no page errors. Merged main's Sign/Execute remained disabled because passkey integration is still in PR #32.

## What is actually merged

| Feature | Main at the verified revision |
|---|---|
| Hold-to-talk, interim preview, final editable transcript | Present |
| Manual correction and manual Send | Present |
| Late-final-result preservation, edit and stale-callback protection | Present |
| Server-offered payee buttons and explicit Confirm selection | Present |
| New draft/nonce after payee clarification | Present and exercised through the real LLM server |
| Explicit server-amount read-back, amount repeated twice | Present; native synthesis invoked in Chromium |
| 380×216 cover | Present |
| Generated candidate question, Hear question, Answer by voice | **Not merged**; separate local `feature/member2-dynamic-ambiguity` worktree |
| Register → Sign → separate Confirm execution | **Not merged**; security-review PR #32 |
| Multi-intent | Rejected before phase 5; do not portray as implemented |

The current ambiguity card asks “Which payee did you mean?” and renders server-provided names and masked accounts. That is the verified merged flow. Do not show the richer local branch as though it is part of main.

## Focused checks in this preparation pass

- Pytest: **91 passed, 1 execution-related test deselected**. Files: `tests/test_fixture_directory_parity.py`, `tests/test_draft_contract.py`, `tests/test_frontend_compat.py`; selection `-k 'not executes'`.
- Existing standalone browser speech regression page: **5 passed**. Covers cumulative final segments, normal release, late finals after release/focus loss, manual-edit protection and stale callbacks.
- **12 mobile/touch checks passed** in Chromium with a 390×844 mobile viewport and actual CDP touch events. Covered no horizontal overflow, microphone cleanup, real John candidates, tap selection versus explicit confirmation, corrected transcript/new draft, native read-back invocation, disabled transaction actions and all four fixture previews.
- A follow-up check waited for each fixture's exact source ID, rather than merely seeing an old card, and confirmed disabled actions and no overflow for all four.
- Concrete defect found: main buttons measured 36–38px high. This PR adds only a 44px minimum height for buttons and candidate controls. Post-fix measurements were 44px; no other frontend behavior changed.
- Both production JavaScript files compiled with Chromium's JavaScript engine. Whitespace checks passed.
- Cover: PNG, **380×216**, **14,742 bytes**, under 10 MB.

### Limits

- Headless Chromium did not produce usable final speech from a physical microphone. Recognition accuracy, permission prompts and audible read-back still require a real-device rehearsal.
- Native synthesis was invoked, but headless testing cannot establish what a person actually hears. The five speech regressions use explicit test doubles for recognition events.
- No hardware Face ID/Touch ID or phone passkey check was performed. Main's authorization controls remain disabled while PR #32 awaits review.
- Historical PR #32 evidence is separate: 128 focused tests passed with 15 Node-dependent skips; 26 browser assertions used a virtual authenticator, deterministic validator and no bank. A test ledger's `executed` result is not real banking, LLM validation, or hardware biometric evidence.
- Replay and tampering are distinct: PR #32 rejects cross-draft key reuse and same signed draft/fresh key as `422 replayed`; a modified challenge is `422 hash_mismatch`, a modified signature is `422 signature_invalid`, and a client-supplied draft body is `422 malformed_request`.

## Public assets and missing uploads

The GitHub repository was verified public. Its configured homepage is absent and GitHub Pages is not enabled. The owner confirmed no hosted demo or uploaded video URL yet. The project URL is the repository; demo and video URLs remain pending. See `submission-copy.md` for the exact text, cover and screenshot capture checklist.
