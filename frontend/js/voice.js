/* Voice is an input/accessibility aid only. It never submits, signs or executes. */
(function (global) {
  "use strict";
  var mic, input, send, status, preview;
  var session = null;
  var playback = null;
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
    // The app owns send/input while a request is in flight.
    if (!input.disabled) send.disabled = false;
    sayStatus(message || (record.finalText ? "Final words added. Review or correct your message, then press Send." : "No final words received. Try again or type your message."));
  }
  function cancelRecognition(message) {
    if (!session) { clearPreview(); return; }
    var record = session;
    finish(record, message || "Microphone stopped. Review your message before sending.");
    // Late callbacks from this session are ignored by the identity checks.
    try { record.recognition.abort(); } catch (_) { /* already ended */ }
  }
  function cancelPlayback() {
    if (!playback) return;
    var old = playback;
    playback = null;
    global.speechSynthesis.cancel();
    old.button.textContent = old.idleLabel;
    old.status.textContent = "Audio stopped. Audio is not authorization.";
  }
  function reset() {
    cancelRecognition();
    cancelPlayback();
  }
  function startRecording(target) {
    target = target || null;
    if (!Recognition || input.disabled || (target && (!target.card.isConnected || target.card.getAttribute("data-clarify-in-flight") === "true"))) return;
    if (session) {
      if (session.target === target) return;
      cancelRecognition("Microphone stopped. You can still tap a payee or type your message.");
    }
    cancelPlayback();
    clearPreview();
    var recognition;
    try { recognition = new Recognition(); }
    catch (_) { (target ? target.status : status).textContent = target ? "Voice could not start. Tap a payee instead." : "Speech recognition could not start. Please type your message."; return; }
    var record = { recognition: recognition, finalText: "", stopping: false, stopIssued: false, timer: null, target: target };
    session = record;
    recognition.lang = "en-SG";
    recognition.interimResults = true;
    recognition.continuous = false;
    if (target) {
      target.onStart();
      target.button.setAttribute("aria-pressed", "true");
      target.button.textContent = "Finish voice answer";
    } else {
      send.disabled = true;
      mic.setAttribute("aria-pressed", "true");
      mic.textContent = "Release to finish";
    }
    recognitionStatus(record, target ? "Listening for a payee. Click again to finish; you must confirm the selection yourself." : "Starting microphone… Hold to speak; release to finish.");
    recognition.onstart = function () {
      if (session !== record) return;
      if (record.stopping) { stopRecording(); return; }
      recognitionStatus(record, target ? "Listening for a payee. Click again to finish; nothing is submitted automatically." : "Listening… Release to finish. Nothing will be sent automatically.");
    };
    recognition.onresult = function (event) {
      if (session !== record) return;
      // results is cumulative for this session. Rebuild finals rather than
      // appending each event, which would duplicate earlier final segments.
      var finals = [], interim = [];
      for (var i = 0; i < event.results.length; i++) {
        var result = event.results[i];
        (result.isFinal ? finals : interim).push(result[0].transcript);
      }
      record.finalText = finals.join(" ").trim();
      if (!target && record.finalText) input.value = record.finalText;
      var previewEl = target ? target.preview : preview;
      previewEl.textContent = interim.length ? "Preview only: " + interim.join(" ") : "";
      previewEl.hidden = !interim.length;
      if (target && record.finalText) target.onHeard(record.finalText);
    };
    recognition.onerror = function (event) {
      if (session !== record) return;
      var messages = {
        "not-allowed": "Microphone permission denied. Allow microphone access in your browser, or type your message.",
        "service-not-allowed": "Speech recognition is unavailable or blocked. Please type your message.",
        "audio-capture": "No microphone is available. Please type your message.",
        "no-speech": "No speech detected. Hold to try again, or type your message.",
        "network": "Speech recognition could not connect. Please type your message."
      };
      finish(record, messages[event.error] || "Speech recognition stopped. Please review or type your message.");
    };
    recognition.onend = function () { finish(record); };
    try {
      recognition.start();
      // Recover from a recognizer that never produces onend (including permission prompts).
      record.timer = setTimeout(function () { cancelRecognition("Microphone timed out. Please try again or type your message."); }, 45000);
    } catch (_) {
      finish(record, "Microphone could not start. Please try again or type your message.");
    }
  }
  function stopRecording() {
    if (!session) return;
    var record = session;
    record.stopping = true;
    if (record.stopIssued) return;
    recognitionStatus(record, record.target ? "Finishing recognition… Review the selected payee before confirming." : "Finishing recognition… Review the final words before sending.");
    // stop() while starting may throw; onstart retries after a quick release.
    try {
      record.recognition.stop();
      record.stopIssued = true;
    } catch (_) { /* onstart retries */ }
    // Keep accepting final results until onend. A short release timeout can
    // discard delayed speech-service results. The session's 45s watchdog
    // still recovers a hung recognizer; edits, resets and navigation cancel.
  }
  function init() {
    mic = document.getElementById("mic-btn");
    input = document.getElementById("text-input");
    send = document.getElementById("send-btn");
    status = document.getElementById("voice-status");
    preview = document.getElementById("voice-preview");
    mic.disabled = !Recognition;
    sayStatus(Recognition ? "Hold to talk. Speech recognition may use your browser's online speech service." : "Speech recognition is not supported here. Please type your message.");
    mic.addEventListener("pointerdown", function (event) {
      if (event.button !== 0 || (session && !session.target)) return;
      event.preventDefault();
      mic.focus();
      mic.setPointerCapture(event.pointerId);
      startRecording();
    });
    mic.addEventListener("pointerup", stopRecording);
    mic.addEventListener("pointercancel", function () { cancelRecognition(); });
    mic.addEventListener("lostpointercapture", function () { if (session && !session.stopping) stopRecording(); });
    mic.addEventListener("keydown", function (event) {
      if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        if (!event.repeat) startRecording();
      }
    });
    mic.addEventListener("keyup", function (event) {
      if (event.key === " " || event.key === "Enter") { event.preventDefault(); stopRecording(); }
    });
    // Assistive technology can invoke click without pointer/key events.
    mic.addEventListener("click", function (event) {
      if (event.detail === 0) { if (session) stopRecording(); else startRecording(); }
    });
    mic.addEventListener("blur", function () {
      // Moving focus after release is not an edit: preserve pending finals.
      // The input handler below still cancels before a user edit is overwritten.
      stopRecording();
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
      if (playback && !playback.button.isConnected) cancelPlayback();
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
  function speakText(button, note, text, idleLabel) {
    if (!canSpeak || !button.isConnected) return;
    if (playback && playback.button === button) { cancelPlayback(); return; }
    cancelRecognition(); cancelPlayback();
    var utterance;
    try { utterance = new global.SpeechSynthesisUtterance(text); }
    catch (_) { note.textContent = "Audio could not start. Please read the text on screen."; return; }
    var record = { button: button, status: note, utterance: utterance, idleLabel: idleLabel };
    playback = record;
    utterance.lang = "en-SG";
    button.textContent = "Stop audio";
    note.textContent = "Playing audio. Audio is not authorization.";
    utterance.onend = function () {
      if (playback !== record) return;
      playback = null; button.textContent = idleLabel;
      note.textContent = "Audio finished. Review the details on screen; audio is not authorization.";
    };
    utterance.onerror = function () {
      if (playback !== record) return;
      playback = null; button.textContent = idleLabel;
      note.textContent = "Audio unavailable. Please read the text on screen.";
    };
    try { global.speechSynthesis.speak(utterance); }
    catch (_) { utterance.onerror(); }
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
  function attachDraftReadback(card, draft, autoplay) {
    if (!card || !card.isConnected) return;
    var controls = document.createElement("div"), button = document.createElement("button"), note = document.createElement("p");
    controls.className = "readback-controls";
    button.type = "button"; button.className = "btn btn-voice js-draft-readback"; button.textContent = "Read updated draft";
    note.className = "hint"; note.setAttribute("role", "status");
    controls.append(button, note); card.appendChild(controls);
    button.disabled = !canSpeak;
    note.textContent = canSpeak ? "Reads the newly returned draft. Speech is acknowledgement only, never authorization." : "Audio is unavailable. Review the updated draft on screen.";
    // Only the new server response is used. Neither the old candidate nor the
    // recognized answer can supply the amount or recipient in this read-back.
    var amount = draft.amount;
    var text = "Updated draft. " + (amount ? "Amount: " + amount.value + " " + amount.currency + ". " : "") +
      (draft.payee ? "Recipient: " + draft.payee.display_name + ", account ending " + String(draft.payee.masked_account).replace(/\*/g, "") + ". " : "Recipient is unresolved. ") +
      "Source account: " + draft.source_account + ". Review the details. This is a draft, not an execution. Spoken confirmation never signs or executes.";
    button.addEventListener("click", function () { speakText(button, note, text, "Read updated draft"); });
    // The user's manual Confirm selection requested this read-back. Browser
    // autoplay restrictions may still require tapping the retained button.
    if (autoplay && canSpeak) button.click();
  }
  global.DctaVoice = { init: init, reset: reset, attachReadback: attachReadback, wireAnswer: wireAnswer, speakText: speakText, canSpeak: canSpeak, attachDraftReadback: attachDraftReadback };
})(window);
