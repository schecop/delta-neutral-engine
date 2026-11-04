"""Adapters that each break one rule of the venue contract, to prove the suite catches it.

Not adapters to use: `test_suite_catches_broken_adapters.py` runs the conformance suite with
these added and checks that every one fails the test guarding the rule it breaks. A suite that
passes a broken adapter would let it trade live.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from delta_neutral.models import Execution, FundingRate, OrderRequest, OrderResult, Quote
from delta_neutral.venues.base import VenueAdapter
from delta_neutral.venues.fake import FakeVenue

_MIDS = {"XAU-USD": Decimal(2400)}


class FundingAsFraction(FakeVenue):
    """Forgets the % conversion and the per-hour scaling: 8h fraction reported as %/h."""

    async def get_funding(self, instrument: str) -> FundingRate:
        rate = await super().get_funding(instrument)
        return rate.model_copy(update={"rate_pct_per_hour": Decimal(5)})


class NativeSymbolLeaks(FakeVenue):
    """Returns quotes labelled with the venue's own symbol instead of the canonical one."""

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        quote = await super().get_quote(instrument, size)
        return quote.model_copy(update={"instrument": "GOLD-PERP"})


class NaiveTimestamps(FakeVenue):
    """Stamps quotes in local time without a timezone."""

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        quote = await super().get_quote(instrument, size)
        return quote.model_copy(update={"ts": datetime(2026, 11, 4, 12, 0)})


class LeaksKeyError(FakeVenue):
    """Lets the venue's own lookup error escape instead of `UnknownInstrumentError`."""

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        if instrument not in _MIDS:
            raise KeyError(instrument)
        return await super().get_quote(instrument, size)


class ReadOnlyClaimsApi:
    """Declares `execution: api` but has no order entry: the engine would try to trade it."""

    execution = Execution.API

    def __init__(self, name: str) -> None:
        self.name = name
        self._inner = FakeVenue(name, _MIDS)

    async def list_instruments(self) -> list[str]:
        return await self._inner.list_instruments()

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        return await self._inner.get_quote(instrument, size)

    async def get_funding(self, instrument: str) -> FundingRate:
        return await self._inner.get_funding(instrument)

    async def aclose(self) -> None:
        pass


class ReduceOnlyOpens(FakeVenue):
    """Ignores `reduce_only`, so a close sent twice opens a position the other way."""

    async def place_order(self, order: OrderRequest) -> OrderResult:
        return await super().place_order(order.model_copy(update={"reduce_only": False}))


class FillsOutsideQuote(FakeVenue):
    """Reports a fill price far from the quote it was given (a unit or scale bug)."""

    async def place_order(self, order: OrderRequest) -> OrderResult:
        result = await super().place_order(order)
        if result.avg_price is None:
            return result
        return result.model_copy(update={"avg_price": result.avg_price * 10})


# id -> (factory, the conformance test that must fail for it)
BROKEN: dict[str, tuple[Callable[[], VenueAdapter], str]] = {
    "broken-funding-units": (
        lambda: FundingAsFraction("broken-funding-units", _MIDS),
        "test_funding_is_normalised",
    ),
    "broken-native-symbol": (
        lambda: NativeSymbolLeaks("broken-native-symbol", _MIDS),
        "test_quote_is_sane",
    ),
    "broken-naive-ts": (lambda: NaiveTimestamps("broken-naive-ts", _MIDS), "test_quote_is_sane"),
    "broken-key-error": (
        lambda: LeaksKeyError("broken-key-error", _MIDS),
        "test_unknown_instrument_raises",
    ),
    "broken-read-only-api": (
        lambda: ReadOnlyClaimsApi("broken-read-only-api"),
        "test_api_venue_can_trade",
    ),
    "broken-reduce-only": (
        lambda: ReduceOnlyOpens("broken-reduce-only", _MIDS),
        "test_reduce_only_cannot_open",
    ),
    "broken-fill-price": (
        lambda: FillsOutsideQuote("broken-fill-price", _MIDS),
        "test_fill_price_within_quote",
    ),
}
FACTORIES = {key: factory for key, (factory, _) in BROKEN.items()}
