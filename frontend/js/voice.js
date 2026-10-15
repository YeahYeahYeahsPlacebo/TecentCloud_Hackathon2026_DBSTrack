/* Voice is an input/accessibility aid only. It never submits, signs or executes. */
(function (global) {
  "use strict";
  var mic, toggleMic, input, send, status, preview;
  var session = null;
  var playback = null;
  var speechQueue = [];
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
      if (!message && record.finalText) record.target.onFinal(record.finalText);
      else recognitionStatus(record, message || "No final answer heard. Try again or tap a payee.");
      return;
    }
    clearPreview();
    mic.setAttribute("aria-pressed", "false");
    mic.textContent = "Hold to talk";
    toggleMic.textContent = "Tap to start talking";
    toggleMic.setAttribute("aria-pressed", "false");
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
  }
  function reset() {
    cancelRecognition();
    cancelPlayback();
  }
  function startRecording(target, mode) {
    target = target || null;
    mode = target ? "answer" : (mode || "toggle");
    if (!Recognition || input.disabled || (target && (!target.card.isConnected || target.card.getAttribute("data-clarify-in-flight") === "true"))) return;
    if (session) {
      if (session.target === target) return;
      cancelRecognition("Microphone stopped. You can still tap a payee or type your message.");
    }
    cancelPlayback(); clearPreview();
    var record = { recognition: null, finalText: "", completed: [], stopping: false,
      stopIssued: false, timer: null, restartTimer: null, emptyEnds: 0, target: target, mode: mode };
    session = record;
    if (target) {
      target.onStart(); target.button.setAttribute("aria-pressed", "true"); target.button.textContent = "Finish voice answer";
    } else {
      send.disabled = true; mic.setAttribute("aria-pressed", "true"); toggleMic.setAttribute("aria-pressed", "true");
      mic.textContent = mode === "hold" ? "Release to finish" : "Listening…";
      toggleMic.textContent = "Tap to stop talking";
    }
    // Bounded session lifetime, including restart cycles and permission prompts.
    record.timer = setTimeout(function () {
      if (session === record) cancelRecognition("Microphone timed out. Review your words; use tap-to-start/tap-to-stop or type.");
    }, 120000);
    function cycle() {
      if (session !== record || record.stopping) return;
      var recognition, cycleFinal = "", ended = false;
      try { recognition = new Recognition(); }
      catch (_) { finish(record, "Voice could not start. Use tap-to-start/tap-to-stop or type."); return; }
      record.recognition = recognition; record.stopIssued = false;
      recognition.lang = "en-SG"; recognition.interimResults = true; recognition.continuous = true;
      recognitionStatus(record, target ? "Listening for a payee; finish, review, then confirm manually." : "Listening… Release the hold or tap Stop. Send is always manual.");
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
        if (target && record.finalText) target.onHeard(record.finalText);
      };
      recognition.onerror = function (event) {
        if (session !== record || ended || record.recognition !== recognition) return;
        var messages = {
          "not-allowed": "Microphone permission denied. Allow access in Chrome, or type your message.",
          "service-not-allowed": "Speech recognition is unavailable or blocked. Please type your message.",
          "audio-capture": "No microphone is available. Please type your message.",
          "no-speech": "No speech detected. Try tap-to-start/tap-to-stop, or type.",
          "network": "Speech recognition could not connect. Please type your message."
        };
        cancelRecognition(messages[event.error] || "Recognition stopped. Review the words; try tap-to-start/tap-to-stop or type.");
      };
      recognition.onend = function () {
        if (session !== record || ended || record.recognition !== recognition) return;
        ended = true;
        if (cycleFinal) { record.completed.push(cycleFinal); record.emptyEnds = 0; }
        else record.emptyEnds++;
        if (!record.stopping && !target) {
          if (record.emptyEnds >= 3) {
            finish(record, "This browser keeps ending recognition. Try tap-to-start/tap-to-stop, or type. Review any final words.");
            return;
          }
          recognitionStatus(record, "Browser paused recognition; restarting. Keep speaking after listening resumes, or release/stop to finish.");
          // Release/reset during this gap cancels the restart. No auto-send.
          record.recognition = null;
          record.restartTimer = setTimeout(cycle, 200);
          return;
        }
        finish(record);
      };
      try { recognition.start(); }
      catch (_) { finish(record, "Microphone could not start. Try tap-to-start/tap-to-stop or type."); }
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
    // stop() while starting may throw; onstart retries after a quick release.
    try {
      record.recognition.stop();
      record.stopIssued = true;
    } catch (_) { /* onstart retries */ }
    // Keep accepting final results until onend. A short release timeout can
    // discard delayed speech-service results. The session's two-minute watchdog
    // still recovers a hung recognizer; edits, resets and navigation cancel.
  }
  function init() {
    mic = document.getElementById("mic-btn");
    toggleMic = document.getElementById("mic-toggle-btn");
    if (!toggleMic) {
      toggleMic = document.createElement("button"); toggleMic.type = "button";
      toggleMic.id = "mic-toggle-btn"; toggleMic.className = "btn btn-voice";
      toggleMic.textContent = "Tap to start talking"; mic.after(toggleMic);
    }
    input = document.getElementById("text-input");
    send = document.getElementById("send-btn");
    status = document.getElementById("voice-status");
    preview = document.getElementById("voice-preview");
    mic.disabled = !Recognition; toggleMic.disabled = !Recognition;
    sayStatus(Recognition ? "Hold to talk, or tap Start then Stop. Browser restarts may leave short gaps. Speech recognition may use your browser's online speech service." : "Speech recognition is not supported here. Please type your message.");
    mic.addEventListener("pointerdown", function (event) {
      if (event.button !== 0 || (session && !session.target)) return;
      event.preventDefault();
      mic.focus();
      try { mic.setPointerCapture(event.pointerId); } catch (_) { /* synthetic/unsupported capture */ }
      startRecording(null, "hold");
    });
    mic.addEventListener("pointerup", stopRecording);
    mic.addEventListener("pointercancel", function () { cancelRecognition(); });
    mic.addEventListener("lostpointercapture", function () { if (session && !session.stopping) stopRecording(); });
    mic.addEventListener("keydown", function (event) {
      if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        if (!event.repeat) startRecording(null, "hold");
      }
    });
    mic.addEventListener("keyup", function (event) {
      if (event.key === " " || event.key === "Enter") { event.preventDefault(); stopRecording(); }
    });
    // Assistive technology can invoke click without pointer/key events.
    mic.addEventListener("click", function (event) {
      if (event.detail === 0) { if (session) stopRecording(); else startRecording(); }
    });
    toggleMic.addEventListener("click", function () {
      if (session && !session.target) stopRecording(); else startRecording(null, "toggle");
    });
    mic.addEventListener("blur", function () {
      // Moving focus after release is not an edit: preserve pending finals.
      // The input handler below still cancels before a user edit is overwritten.
      if (session && session.mode === "hold") stopRecording();
    });
    input.addEventListener("input", function () { cancelRecognition("Message edited. Review it, then press Send."); });
    global.addEventListener("pagehide", reset);
    document.addEventListener("visibilitychange", function () { if (document.hidden) reset(); });
    // Abort pending microphone/playback on a new message, fixture or clarification.
    document.addEventListener("click", function (event) {
      if (event.target.closest(".btn-scenario, #send-btn, .js-clarify-confirm")) reset();
    }, true);
    // A detached/replaced card must never keep talking about an old draft.
    new MutationObserver(function () {
      if ((playback && !playback.button.isConnected) || speechQueue.some(function (item) { return !item.button.isConnected; })) cancelPlayback();
      if (session && session.target && !session.target.card.isConnected) cancelRecognition();
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
      playNext();
    };
    utterance.onerror = fail;
    record.timer = setTimeout(function () { if (playback === record) fail(); }, 5000);
    try { global.speechSynthesis.speak(utterance); }
    catch (_) { fail(); }
  }
  function speakText(button, note, text, idleLabel) {
    if (!canSpeak || !button.isConnected) return;
    if (playback && playback.button === button) { cancelPlayback(); return; }
    cancelRecognition(); cancelPlayback();
    speechQueue.push({ button: button, status: note, text: text, idleLabel: idleLabel });
    playNext();
  }
  function queueCards(items) {
    cancelPlayback();
    if (!canSpeak) return;
    // Render/hash completion order must not decide conversational priority.
    var ordered = items.filter(function (item) {
      // Clarification can replace a card's contents while another hash is
      // pending. Never rebuild speech from that old response in a reused root.
      return item.card.isConnected && (!item.anchor || item.anchor.isConnected);
    }).sort(function (a, b) {
      return Number(!!b.card.querySelector(".js-hear-question")) - Number(!!a.card.querySelector(".js-hear-question")) || a.index - b.index;
    });
    ordered.forEach(function (item) {
      var card = item.card, hear = card.querySelector(".js-hear-question");
      if (hear) {
        speechQueue.push({ button: hear, status: card.querySelector(".js-question-status"),
          text: "Item " + (item.index + 1) + ". " + card.querySelector(".clarify-prompt").textContent, idleLabel: "Hear question" });
      } else {
        var button = card.querySelector(".js-draft-readback");
        if (button) speechQueue.push({ button: button, status: button.nextElementSibling,
          text: draftText(item.draft, item.index), idleLabel: "Read draft" });
      }
    });
    playNext();
  }
  function draftText(draft, index) {
    return (typeof index === "number" ? "Item " + (index + 1) + ". Draft. " : "Updated draft. ") +
      "Amount: " + draft.amount.value + " " + draft.amount.currency + ". " +
      (draft.payee ? "Recipient: " + draft.payee.display_name + ", account ending " + String(draft.payee.masked_account).replace(/\*/g, "") + ". " : "Recipient is unresolved. ") +
      (typeof index === "number" ? "" : "Source account: " + draft.source_account + ". ") +
      "Review the details. Spoken confirmation never signs or executes.";
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
  function attachDraftReadback(card, draft, autoplay, index) {
    if (!card || !card.isConnected) return;
    var controls = document.createElement("div"), button = document.createElement("button"), note = document.createElement("p");
    controls.className = "readback-controls";
    var label = typeof index === "number" ? "Read draft" : "Read updated draft";
    button.type = "button"; button.className = "btn btn-voice js-draft-readback"; button.textContent = label;
    note.className = "hint"; note.setAttribute("role", "status");
    controls.append(button, note); card.appendChild(controls);
    button.disabled = !canSpeak;
    note.textContent = canSpeak ? "Reads the newly returned draft. Speech is acknowledgement only, never authorization." : "Audio is unavailable. Review the updated draft on screen.";
    // Only the new server response is used. Neither the old candidate nor the
    // recognized answer can supply the amount or recipient in this read-back.
    var text = draftText(draft, index);
    button.addEventListener("click", function () { speakText(button, note, text, label); });
    // The user's manual Confirm selection requested this read-back. Browser
    // autoplay restrictions may still require tapping the retained button.
    if (autoplay && canSpeak) button.click();
  }
  global.DctaVoice = { init: init, reset: reset, attachReadback: attachReadback, wireAnswer: wireAnswer, speakText: speakText, canSpeak: canSpeak, queueCards: queueCards, attachDraftReadback: attachDraftReadback };
})(window);
