# DCTA recording script — 7 minutes 20 seconds

Prepared for 16 October 2026, Singapore time. Record the real merged-main UI plus the small mobile touch-target fix in this PR. PR #32 remains unmerged and requires Member 3's review. Do not execute a transaction during this recording. All banking services are mocked.

## Before recording

- Hide all terminals, private configuration, network headers, account settings, and unrelated tabs. Never open `.env` on camera. Use only demo identities and masked accounts.
- Open the current real-LLM gateway, not the old servers on ports 8877/8879. The verified local address is `http://localhost:8883/`; it is not a public demo URL.
- Check health privately: the parser is the LLM bound method, the validator is `LlmValidator`, and the client is `AdpChatClient`. An unconfigured or deterministic validator is a different demonstration. The actual request must still decide its verdict; never replace a freeze with a fabricated pass.
- Rehearse microphone permission and audible playback on the actual Mac/phone. Browser recognition can mishear words or be unavailable. Keep typed input as an honest fallback.
- Keep the clock running through network waits. The verified clear request took about 21 seconds; allow up to roughly 40 seconds or longer for parser and validator calls. If this take is slower, shorten narration elsewhere, not the truth of the result. Aim for 5–8 minutes, not an artificial response time.
- If models fail during recording, use the fail-closed narration below. Do not enable an unvalidated bypass. Do not use a local fixture as proof of AI validation.

## Timed actions and narration

### 0:00–0:40 — Overview and users

**Screen:** Title slide using `docs/cover.png`, then the 390px-wide browser UI. Keep the cursor away from disabled execution controls.

**Say:** “This is DCTA, the Direct Conversational Transaction Agent. We help people describe a payment by voice or text, review the exact transaction draft, and resolve ambiguity before any authorization. Voice interfaces are convenient, but a misheard amount or the wrong John could be costly. Our design makes those uncertainties visible. This hackathon prototype uses mocked banking services; it is not connected to real customer funds.”

### 0:40–1:20 — Trust boundaries and today's scope

**Screen:** Show the project architecture diagram, then return to the app. Do not show secrets or configuration file contents.

**Say:** “Member 1 owns the AI parser and validator. The parser turns the user's words into structured data; a separate validator checks that draft against the transcript. Member 2 owns this review interface and voice interaction. Member 3 owns the gateway, policy, ledger and audit checks. The AI never executes a payment. Today we will demonstrate the merged review flow. The passkey integration is in a separate security-review PR, so we will not present it as released.”

### 1:20–2:25 — Voice, final words and deliberate correction

**Screen/actions:** On the app, hold **Hold to talk** and say “Send fifteen dollars to John.” Release. Show any interim preview, then the final editable text. Correct the message to exactly **“Send fifty dollars to John.”** Do not press Send yet. If speech did not return a final result, say so and type the same starting message, then correct it. Do not claim an intentionally changed amount was a recognition error.

**Say:** “Interim words are a preview. Only final recognition results enter this editable message. Recognition can hear fifteen when the speaker intended fifty, and it can lose context. We do not guess the intended amount. Here I am deliberately changing fifteen to fifty before submitting. The user stays in control of the words. Nothing is submitted just because speech ended, and saying yes is not authorization. I can read and correct the final message before pressing Send myself.”

### 2:25–3:30 — Real AI request and ambiguous payee

**Screen/actions:** Press **Send** once. Let the loading state finish. Show the candidate card with John Doe `****4521` and John Lee `****8892`. Do not tap Confirm selection yet. Network waiting time belongs in this segment.

**Say:** “Now I explicitly send the corrected message. This request uses the real parser and validator services, not a fixture response. There are two offered Johns, so the frontend asks which payee I mean. Names and masked accounts come from the server's directory-backed candidates. The UI does not invent a recipient. Current main uses tap selection here; the richer generated question with Hear question and Answer by voice is local work and is not included in this released demonstration.”

**If the model returns a different response:** describe that exact response. Do not force the planned candidate result or hide a rejection.

### 3:30–4:25 — Select John Lee, then confirm the clarification

**Screen/actions:** Tap **John Lee — `****8892`**. Pause to show selection; no request should have been made by selecting alone. Click **Confirm selection**. Wait for the new draft. Show **John Lee**, **50.00 SGD**, and the corrected transcript. Open Technical details briefly to show the new draft ID/nonce, without pretending the opaque ID has user meaning.

**Say:** “Selecting a candidate is not enough to submit the clarification. I review the highlighted John Lee and press Confirm selection. The browser sends the offered candidate ID; the server resolves it and returns a new draft with a new ID and nonce. The amount remains fifty Singapore dollars, and the original corrected transcript stays attached. We checked this real flow: the parser and validator returned a new John Lee draft with validator pass and policy allow. During this take, I will show the actual verdict on screen.”

### 4:25–4:55 — Explicit amount read-back

**Screen/actions:** Click **Read amount twice**. Let the actual device speak the amount and currency twice. Show that the draft and action state do not change. Use Stop read-back if needed. If audio is unavailable, show the browser's fallback message instead of adding a fake audio track.

**Say:** “Read-back uses the server-returned amount, not a fresh interpretation of my speech. It repeats the amount and currency twice as a review aid. Audio is not authorization, and neither playback nor spoken confirmation signs or executes a transaction.”

### 4:55–5:30 — Live validation result and fail-closed behavior

**Screen:** Show a previously verified clear-request result only if you genuinely recorded it, or show the current real response. The verified sentence is **“Send fifty dollars to John Smith.”** It returned John Smith `****1234`, **50.00 SGD**, **pass**, reason **“The draft matches the transcript.”**, and policy **allow**, in about 21 seconds. Avoid another slow live request unless the take has time.

**Say:** “Our live check returned HTTP 200 with the expected John Smith and fifty-dollar amount. Health confirmed a real LLM validator and ADP client, and the browser displayed the same pass and allow result. A default installation instead freezes with validator_not_configured. That is deliberate fail-closed behavior, not successful validation. Merged main still disables signing and execution while the passkey PR awaits review. A validator pass alone is never an execution instruction.”

**Fail-closed alternative if the live model fails:** “The current result is blocked, and we are showing that honestly. The gateway will not sign or execute a draft without an acceptable validator result and policy decision. We will not relabel a default freeze or a stub response as a real AI pass.”

### 5:30–6:10 — Security test evidence, accurately scoped

**Screen:** Show PR #32's public description or its redacted test summary. Use an on-screen label **ISOLATED TEST — VIRTUAL AUTHENTICATOR / DETERMINISTIC VALIDATOR / NO BANK**. Do not click execution, show a fake Face ID screen, or imply a native biometric check occurred.

**Say:** “The unmerged passkey integration was tested with Chromium's actual WebAuthn APIs and a virtual authenticator. A deterministic test validator and a no-bank gateway allowed us to verify registration IDs, canonical hash binding, ES256 verification, and a separate execution click in an isolated ledger test. This was not hardware Face ID, LLM validation, or a real transfer. The replay regression rejects a reused signed draft with a fresh key as 422 replayed. A tampered challenge is a different failure: 422 hash_mismatch. Member 3 is reviewing that guard and the contract clarification.”

### 6:10–7:00 — Build reflection and development-tool tips

**Screen:** Architecture ownership table, current test summary, and one genuine CodeBuddy/WorkBuddy development-chat screenshot if available. Do not substitute a Codex conversation or fabricated transcript for evidence from a required tool.

**Say:** “Our main integration lesson was to check the running process, not just the code. An older server was still using the stub parser and an unconfigured validator; restarting the intended commit with the real configuration fixed the diagnosis. We also learned to preserve late speech results, make Send explicit, and keep each clarification card independent. A useful CodeBuddy or WorkBuddy workflow is to state the file boundary, the safety invariant and the test expectation before requesting a change. Ask for the exact diff, inspect it, and verify the real endpoint. Unit tests alone cannot prove a browser ceremony or a deployed AI service works. In this preparation pass, 91 focused tests passed, and the mobile check used actual touch events in a 390-pixel Chromium viewport. Those are focused checks, not a claim about every test in the project.”

### 7:00–7:20 — Close and scope

**Screen:** Cover plus public repository URL. Do not display a placeholder as though it were a deployed URL.

**Say:** “DCTA turns an uncertain spoken request into a draft a person can inspect and correct. Our value is the review boundary: explicit amounts, offered recipients, independent validation, policy and deliberate authorization. Multi-intent requests are currently rejected rather than silently shortened. The public repository contains the prototype and test evidence. The hosted demo and final video links will be added after upload.”

## Recording guardrails

- This script ends without an execution call. Do not merge PR #32 to make the recording easier.
- Pass/freeze narration must follow the current response, not a planned outcome.
- Do not claim the generated ambiguity question or voice-answer selection is merged; that worktree is separate.
- Do not claim a phone's microphone, Safari speech recognition, hardware passkeys or audible speech have been tested by headless Chromium.
- Use the test counts above only for the named checks. The prior PR #32 run was 128 focused tests passed with 15 Node-dependent skips and 26 browser assertions; it is separate historical evidence, not a fresh run during this submission pass.
