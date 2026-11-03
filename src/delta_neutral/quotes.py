"""Print quotes and funding for every configured instrument, side by side across venues.

    uv run python -m delta_neutral.quotes --config config/local.yaml [--size-usd 1000]

LIVE: calls the venues' public read-only endpoints. Places no orders and needs no keys.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from delta_neutral.config import AppConfig, load_config
from delta_neutral.instruments import UnknownInstrumentError
from delta_neutral.models import Quote
from delta_neutral.price_guard import check_divergence
from delta_neutral.venues.base import VenueAdapter
from delta_neutral.venues.factory import build_venues


async def _row(venue: VenueAdapter, instrument: str, size_usd: Decimal) -> tuple[str, Quote | None]:
    try:
        probe = await venue.get_quote(instrument, Decimal(1))
        quote = await venue.get_quote(instrument, size_usd / probe.mid)
        funding = await venue.get_funding(instrument)
    except UnknownInstrumentError:
        return f"  {venue.name:<12} not listed", None
    except Exception as exc:  # one bad listing must not hide the others
        return f"  {venue.name:<12} {type(exc).__name__}: {exc}", None
    age = (datetime.now(UTC) - quote.ts).total_seconds()
    flag = " indicative" if quote.indicative else ""
    line = (
        f"  {venue.name:<12} bid {quote.bid:>12.4f} ask {quote.ask:>12.4f} "
        f"spread {quote.spread_bps:6.2f} bps  age {age:5.0f}s{flag}  "
        f"funding {funding.rate_pct_per_hour:+.5f} %/h"
    )
    return line, quote


async def main(config: AppConfig, size_usd: Decimal) -> None:
    venues = build_venues(config, skip_missing=True)
    missing = sorted(n for n, v in config.venues.items() if v.enabled and n not in venues)
    if missing:
        print(f"note: no adapter yet for {', '.join(missing)}")
    print(f"size ${size_usd} per quote\n")
    try:
        await _print_all(config, venues, size_usd)
    finally:
        for venue in venues.values():
            await venue.aclose()


async def _print_all(config: AppConfig, venues: dict[str, VenueAdapter], size_usd: Decimal) -> None:
    for instrument in sorted(config.instruments):
        print(instrument)
        quotes = []
        for venue in venues.values():
            line, quote = await _row(venue, instrument, size_usd)
            print(line)
            if quote is not None:
                quotes.append(quote)
        alert = check_divergence(quotes, config.risk.max_price_divergence)
        if alert is not None:
            print(f"  ALERT {alert.message()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--config", type=Path, default=Path("config/local.yaml"))
    parser.add_argument("--size-usd", type=Decimal, default=Decimal(1000))
    args = parser.parse_args()
    asyncio.run(main(load_config(args.config), args.size_usd))
