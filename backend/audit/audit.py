"""
Hash-chained audit log (CONTRACT.md §7: "the audit log is hash-chained;
``verify_chain()`` detects tampering").

Each row carries the hash of the row before it, so rewriting any earlier row
changes every hash after it. ``verify_chain()`` recomputes from the genesis
row forward and reports the first row whose stored hash disagrees with the one
the chain implies.

The hash covers the row's own fields *and* the previous hash, so an attacker
cannot edit a payload, reorder rows, or drop a row without detection. Dropping
a row is caught by ``row_id`` being part of the hashed material: the sequence
must be 1..N with no gap.

**Limitation.** This detects *partial* tampering. An attacker who rewrites a
payload and then recomputes that row's hash and every hash after it produces a
chain that verifies perfectly — see
``tests/test_audit.py::test_fully_recomputing_the_chain_defeats_detection``.
Closing that gap needs an anchor outside this log: a signed chain head, or the
head hash recorded somewhere the attacker cannot also rewrite. Until then,
read "hash-chained" as "tamper-evident for partial edits", not "tamper-proof".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from backend.canonical import canonical_bytes

# The previous hash of the first row. Not a secret, just an anchor.
GENESIS_PREV_HASH = "0" * 64


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_row_hash(
    row_id: int,
    timestamp: str,
    event: str,
    payload: Mapping[str, Any],
    prev_hash: str,
) -> str:
    """SHA-256 over the canonical bytes of everything that identifies the row.

    Using ``canonical_bytes`` (rather than ``json.dumps``) keeps the hashing
    identical to the draft hash: sorted keys, no whitespace, no float, and a
    lone surrogate raises instead of silently producing a different digest.
    """
    material = {
        "row_id": row_id,
        "timestamp": timestamp,
        "event": event,
        "payload": dict(payload),
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(canonical_bytes(material)).hexdigest()


@dataclass(frozen=True)
class AuditRow:
    """One immutable entry. Every field is hashed, so every field is covered."""

    row_id: int
    timestamp: str
    event: str
    payload: dict
    prev_hash: str
    hash: str

    def to_dict(self) -> dict:
        return {
            "row_id": self.row_id,
            "timestamp": self.timestamp,
            "event": self.event,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AuditRow":
        return cls(
            row_id=data["row_id"],
            timestamp=data["timestamp"],
            event=data["event"],
            payload=data["payload"],
            prev_hash=data["prev_hash"],
            hash=data["hash"],
        )


@dataclass(frozen=True)
class ChainCheck:
    """The result of :meth:`AuditLog.verify_chain`.

    ``broken_row`` is null when the chain is intact, otherwise::

        {"row_id": 17, "expected_hash": "...", "actual_hash": "..."}

    ``expected_hash`` is what the chain implies for that row;
    ``actual_hash`` is what the row actually carries.
    ``rows_checked`` is the total number of rows examined (CONTRACT.md §4,
    GET /api/audit/verify).
    """

    intact: bool
    broken_row: Optional[dict] = None
    rows_checked: int = 0

    def to_response(self) -> dict:
        return {
            "intact": self.intact,
            "broken_row": self.broken_row,
            "rows_checked": self.rows_checked,
        }


class AuditLog:
    """Append-only hash chain. Optionally persisted as one JSON object per line."""

    def __init__(self, path: "Optional[str | Path]" = None) -> None:
        self.path = Path(path) if path is not None else None
        self.rows: list[AuditRow] = []
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.rows.append(AuditRow.from_dict(json.loads(line)))

    # ── Write side ────────────────────────────────────────────────────

    def append(
        self,
        event: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        timestamp: Optional[str] = None,
    ) -> AuditRow:
        """Record an event. The hash is derived, never supplied by the caller."""
        prev_hash = self.rows[-1].hash if self.rows else GENESIS_PREV_HASH
        row = AuditRow(
            row_id=len(self.rows) + 1,
            timestamp=timestamp or _now_z(),
            event=event,
            payload=dict(payload or {}),
            prev_hash=prev_hash,
            hash="",
        )
        row = AuditRow(
            row_id=row.row_id,
            timestamp=row.timestamp,
            event=row.event,
            payload=row.payload,
            prev_hash=row.prev_hash,
            hash=compute_row_hash(
                row.row_id, row.timestamp, row.event, row.payload, prev_hash
            ),
        )
        self.rows.append(row)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")
                handle.flush()
        return row

    # ── Verify side ───────────────────────────────────────────────────

    def verify_chain(self) -> ChainCheck:
        """Recompute every row from the genesis anchor and find the first break."""
        prev_hash = GENESIS_PREV_HASH
        for row in self.rows:
            # 1. The link. A row inserted, removed or reordered breaks this
            #    even if its own hash was recomputed to match.
            if row.prev_hash != prev_hash:
                return ChainCheck(
                    intact=False,
                    broken_row={
                        "row_id": row.row_id,
                        "expected_hash": prev_hash,
                        "actual_hash": row.prev_hash,
                    },
                    rows_checked=len(self.rows),
                )
            # 2. The row's own contents.
            expected = compute_row_hash(
                row.row_id, row.timestamp, row.event, row.payload, prev_hash
            )
            if expected != row.hash:
                return ChainCheck(
                    intact=False,
                    broken_row={
                        "row_id": row.row_id,
                        "expected_hash": expected,
                        "actual_hash": row.hash,
                    },
                    rows_checked=len(self.rows),
                )
            prev_hash = row.hash
        return ChainCheck(intact=True, broken_row=None, rows_checked=len(self.rows))


def verify_chain(rows: "list[AuditRow] | AuditLog") -> ChainCheck:
    """CONTRACT.md §7 names this function. Accepts a log or a plain row list."""
    if isinstance(rows, AuditLog):
        return rows.verify_chain()
    log = AuditLog()
    log.rows = list(rows)
    return log.verify_chain()
