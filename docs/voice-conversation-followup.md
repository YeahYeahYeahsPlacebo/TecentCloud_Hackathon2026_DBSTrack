# Conversation audio follow-up — 16 October 2026

Based on merged main `37ae243`; PR #32 is unchanged. This work does not enable signing or execution.

## Automatic conversation and microphone lifecycle

After a successful `/api/message`, the frontend waits for all card/hash renders, then queues ambiguity questions before draft read-backs. Each utterance identifies its item number. Questions use only the offered `payee_candidates`; read-backs use only that item's server-returned draft. Two known drafts read in item order. Synthesis completion starts the next utterance; speech never submits, selects, signs or executes. Candidate selection still requires a unique voice match or a tap, followed by manual **Confirm selection**. Its newly returned draft is read back separately.

Visible questions, **Hear question**, **Read draft**, **Read updated draft** and **Read amount twice** remain available. If synthesis errors or never starts within five seconds, automatic playback stops with a manual replay reminder. Starting recognition or manual replay, sending a message, confirming a selection, switching fixtures, replacing a card, hiding or leaving the page cancels stale audio and queued work.

Recognition now requests continuous results and restarts when the browser ends a still-held or tap-mode session. Each recognizer's cumulative finals replace only its own cycle; completed cycles are accumulated once. Interim results remain preview-only. Release calls `stop()` and retains late finals until `onend`; release during a restart gap prevents the restart. Cancellation, editing, permission/service errors and navigation stop the session. Three consecutive empty endings or a two-minute session cap stop restart loops. A new **Tap to start talking / Tap to stop talking** button offers an explicit alternative to holding. Send remains manual.

Browser speech services can mishear words, stop unexpectedly or leave short gaps between restart cycles. No frontend can recover audio the recognizer did not deliver. Always review and correct the editable final transcript.

## Mom: intentionally unresolved

The owner explicitly selected **Keep Mom unresolved** on 16 October. No Mom-to-contact mapping was established, configured or inferred. The trusted fixture directory contains John Smith, Jane Tan, John Doe and John Lee; none has a relationship alias. The parser, validator and contract provide no verified alias store. No account or relationship was invented, and this PR adds no production backend or contract change.

Real main API observations:

| Input | HTTP | Result |
| --- | --- | --- |
| `Send thirty dollars to Mom` | 200 | Payee question: `I can't tell who "mom" is. Please name one of your saved payees.` No draft. |
| `Send fifty dollars to John Smith and send thirty dollars to Mom.` | 422 | `multi_intent_unsupported`; no partial John draft. |
| `Send fifty dollars to John Smith and then send thirty dollars to Jane Tan.` | 422 | `multi_intent_unsupported`; ordered wording is independently unsupported even with known payees. |
| `Send fifty dollars to John Smith and then send thirty dollars to Mom.` | 422 | `multi_intent_unsupported`; both constraints apply. |

An unknown nickname and an ordered request are separate causes. Do not present these as alias support. Alias persistence/conflict/change handling is deferred: there is no authorized mapping to persist or change. Regression tests instead verify that Mom stays unknown, model-guessed recipients produce a question, and the independent validator freezes each of the four possible guessed/changed recipients. Future alias support needs explicit owner verification, a server-controlled mapping, consistent independent validation and a defined change/revocation lifecycle.

## Verification and limits

- 614 focused backend tests passed (one existing Starlette/httpx deprecation warning), including 12 new unverified-relationship cases. Model extraction is canned in unit tests; no execution calls are in the new cases.
- 39 browser regressions: 12 recognition lifecycle, 16 ambiguity and 11 conversation queue cases. These use deterministic recognizer/synthesis doubles, not physical microphone accuracy or audibility evidence.
- Real Chromium product flow used actual LLM parser/validator responses, no bypass and no bank. A two-cycle injected recognition result assembled `Send fifteen dollars to John and send thirty dollars to Jane Tan.` A manual correction to fifty and manual Send produced HTTP 200, two cards, John policy hold/unresolved_fields and Jane policy allow. Both real validator verdicts were pass. Manual John Doe confirmation returned HTTP 200, a new 50.00 SGD draft, real pass/allow and matching canonical hash. Jane's card identity, displayed draft/hash/verdict/policy and response data were unchanged. Initial question, draft replay and new-draft read-back invoked native synthesis. No sign/challenge/register/execute requests occurred.
- Native recognition probe: HeadlessChrome 153 exposed recognition; microphone permission was `prompt`, no final words were delivered. Automated tests cannot establish real-device microphone behavior or audible sound. Browser autoplay/voice availability varies; synthesis invocation is not evidence of audible output.
- JavaScript compilation in Chromium, whitespace and private-key scans passed.

## Manual Chrome check

Use the follow-up server URL supplied with the PR; the old main demo does not include this branch until it is merged and restarted. Allow microphone access and turn on Mac sound.

1. Hold **Hold to talk** and say `Send fifty dollars to John and send thirty dollars to Jane Tan.` Include a pause between clauses while continuing to hold. Check that listening resumes if Chrome ends a recognition cycle. Release; wait for final words. Confirm both clauses remain editable without repeated earlier segments. Correct any misheard words before clicking Send. Speech must not click Send.
2. Repeat using **Tap to start talking**, pause and speak both clauses, then **Tap to stop talking**. Also check denial of microphone permission: typing and manual Send must remain available.
3. Click Send. Expect two cards. The offered John question should attempt to speak automatically and identify Item 1. Jane's read-back should follow without overlap. If silent/blocked, use **Hear question** and **Read draft**. Do not infer authorization from audio.
4. Say an unclear answer or `yes` using **Answer by voice**: no payee should be selected. Then say an offered full name/account ending, or tap an offered payee. Check what was heard and selected. Nothing should submit until **Confirm selection**.
5. Click **Confirm selection**. Hear the newly returned draft, and compare it to the screen. Jane's amount, payee, hash, validator and policy must remain unchanged. Use manual replay controls too.
6. Try two known payees, then put the ambiguous John request second. Audio should prioritize John's Item 2 question, followed by the known Item 1 draft. Start a new message or switch a fixture during playback: old audio must stop.
7. Try each Mom/ordered example above. Keep Mom unresolved. Do not say that Jane or any other saved payee is Mom, and do not expect a successful two-transfer response containing Mom.
8. Verify speech and `yes` never enable or invoke signing/execution. Do not call `/api/execute`.
