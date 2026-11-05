"""Scorer: ranks every hedged cycle the market allows by reward per unit of cost.

    uv run python -m delta_neutral.scorer [--config config/local.yaml] [--tick N]

Reads one collected tick from the collector's database (the latest by default) and prints the
ranking. Places no orders, calls no venue.

A candidate is one instrument held long on one venue and short on another, `notional` USD per
leg, for `hold` hours. Its cost in USD:

    spread   sum over legs of notional * (ask - bid) / mid   half crossed to open, half to close
    fees     sum over legs of 2 * notional * taker fee        one fill to open, one to close
    funding  notional * (long rate - short rate) * hours      longs pay their venue's rate, shorts
                                                              receive theirs; may be negative

and its reward, in the objective's own units, is linear per venue (config `reward`):

    sum over legs of (per_usd_volume * 2 * notional + per_usd_hour * notional * hours)
                     * instrument multiplier

Deterministic and pure: same market in, same ranking out. No LLM, no I/O in `score`. Assumptions,
kept visible: today's funding rate holds for the whole hold; quotes fill at the quoted price (no
slippage past it); the price gap between venues is noise around zero. Quotes that are only
indicative are flagged, never repriced with a guess.
"""

from __future__ import annotations

import argparse
import itertools
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from delta_neutral.collector import open_db
from delta_neutral.config import VenueConfig, load_config
from delta_neutral.models import FundingRate, Quote

# instrument -> venue -> (quote sized at the notional, funding)
Market = Mapping[str, Mapping[str, tuple[Quote, FundingRate]]]


@dataclass(frozen=True)
class Candidate:
    instrument: str
    long_venue: str
    short_venue: str
    notional_usd: Decimal  # per leg
    hold_hours: Decimal
    spread_usd: Decimal
    fees_usd: Decimal
    funding_usd: Decimal  # negative when the hedge earns funding
    reward: Decimal
    flags: tuple[str, ...]

    @property
    def cost_usd(self) -> Decimal:
        return self.spread_usd + self.fees_usd + self.funding_usd

    @property
    def score(self) -> Decimal | None:
        """Reward per USD of cost; None when the cycle costs nothing or pays (no ratio exists)."""
        return self.reward / self.cost_usd if self.cost_usd > 0 else None


def _leg_cost(quote: Quote, notional: Decimal, fee: Decimal) -> tuple[Decimal, Decimal]:
    return notional * (quote.ask - quote.bid) / quote.mid, 2 * notional * fee


def _candidate(
    instrument: str,
    long: tuple[str, Quote, FundingRate],
    short: tuple[str, Quote, FundingRate],
    venues: Mapping[str, VenueConfig],
    notional: Decimal,
    hours: Decimal,
) -> Candidate:
    spread = fees = reward = Decimal(0)
    flags = []
    for name, quote, _ in (long, short):
        venue = venues[name]
        leg_spread, leg_fees = _leg_cost(quote, notional, venue.fees.taker_for(instrument))
        spread, fees = spread + leg_spread, fees + leg_fees
        per_leg = venue.reward.per_usd_volume * 2 * notional
        per_leg += venue.reward.per_usd_hour * notional * hours
        reward += per_leg * venue.reward.multiplier(instrument)
        if quote.indicative:
            flags.append(f"indicative:{name}")
    # rate_pct_per_hour > 0 means longs pay: the long leg pays its rate, the short leg receives
    funding = notional * (long[2].rate_pct_per_hour - short[2].rate_pct_per_hour) / 100 * hours
    if spread + fees + funding <= 0:
        flags.append("no_cost")
    return Candidate(
        instrument, long[0], short[0], notional, hours, spread, fees, funding, reward, tuple(flags)
    )


def _rank(c: Candidate) -> tuple[int, Decimal]:
    # Cycles that cost nothing come first, most reward first; then highest reward per USD
    return (0, -c.reward) if c.score is None else (1, -c.score)


def score(
    market: Market,
    venues: Mapping[str, VenueConfig],
    notional_usd: Decimal,
    hold_minutes: int,
) -> list[Candidate]:
    """Every (instrument, long venue, short venue) with data on both legs, best first."""
    hours = Decimal(hold_minutes) / 60
    out = []
    for instrument, by_venue in market.items():
        legs = [(v, q, f) for v, (q, f) in sorted(by_venue.items()) if v in venues]
        for long, short in itertools.permutations(legs, 2):
            out.append(_candidate(instrument, long, short, venues, notional_usd, hours))
    return sorted(out, key=_rank)


_MARKET_SQL = """
    SELECT q.tick_id, t.started_at, q.venue, q.instrument, q.bid, q.ask, q.quote_ts, q.indicative,
           f.rate_pct_per_hour, f.funding_ts
    FROM quotes q
    JOIN ticks t ON t.id = q.tick_id
    JOIN funding f ON f.tick_id = q.tick_id AND f.venue = q.venue AND f.instrument = q.instrument
"""


def load_markets(
    db: sqlite3.Connection, where: str = "", params: tuple[object, ...] = ()
) -> list[tuple[int, datetime, Market]]:
    """(tick id, tick start, market) for every tick with quotes, oldest first."""
    rows = db.execute(f"{_MARKET_SQL} {where} ORDER BY q.tick_id", params).fetchall()
    out: list[tuple[int, datetime, Market]] = []
    market: dict[str, dict[str, tuple[Quote, FundingRate]]] = {}
    for tick_id, started_at, venue, instrument, bid, ask, quote_ts, indicative, rate, f_ts in rows:
        if not out or out[-1][0] != tick_id:
            market = {}
            out.append((tick_id, datetime.fromisoformat(started_at), market))
        quote = Quote(
            venue=venue,
            instrument=instrument,
            bid=Decimal(bid),
            ask=Decimal(ask),
            ts=datetime.fromisoformat(quote_ts),
            indicative=bool(indicative),
        )
        funding = FundingRate(
            venue=venue,
            instrument=instrument,
            rate_pct_per_hour=Decimal(rate),
            ts=datetime.fromisoformat(f_ts),
        )
        market.setdefault(instrument, {})[venue] = (quote, funding)
    return out


def load_market(db: sqlite3.Connection, tick_id: int | None = None) -> tuple[int, Market]:
    """One tick's quotes and funding from the collector's database (the latest with quotes)."""
    if tick_id is None:
        row = db.execute("SELECT MAX(id) FROM ticks WHERE quotes > 0").fetchone()
        if row[0] is None:
            raise LookupError("no tick with quotes yet")
        tick_id = int(row[0])
    markets = load_markets(db, "WHERE q.tick_id = ?", (tick_id,))
    return tick_id, markets[0][2] if markets else {}


def format_ranking(candidates: list[Candidate]) -> str:
    header = (
        f"{'#':>2} {'instrument':<10} {'long':<12} {'short':<12} {'spread$':>8} {'fees$':>7} "
        f"{'fund$':>7} {'cost$':>7} {'reward':>9} {'reward/$':>9}  flags"
    )
    lines = [header]
    for i, c in enumerate(candidates, 1):
        per_usd = "-" if c.score is None else f"{c.score:.1f}"
        lines.append(
            f"{i:>2} {c.instrument:<10} {c.long_venue:<12} {c.short_venue:<12} "
            f"{c.spread_usd:>8.3f} {c.fees_usd:>7.3f} {c.funding_usd:>7.3f} "
            f"{c.cost_usd:>7.3f} {c.reward:>9.1f} {per_usd:>9}  {' '.join(c.flags)}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--config", type=Path, default=Path("config/local.yaml"))
    parser.add_argument("--tick", type=int, default=None, help="tick id (default: latest)")
    args = parser.parse_args()
    config = load_config(args.config)
    db = open_db(config.collector.db_path)
    try:
        tick_id, market = load_market(db, args.tick)
    finally:
        db.close()
    enabled = {n: v for n, v in config.venues.items() if v.enabled}
    notional, hold = config.sizing.notional_per_leg_usd, config.scoring.hold_minutes
    print(f"tick {tick_id}, ${notional} per leg, hold {hold} min")
    print(format_ranking(score(market, enabled, notional, hold)))


if __name__ == "__main__":
    main()
