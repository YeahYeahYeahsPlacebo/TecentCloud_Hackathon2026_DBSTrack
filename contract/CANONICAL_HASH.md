# Canonical Hash Rule

This document defines how a transaction draft is turned into bytes for
hashing and signing. The same rule is implemented identically in Python
(`backend/canonical.py`) and JavaScript (`frontend/js/canonical.js`).
A third party should be able to implement this from the text alone and
produce byte-identical output.

## Overview

Given a draft as a JSON-deserialised object (a tree of objects, arrays,
strings, nulls, and integers), the canonicaliser produces a deterministic
byte string. The SHA-256 of that byte string is the **draft hash**. The
signature is over the draft hash.

## Excluded fields

Two fields are **removed** before canonicalisation:

| Field | Reason |
|---|---|
| `signature` | This is the *output* of signing, not part of the data being signed. Including it would create a circular dependency. |
| `confidence` | A float (0.0–1.0). It is a parser diagnostic, never shown to the user, and floats are forbidden in the canonical form. |

**No other field is excluded.** Unknown or extra fields are included so
that nothing can be smuggled into a draft unsigned. If a field exists in
the input, it is in the canonical bytes.

## Canonicalisation algorithm

### 1. Remove excluded fields

Delete `signature` and `confidence` from the top-level object. Do this
once at the root; do not recurse into nested objects looking for these
keys (a nested key that happens to be named `confidence` is legitimate
data and stays).

### 2. Recursively canonicalise

Process the value as follows, by type:

#### Object (`{}`)

1. Sort keys by **Unicode code point** (see "Key sorting" below).
2. For each key in sorted order:
   - Emit `"` + escaped key + `"` + `:`
   - Emit the canonicalised value
3. Join with `,`
4. Wrap in `{` and `}`

#### Array (`[]`)

1. Keep the original element order (arrays are NOT sorted).
2. Canonicalise each element.
3. Join with `,`
4. Wrap in `[` and `]`

#### String

Emit `"` + escaped string + `"`.

Escaping rules:
- Escape `"` as `\"`
- Escape `\` as `\\`
- Escape control characters U+0000–U+001F using the shortest standard
  JSON escape: `\b` (U+0008), `\t` (U+0009), `\n` (U+000A),
  `\f` (U+000C), `\r` (U+000D). All other control characters use
  `\u00XX`.
- **Do NOT escape non-ASCII characters.** A Chinese character, an
  emoji, an accented letter — all are emitted as their raw UTF-8 bytes.
  Do not use `\uXXXX` escapes for them.

#### Integer

Emit the integer as its decimal string representation (e.g. `42`).

#### Float

**Raise an error.** Floats are forbidden. In Python, any value of type
`float` triggers the error. In JavaScript, any `number` where
`Number.isInteger(x)` is `false` triggers the error.

This is a hard stop, not a formatting choice. The canonicaliser must
not silently convert a float to a string.

#### Null

Emit the four characters `null`.

#### Boolean

Emit `true` or `false`.

### 3. Encode as UTF-8

The canonical JSON string (produced by step 2) is encoded as UTF-8.
The resulting bytes are the output of `canonical_bytes()`.

### 4. Hash

`draft_hash()` = SHA-256 of the canonical bytes, as a lowercase
hexadecimal string.

## Key sorting

Keys are sorted by **Unicode code point**, not by UTF-16 code unit.

This distinction matters for characters outside the Basic Multilingual
Plane (code points U+10000 and above, such as emoji). In UTF-16 these
are represented as surrogate pairs (two 16-bit units). Sorting by UTF-16
code units would place surrogate pairs after all BMP characters, which
is **wrong** — a surrogate pair encodes a single code point that is
numerically larger than any BMP code point, so it sorts after all BMP
characters, but among themselves surrogate pairs must be ordered by
their decoded code point.

### How each implementation honours this

**Python:** `sorted(keys, key=lambda k: [ord(c) for c in k])` sorts by
the numeric value of each character's Unicode code point. Python strings
are sequences of code points, so `ord(c)` returns the full code point
value even for characters outside the BMP.

**JavaScript:** JavaScript strings are UTF-16. Sorting by `.charCodeAt()`
or default `.sort()` would use UTF-16 code units (wrong). Instead, the
implementation spreads each key into an array of code points using
`[...k]` (which correctly splits surrogate pairs into single elements),
then sorts by the numeric value of each code point using
`.codePointAt(0)`.

## No whitespace

There is no whitespace between any tokens. No spaces, no newlines, no
indentation. The output is a single dense JSON string.

## Determinism checklist

- [x] Keys sorted by Unicode code point at every nesting level
- [x] No whitespace
- [x] UTF-8 encoding
- [x] Non-ASCII characters not escaped
- [x] Floats rejected (not formatted)
- [x] Arrays keep insertion order
- [x] null serialised as `null`
- [x] All fields included except `signature` and `confidence`
- [x] Same output in Python and JavaScript
