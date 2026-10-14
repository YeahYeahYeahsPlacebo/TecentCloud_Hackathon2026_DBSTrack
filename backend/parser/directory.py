"""
Payee directory protocol and fixture implementation.

The directory is the server-side source of truth for payee lookups
and ticker resolution.  It deliberately lives in ``backend/parser``
(not in the ledger or gateway) so the parser can resolve mentions
without importing the ledger (CONTEXT.md constraint 2).

DESIGN RULE: the model only extracts; code builds the draft.  The
model never supplies payee ids, masked account numbers or draft
fields directly.  The directory — not the model — maps a
``payee_mention`` string to a concrete ``{id, display_name,
masked_account}`` record.

Member 3's service will pass a real directory implementation at
runtime via the :class:`PayeeDirectory` protocol.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PayeeRecord:
    """A single payee entry from the directory.

    Attributes:
        id: Server-side payee identifier (never supplied by the model).
        display_name: Human-readable name shown to the user.
        masked_account: Masked destination account, e.g. ``****1234``.
    """

    id: str
    display_name: str
    masked_account: str


@runtime_checkable
class PayeeDirectory(Protocol):
    """Protocol for payee and ticker lookups.

    The parser uses this — never the model — to resolve payee mentions
    and stock tickers.  Member 3's service will implement this against
    the real user's payee list.

    Methods:
        payees: Return all payee records known to this directory.
        default_source_account: Return the default funding account.
        tickers: Map equity names/symbols to ticker codes.
    """

    def payees(self) -> list[PayeeRecord]:
        """Return all payee records in the directory."""
        ...

    def default_source_account(self) -> str:
        """Return the default source (funding) account for transfers.

        This is a stated product rule shown to the user on the overlay,
        not a guess: when the user does not name a source account,
        this account is used.
        """
        ...

    def tickers(self) -> dict[str, str]:
        """Map equity names to ticker symbols.

        Keys are case-insensitive lookup names (e.g. ``"dbs"``);
        values are ticker symbols (e.g. ``"D05.SI"``).
        """
        ...


class FixtureDirectory:
    """Hard-coded directory for tests and development.

    Implements :class:`PayeeDirectory` with a small fixed payee list
    and ticker map.  No database, no network, no ledger imports.
    """

    _PAYEES: tuple[PayeeRecord, ...] = (
        PayeeRecord(id="payee-001", display_name="John Smith", masked_account="****1234"),
        PayeeRecord(id="payee-002", display_name="Jane Tan", masked_account="****5678"),
        PayeeRecord(id="payee-101", display_name="John Doe", masked_account="****4521"),
        PayeeRecord(id="payee-102", display_name="John Lee", masked_account="****8892"),
    )

    _DEFAULT_SOURCE_ACCOUNT = "acct-001-2345"

    _TICKERS: dict[str, str] = {
        "dbs": "D05.SI",
        "es3": "ES3.SI",
        "apple": "AAPL",
        "google": "GOOGL",
    }

    def payees(self) -> list[PayeeRecord]:
        return list(self._PAYEES)

    def default_source_account(self) -> str:
        return self._DEFAULT_SOURCE_ACCOUNT

    def tickers(self) -> dict[str, str]:
        return dict(self._TICKERS)


# ── Singlish discourse particles ──────────────────────────────────────
#
# Singapore English often ends an utterance with a discourse particle
# ("Transfer 50 bucks to Jane lah.").  The particle carries no payee
# information, so a mention such as "Jane lah" would otherwise match
# nobody.  Only ONE trailing particle (a separate word) and trailing
# punctuation are removed, and only after the mention as given has
# failed to match.  Nothing is ever removed from the middle of a name.

_SINGLISH_PARTICLES = frozenset({"lah", "leh", "lor", "meh", "ah", "hor"})

_TRAILING_PUNCT_RE = re.compile(r"[\s.,!?;:~。，！？]+$")


def _strip_trailing_particle(mention: str) -> str:
    """Remove trailing punctuation, one trailing particle word, then
    trailing punctuation again.  A one-word mention is never removed."""
    cleaned = _TRAILING_PUNCT_RE.sub("", mention)
    words = cleaned.split()
    if len(words) >= 2 and words[-1].lower() in _SINGLISH_PARTICLES:
        cleaned = " ".join(words[:-1])
    return _TRAILING_PUNCT_RE.sub("", cleaned)


# ── Account clues ("account ending 8892", "****8892") ────────────────
#
# A payee mention can carry an account clue.  The clue must AGREE with
# the directory: the candidate has to match the name AND the last four
# digits of its masked account.  A payee whose account digits
# contradict the mention is never returned, so the parser never drafts
# one and the validator never accepts one.
#
# Rules (shared by the parser, clarify and the validator):
#
# - A clue is a keyword ("account", "acct", "a/c", "ending", "ends",
#   "last four"), a masked form ("****8892", "xxxx8892"), or a run of
#   4+ digits.  A digit run followed by a currency word, preceded by
#   "$", or followed by decimals is an AMOUNT, not a clue.
# - The clue can sit in the mention itself ("John Smith's account
#   ending 8892") or just after it in the transcript, because a model
#   often returns only "John Smith".  ``context`` is that transcript.
# - The digits compared are the LAST FOUR of the digit run.
# - A clue with no digits ("John's account") adds no constraint.
# - A mention that is only a clue (no name) matches nobody, so an
#   account number alone never selects a payee here.  See
#   :func:`match_account_suffix` for the clarification-only exception.

_CLUE_KEYWORD_RE = re.compile(
    r"\b(?:account|acct|a/c|ending|ends|last\s+(?:four|4))\b", re.IGNORECASE
)
_CLUE_MASK_RE = re.compile(r"[*xX\u2022]{2,}\s*(?=\d{4})")
_CURRENCY_AFTER = (
    r"(?!\d|\.\d|,\d|\s*(?:dollars?|bucks?|sgd|usd|rmb|s\$|us\$|\u5757|\u5143)\b)"
)
_CLUE_DIGITS_RE = re.compile(r"(?<![\w$.,])\d{4,}" + _CURRENCY_AFTER, re.IGNORECASE)
_ANY_DIGITS_RE = re.compile(r"\d{4,}")
_TRAILING_CONNECTOR_RE = re.compile(
    r"(?:\s*(?:\'s|\u2019s|\'|\u2019)|\s+(?:at|with|in|on|from|of|for))+\s*$",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"(?<!\d)[.;!?](?=\s|$)|\n")
_CONTEXT_WINDOW = 60


@dataclass(frozen=True)
class PayeeClue:
    """A payee mention split into its name and its account clue.

    ``name`` is the mention with the clue removed.  ``digits`` is the
    first run of 4+ digits in the clue (``None`` if the clue has none),
    and ``last4`` its last four digits.
    """

    name: str
    has_clue: bool
    digits: str | None = None

    @property
    def last4(self) -> str | None:
        return self.digits[-4:] if self.digits else None


def account_clue_start(text: str) -> int | None:
    """Index of the first account keyword or masked form in *text*, or None.

    Everything from there on is an account clue ("account ending 8892"),
    not an amount; the amount reader ignores it.
    """
    starts = [
        m.start()
        for rx in (_CLUE_KEYWORD_RE, _CLUE_MASK_RE)
        for m in [rx.search(text)]
        if m
    ]
    return min(starts) if starts else None


def parse_payee_clue(text: str) -> PayeeClue:
    """Split *text* into a payee name and an account clue."""
    starts = []
    for rx in (_CLUE_KEYWORD_RE, _CLUE_MASK_RE, _CLUE_DIGITS_RE):
        m = rx.search(text)
        if m:
            starts.append(m.start())
    if not starts:
        return PayeeClue(name=text.strip(), has_clue=False)
    start = min(starts)
    name = _TRAILING_CONNECTOR_RE.sub("", text[:start].strip()).strip()
    run = _ANY_DIGITS_RE.search(text[start:])
    return PayeeClue(name=name, has_clue=True, digits=run.group() if run else None)


def _clue_after(mention: str, context: str) -> PayeeClue:
    """The clue (if any) in the context text right after *mention*."""
    at = context.lower().find(mention.strip().lower())
    if at < 0:
        return PayeeClue(name=mention.strip(), has_clue=False)
    tail = context[at + len(mention.strip()):]
    end = _SENTENCE_END_RE.search(tail)
    tail = (tail[: end.start()] if end else tail)[:_CONTEXT_WINDOW]
    clue = parse_payee_clue(tail)
    return PayeeClue(name=mention.strip(), has_clue=clue.has_clue, digits=clue.digits)


def _masked_last4(masked_account: str) -> str | None:
    m = re.search(r"(\d{4})\D*$", masked_account)
    return m.group(1) if m else None


def match_payees(
    mention: str | None,
    directory: PayeeDirectory,
    *,
    context: str | None = None,
) -> list[PayeeRecord]:
    """Return payee records whose display_name matches the mention.

    Shared by the parser, clarify and the validator.

    Matching is case-insensitive.  Two strategies are used:

    1. **Exact match**: the mention equals the full display name
       (case-insensitive).  Returns a single match if found.
    2. **Name-component match**: the mention matches a first name
       or surname component.  For example, ``"John"`` matches any
       payee whose first name (first word of display_name) is
       ``"John"``, and ``"Smith"`` matches any payee whose surname
       (last word) is ``"Smith"``.

    If the mention as given matches nobody, one trailing Singlish
    particle (``lah``, ``leh``, ``lor``, ``meh``, ``ah``, ``hor``) and
    trailing punctuation are stripped and matching is tried once more,
    so ``"Jane lah"`` matches ``"Jane Tan"``.

    **Account clue.**  If the mention (or the *context* text right
    after it) carries account digits, a payee must match the name AND
    the last four digits of its masked account; a contradicting payee
    is never returned.  See "Account clues" above.

    Returns an empty list when ``mention`` is ``None`` or empty, or
    when no payee matches.
    """
    if not mention or not mention.strip():
        return []
    clue = parse_payee_clue(mention)
    if not clue.has_clue and context:
        clue = _clue_after(mention, context)
    if not clue.name:
        return []  # an account number alone names nobody

    results = _match_name(clue.name, directory)
    if clue.last4:
        results = [p for p in results if _masked_last4(p.masked_account) == clue.last4]
    return results


def match_account_suffix(
    text: str | None,
    directory: PayeeDirectory,
) -> list[PayeeRecord] | None:
    """Match a clarification answer that is ONLY a masked-account suffix.

    Returns ``None`` if *text* is not an account-only clue of exactly
    four digits ("account ending 8892", "****8892", "8892").  Otherwise
    returns the directory payees whose masked account ends with those
    digits (possibly none, possibly several).  Used only to answer a
    payee question; a longer number is not a masked suffix and is
    refused.
    """
    if not text or not text.strip():
        return None
    clue = parse_payee_clue(text)
    if not clue.has_clue or clue.name or not clue.digits or len(clue.digits) != 4:
        return None
    return [p for p in directory.payees() if _masked_last4(p.masked_account) == clue.digits]


def _match_name(mention: str, directory: PayeeDirectory) -> list[PayeeRecord]:
    """Name matching, retrying once without a trailing Singlish particle."""
    results = _match(mention, directory)
    if results:
        return results
    stripped = _strip_trailing_particle(mention)
    if stripped and stripped != mention.strip():
        return _match(stripped, directory)
    return []


def _match(mention: str, directory: PayeeDirectory) -> list[PayeeRecord]:
    needle = mention.strip().lower()
    results: list[PayeeRecord] = []

    for p in directory.payees():
        name_lower = p.display_name.lower()
        # Exact match (full name).
        if needle == name_lower:
            results.append(p)
            continue
        # First-name match: mention matches the first word.
        words = name_lower.split()
        if words and needle == words[0]:
            results.append(p)
            continue
        # Surname match: mention matches the last word.
        if words and needle == words[-1]:
            results.append(p)
            continue

    return results


def resolve_ticker(
    mention: str | None,
    directory: PayeeDirectory,
) -> str | None:
    """Resolve a ticker mention to a ticker symbol via the directory.

    Looks up the mention (case-insensitive) in the directory's
    ``tickers()`` map.  Returns ``None`` if the mention is missing,
    empty, or not found.
    """
    if not mention or not mention.strip():
        return None
    key = mention.strip().lower()
    return directory.tickers().get(key)
