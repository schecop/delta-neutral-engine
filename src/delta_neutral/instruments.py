"""Canonical instrument symbols and their venue-native names.

The engine reasons about "XAU-USD"; each venue may call it something else, and may size its
contract differently (one venue's unit can be a tenth of the index). The registry is the single
place that knows both, and it refuses ambiguous mappings at load time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class UnknownInstrumentError(LookupError):
    """An instrument (canonical or venue-native) the registry or a venue doesn't know."""


class VenueSymbol(BaseModel):
    """A venue's listing of a canonical instrument.

    `contract_size` is how many canonical units one venue unit is worth: 0.1 means the venue
    quotes a tenth of the instrument, so its price is a tenth of the canonical price.
    `lot_size` (order size step) and `min_size` are in venue units, as the venue's app shows them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    contract_size: Decimal = Field(default=Decimal(1), gt=0)
    lot_size: Decimal | None = Field(default=None, gt=0)
    min_size: Decimal | None = Field(default=None, gt=0)


class InstrumentRegistry:
    def __init__(self, mapping: Mapping[str, Mapping[str, str | VenueSymbol]]) -> None:
        """`mapping` is canonical symbol -> {venue name: venue symbol, or symbol + scale}."""
        self._to_venue: dict[str, dict[str, VenueSymbol]] = {
            c: {v: s if isinstance(s, VenueSymbol) else VenueSymbol(symbol=s) for v, s in m.items()}
            for c, m in mapping.items()
        }
        self._to_canonical: dict[tuple[str, str], str] = {}
        for canonical, venues in self._to_venue.items():
            for venue, listing in venues.items():
                key = (venue, listing.symbol)
                if key in self._to_canonical:
                    raise ValueError(
                        f"{venue}:{listing.symbol} maps to both "
                        f"{self._to_canonical[key]} and {canonical}"
                    )
                self._to_canonical[key] = canonical

    def _listing(self, canonical: str, venue: str) -> VenueSymbol:
        try:
            return self._to_venue[canonical][venue]
        except KeyError:
            raise UnknownInstrumentError(f"{canonical} is not mapped for {venue}") from None

    def venue_symbol(self, canonical: str, venue: str) -> str:
        return self._listing(canonical, venue).symbol

    def contract_size(self, canonical: str, venue: str) -> Decimal:
        """Canonical units per venue unit; adapters use it to keep prices and sizes canonical."""
        return self._listing(canonical, venue).contract_size

    def size_rules(self, canonical: str, venue: str) -> tuple[Decimal | None, Decimal | None]:
        """(order size step, minimum order size) in venue units; None where not configured."""
        listing = self._listing(canonical, venue)
        return listing.lot_size, listing.min_size

    def canonical(self, venue: str, venue_symbol: str) -> str:
        try:
            return self._to_canonical[(venue, venue_symbol)]
        except KeyError:
            raise UnknownInstrumentError(
                f"{venue}:{venue_symbol} has no canonical symbol"
            ) from None

    def common(self, venues: Iterable[str]) -> list[str]:
        """Canonical instruments listed on every one of `venues`: the tradable pairs."""
        wanted = set(venues)
        return sorted(c for c, v in self._to_venue.items() if wanted <= v.keys())
