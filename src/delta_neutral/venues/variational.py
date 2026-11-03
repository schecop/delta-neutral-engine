"""Variational Omni: a read-only adapter (execution: manual, ADR-0001).

The venue has no trading API. Its only API is a public stats endpoint that returns every listing
with a mark price, funding and indicative bid/ask in four size tiers. Orders are placed by the
operator in the web app, so this adapter implements `VenueAdapter` and never `TradingVenue`.

Venue quirks handled here, so nothing above `venues/` sees them:
- Quotes are indicative and cached by the venue for up to 600s; `Quote.ts` is the venue's own
  `updated_at`, not our fetch time, so staleness is visible to the scorer.
- Funding `interval_rate` is a fraction per funding interval; we convert to % per hour.
- The rate limit is shared globally across all users, so a 429 can come on the first request.
  We honour `retry_after_seconds` and cache the snapshot briefly to stay under 10 req/10s.
- Swaps (`contract_type: swap`) carry separate long/short funding; only perpetuals are supported.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from delta_neutral.instruments import InstrumentRegistry, UnknownInstrumentError
from delta_neutral.models import Execution, FundingRate, Quote
from delta_neutral.venues.base import VenueUnavailableError

BASE_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io"  # D1 V1, docs api page
STATS_PATH = "/metadata/v2/stats"

# Upper bound in USD notional of each quote tier, smallest first
_TIERS = (
    (Decimal(1_000), "size_1k"),
    (Decimal(100_000), "size_100k"),
    (Decimal(1_000_000), "size_1m"),
)


class UnsupportedContractError(ValueError):
    """A listing whose contract type this adapter does not model yet (swaps)."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class VariationalVenue:
    execution = Execution.MANUAL

    def __init__(
        self,
        registry: InstrumentRegistry,
        *,
        name: str = "variational",
        client: httpx.AsyncClient | None = None,
        cache_seconds: float = 5.0,
        max_retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.name = name
        self._registry = registry
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=10.0)
        self._cache_seconds = cache_seconds
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._cached: tuple[float, dict[str, dict[str, Any]]] | None = None

    async def _listings(self) -> dict[str, dict[str, Any]]:
        """ticker -> listing, fetched at most once per `cache_seconds`."""
        if self._cached and time.monotonic() - self._cached[0] < self._cache_seconds:
            return self._cached[1]
        for attempt in range(self._max_retries + 1):
            response = await self._client.get(STATS_PATH)
            if response.status_code != 429:
                break
            if attempt == self._max_retries:
                raise VenueUnavailableError(f"{self.name}: rate limited after {attempt} retries")
            await self._sleep(float(response.json().get("retry_after_seconds", 1)))
        response.raise_for_status()
        listings = {m["ticker"]: m for m in response.json()["listings"]}
        self._cached = (time.monotonic(), listings)
        return listings

    async def _listing(self, instrument: str) -> dict[str, Any]:
        ticker = self._registry.venue_symbol(instrument, self.name)
        listing = (await self._listings()).get(ticker)
        if listing is None:
            raise UnknownInstrumentError(f"{self.name} does not list {ticker} ({instrument})")
        return listing

    async def list_instruments(self) -> list[str]:
        listed = await self._listings()
        out = []
        for ticker in listed:
            try:
                out.append(self._registry.canonical(self.name, ticker))
            except UnknownInstrumentError:
                continue  # listed on the venue but not mapped in config: not ours to trade
        return sorted(out)

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        listing = await self._listing(instrument)
        quotes = listing.get("quotes")
        if not quotes:
            raise VenueUnavailableError(f"{self.name} has no quotes for {instrument}")
        notional = size * Decimal(listing["mark_price"])
        tier = next((key for limit, key in _TIERS if notional <= limit), None)
        if tier is None:
            raise VenueUnavailableError(f"{self.name}: ${notional:.0f} exceeds the quoted depth")
        return Quote(
            venue=self.name,
            instrument=instrument,
            bid=Decimal(quotes[tier]["bid"]),
            ask=Decimal(quotes[tier]["ask"]),
            ts=datetime.fromisoformat(quotes["updated_at"]),
            indicative=True,
        )

    async def get_funding(self, instrument: str) -> FundingRate:
        listing = await self._listing(instrument)
        if listing["contract_type"] != "perpetual":
            raise UnsupportedContractError(
                f"{self.name}: {instrument} is a {listing['contract_type']}; only perps supported"
            )
        funding = listing["funding"]
        hours = Decimal(funding["interval_seconds"]) / 3600
        return FundingRate(
            venue=self.name,
            instrument=instrument,
            # "next" is what gets paid at next_timestamp: the cost of holding from now
            rate_pct_per_hour=Decimal(funding["next"]["interval_rate"]) * 100 / hours,
            ts=self._clock(),
        )
