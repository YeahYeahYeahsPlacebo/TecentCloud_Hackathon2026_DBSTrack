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


def match_payees(
    mention: str | None,
    directory: PayeeDirectory,
) -> list[PayeeRecord]:
    """Return payee records whose display_name matches the mention.

    Matching is case-insensitive.  Two strategies are used:

    1. **Exact match**: the mention equals the full display name
       (case-insensitive).  Returns a single match if found.
    2. **Name-component match**: the mention matches a first name
       or surname component.  For example, ``"John"`` matches any
       payee whose first name (first word of display_name) is
       ``"John"``, and ``"Smith"`` matches any payee whose surname
       (last word) is ``"Smith"``.

    Returns an empty list when ``mention`` is ``None`` or empty, or
    when no payee matches.
    """
    if not mention or not mention.strip():
        return []
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
