"""Adapters wired to recorded venue responses: no network, deterministic.

Fixtures are real responses captured from the venues' public endpoints and trimmed to a few
listings. Re-record them when a venue changes its format.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from delta_neutral.instruments import InstrumentRegistry, VenueSymbol
from delta_neutral.venues.hyperliquid import API_URL, HyperliquidVenue
from delta_neutral.venues.qfex import REST_URL, QfexVenue
from delta_neutral.venues.variational import BASE_URL, VariationalVenue

FIXTURES = Path(__file__).parent.parent / "fixtures"

VARIATIONAL_SYMBOLS: dict[str, str | VenueSymbol] = {
    "BTC-USD": "BTC-PERP",
    "NVDA-USD": "NVDA-PERP",
    "US500-USD": VenueSymbol(symbol="US500-PERP", contract_size=Decimal("0.1")),  # 1/10 index
    "W-USD": "W-PERP",
    "XAU-USD": "XAU-PERP",
    "XAU-SWAP-USD": "XAU-SWAP",
    "COCOA-SWAP-USD": "COCOA-SWAP",
    "MISSING-USD": "NOT-LISTED-PERP",
}


def variational_stats() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES / "variational" / "stats.json").read_text(encoding="utf-8")
    )
    return data


def variational_venue(
    responses: Iterator[httpx.Response] | None = None,
    **kwargs: Any,
) -> VariationalVenue:
    """A Variational adapter that answers from the fixture, or from `responses` in order."""
    stats = variational_stats()

    def handler(request: httpx.Request) -> httpx.Response:
        if responses is not None:
            return next(responses)
        return httpx.Response(200, json=stats)

    registry = InstrumentRegistry({c: {"variational": s} for c, s in VARIATIONAL_SYMBOLS.items()})
    client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return VariationalVenue(registry, client=client, **kwargs)


QFEX_SYMBOLS = {
    "XAU-USD": "GOLD-USD",
    "NVDA-USD": "NVDA-USD",
    "US500-USD": "US500-USD",
    "ABNB-USD": "ABNB-USD",  # INACTIVE in refdata, empty BBO in the recording
    "EUR-USD": "EUR-USD",  # DELISTED in refdata, no BBO and no funding in the recording
    "KIA-KRW": "KIA-KRW",  # INACTIVE in refdata
}
QFEX_QUOTABLE = ("XAU-USD", "NVDA-USD", "US500-USD")


def _fixture(*parts: str) -> Any:
    return json.loads(FIXTURES.joinpath(*parts).read_text(encoding="utf-8"))


def qfex_market_stats() -> dict[str, Any]:
    data: dict[str, Any] = _fixture("qfex", "market_stats.json")
    return data


class RecordedFeed:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot_data = snapshot

    async def snapshot(self) -> dict[str, Any]:
        return self.snapshot_data

    async def aclose(self) -> None:
        pass


def qfex_venue(
    symbols: tuple[str, ...] | None = None,
    snapshot: dict[str, Any] | None = None,
    overrides: Mapping[str, str | VenueSymbol] | None = None,
) -> QfexVenue:
    """A QFEX adapter on recorded refdata and a recorded (or edited) market_stats snapshot."""
    refdata = _fixture("qfex", "refdata.json")
    mapping: dict[str, str | VenueSymbol] = {
        c: s for c, s in QFEX_SYMBOLS.items() if symbols is None or c in symbols
    }
    mapping.update(overrides or {})
    registry = InstrumentRegistry({c: {"qfex": s} for c, s in mapping.items()})
    client = httpx.AsyncClient(
        base_url=REST_URL,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=refdata)),
    )
    return QfexVenue(registry, client=client, feed=RecordedFeed(snapshot or qfex_market_stats()))


HYPERLIQUID_SYMBOLS: dict[str, str | VenueSymbol] = {
    "BTC-USD": "BTC",  # core dex
    "XAU-USD": "xyz:GOLD",  # HIP-3 dex "xyz"
    "US500-USD": "xyz:SP500",
    "NVDA-USD": "xyz:NVDA",  # listed, but no book in the recording
    "MATIC-USD": "MATIC",  # delisted on the core dex
    "URANIUM-USD": "xyz:URANIUM",  # delisted on xyz
    "MISSING-USD": "xyz:NOT-LISTED",
}


def hyperliquid_venue(
    seen: list[dict[str, Any]] | None = None, status: int = 200, **kwargs: Any
) -> HyperliquidVenue:
    """A Hyperliquid adapter answering /info from the recordings; `seen` collects request bodies."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.001)  # network latency: lets concurrent requests overlap
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        if status != 200:
            return httpx.Response(status, json={})
        if body["type"] == "perpDexs":
            return httpx.Response(200, json=_fixture("hyperliquid", "perpDexs.json"))
        if body["type"] == "metaAndAssetCtxs":
            name = body.get("dex") or "core"
            return httpx.Response(
                200, json=_fixture("hyperliquid", f"metaAndAssetCtxs_{name}.json")
            )
        if body["type"] == "l2Book":
            path = FIXTURES / "hyperliquid" / f"l2Book_{body['coin'].replace(':', '_')}.json"
            if not path.exists():  # what the venue returns for a coin with no resting orders
                return httpx.Response(
                    200, json={"coin": body["coin"], "time": 0, "levels": [[], []]}
                )
            return httpx.Response(200, json=json.loads(path.read_text(encoding="utf-8")))
        return httpx.Response(400, json={"error": f"unexpected {body['type']}"})

    registry = InstrumentRegistry({c: {"hyperliquid": s} for c, s in HYPERLIQUID_SYMBOLS.items()})
    client = httpx.AsyncClient(base_url=API_URL, transport=httpx.MockTransport(handler))
    return HyperliquidVenue(registry, client=client, **kwargs)
