"""
The ledger is append-only: a posted transaction is a fact, and the only way to
correct one is to post another (backend/ledger/ledger.py).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.ledger import ENTRY_KEYS, FileLedger, InMemoryLedger, make_entry  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402

FIXTURES = ROOT / "fixtures" / "drafts"


def load(name: str) -> dict:
    return TransactionDraft.model_validate_json(
        (FIXTURES / name).read_text(encoding="utf-8")
    ).model_dump(mode="json", exclude_none=True)


def test_post_returns_increasing_ids():
    ledger = InMemoryLedger()
    assert ledger.post(load("clean_transfer.json"), idempotency_key="k1") == "tx-000001"
    assert ledger.post(load("clean_transfer.json"), idempotency_key="k2") == "tx-000002"
    assert len(ledger.entries) == 2


def test_an_entry_records_the_idempotency_key_and_the_draft():
    draft = load("clean_transfer.json")
    ledger = InMemoryLedger()
    ledger.post(draft, idempotency_key="k1")
    entry = ledger.entries[0]
    assert set(entry) == set(ENTRY_KEYS)
    assert entry["idempotency_key"] == "k1"
    assert entry["draft"] == draft


def test_the_draft_is_recorded_as_hashed_not_reserialised():
    """The ledger must not re-derive the draft, or what was signed and what was
    posted could diverge."""
    draft = load("clean_transfer.json")
    ledger = InMemoryLedger()
    ledger.post(draft, idempotency_key="k1")
    assert ledger.entries[0]["draft"] == draft
    draft["amount"] = {"value": "999999.00", "currency": "SGD"}
    assert ledger.entries[0]["draft"]["amount"]["value"] == "50.00"


def test_append_only_by_construction():
    ledger = InMemoryLedger()
    for name in ("delete", "remove", "pop", "clear", "update", "__delitem__", "__setitem__"):
        assert not hasattr(ledger, name), f"ledger must not expose {name}"


def test_the_ledger_does_not_enforce_idempotency_itself():
    """Idempotency is the gateway's check 10. The ledger records what it is
    told, so a double post here is visible as two rows rather than silently
    collapsed into one."""
    ledger = InMemoryLedger()
    ledger.post(load("clean_transfer.json"), idempotency_key="same")
    ledger.post(load("clean_transfer.json"), idempotency_key="same")
    assert len(ledger.entries) == 2


# ── Persistence ───────────────────────────────────────────────────────


def test_file_ledger_survives_a_restart(tmp_path):
    path = tmp_path / "ledger.jsonl"
    first = FileLedger(path)
    first.post(load("clean_transfer.json"), idempotency_key="k1")

    second = FileLedger(path)  # the process "restarted"
    assert len(second.entries) == 1
    assert second.post(load("clean_transfer.json"), idempotency_key="k2") == "tx-000002"
    assert len(second.entries) == 2


def test_file_ledger_appends_rather_than_rewrites(tmp_path):
    path = tmp_path / "ledger.jsonl"
    FileLedger(path).post(load("clean_transfer.json"), idempotency_key="k1")
    FileLedger(path).post(load("ambiguous_payee.json"), idempotency_key="k2")

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 2
    assert [r["transaction_id"] for r in rows] == ["tx-000001", "tx-000002"]


def test_make_entry_defaults_posted_at_to_now():
    entry = make_entry("tx-000001", "k1", {"a": 1})
    assert entry["posted_at"].endswith("Z")
    assert entry["draft"] == {"a": 1}
