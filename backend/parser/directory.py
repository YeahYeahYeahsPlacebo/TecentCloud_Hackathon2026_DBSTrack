"""
Payee and ticker directory for the parser.

A directory is a read-only lookup of known payees and tickers.  The
parser and clarification functions use it to resolve payee mentions and
ticker symbols — they never guess a payee or ticker that is not in the
directory.

:class:`FixtureDirectory` is a hard-coded directory that matches the
payees in the stub parser and the fixtures.  It exists so tests and the
eval script can construct a parser without a real backend.

Member 3's service may provide its own directory implementation (backed
by a database) by implementing the :class:`PayeeDirectory` and
:class:`TickerDirectory` protocols.

This module has no storage and no HTTP — it is pure data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PayeeRecord:
    """A payee entry in the directory.

    Fields match :class:`backend.models.draft.Payee` so the parser can
    copy them directly into a draft.
    """

    id: str
    display_name: str
    masked_account: str


@dataclass(frozen=True)
class TickerRecord:
    """A ticker entry in the directory."""

    mention: str
    symbol: str


class PayeeDirectory(Protocol):
    """Read-only payee lookup."""

    def payees(self) -> list[PayeeRecord]: ...

    def find_payee(self, mention: str) -> list[PayeeRecord]:
        """Return payees whose display name matches the mention.

        A full-name match (case-insensitive) returns one payee.
        A partial match (first name only) may return multiple.
        No match returns an empty list.
        """
        ...


class TickerDirectory(Protocol):
    """Read-only ticker lookup."""

    def tickers(self) -> list[TickerRecord]: ...

    def resolve_ticker(self, mention: str) -> str | None:
        """Return the canonical ticker symbol for a mention, or None."""
        ...


@dataclass
class FixtureDirectory:
    """Hard-coded directory matching the stub parser and fixtures.

    Payees:
        payee-001  John Smith   ****1234   (clean transfer)
        payee-002  Jane Tan     ****5678
        payee-101  John Doe     ****4521   (ambiguous payee)
        payee-102  John Smith   ****8892   (ambiguous payee)

    Tickers:
        dbs → D05.SI
        es3 → ES3.SI
        apple → AAPL
        google → GOOGL

    Source account:
        acct-001-2345
    """

    _payees: list[PayeeRecord] = field(default_factory=lambda: [
        PayeeRecord(id="payee-001", display_name="John Smith", masked_account="****1234"),
        PayeeRecord(id="payee-002", display_name="Jane Tan", masked_account="****5678"),
        PayeeRecord(id="payee-101", display_name="John Doe", masked_account="****4521"),
        PayeeRecord(id="payee-102", display_name="John Smith", masked_account="****8892"),
    ])

    _tickers: list[TickerRecord] = field(default_factory=lambda: [
        TickerRecord(mention="dbs", symbol="D05.SI"),
        TickerRecord(mention="es3", symbol="ES3.SI"),
        TickerRecord(mention="apple", symbol="AAPL"),
        TickerRecord(mention="google", symbol="GOOGL"),
    ])

    source_account: str = "acct-001-2345"

    # ── PayeeDirectory ───────────────────────────────────────────────

    def payees(self) -> list[PayeeRecord]:
        return list(self._payees)

    def find_payee(self, mention: str) -> list[PayeeRecord]:
        """Return payees matching the mention (case-insensitive).

        Matching strategy:
        1. Exact full-name match → one result.
        2. If no exact match, match by first name (first token of
           display_name) → zero or more results.
        3. If no first-name match, return empty.
        """
        mention_lower = mention.strip().lower()
        if not mention_lower:
            return []

        # Exact full-name match.
        exact = [
            p for p in self._payees
            if p.display_name.lower() == mention_lower
        ]
        if exact:
            return exact

        # First-name (first token) match.
        first_token = mention_lower.split()[0]
        first_name = [
            p for p in self._payees
            if p.display_name.lower().split()[0] == first_token
        ]
        return first_name

    def get_payee_by_id(self, payee_id: str) -> PayeeRecord | None:
        """Return the payee with the given id, or None."""
        for p in self._payees:
            if p.id == payee_id:
                return p
        return None

    # ── TickerDirectory ──────────────────────────────────────────────

    def tickers(self) -> list[TickerRecord]:
        return list(self._tickers)

    def resolve_ticker(self, mention: str) -> str | None:
        mention_lower = mention.strip().lower()
        for t in self._tickers:
            if t.mention == mention_lower:
                return t.symbol
        return None
