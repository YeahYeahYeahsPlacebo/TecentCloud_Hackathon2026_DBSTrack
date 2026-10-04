/**
 * Canonical serialisation and hashing of transaction drafts.
 *
 * This is the JavaScript mirror of backend/canonical.py. The two must
 * agree byte-for-byte. See contract/CANONICAL_HASH.md for the spec.
 *
 * Runs in both the browser (Web Crypto API) and Node (crypto module).
 * No dependencies.
 */

(function (global) {
  "use strict";

  // Fields removed before canonicalisation. See CANONICAL_HASH.md.
  var EXCLUDED_FIELDS = ["signature", "confidence"];

  // Detect environment: Node vs browser.
  var nodeCrypto = null;
  var subtle = null;
  if (typeof module !== "undefined" && module.exports && typeof require === "function") {
    try {
      nodeCrypto = require("crypto");
    } catch (e) {
      nodeCrypto = null;
    }
  }
  if (!nodeCrypto && global.crypto && global.crypto.subtle) {
    subtle = global.crypto.subtle;
  }

  // ── Public API ─────────────────────────────────────────────────

  /**
   * Serialise *draft* into canonical JSON bytes (Uint8Array).
   */
  function canonicalBytes(draft) {
    var root = {};
    var keys = Object.keys(draft);
    for (var i = 0; i < keys.length; i++) {
      if (EXCLUDED_FIELDS.indexOf(keys[i]) === -1) {
        root[keys[i]] = draft[keys[i]];
      }
    }
    var jsonStr = _canonicalise(root);
    return _utf8Encode(jsonStr);
  }

  /**
   * Synchronous SHA-256 hash (lowercase hex).
   * Works in Node. In the browser, use draftHashAsync() instead.
   */
  function draftHash(draft) {
    var bytes = canonicalBytes(draft);
    if (nodeCrypto) {
      return nodeCrypto.createHash("sha256").update(Buffer.from(bytes)).digest("hex");
    }
    if (subtle) {
      throw new Error(
        "draftHash(): browser Web Crypto is async. Use draftHashAsync() instead."
      );
    }
    return _sha256Hex(bytes);
  }

  /**
   * Async SHA-256 hash (lowercase hex) for browser environments.
   */
  function draftHashAsync(draft) {
    var bytes = canonicalBytes(draft);
    if (nodeCrypto) {
      return Promise.resolve(
        nodeCrypto.createHash("sha256").update(Buffer.from(bytes)).digest("hex")
      );
    }
    if (subtle) {
      return subtle.digest("SHA-256", bytes).then(function (hashBuffer) {
        var hex = "";
        var view = new Uint8Array(hashBuffer);
        for (var i = 0; i < view.length; i++) {
          hex += ("0" + view[i].toString(16)).slice(-2);
        }
        return hex;
      });
    }
    return Promise.resolve(_sha256Hex(bytes));
  }

  // ── Internal: recursive canonicaliser ──────────────────────────

  function _canonicalise(value) {
    if (value === null) {
      throw new Error(
        "null is forbidden in the canonical form; the server omits absent fields"
      );
    }
    if (typeof value === "boolean") return value ? "true" : "false";
    if (typeof value === "number") {
      if (!Number.isInteger(value)) {
        throw new Error(
          "float value " + value + " is forbidden in the canonical form; " +
          "use a decimal string instead"
        );
      }
      return String(value);
    }
    if (typeof value === "string") {
      return _escapeString(value);
    }
    if (Array.isArray(value)) {
      var parts = [];
      for (var i = 0; i < value.length; i++) {
        parts.push(_canonicalise(value[i]));
      }
      return "[" + parts.join(",") + "]";
    }
    if (typeof value === "object") {
      var keys = Object.keys(value);
      // Sort keys by Unicode CODE POINT, not UTF-16 code unit.
      keys.sort(function (a, b) {
        var ca = _codePointArray(a);
        var cb = _codePointArray(b);
        var len = Math.min(ca.length, cb.length);
        for (var i = 0; i < len; i++) {
          if (ca[i] !== cb[i]) return ca[i] - cb[i];
        }
        return ca.length - cb.length;
      });
      var parts = [];
      for (var i = 0; i < keys.length; i++) {
        var k = keys[i];
        parts.push(_escapeString(k) + ":" + _canonicalise(value[k]));
      }
      return "{" + parts.join(",") + "}";
    }
    throw new TypeError("unsupported type " + typeof value + " in canonical form");
  }

  function _codePointArray(str) {
    var chars = Array.from(str);
    var codes = [];
    for (var i = 0; i < chars.length; i++) {
      codes.push(chars[i].codePointAt(0));
    }
    return codes;
  }

  // ── Internal: string escaping ──────────────────────────────────

  function _escapeString(s) {
    var out = ['"'];
    var chars = Array.from(s);
    for (var i = 0; i < chars.length; i++) {
      var ch = chars[i];
      var cp = ch.codePointAt(0);
      if (cp >= 0xd800 && cp <= 0xdfff) {
        // Array.from() keeps valid pairs together, so a surrogate here is
        // unpaired. TextEncoder would silently turn it into U+FFFD.
        throw new Error(
          "lone UTF-16 surrogate U+" + cp.toString(16).toUpperCase() +
          " is forbidden in the canonical form"
        );
      } else if (ch === '"') {
        out.push('\\"');
      } else if (ch === "\\") {
        out.push("\\\\");
      } else if (cp === 0x0008) {
        out.push("\\b");
      } else if (cp === 0x0009) {
        out.push("\\t");
      } else if (cp === 0x000a) {
        out.push("\\n");
      } else if (cp === 0x000c) {
        out.push("\\f");
      } else if (cp === 0x000d) {
        out.push("\\r");
      } else if (cp <= 0x001f) {
        // Lowercase hex, matching backend/canonical.py.
        out.push("\\u" + ("0000" + cp.toString(16)).slice(-4));
      } else {
        out.push(ch);
      }
    }
    out.push('"');
    return out.join("");
  }

  // ── Internal: UTF-8 encoding ────────────────────────────────────

  function _utf8Encode(str) {
    if (typeof TextEncoder !== "undefined") {
      return new TextEncoder().encode(str);
    }
    var bytes = [];
    var chars = Array.from(str);
    for (var i = 0; i < chars.length; i++) {
      var cp = chars[i].codePointAt(0);
      if (cp <= 0x007f) {
        bytes.push(cp);
      } else if (cp <= 0x07ff) {
        bytes.push(0xc0 | (cp >> 6));
        bytes.push(0x80 | (cp & 0x3f));
      } else if (cp <= 0xffff) {
        bytes.push(0xe0 | (cp >> 12));
        bytes.push(0x80 | ((cp >> 6) & 0x3f));
        bytes.push(0x80 | (cp & 0x3f));
      } else {
        bytes.push(0xf0 | (cp >> 18));
        bytes.push(0x80 | ((cp >> 12) & 0x3f));
        bytes.push(0x80 | ((cp >> 6) & 0x3f));
        bytes.push(0x80 | (cp & 0x3f));
      }
    }
    return new Uint8Array(bytes);
  }

  // ── Internal: pure-JS SHA-256 (fallback for browser without SubtleCrypto) ─
  function _sha256Hex(bytes) {
    var K = [
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
      0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
      0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
      0x72be5b74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
      0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
      0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
      0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
      0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
      0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
      0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
      0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
      0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
      0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
      0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ];
    var H = [
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ];

    function rotr(x, n) { return ((x >>> n) | (x << (32 - n))) >>> 0; }

    var msg = bytes;
    var l = msg.length;
    var bitLen = l * 8;
    // Padding: msg + 0x80 + zeros + 8-byte big-endian length
    var totalLen = l + 1 + 8;
    var padLen = (64 - (totalLen % 64)) % 64;
    var paddedLen = totalLen + padLen;
    var padded = new Uint8Array(paddedLen);
    padded.set(msg);
    padded[l] = 0x80;
    padded[paddedLen - 4] = (bitLen >>> 24) & 0xff;
    padded[paddedLen - 3] = (bitLen >>> 16) & 0xff;
    padded[paddedLen - 2] = (bitLen >>> 8) & 0xff;
    padded[paddedLen - 1] = bitLen & 0xff;

    var W = new Array(64);
    for (var i = 0; i < paddedLen; i += 64) {
      for (var t = 0; t < 16; t++) {
        W[t] = ((padded[i + t * 4] << 24) |
                (padded[i + t * 4 + 1] << 16) |
                (padded[i + t * 4 + 2] << 8) |
                (padded[i + t * 4 + 3])) >>> 0;
      }
      for (var t = 16; t < 64; t++) {
        var s0 = rotr(W[t - 15], 7) ^ rotr(W[t - 15], 18) ^ (W[t - 15] >>> 3);
        var s1 = rotr(W[t - 2], 17) ^ rotr(W[t - 2], 19) ^ (W[t - 2] >>> 10);
        W[t] = (W[t - 16] + s0 + W[t - 7] + s1) >>> 0;
      }
      var a = H[0], b = H[1], c = H[2], d = H[3];
      var e = H[4], f = H[5], g = H[6], h = H[7];
      for (var t = 0; t < 64; t++) {
        var S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        var ch = (e & f) ^ (~e & g);
        var temp1 = (h + S1 + ch + K[t] + W[t]) >>> 0;
        var S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        var maj = (a & b) ^ (a & c) ^ (b & c);
        var temp2 = (S0 + maj) >>> 0;
        h = g; g = f; f = e;
        e = (d + temp1) >>> 0;
        d = c; c = b; b = a;
        a = (temp1 + temp2) >>> 0;
      }
      H[0] = (H[0] + a) >>> 0;
      H[1] = (H[1] + b) >>> 0;
      H[2] = (H[2] + c) >>> 0;
      H[3] = (H[3] + d) >>> 0;
      H[4] = (H[4] + e) >>> 0;
      H[5] = (H[5] + f) >>> 0;
      H[6] = (H[6] + g) >>> 0;
      H[7] = (H[7] + h) >>> 0;
    }

    var hex = "";
    for (var i = 0; i < 8; i++) {
      hex += ("00000000" + (H[i] >>> 0).toString(16)).slice(-8);
    }
    return hex;
  }

  // ── Export ─────────────────────────────────────────────────────

  var api = {
    canonicalBytes: canonicalBytes,
    draftHash: draftHash,
    draftHashAsync: draftHashAsync,
    EXCLUDED_FIELDS: EXCLUDED_FIELDS,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  global.canonical = api;
})(typeof globalThis !== "undefined" ? globalThis : typeof global !== "undefined" ? global : typeof window !== "undefined" ? window : this);
