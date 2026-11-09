"""Hyperliquid adapter on recorded /info responses (2026-11-09): dex routing, book walking,
delistings, funding units and single-flight caching."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from delta_neutral.instruments import UnknownInstrumentError
from delta_neutral.venues.base import VenueUnavailableError
from tests.venues.recorded import _fixture, hyperliquid_venue


def _gold_book() -> list[list[dict[str, Any]]]:
    levels: list[list[dict[str, Any]]] = _fixture("hyperliquid", "l2Book_xyz_GOLD.json")["levels"]
    return levels


def test_lists_live_mapped_coins_across_dexs() -> None:
    venue = hyperliquid_venue()
    # MATIC and URANIUM are delisted, MISSING is not on the venue
    assert asyncio.run(venue.list_instruments()) == ["BTC-USD", "NVDA-USD", "US500-USD", "XAU-USD"]


def test_a_hip3_coin_is_read_from_its_own_dex() -> None:
    seen: list[dict[str, Any]] = []
    venue = hyperliquid_venue(seen)
    asyncio.run(venue.get_funding("XAU-USD"))
    asyncio.run(venue.get_funding("BTC-USD"))
    assert [b.get("dex") for b in seen if b["type"] == "metaAndAssetCtxs"] == ["xyz", ""]


def test_a_small_size_gets_the_top_of_book() -> None:
    bids, asks = _gold_book()
    quote = asyncio.run(hyperliquid_venue().get_quote("XAU-USD", Decimal("0.01")))
    assert (quote.bid, quote.ask) == (Decimal(bids[0]["px"]), Decimal(asks[0]["px"]))
    assert not quote.indicative
    assert quote.ts == datetime.fromtimestamp(
        _fixture("hyperliquid", "l2Book_xyz_GOLD.json")["time"] / 1000, UTC
    )


def test_a_larger_size_walks_the_book() -> None:
    bids, _ = _gold_book()
    size = Decimal(bids[0]["sz"]) + Decimal(1)  # all of the best level, then 1 from the next
    expected = (
        Decimal(bids[0]["sz"]) * Decimal(bids[0]["px"]) + Decimal(1) * Decimal(bids[1]["px"])
    ) / size
    quote = asyncio.run(hyperliquid_venue().get_quote("XAU-USD", size))
    assert quote.bid == expected and quote.bid < Decimal(bids[0]["px"])
    assert not quote.indicative


def test_a_size_beyond_the_book_is_indicative() -> None:
    bids, asks = _gold_book()
    depth = min(sum((Decimal(lv["sz"]) for lv in side), Decimal(0)) for side in (bids, asks))
    quote = asyncio.run(hyperliquid_venue().get_quote("XAU-USD", depth * 2))
    assert quote.indicative and quote.bid <= quote.ask


def test_an_empty_book_is_unavailable_not_unknown() -> None:
    with pytest.raises(VenueUnavailableError):
        asyncio.run(hyperliquid_venue().get_quote("NVDA-USD", Decimal(1)))


@pytest.mark.parametrize("instrument", ["MATIC-USD", "URANIUM-USD", "MISSING-USD"])
def test_delisted_or_missing_coins_are_unknown(instrument: str) -> None:
    with pytest.raises(UnknownInstrumentError):
        asyncio.run(hyperliquid_venue().get_quote(instrument, Decimal(1)))


def test_funding_is_the_hourly_fraction_in_percent() -> None:
    contexts = _fixture("hyperliquid", "metaAndAssetCtxs_xyz.json")
    raw = next(
        c["funding"]
        for u, c in zip(contexts[0]["universe"], contexts[1], strict=True)
        if u["name"] == "xyz:GOLD"
    )
    funding = asyncio.run(hyperliquid_venue().get_funding("XAU-USD"))
    assert funding.rate_pct_per_hour == Decimal(raw) * 100


def test_concurrent_reads_share_one_request_each() -> None:
    seen: list[dict[str, Any]] = []
    venue = hyperliquid_venue(seen)

    async def burst() -> None:
        await asyncio.gather(*(venue.get_quote("XAU-USD", Decimal(1)) for _ in range(6)))

    asyncio.run(burst())
    assert [b["type"] for b in seen] == ["metaAndAssetCtxs", "l2Book"]


def test_rate_limited_is_unavailable() -> None:
    with pytest.raises(VenueUnavailableError):
        asyncio.run(hyperliquid_venue(status=429).get_quote("XAU-USD", Decimal(1)))
