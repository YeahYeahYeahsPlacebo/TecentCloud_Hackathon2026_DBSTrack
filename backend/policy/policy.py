"""
The policy engine. Pure deterministic code — there is no LLM anywhere in this
module, and there never should be (CONTRACT.md §7).

Input is a draft plus what the server already knows about the account; output
is one of three decisions:

``allow``
    Execution may proceed.
``hold``
    The draft is legitimate but needs something more from the user — an
    unresolved field, or step-up authentication for a new payee. Maps to
    ``step_up_required`` at `/api/execute` until step-up is defined
    (CONTRACT.md §8 open question 8).
``block``
    The draft violates a rule and no amount of extra authentication makes it
    executable. Maps to ``policy_blocked``.

Rules are evaluated independently and the **strictest** decision wins, so a
draft that trips two rules can never be allowed because one of them was
checked first. ``block`` outranks ``hold``, which outranks ``allow``.

Amounts are decimal strings (CONTRACT.md §2) and are compared with
:class:`~decimal.Decimal`, never as floats.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

ALLOW = "allow"
HOLD = "hold"
BLOCK = "block"

# How strict each decision is. Higher wins when several rules fire.
_SEVERITY = {ALLOW: 0, HOLD: 1, BLOCK: 2}

VELOCITY_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class PolicyDecision:
    """What the engine decided, and why. ``reason`` is null when allowed."""

    decision: str
    reason: Optional[str] = None

    def to_response(self) -> dict:
        return {"decision": self.decision, "reason": self.reason}


@dataclass(frozen=True)
class Limits:
    """The numbers the rules compare against.

    Stored as decimal strings because money is never a float. Defaults are a
    starting point, not a decision: CONTRACT.md §8 open question 3 asks what
    the expiry window and the concrete thresholds should be, and §8 open
    question 8 asks what ``hold`` means operationally.
    """

    per_transaction: str = "5000.00"
    daily_velocity: str = "20000.00"
    # A brand-new payee above this needs step-up even under the per-transaction
    # limit. Small transfers to someone new are ordinary; large ones are how
    # money leaves for good.
    new_payee_step_up_above: str = "1000.00"


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _draft_amount(draft: Mapping[str, Any]) -> tuple[Decimal, str]:
    money = draft.get("amount") or {}
    return _money(money.get("value")), str(money.get("currency", ""))


def _draft_of(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """Accept either a ledger entry (with a ``draft`` key) or a bare draft."""
    inner = entry.get("draft")
    return inner if isinstance(inner, Mapping) else entry


def _payee_ids(posted: Iterable[Mapping[str, Any]]) -> frozenset:
    """Payees this account has already paid. Derived here rather than pushed in
    by the caller: the engine already holds the history, so asking every caller
    to repeat this is how a new payee silently stays 'new' forever."""
    ids = set()
    for entry in posted:
        payee = _draft_of(entry).get("payee")
        if isinstance(payee, Mapping) and payee.get("id"):
            ids.add(payee["id"])
    return frozenset(ids)


def _parse_z(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── The rules ─────────────────────────────────────────────────────────


def rule_unresolved(draft: Mapping[str, Any], ctx: "PolicyInput") -> Optional[PolicyDecision]:
    """A draft with unresolved fields cannot be executed at all.

    CONTRACT.md §4 shows this as ``{"decision": "hold", "reason":
    "unresolved_fields"}`` for `ambiguous_payee.json`. The gateway also
    rejects it independently at check 5 — this is the second gate, and the
    two must not disagree.
    """
    if draft.get("unresolved"):
        return PolicyDecision(HOLD, "unresolved_fields")
    return None


def rule_per_transaction_limit(
    draft: Mapping[str, Any], ctx: "PolicyInput"
) -> Optional[PolicyDecision]:
    amount, currency = _draft_amount(draft)
    limit = _money(ctx.limits.per_transaction)
    if amount > limit:
        return PolicyDecision(
            BLOCK, f"amount {amount} {currency} exceeds the per-transaction limit {limit}"
        )
    return None


def rule_daily_velocity(draft: Mapping[str, Any], ctx: "PolicyInput") -> Optional[PolicyDecision]:
    """Rolling 24h total for this account and currency, including this draft."""
    amount, currency = _draft_amount(draft)
    if not currency:
        return None
    window_start = ctx.at - VELOCITY_WINDOW
    total = amount
    for entry in ctx.posted:
        other = _draft_of(entry)
        other_amount, other_currency = _draft_amount(other)
        if other_currency != currency:
            continue
        if other.get("source_account") != draft.get("source_account"):
            continue
        when = _parse_z(other.get("created_at"))
        if when is None or when < window_start:
            continue
        total += other_amount
    limit = _money(ctx.limits.daily_velocity)
    if total > limit:
        return PolicyDecision(
            BLOCK,
            f"24h total {total} {currency} exceeds the velocity limit {limit}",
        )
    return None


def rule_new_payee_step_up(
    draft: Mapping[str, Any], ctx: "PolicyInput"
) -> Optional[PolicyDecision]:
    """A large transfer to a payee this account has never paid needs step-up."""
    payee = draft.get("payee")
    if not isinstance(payee, Mapping):
        return None  # no payee yet: the unresolved rule already covers it
    payee_id = payee.get("id")
    if payee_id in ctx.known_payees:
        return None
    amount, currency = _draft_amount(draft)
    threshold = _money(ctx.limits.new_payee_step_up_above)
    if amount > threshold:
        return PolicyDecision(
            HOLD,
            f"first transfer to payee {payee_id} of {amount} {currency} requires step-up",
        )
    return None


def rule_sufficient_funds(
    draft: Mapping[str, Any], ctx: "PolicyInput"
) -> Optional[PolicyDecision]:
    """The account must be able to cover the amount.

    Checked here — in policy, before signing — rather than only at transfer
    time, so a user is never asked to sign a draft the bank would refuse.
    Produces ``block``, which the gateway maps to ``policy_blocked``: no new
    reason code, so the contract table stays intact.

    Without a bank injected this rule stays silent. That is deliberate: the
    policy engine is still usable standalone (tests, fixtures) with no mock
    bank wired in.
    """
    if ctx.bank is None:
        return None
    payee = draft.get("payee")
    if not isinstance(payee, Mapping):
        return None  # unresolved payee: rule_unresolved already holds it
    destination = payee.get("id")
    source = draft.get("source_account")
    if not source or not destination:
        return None
    amount, currency = _draft_amount(draft)
    if ctx.bank.can_transfer(
        source=source, destination=destination, amount=amount, currency=currency
    ):
        return None
    try:
        available = ctx.bank.balance(source)
    except Exception:  # noqa: BLE001 - unknown account is still "cannot pay"
        available = None
    if available is None:
        return PolicyDecision(BLOCK, f"source account {source!r} is unknown to the bank")
    return PolicyDecision(
        BLOCK,
        f"insufficient funds: {source} has {available} {currency}, "
        f"cannot send {amount}",
    )


RULES = (
    rule_unresolved,
    rule_per_transaction_limit,
    rule_daily_velocity,
    rule_new_payee_step_up,
    rule_sufficient_funds,
)


# ── The engine ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PolicyInput:
    """Everything a rule is allowed to look at, and nothing else."""

    limits: Limits = Limits()
    posted: tuple[Mapping[str, Any], ...] = ()
    known_payees: frozenset = frozenset()
    now: Optional[datetime] = None
    # Optional mock bank. When present, the balance rule runs; when absent it
    # is silent, so the engine still works with no bank wired in.
    bank: Optional[Any] = None

    @property
    def at(self) -> datetime:
        return self.now or datetime.now(timezone.utc)


class PolicyEngine:
    """Evaluates every rule and returns the strictest decision they produce."""

    def __init__(
        self,
        limits: Limits = Limits(),
        *,
        rules: Iterable[Any] = RULES,
        clock: Optional[Any] = None,
        bank: Optional[Any] = None,
    ) -> None:
        self.limits = limits
        self.rules = tuple(rules)
        self.clock = clock
        self.bank = bank

    def evaluate(
        self,
        draft: Mapping[str, Any],
        digest: Optional[str] = None,
        *,
        posted: Iterable[Mapping[str, Any]] = (),
        known_payees: Iterable[str] = (),
        bank: Optional[Any] = None,
    ) -> PolicyDecision:
        """``digest`` is accepted for symmetry with the gateway's call shape;
        policy never keys off the hash, only off the draft's contents."""
        posted = tuple(posted)
        ctx = PolicyInput(
            limits=self.limits,
            posted=posted,
            known_payees=frozenset(known_payees) | _payee_ids(posted),
            now=self.clock() if self.clock is not None else None,
            bank=bank if bank is not None else self.bank,
        )
        return self.evaluate_with(draft, ctx)

    def evaluate_with(self, draft: Mapping[str, Any], ctx: PolicyInput) -> PolicyDecision:
        winner = PolicyDecision(ALLOW)
        for rule in self.rules:
            outcome = rule(draft, ctx)
            if outcome is None:
                continue
            if _SEVERITY[outcome.decision] > _SEVERITY[winner.decision]:
                winner = outcome
        return winner

    def __call__(
        self, draft: Mapping[str, Any], digest: Optional[str] = None
    ) -> PolicyDecision:
        return self.evaluate(draft, digest)


def evaluate(
    draft: Mapping[str, Any],
    *,
    limits: Limits = Limits(),
    posted: Iterable[Mapping[str, Any]] = (),
    known_payees: Iterable[str] = (),
    now: Optional[datetime] = None,
) -> PolicyDecision:
    """One-shot form, for callers that do not want to hold an engine."""
    posted = tuple(posted)
    return PolicyEngine(limits).evaluate_with(
        draft,
        PolicyInput(
            limits=limits,
            posted=posted,
            known_payees=frozenset(known_payees) | _payee_ids(posted),
            now=now,
        ),
    )
