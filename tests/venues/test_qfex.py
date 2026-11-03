from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from delta_neutral.instruments import UnknownInstrumentError, VenueSymbol
from delta_neutral.models import Execution
from delta_neutral.venues.base import NotTradableError, VenueUnavailableError, as_trading
from delta_neutral.venues.qfex import WebSocketFeed
from tests.venues.recorded import qfex_market_stats, qfex_venue


def test_declares_api_but_cannot_trade_yet() -> None:
    venue = qfex_venue()
    assert venue.execution is Execution.API
    with pytest.raises(NotTradableError, match="cannot place orders"):
        as_trading(venue)


def test_lists_only_active_mapped_symbols() -> None:
    venue = qfex_venue()
    assert asyncio.run(venue.list_instruments()) == ["NVDA-USD", "US500-USD", "XAU-USD"]
    for not_active in ("ABNB-USD", "EUR-USD", "KIA-KRW"):  # INACTIVE or DELISTED
        with pytest.raises(UnknownInstrumentError):
            asyncio.run(venue.get_quote(not_active, Decimal(1)))


def test_quote_from_bbo() -> None:
    # Recording: GOLD-USD bid 3928.5 x 39.8386, ask 3928.9 x 0.0763
    venue = qfex_venue()
    small = asyncio.run(venue.get_quote("XAU-USD", Decimal("0.05")))
    assert (small.bid, small.ask) == (Decimal("3928.5"), Decimal("3928.9"))
    assert small.ts == datetime(2026, 11, 3, 19, 50, 50, 37314, tzinfo=UTC)
    assert not small.indicative
    # Larger than the 0.0763 on the ask: the top price would not fill it all
    assert asyncio.run(venue.get_quote("XAU-USD", Decimal("0.1"))).indicative


def test_funding_hourly_fraction_to_pct() -> None:
    # Recording: GOLD-USD funding_rate 0.00004 per hour = 0.004 %/h
    funding = asyncio.run(qfex_venue().get_funding("XAU-USD"))
    assert funding.rate_pct_per_hour == Decimal("0.004")


def test_empty_bbo_and_missing_funding_are_unavailable() -> None:
    # Not seen on an ACTIVE symbol in the recording (only on inactive ones), so the recording is
    # edited: GOLD-USD gets an empty book and loses its funding entry
    snapshot = qfex_market_stats()
    for bbo in snapshot["bbos"]:
        if bbo["symbol"] == "GOLD-USD":
            bbo["bid"] = []
    snapshot["funding_rates"] = [f for f in snapshot["funding_rates"] if f["symbol"] != "GOLD-USD"]
    venue = qfex_venue(snapshot=snapshot)
    with pytest.raises(VenueUnavailableError, match="no BBO"):
        asyncio.run(venue.get_quote("XAU-USD", Decimal(1)))
    with pytest.raises(VenueUnavailableError, match="no funding"):
        asyncio.run(venue.get_funding("XAU-USD"))


# --- the live feed, against a local WebSocket server --------------------------------------


@asynccontextmanager
async def _server(sessions: list[list[dict[str, Any]]]) -> AsyncIterator[str]:
    """Each connection gets the next session's messages, then the server hangs up."""
    queue = iter(sessions)

    async def handler(ws: ServerConnection) -> None:
        subscribe = json.loads(await ws.recv())
        assert subscribe["channels"] == ["market_stats"]
        for message in next(queue, []):
            await ws.send(json.dumps(message))
        await ws.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


def _snapshot(sequence: int) -> dict[str, Any]:
    return {**qfex_market_stats(), "sequence": sequence}


def test_feed_reconnects_after_server_hangs_up() -> None:
    async def run() -> int:
        async with _server([[_snapshot(1)], [_snapshot(2)]]) as url:
            feed = WebSocketFeed(url, max_backoff_seconds=0.05, initial_backoff_seconds=0.01)
            assert (await feed.snapshot())["sequence"] == 1
            sequence = 1
            for _ in range(200):  # the first session ends; the feed must come back on its own
                sequence = (await feed.snapshot())["sequence"]
                if sequence == 2:
                    break
                await asyncio.sleep(0.01)
            await feed.aclose()
            return sequence

    assert asyncio.run(run()) == 2


def test_feed_refuses_a_stale_snapshot() -> None:
    now = [0.0]

    async def run() -> None:
        async with _server([[_snapshot(1)]]) as url:
            feed = WebSocketFeed(url, max_age_seconds=30, clock=lambda: now[0])
            await feed.snapshot()
            now[0] = 31.0  # nothing new for 31s
            with pytest.raises(VenueUnavailableError, match="31s old"):
                await feed.snapshot()
            await feed.aclose()

    asyncio.run(run())


def test_feed_without_data_times_out() -> None:
    async def run() -> None:
        async with _server([[]]) as url:  # connects, never sends a snapshot
            feed = WebSocketFeed(url, first_snapshot_timeout=0.2, initial_backoff_seconds=0.01)
            with pytest.raises(VenueUnavailableError, match="no market data"):
                await feed.snapshot()
            await feed.aclose()

    asyncio.run(run())


def test_aclose_waits_for_the_connection_to_close() -> None:
    # Regression (FAILURES.md 2026-11-03): aclose used to cancel without awaiting, so it returned
    # while the socket was still closing; the ResourceWarning then failed an unrelated test at GC
    async def run() -> None:
        async with serve(_silent_handler, "127.0.0.1", 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            feed = WebSocketFeed(f"ws://127.0.0.1:{port}", first_snapshot_timeout=0.2)
            with pytest.raises(VenueUnavailableError):
                await feed.snapshot()  # connected and subscribed, but no data
            task = feed._task
            assert task is not None and not task.done()
            await feed.aclose()
            assert task.done()  # stopped for real when aclose returns, not "soon"

    asyncio.run(run())


async def _silent_handler(ws: ServerConnection) -> None:
    await ws.wait_closed()


def test_scaled_contract_is_quoted_in_canonical_units() -> None:
    # Pretend GOLD-USD were a tenth of an ounce: prices x10, sizes compared in venue units
    scaled = {"XAU-USD": VenueSymbol(symbol="GOLD-USD", contract_size=Decimal("0.1"))}
    venue = qfex_venue(overrides=scaled)
    quote = asyncio.run(venue.get_quote("XAU-USD", Decimal("0.005")))  # 0.05 venue units
    assert (quote.bid, quote.ask) == (Decimal(39285), Decimal(39289))
    assert not quote.indicative  # 0.05 fits the 0.0763 on the ask
    assert asyncio.run(venue.get_quote("XAU-USD", Decimal("0.01"))).indicative  # 0.1 does not
