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

    *draft* must be the hashed object defined in CANONICAL_HASH.md: the plain
    dict produced by ``TransactionDraft.model_dump(mode="json", exclude_none=True)``
    (or the identical JSON parsed with ``json.loads``). Anything else raises.

    Rules (see CANONICAL_HASH.md):
      - ``signature`` and ``confidence`` are removed from the top level.
      - Object keys are sorted by Unicode code point at every nesting level.
      - No whitespace between tokens.
      - Non-ASCII characters are NOT escaped (emitted as raw UTF-8).
      - Only ``"``, ``\\``, and control characters are escaped (lowercase hex).
      - Only exact ``dict``, ``list``, ``str``, ``int`` and ``bool`` are accepted.
        Floats and null raise ``ValueError``; Decimal, datetime, Enum and any
        other type (including subclasses of str/int) raise ``TypeError``.
      - Lone UTF-16 surrogates in strings raise ``ValueError``.
      - Arrays keep their order.
      - Unknown/extra fields are included (nothing unsigned).
    """
    if type(draft) is not dict:
        raise TypeError(f"draft must be a plain dict, not {type(draft).__name__}")
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
    # Exact type checks on purpose: a str-Enum, an int subclass or a dict
    # subclass means the caller did not pass the plain JSON-mode dump.
    kind = type(value)
    if value is None:
        raise ValueError(
            "null is forbidden in the canonical form; omit the field "
            '(model_dump(mode="json", exclude_none=True))'
        )
    if kind is bool:
        return "true" if value else "false"
    if kind is float:
        raise ValueError(
            f"float value {value!r} is forbidden in the canonical form; "
            "use a decimal string instead"
        )
    if kind is int:
        return str(value)
    if kind is str:
        return _escape_string(value)
    if kind is list:
        return "[" + ",".join(_canonicalise(item) for item in value) + "]"
    if kind is dict:
        for k in value:
            if type(k) is not str:
                raise TypeError(f"object keys must be str, not {type(k).__name__}")
        # Sort keys by Unicode code point.  Python strings are sequences of
        # code points, so ``ord(c)`` gives the true numeric value even for
        # characters outside the BMP.
        sorted_keys = sorted(value.keys(), key=lambda k: [ord(c) for c in k])
        parts: list[str] = []
        for k in sorted_keys:
            parts.append(_escape_string(k) + ":" + _canonicalise(value[k]))
        return "{" + ",".join(parts) + "}"

    # Decimal, datetime, Enum, tuple, or anything else that snuck through.
    raise TypeError(
        f"unsupported type {kind.__name__} in canonical form; "
        'hash model_dump(mode="json", exclude_none=True), not the model or a python-mode dump'
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
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            # A lone surrogate (json.loads turns a valid pair into one code
            # point, so any surrogate left here is unpaired). Not valid
            # Unicode; JavaScript would silently encode it as U+FFFD.
            raise ValueError(f"lone UTF-16 surrogate U+{cp:04X} is forbidden in the canonical form")
        if ch in _ESCAPE_MAP:
            out.append(_ESCAPE_MAP[ch])
        elif cp <= 0x001F:
            # Other control characters (U+0000–U+001F), lowercase hex.
            out.append("\\u%04x" % cp)
        else:
            # Non-ASCII and printable ASCII are emitted raw.
            out.append(ch)
    out.append('"')
    return "".join(out)
