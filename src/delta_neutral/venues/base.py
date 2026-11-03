"""The contract every venue implements.

The engine only ever talks to these interfaces. Adding a venue means writing one class that
satisfies them and passing the conformance suite; the core never changes.

Two levels, chosen by the venue's execution capability (ADR-0001):
- `VenueAdapter`: the read side every venue implements (instruments, quotes, funding).
- `TradingVenue`: adds orders, positions and margin. Only `Execution.API` venues implement it;
  a `Execution.MANUAL` venue's orders are placed by the operator and confirmed on Telegram.

Contract (enforced by tests/conformance/):
- `execution` matches what the adapter can do: an API venue implements `TradingVenue`.
- Symbols in and out are canonical; translation to venue-native names happens inside the adapter.
- `get_quote` / `get_funding` raise `UnknownInstrumentError` for instruments the venue doesn't list,
  and `VenueUnavailableError` when it lists them but cannot price them right now.
- `place_order` never raises for a business rejection: it returns `OrderStatus.REJECTED` with a
  reason. It raises only for transport failures (timeouts, connection errors), which the executor
  must treat as "state unknown" and reconcile through `get_positions`.
- A `reduce_only` order that would open or increase a position is rejected.
- Funding is normalised to % per hour, positive meaning longs pay shorts.
- All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from delta_neutral.models import (
    Execution,
    FundingRate,
    MarginState,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
)


class VenueUnavailableError(RuntimeError):
    """The venue could not answer now: rate limited, stale feed, or no data for a listing.

    Transient by nature: callers skip this snapshot or cycle rather than act on missing data.
    """


class NotTradableError(TypeError):
    """Something tried to place orders through a venue whose execution is manual."""


@runtime_checkable
class VenueAdapter(Protocol):
    name: str
    execution: Execution

    async def list_instruments(self) -> list[str]:
        """Canonical symbols this venue can trade."""
        ...

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        """Executable (or indicative, flagged) prices for `size` base units."""
        ...

    async def get_funding(self, instrument: str) -> FundingRate: ...


@runtime_checkable
class TradingVenue(VenueAdapter, Protocol):
    async def place_order(self, order: OrderRequest) -> OrderResult: ...

    async def get_positions(self) -> list[Position]:
        """Open positions only; a flat instrument is absent from the list."""
        ...

    async def get_margin(self) -> MarginState: ...


def as_trading(venue: VenueAdapter) -> TradingVenue:
    """The only door to order placement: refuses manual venues, whatever methods they have."""
    if venue.execution is not Execution.API:
        raise NotTradableError(f"{venue.name} has execution={venue.execution}; orders are manual")
    if not isinstance(venue, TradingVenue):
        raise NotTradableError(f"{venue.name} declares execution=api but cannot place orders")
    return venue
