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

## Native Chrome follow-up (current refinement)

The exact input was `Send fifty dollars to John and send thirty dollars to Jane.` Native Chrome 155 on the Mac used real LLM `/api/message` and `/api/clarify` responses (both HTTP 200), without replacing synthesis or injecting speech completion. Initially the test tab became hidden during the LLM wait, cancelling automatic review; manual native replay emitted start/end. Headless Chrome's native replay instead failed to start. Neither observation proves an automatic clarification-handoff defect.

Repeating in visible Chrome with DevTools foreground-focus emulation produced the actual candidate question, then—after manual selection of an offered John Doe and manual Confirm selection—the complete resolved transaction 1 followed by complete Jane transaction 2. Both draft utterances emitted native start/end events; manual Read draft also emitted start/end. John was 50.00 SGD / ending 4521, Jane 30.00 SGD / ending 5678; both real validator results were pass, policy allow. Each draft amount was spoken twice in its full utterance. The question was interrupted by the test's manual selection/confirmation; deterministic regression cases separately verify that completing the question does not advance to Jane. No browser JS errors or signing/execution requests occurred. These native events establish the browser lifecycle, not what a person heard from speakers. Physical microphone/speaker verification remains the owner's merge gate.

## Short final Chrome retest

1. Open **http://localhost:8889/** in Mac Chrome and hard-refresh (Cmd+Shift+R). Enable sound and keep the page visible during the LLM wait. Use the single Tap to talk control or type exactly: `Send fifty dollars to John and send thirty dollars to Jane.` Review/edit, then manually press Send.
2. Both cards appear. Hear the actual John candidate question; wait for it to end. Jane must remain silent. Only John's active unresolved card should offer **Skip spoken review / Continue**.
3. Tap an offered John (or use Answer by voice and review the highlighted unique match), then manually click **Confirm selection**. Wait for the server's replacement. Hear **complete John, then complete Jane**: transaction number, payee, masked account ending, amount/currency twice, validation status. Compare to the cards; Jane's original draft/hash/verdict/policy must stay unchanged. Skip must disappear after resolution/completion.
4. Click **Read draft** once on John and Jane. Each must replay the same complete draft with amount twice; no separate Read amount twice/Hear question button should exist. If silent, report whether native manual replay is silent too, check Chrome site sound/mute/output, and use the displayed details. Do not accept a finished status without completed playback.
5. Start the main microphone or send a new message during playback: old audio must stop and not return. Speech/“yes” must never Send, Confirm selection, sign or execute. Do not merge #36 until actual sound is verified; do not call `/api/execute`.
