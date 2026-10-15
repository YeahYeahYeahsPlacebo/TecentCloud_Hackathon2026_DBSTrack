/* Voice is an input/accessibility aid only. It never submits, signs or executes. */
(function (global) {
  "use strict";
  var mic, input, send, status, preview;
  var session = null;
  var playback = null;
  var speechQueue = [];
  var conversation = null;
  var autoReviewAllowed = true;
  var Recognition = global.SpeechRecognition || global.webkitSpeechRecognition;
  var canSpeak = !!(global.speechSynthesis && global.SpeechSynthesisUtterance);

  function sayStatus(text) { status.textContent = text; }
  function clearPreview() { preview.textContent = ""; preview.hidden = true; }
  function recognitionStatus(record, text) {
    (record.target ? record.target.status : status).textContent = text;
  }
  function finish(record, message) {
    if (session !== record) return;
    session = null;
    clearTimeout(record.timer);
    clearTimeout(record.restartTimer);
    if (record.target) {
      record.target.preview.textContent = "";
      record.target.preview.hidden = true;
      record.target.button.setAttribute("aria-pressed", "false");
      record.target.button.textContent = "Answer by voice";
      if (!message && record.finalText) {
        if (record.target.onFinal(record.finalText) === false) record.target.button.textContent = "Retry voice answer";
      } else {
        record.target.button.textContent = "Retry voice answer";
        recognitionStatus(record, (message || "No clear answer heard.") + " Retry or tap a payee; your selection is unchanged.");
      }
      return;
    }
    clearPreview();
    mic.setAttribute("aria-pressed", "false");
    mic.textContent = "Tap to talk";
    // The app owns send/input while a request is in flight.
    if (!input.disabled) send.disabled = false;
    sayStatus(message || (record.finalText ? "Final words added. Review or correct your message, then press Send." : "No final words received. Try again or type your message."));
  }
  function cancelRecognition(message) {
    if (!session) { clearPreview(); return; }
    var record = session;
    finish(record, message || "Microphone stopped. Review your message before sending.");
    // Late callbacks from this session are ignored by the identity checks.
    try { if (record.recognition) record.recognition.abort(); } catch (_) { /* already ended */ }
  }
  function cancelPlayback() {
    speechQueue = [];
    if (!playback) return;
    var old = playback;
    playback = null;
    clearTimeout(old.timer);
    global.speechSynthesis.cancel();
    old.button.textContent = old.idleLabel;
    old.status.textContent = "Audio stopped. Audio is not authorization.";
    var item = conversation && conversation.items[conversation.cursor];
    if (item && item.note && item.card.contains(old.button)) item.note.textContent = item.card.querySelector(".js-hear-question") ? "Paused for your answer and manual Confirm selection, or skip spoken review." : "Audio paused. Replay this draft or choose Skip spoken review / Continue.";
  }
  function pause() { cancelRecognition(); cancelPlayback(); }
  function reset() {
    if (conversation) conversation.items.forEach(function (item) {
      if (item.note) item.note.textContent = "Spoken review canceled. Manual replay is still available.";
      if (item.continueButton) item.continueButton.disabled = true;
    });
    conversation = null;
    pause();
  }
  function startRecording(target) {
    target = target || null;
    if (!Recognition || input.disabled || (target && (!target.card.isConnected || target.card.getAttribute("data-clarify-in-flight") === "true"))) return;
    if (session) {
      if (session.target === target) return;
      cancelRecognition("Microphone stopped. You can still tap a payee or type your message.");
    }
    // A new message microphone invalidates the old narration. Contextual
    // payee answers only pause it so manual clarification can resume review.
    if (!target) { reset(); autoReviewAllowed = false; }
    else cancelPlayback();
    clearPreview();
    var record = { recognition: null, finalText: "", completed: [], stopping: false,
      stopIssued: false, timer: null, restartTimer: null, emptyEnds: 0, target: target };
    session = record;
    if (target) {
      target.onStart(); target.button.setAttribute("aria-pressed", "true"); target.button.textContent = "Stop listening";
    } else {
      send.disabled = true; mic.setAttribute("aria-pressed", "true");
      mic.textContent = "Stop listening";
    }
    // Bounded session lifetime, including restart cycles and permission prompts.
    record.timer = setTimeout(function () {
      if (session === record) cancelRecognition(target ? "No clear answer heard before the microphone timed out." : "Microphone timed out. Review your words; tap to talk again or type.");
    }, target ? 15000 : 120000);
    function cycle() {
      if (session !== record || record.stopping) return;
      var recognition, cycleFinal = "", ended = false;
      try { recognition = new Recognition(); }
      catch (_) { finish(record, target ? "Voice answer could not start." : "Voice could not start. Tap to talk again or type."); return; }
      record.recognition = recognition; record.stopIssued = false;
      recognition.lang = "en-SG"; recognition.interimResults = true; recognition.continuous = !target;
      recognitionStatus(record, target ? "Listening for a payee. A clear final answer stops listening; confirm manually." : "Listening… Tap Stop listening when finished. Send is always manual.");
      recognition.onstart = function () {
        if (session !== record || ended || record.recognition !== recognition) return;
        if (record.stopping) stopRecording();
      };
      recognition.onresult = function (event) {
        if (session !== record || ended || record.recognition !== recognition) return;
        // A recognizer's results are cumulative. Rebuild this cycle, then
        // append completed cycles exactly once; never deduplicate real words.
        var finals = [], interim = [];
        for (var i = 0; i < event.results.length; i++) {
          var result = event.results[i];
          (result.isFinal ? finals : interim).push(result[0].transcript);
        }
        cycleFinal = finals.join(" ").trim();
        record.finalText = record.completed.concat(cycleFinal ? [cycleFinal] : []).join(" ").trim();
        if (!target && record.finalText) input.value = record.finalText;
        var previewEl = target ? target.preview : preview;
        previewEl.textContent = interim.length ? "Preview only: " + interim.join(" ") : "";
        previewEl.hidden = !interim.length;
        if (target && record.finalText) {
          target.onHeard(record.finalText);
          if (!interim.length && target.isUnique && target.isUnique(record.finalText)) {
            // Commit a final offered-candidate match only to visual selection.
            // Finish invalidates this session before stopping the native mic.
            finish(record);
            try { recognition.stop(); } catch (_) { try { recognition.abort(); } catch (_) {} }
          }
        }
      };
      recognition.onerror = function (event) {
        if (session !== record || ended || record.recognition !== recognition) return;
        var messages = {
          "not-allowed": "Microphone permission denied. Allow access in Chrome, or type your message.",
          "service-not-allowed": "Speech recognition is unavailable or blocked. Please type your message.",
          "audio-capture": "No microphone is available. Please type your message.",
          "no-speech": "No speech detected. Try Tap to talk, then Stop listening, or type.",
          "network": "Speech recognition could not connect. Please type your message."
        };
        var message = messages[event.error] || "Recognition stopped. Review the words; tap to talk again or type.";
        if (target) message = event.error === "not-allowed" ? "Microphone permission denied. Enable it in Chrome to retry, or tap a payee." : event.error === "no-speech" ? "No answer heard." : "Voice answer unavailable or interrupted.";
        cancelRecognition(message);
      };
      recognition.onend = function () {
        if (session !== record || ended || record.recognition !== recognition) return;
        ended = true;
        if (cycleFinal) { record.completed.push(cycleFinal); record.emptyEnds = 0; }
        else record.emptyEnds++;
        if (!record.stopping && !target) {
          if (record.emptyEnds >= 3) {
            finish(record, "This browser keeps ending recognition. Try Tap to talk, then Stop listening, or type. Review any final words.");
            return;
          }
          recognitionStatus(record, "Browser paused recognition; restarting. Keep speaking after listening resumes, or tap Stop listening to finish.");
          // Stop/reset during this gap cancels the restart. No auto-send.
          record.recognition = null;
          record.restartTimer = setTimeout(cycle, 200);
          return;
        }
        finish(record);
      };
      try { recognition.start(); }
      catch (_) { finish(record, target ? "Voice answer could not start." : "Microphone could not start. Tap to talk again or type."); }
    }
    cycle();
  }
  function stopRecording() {
    if (!session) return;
    var record = session;
    record.stopping = true;
    clearTimeout(record.restartTimer);
    if (!record.recognition) { finish(record); return; }
    if (record.stopIssued) return;
    recognitionStatus(record, record.target ? "Finishing recognition… Review the selected payee before confirming." : "Finishing recognition… Review the final words before sending.");
    // stop() while starting may throw; onstart retries after a quick stop.
    try {
      record.recognition.stop();
      record.stopIssued = true;
    } catch (_) { /* onstart retries */ }
    // Keep accepting final results until onend. A short stop timeout can
    // discard delayed speech-service results. The session's two-minute watchdog
    // still recovers a hung recognizer; edits, resets and navigation cancel.
  }
  function init() {
    mic = document.getElementById("mic-btn");
    input = document.getElementById("text-input");
    send = document.getElementById("send-btn");
    status = document.getElementById("voice-status");
    preview = document.getElementById("voice-preview");
    mic.disabled = !Recognition;
    sayStatus(Recognition ? "Tap to talk; tap Stop listening when finished. Final words stay editable. Send is manual. Browser speech services may pause or mishear words." : "Speech recognition is not supported here. Please type your message.");
    // Native click semantics support touch, keyboard and assistive technology.
    mic.addEventListener("click", function () {
      if (session && !session.target) stopRecording(); else startRecording();
    });
    input.addEventListener("input", function () { cancelRecognition("Message edited. Review it, then press Send."); });
    global.addEventListener("pagehide", function () { reset(); autoReviewAllowed = false; });
    document.addEventListener("visibilitychange", function () { if (document.hidden) { reset(); autoReviewAllowed = false; } });
    // Abort pending microphone/playback on a new message, fixture or clarification.
    document.addEventListener("click", function (event) {
      if (event.target.closest(".js-clarify-confirm")) pause();
      else if (event.target.closest(".btn-scenario, #send-btn")) { reset(); autoReviewAllowed = true; }
    }, true);
    // A detached/replaced card must never keep talking about an old draft.
    new MutationObserver(function () {
      if ((playback && !playback.button.isConnected) || speechQueue.some(function (item) { return !item.button.isConnected; })) cancelPlayback();
      if (conversation && conversation.items.some(function (item) { return !item.card.isConnected; })) reset();
      if (session && session.target && (!session.target.card.isConnected || !session.target.button.isConnected)) cancelRecognition();
    }).observe(document.getElementById("draft-display"), { childList: true, subtree: true });
  }
  function attachReadback(card, draft) {
    if (!card) return;
    var controls = document.createElement("div");
    controls.className = "readback-controls";
    var button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-voice js-readback";
    button.textContent = "Read amount twice";
    var note = document.createElement("p");
    note.className = "hint";
    note.setAttribute("role", "status");
    // Copy only the displayed server amount. Never parse the transcript or UI text.
    var amount = draft.amount;
    var available = amount && typeof amount.value === "string" && typeof amount.currency === "string";
    button.disabled = !canSpeak || !available;
    note.textContent = !canSpeak ? "Read-back is not supported in this browser." : !available ? "No server-returned amount to read." : "Reads this draft's amount twice. Audio is not authorization.";
    controls.appendChild(button); controls.appendChild(note); card.appendChild(controls);
    button.addEventListener("click", function () {
      speakText(button, note, "The draft amount is " + amount.value + " " + amount.currency + ". I repeat: " + amount.value + " " + amount.currency + ". Review the draft on screen. This is not authorization.", "Read amount twice");
    });
  }
  function playNext() {
    if (playback || !canSpeak) return;
    var record = speechQueue.shift();
    if (!record) return;
    if (!record.button.isConnected) { playNext(); return; }
    playback = record;
    var utterance;
    function fail() {
      if (playback !== record) return;
      clearTimeout(record.timer); playback = null; speechQueue = [];
      global.speechSynthesis.cancel(); record.button.textContent = record.idleLabel;
      record.status.textContent = "Automatic audio may be blocked. Read the question/details or tap the replay button. Audio is not authorization.";
      var item = conversation && conversation.items[conversation.cursor];
      if (item && item.note && item.card.contains(record.button)) item.note.textContent = "Audio unavailable or blocked. Replay this item, or choose Skip spoken review / Continue. Nothing is authorized.";
    }
    try { utterance = new global.SpeechSynthesisUtterance(record.text); }
    catch (_) { fail(); return; }
    record.utterance = utterance; utterance.lang = "en-SG";
    record.button.textContent = "Stop audio";
    record.status.textContent = "Attempting audio. If silent, use the replay button. Audio is not authorization.";
    utterance.onstart = function () { if (playback === record) { clearTimeout(record.timer); record.status.textContent = "Playing audio. Audio is not authorization."; } };
    utterance.onend = function () {
      if (playback !== record) return;
      clearTimeout(record.timer); playback = null; record.button.textContent = record.idleLabel;
      record.status.textContent = "Audio finished. Review the details; audio is not authorization.";
      if (record.onComplete) record.onComplete();
      playNext();
    };
    utterance.onerror = fail;
    record.timer = setTimeout(function () { if (playback === record) fail(); }, 5000);
    try { global.speechSynthesis.speak(utterance); }
    catch (_) { fail(); }
  }
  function speakText(button, note, text, idleLabel, onComplete) {
    if (!canSpeak || !button.isConnected) return;
    if (playback && playback.button === button) { cancelPlayback(); return; }
    cancelRecognition(); cancelPlayback();
    speechQueue.push({ button: button, status: note, text: text, idleLabel: idleLabel, onComplete: onComplete });
    playNext();
  }
  function current(plan, item) {
    return conversation === plan && plan.items[plan.cursor] === item && item.card.isConnected;
  }
  function complete(plan, item) {
    if (!current(plan, item)) return;
    item.note.textContent = "Spoken review finished. This does not authorize the transaction.";
    item.continueButton.disabled = true;
    plan.cursor++; advance(plan);
  }
  function advance(plan) {
    if (conversation !== plan) return;
    var item = plan.items[plan.cursor];
    if (!item) return;
    if (item.skipped) { plan.cursor++; advance(plan); return; }
    if (!item.card.isConnected || (item.anchor && !item.anchor.isConnected)) return;
    var hear = item.card.querySelector(".js-hear-question");
    item.note.textContent = hear ? "Spoken review is paused here. Answer and Confirm selection, or skip this item's spoken review." : "Reviewing transaction " + (item.index + 1) + ". You may skip spoken review.";
    if (!canSpeak) return;
    if (hear) {
      // A question is a conversational barrier: its onend MUST NOT advance.
      speakText(hear, item.card.querySelector(".js-question-status"),
        "Transaction " + (item.index + 1) + ". " + item.card.querySelector(".clarify-prompt").textContent, "Hear question");
    } else {
      var button = item.card.querySelector(".js-draft-readback");
      if (button) speakText(button, button.nextElementSibling, draftText(item.draft, item.index, item.validator, item.policy), button.dataset.readbackLabel || "Read draft", function () { complete(plan, item); });
    }
  }
  function addContinue(plan, item) {
    var controls = document.createElement("div"), button = document.createElement("button"), note = document.createElement("p");
    button.type = "button"; button.className = "btn btn-voice js-review-continue";
    button.textContent = "Skip spoken review for this item / Continue";
    note.className = "hint js-conversation-status"; note.setAttribute("role", "status");
    note.textContent = "Waiting for this transaction's spoken review. Skipping never changes the draft.";
    controls.append(button, note); item.card.append(controls); item.note = note; item.continueButton = button;
    button.addEventListener("click", function () {
      if (conversation !== plan) return;
      item.skipped = true; button.disabled = true;
      note.textContent = "Spoken review skipped. The draft and selection are unchanged.";
      if (current(plan, item)) { pause(); plan.cursor++; advance(plan); }
    });
  }
  function queueCards(items) {
    reset();
    if (!autoReviewAllowed) return;
    var plan = { items: items.filter(function (item) {
      return item.card.isConnected && (!item.anchor || item.anchor.isConnected);
    }).sort(function (a, b) { return a.index - b.index; }), cursor: 0 };
    conversation = plan;
    plan.items.forEach(function (item) { addContinue(plan, item); });
    advance(plan);
  }
  function resolvedCard(oldCard, card, draft, validator, policy) {
    if (!oldCard.isConnected || !card.isConnected) return;
    var plan = conversation, item = plan && plan.items.find(function (entry) { return entry.card === oldCard; });
    if (!item) { attachDraftReadback(card, draft, false, undefined, validator, policy); return; }
    pause();
    item.card = card; item.draft = draft; item.validator = validator; item.policy = policy;
    attachDraftReadback(card, draft, false, item.index, validator, policy, true);
    item.anchor = card.querySelector(".js-draft-readback");
    addContinue(plan, item);
    // Resume only after this server-created replacement is ready. The other
    // cards are neither re-rendered nor revalidated by this frontend transition.
    advance(plan);
  }
  function draftText(draft, index, validator, policy) {
    return (typeof index === "number" ? "Transaction " + (index + 1) + ". " : "Updated draft. ") +
      "Recipient: " + (draft.payee ? draft.payee.display_name + ", masked account ending " + String(draft.payee.masked_account).replace(/\*/g, "") : "unresolved") + ". " +
      "Amount: " + draft.amount.value + " " + draft.amount.currency + ". " +
      "Validation: " + (validator && validator.verdict ? validator.verdict : "unavailable") + ". " +
      "Policy: " + (policy && policy.decision ? policy.decision : "unavailable") + ". " +
      "Source account: " + draft.source_account + ". Review the displayed draft. Spoken confirmation never signs or executes.";
  }
  function wireAnswer(target) {
    target.button.disabled = !Recognition;
    if (!Recognition) target.status.textContent = "Voice recognition is unavailable. Tap a payee, then Confirm selection.";
    target.button.addEventListener("click", function () {
      if (session && session.target === target) stopRecording();
      else startRecording(target);
    });
    // Tapping/filtering is an explicit alternative to the pending voice answer.
    target.card.addEventListener("click", function (event) {
      if (event.target.closest(".candidate-btn") && session && session.target === target) cancelRecognition();
    });
    target.card.addEventListener("input", function () {
      if (session && session.target === target) cancelRecognition();
    });
  }
  function attachDraftReadback(card, draft, autoplay, index, validator, policy, updated) {
    if (!card || !card.isConnected) return;
    var controls = document.createElement("div"), button = document.createElement("button"), note = document.createElement("p");
    controls.className = "readback-controls";
    var label = updated || typeof index !== "number" ? "Read updated draft" : "Read draft";
    button.type = "button"; button.className = "btn btn-voice js-draft-readback"; button.textContent = label; button.dataset.readbackLabel = label;
    note.className = "hint"; note.setAttribute("role", "status");
    controls.append(button, note); card.appendChild(controls);
    button.disabled = !canSpeak;
    note.textContent = canSpeak ? "Reads the newly returned draft. Speech is acknowledgement only, never authorization." : "Audio is unavailable. Review the updated draft on screen.";
    // Only the new server response is used. Neither the old candidate nor the
    // recognized answer can supply the amount or recipient in this read-back.
    var text = draftText(draft, index, validator, policy);
    button.addEventListener("click", function () {
      var plan = conversation, item = plan && plan.items[plan.cursor];
      var done = item && item.card === card ? function () { complete(plan, item); } : null;
      speakText(button, note, text, label, done);
    });
    // The user's manual Confirm selection requested this read-back. Browser
    // autoplay restrictions may still require tapping the retained button.
    if (autoplay && canSpeak) button.click();
  }
  global.DctaVoice = { init: init, reset: reset, pause: pause, resolvedCard: resolvedCard, attachReadback: attachReadback, wireAnswer: wireAnswer, speakText: speakText, canSpeak: canSpeak, queueCards: queueCards, attachDraftReadback: attachDraftReadback };
})(window);
