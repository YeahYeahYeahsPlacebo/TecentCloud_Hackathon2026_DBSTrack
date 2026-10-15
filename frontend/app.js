/* ─────────────────────────────────────────────────────────────
 * DCTA frontend prototype — app.js
 *
 * Text input calls POST /api/message per CONTRACT.md §4.
 * The server returns {draft, validator, policy}. The frontend
 * renders the server-returned draft — it never constructs or
 * mutates one (CONTEXT.md core rule + constraint 1).
 *
 * Fixture scenario buttons remain as a demo fallback when the
 * backend is not running.
 *
 * WebAuthn signing and execution are deliberately disabled.
 * ───────────────────────────────────────────────────────────── */

(function () {
  "use strict";

  // API endpoints (CONTRACT.md §4).
  // Relative to frontend/index.html → same origin.
  var API_MESSAGE_URL = "/api/message";
  var API_CLARIFY_URL = "/api/clarify";

  // Fixture filenames relative to frontend/index.html.
  // Used only by the demo-fallback scenario buttons. These preview assets
  // mirror fixtures/drafts/ so the gateway can serve them from frontend/.
  var FIXTURES = {
    clean_transfer:   "fixtures/drafts/clean_transfer.json",
    ambiguous_payee:  "fixtures/drafts/ambiguous_payee.json",
    equity_purchase:  "fixtures/drafts/equity_purchase.json",
    over_limit:       "fixtures/drafts/over_limit.json",
  };

  var displayEl = null;
  var scenarioBtns = null;
  var textInput = null;
  var sendBtn = null;
  var inputHint = null;

  // Module-level reference to the current draft's payee_candidates.
  // Kept for fixture-preview backward compatibility (single-card fixture
  // renders). Server ambiguous-payee cards now carry their own candidates
  // in a data attribute and closure-scoped state — see
  // renderServerAmbiguousPayeeInto.
  var currentCandidates = [];

  // Page/message generation counter. Incremented ONLY on new
  // /api/message requests and fixture loads — NOT on /api/clarify.
  // Each ambiguous-payee card captures the current messageGen at
  // creation time and stores it in data-message-gen. When a clarify
  // response arrives, if the card's messageGen doesn't match the
  // global, the entire display has been replaced by a newer message —
  // the clarify response is stale and must be discarded.
  // Per-card /api/clarify requests use data-clarify-seq on the card
  // element itself, so confirming card A does not make card B's
  // in-flight clarify response stale.
  var messageGen = 0;

  // ── Initialization ─────────────────────────────────────────
  function init() {
    displayEl = document.getElementById("draft-display");
    scenarioBtns = document.querySelectorAll(".btn-scenario");
    textInput = document.getElementById("text-input");
    sendBtn = document.getElementById("send-btn");
    inputHint = document.getElementById("input-hint");
    if (!displayEl || !scenarioBtns.length) {
      console.error("[app.js] Could not find #draft-display or .btn-scenario elements");
      return;
    }

    DctaVoice.init();

    // ── Scenario button wiring (demo fallback) ───────────────
    Array.prototype.forEach.call(scenarioBtns, function (btn) {
      btn.addEventListener("click", function () {
        var key = btn.getAttribute("data-scenario");
        setActiveButton(btn);
        loadFixture(key);
      });
    });

    // ── Send button + Enter-to-send wiring ───────────────────
    if (sendBtn && textInput) {
      sendBtn.addEventListener("click", function () {
        sendMessage();
      });
      // Ctrl/Cmd+Enter sends; plain Enter inserts newline.
      textInput.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
          e.preventDefault();
          sendMessage();
        }
      });
    }
  }

  function setActiveButton(active) {
    Array.prototype.forEach.call(scenarioBtns, function (b) {
      b.classList.remove("active");
    });
    active.classList.add("active");
  }

  // ── POST /api/message ──────────────────────────────────────
  //
  // Sends the user's exact text as {"transcript": "..."} per
  // CONTRACT.md §4. Shows a loading state, renders the returned
  // draft + validator/policy status, or shows a clear error.
  //
  // The frontend never constructs or mutates a draft. It only
  // displays what the server returns.

  function sendMessage() {
    if (!textInput || !sendBtn || sendBtn.disabled) return;

    var transcript = textInput.value.trim();
    if (!transcript) {
      setHint("Please type a message before sending.");
      return;
    }

    DctaVoice.reset();

    // Clear any previous scenario-button highlight and stale server state.
    Array.prototype.forEach.call(scenarioBtns, function (b) {
      b.classList.remove("active");
    });
    // Increment the message generation so any in-flight /api/clarify
    // response for a previous card is treated as stale.
    messageGen++;

    // Loading state.
    sendBtn.disabled = true;
    sendBtn.textContent = "Sending…";
    textInput.disabled = true;
    setHint("Preparing your transaction draft…");
    displayEl.innerHTML = '<p class="placeholder">Loading…</p>';

    // Capture the generation so we can detect stale /api/message responses.
    var myGen = messageGen;

    fetch(API_MESSAGE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript: transcript }),
    })
      .then(function (res) {
        // Both 200 (success) and 422 (rejection) normally carry a
        // JSON body per CONTRACT.md §4/§5. If the endpoint doesn't
        // exist at all (404, 501, etc.), the body may be HTML and
        // res.json() will fail — we handle that by showing the HTTP
        // status with no body.
        return res.json()
          .then(function (body) {
            return { ok: res.ok, status: res.status, body: body };
          })
          .catch(function () {
            return { ok: res.ok, status: res.status, body: null };
          });
      })
      .then(function (result) {
        // Stale response guard: a newer /api/message request has
        // been issued since this one started. Discard the result.
        if (myGen !== messageGen) return;

        if (result.ok) {
          renderApiResponse(result.body, transcript);
        } else {
          renderApiError(result.status, result.body, transcript);
        }
      })
      .catch(function (err) {
        if (myGen !== messageGen) return;
        // Network failure, connection refused, CORS, or JSON parse error.
        renderApiError(0, null, transcript, err.message);
      })
      .then(function () {
        // Only restore input state if we are still the latest request.
        if (myGen !== messageGen) return;
        // Always restore input state.
        sendBtn.disabled = false;
        sendBtn.textContent = "Send";
        textInput.disabled = false;
      });
  }

  // ── Response reader (CONTRACT.md §4 — merged items-list shape) ──
  //
  // POST /api/message returns:
  //   { intents_detected: <int>, items: [ ... ] }
  //
  // Each item is one of:
  //   { kind: "draft", draft: {...}, validator: {...}, policy: {...} }
  //   { kind: "question", question_id: "...", field: "...", question: "..." }
  //
  // A "question" item means no draft exists yet for that intent;
  // the server is asking the user to clarify a field other than
  // payee (payee ambiguity is resolved inside an existing draft).
  //
  // We read the documented shape exactly — we do NOT invent fields
  // or mutate server-provided drafts.

  function parseMessageResponse(body) {
    if (!body || typeof body !== "object") return null;
    var items = body.items;
    if (!Array.isArray(items)) return null;
    var intentsDetected = body.intents_detected;
    if (typeof intentsDetected !== "number") {
      // If the server omits intents_detected, fall back to len(items).
      intentsDetected = items.length;
    }
    return { items: items, intentsDetected: intentsDetected };
  }

  // ── Render API success ─────────────────────────────────────

  function renderApiResponse(body, sentTranscript) {
    var parsed = parseMessageResponse(body);
    if (!parsed) {
      renderApiError(200, body, sentTranscript,
        "Response did not contain an items list.");
      return;
    }

    var items = parsed.items;
    var intents = parsed.intentsDetected;
    var responseGen = messageGen;

    // Build a container for all rendered items.
    var containerHtml = '';

    // Summary line: how many intents were detected.
    if (intents === 1) {
      containerHtml += '<p class="items-summary">1 intent detected.</p>';
    } else {
      containerHtml += '<p class="items-summary">' + escapeHtml(String(intents)) +
                       ' intents detected.</p>';
    }

    if (body.request_mode === "independent_transfers" && typeof body.source_transcript === "string") {
      containerHtml += '<p class="hint">Two independent transfers. Review each draft separately; no ordering or automatic execution.</p>' +
                       '<div class="transcript">Original message: ' + escapeHtml(body.source_transcript) + '</div>';
    }

    // Render each item. Draft items are rendered asynchronously
    // (hash computation), so we build a placeholder first, then
    // fill it in. Question items are rendered synchronously.
    var draftCount = 0;
    var questionCount = 0;

    items.forEach(function (item, idx) {
      if (!item || typeof item !== "object") return;

      if (item.kind === "draft" && item.draft) {
        var draft = item.draft;
        var validator = item.validator || null;
        var policy = item.policy || null;
        var placeholderId = 'api-item-' + idx;

        containerHtml += '<div id="' + placeholderId + '">' +
                         '<p class="placeholder">Loading draft…</p>' +
                         '</div>';

        // Compute hash, then render into the placeholder.
        // We use a closure to capture the placeholderId and item data.
        canonical.draftHashAsync(draft)
          .then(function (hash) {
            if (responseGen !== messageGen) return;
            renderApiDraftInto(draft, hash, validator, policy, placeholderId);
          })
          .catch(function (err) {
            if (responseGen !== messageGen) return;
            var el = document.getElementById(placeholderId);
            if (el) el.innerHTML =
              '<p class="placeholder">Hash error: ' +
              escapeHtml(err.message) + "</p>";
          });
        draftCount++;
      } else if (item.kind === "question") {
        containerHtml += renderQuestionItem(item);
        questionCount++;
      }
    });

    // Set the container HTML. Draft placeholders will be filled
    // asynchronously as each hash completes.
    displayEl.innerHTML = containerHtml;

    if (draftCount === 0 && questionCount === 0) {
      // No recognizable items.
      renderApiError(200, body, sentTranscript,
        "Response items list contained no draft or question items.");
      return;
    }

    if (questionCount > 0) {
      setHint("Server asked " + questionCount +
              " clarifying question" + (questionCount > 1 ? "s" : "") +
              ". Draft signing and execution are disabled.");
    } else {
      setHint("Review the transaction details below. Signing and execution are disabled in this demo.");
    }
  }

  // Render a draft item from the API into a placeholder element.
  // Reuses renderServerDraft / renderServerAmbiguousPayee, which
  // already accept (draft, hash, validator, policy).
  function renderApiDraftInto(draft, hash, validator, policy, placeholderId) {
    // Build into a temporary approach: set the element's innerHTML.
    var el = document.getElementById(placeholderId);
    if (!el) return;

    // Save the current displayEl, render into a temp fragment,
    // then move the result into the placeholder.
    var hasUnresolved = draft.unresolved && draft.unresolved.length > 0;
    var isAmbiguous = hasUnresolved && draft.unresolved.indexOf("payee") !== -1;

    if (isAmbiguous && draft.payee_candidates) {
      renderServerAmbiguousPayeeInto(draft, hash, validator, policy, el);
    } else {
      renderServerDraftInto(draft, hash, validator, policy, el);
    }
  }

  // ── Render a "question" item ───────────────────────────────
  //
  // A question item means no draft exists yet. The server is
  // asking the user to clarify a field. We display the question
  // as text — we do NOT construct a draft or attempt to answer it.

  function renderQuestionItem(item) {
    var html = '<div class="card question-card">';
    html += '<div class="card-header">';
    html +=   '<span class="intent">Question</span>';
    html +=   '<span class="badge badge-warn">Needs input</span>';
    html += "</div>";
    html += '<div class="row">';
    html +=   '<span class="label">Field</span>';
    html +=   '<span class="value">' + escapeHtml(item.field || "—") + "</span>";
    html += "</div>";
    html += '<div class="question-text">' +
            escapeHtml(item.question || "") + "</div>";
    html += '<p class="hint">The server needs this field before a draft ' +
            'can be created. Answering will go through the chat input, ' +
            'not through a draft mutation.</p>';
    html += technicalDetailsQuestionHtml(item);
    html += disabledActionBar();
    html += "</div>";
    return html;
  }

  function technicalDetailsQuestionHtml(item) {
    var html = '<details class="tech-details">';
    html +=   '<summary>Technical details</summary>';
    html +=   '<div class="meta">';
    html +=     '<span>kind: question</span>';
    if (item.question_id) html += '<span>question_id: ' + escapeHtml(item.question_id) + '</span>';
    if (item.field) html += '<span>field: ' + escapeHtml(item.field) + '</span>';
    html +=   '</div>';
    html += '</details>';
    return html;
  }

  // ── Render API error ───────────────────────────────────────
  //
  // Handles: network failure (status 0), HTTP error (422 etc.),
  // or unexpected response shape. Always shows a clear message.

  function renderApiError(status, body, sentTranscript, extraMsg) {
    var html = '<div class="card error-card">';
    html += '<div class="card-header">';
    html +=   '<span class="intent">API Error</span>';
    html +=   '<span class="badge badge-danger">Failed</span>';
    html += "</div>";

    html += '<div class="row">';
    html +=   '<span class="label">Status</span>';
    html +=   '<span class="value mono">' +
              (status === 0 ? "Connection failed" : "HTTP " + status) +
              "</span>";
    html += "</div>";

    // Show the reason_code and message from a CONTRACT.md §5 rejection
    // if present.
    if (body && body.reason_code) {
      html += '<div class="row">';
      html +=   '<span class="label">Reason</span>';
      html +=   '<span class="value mono">' + escapeHtml(body.reason_code) + "</span>";
      html += "</div>";
    }
    if (body && body.message) {
      html += '<div class="row">';
      html +=   '<span class="label">Detail</span>';
      html +=   '<span class="value">' + escapeHtml(body.message) + "</span>";
      html += "</div>";
    }
    if (extraMsg) {
      html += '<div class="row">';
      html +=   '<span class="label">Error</span>';
      html +=   '<span class="value">' + escapeHtml(extraMsg) + "</span>";
      html += "</div>";
    }

    html += '<p class="hint">';
    if (status === 0) {
      html += 'Could not reach <code>/api/message</code>. The backend is ' +
              'not running on this origin, or the response was not valid ' +
              'JSON. Start the FastAPI server (Member 3) and make sure it ' +
              'serves <code>/api/message</code> on the same host and port ' +
              'as this page. Use the sample-scenario buttons below to ' +
              'preview fixture drafts in the meantime.';
    } else if (status === 404) {
      html += '<code>/api/message</code> returned 404. The endpoint is not ' +
              'implemented yet. Use the sample-scenario buttons below to ' +
              'preview fixture drafts.';
    } else if (status === 501 || status === 405) {
      html += 'The server does not handle <code>POST /api/message</code> ' +
              'yet (HTTP ' + status + '). A static file server is running, ' +
              'but it does not implement the API. Member 3 needs to ' +
              'implement the FastAPI backend with <code>POST /api/message</code>. ' +
              'Use the sample-scenario buttons below to preview fixture drafts.';
    } else if (status >= 500) {
      html += 'The server returned an error (HTTP ' + status + '). Check ' +
              'the backend logs and try again.';
    }
    html += '</p>';

    html += "</div>";
    displayEl.innerHTML = html;
    setHint("API call failed. See details above, or use the demo buttons below.");
  }

  // ── Render a server-returned draft ──────────────────────────
  //
  // Renders a draft received from POST /api/message along with its
  // validator verdict and policy decision (CONTRACT.md §4). The
  // draft is displayed exactly as received — never constructed or
  // mutated by the frontend.
  //
  // Signing and execution remain disabled. An unresolved or
  // policy-held draft is never treated as ready.

  function renderServerDraft(draft, hash, validator, policy) {
    var hasUnresolved = draft.unresolved && draft.unresolved.length > 0;
    var isAmbiguous = hasUnresolved && draft.unresolved.indexOf("payee") !== -1;

    // Route ambiguous-payee drafts to the clarification renderer.
    if (isAmbiguous && draft.payee_candidates) {
      renderServerAmbiguousPayee(draft, hash, validator, policy);
      return;
    }

    renderServerDraftInto(draft, hash, validator, policy, displayEl);
  }

  // Build the draft card HTML as a string, then set it into a
  // target element (either displayEl or a per-item placeholder).

  // ── Draft state classification ──────────────────────────────
  //
  // A single helper computes both the badge and the blocking flags
  // so they can never disagree.  The neutral "Draft received" badge
  // is shown ONLY when all three conditions are met:
  //
  //   1. unresolved is empty
  //   2. validator.verdict === "pass"   (exactly — not just "not freeze")
  //   3. policy.decision === "allow"   (exactly — not just "not hold")
  //
  // "freeze", a missing validator, or any unknown verdict is treated
  // as blocked.  A missing or unknown policy decision is treated as
  // not-allow.  This means policy "allow" alone never produces the
  // neutral badge — the validator must also be exactly "pass".

  function classifyDraft(validator, policy, hasUnresolved) {
    var vVerdict  = (validator && validator.verdict) ? validator.verdict : null;
    var vReason  = (validator && validator.reason)  ? validator.reason  : null;
    var pDecision = (policy && policy.decision) ? policy.decision : null;
    var pReason   = (policy && policy.reason)   ? policy.reason   : null;

    var validatorPassed  = vVerdict === "pass";
    var validatorFrozen  = vVerdict === "freeze";
    var validatorMissing = !vVerdict;
    var validatorUnknown = vVerdict && !validatorPassed && !validatorFrozen;

    var policyAllow   = pDecision === "allow";
    var policyMissing = !pDecision;

    var blocked = hasUnresolved ||
                  validatorFrozen ||
                  validatorMissing ||
                  validatorUnknown ||
                  !policyAllow; // covers policyHeld, policyMissing, unknown

    return {
      vVerdict: vVerdict,
      vReason: vReason,
      pDecision: pDecision,
      pReason: pReason,
      validatorPassed: validatorPassed,
      validatorFrozen: validatorFrozen,
      validatorMissing: validatorMissing,
      validatorUnknown: validatorUnknown,
      policyAllow: policyAllow,
      policyMissing: policyMissing,
      blocked: blocked
    };
  }

  // Customer-facing badge text.  We use plain language instead of
  // raw verdict strings where possible; the exact technical reason
  // is always shown in the details row and notice below the card.
  function draftBadgeHtml(st) {
    if (st.validatorFrozen) {
      return '<span class="badge badge-danger">Validation blocked</span>';
    }
    if (st.validatorMissing) {
      return '<span class="badge badge-danger">Validation unavailable</span>';
    }
    if (st.validatorUnknown) {
      return '<span class="badge badge-danger">Validation unclear</span>';
    }
    if (st.hasUnresolved) {
      return '<span class="badge badge-warn">Needs clarification</span>';
    }
    if (st.policyMissing) {
      return '<span class="badge badge-danger">Policy unavailable</span>';
    }
    if (!st.policyAllow) {
      return '<span class="badge badge-warn">Policy hold</span>';
    }
    // Only reached when: unresolved empty, validator "pass", policy "allow".
    return '<span class="badge badge-preview">Draft received</span>';
  }

  // Customer-facing notice text shown below the card body, explaining
  // why the draft is blocked.  Each blocking reason gets its own
  // paragraph so the customer sees a clear explanation.
  function draftBlockNoticeHtml(st) {
    var html = "";

    if (st.hasUnresolved) {
      html += '<div class="policy-hold">' +
              'This draft has unresolved fields. It cannot be signed or ' +
              'executed until the server resolves them.' +
              '</div>';
    }

    if (st.validatorFrozen) {
      html += '<div class="policy-hold">' +
              'Validation blocked: the validator returned a verdict of ' +
              '"freeze". The draft is not approved for signing or execution.' +
              (st.vReason ? ' Reason: ' + escapeHtml(st.vReason) + '.' : '') +
              '</div>';
    }

    if (st.validatorMissing) {
      html += '<div class="policy-hold">' +
              'Validation unavailable: no validator verdict was returned ' +
              'with this draft. The draft is not approved for signing or ' +
              'execution.' +
              '</div>';
    }

    if (st.validatorUnknown) {
      html += '<div class="policy-hold">' +
              'Validation unclear: the validator returned an unrecognized ' +
              'verdict ("' + escapeHtml(st.vVerdict) + '"). The draft is ' +
              'not approved for signing or execution.' +
              '</div>';
    }

    if (st.policyMissing && !st.validatorFrozen && !st.validatorMissing &&
        !st.validatorUnknown) {
      html += '<div class="policy-hold">' +
              'Policy unavailable: no policy decision was returned with ' +
              'this draft. The draft is not approved for signing or ' +
              'execution.' +
              '</div>';
    }

    if (!st.policyAllow && !st.policyMissing &&
        !st.validatorFrozen && !st.validatorMissing &&
        !st.validatorUnknown) {
      html += '<div class="policy-hold">' +
              'Policy decision is "' + escapeHtml(st.pDecision) +
              '". This draft is not approved and cannot be signed or ' +
              'executed until the policy engine returns "allow".' +
              '</div>';
    }

    return html;
  }

  function renderServerDraftInto(draft, hash, validator, policy, el) {
    var hasUnresolved = draft.unresolved && draft.unresolved.length > 0;
    var st = classifyDraft(validator, policy, hasUnresolved);
    st.hasUnresolved = hasUnresolved;

    var html = '<div class="card">';

    // Header
    html += '<div class="card-header">';
    html +=   '<span class="intent">' + escapeHtml(draft.intent_type) + "</span>";
    html += draftBadgeHtml(st);
    html += "</div>";

    // Validator + policy status (prominent, from server)
    html += statusRowsHtml(validator, policy);

    // Amount
    if (draft.amount) {
      html += '<div class="row amount-row">';
      html +=   '<span class="label">Amount</span>';
      html +=   '<span class="value">' + escapeHtml(draft.amount.value) +
                " " + escapeHtml(draft.amount.currency) + "</span>";
      html += "</div>";
    }

    // Payee
    if (draft.payee) {
      html += '<div class="row">';
      html +=   '<span class="label">Payee</span>';
      html +=   '<span class="value">' + escapeHtml(draft.payee.display_name) +
                "<br><span class=\"value mono\">" +
                escapeHtml(draft.payee.masked_account) + "</span></span>";
      html += "</div>";
    }

    // Equity fields
    if (draft.intent_type === "equity_purchase") {
      if (draft.ticker)        html += rowHtml("Ticker", draft.ticker, true);
      if (draft.notional_amount) html += rowHtml("Notional", draft.notional_amount, true);
      if (draft.order_type)    html += rowHtml("Order type", draft.order_type);
    }

    // Source account — ID only
    html += '<div class="row">';
    html +=   '<span class="label">Source account</span>';
    html +=   '<span class="value mono">' + escapeHtml(draft.source_account) + "</span>";
    html += "</div>";

    // Transcript
    html += '<div class="transcript">\u201c' + escapeHtml(draft.transcript) + '\u201d</div>';

    // Block notices (unresolved, validator, policy)
    html += draftBlockNoticeHtml(st);

    // Technical details
    html += technicalDetailsHtml(draft, hash);

    // Disabled actions
    html += disabledActionBar();

    html += "</div>";
    el.innerHTML = html;
    DctaVoice.attachReadback(el.querySelector(".card"), draft);
  }

  // Render an ambiguous-payee draft received from the API.
  // Reuses the same clarification UI as the fixture renderer, but
  // shows validator/policy status from the server response.
  function renderServerAmbiguousPayee(draft, hash, validator, policy) {
    renderServerAmbiguousPayeeInto(draft, hash, validator, policy, displayEl);
  }

  function renderServerAmbiguousPayeeInto(draft, hash, validator, policy, el) {
    var candidates = draft.payee_candidates || [];
    var draftId = draft.id || "";
    var candidateIds = candidates.map(function (c) { return c.id; }).join(",");
    // Per-card in-flight flag stored on the DOM element so each card
    // is fully independent — no module-level shared state.
    var cardUid = "card-" + Math.random().toString(36).slice(2, 10);

    var html = '<div class="card clarify-card" ' +
               'data-draft-id="' + escapeHtml(draftId) + '" ' +
               'data-card-uid="' + escapeHtml(cardUid) + '" ' +
               'data-candidate-ids="' + escapeHtml(candidateIds) + '" ' +
               'data-clarify-in-flight="false" ' +
               'data-message-gen="' + messageGen + '" ' +
               'data-clarify-seq="0">';

    html += '<div class="card-header">';
    html +=   '<span class="intent">' + escapeHtml(draft.intent_type) + "</span>";
    html +=   '<span class="badge badge-warn">Clarification needed</span>';
    html += "</div>";

    // Validator + policy status
    html += statusRowsHtml(validator, policy);

    // Amount
    html += '<div class="row amount-row">';
    html +=   '<span class="label">Amount</span>';
    html +=   '<span class="value">' + escapeHtml(draft.amount.value) +
              " " + escapeHtml(draft.amount.currency) + "</span>";
    html += "</div>";

    // Source account
    html += '<div class="row">';
    html +=   '<span class="label">Source account</span>';
    html +=   '<span class="value mono">' + escapeHtml(draft.source_account) + "</span>";
    html += "</div>";

    // Transcript
    html += '<div class="transcript">\u201c' + escapeHtml(draft.transcript) + '\u201d</div>';

    // Clarification prompt
    html += '<p class="clarify-prompt">Which payee did you mean?</p>';

    // Search field — uses class-based selectors, not id, so multiple
    // cards can coexist without ID collisions.
    html += '<div class="payee-search">';
    html +=   '<label class="input-label">Search offered payees</label>';
    html +=   '<input type="text" class="filter-input js-payee-filter" ' +
              'placeholder="Filter by name or account suffix…" ' +
              'autocomplete="off" />';
    html += '</div>';

    // Candidate list
    html += '<div class="clarify-list js-clarify-list">';
    html += renderCandidateButtons(candidates);
    html += '</div>';

    // No-match message
    html += '<p class="no-match js-no-match" hidden>No matching offered payee.</p>';

    // Clarify status area (loading / error messages)
    html += '<div class="clarify-status js-clarify-status" hidden></div>';

    // Confirm Selection button — submits the selected candidate id to
    // /api/clarify. Disabled until a candidate is selected.
    html += '<div class="clarify-actions">';
    html +=   '<button type="button" class="btn btn-primary clarify-confirm-btn js-clarify-confirm" disabled>';
    html +=     'Confirm selection';
    html +=   '</button>';
    html += '</div>';

    // Signing and execution remain disabled.
    html += disabledActionBar();

    html += "</div>";
    el.innerHTML = html;
    DctaVoice.attachReadback(el.querySelector(".card"), draft);

    // Wire all interactions scoped to THIS card only. The card root
    // is the .clarify-card element inside el.
    var cardEl = el.querySelector(".clarify-card");
    wireCandidateButtons(cardEl);
    wirePayeeFilter(cardEl);
    wireClarifyConfirmBtn(cardEl);
    DctaAmbiguity.attach(cardEl, candidates);
    setHint("Select the intended payee, then choose Confirm selection to update the draft.");
  }

  // Render validator verdict and policy decision rows.
  function statusRowsHtml(validator, policy) {
    var html = "";
    if (validator) {
      html += '<div class="row">';
      html +=   '<span class="label">Validator</span>';
      html +=   '<span class="value mono">' + escapeHtml(validator.verdict || "—") + "</span>";
      html += "</div>";
      if (validator.discrepancies && validator.discrepancies.length > 0) {
        html += '<div class="row">';
        html +=   '<span class="label">Discrepancies</span>';
        html +=   '<span class="value">' +
                  escapeHtml(validator.discrepancies.join("; ")) + "</span>";
        html += "</div>";
      }
    }
    if (policy) {
      html += '<div class="row">';
      html +=   '<span class="label">Policy</span>';
      var pText = escapeHtml(policy.decision || "—");
      if (policy.reason) pText += " (" + escapeHtml(policy.reason) + ")";
      html +=   '<span class="value">' + pText + "</span>";
      html += "</div>";
    }
    return html;
  }

  function setHint(msg) {
    if (inputHint) inputHint.textContent = msg;
  }

  // ── Fixture loader (demo fallback) ──────────────────────────

  function loadFixture(key) {
    DctaVoice.reset();
    // Restore text controls if a fixture supersedes an in-flight message.
    sendBtn.disabled = false;
    sendBtn.textContent = "Send";
    textInput.disabled = false;
    var url = FIXTURES[key];
    if (!url) return;
    // Fixture loads must never carry server draft state into clarify.
    // Increment the generation so any in-flight /api/clarify is stale.
    messageGen++;
    displayEl.innerHTML = '<p class="placeholder">Loading…</p>';
    var fixtureGen = messageGen;

    fetch(url)
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (draft) {
        if (fixtureGen !== messageGen) return;
        renderDraft(draft, key, fixtureGen);
      })
      .catch(function (err) {
        if (fixtureGen !== messageGen) return;
        displayEl.innerHTML =
          '<p class="placeholder">Failed to load fixture: ' +
          escapeHtml(err.message) + "</p>";
      });
    setHint("Loaded a local fixture (demo fallback). This is not a server response.");
  }

  // ── Renderers ───────────────────────────────────────────────

  function renderDraft(draft, key, fixtureGen) {
    // Recompute the hash using frontend/js/canonical.js (CONTRACT.md §7).
    // In this prototype the hash is shown for transparency — no signing
    // or execution is performed.
    canonical.draftHashAsync(draft)
      .then(function (hash) {
        if (fixtureGen !== messageGen) return;
        if (key === "ambiguous_payee") {
          renderAmbiguousPayee(draft, hash);
        } else if (key === "over_limit") {
          renderOverLimit(draft, hash);
        } else {
          renderTransferOrEquity(draft, hash, key);
        }
      })
      .catch(function (err) {
        if (fixtureGen !== messageGen) return;
        displayEl.innerHTML =
          '<p class="placeholder">Hash error: ' + escapeHtml(err.message) + "</p>";
      });
  }

  // clean_transfer and equity_purchase share the same card layout.
  function renderTransferOrEquity(draft, hash, key) {
    var isEquity = draft.intent_type === "equity_purchase";
    var html = '<div class="card">';

    // Header — every card is labelled "Fixture preview", never "ready".
    html += '<div class="card-header">';
    html +=   '<span class="intent">' + escapeHtml(draft.intent_type) + "</span>";
    html +=   '<span class="badge badge-preview">Fixture preview</span>';
    html += "</div>";

    // Expiry notice — fixtures expired 2026-10-03; do not imply signable.
    html += expiryNotice(draft);

    // Amount (prominent)
    html += '<div class="row amount-row">';
    html +=   '<span class="label">Amount</span>';
    html +=   '<span class="value">' + escapeHtml(draft.amount.value) +
              " " + escapeHtml(draft.amount.currency) + "</span>";
    html += "</div>";

    // Payee (transfer) or ticker (equity) — prominent
    if (draft.payee) {
      html += '<div class="row">';
      html +=   '<span class="label">Payee</span>';
      html +=   '<span class="value">' + escapeHtml(draft.payee.display_name) +
                "<br><span class=\"value mono\">" +
                escapeHtml(draft.payee.masked_account) + "</span></span>";
      html += "</div>";
    }
    if (isEquity) {
      html += rowHtml("Ticker", draft.ticker, true);
      html += rowHtml("Notional", draft.notional_amount, true);
      html += rowHtml("Order type", draft.order_type);
    }

    // Source account — show ID only, no display name invented.
    html += '<div class="row">';
    html +=   '<span class="label">Source account</span>';
    html +=   '<span class="value mono">' + escapeHtml(draft.source_account) + "</span>";
    html += "</div>";

    // Transcript
    html += '<div class="transcript">\u201c' + escapeHtml(draft.transcript) + '\u201d</div>';

    // Technical details (collapsible) — id, nonce, confidence, timestamps,
    // hash, and implementation notes.
    html += technicalDetailsHtml(draft, hash);

    // Disabled actions
    html += disabledActionBar();

    html += "</div>";
    displayEl.innerHTML = html;
  }

  function renderAmbiguousPayee(draft, hash) {
    // Store the original candidates at module level so the filter always
    // recalculates from the same immutable source (the fetched fixture
    // JSON, never mutated by the frontend).
    currentCandidates = draft.payee_candidates || [];

    var html = '<div class="card clarify-card">';

    // Header
    html += '<div class="card-header">';
    html +=   '<span class="intent">' + escapeHtml(draft.intent_type) + "</span>";
    html +=   '<span class="badge badge-preview">Fixture preview</span>';
    html += "</div>";

    // Expiry notice
    html += expiryNotice(draft);

    // Amount (prominent)
    html += '<div class="row amount-row">';
    html +=   '<span class="label">Amount</span>';
    html +=   '<span class="value">' + escapeHtml(draft.amount.value) +
              " " + escapeHtml(draft.amount.currency) + "</span>";
    html += "</div>";

    // Source account (prominent)
    html += '<div class="row">';
    html +=   '<span class="label">Source account</span>';
    html +=   '<span class="value mono">' + escapeHtml(draft.source_account) + "</span>";
    html += "</div>";

    // Transcript
    html += '<div class="transcript">\u201c' + escapeHtml(draft.transcript) + '\u201d</div>';

    // Clarification prompt
    html += '<p class="clarify-prompt">Which "John" did you mean?</p>';

    // ── Search field (filters only the offered payee_candidates) ──
    // Uses class-based selectors (no id) so it does not collide with
    // any server-rendered cards. The fixture card has NO Confirm
    // button and never calls /api/clarify.
    html += '<div class="payee-search">';
    html +=   '<label class="input-label">Search offered payees</label>';
    html +=   '<input type="text" class="filter-input js-payee-filter" ' +
              'placeholder="Filter by name or account suffix…" ' +
              'autocomplete="off" />';
    html += '</div>';

    // Candidate list — all buttons rendered ONCE and kept in the DOM.
    // The filter toggles the `hidden` attribute on existing buttons
    // rather than destroying and re-creating them. This avoids stale
    // state and listener issues on repeated search/clear cycles.
    html += '<div class="clarify-list js-clarify-list">';
    html += renderCandidateButtons(currentCandidates);
    html += '</div>';

    // No-match message (hidden until search yields zero results)
    html += '<p class="no-match js-no-match" hidden>' +
            'No matching offered payee.' +
            '</p>';

    // Note: selecting a candidate does NOT mutate the draft.
    // In the real system, /api/clarify returns a NEW draft.
    html += '<p class="hint">' +
            'Selecting a candidate here is a UI preview only — it does not ' +
            'mutate the draft or submit a clarification. In the real system, ' +
            '<code>/api/clarify</code> returns a new server-created draft with ' +
            'a new id and nonce — the old draft stays unresolved and can ' +
            'never be executed.' +
            '</p>';

    // DEV NOTE (code comment only — not rendered on the customer-facing page):
    // A future enhancement may show "Did you mean the John you transfer to
    // most often?" followed by "View other Johns". The current draft schema
    // does not provide transaction frequency or a recommended-payee marker.
    // The server would need to supply this ranking information; the frontend
    // must not invent it or auto-select a payee.

    // Technical details
    html += technicalDetailsHtml(draft, hash);
    html += disabledActionBar();

    html += "</div>";
    displayEl.innerHTML = html;

    // Wire candidate buttons scoped to displayEl (fixture is single-card).
    // No Confirm button is rendered and no /api/clarify call is possible.
    wireCandidateButtons(displayEl);
    wirePayeeFilter(displayEl);
  }

  function renderOverLimit(draft, hash) {
    var html = '<div class="card">';

    // Header
    html += '<div class="card-header">';
    html +=   '<span class="intent">' + escapeHtml(draft.intent_type) + "</span>";
    html +=   '<span class="badge badge-warn">Policy review pending</span>';
    html += "</div>";

    // Expiry notice
    html += expiryNotice(draft);

    // Amount (prominent)
    html += '<div class="row amount-row">';
    html +=   '<span class="label">Amount</span>';
    html +=   '<span class="value">' + escapeHtml(draft.amount.value) +
              " " + escapeHtml(draft.amount.currency) + "</span>";
    html += "</div>";

    // Payee (prominent)
    if (draft.payee) {
      html += '<div class="row">';
      html +=   '<span class="label">Payee</span>';
      html +=   '<span class="value">' + escapeHtml(draft.payee.display_name) +
                "<br><span class=\"value mono\">" +
                escapeHtml(draft.payee.masked_account) + "</span></span>";
      html += "</div>";
    }

    // Source account (prominent)
    html += '<div class="row">';
    html +=   '<span class="label">Source account</span>';
    html +=   '<span class="value mono">' + escapeHtml(draft.source_account) + "</span>";
    html += "</div>";

    // Transcript
    html += '<div class="transcript">\u201c' + escapeHtml(draft.transcript) + '\u201d</div>';

    // Policy hold notice — do NOT label approved, do NOT allow signing.
    html += '<div class="policy-hold">' +
            'Draft exists but policy approval is pending. ' +
            'This draft is NOT approved and cannot be signed or executed ' +
            'based only on the fixture. The policy engine (server-side, ' +
            'deterministic, no LLM) must return <code>allow</code> before ' +
            'signing can proceed.' +
            '</div>';

    // Technical details
    html += technicalDetailsHtml(draft, hash);
    html += disabledActionBar();

    html += "</div>";
    displayEl.innerHTML = html;
  }

  // ── Candidate buttons (real <button> elements) ──────────────

  function renderCandidateButtons(candidates) {
    if (candidates.length === 0) return '';
    var html = '';
    candidates.forEach(function (c) {
      html += '<button type="button" class="candidate-btn" ' +
              'data-candidate-id="' + escapeHtml(c.id) + '" ' +
              'data-display="' + escapeHtml(c.display_name) + '" ' +
              'data-masked="' + escapeHtml(c.masked_account) + '" ' +
              'aria-pressed="false">';
      html +=   '<span class="candidate-name">' + escapeHtml(c.display_name) + '</span>';
      html +=   '<span class="candidate-acct">' + escapeHtml(c.masked_account) + '</span>';
      html +=   '<span class="candidate-check" aria-hidden="true">✓</span>';
      html += '</button>';
    });
    return html;
  }

  // Wire candidate buttons scoped to a specific card root element.
  // This ensures that when multiple ambiguous-payee cards are on
  // screen, clicking a candidate on card A only affects card A's
  // selection state and confirm button — not card B's.
  function wireCandidateButtons(scopeEl) {
    var root = scopeEl || displayEl;
    var buttons = root.querySelectorAll(".candidate-btn");
    // Use Array.prototype.forEach for NodeList compatibility.
    Array.prototype.forEach.call(buttons, function (btn) {
      // click works for mouse and touch; button natively receives
      // keyboard Enter/Space, so no separate keydown handler needed.
      btn.addEventListener("click", function () {
        // Deselect all candidates within THIS card only.
        Array.prototype.forEach.call(buttons, function (b) {
          b.classList.remove("selected");
          b.setAttribute("aria-pressed", "false");
          b.querySelector(".candidate-check").style.display = "none";
        });
        btn.classList.add("selected");
        btn.setAttribute("aria-pressed", "true");
        btn.querySelector(".candidate-check").style.display = "inline";

        // Enable this card's Confirm button only.
        var confirmBtn = root.querySelector(".js-clarify-confirm");
        if (confirmBtn) confirmBtn.disabled = false;
      });
    });
  }

  // ── Payee search filter ──────────────────────────────────────
  //
  // The filter toggles the `hidden` attribute on existing <button>
  // elements. It NEVER destroys or re-creates buttons (the old
  // approach replaced list.innerHTML on every keystroke, which
  // could leave stale state). Because the buttons persist in the
  // DOM, their event listeners stay attached and there is no risk
  // of an invisible selected candidate after clearing the search.
  //
  // The filter always recalculates from `currentCandidates` — the
  // original, immutable payee_candidates from the fetched fixture
  // JSON. The frontend never modifies this array.

  // Wire the payee search filter scoped to a specific card root.
  // Multiple cards can each have their own filter without interference.
  function wirePayeeFilter(scopeEl) {
    var root = scopeEl || displayEl;
    var input = root.querySelector(".js-payee-filter");
    if (!input) return;

    input.addEventListener("input", function () {
      var query = input.value.trim().toLowerCase();
      var list = root.querySelector(".js-clarify-list");
      var noMatch = root.querySelector(".js-no-match");
      if (!list || !noMatch) return;

      var visibleCount = 0;

      // Iterate over the button elements that already exist in the DOM.
      // Each button was created by renderCandidateButtons and carries
      // data-* attributes with the original candidate values.
      var buttons = list.querySelectorAll(".candidate-btn");
      Array.prototype.forEach.call(buttons, function (btn) {
        var display = btn.getAttribute("data-display") || "";
        var masked  = btn.getAttribute("data-masked") || "";

        var matches = false;
        if (!query) {
          // Empty search → show everything.
          matches = true;
        } else {
          // Match against display_name (case-insensitive) or the
          // masked_account suffix (digits after the asterisks).
          var nameMatch = display.toLowerCase().indexOf(query) !== -1;
          var suffix = masked.replace(/[\s*]/g, "");
          var suffixMatch = suffix.toLowerCase().indexOf(query) !== -1;
          matches = nameMatch || suffixMatch;
        }

        if (matches) {
          btn.hidden = false;
          visibleCount++;
        } else {
          btn.hidden = true;
          // Clear selection on a now-hidden button so no invisible
          // selected candidate persists after the filter changes.
          var wasSelected = btn.classList.contains("selected");
          btn.classList.remove("selected");
          btn.setAttribute("aria-pressed", "false");
          var check = btn.querySelector(".candidate-check");
          if (check) check.style.display = "none";
          // If the selected candidate was hidden, disable this card's Confirm.
          if (wasSelected) {
            var cb = root.querySelector(".js-clarify-confirm");
            if (cb) cb.disabled = true;
          }
        }
      });

      if (visibleCount === 0) {
        list.hidden = true;
        noMatch.hidden = false;
      } else {
        list.hidden = false;
        noMatch.hidden = true;
      }
    });
  }

  // ── Technical details (collapsible) ─────────────────────────

  // ── Payee clarification submission (CONTRACT.md §4) ──────────
  //
  // Submits the selected candidate id to POST /api/clarify with
  //   { draft_id, field: "payee", answer: selectedCandidateId }.
  //
  // All state is per-card — no module-level shared variables. The
  // card root element (a .clarify-card <div>) carries:
  //   data-draft-id      — the server draft id to submit
  //   data-card-uid      — unique card identifier
  //   data-candidate-ids — comma-separated offered candidate ids
  //   data-clarify-in-flight — per-card duplicate-submission guard
  //   data-message-gen   — the messageGen value when this card was
  //                         rendered (used to detect superseded displays)
  //   data-clarify-seq   — per-card monotonic counter for this card's
  //                         own /api/clarify requests
  //
  // Guards:
  // - Only fires if the card has a data-draft-id (server draft, not fixture).
  // - data-clarify-in-flight on the card prevents duplicate submissions
  //   for THAT card only — other cards remain interactive.
  // - The selected candidate id must be one of the card's offered
  //   payee_candidates (validated by the server, but we check locally too).
  // - messageGen (global) guards against stale /api/clarify responses
  //   arriving after the user has sent a new /api/message — the entire
  //   display has been replaced.
  // - data-clarify-seq (per-card) guards against stale /api/clarify
  //   responses for THIS card only. Confirming card A does NOT
  //   invalidate card B's in-flight clarify response.

  function wireClarifyConfirmBtn(scopeEl) {
    var root = scopeEl || displayEl;
    var btn = root.querySelector(".js-clarify-confirm");
    if (!btn) return;
    btn.addEventListener("click", function () {
      submitClarify(root);
    });
  }

  function submitClarify(cardEl) {
    if (!cardEl) return;
    var inFlight = cardEl.getAttribute("data-clarify-in-flight") === "true";
    if (inFlight) return;

    var draftId = cardEl.getAttribute("data-draft-id") || "";
    if (!draftId) {
      showClarifyError(cardEl, "No server draft is currently displayed. " +
                       "Fixture previews cannot be submitted to /api/clarify.");
      return;
    }

    // Find the selected candidate within THIS card only.
    var selectedBtn = cardEl.querySelector(".candidate-btn.selected");
    if (!selectedBtn) {
      showClarifyError(cardEl, "Select a payee candidate before confirming.");
      return;
    }
    var answerId = selectedBtn.getAttribute("data-candidate-id") || "";

    // Local sanity: answer must be one of the offered candidates.
    var candidateIdsStr = cardEl.getAttribute("data-candidate-ids") || "";
    var candidateIds = candidateIdsStr ? candidateIdsStr.split(",") : [];
    var isValid = candidateIds.indexOf(answerId) !== -1;
    if (!isValid) {
      showClarifyError(cardEl, "The selected candidate is not among the offered payee_candidates.");
      return;
    }

    // Per-card in-flight flag.
    cardEl.setAttribute("data-clarify-in-flight", "true");

    var confirmBtn = cardEl.querySelector(".js-clarify-confirm");
    var candidateBtns = cardEl.querySelectorAll(".candidate-btn");
    var filterInput = cardEl.querySelector(".js-payee-filter");

    // Loading state: disable all interactive elements in THIS card.
    if (confirmBtn) {
      confirmBtn.disabled = true;
      confirmBtn.textContent = "Submitting…";
    }
    Array.prototype.forEach.call(candidateBtns, function (b) {
      b.disabled = true;
    });
    if (filterInput) filterInput.disabled = true;

    showClarifyLoading(cardEl, "Submitting clarification to /api/clarify…");

    // Per-card clarify sequence: independent per card. Confirming
    // card A does NOT increment card B's counter, so card B's
    // in-flight clarify response remains valid.
    var myCardSeq = (parseInt(cardEl.getAttribute("data-clarify-seq") || "0", 10) + 1);
    cardEl.setAttribute("data-clarify-seq", String(myCardSeq));

    // Capture the message generation at the time of this request.
    // If the user sends a new /api/message while this request is
    // in-flight, messageGen will increment and we discard this
    // response — the entire display has been replaced.
    var myGen = parseInt(cardEl.getAttribute("data-message-gen") || "0", 10);

    fetch(API_CLARIFY_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        draft_id: draftId,
        field: "payee",
        answer: answerId,
      }),
    })
      .then(function (res) {
        return res.json()
          .then(function (body) {
            return { ok: res.ok, status: res.status, body: body };
          })
          .catch(function () {
            return { ok: res.ok, status: res.status, body: null };
          });
      })
      .then(function (result) {
        // Stale-response guards (both must pass):
        // 1. messageGen mismatch → a new /api/message has replaced
        //    the entire display. This card no longer exists in the DOM.
        // 2. clarify-seq mismatch → a newer /api/clarify request has
        //    been issued for THIS card (duplicate submission guard).
        if (myGen !== messageGen) return;
        if (myCardSeq !== parseInt(cardEl.getAttribute("data-clarify-seq") || "0", 10)) return;

        if (result.ok) {
          // Clear this card's in-flight flag and replace ONLY this
          // card's content with the server's new items-list response.
          // The new draft has a new id and nonce — the old one is
          // never reused. Other cards on screen are untouched.
          cardEl.setAttribute("data-clarify-in-flight", "false");
          renderClarifySuccessInto(result.body, cardEl);
        } else {
          cardEl.setAttribute("data-clarify-in-flight", "false");
          showClarifyRejection(cardEl, result.status, result.body);
          restoreClarifyControls(cardEl);
        }
      })
      .catch(function (err) {
        if (myGen !== messageGen) return;
        if (myCardSeq !== parseInt(cardEl.getAttribute("data-clarify-seq") || "0", 10)) return;
        cardEl.setAttribute("data-clarify-in-flight", "false");
        showClarifyError(cardEl, "Could not reach /api/clarify: " + err.message);
        restoreClarifyControls(cardEl);
      });
  }

  // Render the server's items-list response from /api/clarify into the
  // specific card element that was clarified. This replaces ONLY that
  // card's content — other cards on screen are untouched. The server
  // returns a new draft with a new id and nonce.
  function renderClarifySuccessInto(body, cardEl) {
    DctaVoice.reset();
    var parsed = parseMessageResponse(body);
    if (!parsed) {
      cardEl.innerHTML =
        '<p class="placeholder">Clarification succeeded but the response ' +
        'did not contain an items list.</p>';
      return;
    }

    var items = parsed.items;
    var html = "";

    items.forEach(function (item, idx) {
      if (!item || typeof item !== "object") return;

      if (item.kind === "draft" && item.draft) {
        var draft = item.draft;
        var validator = item.validator || null;
        var policy = item.policy || null;
        // For clarify success, the draft should be resolved (no
        // unresolved payee). Render it as a normal draft card.
        html += '<div class="api-item-inner" data-item-idx="' + idx + '">' +
                '<p class="placeholder">Loading draft…</p></div>';
        // Compute hash and render into the placeholder asynchronously.
        // We use a unique marker so the closure can find the right div.
        var marker = 'data-item-idx="' + idx + '"';
        canonical.draftHashAsync(draft)
          .then(function (hash) {
            var inner = cardEl.querySelector('[' + marker + ']');
            if (!inner) return;
            renderServerDraftInto(draft, hash, validator, policy, inner);
            DctaVoice.attachDraftReadback(inner.querySelector(".card"), draft, true);
          })
          .catch(function (err) {
            var inner = cardEl.querySelector('[' + marker + ']');
            if (inner) inner.innerHTML =
              '<p class="placeholder">Hash error: ' +
              escapeHtml(err.message) + "</p>";
          });
      } else if (item.kind === "question") {
        html += renderQuestionItem(item);
      }
    });

    cardEl.innerHTML = html;
    setHint("Your selection was applied. Review the updated transaction details below.");
  }

  // Restore candidate buttons, filter, and confirm button after a
  // failed clarification (422 or network error) — scoped to one card.
  function restoreClarifyControls(cardEl) {
    var confirmBtn = cardEl.querySelector(".js-clarify-confirm");
    var candidateBtns = cardEl.querySelectorAll(".candidate-btn");
    var filterInput = cardEl.querySelector(".js-payee-filter");
    if (confirmBtn) {
      confirmBtn.textContent = "Confirm selection";
      // Re-enable only if a candidate is still selected.
      var hasSelected = cardEl.querySelector(".candidate-btn.selected");
      confirmBtn.disabled = !hasSelected;
    }
    Array.prototype.forEach.call(candidateBtns, function (b) {
      b.disabled = false;
    });
    if (filterInput) filterInput.disabled = false;
  }

  function showClarifyLoading(cardEl, msg) {
    var el = cardEl.querySelector(".js-clarify-status");
    if (!el) return;
    el.hidden = false;
    el.className = "clarify-status clarify-loading";
    el.innerHTML = escapeHtml(msg);
  }

  function showClarifyError(cardEl, msg) {
    var el = cardEl.querySelector(".js-clarify-status");
    if (!el) return;
    el.hidden = false;
    el.className = "clarify-status clarify-error";
    el.innerHTML = escapeHtml(msg);
  }

  function showClarifyRejection(cardEl, status, body) {
    var el = cardEl.querySelector(".js-clarify-status");
    if (!el) return;
    el.hidden = false;
    el.className = "clarify-status clarify-error";
    var msg = "Clarification rejected (HTTP " + status + ")";
    if (body && body.reason_code) {
      msg += " — " + body.reason_code;
    }
    if (body && body.message) {
      msg += ": " + body.message;
    }
    el.innerHTML = escapeHtml(msg);
    setHint("Clarification rejected. Select a different candidate or try again.");
  }

  function technicalDetailsHtml(draft, hash) {
    var html = '<details class="tech-details">';
    html +=   '<summary>Technical details</summary>';
    html +=   '<div class="meta">';
    html +=     '<span>id: ' + escapeHtml(draft.id) + '</span>';
    html +=     '<span>nonce: ' + escapeHtml(draft.nonce) + '</span>';
    html +=     '<span>confidence: ' + String(draft.confidence) + '</span>';
    html +=     '<span>created: ' + escapeHtml(draft.created_at) + '</span>';
    html +=     '<span>expires: ' + escapeHtml(draft.expires_at) + '</span>';
    html +=     '<span class="hash-line">hash: ' + escapeHtml(hash) + '</span>';
    html +=   '</div>';
    html +=   '<div class="acct-note">' +
              'Account ID shown as-is. An account-display API or mapping ' +
              'is still needed — do not fabricate a display name.' +
              '</div>';
    html += '</details>';
    return html;
  }

  // ── Helpers ─────────────────────────────────────────────────

  function expiryNotice(draft) {
    return '<div class="expiry-notice">' +
           'This is a sample fixture that expired on ' +
           escapeHtml(draft.expires_at) + '. It is shown for preview only — ' +
           'it cannot be signed or executed.' +
           '</div>';
  }

  function rowHtml(label, value, mono) {
    return '<div class="row">' +
           '<span class="label">' + escapeHtml(label) + "</span>" +
           '<span class="value' + (mono ? " mono" : "") + '">' +
           escapeHtml(String(value)) + "</span>" +
           "</div>";
  }

  function disabledActionBar() {
    return '<div class="action-bar">' +
           '<button class="btn btn-action" disabled>Sign (disabled)</button>' +
           '<button class="btn btn-action" disabled>Execute (disabled)</button>' +
           "</div>";
  }

  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // ── Boot ───────────────────────────────────────────────────
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    // DOM already parsed (script at end of <body>)
    init();
  }
})();
