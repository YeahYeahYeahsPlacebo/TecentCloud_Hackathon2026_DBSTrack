"""
Tests for the transaction draft contract.

Every fixture is validated against BOTH the JSON Schema and the Pydantic model.
Negative tests confirm that invalid drafts are rejected.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

# Ensure the project root is on the path so `backend` is importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.models.draft import (  # noqa: E402
    MONEY_PATTERN,
    UNRESOLVED_FIELDS,
    UTC_TIMESTAMP_PATTERN,
    TransactionDraft,
)

# ── Paths ──────────────────────────────────────────────────────────────

SCHEMA_PATH = ROOT / "contract" / "draft.schema.json"
FIXTURES_DIR = ROOT / "fixtures" / "drafts"

VALID_FIXTURES = [
    "clean_transfer.json",
    "ambiguous_payee.json",
    "equity_purchase.json",
    "multi_step.json",
    "over_limit.json",
]


# ── Helpers ────────────────────────────────────────────────────────────


def load_schema() -> dict:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


SCHEMA = load_schema()


# ── Positive tests: every fixture validates against both ──────────────


@pytest.mark.parametrize("fixture_name", VALID_FIXTURES)
def test_fixture_validates_against_json_schema(fixture_name: str):
    """Every fixture must be valid under the JSON Schema."""
    data = load_fixture(fixture_name)
    jsonschema.validate(instance=data, schema=SCHEMA)


@pytest.mark.parametrize("fixture_name", VALID_FIXTURES)
def test_fixture_validates_against_pydantic(fixture_name: str):
    """Every fixture must be accepted by the Pydantic model."""
    data = load_fixture(fixture_name)
    model = TransactionDraft(**data)
    assert model is not None


# ── Negative tests ─────────────────────────────────────────────────────


def test_float_amount_rejected_by_pydantic():
    """A float amount value must be rejected - money is never a float."""
    data = load_fixture("clean_transfer.json")
    data["amount"]["value"] = 50.0  # type: ignore  - float, not string
    with pytest.raises(ValidationError):
        TransactionDraft(**data)


def test_missing_required_field_rejected_by_json_schema():
    """A draft missing `source_account` must be rejected by the JSON Schema."""
    data = load_fixture("clean_transfer.json")
    del data["source_account"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=data, schema=SCHEMA)


def test_missing_required_field_rejected_by_pydantic():
    """A draft missing `source_account` must be rejected by the Pydantic model."""
    data = load_fixture("clean_transfer.json")
    del data["source_account"]
    with pytest.raises(ValidationError):
        TransactionDraft(**data)


def test_unknown_intent_type_rejected_by_json_schema():
    """An unknown intent_type must be rejected by the JSON Schema."""
    data = load_fixture("clean_transfer.json")
    data["intent_type"] = "loan_application"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=data, schema=SCHEMA)


def test_unknown_intent_type_rejected_by_pydantic():
    """An unknown intent_type must be rejected by the Pydantic model."""
    data = load_fixture("clean_transfer.json")
    data["intent_type"] = "loan_application"
    with pytest.raises(ValidationError):
        TransactionDraft(**data)


# ── Constraint-specific tests ─────────────────────────────────────────


def test_ambiguous_payee_fixture():
    """The ambiguous fixture parses with no payee, 'payee' unresolved, and two candidates."""
    model = TransactionDraft(**load_fixture("ambiguous_payee.json"))
    assert model.payee is None
    assert "payee" in model.unresolved
    assert [c.id for c in model.payee_candidates] == ["payee-101", "payee-102"]


def test_null_payee_id_rejected_by_json_schema():
    """A payee object with a null id must be rejected by the JSON Schema."""
    data = load_fixture("clean_transfer.json")
    data["payee"]["id"] = None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=data, schema=SCHEMA)


def test_null_payee_id_rejected_by_pydantic():
    """A payee object with a null id must be rejected by the Pydantic model."""
    data = load_fixture("clean_transfer.json")
    data["payee"]["id"] = None
    with pytest.raises(ValidationError):
        TransactionDraft(**data)


def test_transfer_without_payee_and_empty_unresolved_rejected_by_json_schema():
    """A transfer with no payee and empty unresolved must be rejected by the JSON Schema."""
    data = load_fixture("clean_transfer.json")
    del data["payee"]
    data["unresolved"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=data, schema=SCHEMA)


def test_transfer_without_payee_and_empty_unresolved_rejected_by_pydantic():
    """A transfer with no payee and empty unresolved must be rejected by the Pydantic model."""
    data = load_fixture("clean_transfer.json")
    del data["payee"]
    data["unresolved"] = []
    with pytest.raises(ValidationError):
        TransactionDraft(**data)


def test_over_limit_is_still_a_valid_draft():
    """The over-limit fixture is structurally valid; the limit is a policy concern, not a schema concern."""
    data = load_fixture("over_limit.json")
    jsonschema.validate(instance=data, schema=SCHEMA)
    model = TransactionDraft(**data)
    assert model is not None


def test_ambiguous_fixture_has_unresolved_fields():
    """Fixture sanity check only. Enforcement of rule 10 (unresolved drafts
    can never be signed or executed) belongs to the gateway tests."""
    data = load_fixture("ambiguous_payee.json")
    assert len(data["unresolved"]) > 0


# ── Schema and Pydantic must agree ────────────────────────────────────


def schema_accepts(data: dict) -> bool:
    return jsonschema.Draft202012Validator(SCHEMA).is_valid(data)


def pydantic_accepts(data: dict) -> bool:
    try:
        TransactionDraft(**data)
        return True
    except ValidationError:
        return False


def assert_both_reject(data: dict) -> None:
    assert not schema_accepts(data), "JSON Schema accepted it"
    assert not pydantic_accepts(data), "Pydantic accepted it"


def assert_both_accept(data: dict) -> None:
    assert schema_accepts(data), "JSON Schema rejected it"
    assert pydantic_accepts(data), "Pydantic rejected it"


def test_schema_and_model_share_patterns_and_enums():
    """The constants duplicated between draft.py and the schema must not drift."""
    assert SCHEMA["$defs"]["money"]["pattern"] == MONEY_PATTERN
    assert SCHEMA["$defs"]["utc_timestamp"]["pattern"] == UTC_TIMESTAMP_PATTERN
    assert tuple(SCHEMA["properties"]["unresolved"]["items"]["enum"]) == UNRESOLVED_FIELDS


def test_explicit_null_rejected_by_both():
    """Optional fields are omitted, never null (the hashed dump has no nulls)."""
    data = load_fixture("ambiguous_payee.json")
    data["payee"] = None
    assert_both_reject(data)


# Item 4: `unresolved` is required, with no default.


def test_missing_unresolved_rejected_by_both():
    data = load_fixture("clean_transfer.json")
    del data["unresolved"]
    assert_both_reject(data)


# Item 5: money is an ASCII decimal string.


@pytest.mark.parametrize(
    "value",
    [
        "٥٠.٠٠",  # Arabic-Indic digits "٥٠.٠٠"
        "５０.００",  # fullwidth digits "５０.００"
        "50.00\n",
        "0050.00",
        "-50.00",
        "1e5",
        ".50",
        "50.",
        "50",
        "50.000",
        " 50.00",
        50,
        True,
    ],
)
def test_bad_money_value_rejected_by_both(value):
    data = load_fixture("clean_transfer.json")
    data["amount"]["value"] = value
    assert_both_reject(data)


@pytest.mark.parametrize("value", ["0.00", "50.00", "999999.00"])
def test_good_money_value_accepted_by_both(value):
    data = load_fixture("clean_transfer.json")
    data["amount"]["value"] = value
    assert_both_accept(data)
    assert TransactionDraft(**data).amount.value == value  # round-trips exactly, no Decimal


@pytest.mark.parametrize("field", ["quantity", "notional_amount"])
@pytest.mark.parametrize("value", ["٥٠.٠٠", "10.00\n", "abc", "1e3"])
def test_bad_equity_decimal_rejected_by_both(field, value):
    data = load_fixture("equity_purchase.json")
    data[field] = value
    assert_both_reject(data)


def test_currency_with_trailing_newline_rejected_by_both():
    data = load_fixture("clean_transfer.json")
    data["amount"]["currency"] = "SGD\n"
    assert_both_reject(data)


# Item 6: timestamps are UTC with a Z suffix, and expires_at > created_at.


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-03T10:00:00",  # naive
        "2026-10-03T10:00:00+00:00",
        "2026-10-03T18:00:00+08:00",
        "2026-10-03T10:00:00.123Z",
        "2026-10-03",
        "2026-10-03 10:00:00Z",
        "2026-10-03T10:00:00Z\n",
        1759485600,
    ],
)
def test_non_z_timestamp_rejected_by_both(value):
    data = load_fixture("clean_transfer.json")
    data["created_at"] = value
    assert_both_reject(data)


@pytest.mark.parametrize("expires_at", ["2026-10-03T10:00:00Z", "2026-10-03T09:59:59Z"])
def test_expires_at_not_after_created_at_rejected_by_pydantic(expires_at):
    """JSON Schema cannot compare two fields; this rule is Pydantic-only (documented in the schema)."""
    data = load_fixture("clean_transfer.json")
    data["expires_at"] = expires_at
    assert not pydantic_accepts(data)


def test_naive_datetime_object_rejected_by_pydantic():
    data = load_fixture("clean_transfer.json")
    data["created_at"] = datetime(2026, 10, 3, 10, 0)
    assert not pydantic_accepts(data)


# Item 7: payee / unresolved consistency.


def test_payee_with_payee_unresolved_rejected_by_both():
    data = load_fixture("ambiguous_payee.json")
    data["payee"] = load_fixture("clean_transfer.json")["payee"]
    assert_both_reject(data)


def test_candidates_with_resolved_payee_rejected_by_both():
    data = load_fixture("clean_transfer.json")
    data["payee_candidates"] = load_fixture("ambiguous_payee.json")["payee_candidates"]
    assert_both_reject(data)


def test_empty_candidates_with_resolved_payee_accepted_by_both():
    data = load_fixture("clean_transfer.json")
    data["payee_candidates"] = []
    assert_both_accept(data)


@pytest.mark.parametrize("count", [0, 1])
def test_payee_unresolved_needs_at_least_two_candidates(count):
    data = load_fixture("ambiguous_payee.json")
    data["payee_candidates"] = data["payee_candidates"][:count]
    assert_both_reject(data)


def test_payee_unresolved_without_candidates_key_rejected_by_both():
    data = load_fixture("ambiguous_payee.json")
    del data["payee_candidates"]
    assert_both_reject(data)


@pytest.mark.parametrize(
    "fixture_name,unresolved",
    [
        # clean_transfer has a payee, so only the enum / uniqueness rule can reject these.
        ("clean_transfer.json", ["bogus_field"]),
        ("clean_transfer.json", [""]),
        ("clean_transfer.json", ["Payee"]),
        ("clean_transfer.json", ["amount", "amount"]),
        ("ambiguous_payee.json", ["payee", "payee"]),
    ],
)
def test_bad_unresolved_items_rejected_by_both(fixture_name, unresolved):
    data = load_fixture(fixture_name)
    data["unresolved"] = unresolved
    assert_both_reject(data)


def test_known_unresolved_item_accepted_by_both():
    """Control for the test above: a real field name passes on the same fixture."""
    data = load_fixture("clean_transfer.json")
    data["unresolved"] = ["amount"]
    assert_both_accept(data)
