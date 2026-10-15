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
  function finish(record, message) {
    if (session !== record) return;
    session = null;
    clearTimeout(record.timer);
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
    old.button.textContent = "Read amount twice";
    old.status.textContent = "Read-back stopped. Audio is not authorization.";
  }
  function reset() {
    cancelRecognition();
    cancelPlayback();
  }
  function startRecording() {
    if (!Recognition || session || input.disabled) return;
    cancelPlayback();
    clearPreview();
    var recognition;
    try { recognition = new Recognition(); }
    catch (_) { sayStatus("Speech recognition could not start. Please type your message."); return; }
    var record = { recognition: recognition, finalText: "", stopping: false, stopIssued: false, timer: null };
    session = record;
    recognition.lang = "en-SG";
    recognition.interimResults = true;
    recognition.continuous = false;
    send.disabled = true;
    mic.setAttribute("aria-pressed", "true");
    mic.textContent = "Release to finish";
    sayStatus("Starting microphone… Hold to speak; release to finish.");
    recognition.onstart = function () {
      if (session !== record) return;
      if (record.stopping) { stopRecording(); return; }
      sayStatus("Listening… Release to finish. Nothing will be sent automatically.");
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
      if (record.finalText) input.value = record.finalText;
      preview.textContent = interim.length ? "Preview only: " + interim.join(" ") : "";
      preview.hidden = !interim.length;
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
    sayStatus("Finishing recognition… Review the final words before sending.");
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
      if (event.button !== 0 || session) return;
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
      if (playback && playback.button === button) { cancelPlayback(); return; }
      cancelRecognition(); cancelPlayback();
      var text = "The draft amount is " + amount.value + " " + amount.currency + ". I repeat: " + amount.value + " " + amount.currency + ". Review the draft on screen. This is not authorization.";
      var utterance;
      try { utterance = new global.SpeechSynthesisUtterance(text); }
      catch (_) { note.textContent = "Read-back could not start. Review the amount on screen."; return; }
      var record = { button: button, status: note, utterance: utterance };
      playback = record;
      utterance.lang = "en-SG";
      button.textContent = "Stop read-back";
      note.textContent = "Reading the server-returned amount twice. Audio is not authorization.";
      utterance.onend = function () {
        if (playback !== record) return;
        playback = null; button.textContent = "Read amount twice";
        note.textContent = "Read-back finished. Review the draft on screen; audio is not authorization.";
      };
      utterance.onerror = function () {
        if (playback !== record) return;
        playback = null; button.textContent = "Read amount twice";
        note.textContent = "Read-back unavailable. Review the amount on screen.";
      };
      try { global.speechSynthesis.speak(utterance); }
      catch (_) { utterance.onerror(); }
    });
  }
  global.DctaVoice = { init: init, reset: reset, attachReadback: attachReadback };
})(window);
