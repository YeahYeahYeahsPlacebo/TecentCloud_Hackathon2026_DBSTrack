"""Mock banking services: accounts, balances, transfers.

CONTRACT.md: everything about accounts, balances and transfers is mocked —
the AI never touches the ledger and nothing here talks to a real bank.

Folder ownership note: ``backend/bank`` is **not** assigned to anyone in
README's Folder Ownership table. Member 3 created it because Phase 1 lists
"Mock bank (two Johns)" under Member 3's column. If the team wants it owned
elsewhere, say so and it moves — nothing outside ``backend/gateway`` imports
it yet.
"""

from backend.bank.bank import (  # noqa: F401
    SEED_ACCOUNTS,
    Account,
    Bank,
    BankError,
    CurrencyMismatch,
    InsufficientFunds,
    Transfer,
    UnknownAccount,
    seed_bank,
)

__all__ = [
    "SEED_ACCOUNTS",
    "Account",
    "Bank",
    "BankError",
    "CurrencyMismatch",
    "InsufficientFunds",
    "Transfer",
    "UnknownAccount",
    "seed_bank",
]
