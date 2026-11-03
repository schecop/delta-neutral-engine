"""Live check: the real stats endpoint still parses. Run on purpose: `uv run pytest -m live`."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from delta_neutral.instruments import InstrumentRegistry
from delta_neutral.venues.variational import VariationalVenue

pytestmark = pytest.mark.live


def test_real_endpoint_parses() -> None:
    async def run() -> None:
        venue = VariationalVenue(InstrumentRegistry({"XAU-USD": {"variational": "XAU-PERP"}}))
        try:
            assert await venue.list_instruments() == ["XAU-USD"]
            quote = await venue.get_quote("XAU-USD", Decimal("0.1"))
            assert quote.indicative and Decimal(0) < quote.bid <= quote.ask
            funding = await venue.get_funding("XAU-USD")
            assert abs(funding.rate_pct_per_hour) < 1
        finally:
            await venue.aclose()

    asyncio.run(run())
