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

  // API endpoint for the message parser (CONTRACT.md §4).
  // Relative to frontend/index.html → /api/message on the same origin.
  var API_MESSAGE_URL = "/api/message";

  // Fixture filenames relative to frontend/index.html.
  // Used only by the demo-fallback scenario buttons.
  var FIXTURES = {
    clean_transfer:   "../fixtures/drafts/clean_transfer.json",
    ambiguous_payee:  "../fixtures/drafts/ambiguous_payee.json",
    equity_purchase:  "../fixtures/drafts/equity_purchase.json",
    over_limit:       "../fixtures/drafts/over_limit.json",
  };

  var displayEl = null;
  var scenarioBtns = null;
  var textInput = null;
  var sendBtn = null;
  var inputHint = null;

  // Module-level reference to the current draft's payee_candidates.
  var currentCandidates = [];

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
    if (!textInput || !sendBtn) return;

    var transcript = textInput.value.trim();
    if (!transcript) {
      setHint("Please type a message before sending.");
      return;
    }

    // Clear any previous scenario-button highlight.
    Array.prototype.forEach.call(scenarioBtns, function (b) {
      b.classList.remove("active");
    });

    // Loading state.
    sendBtn.disabled = true;
    sendBtn.textContent = "Sending…";
    textInput.disabled = true;
    setHint("Sending to /api/message…");
    displayEl.innerHTML = '<p class="placeholder">Loading…</p>';

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
        if (result.ok) {
          renderApiResponse(result.body, transcript);
        } else {
          renderApiError(result.status, result.body, transcript);
        }
      })
      .catch(function (err) {
        // Network failure, connection refused, CORS, or JSON parse error.
        renderApiError(0, null, transcript, err.message);
      })
      .then(function () {
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

    // Build a container for all rendered items.
    var containerHtml = '';

    // Summary line: how many intents were detected.
    if (intents === 1) {
      containerHtml += '<p class="items-summary">1 intent detected.</p>';
    } else {
      containerHtml += '<p class="items-summary">' + escapeHtml(String(intents)) +
                       ' intents detected.</p>';
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
            renderApiDraftInto(draft, hash, validator, policy, placeholderId);
          })
          .catch(function (err) {
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
      setHint("Draft received from /api/message. Signing and execution are disabled.");
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
  function renderServerDraftInto(draft, hash, validator, policy, el) {
    var hasUnresolved = draft.unresolved && draft.unresolved.length > 0;
    var policyHeld = policy && policy.decision !== "allow";
    var validatorFrozen = validator && validator.verdict === "frozen";
    var blocked = hasUnresolved || policyHeld || validatorFrozen;

    var html = '<div class="card">';

    // Header
    html += '<div class="card-header">';
    html +=   '<span class="intent">' + escapeHtml(draft.intent_type) + "</span>";
    if (blocked) {
      html += '<span class="badge badge-warn">Not ready</span>';
    } else {
      html += '<span class="badge badge-preview">Draft received</span>';
    }
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

    // Unresolved notice
    if (hasUnresolved) {
      html += '<div class="policy-hold">' +
              'This draft has unresolved fields: ' +
              escapeHtml(draft.unresolved.join(", ")) +
              '. It cannot be signed or executed until the server ' +
              'resolves them.' +
              '</div>';
    }

    // Validator frozen notice
    if (validatorFrozen) {
      html += '<div class="policy-hold">' +
              'The validator returned a verdict of "frozen" — the ' +
              'transcript does not match the draft. This draft cannot ' +
              'be signed or executed.' +
              '</div>';
    }

    // Policy hold notice
    if (policyHeld && !validatorFrozen) {
      html += '<div class="policy-hold">' +
              'Policy decision is "' + escapeHtml(policy.decision) +
              '". This draft is not approved and cannot be signed or ' +
              'executed until the policy engine returns "allow".' +
              '</div>';
    }

    // Technical details
    html += technicalDetailsHtml(draft, hash);

    // Disabled actions
    html += disabledActionBar();

    html += "</div>";
    el.innerHTML = html;
  }

  // Render an ambiguous-payee draft received from the API.
  // Reuses the same clarification UI as the fixture renderer, but
  // shows validator/policy status from the server response.
  function renderServerAmbiguousPayee(draft, hash, validator, policy) {
    renderServerAmbiguousPayeeInto(draft, hash, validator, policy, displayEl);
  }

  function renderServerAmbiguousPayeeInto(draft, hash, validator, policy, el) {
    currentCandidates = draft.payee_candidates || [];

    var html = '<div class="card clarify-card">';

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

    // Search field
    html += '<div class="payee-search">';
    html +=   '<label for="payee-filter" class="input-label">Search offered payees</label>';
    html +=   '<input type="text" id="payee-filter" class="filter-input" ' +
              'placeholder="Filter by name or account suffix…" ' +
              'autocomplete="off" />';
    html += '</div>';

    // Candidate list
    html += '<div class="clarify-list" id="clarify-list">';
    html += renderCandidateButtons(currentCandidates);
    html += '</div>';

    // No-match message
    html += '<p class="no-match" id="no-match" hidden>No matching offered payee.</p>';

    // Note: selecting a candidate here is UI preview only.
    // In the real system, /api/clarify returns a NEW draft.
    html += '<p class="hint">' +
            'Selecting a candidate is a UI preview only — it does not ' +
            'mutate the draft or submit a clarification. In the real ' +
            'system, <code>/api/clarify</code> returns a new server-created ' +
            'draft with a new id and nonce.' +
            '</p>';

    // Technical details
    html += technicalDetailsHtml(draft, hash);
    html += disabledActionBar();

    html += "</div>";
    el.innerHTML = html;

    wireCandidateButtons();
    wirePayeeFilter();
    setHint("Draft received from /api/message. Selecting a payee is preview only.");
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
    var url = FIXTURES[key];
    if (!url) return;
    displayEl.innerHTML = '<p class="placeholder">Loading…</p>';

    fetch(url)
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (draft) {
        renderDraft(draft, key);
      })
      .catch(function (err) {
        displayEl.innerHTML =
          '<p class="placeholder">Failed to load fixture: ' +
          escapeHtml(err.message) + "</p>";
      });
    setHint("Loaded a local fixture (demo fallback). This is not a server response.");
  }

  // ── Renderers ───────────────────────────────────────────────

  function renderDraft(draft, key) {
    // Recompute the hash using frontend/js/canonical.js (CONTRACT.md §7).
    // In this prototype the hash is shown for transparency — no signing
    // or execution is performed.
    canonical.draftHashAsync(draft)
      .then(function (hash) {
        if (key === "ambiguous_payee") {
          renderAmbiguousPayee(draft, hash);
        } else if (key === "over_limit") {
          renderOverLimit(draft, hash);
        } else {
          renderTransferOrEquity(draft, hash, key);
        }
      })
      .catch(function (err) {
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
    html += '<div class="payee-search">';
    html +=   '<label for="payee-filter" class="input-label">Search offered payees</label>';
    html +=   '<input type="text" id="payee-filter" class="filter-input" ' +
              'placeholder="Filter by name or account suffix…" ' +
              'autocomplete="off" />';
    html += '</div>';

    // Candidate list — all buttons rendered ONCE and kept in the DOM.
    // The filter toggles the `hidden` attribute on existing buttons
    // rather than destroying and re-creating them. This avoids stale
    // state and listener issues on repeated search/clear cycles.
    html += '<div class="clarify-list" id="clarify-list">';
    html += renderCandidateButtons(currentCandidates);
    html += '</div>';

    // No-match message (hidden until search yields zero results)
    html += '<p class="no-match" id="no-match" hidden>' +
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

    // Wire candidate buttons (keyboard + mouse + touch)
    wireCandidateButtons();
    // Wire search filter — reads from currentCandidates, not a closure
    // param, so it always uses the original immutable list.
    wirePayeeFilter();
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

  function wireCandidateButtons() {
    var buttons = displayEl.querySelectorAll(".candidate-btn");
    // Use Array.prototype.forEach for NodeList compatibility.
    Array.prototype.forEach.call(buttons, function (btn) {
      // click works for mouse and touch; button natively receives
      // keyboard Enter/Space, so no separate keydown handler needed.
      btn.addEventListener("click", function () {
        Array.prototype.forEach.call(buttons, function (b) {
          b.classList.remove("selected");
          b.setAttribute("aria-pressed", "false");
          b.querySelector(".candidate-check").style.display = "none";
        });
        btn.classList.add("selected");
        btn.setAttribute("aria-pressed", "true");
        btn.querySelector(".candidate-check").style.display = "inline";
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

  function wirePayeeFilter() {
    var input = displayEl.querySelector("#payee-filter");
    if (!input) return;

    input.addEventListener("input", function () {
      var query = input.value.trim().toLowerCase();
      var list = displayEl.querySelector("#clarify-list");
      var noMatch = displayEl.querySelector("#no-match");
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
          btn.classList.remove("selected");
          btn.setAttribute("aria-pressed", "false");
          var check = btn.querySelector(".candidate-check");
          if (check) check.style.display = "none";
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
