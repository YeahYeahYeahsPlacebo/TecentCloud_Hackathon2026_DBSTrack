"""Mock bank: two Johns, balances, and the atomicity of a transfer.

The point of these tests is not that Decimal arithmetic works. It is that a
draft the bank cannot honour is refused **before the user signs it**, and that
a refused transfer leaves both accounts exactly as they were.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.bank import (  # noqa: E402
    SEED_ACCOUNTS,
    Bank,
    CurrencyMismatch,
    InsufficientFunds,
    UnknownAccount,
    seed_bank,
)
from backend.models.draft import TransactionDraft  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "drafts"


def load(name: str) -> TransactionDraft:
    return TransactionDraft.model_validate_json((FIXTURES / name).read_text())


def dump(draft: TransactionDraft) -> dict:
    return draft.model_dump(mode="json", exclude_none=True)


# ── The two Johns ──────────────────────────────────────────────────────


def test_seed_holds_two_different_payees_both_named_john_smith():
    """This is the whole reason "send to John" is ambiguous."""
    johns = [a for a in SEED_ACCOUNTS if a.display_name == "John Smith"]
    assert len(johns) == 2
    assert {a.account_id for a in johns} == {"payee-001", "payee-102"}
    assert {a.masked_account for a in johns} == {"****1234", "****8892"}


def test_seed_source_account_matches_the_fixtures():
    """The draft's source_account and payee.id must name real bank accounts,
    or nothing can ever move."""
    bank = seed_bank()
    for name in ("clean_transfer.json", "ambiguous_payee.json"):
        draft = dump(load(name))
        assert bank.has(draft["source_account"]), f"{name}: unknown source"


def test_every_payee_in_the_fixtures_is_a_real_account():
    bank = seed_bank()
    draft = dump(load("ambiguous_payee.json"))
    for candidate in draft["payee_candidates"]:
        assert bank.has(candidate["id"]), f"{candidate['id']} is not an account"


# ── Balances ───────────────────────────────────────────────────────────


def test_transfer_moves_both_legs():
    bank = seed_bank()
    before_src = bank.balance("acct-001-2345")
    before_dst = bank.balance("payee-001")

    result = bank.transfer(
        source="acct-001-2345", destination="payee-001", amount="50.00", currency="SGD"
    )

    assert bank.balance("acct-001-2345") == before_src - Decimal("50.00")
    assert bank.balance("payee-001") == before_dst + Decimal("50.00")
    assert result.source_balance == before_src - Decimal("50.00")
    assert result.destination_balance == before_dst + Decimal("50.00")


def test_insufficient_funds_moves_nothing():
    """A refused transfer must leave both accounts untouched — not one leg of
    it applied."""
    bank = seed_bank()
    start = Decimal("10000.00")
    assert bank.balance("acct-001-2345") == start

    with pytest.raises(InsufficientFunds):
        bank.transfer(
            source="acct-001-2345",
            destination="payee-001",
            amount="10000.01",
            currency="SGD",
        )

    assert bank.balance("acct-001-2345") == start, "source was debited anyway"
    assert bank.balance("payee-001") == Decimal("0.00"), "destination was credited"


def test_exactly_the_whole_balance_is_allowed():
    """The check is ``balance >= amount``, so spending everything is fine."""
    bank = seed_bank()
    bank.transfer(
        source="acct-001-2345", destination="payee-001", amount="10000.00", currency="SGD"
    )
    assert bank.balance("acct-001-2345") == Decimal("0.00")


def test_unknown_account_is_refused_not_created():
    bank = seed_bank()
    with pytest.raises(UnknownAccount):
        bank.transfer(
            source="acct-001-2345",
            destination="payee-does-not-exist",
            amount="10.00",
            currency="SGD",
        )
    assert not bank.has("payee-does-not-exist")


def test_currency_mismatch_is_refused():
    bank = seed_bank()
    with pytest.raises(CurrencyMismatch):
        bank.transfer(
            source="acct-001-2345",
            destination="payee-001",
            amount="10.00",
            currency="USD",
        )
    assert bank.balance("acct-001-2345") == Decimal("10000.00")


def test_float_amounts_are_rejected_outright():
    """Floats cannot hold money. Refusing them beats silently rounding."""
    bank = seed_bank()
    with pytest.raises(TypeError, match="float"):
        bank.transfer(
            source="acct-001-2345", destination="payee-001", amount=50.0, currency="SGD"
        )


def test_can_transfer_agrees_with_transfer():
    """``can_transfer`` is what policy asks before signing. If it disagreed with
    the real thing, users would sign drafts that then fail."""
    bank = seed_bank()
    cases = [
        ("acct-001-2345", "payee-001", "50.00", True),
        ("acct-001-2345", "payee-001", "10000.00", True),
        ("acct-001-2345", "payee-001", "10000.01", False),
        ("acct-001-2345", "payee-nope", "50.00", False),
    ]
    for source, destination, amount, expected in cases:
        assert (
            bank.can_transfer(source=source, destination=destination, amount=amount)
            is expected
        ), f"{amount} -> {destination}"


def test_a_bank_can_be_built_from_scratch():
    """Not everyone wants the seed data."""
    bank = Bank()
    assert len(bank.accounts()) == len(SEED_ACCOUNTS)
    empty = Bank([])
    assert empty.accounts() == ()


def test_decrementing_repeatedly_stays_exact():
    """The reason for Decimal. With floats this drifts; here it must not."""
    bank = seed_bank()
    for _ in range(100):
        bank.transfer(
            source="acct-001-2345", destination="payee-001", amount="0.10", currency="SGD"
        )
    assert bank.balance("acct-001-2345") == Decimal("9990.00")
    assert bank.balance("payee-001") == Decimal("10.00")
