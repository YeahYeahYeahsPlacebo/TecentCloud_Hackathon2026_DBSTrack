/* Gateway contract: extracted ES256 SPKI registration (not attestation verification).
 * Voice acknowledgement has no connection to these deliberately clicked actions. */
(function (global) {
  "use strict";
  var config = null, credentialId = null, registering = false;
  var cards = new Set();
  function encode(bytes) {
    return btoa(String.fromCharCode.apply(null, new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function decode(text) {
    var binary = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - text.length % 4) % 4));
    return Uint8Array.from(binary, function (c) { return c.charCodeAt(0); });
  }
  // Strict P-256 DER -> 32-byte r || 32-byte s. Reject malformed lengths;
  // never truncate a malformed integer into a superficially valid signature.
  function derToRaw(signature) {
    var der = new Uint8Array(signature), index = 2, raw = new Uint8Array(64);
    if (der.length < 8 || der[0] !== 0x30 || der[1] !== der.length - 2) throw Error("Invalid ES256 DER sequence");
    for (var part = 0; part < 2; part++) {
      if (der[index++] !== 0x02) throw Error("Invalid ES256 DER integer");
      var length = der[index++];
      if (!length || index + length > der.length || der[index] & 0x80) throw Error("Invalid ES256 integer length/sign");
      var value = der.subarray(index, index + length); index += length;
      if (value.length > 1 && value[0] === 0) {
        if (!(value[1] & 0x80)) throw Error("Non-canonical ES256 integer");
        value = value.subarray(1);
      }
      if (value.length > 32) throw Error("Oversized ES256 integer");
      raw.set(value, part * 32 + 32 - value.length);
    }
    if (index !== der.length) throw Error("Trailing ES256 data");
    return raw;
  }
  async function json(url, body, signal) {
    var response = await fetch(url, body === undefined ? { signal: signal } : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: signal
    });
    var result = await response.json();
    if (!response.ok) throw Error((result.reason_code || "HTTP " + response.status) + ": " + (result.message || "Request refused"));
    return result;
  }
  function checkOrigin() {
    if (!config || config.origin !== location.origin || !(location.hostname === config.rp_id || location.hostname.endsWith("." + config.rp_id))) {
      throw Error("The server's passkey origin does not match this page. Open the configured server address.");
    }
  }
  function refresh() {
    cards.forEach(function (state) {
      if (!state.card.isConnected) { if (state.controller) state.controller.abort(); cards.delete(state); return; }
      state.sign.disabled = !config || !credentialId || !state.eligible || state.busy || !!state.signed || state.executed;
    });
  }
  async function register() {
    if (registering) return;
    var button = document.getElementById("register-passkey"), note = document.getElementById("passkey-status");
    registering = true; button.disabled = true;
    try {
      checkOrigin();
      note.textContent = "Create a passkey with your browser or authenticator. This does not authorize a transaction.";
      var created = await navigator.credentials.create({ publicKey: {
        challenge: crypto.getRandomValues(new Uint8Array(32)),
        rp: { id: config.rp_id, name: "DCTA prototype" },
        user: { id: crypto.getRandomValues(new Uint8Array(16)), name: "demo-user", displayName: "Demo user" },
        pubKeyCredParams: [{ type: "public-key", alg: -7 }],
        authenticatorSelection: { userVerification: "required" }, attestation: "none", timeout: 60000
      } });
      if (!created || !created.response.getPublicKey || created.response.getPublicKeyAlgorithm() !== -7) throw Error("This browser did not expose an ES256 public key. Try a compatible browser.");
      var publicKey = created.response.getPublicKey();
      if (!publicKey) throw Error("No public key returned by the authenticator.");
      var browserId = encode(created.rawId);
      var result = await json("/api/webauthn/register", { user_id: "user-001", credential_id: browserId, public_key: encode(publicKey) });
      if (result.status !== "registered" || result.credential_id_source !== "client" || result.credential_id !== browserId) throw Error("Registration did not preserve this browser's credential ID. Signing remains disabled.");
      credentialId = browserId;
      note.textContent = "Passkey registered for this server session. Review a server-approved draft before signing.";
      button.textContent = "Register another passkey";
    } catch (error) { note.textContent = "Passkey registration failed: " + error.message; }
    finally { registering = false; button.disabled = !config; refresh(); }
  }
  function init() {
    var button = document.getElementById("register-passkey"), note = document.getElementById("passkey-status");
    button.addEventListener("click", register);
    if (!global.isSecureContext || !global.PublicKeyCredential || !navigator.credentials || !crypto.subtle) {
      note.textContent = "Passkeys are unavailable here. Use a compatible browser on HTTPS or localhost. Draft review remains available.";
      return;
    }
    json("/api/health").then(function (health) {
      config = health;
      checkOrigin();
      button.disabled = false;
      note.textContent = "Register a passkey, then review and sign an approved draft. Execution requires a separate click.";
      var mode = document.getElementById("validation-mode");
      mode.hidden = false;
      mode.textContent = health.validator === "StubValidator" ? "TEST / DEMO: deterministic validator; no LLM validation. Not live banking." : health.validator === "UnconfiguredValidator" ? "Validation is not configured: drafts stay blocked from signing and execution." : "The server validates each draft before allowing signing. This is a banking prototype.";
      refresh();
    }).catch(function (error) { config = null; note.textContent = "Passkeys unavailable: " + error.message; refresh(); });
    new MutationObserver(refresh).observe(document.getElementById("draft-display"), { childList: true, subtree: true });
  }
  function attach(card, draft, eligible) {
    // Copy the exact server payload displayed by this card, never a voice answer.
    var displayed = JSON.parse(JSON.stringify(draft));
    var actions = card.querySelector(".action-bar");
    actions.innerHTML = '<button type="button" class="btn btn-primary js-passkey-sign" disabled>Sign displayed draft</button>' +
      '<button type="button" class="btn btn-primary js-passkey-execute" disabled>Confirm execution</button>';
    var note = document.createElement("p"); note.className = "hint passkey-card-status"; note.setAttribute("role", "status"); card.appendChild(note);
    note.textContent = eligible ? "Register a passkey, review this draft, then sign. Voice is acknowledgement only." : "This draft is blocked. Signing and execution require resolved fields, validator pass and policy allow.";
    var details = document.createElement("details"), summary = document.createElement("summary"), payload = document.createElement("pre");
    details.className = "tech-details"; summary.textContent = "Exact server draft being signed"; payload.textContent = JSON.stringify(displayed, null, 2);
    details.append(summary, payload); card.appendChild(details);
    var state = { card: card, sign: actions.children[0], execute: actions.children[1], eligible: eligible, busy: false, signed: null, executed: false, key: null, controller: null };
    cards.add(state); refresh();
    state.sign.addEventListener("click", async function () {
      if (state.sign.disabled) return;
      state.busy = true; refresh(); state.controller = new AbortController();
      try {
        checkOrigin();
        var challenge = await json("/api/webauthn/challenge", { draft_id: displayed.id }, state.controller.signal);
        var localHash = await canonical.draftHashAsync(displayed);
        var expectedChallenge = encode(Uint8Array.from(localHash.match(/../g), function (hex) { return parseInt(hex, 16); }));
        if (challenge.draft_id !== displayed.id || challenge.draft_hash !== localHash || challenge.challenge !== expectedChallenge) throw Error("hash_mismatch: the displayed draft differs from the server challenge. Signing stopped.");
        if (challenge.rp_id !== config.rp_id || challenge.user_verification !== "required" || !Array.isArray(challenge.allow_credentials) || !challenge.allow_credentials.includes(credentialId)) throw Error("The server did not offer the registered passkey with required user verification.");
        if (!card.isConnected) return;
        note.textContent = "Draft hash matched. Verify with your authenticator to sign this exact draft.";
        var assertion = await navigator.credentials.get({ signal: state.controller.signal, publicKey: {
          challenge: decode(challenge.challenge), rpId: challenge.rp_id, userVerification: "required",
          allowCredentials: [{ type: "public-key", id: decode(credentialId) }], timeout: 60000
        } });
        if (!card.isConnected) return;
        if (!assertion || encode(assertion.rawId) !== credentialId) throw Error("The authenticator returned a different credential.");
        var client = JSON.parse(new TextDecoder().decode(assertion.response.clientDataJSON));
        if (client.type !== "webauthn.get" || client.origin !== config.origin || client.challenge !== expectedChallenge) throw Error("Unexpected authenticator origin or challenge.");
        state.signed = { credential_id: encode(assertion.rawId), authenticator_data: encode(assertion.response.authenticatorData), client_data_json: encode(assertion.response.clientDataJSON), signature: encode(derToRaw(assertion.response.signature)) };
        state.key = crypto.randomUUID(); // Retain this exact key for uncertain-network retries.
        state.execute.disabled = false;
        note.textContent = "Draft signed; nothing executed. Review the details, then click Confirm execution. Spoken yes is not authorization.";
      } catch (error) { if (card.isConnected) note.textContent = "Signing stopped: " + error.message; }
      finally { state.busy = false; refresh(); }
    });
    state.execute.addEventListener("click", async function () {
      if (state.execute.disabled || !state.signed || state.busy || state.executed || !card.isConnected) return;
      state.busy = true; state.execute.disabled = true; refresh();
      try {
        checkOrigin();
        var result = await json("/api/execute", { draft_id: displayed.id, credential_id: state.signed.credential_id, assertion: state.signed, idempotency_key: state.key });
        if (result.result !== "executed") throw Error("The gateway did not confirm execution.");
        state.executed = true;
        note.textContent = "Gateway recorded execution: " + result.transaction_id + ". This prototype does not move real bank funds.";
        state.execute.textContent = "Executed";
      } catch (error) {
        note.textContent = "Execution not confirmed: " + error.message + ". A retry keeps the same request key.";
        state.execute.disabled = false;
      } finally { state.busy = false; refresh(); }
    });
  }
  global.DctaPasskeys = { init: init, attach: attach, derToRaw: derToRaw };
})(window);
