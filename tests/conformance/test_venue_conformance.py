"""Conformance suite: the contract in `venues/base.py`, as tests.

Every adapter must pass this before it may trade live. To add one, append a factory to
`ADAPTER_FACTORIES`. Real adapters run against a sandbox or recorded responses here, never
against a live account.

Read-side tests run on every venue. Trading tests run only on `execution: api` venues; a manual
venue instead has to prove the engine cannot place orders through it (ADR-0001).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from uuid import uuid4

import pytest

from delta_neutral.instruments import UnknownInstrumentError
from delta_neutral.models import Execution, OrderRequest, OrderStatus, Side
from delta_neutral.venues.base import NotTradableError, TradingVenue, VenueAdapter, as_trading
from delta_neutral.venues.fake import FakeVenue

_FAKE_MIDS = {"XAU-USD": Decimal(2400), "EUR-USD": Decimal("1.08")}

ADAPTER_FACTORIES: dict[str, Callable[[], VenueAdapter]] = {
    "fake-api": lambda: FakeVenue("fake-api", _FAKE_MIDS),
    "fake-manual": lambda: FakeVenue("fake-manual", _FAKE_MIDS, execution=Execution.MANUAL),
}

SIZE = Decimal("0.5")


def _ids_with(execution: Execution) -> list[str]:
    return sorted(k for k, make in ADAPTER_FACTORIES.items() if make().execution is execution)


@pytest.fixture(params=sorted(ADAPTER_FACTORIES))
def venue(request: pytest.FixtureRequest) -> VenueAdapter:
    factory: Callable[[], VenueAdapter] = ADAPTER_FACTORIES[request.param]
    return factory()


@pytest.fixture(params=_ids_with(Execution.API))
def trading_venue(request: pytest.FixtureRequest) -> TradingVenue:
    return as_trading(ADAPTER_FACTORIES[request.param]())


@pytest.fixture(params=_ids_with(Execution.MANUAL))
def manual_venue(request: pytest.FixtureRequest) -> VenueAdapter:
    return ADAPTER_FACTORIES[request.param]()


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


def test_declares_execution(venue: VenueAdapter) -> None:
    assert isinstance(venue, VenueAdapter)
    assert venue.execution in set(Execution)
    if venue.execution is Execution.API:
        assert isinstance(venue, TradingVenue)


def test_manual_venue_refuses_orders(manual_venue: VenueAdapter) -> None:
    with pytest.raises(NotTradableError):
        as_trading(manual_venue)


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


def test_open_then_close_leaves_flat(trading_venue: TradingVenue) -> None:
    instrument = _instrument(trading_venue)
    opened = asyncio.run(trading_venue.place_order(_order(trading_venue, instrument, Side.LONG)))
    assert opened.status is OrderStatus.FILLED
    positions = asyncio.run(trading_venue.get_positions())
    assert [(p.instrument, p.size) for p in positions] == [(instrument, SIZE)]

    closed = asyncio.run(
        trading_venue.place_order(_order(trading_venue, instrument, Side.SHORT, reduce_only=True))
    )
    assert closed.status is OrderStatus.FILLED
    assert asyncio.run(trading_venue.get_positions()) == []


def test_fill_price_within_quote(trading_venue: TradingVenue) -> None:
    instrument = _instrument(trading_venue)
    quote = asyncio.run(trading_venue.get_quote(instrument, SIZE))
    result = asyncio.run(trading_venue.place_order(_order(trading_venue, instrument, Side.LONG)))
    assert result.avg_price is not None
    assert quote.bid <= result.avg_price <= quote.ask
    assert Decimal(0) < result.filled_size <= SIZE


def test_reduce_only_cannot_open(trading_venue: TradingVenue) -> None:
    instrument = _instrument(trading_venue)
    result = asyncio.run(
        trading_venue.place_order(_order(trading_venue, instrument, Side.SHORT, reduce_only=True))
    )
    assert result.status is OrderStatus.REJECTED
    assert result.reason
    assert asyncio.run(trading_venue.get_positions()) == []


def test_margin_reflects_open_position(trading_venue: TradingVenue) -> None:
    before = asyncio.run(trading_venue.get_margin())
    assert Decimal(0) <= before.free_fraction <= Decimal(1)
    asyncio.run(
        trading_venue.place_order(_order(trading_venue, _instrument(trading_venue), Side.LONG))
    )
    after = asyncio.run(trading_venue.get_margin())
    assert after.used_margin_usd > before.used_margin_usd
