"""
Speech-style normalisation, shared by the LLM parser, the stub and clarify.

The input is what a person says or types into a speech-to-text box: lower
case, no punctuation, fillers ("uh", "lah"), spoken amounts ("a grand"),
and sometimes no "to" ("send fifty dollars john smith").  The server
normalises; the frontend passes the text unchanged.

THE ORIGINAL TEXT IS NEVER MODIFIED (R1).  Everything here returns new
values.  The draft's ``transcript`` stays exactly what was submitted --
validator and audit depend on the raw wording -- and the normalised form
is used only internally: for the prompt, for matching, and for amounts.

Rules, and where each one lives
-------------------------------

R2  Never guess a payee.  :func:`person_reference` recognises "mum", "my
    wife", ...; a reference with no directory match is a payee question.
    Nothing here maps a relation to a directory entry.

R3  Money words become exact amounts, deterministically, in
    :func:`spoken_amount`.  Only exact, unambiguous phrases count:
    "a grand" = 1000.00, "fifteen" = 15.00, "two hundred fifty" = 250.00.
    "a few hundred", "some", "a bit", "a lot", "about fifty", "two fifty"
    are ``vague`` and always become an amount question, never a number.
    The model's number is a cross-check, never the source.

R4  Fillers are removed by :func:`normalise`, and only these: "lah",
    "lor", "leh", "meh", "hor", "ah", "uh", "um", and "like" when it sits
    between two words.  Only standalone words are touched (never "Sarah",
    "Allah", "likely"), and a word that is part of a directory payee's
    name is protected ("Ah Beng" keeps its "Ah").

R5  A spoken "to" may be missing.  :func:`payee_phrase` takes the recipient
    from after "to"/"for" if there is one, else from what remains after
    the verb and the amount.  The caller still has to match it against
    the directory: unique match -> draft, otherwise a question.

R6  Complete sentences only, but never a crash on lower case, missing
    punctuation, trailing whitespace, or empty input.

R7  The draft schema has no field for any of this, so none of it is
    stored.  :class:`Normalised.rules` and :class:`AmountReading.rule`
    exist for logs and tests only.

This module imports nothing from the ledger or gateway
(``tests/test_import_boundaries.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable, Literal, Optional

from backend.parser.directory import PayeeDirectory, account_clue_start

# ═══════════════════════════════════════════════════════════════════════
# R4: fillers
# ═══════════════════════════════════════════════════════════════════════

# Removed wherever they stand alone.
PURE_FILLERS: tuple[str, ...] = ("uh", "um", "lah", "lor", "leh", "meh", "hor", "ah")
# Removed only when it sits between two words ("send like fifty dollars").
CONTEXT_FILLER = "like"
FILLERS: tuple[str, ...] = PURE_FILLERS + (CONTEXT_FILLER,)

# A filler is a whole word: not preceded or followed by a letter, digit,
# apostrophe or hyphen, so "Sarah", "Allah", "ah-beng" and "um's" are safe.
_PURE_FILLER_RE = re.compile(
    r"(?<![\w'’-])(" + "|".join(PURE_FILLERS) + r")(?![\w'’-])",
    re.IGNORECASE,
)
_LIKE_RE = re.compile(r"(?<=[\w'’])\s+like\s+(?=[\w'’$])", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Normalised:
    """The result of :func:`normalise`.

    ``original`` is the input, untouched.  ``text`` is the working form,
    used only internally.  ``rules`` names each rule that changed
    something, in order (for logs and tests; never stored).
    """

    original: str
    text: str
    rules: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return self.text != self.original


def directory_name_words(directory: PayeeDirectory) -> frozenset[str]:
    """Every lower-case word in every payee's display name.

    These are protected from filler removal: "Ah" in "Tan Ah Kow" is a
    name, not a particle.
    """
    return frozenset(
        word.lower()
        for payee in directory.payees()
        for word in re.findall(r"[\w'’]+", payee.display_name)
    )


def normalise(text: str, *, protected_words: Iterable[str] = ()) -> Normalised:
    """Return the working form of *text* and the rules that produced it.

    Removes the R4 fillers and tidies whitespace.  Never lower-cases and
    never touches content words; case-insensitive matching is the
    caller's job.  *text* itself is not modified (strings are immutable;
    the point is that no caller ever stores the result as the transcript).
    """
    original = text if isinstance(text, str) else ""
    protected = frozenset(w.lower() for w in protected_words)
    rules: list[str] = []

    def drop_pure(m: re.Match) -> str:
        word = m.group(1).lower()
        if word in protected:
            return m.group(0)
        rules.append(f"remove_filler:{word}")
        return ""

    def drop_like(m: re.Match) -> str:
        if CONTEXT_FILLER in protected:
            return m.group(0)
        rules.append("remove_filler:like")
        return " "

    # "like" first: in "jane like lah" it sits between two words, and only
    # once the "lah" after it is gone would it look like it ends the text.
    work = _LIKE_RE.sub(drop_like, original)
    work = _PURE_FILLER_RE.sub(drop_pure, work)

    tidy = _WS_RE.sub(" ", work).strip()
    tidy = re.sub(r"\s+([,.;:!?])", r"\1", tidy)   # "jane ." -> "jane."
    tidy = re.sub(r"^[,.;:!?\s]+", "", tidy)         # a leading "uh," leaves a comma
    tidy = re.sub(r"([,;:])\s*(?:\1\s*)+", r"\1 ", tidy)
    tidy = re.sub(r"[,;:]+\s*([.!?])", r"\1", tidy)  # "jane, lor!" -> "jane!"
    tidy = re.sub(r"[,;:]+$", "", tidy)               # a trailing comma left behind
    if _WS_RE.sub(" ", original).strip() != original:
        rules.insert(0, "collapse_whitespace")
    return Normalised(original=original, text=tidy, rules=tuple(rules))


# ═══════════════════════════════════════════════════════════════════════
# R3: spoken amounts
# ═══════════════════════════════════════════════════════════════════════

_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "grand": 1000}

# Scales this module will not interpret: ask rather than guess.
_BIG_SCALES = frozenset({
    "million", "millions", "billion", "billions", "trillion", "lakh",
    "lakhs", "crore", "crores", "mil",
})
# "hundreds" / "thousands" are never exact.
_PLURAL_SCALES = frozenset({"hundreds", "thousands", "grands"})

# Words that make an amount unquantified.
_VAGUE = frozenset({
    "some", "few", "several", "couple", "lot", "lots", "bit", "bunch",
    "little", "many", "plenty", "ton", "tons", "loads", "heaps", "something",
    "enough", "half", "quarter",
})
# Words that make a stated amount approximate.
_HEDGES = frozenset({
    "about", "around", "roughly", "approximately", "approx", "maybe",
    "perhaps", "nearly", "almost", "over", "under", "circa", "estimated",
})
_TRAILING_HEDGES = frozenset({"ish", "odd"})

_CURRENCY_WORDS = frozenset({
    "dollar", "dollars", "buck", "bucks", "sgd", "usd", "rmb", "cents",
})

_TOKEN_RE = re.compile(
    r"(?P<digits>\$?\d[\d,]*(?:\.\d+)?(?:[kK](?![A-Za-z]))?)"
    r"|(?P<word>[A-Za-z]+)"
    r"|(?P<dash>[-‐-―])"
    r"|(?P<other>\S)"
)
_DIGITS_OK_RE = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


@dataclass(frozen=True)
class _Tok:
    kind: str
    text: str
    start: int
    end: int
    value: Optional[int] = None


def _tokenise(text: str) -> list[_Tok]:
    toks: list[_Tok] = []
    for m in _TOKEN_RE.finditer(text):
        raw = m.group()
        if m.lastgroup == "digits":
            toks.append(_Tok("digits", raw, m.start(), m.end()))
        elif m.lastgroup == "word":
            w = raw.lower()
            if w in _UNITS:
                toks.append(_Tok("unit", raw, m.start(), m.end(), _UNITS[w]))
            elif w in _TEENS:
                toks.append(_Tok("teen", raw, m.start(), m.end(), _TEENS[w]))
            elif w in _TENS:
                toks.append(_Tok("tens", raw, m.start(), m.end(), _TENS[w]))
            elif w == "hundred":
                toks.append(_Tok("hundred", raw, m.start(), m.end(), 100))
            elif w in ("thousand", "grand"):
                toks.append(_Tok("thousand", raw, m.start(), m.end(), 1000))
            elif w in _BIG_SCALES:
                toks.append(_Tok("bigscale", raw, m.start(), m.end()))
            elif w in _PLURAL_SCALES:
                toks.append(_Tok("plural", raw, m.start(), m.end()))
            elif w in ("a", "an"):
                toks.append(_Tok("article", raw, m.start(), m.end()))
            elif w == "and":
                toks.append(_Tok("and", raw, m.start(), m.end()))
            elif w == "of":
                toks.append(_Tok("of", raw, m.start(), m.end()))
            elif w in _VAGUE:
                toks.append(_Tok("vague", raw, m.start(), m.end()))
            elif w in _HEDGES:
                toks.append(_Tok("hedge", raw, m.start(), m.end()))
            else:
                toks.append(_Tok("word", raw, m.start(), m.end()))
        elif m.lastgroup == "dash":
            toks.append(_Tok("dash", raw, m.start(), m.end()))
        else:
            toks.append(_Tok("other", raw, m.start(), m.end()))
    return toks


_NUMERIC_KINDS = frozenset({"unit", "teen", "tens"})
_SCALE_KINDS = frozenset({"hundred", "thousand"})
_RUN_KINDS = _NUMERIC_KINDS | _SCALE_KINDS | {"digits", "bigscale", "plural"}


def _starts_run(toks: list[_Tok], i: int) -> bool:
    t = toks[i]
    if t.kind in _RUN_KINDS:
        return True
    return (
        t.kind == "article"
        and i + 1 < len(toks)
        and toks[i + 1].kind in _SCALE_KINDS
    )


def _continues_run(run: list[_Tok], toks: list[_Tok], j: int) -> bool:
    t, last = toks[j], run[-1]
    if run[0].kind == "digits":
        # digits may be followed by exactly one scale word ("5 hundred").
        return len(run) == 1 and t.kind in _SCALE_KINDS
    if t.kind in _RUN_KINDS:
        return True
    if t.kind == "and":
        return (
            last.kind in _SCALE_KINDS
            and j + 1 < len(toks)
            and toks[j + 1].kind in _NUMERIC_KINDS
        )
    if t.kind == "dash":
        return (
            last.kind == "tens"
            and j + 1 < len(toks)
            and toks[j + 1].kind == "unit"
        )
    return False


def _find_runs(toks: list[_Tok]) -> list[tuple[int, int]]:
    """Half-open ``(first, last+1)`` token index ranges of number phrases."""
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(toks):
        if _starts_run(toks, i):
            run = [toks[i]]
            j = i + 1
            while j < len(toks) and _continues_run(run, toks, j):
                run.append(toks[j])
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def _eval_words(run: list[_Tok]) -> Optional[int]:
    """Value of a run of number words, or None if it is not well formed."""
    total = hundreds = tens = units = 0
    has_h = has_t = has_u = has_teen = False
    thousands_used = False
    article = False
    for idx, t in enumerate(run):
        k = t.kind
        if k == "article":
            if idx != 0:
                return None
            article = True
            continue
        if k in ("dash", "and"):
            continue  # their position was validated when the run was built
        if k == "unit":
            if has_u or has_teen:
                return None
            units, has_u = t.value or 0, True
        elif k == "teen":
            if has_teen or has_t or has_u:
                return None
            tens, has_teen = t.value or 0, True
        elif k == "tens":
            if has_t or has_teen or has_u:
                return None
            tens, has_t = t.value or 0, True
        elif k == "hundred":
            if has_h or has_t or has_teen:
                return None
            if has_u:
                mult = units
            elif article:
                mult = 1
            else:
                return None
            hundreds, has_h = mult * 100, True
            units, has_u, article = 0, False, False
        elif k == "thousand":
            if thousands_used:
                return None
            group = hundreds + tens + units
            if group:
                mult = group
            elif article:
                mult = 1
            else:
                return None
            total += mult * 1000
            thousands_used = True
            hundreds = tens = units = 0
            has_h = has_t = has_u = has_teen = False
            article = False
        else:  # bigscale / plural / anything else: not exact
            return None
    total += hundreds + tens + units
    return total if 0 < total < 1_000_000 else None


def _eval_digits(run: list[_Tok]) -> Optional[Decimal]:
    raw = run[0].text.lstrip("$")
    k_suffix = raw[-1:] in ("k", "K")
    if k_suffix:
        raw = raw[:-1]
    if not _DIGITS_OK_RE.match(raw):
        return None
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    if k_suffix:
        if len(run) != 1:
            return None
        value *= 1000
    elif len(run) == 2:
        value *= run[1].value or 1
    if value <= 0 or value != value.quantize(Decimal("0.01")):
        return None
    return value


def _money(value: Decimal | int) -> str:
    return format(Decimal(value).quantize(Decimal("0.01")), "f")


@dataclass(frozen=True)
class AmountReading:
    """What :func:`spoken_amount` found.

    ``status``:
      ``exact``    exactly one unambiguous amount; ``value`` is "50.00".
      ``vague``    an amount that is unquantified, approximate, or not
                   well formed ("some", "a few hundred", "about fifty",
                   "two fifty", "a million").  Never becomes a number.
      ``multiple`` more than one number phrase; which one is the amount?
      ``none``     nothing that looks like an amount.
    """

    status: Literal["exact", "vague", "multiple", "none"]
    value: Optional[str] = None
    phrase: Optional[str] = None
    span: Optional[tuple[int, int]] = None
    rule: Optional[str] = None  # for logs and tests; never stored (R7)


def _modified_before(toks: list[_Tok], first: int) -> bool:
    """A vague or hedging word directly before the run ("about fifty",
    "a couple of hundred")."""
    k, seen = first - 1, 0
    while k >= 0 and seen < 2:
        kind = toks[k].kind
        if kind in ("vague", "hedge"):
            return True
        if kind in ("article", "of"):
            k -= 1
            continue
        seen += 1
        break
    return False


def _modified_after(toks: list[_Tok], last: int) -> bool:
    """"fifty or so", "fifty-ish", "fifty odd"."""
    nxt = [t.text.lower() for t in toks[last:last + 3]]
    if nxt[:2] == ["or", "so"]:
        return True
    return any(w in _TRAILING_HEDGES for w in nxt[:2])


def spoken_amount(text: str) -> AmountReading:
    """Read the amount in *text* deterministically (R3).

    Handles digits ("50", "50.50", "$1,000", "5k", "5 hundred") and
    number words ("fifteen", "twenty-five", "a grand", "two hundred
    fifty", "one thousand five hundred").  Anything that is not exact and
    unambiguous is ``vague`` or ``multiple``; nothing is ever rounded or
    guessed.  Digits after an account keyword ("account ending 8892") are
    not amounts and are ignored.
    """
    if not isinstance(text, str) or not text.strip():
        return AmountReading("none")

    cut = account_clue_start(text)
    toks = [t for t in _tokenise(text) if cut is None or t.start < cut]
    runs = _find_runs(toks)

    if not runs:
        if any(t.kind == "vague" for t in toks):
            return AmountReading("vague", rule="vague word, no number")
        return AmountReading("none")

    readings: list[AmountReading] = []
    for first, last in runs:
        run = toks[first:last]
        span = (run[0].start, run[-1].end)
        phrase = text[span[0]:span[1]]
        if run[0].kind == "digits":
            value = _eval_digits(run)
        else:
            n = _eval_words(run)
            value = Decimal(n) if n is not None else None
            # A lone "one".."nine" is a pronoun as often as a number
            # ("the one I owe"): exact only beside a currency word.
            if (
                value is not None
                and len(run) == 1
                and run[0].kind == "unit"
                and not _currency_beside(toks, first, last)
            ):
                value = None
        if (
            value is None
            or _modified_before(toks, first)
            or _modified_after(toks, last)
        ):
            readings.append(AmountReading("vague", phrase=phrase, span=span,
                                          rule=f"{phrase!r} is not exact"))
        else:
            readings.append(AmountReading(
                "exact", value=_money(value), phrase=phrase, span=span,
                rule=f"{phrase!r} -> {_money(value)}",
            ))

    if any(r.status == "vague" for r in readings):
        return next(r for r in readings if r.status == "vague")
    if len(readings) > 1:
        return AmountReading("multiple", rule=f"{len(readings)} number phrases")
    return readings[0]


def _currency_beside(toks: list[_Tok], first: int, last: int) -> bool:
    nxt = toks[last].text.lower() if last < len(toks) else ""
    prv = toks[first - 1].text.lower() if first > 0 else ""
    return nxt in _CURRENCY_WORDS or prv in ("sgd", "usd", "rmb")


# ═══════════════════════════════════════════════════════════════════════
# R5: the recipient phrase, with or without "to"
# ═══════════════════════════════════════════════════════════════════════

_VERB_RE = re.compile(r"\b(?:send|transfer|pay|give|wire|remit)\b", re.IGNORECASE)
_PREPOSITION_RE = re.compile(r"\b(?:to|for)\b", re.IGNORECASE)
_CURRENCY_AFTER_RE = re.compile(
    r"\s*(?:(?:us|singapore)\s+)?(?:(?:dollars?|bucks?|sgd|usd|rmb)(?![A-Za-z])|[块元])",
    re.IGNORECASE,
)
_CURRENCY_BEFORE_RE = re.compile(r"(?:sgd|usd|rmb|s\$|us\$)\s*$", re.IGNORECASE)
_MONEY_WORDS_RE = re.compile(r"\b(?:money|cash|dollars?|bucks?)\b", re.IGNORECASE)
_COURTESY_TAIL_RE = re.compile(r"\b(?:please|pls|thanks|thank\s+you)\W*$", re.IGNORECASE)
_EDGE_PUNCT = " \t,.;:!?"


def payee_phrase(text: str) -> Optional[str]:
    """The words that name the recipient, or None.

    After "to"/"for" if the sentence has one; otherwise whatever remains
    once the verb and the amount (with its currency word) are taken out,
    so a dropped "to" ("send fifty dollars john smith") still yields
    "john smith".  The result is a slice of *text*, case preserved.  It is
    only a candidate: the caller must match it against the directory and
    ask if it does not resolve to exactly one payee.
    """
    if not isinstance(text, str) or not text.strip():
        return None

    verb = _VERB_RE.search(text)
    start = verb.end() if verb else 0

    reading = spoken_amount(text)
    rest = text[start:]
    if reading.span is not None and reading.span[0] >= start:
        s, e = reading.span
        before = _CURRENCY_BEFORE_RE.search(text[start:s])
        if before:
            s = start + before.start()
        after = _CURRENCY_AFTER_RE.match(text, e)
        if after:
            e = after.end()
        rest = text[start:s] + " " + text[e:]

    # "send money jane": the words "money"/"cash"/"dollars" are not a name.
    rest = _MONEY_WORDS_RE.sub(" ", rest)

    prep = _PREPOSITION_RE.search(rest)
    if prep:
        rest = rest[prep.end():]

    rest = rest.strip(_EDGE_PUNCT)
    rest = _COURTESY_TAIL_RE.sub("", rest).strip(_EDGE_PUNCT)
    return rest or None


# ═══════════════════════════════════════════════════════════════════════
# R2: person references
# ═══════════════════════════════════════════════════════════════════════

PERSON_WORDS: tuple[str, ...] = (
    "mum", "mom", "ma", "dad", "wife", "husband", "boss", "brother", "sister",
)
_PERSON_RE = re.compile(
    r"^\s*(?:my\s+)?(" + "|".join(PERSON_WORDS) + r")(?![\w])",
    re.IGNORECASE,
)


def person_reference(phrase: Optional[str]) -> Optional[str]:
    """Return "mum" / "my wife" if *phrase* is a relation, else None.

    A relation is not a name.  This never looks anything up and never maps
    a relation to a payee (R2): the caller asks for a payee unless the
    directory itself matches the words.
    """
    if not phrase:
        return None
    m = _PERSON_RE.match(phrase)
    if not m:
        return None
    return _WS_RE.sub(" ", m.group(0).strip().lower())
