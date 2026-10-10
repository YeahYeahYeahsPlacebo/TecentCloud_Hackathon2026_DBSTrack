"""
The policy engine is pure deterministic code — no LLM anywhere
(CONTRACT.md §7). These tests pin each rule and the tie-break between them.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.models.draft import TransactionDraft  # noqa: E402
from backend.policy import (  # noqa: E402
    ALLOW,
    BLOCK,
    HOLD,
    Limits,
    PolicyEngine,
    evaluate,
)

FIXTURES = ROOT / "fixtures" / "drafts"


def load(name: str) -> dict:
    return TransactionDraft.model_validate_json(
        (FIXTURES / name).read_text(encoding="utf-8")
    ).model_dump(mode="json", exclude_none=True)


def with_amount(draft: dict, value: str) -> dict:
    out = dict(draft)
    out["amount"] = {"value": value, "currency": draft["amount"]["currency"]}
    return out


def now_z(offset_hours: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ── Each rule on its own ──────────────────────────────────────────────


def test_clean_draft_is_allowed():
    outcome = evaluate(load("clean_transfer.json"))
    assert outcome.decision == ALLOW and outcome.reason is None


def test_unresolved_fields_are_held_not_blocked():
    """CONTRACT.md §4 shows ambiguous_payee.json as hold / unresolved_fields."""
    outcome = evaluate(load("ambiguous_payee.json"))
    assert outcome.decision == HOLD
    assert outcome.reason == "unresolved_fields"


def test_over_the_per_transaction_limit_is_blocked():
    engine = PolicyEngine()
    assert engine.evaluate(with_amount(load("clean_transfer.json"), "5000.01")).decision == BLOCK


def test_exactly_at_the_limit_is_allowed():
    """The limit is inclusive: 'exceeds' means strictly greater.

    known_payees is passed so only the limit rule is in play.
    """
    at_limit = with_amount(load("clean_transfer.json"), "5000.00")
    assert evaluate(at_limit, known_payees={"payee-001"}).decision == ALLOW


def test_limits_are_configurable():
    strict = PolicyEngine(Limits(per_transaction="10.00"))
    assert strict.evaluate(load("clean_transfer.json")).decision == BLOCK


def test_velocity_sums_previous_transfers_in_the_window():
    clean = load("clean_transfer.json")
    posted = [
        {"draft": {**with_amount(clean, "3000.00"), "created_at": now_z(-1)}},
        {"draft": {**with_amount(clean, "2500.00"), "created_at": now_z(-2)}},
    ]
    # 50 + 3000 + 2500 = 5550, under the 20000 default — allowed.
    assert evaluate(clean, posted=posted).decision == ALLOW
    tight = PolicyEngine(Limits(daily_velocity="5000.00"))
    assert tight.evaluate(clean, posted=posted).decision == BLOCK


def test_velocity_ignores_other_currencies_and_other_accounts():
    clean = load("clean_transfer.json")
    other_currency = {"draft": {**with_amount(clean, "9000.00"), "amount": {"value": "9000.00", "currency": "USD"}}}
    other_account = {"draft": {**with_amount(clean, "9000.00"), "source_account": "acct-other"}}
    engine = PolicyEngine(Limits(daily_velocity="5000.00"))
    assert engine.evaluate(clean, posted=[other_currency]).decision == ALLOW
    assert engine.evaluate(clean, posted=[other_account]).decision == ALLOW


def test_velocity_ignores_transfers_outside_the_24h_window():
    clean = load("clean_transfer.json")
    old = {"draft": {**with_amount(clean, "9000.00"), "created_at": now_z(-48)}}
    assert PolicyEngine(Limits(daily_velocity="5000.00")).evaluate(clean, posted=[old]).decision == ALLOW


def test_new_payee_above_the_threshold_needs_step_up():
    clean = load("clean_transfer.json")
    big = with_amount(clean, "1500.00")
    assert evaluate(big, posted=(), known_payees=set()).decision == HOLD
    assert evaluate(big, posted=(), known_payees={"payee-001"}).decision == ALLOW


def test_small_transfer_to_a_new_payee_is_ordinary():
    clean = load("clean_transfer.json")
    assert evaluate(with_amount(clean, "80.00"), known_payees=set()).decision == ALLOW


def test_a_payee_paid_before_counts_as_known():
    clean = load("clean_transfer.json")
    posted = [{"draft": clean}]
    assert evaluate(with_amount(clean, "1500.00"), posted=posted).decision == ALLOW


# ── The tie-break ─────────────────────────────────────────────────────


def test_the_strictest_decision_wins_regardless_of_rule_order():
    """Both a block rule and a hold rule fire; block must win."""
    draft = with_amount(load("ambiguous_payee.json"), "9000.00")
    outcome = evaluate(draft)
    assert outcome.decision == BLOCK, outcome
    assert "per-transaction" in outcome.reason


def test_severity_is_block_over_hold_over_allow():
    engine = PolicyEngine()
    allow_then_hold = [load("clean_transfer.json"), load("ambiguous_payee.json")]
    assert [engine.evaluate(d).decision for d in allow_then_hold] == [ALLOW, HOLD]


# ── Determinism ───────────────────────────────────────────────────────


def test_the_same_input_always_gives_the_same_decision():
    clean = load("clean_transfer.json")
    posted = [{"draft": with_amount(clean, "100.00")}]
    first = evaluate(clean, posted=posted)
    for _ in range(5):
        assert evaluate(clean, posted=posted) == first


def test_decision_serialises_to_the_contract_shape():
    assert evaluate(load("clean_transfer.json")).to_response() == {
        "decision": "allow",
        "reason": None,
    }
    held = evaluate(load("ambiguous_payee.json")).to_response()
    assert held == {"decision": "hold", "reason": "unresolved_fields"}
