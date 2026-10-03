"""
Tests for the transaction draft contract.

Every fixture is validated against BOTH the JSON Schema and the Pydantic model.
Negative tests confirm that invalid drafts are rejected.
"""

import json
import sys
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

# Ensure the project root is on the path so `backend` is importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.models.draft import TransactionDraft  # noqa: E402

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
    """The ambiguous fixture has payee null, 'payee' in unresolved, and exactly two candidates."""
    data = load_fixture("ambiguous_payee.json")
    assert data["payee"] is None
    assert "payee" in data["unresolved"]
    assert len(data["payee_candidates"]) == 2
    # Every candidate must have non-null required fields
    for cand in data["payee_candidates"]:
        assert cand["id"] is not None
        assert cand["display_name"] is not None
        assert cand["masked_account"] is not None


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


def test_transfer_with_null_payee_and_empty_unresolved_rejected_by_json_schema():
    """A transfer with payee null and empty unresolved must be rejected by the JSON Schema."""
    data = load_fixture("clean_transfer.json")
    data["payee"] = None
    data["unresolved"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=data, schema=SCHEMA)


def test_transfer_with_null_payee_and_empty_unresolved_rejected_by_pydantic():
    """A transfer with payee null and empty unresolved must be rejected by the Pydantic model."""
    data = load_fixture("clean_transfer.json")
    data["payee"] = None
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
