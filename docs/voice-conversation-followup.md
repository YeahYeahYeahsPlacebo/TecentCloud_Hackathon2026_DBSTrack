# PR #36: one voice conversation — 16 October 2026

Preview: **http://localhost:8889/** on the owner's Mac. Hard-refresh with Cmd+Shift+R. Port 8888 remains the older merged-main demo. PR #36 is unmerged; PR #32 and all backend transaction/WebAuthn behavior are unchanged.

## Final UI behavior

There is one main message microphone: **Tap to talk**. Tap once to start; its accessible pressed state and status show listening, and the label becomes **Stop listening**. Tap again to finish. Browser-ended recognition cycles restart while the session is active. Cumulative final results are rebuilt within each cycle and completed cycles accumulate once. Interim words remain preview-only. Late final results after Stop are accepted until the session ends. Finals stay editable and Send is always manual. Permission/service errors, editing and cancellation stop recognition; typing stays available. Repeated empty endings and a two-minute cap bound retries. Short restart gaps and recognition mistakes remain possible.

Both response cards display immediately. Automatic spoken review follows transaction order and pauses at an unresolved payee:

- Two known payees: transaction 1 draft, then transaction 2 draft, without overlap.
- John ambiguous first: transaction 1 candidate question, then **pause**. Jane remains visible and silent. After manual Confirm selection and a new server-created John draft: complete resolved transaction 1 read-back, then waiting transaction 2 Jane read-back.
- Ambiguous second: transaction 1 read-back, transaction 2 candidate question, then pause there.
- Each draft read-back identifies the transaction and includes the server-returned payee, masked account ending, amount, currency, validator verdict and policy decision. A missing verdict is spoken as unavailable. No spoken response authorizes anything.

**Answer by voice** appears only on an ambiguous card. It stops speech before starting the microphone. A unique final offered name or full account suffix naturally stops listening and highlights a candidate for visual review. It never submits clarification. Partial/shared names, unclear answers, silence, errors and denial preserve the existing selection and offer **Retry voice answer** plus tap choices. **Stop listening** remains available as a fallback. Late callbacks from replaced cards are ignored.

Every review item offers **Skip spoken review for this item / Continue**. This advances narration only; it never resolves or changes the draft or confirms a candidate. **Cancel spoken review** stops active/pending review and prevents a delayed result from reviving it. A new manually sent message starts a fresh review. Manual question/draft/amount replay remains available. Playback errors and a five-second no-start timeout show replay/Continue fallbacks. New messages, replaced cards and page navigation invalidate stale work. Clarifying John does not re-render Jane or change her draft, hash, validator or policy; her narration status changes when her turn begins.

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

- 614 focused backend tests passed, with one existing Starlette/httpx deprecation warning. No production backend or contract changes in this follow-up.
- 48 focused browser regressions passed: 12 main recognition lifecycle cases, 20 contextual ambiguity cases and 16 conversation sequence cases. These use explicitly injected recognizer/synthesis events, with no transaction API calls in the regression pages.
- Real Chrome product test used actual LLM parser/validator API responses, no bypass and no bank. A long two-cycle injected recognition result was manually corrected from fifteen to fifty before manual Send. HTTP 200 returned two cards; John was held for clarification and Jane allowed. Partial John preserved the prior tap selection and offered Retry. A final John Doe answer stopped listening naturally and only highlighted the candidate. Manual Confirm selection returned HTTP 200, a new John Doe / 50.00 SGD draft with real validator pass, policy allow and matching canonical hash. The exact injected speech completion sequence was John question → pause → resolved John read-back → Jane read-back. Jane's draft/core DOM/hash/verdict/policy and card identity stayed unchanged.
- Real two-known-payee and ambiguous-second requests were also exercised in Chrome against the actual API. Speech/recognition completion was injected so sequence order and barriers could be asserted; this does not establish microphone recognition accuracy or audible sound.
- Native HeadlessChrome 153 exposed recognition but microphone permission stayed `prompt` and no final words arrived. Native synthesis listed 180 voices but produced no start/end event within six seconds. This cannot establish physical microphone accuracy or audible output. Browser autoplay and recognition service availability vary. The manual procedure below is required before merging.
- JavaScript compilation, whitespace and private-key checks passed. No `/api/execute`, signing, registration or challenge requests were made by the product test.

## One final manual procedure

1. Open **http://localhost:8889/** in Mac Chrome, hard-refresh, allow microphone access and turn on sound. Check that there is just one main **Tap to talk** button. **Answer by voice** belongs only to an ambiguous card; **Cancel spoken review** controls audio, not message entry.
2. Tap to talk and say `Send fifty dollars to John and send thirty dollars to Jane Tan.` Pause between clauses while listening remains active. Tap **Stop listening**. Wait for final words; check both clauses are retained without duplicated segments. Correct any mistakes in the editable textarea, then click **Send** yourself.
3. Both cards must appear. Hear transaction 1's question containing only the actual offered names/account endings. After the question, wait: Jane must remain silent. If playback is blocked, use **Hear question**; the pause must still apply.
4. Tap an offered John candidate, then try **Answer by voice** with only `John`. The existing selection must stay unchanged; Retry/tap choices remain. Retry with silence or an unclear phrase and verify the same. No clarification should submit. Use the **Stop listening** fallback if Chrome keeps listening.
5. Retry with an offered full name, such as `John Doe` only if that name is actually offered, or its full account ending. Speech must stop before listening begins. A unique final answer should stop listening automatically and highlight the offered candidate. Do not press another Finish button. Review the heard words and selected payee, then manually click **Confirm selection**.
6. Hear complete resolved John first, then Jane: transaction number, payee, account ending, amount/currency and validation status. Compare every value with its card. Jane's draft, hash, validator and policy must remain unchanged. Use a manual draft replay once.
7. Send `Send fifty dollars to John Smith and send thirty dollars to Jane Tan.` Expect two complete read-backs in order, with no overlap. Then send `Send thirty dollars to Jane Tan and send fifty dollars to John.` Expect Jane first, then John's question and a pause. Use tap selection and manual Confirm once to verify the tap path.
8. Repeat an ambiguous-first request and choose **Skip spoken review for this item / Continue**. Jane should read next while John stays unresolved and unconfirmed. Spoken `yes` must neither select a payee nor submit anything.
9. While speech is playing, click **Cancel spoken review**. It must stop and not resume from a delayed response. Send a new message during speech and confirm no old audio returns. Switch a fixture or leave/reopen the page to check cancellation too. Replay controls must still work.
10. Deny microphone permission once and retry message input and a card answer. The UI must show the failure/Retry; typing, tap choices, manual Send and manual Confirm remain available. Re-enable permission afterward if desired. Do not sign, execute, or call `/api/execute`. Keep Mom unresolved as documented above.

Report any misheard words, missing final segments, unexpected narration order or actual audio failure before merging #36. Automated injected events do not replace this physical microphone-and-speaker check.
