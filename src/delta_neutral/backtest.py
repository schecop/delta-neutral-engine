"""Backtest: replay every hedged cycle the collected data allows, and compare it with the scorer.

    uv run python -m delta_neutral.backtest [--config config/local.yaml] [--db PATH]
                                            [--holds 5,15,30,60,120,180,240]
                                            [--reward VENUE.FIELD=VALUE ...]

Reads the collector's database only. Places no orders, calls no venue.

For every tick t, every candidate (instrument, long venue, short venue) and every hold H:
- the scorer's *predicted* cost at t, from the quotes and funding of t alone;
- the *realised* cost: each leg sized in USD from its own venue's mid at t (the D4 sizing rule),
  bought at the ask and sold at the bid at t, unwound at the first tick at or after t + H at that
  tick's prices, taker fees on all four fills, and funding accrued tick by tick at the rates seen.

Realised minus predicted is what the scorer misses: spread at exit, the venues' prices drifting
apart, funding changing. Trades whose window has a hole in the data (no tick for over
interval x HOLE_TICKS) are skipped and counted, never filled in.

Reliability, in order: fees are documented; spreads and prices are as reliable as the quotes
(indicative on RFQ venues, top of book only on order books); funding accrues continuously here,
while venues pay at discrete times; the reward is the config's formula, not a measurement, so it
can be varied with `--reward` but never validated by this tool.
"""

from __future__ import annotations

import argparse
import bisect
import itertools
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from delta_neutral.collector import open_db
from delta_neutral.config import VenueConfig, load_config
from delta_neutral.models import FundingRate, Quote
from delta_neutral.scorer import Candidate, Market, load_markets, score

Leg = tuple[Quote, FundingRate]
Key = tuple[str, str, str]  # instrument, long venue, short venue
HOLE_TICKS = 5  # a gap longer than this many intervals inside a hold drops the window


@dataclass(frozen=True)
class Trade:
    instrument: str
    long_venue: str
    short_venue: str
    hold_minutes: int
    entry_at: datetime
    exit_at: datetime
    predicted_usd: Decimal
    realised_usd: Decimal
    funding_usd: Decimal  # realised part of realised_usd
    reward: Decimal
    top_pick: bool  # the scorer ranked this candidate first at entry

    @property
    def key(self) -> Key:
        return self.instrument, self.long_venue, self.short_venue

    @property
    def error_usd(self) -> Decimal:
        return self.realised_usd - self.predicted_usd


@dataclass(frozen=True)
class Result:
    trades: list[Trade]
    skipped: dict[tuple[Key, int], int]  # windows dropped for a hole in the data
    entries: dict[int, int]  # hold -> entry ticks that had a scorer ranking


def _leg_fees(venue: VenueConfig, instrument: str, notional_in: Decimal, out: Decimal) -> Decimal:
    return venue.fees.taker_for(instrument) * (notional_in + out)


def _realised(
    instrument: str,
    long_v: VenueConfig,
    short_v: VenueConfig,
    entry: tuple[Leg, Leg],
    exit_: tuple[Leg, Leg],
    notional: Decimal,
) -> Decimal:
    """Spread, price drift and fees of one cycle; funding is added by the caller."""
    (long_in, _), (short_in, _) = entry
    (long_out, _), (short_out, _) = exit_
    long_qty, short_qty = notional / long_in.mid, notional / short_in.mid  # per-leg USD sizing
    pnl = long_qty * (long_out.bid - long_in.ask) + short_qty * (short_in.bid - short_out.ask)
    fees = _leg_fees(long_v, instrument, long_qty * long_in.ask, long_qty * long_out.bid)
    fees += _leg_fees(short_v, instrument, short_qty * short_in.bid, short_qty * short_out.ask)
    return fees - pnl


def run(
    markets: Sequence[tuple[int, datetime, Market]],
    venues: Mapping[str, VenueConfig],
    notional_usd: Decimal,
    holds: Sequence[int],
    interval_seconds: int,
) -> Result:
    exit_slack = timedelta(seconds=2 * interval_seconds)  # how late the exit tick may be
    # A hole is more than a few missed ticks: one rate-limited tick does not distort 4h of funding
    max_hole = timedelta(seconds=HOLE_TICKS * interval_seconds)
    # Every hold's scorer ranking at every tick: the prediction and the top pick come from here
    rankings: dict[tuple[int, int], dict[Key, Candidate]] = {}
    top: dict[tuple[int, int], Key] = {}
    for i, (_, _, market) in enumerate(markets):
        for hold in holds:
            ranked = score(market, venues, notional_usd, hold)
            if ranked:
                rankings[i, hold] = {(c.instrument, c.long_venue, c.short_venue): c for c in ranked}
                top[i, hold] = (ranked[0].instrument, ranked[0].long_venue, ranked[0].short_venue)

    trades: list[Trade] = []
    skipped: dict[tuple[Key, int], int] = defaultdict(int)
    instruments = sorted({i for _, _, m in markets for i in m})
    for instrument in instruments:
        names = sorted({v for _, _, m in markets for v in m.get(instrument, {}) if v in venues})
        for a, b in itertools.combinations(names, 2):
            # the ticks where both venues quoted this instrument, as one series
            idx = [
                i for i, (_, _, m) in enumerate(markets) if {a, b} <= m.get(instrument, {}).keys()
            ]
            times = [markets[i][1] for i in idx]
            legs = [(markets[i][2][instrument][a], markets[i][2][instrument][b]) for i in idx]
            # cumulative funding of long a / short b in %·h, and of holes in the series
            cum_funding, cum_holes = [Decimal(0)], [0]
            for k in range(len(idx) - 1):
                hours = Decimal((times[k + 1] - times[k]).total_seconds()) / 3600
                rate = legs[k][0][1].rate_pct_per_hour - legs[k][1][1].rate_pct_per_hour
                cum_funding.append(cum_funding[-1] + rate * hours)
                cum_holes.append(cum_holes[-1] + (times[k + 1] - times[k] > max_hole))
            for hold in holds:
                for k, start in enumerate(times):
                    target = start + timedelta(minutes=hold)
                    j = bisect.bisect_left(times, target)
                    if j == len(times):
                        break  # the data ends before this hold does
                    for long, short, sign in ((a, b, 1), (b, a, -1)):
                        key = (instrument, long, short)
                        if times[j] - target > exit_slack or cum_holes[j] != cum_holes[k]:
                            skipped[key, hold] += 1
                            continue
                        entry, exit_ = legs[k], legs[j]
                        if sign < 0:
                            entry, exit_ = (entry[1], entry[0]), (exit_[1], exit_[0])
                        funding = sign * notional_usd * (cum_funding[j] - cum_funding[k]) / 100
                        cost = _realised(
                            instrument, venues[long], venues[short], entry, exit_, notional_usd
                        )
                        predicted = rankings[idx[k], hold][key]
                        trades.append(
                            Trade(
                                instrument=instrument,
                                long_venue=long,
                                short_venue=short,
                                hold_minutes=hold,
                                entry_at=start,
                                exit_at=times[j],
                                predicted_usd=predicted.cost_usd,
                                realised_usd=cost + funding,
                                funding_usd=funding,
                                reward=predicted.reward,
                                top_pick=top[idx[k], hold] == key,
                            )
                        )
    entries = {h: sum(1 for (_, hold) in top if hold == h) for h in holds}
    return Result(trades, dict(skipped), entries)


def _quantile(values: Sequence[Decimal], q: Decimal) -> Decimal:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, start=Decimal(0)) / len(values)


def format_report(result: Result, min_hold_minutes: int, notional_usd: Decimal) -> str:
    groups: dict[int, dict[Key, list[Trade]]] = defaultdict(lambda: defaultdict(list))
    for t in result.trades:
        groups[t.hold_minutes][t.key].append(t)
    lines = [
        f"${notional_usd} per leg; costs in USD per cycle (positive = you pay); "
        "pred = scorer at entry, real = replayed",
    ]
    for hold in sorted(groups):
        note = (
            f"  (below risk.min_hold_minutes {min_hold_minutes})" if hold < min_hold_minutes else ""
        )
        lines += [
            "",
            f"hold {hold} min{note}: {result.entries.get(hold, 0)} entry ticks",
            f"  {'instrument':<10} {'long':<12} {'short':<12} {'n':>5} {'pred':>7} {'real':>7} "
            f"{'p50':>7} {'p95':>7} {'err':>7} {'|err|95':>7} {'fund':>7} {'rew/$':>8} {'top%':>5}"
            f" {'skip':>5}",
        ]
        rows = sorted(groups[hold].items(), key=lambda kv: _mean([t.realised_usd for t in kv[1]]))
        for key, trades in rows:
            real = [t.realised_usd for t in trades]
            cost = sum(real, start=Decimal(0))
            rew = sum((t.reward for t in trades), start=Decimal(0))
            per_usd = f"{rew / cost:.0f}" if cost > 0 else "free"
            entries = result.entries.get(hold, 0)
            top = sum(t.top_pick for t in trades) / entries * 100 if entries else 0
            lines.append(
                f"  {key[0]:<10} {key[1]:<12} {key[2]:<12} {len(trades):>5} "
                f"{_mean([t.predicted_usd for t in trades]):>7.3f} {_mean(real):>7.3f} "
                f"{_quantile(real, Decimal('0.5')):>7.3f} {_quantile(real, Decimal('0.95')):>7.3f} "
                f"{_mean([t.error_usd for t in trades]):>7.3f} "
                f"{_quantile([abs(t.error_usd) for t in trades], Decimal('0.95')):>7.3f} "
                f"{_mean([t.funding_usd for t in trades]):>7.3f} {per_usd:>8} {top:>5.0f}"
                f" {result.skipped.get((key, hold), 0):>5}"
            )
    if not groups:
        lines.append("no complete cycle in the data for these holds")
    return "\n".join(lines)


def _override_rewards(
    venues: dict[str, VenueConfig], overrides: Sequence[str]
) -> dict[str, VenueConfig]:
    """`venue.field=value` sets one reward field, to try a scenario without editing the config."""
    for item in overrides:
        target, value = item.split("=")
        name, field = target.split(".")
        reward = venues[name].reward.model_copy(update={field: Decimal(value)})
        venues[name] = venues[name].model_copy(update={"reward": reward})
    return venues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--config", type=Path, default=Path("config/local.yaml"))
    parser.add_argument("--db", type=Path, default=None, help="default: collector.db_path")
    parser.add_argument(
        "--holds", default="5,15,30,60,120,180,240", help="minutes, comma-separated"
    )
    parser.add_argument("--reward", action="append", default=[], help="VENUE.FIELD=VALUE")
    args = parser.parse_args()
    config = load_config(args.config)
    db = open_db(args.db or config.collector.db_path)
    try:
        markets = load_markets(db)
    finally:
        db.close()
    venues = _override_rewards({n: v for n, v in config.venues.items() if v.enabled}, args.reward)
    holds = [int(h) for h in args.holds.split(",")]
    notional = config.sizing.notional_per_leg_usd
    if markets:
        print(
            f"{len(markets)} ticks from {markets[0][1]:%Y-%m-%d %H:%M} "
            f"to {markets[-1][1]:%Y-%m-%d %H:%M} UTC"
        )
    result = run(markets, venues, notional, holds, config.collector.interval_seconds)
    print(format_report(result, config.risk.min_hold_minutes, notional))


if __name__ == "__main__":
    main()
