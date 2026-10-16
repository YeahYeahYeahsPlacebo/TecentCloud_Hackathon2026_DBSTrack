/* Spoken questions use offered candidates; clarification is explicit tap + Confirm. */
(function (global) {
  "use strict";
  function suffix(candidate) {
    var match = String(candidate.masked_account || "").match(/(\d+)\s*$/);
    return match ? match[1] : "";
  }
  function label(candidate) {
    return candidate.display_name + (suffix(candidate) ? " ending in " + suffix(candidate) : "");
  }
  function question(candidates) {
    var names = candidates.map(label);
    if (!names.length) return "Which payee did you mean?";
    var joined = names.length < 2 ? names[0] : names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
    return "I found " + joined + ". Which one did you mean?";
  }
  function attach(card, candidates) {
    var prompt = card.querySelector(".clarify-prompt");
    prompt.textContent = question(candidates);
    var controls = document.createElement("div");
    controls.className = "ambiguity-voice";
    controls.innerHTML = '<p class="hint">Tap an offered payee, review the selection, then click Confirm selection.</p>' +
      '<p class="hint js-answer-selected" role="status">Selected: none.</p>';
    prompt.after(controls);
    var selected = controls.querySelector(".js-answer-selected");
    var buttons = card.querySelectorAll(".candidate-btn");
    function showSelection() {
      var button = card.querySelector(".candidate-btn.selected");
      var candidate = button && candidates.find(function (candidate) { return candidate.id === button.getAttribute("data-candidate-id"); });
      selected.textContent = candidate ? "Selected: " + label(candidate) + ". Review, then click Confirm selection." : "Selected: none.";
    }
    Array.prototype.forEach.call(buttons, function (button) { button.addEventListener("click", showSelection); });
    card.querySelector(".js-payee-filter").addEventListener("input", showSelection);

  }
  global.DctaAmbiguity = { attach: attach, question: question };
})(window);
