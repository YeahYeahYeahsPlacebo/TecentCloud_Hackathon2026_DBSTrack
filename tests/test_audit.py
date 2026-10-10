"""
The audit log is hash-chained and ``verify_chain()`` detects tampering
(CONTRACT.md §7). The point of these tests is that *every* kind of edit is
caught: a payload change, a broken link, a removed row, a reordered pair.
"""

import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.audit import (  # noqa: E402
    GENESIS_PREV_HASH,
    AuditLog,
    AuditRow,
    compute_row_hash,
    verify_chain,
)


def filled(count: int = 5) -> AuditLog:
    log = AuditLog()
    for i in range(count):
        log.append(f"event-{i}", {"n": i})
    return log


# ── An intact chain ───────────────────────────────────────────────────


def test_empty_chain_is_intact():
    assert AuditLog().verify_chain().to_response() == {
        "intact": True,
        "broken_row": None,
        "rows_checked": 0,
    }


def test_fresh_chain_verifies():
    check = filled(5).verify_chain()
    assert check.intact and check.broken_row is None and check.rows_checked == 5


def test_each_row_carries_the_hash_of_the_one_before():
    log = filled(3)
    assert log.rows[0].prev_hash == GENESIS_PREV_HASH
    assert log.rows[1].prev_hash == log.rows[0].hash
    assert log.rows[2].prev_hash == log.rows[1].hash


def test_row_hash_covers_every_field():
    """Changing any hashed field changes the digest."""
    base = compute_row_hash(1, "2026-10-03T10:00:00Z", "e", {"a": 1}, GENESIS_PREV_HASH)
    assert base != compute_row_hash(2, "2026-10-03T10:00:00Z", "e", {"a": 1}, GENESIS_PREV_HASH)
    assert base != compute_row_hash(1, "2026-10-03T10:00:01Z", "e", {"a": 1}, GENESIS_PREV_HASH)
    assert base != compute_row_hash(1, "2026-10-03T10:00:00Z", "f", {"a": 1}, GENESIS_PREV_HASH)
    assert base != compute_row_hash(1, "2026-10-03T10:00:00Z", "e", {"a": 2}, GENESIS_PREV_HASH)
    assert base != compute_row_hash(1, "2026-10-03T10:00:00Z", "e", {"a": 1}, "f" * 64)


# ── Tampering ─────────────────────────────────────────────────────────


def test_editing_a_payload_in_the_middle_is_detected():
    log = filled(5)
    log.rows[2].payload["n"] = 999
    check = log.verify_chain()
    assert not check.intact
    assert check.broken_row["row_id"] == 3
    assert check.broken_row["expected_hash"] != check.broken_row["actual_hash"]
    assert check.rows_checked == 5


def test_editing_the_last_row_is_detected():
    log = filled(5)
    log.rows[-1].payload["n"] = 0
    assert not log.verify_chain().intact
    assert log.verify_chain().broken_row["row_id"] == 5


def test_breaking_a_link_is_detected():
    log = filled(4)
    log.rows[2] = dataclasses.replace(log.rows[2], prev_hash="f" * 64)
    check = log.verify_chain()
    assert not check.intact
    assert check.broken_row["row_id"] == 3


def test_removing_a_row_is_detected():
    """Dropping row 2 leaves ids 1,3 — the link no longer matches."""
    log = filled(4)
    del log.rows[1]
    check = log.verify_chain()
    assert not check.intact
    assert check.broken_row["row_id"] == 3


def test_swapping_two_rows_is_detected():
    log = filled(3)
    log.rows[0], log.rows[1] = log.rows[1], log.rows[0]
    assert not log.verify_chain().intact


def test_fully_recomputing_the_chain_defeats_detection():
    """A hash chain alone does NOT stop an attacker who rewrites every row.

    Edit a payload, recompute that row's hash and every hash after it, and the
    chain verifies cleanly. Catching that needs an anchor *outside* the log —
    a signed chain head, or the head hash recorded somewhere the attacker
    cannot also rewrite.

    This test asserts the limitation rather than a security property, so nobody
    reads 'the audit log is hash-chained' as 'the audit log is tamper-proof'.
    """
    log = filled(4)
    log.rows[1].payload["n"] = 999
    prev = log.rows[0].hash
    for index in range(1, len(log.rows)):
        row = log.rows[index]
        recomputed = compute_row_hash(
            row.row_id, row.timestamp, row.event, row.payload, prev
        )
        log.rows[index] = dataclasses.replace(row, prev_hash=prev, hash=recomputed)
        prev = recomputed
    assert log.verify_chain().intact


def test_verify_chain_accepts_a_plain_row_list():
    log = filled(3)
    assert verify_chain(log.rows).intact
    broken = list(log.rows)
    broken[1] = dataclasses.replace(broken[1], hash="0" * 64)
    assert not verify_chain(broken).intact


# ── Persistence ───────────────────────────────────────────────────────


def test_a_persisted_chain_survives_a_reload(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("first", {"a": 1})
    log.append("second", {"b": 2})
    assert log.verify_chain().intact

    reloaded = AuditLog(path)
    assert len(reloaded.rows) == 2
    assert reloaded.verify_chain().intact
    assert reloaded.rows[1].payload == {"b": 2}


def test_a_persisted_chain_detects_an_edit_made_outside_the_process(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("first", {"amount": "50.00"})
    log.append("second", {"amount": "10.00"})

    import json

    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    rows[0]["payload"]["amount"] = "500000.00"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    check = AuditLog(path).verify_chain()
    assert not check.intact
    assert check.broken_row["row_id"] == 1


def test_row_round_trips_through_dict():
    row = filled(1).rows[0]
    assert AuditRow.from_dict(row.to_dict()) == row
