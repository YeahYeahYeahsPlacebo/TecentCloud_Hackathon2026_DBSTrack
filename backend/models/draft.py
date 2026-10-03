"""
Pydantic v2 models for the transaction draft contract.

Money is NEVER a float. Amounts are decimal strings (e.g. "50.00") so that
Python and JavaScript agree exactly when canonicalised and hashed.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    value: Decimal = Field(
        ...,
        description="Decimal string, e.g. '50.00'. No scientific notation, no float.",
        json_schema_extra={"pattern": r"^\d+\.\d{2}$"},
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
    def reject_float(cls, v: Any) -> Any:
        """Money is never a float. Reject float inputs before Decimal coercion."""
        if isinstance(v, float):
            raise ValueError("money value must be a decimal string, not a float")
        if isinstance(v, str):
            import re

            if not re.match(r"^\d+\.\d{2}$", v):
                raise ValueError(
                    f"money value must match pattern ^\\d+\\.\\d{{2}}$, got '{v}'"
                )
        return v


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
    created_at: datetime = Field(..., description="ISO-8601 timestamp the draft was created.")
    expires_at: datetime = Field(..., description="ISO-8601 timestamp after which the draft is stale.")
    nonce: str = Field(..., min_length=1, description="Caller-supplied nonce for idempotent execution.")
    intent_type: IntentType = Field(..., description="What the user wants to do: transfer, bill_payment, or equity_purchase.")
    source_account: str = Field(..., min_length=1, description="Account money leaves or shares are bought from.")
    amount: Money = Field(..., description="The monetary amount. Always a decimal string.")
    confidence: float = Field(..., ge=0, le=1, description="Parser confidence, 0.0 to 1.0 inclusive.")
    unresolved: list[str] = Field(
        default_factory=list,
        description="Field names the parser could not resolve. If non-empty, draft cannot be signed or executed.",
    )
    transcript: str = Field(..., description="The raw user text the draft was produced from. Data, never instructions.")

    # ── Per-intent fields (conditional on intent_type) ──
    payee: Optional[Payee] = Field(None, description="Beneficiary for transfer or bill_payment intents. Null when payee is unresolved.")
    payee_candidates: Optional[list[PayeeCandidate]] = Field(
        None,
        description="Candidate payees when 'payee' is in unresolved. Only meaningful then.",
    )
    ticker: Optional[str] = Field(None, description="Stock ticker for equity_purchase intents.")
    quantity: Optional[str] = Field(None, description="Share quantity for equity_purchase (decimal string).")
    notional_amount: Optional[str] = Field(None, description="Dollar target for equity_purchase (decimal string).")
    order_type: Optional[OrderType] = Field(None, description="Order type for equity_purchase.")

    def model_post_init(self, __context) -> None:
        """Validate conditional requirements after pydantic assembles the model."""
        intent = self.intent_type

        if intent in (IntentType.transfer, IntentType.bill_payment):
            if self.payee is None:
                if "payee" not in self.unresolved:
                    raise ValueError(
                        f"intent_type '{intent.value}' requires a 'payee' "
                        "or 'payee' in unresolved"
                    )

        if intent == IntentType.equity_purchase:
            if self.ticker is None:
                raise ValueError("intent_type 'equity_purchase' requires 'ticker'")
            if self.order_type is None:
                raise ValueError("intent_type 'equity_purchase' requires 'order_type'")
