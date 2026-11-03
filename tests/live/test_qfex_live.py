"""Live check: QFEX refdata and the market_stats feed still parse. Run with `-m live`."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from delta_neutral.instruments import InstrumentRegistry
from delta_neutral.venues.qfex import QfexVenue

pytestmark = pytest.mark.live


def test_real_feed_parses() -> None:
    async def run() -> None:
        venue = QfexVenue(InstrumentRegistry({"XAU-USD": {"qfex": "GOLD-USD"}}))
        try:
            assert await venue.list_instruments() == ["XAU-USD"]
            quote = await venue.get_quote("XAU-USD", Decimal("0.01"))
            assert Decimal(0) < quote.bid <= quote.ask
            funding = await venue.get_funding("XAU-USD")
            assert abs(funding.rate_pct_per_hour) < 1
        finally:
            await venue.aclose()

    asyncio.run(run())
