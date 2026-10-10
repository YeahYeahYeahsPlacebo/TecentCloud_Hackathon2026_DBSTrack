#!/usr/bin/env python3
"""
Manual evaluation script for the Validation Agent.

NOT run by pytest.  Loads real settings from .env, creates an LlmValidator
with the real dcta-validator ADP application, and runs a fixed set of
draft+transcript pairs through it.  Prints a table showing case, expected,
verdict, discrepancies, and match.

Never prints AppKeys or request bodies.

Usage:
    .venv/bin/python scripts/eval_validator.py [--delay SECONDS]

Options:
    --delay SECONDS   Wait this many seconds between live ADP calls
                       to avoid hitting the rate limit.  Default: 3.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.llm.adp_client import AdpChatClient  # noqa: E402
from backend.llm.config import load_llm_settings  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402
from backend.parser.directory import FixtureDirectory  # noqa: E402
from backend.parser.interface import ClarificationRecord  # noqa: E402
from backend.validator.llm_validator import LlmValidator  # noqa: E402

DIRECTORY = FixtureDirectory()


# ── Draft builders ────────────────────────────────────────────────────


def _transfer_draft(
    *,
    amount: str = "50.00",
    payee_id: str = "payee-001",
    source_account: str = "acct-001-2345",
    transcript: str = "Send fifty dollars to John Smith.",
) -> TransactionDraft:
    payee = next(p for p in DIRECTORY.payees() if p.id == payee_id)
    return TransactionDraft.model_validate({
        "id": "11111111-1111-1111-1111-111111111111",
        "created_at": "2026-10-03T10:00:00Z",
        "expires_at": "2026-10-03T10:05:00Z",
        "nonce": "nonce-eval-val-001",
        "intent_type": "transfer",
        "source_account": source_account,
        "amount": {"value": amount, "currency": "SGD"},
        "confidence": 0.95,
        "unresolved": [],
        "transcript": transcript,
        "payee": {
            "id": payee.id,
            "display_name": payee.display_name,
            "masked_account": payee.masked_account,
        },
    })


def _equity_draft(
    *,
    amount: str = "1000.00",
    ticker: str = "D05.SI",
    order_type: str = "market",
    source_account: str = "acct-001-2345",
    transcript: str = "Buy one thousand dollars of DBS shares at market price.",
) -> TransactionDraft:
    return TransactionDraft.model_validate({
        "id": "33333333-3333-3333-3333-333333333333",
        "created_at": "2026-10-03T11:00:00Z",
        "expires_at": "2026-10-03T11:05:00Z",
        "nonce": "nonce-eval-val-eq-001",
        "intent_type": "equity_purchase",
        "source_account": source_account,
        "amount": {"value": amount, "currency": "SGD"},
        "confidence": 0.92,
        "unresolved": [],
        "transcript": transcript,
        "ticker": ticker,
        "notional_amount": amount,
        "order_type": order_type,
    })


def _jane_draft() -> TransactionDraft:
    return _transfer_draft(
        amount="1000.00",
        payee_id="payee-002",
        transcript="Transfer one thousand dollars to Jane.",
    )


# ── Cases ─────────────────────────────────────────────────────────────


def _build_cases() -> list[dict]:
    """Build the list of evaluation cases.

    Each case is a dict with:
        name: short label
        draft: the TransactionDraft to validate
        transcript: the transcript to compare against
        expected: "pass" or "freeze"
        clarification: optional ClarificationRecord
    """
    cases: list[dict] = []

    # ── Correct drafts -> pass ────────────────────────────────────
    cases.append({
        "name": "clean_transfer (John Smith, 50.00)",
        "draft": _transfer_draft(),
        "transcript": "Send fifty dollars to John Smith.",
        "expected": "pass",
    })

    cases.append({
        "name": "Jane Tan transfer (1000.00)",
        "draft": _jane_draft(),
        "transcript": "Transfer one thousand dollars to Jane.",
        "expected": "pass",
    })

    cases.append({
        "name": "DBS equity (1000.00, D05.SI)",
        "draft": _equity_draft(),
        "transcript": "Buy one thousand dollars of DBS shares at market price.",
        "expected": "pass",
    })

    # ── Amount times 10 -> freeze ─────────────────────────────────
    cases.append({
        "name": "amount x10 (500.00 vs 50.00)",
        "draft": _transfer_draft(amount="500.00"),
        "transcript": "Send fifty dollars to John Smith.",
        "expected": "freeze",
    })

    # ── Payee swapped -> freeze ───────────────────────────────────
    cases.append({
        "name": "payee swapped (Jane instead of John)",
        "draft": _transfer_draft(payee_id="payee-002"),
        "transcript": "Send fifty dollars to John Smith.",
        "expected": "freeze",
    })

    # ── Intent switched -> freeze ─────────────────────────────────
    cases.append({
        "name": "intent switched (equity draft, transfer said)",
        "draft": _equity_draft(),
        "transcript": "Send fifty dollars to John Smith.",
        "expected": "freeze",
    })

    # ── Phase 1 bad output -> freeze ──────────────────────────────
    cases.append({
        "name": "phase 1 bad output (50.00 D05.SI equity)",
        "draft": _equity_draft(
            amount="50.00",
            ticker="D05.SI",
            transcript="Send fifty dollars to John Smith and buy one thousand dollars of DBS shares.",
        ),
        "transcript": "Send fifty dollars to John Smith and buy one thousand dollars of DBS shares.",
        "expected": "freeze",
    })

    # ── Clarification with answer -> pass ────────────────────────
    clarification_with = ClarificationRecord(
        question_id="q-amount-eval-001",
        field="amount",
        original_transcript="Send some money to John Smith.",
        answer="fifty dollars",
    )
    cases.append({
        "name": "clarification WITH answer (50.00)",
        "draft": _transfer_draft(
            amount="50.00",
            transcript="Send some money to John Smith.",
        ),
        "transcript": "Send some money to John Smith.",
        "expected": "pass",
        "clarification": clarification_with,
    })

    # ── Clarification without answer -> freeze ───────────────────
    cases.append({
        "name": "clarification WITHOUT answer",
        "draft": _transfer_draft(
            amount="50.00",
            transcript="Send some money to John Smith.",
        ),
        "transcript": "Send some money to John Smith.",
        "expected": "freeze",
    })

    return cases


# ── Table printing ────────────────────────────────────────────────────


def _print_table(
    rows: list[tuple[str, str, str, str, str]],
) -> tuple[int, int]:
    """Print the results table. Returns (matched, total)."""
    w_name = 50
    w_expected = 10
    w_verdict = 10
    w_disc = 30
    w_match = 5

    hdr = (
        f"{'case':<{w_name}} "
        f"{'expected':<{w_expected}} "
        f"{'verdict':<{w_verdict}} "
        f"{'discrepancies':<{w_disc}} "
        f"{'match':<{w_match}}"
    )
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)

    matched = 0
    for name, expected, verdict, disc, match_str in rows:
        if match_str == "OK":
            matched += 1
        name_short = name if len(name) <= w_name else name[: w_name - 3] + "..."
        disc_short = disc if len(disc) <= w_disc else disc[: w_disc - 3] + "..."
        print(
            f"{name_short:<{w_name}} "
            f"{expected:<{w_expected}} "
            f"{verdict:<{w_verdict}} "
            f"{disc_short:<{w_disc}} "
            f"{match_str:<{w_match}}"
        )
    return matched, len(rows)


def _delay(seconds: float) -> None:
    """Wait between live ADP calls to avoid hitting the rate limit."""
    if seconds > 0:
        time.sleep(seconds)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Seconds to wait between live ADP calls (default: 3).",
    )
    args = ap.parse_args()
    delay = args.delay

    settings = load_llm_settings(env_path=str(ROOT / ".env"))

    client = AdpChatClient(
        app_key=settings.validator_app_key,
        endpoint=settings.adp_endpoint,
        timeout_seconds=settings.adp_timeout_seconds,
    )
    validator = LlmValidator(client=client, directory=DIRECTORY)

    print(f"=== Validation Agent eval (delay={delay}s) ===")
    print(f"Validator model: {settings.validator_model_label}")
    print(f"Parser model: {settings.parser_model_label}")
    print()

    cases = _build_cases()
    rows: list[tuple[str, str, str, str, str]] = []

    for i, case in enumerate(cases):
        # Wait before every call except the first.
        if i > 0:
            _delay(delay)

        draft = case["draft"]
        transcript = case["transcript"]
        expected = case["expected"]
        clarification = case.get("clarification")

        try:
            verdict = validator.validate(draft, transcript, clarification=clarification)
            v = verdict.verdict
            disc = ", ".join(verdict.discrepancies) if verdict.discrepancies else ""
        except Exception as e:
            # Never print the full error if it might contain keys.
            v = "error"
            disc = str(type(e).__name__)

        match = "OK" if v == expected else "FAIL"
        rows.append((case["name"], expected, v, disc, match))

    matched, total = _print_table(rows)
    print()
    print(f"{total} cases, {matched} matched expectation.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
