"""
Cross-language parity: Python and JavaScript must agree on every draft hash.

This is the seam between Member 3 (backend/canonical.py) and Member 2
(frontend/js/canonical.js). The gateway hashes the draft it stored; the
frontend hashes the draft it is displaying. If the two implementations ever
disagree by a single byte, the challenge comparison at /api/execute step 8
fails for every user and money stops moving.

Each test hashes the SAME concrete draft (a real fixture, or the server's
model_dump of one) in both languages and asserts the hex digests are equal.

Skipped automatically when node is not on PATH; the Python suite alone never
proves parity, so a skip is visible in the report rather than silent.
"""

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.canonical import draft_hash  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402

FIXTURES_DIR = ROOT / "fixtures" / "drafts"
CANONICAL_JS = ROOT / "frontend" / "js" / "canonical.js"
FIXTURES = sorted(p.name for p in FIXTURES_DIR.glob("*.json"))

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Reads a JSON file (path in argv[1]) and prints draftHash() of its contents.
# The require() path is injected with json.dumps so it cannot break on quotes.
_JS_SNIPPET = (
    "const fs=require('fs');"
    f"const c=require({json.dumps(str(CANONICAL_JS))});"
    "process.stdout.write(c.draftHash(JSON.parse(fs.readFileSync(process.argv[1],'utf-8'))));"
)


def js_draft_hash(draft: dict) -> str:
    """Hash *draft* with the frontend's canonical.js, via node."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as fh:
        json.dump(draft, fh, ensure_ascii=False)
        tmp = fh.name
    try:
        proc = subprocess.run(
            [NODE, "-e", _JS_SNIPPET, tmp],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        Path(tmp).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def server_dump(name: str) -> dict:
    """The hashed object: what the server would serialise and the client would parse."""
    raw = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return TransactionDraft.model_validate(raw).model_dump(mode="json", exclude_none=True)


# ── The parity guarantee ──────────────────────────────────────────────


@requires_node
@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_fixture_hashes_identically_in_python_and_js(fixture_name: str):
    """For every real fixture: Python draft_hash == JavaScript draftHash."""
    draft = server_dump(fixture_name)
    assert draft_hash(draft) == js_draft_hash(draft), (
        f"{fixture_name}: backend and frontend disagree on the draft hash"
    )


@requires_node
@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_raw_fixture_json_hashes_identically_in_python_and_js(fixture_name: str):
    """The raw fixture file (what /api/message puts on the wire) also agrees."""
    raw = json.loads((FIXTURES_DIR / fixture_name).read_text(encoding="utf-8"))
    assert draft_hash(raw) == js_draft_hash(raw)


@requires_node
def test_server_dump_and_raw_json_agree_in_both_languages():
    """Both paths converge: raw JSON == model_dump, in Python and in JS alike."""
    raw = json.loads((FIXTURES_DIR / "clean_transfer.json").read_text(encoding="utf-8"))
    dumped = server_dump("clean_transfer.json")
    assert draft_hash(raw) == draft_hash(dumped)
    assert js_draft_hash(raw) == js_draft_hash(dumped)
    assert draft_hash(dumped) == js_draft_hash(raw)


# ── Divergence is detected, not silently tolerated ────────────────────


@requires_node
def test_single_character_change_diverges_in_both_languages():
    """A tampered draft must hash differently in BOTH implementations.

    If one language normalised the change away, the gateway's step 8 would
    raise hash_mismatch while a tampered frontend still showed a match.
    """
    clean = server_dump("clean_transfer.json")
    tampered = copy.deepcopy(clean)
    tampered["amount"]["value"] = "5000.00"

    assert draft_hash(clean) != draft_hash(tampered)
    assert js_draft_hash(clean) != js_draft_hash(tampered)
    # And the two languages still agree on the tampered copy itself.
    assert draft_hash(tampered) == js_draft_hash(tampered)


@requires_node
def test_swapped_payee_diverges_in_both_languages():
    """The draft-swap attack: same shape, different beneficiary."""
    clean = server_dump("clean_transfer.json")
    swapped = copy.deepcopy(clean)
    swapped["payee"] = {
        "id": "payee-666",
        "display_name": "Mallory",
        "masked_account": "****6666",
    }
    assert draft_hash(clean) != draft_hash(swapped)
    assert js_draft_hash(clean) != js_draft_hash(swapped)
    assert draft_hash(swapped) == js_draft_hash(swapped)


@requires_node
def test_confidence_is_excluded_in_both_languages():
    """confidence is excluded from the hash on both sides (CANONICAL_HASH.md)."""
    clean = server_dump("clean_transfer.json")
    bumped = {**copy.deepcopy(clean), "confidence": 0.01}
    assert clean["confidence"] != bumped["confidence"]
    assert draft_hash(clean) == draft_hash(bumped)
    assert js_draft_hash(clean) == js_draft_hash(bumped)
    assert draft_hash(clean) == js_draft_hash(bumped)


@requires_node
def test_non_ascii_and_emoji_agree_in_both_languages():
    """Non-ASCII is emitted raw; keys sort by code point, not UTF-16 code unit."""
    draft = {
        "id": "x",
        "transcript": "Pay José 😀 五十元",
        "nested": {"ｚ": 1, "\U0001F600": 2, " José": [True, False, 7]},
    }
    assert draft_hash(draft) == js_draft_hash(draft)
