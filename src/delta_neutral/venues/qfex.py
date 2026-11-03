"""QFEX: market data now (slice 2.3), trading in D4 (execution: api).

Market data arrives on a public WebSocket (`market_stats`), which pushes a full snapshot of every
symbol's BBO, mark and funding every few seconds. A background task keeps one connection open,
reconnects with backoff, and stores the latest snapshot; reads never open a connection of their own.
Instruments come from the public REST refdata, cached because listings change rarely.

Venue quirks handled here, so nothing above `venues/` sees them:
- Funding is a decimal fraction per hour, paid hourly and only during market hours.
- The BBO carries one level with its quantity; a quote larger than that level is flagged
  `indicative`, since the top price would not fill the whole size.
- Symbols outside market hours may have an empty BBO or no funding entry: `VenueUnavailableError`.
- A feed that stops delivering is worse than no feed: snapshots older than `max_age_seconds` are
  refused rather than served as if current.

Not built yet: order entry, positions and margin (`wss://trade.qfex.com`, D4). Until then this
adapter declares `execution: api` but is not a `TradingVenue`, so `as_trading` refuses it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx
import websockets

from delta_neutral.instruments import InstrumentRegistry, UnknownInstrumentError
from delta_neutral.models import Execution, FundingRate, Quote
from delta_neutral.venues.base import VenueUnavailableError

log = logging.getLogger(__name__)

REST_URL = "https://api.qfex.com"  # D1 Q1, docs api-reference
MDS_URL = "wss://mds.qfex.com"  # D1 Q11, docs websocket overview
SUBSCRIBE = {"type": "subscribe", "channels": ["market_stats"], "symbols": ["*"]}


class MarketStatsFeed(Protocol):
    async def snapshot(self) -> dict[str, Any]:
        """The latest `market_stats` message; raises `VenueUnavailableError` if there is none."""
        ...

    async def aclose(self) -> None: ...


class WebSocketFeed:
    """Keeps one subscription alive in the background and serves its latest snapshot."""

    def __init__(
        self,
        url: str = MDS_URL,
        *,
        first_snapshot_timeout: float = 15.0,
        max_age_seconds: float = 30.0,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._first_timeout = first_snapshot_timeout
        self._max_age = max_age_seconds
        self._initial_backoff = initial_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._clock = clock
        self._latest: dict[str, Any] | None = None
        self._received_at = 0.0
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Event | None = None

    def _ensure_running(self) -> asyncio.Event:
        # The task belongs to one event loop; a new loop (a new asyncio.run) needs a new task
        loop = asyncio.get_running_loop()
        if self._task is None or self._task.done() or self._task.get_loop() is not loop:
            self._ready = asyncio.Event()
            if self._latest is not None:
                self._ready.set()
            self._task = loop.create_task(self._run(), name="qfex-mds")
        assert self._ready is not None
        return self._ready

    async def _run(self) -> None:
        backoff = self._initial_backoff
        while True:
            try:
                async with websockets.connect(self._url, open_timeout=10) as ws:
                    await ws.send(json.dumps(SUBSCRIBE))
                    log.info("qfex mds connected")
                    async for raw in ws:
                        message = json.loads(raw)
                        if message.get("type") == "market_stats":
                            self._latest, self._received_at = message, self._clock()
                            backoff = (
                                self._initial_backoff
                            )  # only a delivered snapshot proves the link healthy
                            if self._ready is not None:
                                self._ready.set()
            except (OSError, TimeoutError, websockets.WebSocketException) as exc:
                log.warning("qfex mds disconnected: %r; retrying in %.0fs", exc, backoff)
            else:
                log.warning("qfex mds closed by server; retrying in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def snapshot(self) -> dict[str, Any]:
        ready = self._ensure_running()
        try:
            await asyncio.wait_for(ready.wait(), self._first_timeout)
        except TimeoutError:
            raise VenueUnavailableError("qfex: no market data snapshot yet") from None
        age = self._clock() - self._received_at
        if self._latest is None or age > self._max_age:
            raise VenueUnavailableError(f"qfex: market data is {age:.0f}s old")
        return self._latest

    async def aclose(self) -> None:
        """Stop the feed and wait until its connection is closed (FAILURES.md, 2026-11-03)."""
        task, self._task = self._task, None
        if task is None or task.get_loop() is not asyncio.get_running_loop():
            return  # a task from a finished loop is already gone with it
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _by_symbol(snapshot: dict[str, Any], component: str) -> dict[str, dict[str, Any]]:
    return {entry["symbol"]: entry for entry in snapshot.get(component) or []}


class QfexVenue:
    execution = Execution.API

    def __init__(
        self,
        registry: InstrumentRegistry,
        *,
        name: str = "qfex",
        client: httpx.AsyncClient | None = None,
        feed: MarketStatsFeed | None = None,
        refdata_seconds: float = 3600.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.name = name
        self._registry = registry
        # Injected client and feed belong to the caller; the ones made here are ours to close
        self._owns_client, self._owns_feed = client is None, feed is None
        self._client = client or httpx.AsyncClient(base_url=REST_URL, timeout=10.0)
        self._feed = feed or WebSocketFeed()
        self._refdata_seconds = refdata_seconds
        self._clock = clock
        self._active: tuple[float, set[str]] | None = None

    async def _active_symbols(self) -> set[str]:
        if self._active and time.monotonic() - self._active[0] < self._refdata_seconds:
            return self._active[1]
        response = await self._client.get("/refdata")
        response.raise_for_status()
        active = {s["symbol"] for s in response.json()["data"] or [] if s["status"] == "ACTIVE"}
        self._active = (time.monotonic(), active)
        return active

    async def _symbol(self, instrument: str) -> str:
        symbol = self._registry.venue_symbol(instrument, self.name)
        if symbol not in await self._active_symbols():
            raise UnknownInstrumentError(f"{self.name} does not list {symbol} ({instrument})")
        return symbol

    async def list_instruments(self) -> list[str]:
        out = []
        for symbol in await self._active_symbols():
            try:
                out.append(self._registry.canonical(self.name, symbol))
            except UnknownInstrumentError:
                continue  # listed on the venue but not mapped in config: not ours to trade
        return sorted(out)

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        symbol = await self._symbol(instrument)
        bbo = _by_symbol(await self._feed.snapshot(), "bbos").get(symbol)
        if not bbo or not bbo["bid"] or not bbo["ask"]:
            raise VenueUnavailableError(f"{self.name} has no BBO for {instrument}")
        (bid, bid_qty), (ask, ask_qty) = bbo["bid"][0], bbo["ask"][0]
        scale = self._registry.contract_size(instrument, self.name)
        return Quote(
            venue=self.name,
            instrument=instrument,
            bid=Decimal(bid) / scale,  # venue unit -> canonical unit
            ask=Decimal(ask) / scale,
            ts=datetime.fromisoformat(bbo["time"]),
            indicative=size / scale > min(Decimal(bid_qty), Decimal(ask_qty)),
        )

    async def get_funding(self, instrument: str) -> FundingRate:
        symbol = await self._symbol(instrument)
        entry = _by_symbol(await self._feed.snapshot(), "funding_rates").get(symbol)
        if entry is None:
            raise VenueUnavailableError(f"{self.name} publishes no funding for {instrument}")
        return FundingRate(
            venue=self.name,
            instrument=instrument,
            rate_pct_per_hour=Decimal(entry["funding_rate"]) * 100,  # hourly fraction -> %/h
            ts=datetime.fromisoformat(entry["time"]),
        )

    async def aclose(self) -> None:
        if self._owns_feed:
            await self._feed.aclose()
        if self._owns_client:
            await self._client.aclose()
