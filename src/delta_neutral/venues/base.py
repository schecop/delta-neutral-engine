"""The contract every venue implements.

The engine only ever talks to this interface. Adding a venue means writing one class that
satisfies it and passing the conformance suite; the core never changes.

Contract (enforced by tests/conformance/):
- Symbols in and out are canonical; translation to venue-native names happens inside the adapter.
- `get_quote` / `get_funding` raise `UnknownInstrumentError` for instruments the venue doesn't list.
- `place_order` never raises for a business rejection: it returns `OrderStatus.REJECTED` with a
  reason. It raises only for transport failures (timeouts, connection errors), which the executor
  must treat as "state unknown" and reconcile through `get_positions`.
- A `reduce_only` order that would open or increase a position is rejected.
- Funding is normalised to % per hour, positive meaning longs pay shorts.
- All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from delta_neutral.models import (
    FundingRate,
    MarginState,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
)


class VenueAdapter(Protocol):
    name: str

    async def list_instruments(self) -> list[str]:
        """Canonical symbols this venue can trade."""
        ...

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        """Executable (or indicative, flagged) prices for `size` base units."""
        ...

    async def get_funding(self, instrument: str) -> FundingRate: ...

    async def place_order(self, order: OrderRequest) -> OrderResult: ...

    async def get_positions(self) -> list[Position]:
        """Open positions only; a flat instrument is absent from the list."""
        ...

    async def get_margin(self) -> MarginState: ...
