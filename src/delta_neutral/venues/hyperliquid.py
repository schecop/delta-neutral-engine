"""Hyperliquid: market-data adapter (execution: api; order entry not built yet).

Sources: the official docs at hyperliquid.gitbook.io/hyperliquid-docs (linked from
hyperliquid.xyz), pages "Info endpoint", "Perpetuals", "Tick and lot size", "Rate limits",
"Funding". Everything is a public POST to /info; no keys.

Venue quirks handled here, so nothing above `venues/` sees them:
- Perps live on several "perp dexs": the first (core) dex and builder-deployed ones (HIP-3).
  A HIP-3 coin is named `{dex}:{coin}` (e.g. `xyz:GOLD`) and is read with `"dex": "xyz"`. The
  venue symbol in config is that full coin name, so the dex comes from the symbol itself.
- Quotes walk the L2 book (up to 20 levels a side) to the requested size, so the price is what
  that size would really pay. A book that does not cover the size gives the average over the
  whole book, flagged `indicative`.
- `funding` in the asset context is the current hourly rate as a fraction (funding is paid every
  hour; the docs do not state the unit of this field, but its value matches the hourly interest
  component of 0.00125%). Positive means longs pay shorts.
- Delisted coins (`isDelisted`) count as not listed.
- REST is limited to 1200 weight per minute per IP: `l2Book` weighs 2, `metaAndAssetCtxs` and
  `perpDexs` 20. Contexts and books are cached briefly with single-flight reads, so a collector
  tick costs a few dozen weight. A 429 surfaces as `VenueUnavailableError` (skip this snapshot).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from delta_neutral.instruments import InstrumentRegistry, UnknownInstrumentError
from delta_neutral.models import Execution, FundingRate, Quote
from delta_neutral.venues._sync import LoopLock
from delta_neutral.venues.base import VenueUnavailableError

API_URL = "https://api.hyperliquid.xyz"  # docs: Info endpoint (mainnet)
INFO_PATH = "/info"

Listings = dict[str, tuple[dict[str, Any], dict[str, Any]]]  # coin -> (universe entry, context)


def _dex(coin: str) -> str:
    """The perp dex a coin trades on: "" (core) for "BTC", "xyz" for "xyz:GOLD"."""
    return coin.split(":", 1)[0] if ":" in coin else ""


def _walk(levels: list[dict[str, str]], size: Decimal) -> tuple[Decimal, bool]:
    """Average price to fill `size` against `levels` (best first), and whether they covered it."""
    left, cost = size, Decimal(0)
    for level in levels:
        take = min(left, Decimal(level["sz"]))
        cost += take * Decimal(level["px"])
        left -= take
        if left <= 0:
            return cost / size, True
    filled = size - left
    return cost / filled, False


class HyperliquidVenue:
    execution = Execution.API

    def __init__(
        self,
        registry: InstrumentRegistry,
        *,
        name: str = "hyperliquid",
        client: httpx.AsyncClient | None = None,
        context_seconds: float = 5.0,
        book_seconds: float = 2.0,
        dexs_seconds: float = 3600.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.name = name
        self._registry = registry
        self._client = client or httpx.AsyncClient(base_url=API_URL, timeout=10.0)
        self._owns_client = client is None  # an injected client belongs to the caller
        self._context_seconds = context_seconds
        self._book_seconds = book_seconds
        self._dexs_seconds = dexs_seconds
        self._clock = clock
        self._contexts: dict[str, tuple[float, Listings]] = {}
        self._books: dict[str, tuple[float, dict[str, Any]]] = {}
        self._dexs: tuple[float, list[str]] | None = None
        self._lock = LoopLock()

    async def _info(self, body: dict[str, Any]) -> Any:
        response = await self._client.post(INFO_PATH, json=body)
        if response.status_code == 429:
            raise VenueUnavailableError(f"{self.name}: rate limited ({body['type']})")
        response.raise_for_status()
        return response.json()

    async def _listings(self, dex: str) -> Listings:
        """coin -> (universe entry, asset context) for one dex, live coins only; cached."""
        async with self._lock():  # single-flight: concurrent reads share one request
            cached = self._contexts.get(dex)
            if cached and time.monotonic() - cached[0] < self._context_seconds:
                return cached[1]
            meta, contexts = await self._info({"type": "metaAndAssetCtxs", "dex": dex})
            listings = {
                u["name"]: (u, c)
                for u, c in zip(meta["universe"], contexts, strict=True)
                if not u.get("isDelisted")
            }
            self._contexts[dex] = (time.monotonic(), listings)
            return listings

    async def _all_dexs(self) -> list[str]:
        async with self._lock():
            if self._dexs and time.monotonic() - self._dexs[0] < self._dexs_seconds:
                return self._dexs[1]
            dexs = ["" if d is None else d["name"] for d in await self._info({"type": "perpDexs"})]
            self._dexs = (time.monotonic(), dexs)
            return dexs

    async def _listing(self, instrument: str) -> tuple[str, dict[str, Any]]:
        coin = self._registry.venue_symbol(instrument, self.name)
        listing = (await self._listings(_dex(coin))).get(coin)
        if listing is None:
            raise UnknownInstrumentError(f"{self.name} does not list {coin} ({instrument})")
        return coin, listing[1]

    async def _book(self, coin: str) -> dict[str, Any]:
        async with self._lock():
            cached = self._books.get(coin)
            if cached and time.monotonic() - cached[0] < self._book_seconds:
                return cached[1]
            book: dict[str, Any] = await self._info({"type": "l2Book", "coin": coin})
            self._books[coin] = (time.monotonic(), book)
            return book

    async def list_instruments(self) -> list[str]:
        out = []
        for dex in await self._all_dexs():
            for coin in await self._listings(dex):
                try:
                    out.append(self._registry.canonical(self.name, coin))
                except UnknownInstrumentError:
                    continue  # listed on the venue but not mapped in config: not ours to trade
        return sorted(out)

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        coin, _ = await self._listing(instrument)
        book = await self._book(coin)
        bids, asks = book["levels"]
        if not bids or not asks:
            raise VenueUnavailableError(f"{self.name} has an empty book for {instrument}")
        scale = self._registry.contract_size(instrument, self.name)
        venue_size = size / scale
        bid, bid_covered = _walk(bids, venue_size)
        ask, ask_covered = _walk(asks, venue_size)
        return Quote(
            venue=self.name,
            instrument=instrument,
            bid=bid / scale,  # venue unit -> canonical unit
            ask=ask / scale,
            ts=datetime.fromtimestamp(book["time"] / 1000, UTC),
            indicative=not (bid_covered and ask_covered),
        )

    async def get_funding(self, instrument: str) -> FundingRate:
        _, context = await self._listing(instrument)
        if context.get("funding") is None:
            raise VenueUnavailableError(f"{self.name} publishes no funding for {instrument}")
        return FundingRate(
            venue=self.name,
            instrument=instrument,
            rate_pct_per_hour=Decimal(context["funding"]) * 100,  # hourly fraction -> %/h
            ts=self._clock(),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
