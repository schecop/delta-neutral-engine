from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from delta_neutral.instruments import UnknownInstrumentError
from delta_neutral.models import Execution
from delta_neutral.venues.base import NotTradableError, as_trading
from delta_neutral.venues.variational import UnsupportedContractError, VenueUnavailableError
from tests.venues.recorded import variational_stats, variational_venue


def test_is_manual_and_refuses_orders() -> None:
    venue = variational_venue()
    assert venue.execution is Execution.MANUAL
    with pytest.raises(NotTradableError):
        as_trading(venue)


def test_lists_only_mapped_listings() -> None:
    # MISSING-USD is mapped but not listed; the fixture's other tickers are all mapped
    listed = asyncio.run(variational_venue().list_instruments())
    assert "MISSING-USD" not in listed
    assert {"XAU-USD", "NVDA-USD", "US500-USD", "XAU-SWAP-USD"} <= set(listed)


def test_funding_converted_to_pct_per_hour() -> None:
    # XAU-PERP fixture: interval_rate 0.0001269 per 4h -> 0.01269 % / 4h = 0.003172500 %/h
    funding = asyncio.run(variational_venue().get_funding("XAU-USD"))
    assert funding.rate_pct_per_hour == Decimal("0.0031725")


def test_negative_funding_keeps_its_sign() -> None:
    # W-PERP fixture: negative interval_rate, so shorts pay longs
    assert asyncio.run(variational_venue().get_funding("W-USD")).rate_pct_per_hour < 0


def test_swap_funding_is_unsupported() -> None:
    with pytest.raises(UnsupportedContractError):
        asyncio.run(variational_venue().get_funding("XAU-SWAP-USD"))


def test_quote_tier_follows_notional() -> None:
    venue = variational_venue()
    tiers = variational_stats()["listings"]
    xau = next(m for m in tiers if m["ticker"] == "XAU-PERP")["quotes"]
    small = asyncio.run(venue.get_quote("XAU-USD", Decimal("0.1")))  # ~$390
    large = asyncio.run(venue.get_quote("XAU-USD", Decimal(100)))  # ~$390k
    assert small.bid == Decimal(xau["size_1k"]["bid"])
    assert large.bid == Decimal(xau["size_1m"]["bid"])
    with pytest.raises(VenueUnavailableError, match="depth"):
        asyncio.run(venue.get_quote("XAU-USD", Decimal(1000)))  # ~$3.9M


def test_quote_is_indicative_and_stamped_by_the_venue() -> None:
    quote = asyncio.run(variational_venue().get_quote("XAU-USD", Decimal("0.1")))
    assert quote.indicative
    assert quote.ts == datetime(2026, 11, 3, 19, 23, 9, 715683, tzinfo=UTC)


def test_listing_without_quotes() -> None:
    with pytest.raises(VenueUnavailableError, match="no quotes"):
        asyncio.run(variational_venue().get_quote("COCOA-SWAP-USD", Decimal(1)))


def test_mapped_but_not_listed_is_unknown() -> None:
    with pytest.raises(UnknownInstrumentError):
        asyncio.run(variational_venue().get_quote("MISSING-USD", Decimal(1)))


def _rate_limited(retry_after: int) -> httpx.Response:
    return httpx.Response(429, json={"error": "rate_limited", "retry_after_seconds": retry_after})


def test_retries_after_global_rate_limit() -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    responses = iter(
        [_rate_limited(1), _rate_limited(2), httpx.Response(200, json=variational_stats())]
    )
    venue = variational_venue(responses, sleep=sleep)
    assert asyncio.run(venue.list_instruments())
    assert slept == [1.0, 2.0]


def test_gives_up_after_max_retries() -> None:
    async def sleep(seconds: float) -> None:
        pass

    responses = iter([_rate_limited(1)] * 3)
    venue = variational_venue(responses, sleep=sleep, max_retries=2)
    with pytest.raises(VenueUnavailableError, match="rate limited"):
        asyncio.run(venue.list_instruments())


def test_snapshot_is_cached() -> None:
    calls = 0
    stats = variational_stats()

    def count(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=stats)

    venue = variational_venue()
    venue._client = httpx.AsyncClient(
        base_url="https://x.test", transport=httpx.MockTransport(count)
    )
    asyncio.run(venue.get_quote("XAU-USD", Decimal("0.1")))
    asyncio.run(venue.get_funding("XAU-USD"))
    assert calls == 1
