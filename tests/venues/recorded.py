"""Adapters wired to recorded venue responses: no network, deterministic.

Fixtures are real responses captured from the venues' public endpoints and trimmed to a few
listings. Re-record them when a venue changes its format.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from delta_neutral.instruments import InstrumentRegistry
from delta_neutral.venues.variational import BASE_URL, VariationalVenue

FIXTURES = Path(__file__).parent.parent / "fixtures"

VARIATIONAL_SYMBOLS = {
    "BTC-USD": "BTC-PERP",
    "NVDA-USD": "NVDA-PERP",
    "US500-USD": "US500-PERP",
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
