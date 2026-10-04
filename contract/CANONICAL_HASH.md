# Canonical Hash Rule

This document defines how a transaction draft is turned into bytes for
hashing and signing. The same rule is implemented identically in Python
(`backend/canonical.py`) and JavaScript (`frontend/js/canonical.js`).
A third party should be able to implement this from the text alone and
produce byte-identical output.

## Overview

Given a draft as a JSON-deserialised object (a tree of objects, arrays,
strings, booleans and integers), the canonicaliser produces a deterministic
byte string. The SHA-256 of that byte string is the **draft hash**. The
WebAuthn challenge is the draft hash (see CONTRACT.md §4).

## The hashed object

**The hashed object is the server-stored draft as the plain dict produced
by `TransactionDraft.model_dump(mode="json", exclude_none=True)`.** Nothing
else is hashed: not the Pydantic model, not a python-mode dump, not a
client-supplied body, not the raw bytes of an HTTP response.

That dict has this shape, and `draft_hash()` rejects anything else:

- Only plain `dict`, `list`, `str`, `int` and `bool` values (and the
  excluded top-level `confidence` float). No `Decimal`, `datetime`,
  `Enum`, tuple or subclass of any of these (`TypeError`).
- **No `null` anywhere.** Absent optional fields are omitted, never
  null (`ValueError`). The model also rejects explicit nulls on input.
- Money (`amount.value`, `quantity`, `notional_amount`) is already a
  string such as `"50.00"`. It is never a `Decimal` or a float.
- Timestamps are serialised as UTC with a `Z` suffix and whole seconds:
  `YYYY-MM-DDTHH:MM:SSZ`. `+00:00`, `+08:00` and `Z` for the same instant
  all serialise to the same string, so they hash the same.

Parsing the server's JSON response with `JSON.parse` (frontend) or
`json.loads` (backend) yields the same tree, so both sides hash the same
object. `tests/test_canonical.py` checks that every fixture hashes the
same raw and after a round trip through the model.

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
  `\u00xx` with **lowercase** hex digits, e.g. `\u001f`, never `\u001F`.
- **Do NOT escape non-ASCII characters.** A Chinese character, an
  emoji, an accented letter — all are emitted as their raw UTF-8 bytes.
  Do not use `\uXXXX` escapes for them. DEL (U+007F), C1 controls and
  U+2028/U+2029 are also emitted raw.
- **Lone UTF-16 surrogates are an error.** A string containing an
  unpaired surrogate (U+D800–U+DFFF not part of a valid pair) is
  malformed input and must raise. It is not valid Unicode, so it has no
  UTF-8 encoding. Python's encoder raises, while JavaScript's
  `TextEncoder` silently replaces it with U+FFFD (which would make
  `"\ud800"` and `"�"` hash the same). Both implementations
  therefore reject it explicitly, in keys and values.

#### Integer

Emit the integer as its decimal string representation (e.g. `42`).

#### Float

**Raise an error.** Floats are forbidden. In Python, any value of type
`float` triggers the error. In JavaScript, any `number` where
`Number.isInteger(x)` is `false` triggers the error.

This is a hard stop, not a formatting choice. The canonicaliser must
not silently convert a float to a string.

#### Null

**Raise an error.** The hashed object never contains null (see "The
hashed object" above); a null means the caller hashed the wrong object.

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
Compare keys character by character by code point; if one key is a
prefix of the other, the shorter key sorts first.

The two orders differ in exactly one situation. A character above
U+FFFF (e.g. an emoji, U+1F600) is stored in UTF-16 as a surrogate pair
whose first unit is in U+D800–U+DBFF. A character in U+E000–U+FFFF
(private use, CJK compatibility, fullwidth forms such as `ｚ` U+FF5A) is a
single unit in that higher range. So:

| Keys | Code-point order (correct) | UTF-16 code-unit order (wrong) |
|---|---|---|
| `"ｚ"` (U+FF5A), `"😀"` (U+1F600) | `ｚ`, `😀` (0xFF5A < 0x1F600) | `😀`, `ｚ` (0xD83D < 0xFF5A) |

For every other pair of characters (both at or below U+D7FF, both in
U+E000–U+FFFF, or both above U+FFFF) the two orders agree. That is why
a vector with only ASCII and emoji keys cannot detect the bug: `z` < `😀`
in both orders.

The vector "key sort: U+FF5A vs emoji U+1F600" in `test_vectors.json`
exercises the difference. Both test suites prove it catches a naive
implementation: they run a deliberately wrong canonicaliser that uses
JavaScript's default `.sort()` (or Python's equivalent UTF-16 sort) and
assert that it agrees on ASCII keys but produces a different hash for
this vector.

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

- [x] Hashed object is `model_dump(mode="json", exclude_none=True)` of the server-stored draft
- [x] Keys sorted by Unicode code point at every nesting level
- [x] No whitespace
- [x] UTF-8 encoding
- [x] Non-ASCII characters not escaped
- [x] Control-character escapes use lowercase hex
- [x] Floats rejected (not formatted)
- [x] null rejected (absent fields are omitted)
- [x] Lone UTF-16 surrogates rejected
- [x] Arrays keep insertion order
- [x] All fields included except `signature` and `confidence`
- [x] Same output in Python and JavaScript for every valid hashed object
  (see CONTRACT.md "Known limitations" for inputs outside that shape,
  such as numbers other than small integers)
