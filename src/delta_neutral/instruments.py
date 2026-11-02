"""Canonical instrument symbols and their venue-native names.

The engine reasons about "XAU-USD"; each venue may call it something else. The registry is the
single place that knows both, and it refuses ambiguous mappings at load time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


class UnknownInstrumentError(LookupError):
    """An instrument (canonical or venue-native) the registry or a venue doesn't know."""


class InstrumentRegistry:
    def __init__(self, mapping: Mapping[str, Mapping[str, str]]) -> None:
        """`mapping` is canonical symbol -> {venue name: venue-native symbol}."""
        self._to_venue: dict[str, dict[str, str]] = {c: dict(v) for c, v in mapping.items()}
        self._to_canonical: dict[tuple[str, str], str] = {}
        for canonical, venues in self._to_venue.items():
            for venue, symbol in venues.items():
                key = (venue, symbol)
                if key in self._to_canonical:
                    raise ValueError(
                        f"{venue}:{symbol} maps to both {self._to_canonical[key]} and {canonical}"
                    )
                self._to_canonical[key] = canonical

    def venue_symbol(self, canonical: str, venue: str) -> str:
        try:
            return self._to_venue[canonical][venue]
        except KeyError:
            raise UnknownInstrumentError(f"{canonical} is not mapped for {venue}") from None

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
