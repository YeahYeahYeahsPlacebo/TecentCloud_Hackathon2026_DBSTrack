"""Step-up: the one policy rule that produces ``hold``.

CONTRACT.md §8 open question 8 asks what ``hold`` means operationally. The
gateway answers it in ``backend/gateway/execute.py`` ``_check_policy_allowed``:
``block`` is terminal, and anything else needs step-up, which does not exist
yet, so it fails closed as ``step_up_required``.

These tests exist because the rule behind it had **none**. Every other rule is
exercised somewhere; ``rule_new_payee_step_up`` never was, and the reason is
quiet: the fixtures are 50 SGD and the default threshold is 1000 SGD, so no
fixture-driven path can ever reach it. A rule that only fires above a threshold
nobody crosses is a rule nobody has checked. The two end-to-end tests below
lower the threshold to 10 SGD so a real 50 SGD draft lands inside it.

If step-up is ever implemented, ``step_up_required`` stops being a refusal and
these tests must be rewritten — that is the point of having them.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.gateway.api import GatewayAPI  # noqa: E402
from backend.gateway.webauthn import public_key_to_pem  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402
from backend.parser.interface import ParseResult  # noqa: E402
from backend.policy import Limits, PolicyEngine  # noqa: E402
from gateway_fakes import new_key, passing_validator, signed_full_run  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "drafts"

# The fixture is 50.00 SGD. 10.00 puts it above the threshold, 1000.00 (the
# default) puts it below — that difference is the whole reason this file exists.
STEP_UP_ABOVE = Limits(new_payee_step_up_above="10.00")


def load(name: str) -> TransactionDraft:
    return TransactionDraft.model_validate_json((FIXTURES / name).read_text())


def draft_dict(name: str = "clean_transfer.json") -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeParser:
    def __init__(self, *items) -> None:
        self.items = list(items)

    def __call__(self, transcript: str) -> ParseResult:
        return ParseResult(items=self.items, intents_detected=len(self.items))


def make_api(limits: Limits | None = None, known_payees=()) -> GatewayAPI:
    clean = load("clean_transfer.json")
    return GatewayAPI(
        parser=FakeParser(clean),
        clock=lambda: clean.created_at + timedelta(minutes=1),
        limits=limits,
        known_payees=known_payees,
        validator=passing_validator(),
    )


# ── The rule itself ────────────────────────────────────────────────────


def test_a_first_transfer_above_the_threshold_holds():
    engine = PolicyEngine(limits=STEP_UP_ABOVE)
    outcome = engine.evaluate(draft_dict(), "0" * 64)

    assert outcome.decision == "hold"
    assert "requires step-up" in outcome.reason


def test_the_same_transfer_to_a_declared_known_payee_is_allowed():
    engine = PolicyEngine(limits=STEP_UP_ABOVE)
    outcome = engine.evaluate(draft_dict(), "0" * 64, known_payees={"payee-001"})

    assert outcome.decision == "allow"


def test_a_payee_learned_from_posted_history_counts_as_known():
    """Paying someone once makes them known — no second step-up per transfer."""
    engine = PolicyEngine(limits=STEP_UP_ABOVE)
    outcome = engine.evaluate(
        draft_dict(), "0" * 64, posted=[{"payee": {"id": "payee-001"}}]
    )

    assert outcome.decision == "allow"


def test_below_the_threshold_a_new_payee_needs_nothing():
    """Small transfers to someone new are ordinary — this is the point of the
    threshold, not an accident of it."""
    engine = PolicyEngine(limits=Limits(new_payee_step_up_above="10000.00"))
    outcome = engine.evaluate(draft_dict(), "0" * 64)

    assert outcome.decision == "allow"


def test_under_default_limits_the_rule_is_never_reached():
    """Documents why this rule went untested: 50 SGD against a 1000 SGD
    default. Kept explicit so a future threshold change is a visible event."""
    outcome = PolicyEngine().evaluate(draft_dict(), "0" * 64)

    assert outcome.decision == "allow"
    assert Limits().new_payee_step_up_above == "1000.00"


def test_a_draft_with_no_payee_stays_silent_here():
    """Unresolved payees are ``rule_unresolved``'s job. Two rules holding for
    the same reason would make the reported reason ambiguous."""
    draft = draft_dict()
    draft["payee"] = None
    engine = PolicyEngine(limits=STEP_UP_ABOVE)

    assert engine.evaluate(draft, "0" * 64).decision == "allow"


# ── Through the real gateway ───────────────────────────────────────────


def test_step_12_turns_hold_into_step_up_required_with_nothing_posted():
    api = make_api(limits=STEP_UP_ABOVE)
    result = signed_full_run(api, new_key())

    assert result["result"] == "rejected", result
    assert result["reason_code"] == "step_up_required"
    assert api.ledger.entries == []


def test_the_same_draft_to_a_known_payee_executes_instead():
    api = make_api(limits=STEP_UP_ABOVE, known_payees={"payee-001"})
    result = signed_full_run(api, new_key())

    assert result["result"] == "executed", result
    assert len(api.ledger.entries) == 1


def test_after_one_execution_the_payee_is_known_and_the_next_transfer_is_clean():
    """The ledger is what makes a payee known, not a pre-declared list — so
    the second transfer to the same person must not ask for step-up again."""
    api = make_api(limits=STEP_UP_ABOVE, known_payees={"payee-001"})
    assert signed_full_run(api, new_key())["result"] == "executed"

    api2 = make_api(limits=STEP_UP_ABOVE)
    api2.ledger.entries.extend(api.ledger.entries)
    assert signed_full_run(api2, new_key())["result"] == "executed"


def test_a_hold_still_gets_a_challenge_unlike_a_block():
    """``block`` refuses at the challenge (backend/gateway/credential.py), so
    there is nothing to sign. ``hold`` lets the user sign and refuses at step
    12 instead. Conflating the two would make step-up unreachable."""
    api = make_api(limits=STEP_UP_ABOVE)
    key = new_key()

    draft_id = api.message({"transcript": "Send fifty dollars to John Smith."})[
        "items"
    ][0]["draft"]["id"]
    credential_id = api.register(
        {"user_id": "user-001", "public_key": public_key_to_pem(key.public_key())}
    )["credential_id"]
    challenge = api.challenge({"draft_id": draft_id, "credential_id": credential_id})

    assert "challenge" in challenge, challenge
