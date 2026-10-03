"""
Canonical serialisation and hashing of transaction drafts.

This module turns a draft dict into deterministic bytes for SHA-256 hashing
and signing. The same logic is implemented in JavaScript in
``frontend/js/canonical.js``; the two must agree byte-for-byte.

See ``contract/CANONICAL_HASH.md`` for the full specification.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Fields removed before canonicalisation. See CANONICAL_HASH.md for rationale.
EXCLUDED_FIELDS: frozenset[str] = frozenset({"signature", "confidence"})


def canonical_bytes(draft: dict) -> bytes:
    """
    Serialise *draft* into canonical JSON bytes.

    Rules (see CANONICAL_HASH.md):
      - ``signature`` and ``confidence`` are removed from the top level.
      - Object keys are sorted by Unicode code point at every nesting level.
      - No whitespace between tokens.
      - Non-ASCII characters are NOT escaped (emitted as raw UTF-8).
      - Only ``"``, ``\\``, and control characters are escaped.
      - Floats raise ``ValueError``; integers are allowed.
      - Arrays keep their order.
      - null serialises as ``null``.
      - Unknown/extra fields are included (nothing unsigned).
    """
    # Remove excluded fields from a shallow copy so the caller's dict is
    # not mutated.
    root = {k: v for k, v in draft.items() if k not in EXCLUDED_FIELDS}
    return _canonicalise(root).encode("utf-8")


def draft_hash(draft: dict) -> str:
    """Return the lowercase hex SHA-256 of the canonical bytes of *draft*."""
    return hashlib.sha256(canonical_bytes(draft)).hexdigest()


# ── Internal helpers ──────────────────────────────────────────────────


def _canonicalise(value: Any) -> str:
    """Recursively canonicalise *value* into a JSON string."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        raise ValueError(
            f"float value {value!r} is forbidden in the canonical form; "
            "use a decimal string instead"
        )
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _escape_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_canonicalise(item) for item in value) + "]"
    if isinstance(value, dict):
        # Sort keys by Unicode code point.  Python strings are sequences of
        # code points, so ``ord(c)`` gives the true numeric value even for
        # characters outside the BMP.
        sorted_keys = sorted(value.keys(), key=lambda k: [ord(c) for c in k])
        parts: list[str] = []
        for k in sorted_keys:
            parts.append(_escape_string(k) + ":" + _canonicalise(value[k]))
        return "{" + ",".join(parts) + "}"

    # Decimal, datetime, or anything else that snuck through.
    # If it's a Decimal, it should have been a string at this point.
    raise TypeError(
        f"unsupported type {type(value).__name__} in canonical form"
    )


# Minimal JSON string escaping: only what JSON requires.
# Do NOT escape non-ASCII — emit it as raw UTF-8.
_ESCAPE_MAP: dict[str, str] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _escape_string(s: str) -> str:
    """Escape *s* per the canonical rule: quotes, backslash, controls only."""
    out: list[str] = ['"']
    for ch in s:
        if ch in _ESCAPE_MAP:
            out.append(_ESCAPE_MAP[ch])
        elif ord(ch) <= 0x001F:
            # Other control characters (U+0000–U+001F)
            out.append("\\u%04x" % ord(ch))
        else:
            # Non-ASCII and printable ASCII are emitted raw.
            out.append(ch)
    out.append('"')
    return "".join(out)
