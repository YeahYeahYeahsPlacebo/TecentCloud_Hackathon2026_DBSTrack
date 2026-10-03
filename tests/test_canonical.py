"""
Tests for the canonical draft hashing implementation.

Loads every vector from contract/test_vectors.json and checks that the
Python implementation (backend/canonical.py) produces the expected hash
or raises as specified.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.canonical import canonical_bytes, draft_hash  # noqa: E402

VECTORS_PATH = ROOT / "contract" / "test_vectors.json"


def load_vectors() -> list[dict]:
    with open(VECTORS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["vectors"]


VECTORS = load_vectors()

# Vectors that should succeed (not raise)
OK_VECTORS = [(i, v) for i, v in enumerate(VECTORS) if not v.get("should_raise", False)]
# Vectors that should raise
RAISE_VECTORS = [(i, v) for i, v in enumerate(VECTORS) if v.get("should_raise", False)]


# ── Positive tests: hash matches expected ─────────────────────────────


@pytest.mark.parametrize("index,vector", OK_VECTORS)
def test_hash_matches_expected(index: int, vector: dict):
    """Python draft_hash must produce the expected hex hash for each OK vector."""
    actual = draft_hash(vector["draft"])
    expected = vector["expected_hash"]
    assert actual == expected, (
        f"Vector {index} ({vector['description']}): "
        f"expected {expected}, got {actual}"
    )


# ── Negative tests: float must raise ───────────────────────────────────


@pytest.mark.parametrize("index,vector", RAISE_VECTORS)
def test_float_raises(index: int, vector: dict):
    """Vectors marked should_raise must raise an error in Python."""
    with pytest.raises((ValueError, TypeError)):
        draft_hash(vector["draft"])


# ── Key-order independence ─────────────────────────────────────────────


def test_key_order_does_not_affect_hash():
    """Two vectors with the same data but different key order must hash the same."""
    # Vectors 0 (normal), 1 (scrambled), 2 (another order) all have the same data
    h0 = draft_hash(VECTORS[0]["draft"])
    h1 = draft_hash(VECTORS[1]["draft"])
    h2 = draft_hash(VECTORS[2]["draft"])
    assert h0 == h1 == h2


# ── Excluded fields do not affect hash ─────────────────────────────────


def test_excluded_fields_do_not_affect_hash():
    """A draft with signature and confidence must hash the same as without them."""
    # Vector 0 (without signature) and vector 5 (with signature) have same data
    h_without = draft_hash(VECTORS[0]["draft"])
    h_with = draft_hash(VECTORS[5]["draft"])
    assert h_without == h_with


# ── One-character change produces a different hash ─────────────────────


def test_one_char_change_produces_different_hash():
    """Changing one character must produce a different hash."""
    h_original = draft_hash(VECTORS[0]["draft"])
    h_changed = draft_hash(VECTORS[7]["draft"])
    assert h_original != h_changed


# ── Array order matters ────────────────────────────────────────────────


def test_array_order_matters():
    """Same array elements in different order must produce different hashes."""
    h1 = draft_hash(VECTORS[9]["draft"])   # ["amount", "payee"]
    h2 = draft_hash(VECTORS[10]["draft"])  # ["payee", "amount"]
    assert h1 != h2


# ── Non-BMP characters in keys: code-point sort, not UTF-16 ────────────


def test_non_bmp_key_sorting():
    """
    Vector 4 has an emoji (U+1F600) as the first character of a key.
    In UTF-16 code-unit sort, the surrogate pair would sort differently.
    This test confirms the hash is computed with code-point sorting.
    We can't test the wrong sort directly, but we verify the hash matches
    the expected value (which was computed with code-point sort in Python).
    """
    h = draft_hash(VECTORS[4]["draft"])
    assert h == VECTORS[4]["expected_hash"]


# ── Canonical bytes are deterministic ──────────────────────────────────


def test_canonical_bytes_deterministic():
    """Calling canonical_bytes twice on the same input must produce identical bytes."""
    draft = VECTORS[0]["draft"]
    b1 = canonical_bytes(draft)
    b2 = canonical_bytes(draft)
    assert b1 == b2


# ── No whitespace in canonical bytes ───────────────────────────────────


def test_no_whitespace_between_tokens():
    """The canonical JSON string must contain no whitespace between tokens.

    Spaces inside string values (e.g. in the transcript) are fine; we only
    check for whitespace that would appear as token separators.
    """
    draft = VECTORS[0]["draft"]
    b = canonical_bytes(draft)
    s = b.decode("utf-8")

    # Check that there is no whitespace immediately after { [ , :
    # and no whitespace immediately before } ] ,
    import re

    # No space after structural characters
    assert not re.search(r"[{[,]\s", s)
    # No space before structural characters
    assert not re.search(r"\s[}\],]", s)
    # No space around colons
    assert not re.search(r"\s:", s)
    assert not re.search(r":\s", s)
