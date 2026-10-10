"""
Mock bank: accounts, balances, transfers.

README: *"All banking services — accounts, balances, transfers — are mocked."*
This module is that mock. It is the only place in the backend that holds money
as mutable state; ``backend/ledger`` is the record of what already moved, and
this is the thing that moves it.

Two Johns, deliberately
-----------------------
The seed data contains **two different payees both named "John Smith"**
(``payee-001`` / ``****1234`` and ``payee-102`` / ``****8892``), which is why
"send to John" is ambiguous and why ``ambiguous_payee.json`` exists. The
ambiguity is in the *data*, not in the code — nothing here special-cases it.

Money
-----
Every amount is :class:`~decimal.Decimal`. Floats never touch a balance:
``0.1 + 0.2 != 0.3`` is not an acceptable property for something that holds
money, and the canonical hash already refuses to serialise floats.

Atomicity
---------
``transfer`` checks the balance and moves both legs before returning. If the
source cannot cover the amount, **nothing** changes and ``InsufficientFunds``
is raised — there is no state in which one account was debited and the other
was not.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional, Tuple

__all__ = [
    "Account",
    "Bank",
    "Transfer",
    "BankError",
    "UnknownAccount",
    "CurrencyMismatch",
    "InsufficientFunds",
    "SEED_ACCOUNTS",
    "seed_bank",
]


class BankError(Exception):
    """Base class for every rejection the mock bank can produce."""


class UnknownAccount(BankError):
    """A draft named an account the bank does not hold."""


class CurrencyMismatch(BankError):
    """The draft's currency is not the account's currency."""


class InsufficientFunds(BankError):
    """The source account cannot cover the amount. Nothing was moved."""


@dataclass(frozen=True)
class Account:
    """One account. ``account_id`` is what a draft's ``payee.id`` refers to."""

    account_id: str
    display_name: str
    masked_account: str
    balance: Decimal
    currency: str = "SGD"


@dataclass(frozen=True)
class Transfer:
    """The result of a successful transfer: both legs, both balances after."""

    source_account: str
    destination_account: str
    amount: Decimal
    currency: str
    source_balance: Decimal
    destination_balance: Decimal


def _money(value: "str | Decimal | int") -> Decimal:
    """Coerce to Decimal. Rejects floats outright rather than silently
    inheriting their error."""
    if isinstance(value, float):
        raise TypeError(
            "amounts must be str or Decimal, not float: "
            "floats cannot represent money exactly"
        )
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"not a valid monetary amount: {value!r}") from exc


# Seed accounts, matching fixtures/drafts and backend/parser/stub.py.
# payee-001 and payee-102 are BOTH "John Smith" at different accounts — that
# is the whole reason "send to John" is ambiguous.
SEED_ACCOUNTS: Tuple[Account, ...] = (
    Account("acct-001-2345", "My Account", "****2345", Decimal("10000.00"), "SGD"),
    Account("payee-001", "John Smith", "****1234", Decimal("0.00"), "SGD"),
    Account("payee-101", "John Doe", "****4521", Decimal("0.00"), "SGD"),
    Account("payee-102", "John Smith", "****8892", Decimal("0.00"), "SGD"),
)


def seed_bank() -> "Bank":
    """A bank holding the seed accounts. This is what the demo runs on."""
    return Bank(SEED_ACCOUNTS)


class Bank:
    """Accounts and transfers. In-memory; a restart resets balances.

    Persistence is deliberately absent: this is a mock, and a demo that starts
    from known balances is more useful than one that starts from last night's.
    """

    def __init__(self, accounts: "Mapping[str, Account] | Tuple[Account, ...] | None" = None) -> None:
        if accounts is None:
            accounts = SEED_ACCOUNTS
        if isinstance(accounts, (list, tuple)):
            self._accounts: dict[str, Account] = {a.account_id: a for a in accounts}
        else:
            self._accounts = dict(accounts)

    # ── Read side ────────────────────────────────────────────────────

    def get(self, account_id: str) -> Optional[Account]:
        return self._accounts.get(account_id)

    def has(self, account_id: str) -> bool:
        return account_id in self._accounts

    def balance(self, account_id: str) -> Decimal:
        account = self._accounts.get(account_id)
        if account is None:
            raise UnknownAccount(f"no such account: {account_id!r}")
        return account.balance

    def accounts(self) -> Tuple[Account, ...]:
        """Snapshot of every account. Read-only by convention."""
        return tuple(self._accounts.values())

    # ── Write side ───────────────────────────────────────────────────

    def can_transfer(
        self,
        *,
        source: str,
        destination: str,
        amount: "str | Decimal",
        currency: str = "SGD",
    ) -> bool:
        """True if ``transfer`` would succeed. Used by policy so an
        unaffordable draft is blocked *before* the user signs it."""
        try:
            value = _money(amount)
        except (TypeError, ValueError):
            return False

        src = self._accounts.get(source)
        dst = self._accounts.get(destination)
        if src is None or dst is None:
            return False
        if src.currency != currency or dst.currency != currency:
            return False
        # A transfer to yourself neither needs nor consumes funds, but a
        # zero amount is still refused by policy — this is only "can the
        # money move".
        return src.balance >= value

    def transfer(
        self,
        *,
        source: str,
        destination: str,
        amount: "str | Decimal",
        currency: str = "SGD",
    ) -> Transfer:
        """Move money. Raises :class:`BankError` and changes nothing if it
        cannot."""
        value = _money(amount)

        src = self._accounts.get(source)
        if src is None:
            raise UnknownAccount(f"no such source account: {source!r}")
        dst = self._accounts.get(destination)
        if dst is None:
            raise UnknownAccount(f"no such destination account: {destination!r}")

        if src.currency != currency:
            raise CurrencyMismatch(
                f"source {source!r} is {src.currency}, draft is {currency}"
            )
        if dst.currency != currency:
            raise CurrencyMismatch(
                f"destination {destination!r} is {dst.currency}, draft is {currency}"
            )

        if src.balance < value:
            raise InsufficientFunds(
                f"{source!r} has {src.balance} {currency}, cannot send {value}"
            )

        # Both legs, or neither. There is no interleaving here: the dict is
        # replaced only after both new balances are computed.
        new_src = replace(src, balance=src.balance - value)
        new_dst = replace(dst, balance=dst.balance + value)
        self._accounts[source] = new_src
        self._accounts[destination] = new_dst

        return Transfer(
            source_account=source,
            destination_account=destination,
            amount=value,
            currency=currency,
            source_balance=new_src.balance,
            destination_balance=new_dst.balance,
        )
