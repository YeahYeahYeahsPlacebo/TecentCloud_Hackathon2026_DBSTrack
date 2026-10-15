# Copy-ready submission text

Prepared 16 October 2026, Singapore time. Submission cutoff time was not supplied; confirm the portal's displayed deadline.

## Project title

DCTA — Voice to Reviewed Transaction Drafts

## English blurb — 7 words

Speak, review, and verify every transaction draft.

## Project description

DCTA helps people who prefer speaking or need a clearer way to review payment instructions. Voice recognition can confuse an amount, and a name such as John can identify more than one recipient. A conversational interface should make those uncertainties visible before authorization.

Users speak or type a message, review and correct its final transcript, and press Send manually. A real AI parser extracts a structured draft, while a separate AI validator checks the draft against the user's words. The frontend displays the server-returned amount, currency, recipient and masked account. When a recipient is ambiguous, the user selects an offered directory candidate and explicitly confirms that selection. The server returns a new draft. Optional amount read-back repeats the server's amount twice; voice and audio never authorize or execute a transaction.

The gateway keeps policy and validation checks fail closed. In the verified live configuration, a fifty-dollar John Smith request returned validator pass and policy allow. An unconfigured validator instead freezes the draft. All banking services are mocked, and this prototype is not a live banking product. Multi-intent requests are currently rejected rather than partially drafted.

A separate, unmerged WebAuthn security-review PR adds passkey registration, signing of the displayed canonical draft hash, and a separate execution click. Its cryptographic test used a virtual authenticator, deterministic validator and no bank; it is not proof of hardware Face ID or LLM-validated execution. DCTA's value is a clear boundary between AI assistance, human review and deliberate authorization.

## URLs

- **Verified public project repository:** https://github.com/YeahYeahYeahsPlacebo/TecentCloud_Hackathon2026_DBSTrack
- **Public hosted demo:** PENDING — none supplied or recorded; the repository has no configured homepage or GitHub Pages deployment. Use the repository as a project/demo link only if the submission form explicitly accepts it.
- **Video URL:** PENDING — record, upload and paste the final accessible URL before submitting.
- Localhost addresses are local rehearsal tools, not public submission URLs.

## Cover asset

- `docs/cover.png`
- Verified PNG: **380 × 216 pixels**, **14,742 bytes** (under 10 MB).
- Existing neutral red/white styling; text says prototype/demo and “No live banking.”
- Upload the actual PNG, not a screenshot of the PNG.

## Three genuine development-chat screenshots to capture

Existing `proof/setup.png`, `proof/parser.png`, `proof/hash rules.png` and `proof/jsons and venv.png` are small enough, but this pass did not verify that they show genuine CodeBuddy/WorkBuddy development conversations or are safe to publish. Do not submit them solely because their filenames look suitable.

Capture these from your **actual CodeBuddy or WorkBuddy conversations**, keeping the tool identity, your prompt and the relevant response visible. Do not fabricate chats or relabel another assistant's conversation. If the exact earlier conversation does not exist in those tools, use genuine development/review work there and capture that actual exchange.

1. **Voice review boundary:** a real discussion of interim preview versus final editable transcript, late-result handling, manual Send, and no voice authorization. Suggested filename: `member2-20261016-voice-review.png`.
2. **Payee clarification and mobile UI:** a real discussion of server-offered IDs, John selection, manual Confirm selection, card-scoped state, and touch usability. Suggested filename: `member2-20261016-payee-clarification.png`.
3. **Security/test reflection:** a real discussion reviewing canonical draft hash binding or the replay regression, including the distinction between `replayed` and `hash_mismatch` and the limits of a virtual-authenticator test. Suggested filename: `member2-20261016-security-review.png`.

Each must be **under 10 MB**. Crop to the conversation and verify the exported image in Finder. Never include `.env`, credentials, request headers, clipboard contents, private model-console pages, or secret-bearing logs. Do not upload a screenshot until you have visually checked it for secrets. This is a capture checklist, not a claim that the three images already exist.

## Personal upload checklist

- [ ] Rehearse real speech input, manual correction and audible read-back on the recording device.
- [ ] Record the 5–8 minute English video using `video-script.md`; show actual verdicts and leave execution untouched.
- [ ] Capture and inspect the three genuine tool-chat screenshots; verify each is under 10 MB.
- [ ] Upload `docs/cover.png` and the required screenshots.
- [ ] Upload the video, verify its sharing permissions in a signed-out browser, and paste its URL.
- [ ] Deploy a public demo if the form requires one; otherwise confirm whether the repository project link is accepted. Never substitute localhost.
- [ ] Verify team/member details and the portal deadline in Singapore time.
- [ ] Leave WebAuthn PR #32 to Member 3's security review; do not merge it for submission convenience.
