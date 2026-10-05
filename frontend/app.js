/* ─────────────────────────────────────────────────────────────
 * DCTA frontend prototype — app.js
 *
 * Fixture-only, view-only. No live API calls. No draft construction
 * or mutation in the frontend (CONTEXT.md core rule + constraint 1).
 * WebAuthn signing and execution are deliberately disabled.
 *
 * All fixtures expired on 2026-10-03. The page is a fixture preview,
 * not a signable interface — no draft is ever presented as "ready".
 * ───────────────────────────────────────────────────────────── */

(function () {
  "use strict";

  // Fixture filenames relative to frontend/index.html.
  // The server does not exist yet, so we load the shared JSON fixtures
  // directly. These are read-only — the frontend never edits them.
  var FIXTURES = {
    clean_transfer:   "../fixtures/drafts/clean_transfer.json",
    ambiguous_payee:  "../fixtures/drafts/ambiguous_payee.json",
    equity_purchase:  "../fixtures/drafts/equity_purchase.json",
    over_limit:       "../fixtures/drafts/over_limit.json",
  };

  var displayEl = null;
  var scenarioBtns = null;

  // Module-level reference to the current draft's payee_candidates.
  // The filter always recalculates from this immutable source — it never
  // reads from the DOM, so clearing the search always restores the full
  // original set without stale state.
  var currentCandidates = [];

  // ── Initialization ─────────────────────────────────────────
  // Wrap in DOMContentLoaded so the DOM is fully parsed before we
  // query elements. The <script> tags are at the end of <body>, so
  // the DOM should be ready — but this guards against edge cases
  // and makes the dependency explicit.
  function init() {
    displayEl = document.getElementById("draft-display");
    scenarioBtns = document.querySelectorAll(".btn-scenario");
    if (!displayEl || !scenarioBtns.length) {
      console.error("[app.js] Could not find #draft-display or .btn-scenario elements");
      return;
    }

    // ── Event wiring ──────────────────────────────────────────
    // Use Array.prototype.forEach for NodeList compatibility
    // (NodeList.forEach is not available in all browsers).
    Array.prototype.forEach.call(scenarioBtns, function (btn) {
      btn.addEventListener("click", function () {
        var key = btn.getAttribute("data-scenario");
        setActiveButton(btn);
        loadFixture(key);
      });
    });
  }

  function setActiveButton(active) {
    Array.prototype.forEach.call(scenarioBtns, function (b) {
      b.classList.remove("active");
    });
    active.classList.add("active");
  }

  // ── Fixture loader ──────────────────────────────────────────

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
