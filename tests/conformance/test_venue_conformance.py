"""Conformance suite: the contract in `venues/base.py`, as tests.

Every adapter must pass this before it may trade live. To add one, append a factory to
`ADAPTER_FACTORIES`. Real adapters run against a sandbox or recorded responses here, never
against a live account.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from uuid import uuid4

import pytest

from delta_neutral.instruments import UnknownInstrumentError
from delta_neutral.models import OrderRequest, OrderStatus, Side
from delta_neutral.venues.base import VenueAdapter
from delta_neutral.venues.fake import FakeVenue

ADAPTER_FACTORIES: dict[str, Callable[[], VenueAdapter]] = {
    "fake": lambda: FakeVenue("fake", {"XAU-USD": Decimal(2400), "EUR-USD": Decimal("1.08")}),
}

SIZE = Decimal("0.5")


@pytest.fixture(params=sorted(ADAPTER_FACTORIES))
def venue(request: pytest.FixtureRequest) -> VenueAdapter:
    factory: Callable[[], VenueAdapter] = ADAPTER_FACTORIES[request.param]
    return factory()


def _instrument(venue: VenueAdapter) -> str:
    return asyncio.run(venue.list_instruments())[0]


def _order(
    venue: VenueAdapter, instrument: str, side: Side, *, reduce_only: bool = False
) -> OrderRequest:
    return OrderRequest(
        venue=venue.name,
        instrument=instrument,
        side=side,
        size=SIZE,
        client_order_id=str(uuid4()),
        reduce_only=reduce_only,
    )


def test_lists_instruments(venue: VenueAdapter) -> None:
    assert asyncio.run(venue.list_instruments())


def test_quote_is_sane(venue: VenueAdapter) -> None:
    instrument = _instrument(venue)
    quote = asyncio.run(venue.get_quote(instrument, SIZE))
    assert quote.venue == venue.name
    assert quote.instrument == instrument
    assert Decimal(0) < quote.bid <= quote.ask
    assert quote.ts.tzinfo is not None


def test_unknown_instrument_raises(venue: VenueAdapter) -> None:
    with pytest.raises(UnknownInstrumentError):
        asyncio.run(venue.get_quote("NOT-A-REAL-PAIR", SIZE))
    with pytest.raises(UnknownInstrumentError):
        asyncio.run(venue.get_funding("NOT-A-REAL-PAIR"))


def test_funding_is_normalised(venue: VenueAdapter) -> None:
    instrument = _instrument(venue)
    funding = asyncio.run(venue.get_funding(instrument))
    assert funding.venue == venue.name
    assert funding.instrument == instrument
    assert abs(funding.rate_pct_per_hour) < 1  # % per hour: anything near 1 means a unit bug


def test_open_then_close_leaves_flat(venue: VenueAdapter) -> None:
    instrument = _instrument(venue)
    opened = asyncio.run(venue.place_order(_order(venue, instrument, Side.LONG)))
    assert opened.status is OrderStatus.FILLED
    positions = asyncio.run(venue.get_positions())
    assert [(p.instrument, p.size) for p in positions] == [(instrument, SIZE)]

    closed = asyncio.run(venue.place_order(_order(venue, instrument, Side.SHORT, reduce_only=True)))
    assert closed.status is OrderStatus.FILLED
    assert asyncio.run(venue.get_positions()) == []


def test_fill_price_within_quote(venue: VenueAdapter) -> None:
    instrument = _instrument(venue)
    quote = asyncio.run(venue.get_quote(instrument, SIZE))
    result = asyncio.run(venue.place_order(_order(venue, instrument, Side.LONG)))
    assert result.avg_price is not None
    assert quote.bid <= result.avg_price <= quote.ask
    assert Decimal(0) < result.filled_size <= SIZE


def test_reduce_only_cannot_open(venue: VenueAdapter) -> None:
    instrument = _instrument(venue)
    result = asyncio.run(venue.place_order(_order(venue, instrument, Side.SHORT, reduce_only=True)))
    assert result.status is OrderStatus.REJECTED
    assert result.reason
    assert asyncio.run(venue.get_positions()) == []


def test_margin_reflects_open_position(venue: VenueAdapter) -> None:
    before = asyncio.run(venue.get_margin())
    assert Decimal(0) <= before.free_fraction <= Decimal(1)
    asyncio.run(venue.place_order(_order(venue, _instrument(venue), Side.LONG)))
    after = asyncio.run(venue.get_margin())
    assert after.used_margin_usd > before.used_margin_usd
