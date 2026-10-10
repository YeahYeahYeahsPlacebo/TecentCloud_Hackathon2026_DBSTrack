"""
The transaction ledger: the record of money that actually moved.

Append-only by construction. There is no update and no delete — a posted
transaction is a fact, and the only way to correct one is to post another.
That is why ``post`` returns a new id rather than taking one, and why
``entries`` is exposed read-only by convention.

The gateway is the only writer (CONTRACT.md §4, POST /api/execute). The
parser, validator and policy engine must never import this module.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

# One posted transaction. Keys are fixed; adding one is a contract change.
ENTRY_KEYS = ("transaction_id", "idempotency_key", "posted_at", "draft")


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_entry(
    transaction_id: str,
    idempotency_key: str,
    draft: Mapping[str, Any],
    posted_at: Optional[str] = None,
) -> dict:
    """Build one ledger row. ``draft`` is stored as hashed, not re-serialised."""
    return {
        "transaction_id": transaction_id,
        "idempotency_key": idempotency_key,
        "posted_at": posted_at or _now_z(),
        "draft": dict(draft),
    }


class Ledger(Protocol):
    """Write side. Only the gateway posts to it."""

    def post(self, draft: Mapping[str, Any], *, idempotency_key: str) -> str:
        """Record an executed draft and return its transaction id."""
        ...


class InMemoryLedger:
    """Append-only, in-process. Used by tests and as the default sink."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def post(self, draft: Mapping[str, Any], *, idempotency_key: str) -> str:
        transaction_id = f"tx-{len(self.entries) + 1:06d}"
        self.entries.append(make_entry(transaction_id, idempotency_key, draft))
        return transaction_id


class FileLedger:
    """Append-only, one JSON object per line, so a restart does not lose it.

    Every ``post`` writes the line and flushes before returning: a transaction
    that was acknowledged must survive the process dying on the next line.
    """

    def __init__(self, path: "str | Path") -> None:
        self.path = Path(path)
        self.entries: list[dict] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.entries.append(json.loads(line))

    def post(self, draft: Mapping[str, Any], *, idempotency_key: str) -> str:
        transaction_id = f"tx-{len(self.entries) + 1:06d}"
        entry = make_entry(transaction_id, idempotency_key, draft)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
        self.entries.append(entry)
        return transaction_id
