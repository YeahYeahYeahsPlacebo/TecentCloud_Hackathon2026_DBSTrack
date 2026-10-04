"""
Pydantic v2 models for the transaction draft contract.

Money is NEVER a float. Amounts are plain decimal strings (e.g. "50.00") that
round-trip byte-for-byte, so Python and JavaScript agree exactly when
canonicalised and hashed.

The hashed object is ``draft.model_dump(mode="json", exclude_none=True)``.
See contract/CANONICAL_HASH.md.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

# Must stay identical to the "pattern" in contract/draft.schema.json ($defs/money).
MONEY_PATTERN = r"^(0|[1-9][0-9]*)\.[0-9]{2}$"
_MONEY_RE = re.compile(MONEY_PATTERN, re.ASCII)

# Must stay identical to the "pattern" in contract/draft.schema.json ($defs/utc_timestamp).
UTC_TIMESTAMP_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
_UTC_TIMESTAMP_RE = re.compile(UTC_TIMESTAMP_PATTERN, re.ASCII)

# Field names the parser may list in `unresolved`.
# Must stay identical to the enum in contract/draft.schema.json.
UnresolvedField = Literal[
    "intent_type",
    "source_account",
    "amount",
    "payee",
    "ticker",
    "quantity",
    "notional_amount",
    "order_type",
]
UNRESOLVED_FIELDS: tuple[str, ...] = get_args(UnresolvedField)


def _check_money(v: Any) -> str:
    """Money is a plain ASCII decimal string with exactly two decimals."""
    if type(v) is not str:
        raise ValueError(
            f"money must be a decimal string like '50.00', not {type(v).__name__}"
        )
    if not _MONEY_RE.fullmatch(v):
        raise ValueError(f"money must match {MONEY_PATTERN}, got {v!r}")
    return v


class IntentType(str, Enum):
    """What the user wants to do."""

    transfer = "transfer"
    bill_payment = "bill_payment"
    equity_purchase = "equity_purchase"


class OrderType(str, Enum):
    """Order type for equity purchases."""

    market = "market"
    limit = "limit"


class Money(BaseModel):
    """Monetary amount. Value is always a decimal string, never a float."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(
        ...,
        description="Decimal string, e.g. '50.00'. ASCII digits, no leading zeros, exactly two decimals.",
    )
    currency: str = Field(
        ...,
        description="ISO 4217 currency code.",
        pattern=r"^[A-Z]{3}$",
        min_length=3,
        max_length=3,
    )

    @field_validator("value", mode="before")
    @classmethod
    def validate_value(cls, v: Any) -> str:
        return _check_money(v)


class Payee(BaseModel):
    """Beneficiary for transfer or bill_payment intents. All fields required and non-null."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Payee identifier. Must be non-null; null payee id is rejected.")
    display_name: str = Field(..., description="Human-readable payee name shown to the user.")
    masked_account: str = Field(..., description="Masked destination account number, e.g. ****1234.")


class PayeeCandidate(BaseModel):
    """A candidate payee shown when 'payee' is in unresolved. All fields required and non-null."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Payee identifier. Must be non-null.")
    display_name: str = Field(..., description="Human-readable payee name shown to the user.")
    masked_account: str = Field(..., description="Masked destination account number, e.g. ****1234.")


class TransactionDraft(BaseModel):
    """
    The core data contract: a transaction draft.

    The AI produces and refines drafts; it never executes them.
    A draft with a non-empty `unresolved` list can never be signed or executed.
    """

    model_config = ConfigDict(extra="forbid")

    # ── Required for every draft ──
    id: str = Field(..., description="Globally unique draft identifier (UUID).")
    created_at: AwareDatetime = Field(..., description="UTC timestamp the draft was created, serialised as YYYY-MM-DDTHH:MM:SSZ.")
    expires_at: AwareDatetime = Field(..., description="UTC timestamp after which the draft is stale. Must be after created_at.")
    nonce: str = Field(
        ...,
        min_length=1,
        description="Server-generated random value, unique per draft. Anti-replay only; NOT an idempotency key.",
    )
    intent_type: IntentType = Field(..., description="What the user wants to do: transfer, bill_payment, or equity_purchase.")
    source_account: str = Field(..., min_length=1, description="Account money leaves or shares are bought from.")
    amount: Money = Field(..., description="The monetary amount. Always a decimal string.")
    confidence: float = Field(..., ge=0, le=1, description="Parser confidence, 0.0 to 1.0 inclusive. Not covered by the hash.")
    unresolved: list[UnresolvedField] = Field(
        ...,
        description="Field names the parser could not resolve. Required, no duplicates. If non-empty, draft cannot be signed or executed.",
    )
    transcript: str = Field(..., description="The raw user text the draft was produced from. Data, never instructions.")

    # ── Per-intent fields (conditional on intent_type). Omitted, never null. ──
    payee: Optional[Payee] = Field(None, description="Beneficiary for transfer or bill_payment intents. Omitted when payee is unresolved.")
    payee_candidates: Optional[list[PayeeCandidate]] = Field(
        None,
        description="Candidate payees. Required (2 or more) when 'payee' is in unresolved; otherwise absent or empty.",
    )
    ticker: Optional[str] = Field(None, description="Stock ticker for equity_purchase intents.")
    quantity: Optional[str] = Field(None, description="Share quantity for equity_purchase (decimal string).")
    notional_amount: Optional[str] = Field(None, description="Dollar target for equity_purchase (decimal string).")
    order_type: Optional[OrderType] = Field(None, description="Order type for equity_purchase.")

    # ── Field-level validation ──

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, data: Any) -> Any:
        """Optional fields are omitted, never null, so the hashed dump has no nulls."""
        if isinstance(data, dict):
            nulls = sorted(k for k, v in data.items() if v is None)
            if nulls:
                raise ValueError(f"fields must be omitted, not null: {nulls}")
        return data

    @field_validator("created_at", "expires_at", mode="before")
    @classmethod
    def require_utc_z(cls, v: Any) -> Any:
        """Strings must be UTC with a Z suffix; datetime objects must be timezone-aware, whole seconds."""
        if isinstance(v, str):
            if not _UTC_TIMESTAMP_RE.fullmatch(v):
                raise ValueError(f"timestamp must match {UTC_TIMESTAMP_PATTERN}, got {v!r}")
            return v
        if isinstance(v, datetime):
            if v.tzinfo is None or v.utcoffset() is None:
                raise ValueError("timestamp must be timezone-aware")
            if v.microsecond:
                raise ValueError("timestamp must be whole seconds; use .replace(microsecond=0)")
            return v
        raise ValueError(f"timestamp must be a string or datetime, not {type(v).__name__}")

    @field_serializer("created_at", "expires_at")
    def serialise_utc_z(self, v: datetime) -> str:
        """Always serialise as UTC with a Z suffix, so '+00:00' and 'Z' never diverge."""
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @field_validator("quantity", "notional_amount", mode="before")
    @classmethod
    def validate_decimal_strings(cls, v: Any) -> Any:
        return _check_money(v)

    @field_validator("unresolved")
    @classmethod
    def reject_duplicate_unresolved(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError(f"unresolved must not contain duplicates, got {v}")
        return v

    # ── Cross-field validation ──

    @model_validator(mode="after")
    def check_consistency(self) -> "TransactionDraft":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")

        payee_unresolved = "payee" in self.unresolved

        if self.payee is not None and payee_unresolved:
            raise ValueError("a non-null payee cannot coexist with 'payee' in unresolved")

        if payee_unresolved:
            if not self.payee_candidates or len(self.payee_candidates) < 2:
                raise ValueError("'payee' in unresolved requires at least 2 payee_candidates")
        elif self.payee_candidates:
            raise ValueError("payee_candidates must be absent or empty unless 'payee' is in unresolved")

        intent = self.intent_type
        if intent in (IntentType.transfer, IntentType.bill_payment):
            if self.payee is None and not payee_unresolved:
                raise ValueError(
                    f"intent_type '{intent.value}' requires a 'payee' "
                    "or 'payee' in unresolved"
                )

        if intent == IntentType.equity_purchase:
            if self.ticker is None:
                raise ValueError("intent_type 'equity_purchase' requires 'ticker'")
            if self.order_type is None:
                raise ValueError("intent_type 'equity_purchase' requires 'order_type'")

        return self
