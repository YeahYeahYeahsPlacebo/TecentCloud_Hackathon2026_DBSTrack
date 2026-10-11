"""
Text sanitiser shared by the parser, the validator, and
:func:`backend.parser.clarify.resolve_question`.

User text is data, never instructions (CONTEXT.md constraint 5).  Every
entry point cleans the transcript and any clarification answer **once**,
with :func:`sanitise`, and then uses that same cleaned text for the
prompt and for every verbatim and length check.  A check must never run
on different text from the text the model saw.

The sanitiser:

1. removes invisible format characters (Unicode category ``Cf``: zero
   width space/joiners, word joiner, BOM, soft hyphen, bidi controls),
   which an attacker could use to split a keyword or a name;
2. normalises whitespace controls (tab, CR, VT, FF) to a space or a
   newline, so two words are never glued together, and removes every
   other control character;
3. removes the ``<<<...>>>`` prompt markers, repeating until none is
   left, so user text cannot close a delimited prompt block (and a
   marker split by another marker cannot reassemble).

Normal visible text passes through unchanged.
"""

from __future__ import annotations

import unicodedata

# The delimiters and placeholders used by the parser, clarify and
# validator prompt templates.
MARKERS: tuple[str, ...] = (
    "<<<TRANSCRIPT>>>",
    "<<<END>>>",
    "<<<ANSWER>>>",
    "<<<FIELD>>>",
    "<<<USER_ANSWER>>>",
)

# Whitespace controls are replaced, not removed, so "Send\tfifty" stays
# two words.  \r\n is handled first so it becomes one newline.
_WHITESPACE_CONTROLS = {"\t": " ", "\v": " ", "\f": " ", "\r": "\n"}


def strip_markers(text: str) -> str:
    """Remove every prompt marker from *text*, repeating until stable.

    A single pass is not enough: ``"<<<EN<<<END>>>D>>>"`` becomes
    ``"<<<END>>>"`` after one pass.
    """
    previous = None
    result = text
    while result != previous:
        previous = result
        for marker in MARKERS:
            result = result.replace(marker, "")
    return result


def _clean_chars(text: str) -> str:
    """Drop format characters, normalise whitespace controls, drop other controls."""
    text = text.replace("\r\n", "\n")
    out: list[str] = []
    for ch in text:
        if ch == "\n":
            out.append(ch)
        elif ch in _WHITESPACE_CONTROLS:
            out.append(_WHITESPACE_CONTROLS[ch])
        elif unicodedata.category(ch) in ("Cf", "Cc"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def sanitise(text: str) -> str:
    """Return the cleaned form of a transcript or clarification answer.

    Characters are cleaned before markers are stripped, so
    ``"<<<E\\u200bND>>>"`` is recognised as a marker.
    """
    if not text:
        return text
    return strip_markers(_clean_chars(text))
