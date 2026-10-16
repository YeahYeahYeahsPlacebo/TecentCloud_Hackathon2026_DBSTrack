/* Only offered candidates can be selected. Recognition never submits clarification. */
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
  function normalise(text) {
    return String(text).normalize("NFKC").toLocaleLowerCase().replace(/[^\p{L}\p{N}\s]/gu, " ").replace(/\s+/g, " ").trim();
  }
  function matchAnswer(text, candidates) {
    var words = normalise(text).replace(/^(?:i meant|i mean|i want|please choose|please select|choose|select|use)\s+/, "");
    // Only these optional carrier words are accepted. Negation, alternatives,
    // unknown names and incomplete suffixes cannot accidentally select a payee.
    words = words.replace(/^the one(?: ending(?: in)?)?\s+/, "").replace(/\b(?:the|account|ending|in|please)\b/g, " ").trim();
    var digits = { zero: "0", oh: "0", one: "1", two: "2", three: "3", four: "4", five: "5", six: "6", seven: "7", eight: "8", nine: "9" };
    var tokens = words.split(/\s+/).filter(Boolean).map(function (token) { return digits[token] || token; });
    var numberTokens = tokens.filter(function (token) { return /^\d+$/.test(token); });
    var name = tokens.filter(function (token) { return !/^\d+$/.test(token); }).join(" ");
    var number = numberTokens.join("");
    if (!name && !number) return [];
    return candidates.filter(function (candidate) {
      var offeredName = " " + normalise(candidate.display_name) + " ";
      var nameMatches = !name || offeredName.indexOf(" " + name + " ") !== -1;
      var numberMatches = !number || suffix(candidate) === number;
      return nameMatches && numberMatches;
    });
  }
  function attach(card, candidates) {
    var prompt = card.querySelector(".clarify-prompt");
    prompt.textContent = question(candidates);
    var controls = document.createElement("div");
    controls.className = "ambiguity-voice";
    controls.innerHTML = '<div class="clarify-actions"><button type="button" class="btn btn-voice js-hear-question">Hear question</button>' +
      '<button type="button" class="btn btn-voice js-answer-voice" aria-pressed="false">Answer by voice</button></div>' +
      '<p class="hint js-answer-heard" role="status">Heard: nothing yet.</p>' +
      '<p class="hint js-answer-selected" role="status">Selected: none.</p>' +
      '<p class="hint js-answer-status" role="status">Say a name or the full account ending. Review the selection, then click Confirm selection. You can always tap a payee.</p>' +
      '<p class="voice-preview js-answer-preview" aria-live="polite" hidden></p>' +
      '<p class="hint js-question-status" role="status"></p>';
    prompt.after(controls);
    var status = controls.querySelector(".js-answer-status");
    var heard = controls.querySelector(".js-answer-heard");
    var selected = controls.querySelector(".js-answer-selected");
    var confirm = card.querySelector(".js-clarify-confirm");
    var buttons = card.querySelectorAll(".candidate-btn");
    function clearSelection() {
      Array.prototype.forEach.call(buttons, function (button) {
        button.classList.remove("selected"); button.setAttribute("aria-pressed", "false");
        button.querySelector(".candidate-check").style.display = "none";
      });
      confirm.disabled = true;
      selected.textContent = "Selected: none.";
    }
    function showSelection() {
      var button = card.querySelector(".candidate-btn.selected");
      var candidate = button && candidates.find(function (candidate) { return candidate.id === button.getAttribute("data-candidate-id"); });
      selected.textContent = candidate ? "Selected: " + label(candidate) + ". Review, then click Confirm selection." : "Selected: none.";
    }
    Array.prototype.forEach.call(buttons, function (button) { button.addEventListener("click", showSelection); });
    card.querySelector(".js-payee-filter").addEventListener("input", showSelection);
    var hear = controls.querySelector(".js-hear-question");
    var questionStatus = controls.querySelector(".js-question-status");
    hear.disabled = !DctaVoice.canSpeak;
    if (!DctaVoice.canSpeak) questionStatus.textContent = "Audio is unavailable. Read the question or tap a payee.";
    hear.addEventListener("click", function () {
      if (card.getAttribute("data-clarify-in-flight") === "true") return;
      DctaVoice.speakText(hear, questionStatus, prompt.textContent, "Hear question");
    });
    DctaVoice.wireAnswer({
      card: card, button: controls.querySelector(".js-answer-voice"), status: status,
      preview: controls.querySelector(".js-answer-preview"),
      onStart: function () { clearSelection(); heard.textContent = "Heard: listening…"; },
      onHeard: function (text) { heard.textContent = "Heard: “" + text + "” (finishing recognition)."; },
      onFinal: function (text) {
        if (!card.isConnected || card.getAttribute("data-clarify-in-flight") === "true") return;
        heard.textContent = "Heard: “" + text + "”.";
        var matches = matchAnswer(text, candidates);
        clearSelection();
        if (matches.length !== 1) {
          status.textContent = matches.length > 1 ? "That matches more than one payee. Say the full name or account ending, or tap a payee." : "No clear match to the offered payees. Try again or tap a payee.";
          return;
        }
        // Reuse the existing tap handler; it stores only the offered candidate ID.
        var filter = card.querySelector(".js-payee-filter");
        filter.value = ""; filter.dispatchEvent(new Event("input"));
        Array.prototype.find.call(buttons, function (button) { return button.getAttribute("data-candidate-id") === matches[0].id; }).click();
        status.textContent = "Review the selected payee, then click Confirm selection. Nothing was submitted by voice.";
      }
    });
  }
  global.DctaAmbiguity = { attach: attach, question: question, matchAnswer: matchAnswer };
})(window);
