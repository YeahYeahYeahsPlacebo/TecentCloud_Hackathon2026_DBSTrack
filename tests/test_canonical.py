"""
Tests for the canonical draft hashing implementation.

Loads every vector from contract/test_vectors.json and checks that the
Python implementation (backend/canonical.py) produces the expected hash
or raises as specified.
"""

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.canonical import canonical_bytes, draft_hash  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402

VECTORS_PATH = ROOT / "contract" / "test_vectors.json"
FIXTURES_DIR = ROOT / "fixtures" / "drafts"
FIXTURES = sorted(p.name for p in FIXTURES_DIR.glob("*.json"))


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


# ── Negative tests: float, null and lone surrogates must raise ─────────


@pytest.mark.parametrize("index,vector", RAISE_VECTORS)
def test_should_raise_vectors_raise(index: int, vector: dict):
    """Vectors marked should_raise must raise an error in Python."""
    with pytest.raises(ValueError):
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


# ── Key sort: code point, not UTF-16 code unit ─────────────────────────


def _naive_utf16_sorted_hash(draft: dict) -> str:
    """A deliberately wrong canonicaliser: identical except keys are sorted
    by UTF-16 code units (what JavaScript's default .sort() does)."""

    def ser(v):
        if isinstance(v, dict):
            keys = sorted(v, key=lambda k: k.encode("utf-16-be"))
            return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + ser(v[k]) for k in keys) + "}"
        if isinstance(v, list):
            return "[" + ",".join(ser(x) for x in v) + "]"
        return json.dumps(v, ensure_ascii=False)

    root = {k: v for k, v in draft.items() if k not in ("signature", "confidence")}
    return hashlib.sha256(ser(root).encode("utf-8")).hexdigest()


def _vector(description_prefix: str) -> dict:
    return next(v for v in VECTORS if v["description"].startswith(description_prefix))


def test_naive_utf16_sort_matches_on_ascii_keys():
    """Control: on ASCII-only keys the naive canonicaliser agrees, so any
    difference below comes from the sort order alone."""
    v = VECTORS[0]
    assert _naive_utf16_sorted_hash(v["draft"]) == v["expected_hash"]


def test_key_sort_vector_rejects_naive_utf16_sort():
    """The U+FF5A vs U+1F600 vector must catch a UTF-16 code-unit sort."""
    v = _vector("key sort: U+FF5A vs emoji")
    keys = ["ｚ", "\U0001F600"]
    assert sorted(keys, key=lambda k: [ord(c) for c in k]) == ["ｚ", "\U0001F600"]
    assert sorted(keys, key=lambda k: k.encode("utf-16-be")) == ["\U0001F600", "ｚ"]
    assert draft_hash(v["draft"]) == v["expected_hash"]
    assert _naive_utf16_sorted_hash(v["draft"]) != v["expected_hash"]


# ── Malformed input is rejected, not hashed inconsistently ─────────────


def test_lone_surrogates_raise():
    for s in ("\ud800", "\udc00", "a\ud83d", "\ude00b"):
        with pytest.raises(ValueError, match="surrogate"):
            draft_hash({"v": s})
        with pytest.raises(ValueError, match="surrogate"):
            draft_hash({s: "v"})


def test_valid_surrogate_pair_from_json_is_one_code_point():
    """json.loads joins an escaped pair into one code point, which is fine."""
    assert draft_hash(json.loads('{"v":"\\ud83d\\ude00"}')) == draft_hash({"v": "\U0001F600"})


def test_control_characters_use_lowercase_hex():
    assert canonical_bytes({"v": "\x00\x1f\n"}) == b'{"v":"\\u0000\\u001f\\n"}'


# ── The hashed object: model_dump(mode="json", exclude_none=True) ─────


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_raw_fixture_and_model_dump_hash_identically(fixture_name: str):
    """Hashing the raw JSON and the server's dump of the same draft must agree."""
    raw = json.loads((FIXTURES_DIR / fixture_name).read_text(encoding="utf-8"))
    dumped = TransactionDraft.model_validate(raw).model_dump(mode="json", exclude_none=True)
    assert draft_hash(raw) == draft_hash(dumped)


def test_equal_instants_serialise_to_the_same_z_timestamp():
    """'+00:00', '+08:00' and 'Z' datetimes for the same instant hash the same."""
    raw = json.loads((FIXTURES_DIR / "clean_transfer.json").read_text(encoding="utf-8"))
    sgt = timezone(timedelta(hours=8))
    variants = [
        (datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc), datetime(2026, 10, 3, 10, 5, tzinfo=timezone.utc)),
        (datetime(2026, 10, 3, 18, 0, tzinfo=sgt), datetime(2026, 10, 3, 18, 5, tzinfo=sgt)),
    ]
    hashes = set()
    for created, expires in variants:
        dumped = TransactionDraft.model_validate({**raw, "created_at": created, "expires_at": expires}).model_dump(
            mode="json", exclude_none=True
        )
        assert dumped["created_at"] == "2026-10-03T10:00:00Z"
        assert dumped["expires_at"] == "2026-10-03T10:05:00Z"
        hashes.add(draft_hash(dumped))
    assert hashes == {draft_hash(raw)}


def test_python_mode_dump_is_rejected():
    """model_dump() without mode="json" contains Enum objects; refuse to hash it."""
    raw = json.loads((FIXTURES_DIR / "clean_transfer.json").read_text(encoding="utf-8"))
    with pytest.raises(TypeError):
        draft_hash(TransactionDraft.model_validate(raw).model_dump())


def test_dump_without_exclude_none_is_rejected():
    """A dump that still contains null fields is not the hashed object."""
    raw = json.loads((FIXTURES_DIR / "clean_transfer.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="null"):
        draft_hash(TransactionDraft.model_validate(raw).model_dump(mode="json"))


def test_non_plain_types_are_rejected():
    for bad in ({"v": Decimal("50.00")}, {"v": (1, 2)}, {"v": datetime(2026, 1, 1, tzinfo=timezone.utc)}):
        with pytest.raises(TypeError):
            draft_hash(bad)


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
