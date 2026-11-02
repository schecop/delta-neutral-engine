"""In-memory venue: deterministic, offline, used by tests and dry runs.

It is also the reference implementation of the `VenueAdapter` contract: if a real adapter behaves
differently from this one on the conformance suite, the real adapter is wrong (or the contract
needs an ADR).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal

from delta_neutral.instruments import UnknownInstrumentError
from delta_neutral.models import (
    FundingRate,
    MarginState,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    Quote,
    Side,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FakeVenue:
    def __init__(
        self,
        name: str,
        mids: Mapping[str, Decimal],
        *,
        spread_bps: Decimal = Decimal(10),
        funding_pct_per_hour: Decimal = Decimal("0.001"),
        fee_bps: Decimal = Decimal(0),
        equity_usd: Decimal = Decimal(2000),
        leverage: Decimal = Decimal(3),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.name = name
        self._mids = dict(mids)
        self._half_spread = spread_bps / 2 / 10_000
        self._funding = funding_pct_per_hour
        self._fee_rate = fee_bps / 10_000
        self._equity = equity_usd
        self._leverage = leverage
        self._clock = clock
        self._positions: dict[
            str, tuple[Decimal, Decimal]
        ] = {}  # instrument -> (signed size, entry)

    def set_mid(self, instrument: str, mid: Decimal) -> None:
        """Move the market (tests use this to simulate price changes)."""
        self._mids[instrument] = mid

    def _mid(self, instrument: str) -> Decimal:
        try:
            return self._mids[instrument]
        except KeyError:
            raise UnknownInstrumentError(f"{self.name} does not list {instrument}") from None

    async def list_instruments(self) -> list[str]:
        return sorted(self._mids)

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        mid = self._mid(instrument)
        return Quote(
            venue=self.name,
            instrument=instrument,
            bid=mid * (1 - self._half_spread),
            ask=mid * (1 + self._half_spread),
            ts=self._clock(),
        )

    async def get_funding(self, instrument: str) -> FundingRate:
        self._mid(instrument)
        return FundingRate(
            venue=self.name,
            instrument=instrument,
            rate_pct_per_hour=self._funding,
            ts=self._clock(),
        )

    def _reject(self, order: OrderRequest, reason: str) -> OrderResult:
        return OrderResult(
            client_order_id=order.client_order_id,
            venue=self.name,
            instrument=order.instrument,
            side=order.side,
            status=OrderStatus.REJECTED,
            filled_size=Decimal(0),
            reason=reason,
            ts=self._clock(),
        )

    async def place_order(self, order: OrderRequest) -> OrderResult:
        if order.instrument not in self._mids:
            return self._reject(order, "unknown instrument")

        current, entry = self._positions.get(order.instrument, (Decimal(0), Decimal(0)))
        signed = order.size if order.side is Side.LONG else -order.size
        new_size = current + signed

        if order.reduce_only and (
            current == 0 or abs(new_size) > abs(current) or new_size * current < 0
        ):
            return self._reject(order, "reduce_only would open, increase or flip the position")

        quote = await self.get_quote(order.instrument, order.size)
        price = quote.ask if order.side is Side.LONG else quote.bid

        if new_size == 0:
            self._positions.pop(order.instrument, None)
        elif current == 0 or new_size * current < 0:
            self._positions[order.instrument] = (new_size, price)
        elif abs(new_size) > abs(current):  # adding: volume-weighted entry
            weighted = (abs(current) * entry + order.size * price) / abs(new_size)
            self._positions[order.instrument] = (new_size, weighted)
        else:  # reducing: entry unchanged
            self._positions[order.instrument] = (new_size, entry)

        return OrderResult(
            client_order_id=order.client_order_id,
            venue=self.name,
            instrument=order.instrument,
            side=order.side,
            status=OrderStatus.FILLED,
            filled_size=order.size,
            avg_price=price,
            fee_usd=order.size * price * self._fee_rate,
            ts=self._clock(),
        )

    async def get_positions(self) -> list[Position]:
        now = self._clock()
        return [
            Position(venue=self.name, instrument=i, size=s, entry_price=e, ts=now)
            for i, (s, e) in sorted(self._positions.items())
        ]

    async def get_margin(self) -> MarginState:
        used = sum(
            (abs(s) * self._mids[i] / self._leverage for i, (s, _) in self._positions.items()),
            start=Decimal(0),
        )
        return MarginState(
            venue=self.name, equity_usd=self._equity, used_margin_usd=used, ts=self._clock()
        )
