/* Loaded only by build_public_preview.py; normal gateway builds are unchanged. */
(function () {
  "use strict";
  // A static host cannot validate, register, sign or execute. Do not contact
  // another origin or replace an attempted live request with fixture data.
  window.DctaPasskeys.init = function () {
    document.getElementById("register-passkey").disabled = true;
    document.getElementById("passkey-status").textContent =
      "Passkeys unavailable in this static preview: no registration or signing server is connected.";
  };
  document.addEventListener("DOMContentLoaded", function () {
    var send = document.getElementById("send-btn");
    send.disabled = true;
    send.textContent = "Send unavailable in preview";
    document.getElementById("api-banner").textContent =
      "Static sample preview — no live parser, validator or transaction API is connected.";
    document.getElementById("input-hint").textContent =
      "Try Tap to talk and edit the final text. Sending is unavailable; sample buttons load labelled fixture data only.";
    // Fixture loading can re-enable Send in the shared app; enforce the static
    // boundary after every render and prevent keyboard submission as well.
    new MutationObserver(function () { if (!send.disabled) send.disabled = true; })
      .observe(send, { attributes: true, attributeFilter: ["disabled"] });
    document.getElementById("text-input").addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault(); event.stopImmediatePropagation();
      }
    });
  });
}());
