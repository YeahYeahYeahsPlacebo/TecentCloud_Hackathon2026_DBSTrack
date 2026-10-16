# PR #36: one voice conversation — 16 October 2026

Preview: **http://localhost:8889/** on the owner's Mac. Hard-refresh with Cmd+Shift+R. Port 8888 remains the older merged-main demo. PR #36 is unmerged; PR #32 and all backend transaction/WebAuthn behavior are unchanged.

## Final UI behavior

There is one main message microphone: **Tap to talk**. Tap once to start; its accessible pressed state and status show listening, and the label becomes **Stop listening**. Tap again to finish. Browser-ended recognition cycles restart while the session is active. Cumulative final results are rebuilt within each cycle and completed cycles accumulate once. Interim words remain preview-only. Late final results after Stop are accepted until the session ends. Finals stay editable and Send is always manual. Permission/service errors, editing and cancellation stop recognition; typing stays available. Repeated empty endings and a two-minute cap bound retries. Short restart gaps and recognition mistakes remain possible.

Both response cards display immediately. Automatic spoken review follows transaction order and pauses at an unresolved payee:

- Two known payees: transaction 1 draft, then transaction 2 draft, without overlap.
- John ambiguous first: transaction 1 candidate question, then **pause**. Jane remains visible and silent. After manual Confirm selection and a new server-created John draft: complete resolved transaction 1 read-back, then waiting transaction 2 Jane read-back.
- Ambiguous second: transaction 1 read-back, transaction 2 candidate question, then pause there.
- Each draft read-back identifies the transaction and includes the server-returned payee, masked account ending, amount and currency twice, validator verdict and policy decision. A missing verdict is spoken as unavailable. No spoken response authorizes anything.

**Answer by voice** appears only on an ambiguous card. It stops speech before starting the microphone. A unique final offered name or full account suffix naturally stops listening and highlights a candidate for visual review. It never submits clarification. Partial/shared names, unclear answers, silence, errors and denial preserve the existing selection and offer **Retry voice answer** plus tap choices. **Stop listening** remains available as a fallback. Late callbacks from replaced cards are ignored.

Each server draft has one replay button, **Read draft**. Its complete read-back repeats the server amount/currency twice. On an unresolved card it also includes the offered-candidate question, without selecting or submitting anything. The separate amount-only and question replay buttons are removed.

**Skip spoken review / Continue** appears only on the unresolved item actively holding narration. It is hidden on waiting, resolved, completed, skipped or cancelled items. Skipping advances narration only; it never resolves or changes a draft. Starting a new message or the main microphone cancels old active/pending narration. Replaced cards and navigation invalidate stale work. Clarifying John does not re-render Jane or change her draft, hash, validator or policy.

Playback must emit a native start and end before the queue advances or reports completion. Errors, a five-second no-start timeout and a bounded started-but-stalled timeout retain a truthful visual fallback and manual replay. A successful manual replay of the current resolved item resumes waiting narration. If manual replay is also silent, check Chrome site sound, mute/output settings and the complete displayed details; the app does not pretend audio finished. Foreground playback is required: hiding/leaving the page cancels review, including a delayed response, rather than reviving stale audio.

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

- Earlier verification: 614 focused backend tests passed, with one existing Starlette/httpx deprecation warning. No production backend or contract changes in this follow-up.
- 52 focused browser regressions passed: 13 main recognition lifecycle cases, 20 contextual ambiguity cases and 19 conversation sequence cases. The added cases cover one replay control, twice-spoken amount, Skip visibility, end-without-start and stalled playback. These use explicitly injected recognizer/synthesis events, with no transaction API calls in the regression pages.
- Real Chrome product test used actual LLM parser/validator API responses, no bypass and no bank. A long two-cycle injected recognition result was manually corrected from fifteen to fifty before manual Send. HTTP 200 returned two cards; John was held for clarification and Jane allowed. Partial John preserved the prior tap selection and offered Retry. A final John Doe answer stopped listening naturally and only highlighted the candidate. Manual Confirm selection returned HTTP 200, a new John Doe / 50.00 SGD draft with real validator pass, policy allow and matching canonical hash. The exact injected speech completion sequence was John question → pause → resolved John read-back → Jane read-back. Jane's draft/core DOM/hash/verdict/policy and card identity stayed unchanged.
- Real two-known-payee and ambiguous-second requests were also exercised in Chrome against the actual API. Speech/recognition completion was injected so sequence order and barriers could be asserted; this does not establish microphone recognition accuracy or audible sound.
- Native HeadlessChrome 153 exposed recognition but microphone permission stayed `prompt` and no final words arrived. Native synthesis listed 180 voices but produced no start/end event within six seconds. This cannot establish physical microphone accuracy or audible output. Browser autoplay and recognition service availability vary. The manual procedure below is required before merging.
- JavaScript compilation, whitespace and private-key checks passed. No `/api/execute`, signing, registration or challenge requests were made by the product test.

## Current lifecycle investigation

The owner's physical Chrome failures are the failing reproduction. A successful injected speech event is not evidence that their microphone or speakers work.

The native Chrome 155 trace used exactly `Send fifty dollars to John and send thirty dollars to Jane.` Permission was granted, TTS stopped before recognition started, and the page stayed visible. Chrome emitted start → audiostart → speechstart → speechend → audioend → end, but **no result event and no error**. The app issued no premature stop/restart/abort before that end. No words reached candidate matching. No physical full-name utterance was supplied by automation, so this identifies the native no-result path without claiming a successful spoken John Lee test or proving the cause of the owner's missing words.

Concrete frontend corrections:

- “Listening” now waits for native start. Permission/startup gets a separate 45-second bound; the short 15-second answer timer starts at native start. The previous timer could expire during a permission prompt.
- Audio capture/speech detection phases are tracked without recording/logging transcripts. No-result cleanup replaces stale “Heard: listening…” with “Chrome returned no final words”, identifies the last observed phase, preserves selection, and offers Retry/tap. Native errors retain useful public codes such as network, not-allowed and audio-capture instead of collapsing into a generic failure. Interim words never select a payee.
- Recognition cancels native TTS and waits for its speaking/pending flags to clear. This is handoff hardening; the captured native attempt already showed TTS stopped.
- Hiding the tab cancels active audio/microphone but retains this message's review position. It never resumes automatically merely on return. Read draft or deliberate Confirm selection can recover resolved John then waiting Jane. New messages/navigation still discard old work. This fixes the observed loss of the review plan after visibility cancellation.
- Clarification marks the card replacement before the asynchronous hash/render handoff. Observing that replacement, or removal of an already completed earlier card, must not discard the remaining conversation. Actual detached/stale message callbacks remain rejected.
- One Read draft per card; full server details and amount/currency twice. Native start/end remain required for completion. Technical details stays collapsed; the developer-facing account-display mapping note is removed while IDs/nonces/hashes remain available.

After the corrections, a second native attempt again returned no result, now with truthful phase feedback. Actual tap selection of offered **John Lee**, manual Confirm selection and real `/api/clarify` returned HTTP 200. Native synthesis emitted start/end for resolved John Lee (50.00 SGD, ending 8892), then Jane Tan (30.00 SGD, ending 5678). Jane's core DOM/card/hash/validator/policy stayed unchanged; the new John hash matched validation. Technical details was collapsed and each card had one replay control. No synthesis completion or recognition result was injected in this run; foreground focus was maintained with DevTools. This verifies native lifecycle events, not audible sound or physical full-name recognition.

Current focused browser regressions: **60 passed** (13 main recognition, 25 ambiguity, 22 conversation), using labelled event doubles. These cover no-result cleanup, native error distinction, delayed start, asynchronous TTS cancellation, late finals, hidden-tab manual recovery and card replacement, as well as the prior conversation boundaries. Earlier 614-backend-test and 52-browser-test results above are historical, not counts for this refinement.

## Three-minute physical Chrome acceptance

Open **http://localhost:8889/** on the owner's Mac and hard-refresh (Cmd+Shift+R). Keep the tab visible and sound enabled. LLM waits may extend the timing below.

1. **0:00–1:00:** Type exactly `Send fifty dollars to John and send thirty dollars to Jane.` and manually Send. Hear the actual offered-John question, then silence from Jane. Tap Answer by voice, allow access, wait for **Listening**, then clearly say **John Lee** (only if offered). Report whether it shows the final heard words and selects John Lee. If not, report the exact phase/error status, not private console logs. Tap selection remains available.
2. **1:00–2:15:** Review the candidate and manually Confirm selection. Hear **complete transaction 1 John Lee → complete transaction 2 Jane**, without overlap: payee, account ending, amount/currency **twice**, validation status. Check Jane's hash/verdict/policy are unchanged. If voice selection failed, tap an offered John and perform this same confirmation.
3. **2:15–3:00:** Click Read draft on each card; report **manual audible yes/no** and **automatic John audible yes/no, Jane audible yes/no**. If silent, report whether the UI says Playing, Browser reported playback complete, or playback failed; check Chrome site sound and macOS output/mute. Briefly hide/return: old audio must stop, and Read draft must recover this message's review. Technical details stays collapsed. Do not sign or execute.

The remaining physical acceptance gate is an actual spoken unique-name result plus speaker sound on the owner's device. No virtual authenticator or injected event can satisfy it. No PR is merged by this work.
