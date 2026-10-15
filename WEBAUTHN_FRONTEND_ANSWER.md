# For Member 2 — the WebAuthn registration flow, and the validator question

Both questions were answered by running the code, not by reading it. The
short version is at the top; the evidence is below.

## Q1: Can a browser create and register a usable credential?

**Yes — but only in one of two shapes, and the obvious third one silently
does not work.**

`POST /api/webauthn/register` takes `{user_id, public_key}`. As of the
`feature/member3-register-key-check` branch, `public_key` may be:

| Form | Works? | Notes |
|---|---|---|
| PEM string (`-----BEGIN PUBLIC KEY-----…`) | ✅ | What the rest of the suite uses |
| base64 or base64url of a DER SubjectPublicKeyInfo | ✅ | **Use this one** — it is what `getPublicKey()` gives you |
| COSE_Key map written as JSON | ❌ | Refused with `malformed_request` |

The frontend path that works:

```js
const credential = await navigator.credentials.create({
  publicKey: {
    challenge: crypto.getRandomValues(new Uint8Array(32)),
    rp: { name: "DCTA", id: "localhost" },
    user: { id: crypto.getRandomValues(new Uint8Array(16)),
            name: "demo", displayName: "Demo User" },
    pubKeyCredParams: [{ type: "public-key", alg: -7 }],   // -7 = ES256
    authenticatorSelection: { userVerification: "required" },
  },
});

// getPublicKey() returns a SubjectPublicKeyInfo DER. Stringify it and send
// it as-is — the server now accepts base64/base64url DER directly.
const der = new Uint8Array(credential.response.getPublicKey());
const b64url = btoa(String.fromCharCode(...der))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

await fetch("/api/webauthn/register", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    user_id: "demo",
    public_key: b64url,          // or a PEM string, both work
  }),
});
// -> {"status":"registered","credential_id":"..."}
```

If `getPublicKey()` is unavailable, `credential.response.attestationObject`
also carries the key, but you would have to parse CBOR + authData to get at
it. Prefer `getPublicKey()`.

### The trap, and why it now fails loudly

**A COSE_Key map written as JSON cannot work.** COSE labels are integers
(`1`, `3`, `-1`, `-2`, `-3`); JSON object keys can only be strings, so
`{"-2": "…"}` never matches and the server parsed it into nothing.

Before this change, register stored the junk anyway and returned
`{"status":"registered"}` — so registration looked successful and
`/api/execute` failed much later with `signature_invalid`. That reads like a
bug in *your* signing code. Now:

```json
{"result":"rejected","reason_code":"malformed_request",
 "message":"unusable public_key: credential is not an EC2 key. Send either a
 PEM SubjectPublicKeyInfo string or base64/base64url of the DER. A COSE_Key
 map written as JSON will not work: COSE keys are integers (-1, -2, -3) and
 JSON keys can only be strings."}
```

Same for a non-P-256 key (P-384 parses fine as "an EC key" but every
signature here is ES256, so it could register and then never verify).

### The rest of the flow, with exact field names

```
POST /api/message                 {"transcript": "…"}
  -> items[0].draft.id

POST /api/webauthn/challenge      {"draft_id": "<id>"}
  -> {draft_id, draft_hash, challenge, rp_id, user_verification,
      allow_credentials}
     ⚠️ the field you sign is `challenge`, NOT `draft_hash`.
        `challenge` is base64url of the raw 32 bytes; `draft_hash` is hex of
        the same. Using the wrong one gives hash_mismatch.

POST /api/execute                 {draft_id, credential_id, idempotency_key,
                                   assertion: {credential_id,
                                               authenticator_data,
                                               client_data_json, signature}}
```

Signature is ES256 over `authenticator_data ‖ SHA256(client_data_json)`.
`client_data_json` must be `{"type":"webauthn.get","challenge":<challenge>,
"origin":<origin>}`; the server checks `type`, `origin`, and the rpIdHash in
`authenticator_data`, and requires the **user-verified** flag. `rp_id` is
`localhost`, so run the demo on `http://localhost:8000`.

Verified end to end: register → challenge → sign → execute returns
`{"result":"executed","transaction_id":"tx-000001"}`.

## Q2: Is there a pass path without the unvalidated bypass?

**No. Not right now.** Three configurations exist, and I ran all of them:

| Config | Verdict | Reaches execute? |
|---|---|---|
| default (no env) | `freeze` / `validator_not_configured` | ❌ challenge is 422 `validator_frozen` |
| `DCTA_ALLOW_UNVALIDATED=true` | `pass` / `unvalidated (dev only)` | ✅ — this is the bypass you mean |
| `VALIDATOR_BACKEND=llm` + ADP keys | real `LlmValidator` | ✅ the honest path, needs `.env` keys |

Note the middle row: the challenge endpoint refuses anything that is not a
recorded pass, so with the default validator you never even get to signing.
That is deliberate (it is the same condition as check 11), but it means a
default-config server cannot demo the happy path at all.

### My suggestion — needs Member 1's call, it is their directory

Add a deterministic validator for demos: `VALIDATOR_BACKEND=stub` (or
`DCTA_DEMO_VALIDATOR=1`) that returns `pass` for a draft whose fields match
the transcript, and `freeze` with a real discrepancy list otherwise.

Why bother: the LLM validator is flaky for a live demo (network, model
nondeterminism), and `DCTA_ALLOW_UNVALIDATED` literally reports
`"unvalidated (dev only)"` — which undercuts the thing we are demoing. A
deterministic stub gives a repeatable happy path *and* a real freeze case,
without lying. It belongs in `backend/validator/`, so it is Member 1's
decision; I am happy to write it if they want it.

## For everyone: the test count

Member 1 was right that 857 was stale. On current `main` (`ce1b778`) the
suite is **1200 passed, 1 skipped**. With the registration branch, **1207**.

```
git checkout main && git pull
```
