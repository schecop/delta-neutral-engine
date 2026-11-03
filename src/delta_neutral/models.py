"""Venue-agnostic domain types.

Every adapter translates venue-native payloads into these types, so nothing above `venues/`
ever sees a venue's own format. Money, prices and sizes are Decimal: floats lose cents.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"

    def opposite(self) -> Side:
        return Side.SHORT if self is Side.LONG else Side.LONG


class Execution(StrEnum):
    """How orders reach a venue (ADR-0001). The core reads this; it never reads a venue's name."""

    API = "api"  # the adapter places orders itself
    MANUAL = "manual"  # the operator places orders by hand; the adapter is read-only


class _Frozen(BaseModel):
    """Immutable: a quote or a fill is a fact about the past, never edited in place."""

    model_config = ConfigDict(frozen=True)


class Quote(_Frozen):
    venue: str
    instrument: str  # canonical symbol
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    ts: datetime
    indicative: bool = False  # RFQ venues may only offer indicative prices before a request

    @model_validator(mode="after")
    def _ask_not_below_bid(self) -> Quote:
        if self.ask < self.bid:
            raise ValueError(f"crossed quote: ask {self.ask} < bid {self.bid}")
        return self

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * 10_000


class FundingRate(_Frozen):
    venue: str
    instrument: str
    rate_pct_per_hour: Decimal  # normalised across venues; positive means longs pay shorts
    ts: datetime


class OrderRequest(_Frozen):
    venue: str
    instrument: str
    side: Side
    size: Decimal = Field(gt=0)  # base units of the canonical instrument
    client_order_id: str
    reduce_only: bool = False


class OrderStatus(StrEnum):
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"


class OrderResult(_Frozen):
    client_order_id: str
    venue: str
    instrument: str
    side: Side
    status: OrderStatus
    filled_size: Decimal = Field(ge=0)
    avg_price: Decimal | None = None
    fee_usd: Decimal = Decimal(0)
    reason: str | None = None  # why it was rejected, in the venue's words
    ts: datetime


class Position(_Frozen):
    venue: str
    instrument: str
    size: Decimal  # signed: positive long, negative short
    entry_price: Decimal | None = None
    ts: datetime


class MarginState(_Frozen):
    venue: str
    equity_usd: Decimal = Field(ge=0)
    used_margin_usd: Decimal = Field(ge=0)
    ts: datetime

    @property
    def free_fraction(self) -> Decimal:
        """Share of equity not tied up as margin, clamped to [0, 1]."""
        if self.equity_usd == 0:
            return Decimal(0)
        free = (self.equity_usd - self.used_margin_usd) / self.equity_usd
        return max(Decimal(0), min(Decimal(1), free))
