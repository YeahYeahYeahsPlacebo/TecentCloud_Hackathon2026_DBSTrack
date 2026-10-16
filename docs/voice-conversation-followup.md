# Submission demo: voice input and spoken review with explicit tap clarification

Combined preview: **http://localhost:8891/** on the owner's Mac. Hard-refresh with Cmd+Shift+R. No mobile deployment is part of this work. PRs #32, #36 and #37 remain independent, unmerged PRs.

## Current flow

- **Tap to talk** enters the initial instruction. Interim words are preview-only; final words stay editable. The user reviews/corrects the text and manually presses **Send**. Typing remains available when recognition is unsupported or unreliable.
- Both transfer cards display immediately. An ambiguous payee automatically gets a short spoken question generated only from actual server-offered names/account endings. Later narration pauses at that card.
- Payee clarification is **explicit tap selection** followed by manual **Confirm selection**. The selected name/account ending stays visible for review. The contextual microphone, heard transcript, voice matching and retry states have been removed; the demo does not accept spoken payee answers.
- After `/api/clarify` returns a new server draft, read complete resolved John first, then waiting Jane. Identify each transaction, payee, masked account ending, amount/currency **twice**, validation status and policy decision. Jane's draft/hash/validator/policy are unchanged by John's clarification.
- Each card has one **Read draft** replay button. Native start/end are required before reporting completion or advancing narration. When playback is blocked or the tab becomes hidden, use the visible details and manual replay. Returning to a hidden tab alone does not resume audio; Read draft can recover the current review. New messages and navigation cancel stale work.
- **Skip spoken review / Continue** is shown only on the unresolved card actively holding narration. Skipping does not resolve, sign or execute anything.
- Passkey registration/signing and separate execution confirmation are unchanged. Speech and spoken “yes” never authorize a transaction. Every frozen/unapproved draft remains blocked. Technical details stays collapsed; canonical IDs/hashes/audit data remain available.

## Honest recording language

“Our demo supports voice input and spoken review with explicit tap clarification. I review the editable instruction before sending, tap the intended saved payee, and manually confirm that selection. Spoken review is an accessibility aid, never authorization.”

Do not claim reliable voice payee answering, hardware Face ID, real bank transfers or multi-intent behavior beyond the two supported independent transfer clauses. Mom remains unresolved: no verified alias was configured or inferred. Real validator pass may be shown only when the current server response actually passes; a timeout freeze is a fail-closed result, never approval.

## Short hands-on check

1. Open **http://localhost:8891/** and hard-refresh. If demonstrating registration, do it first and wait for the explicit registered/browser-ID-matched success indicator.
2. Tap to talk or type `Send fifty dollars to John and send thirty dollars to Jane.` Review/correct the editable text, then manually Send.
3. Both cards appear. Hear the actual John candidate question, then a pause. There must be no Answer by voice control, heard transcript or payee voice-retry instruction.
4. Tap **John Lee** only if offered, review the selected name/ending, then manually Confirm selection. Wait for the new server draft. Hear complete John Lee, then complete Jane, each amount twice. Compare the actual verdicts/details and Jane's unchanged hash/state.
5. Try Read draft. If Chrome blocks sound or the tab becomes hidden, return and replay manually; never claim audio was heard from automated events alone. If validation freezes, send a fresh message for a new attempt; do not reuse the frozen draft. Do not execute a real transaction.

## Verification boundaries

Focused tests cover the initial recognition lifecycle, offered-candidate question generation, absence of payee voice controls, independent tap selection, manual confirmation, ordered review/barriers, stale callbacks, hidden-tab recovery and frozen controls. Event doubles are regression evidence, not proof of physical microphone accuracy or audible sound. Native browser lifecycle/API observations and their actual pass/freeze results are reported separately in the PR. Physical microphone/speaker quality remains browser/device dependent; explicit tap clarification is the submission flow.
