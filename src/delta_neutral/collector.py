"""Market-data collector: snapshots quotes and funding for every configured pair, every N seconds.

    uv run python -m delta_neutral.collector run    [--config config/local.yaml] [--ticks N]
    uv run python -m delta_neutral.collector report [--config config/local.yaml]

LIVE (`run`): reads the venues' public endpoints. Places no orders and needs no keys.

Each tick asks every venue for every pair at once, sized at the real notional per leg, and writes
one SQLite transaction: a `ticks` row, the quotes and funding that came back, and an `events` row
for anything that did not (an unavailable venue, a price-guard alert). A failing venue never stops
the others, and the collector never stops on a venue error: a gap in the data is itself data.

Ticks run on a fixed schedule (start + k * interval) so a slow tick does not drift the ones after
it; a tick that overruns its slot skips to the next one and logs an `overrun` event.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from delta_neutral.config import AppConfig, load_config
from delta_neutral.instruments import UnknownInstrumentError
from delta_neutral.models import FundingRate, Quote
from delta_neutral.price_guard import check_divergence
from delta_neutral.venues.base import VenueAdapter, quote_for_notional
from delta_neutral.venues.factory import build_venues

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,      -- UTC ISO-8601
    duration_ms INTEGER NOT NULL,
    quotes INTEGER NOT NULL,       -- rows written this tick
    errors INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    tick_id INTEGER NOT NULL REFERENCES ticks(id),
    venue TEXT NOT NULL,
    instrument TEXT NOT NULL,      -- canonical symbol
    size TEXT NOT NULL,            -- canonical units; Decimals are stored as text, never floats
    bid TEXT NOT NULL,
    ask TEXT NOT NULL,
    quote_ts TEXT NOT NULL,        -- the venue's own timestamp: its age is part of the data
    indicative INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS funding (
    tick_id INTEGER NOT NULL REFERENCES ticks(id),
    venue TEXT NOT NULL,
    instrument TEXT NOT NULL,
    rate_pct_per_hour TEXT NOT NULL,
    funding_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    tick_id INTEGER REFERENCES ticks(id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL, -- unavailable | unknown_instrument | price_divergence | late_tick | overrun
    venue TEXT,
    instrument TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quotes_by_pair ON quotes (instrument, venue, tick_id);
CREATE INDEX IF NOT EXISTS funding_by_pair ON funding (instrument, venue, tick_id);
"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")  # readers (report, supervisor) never block the writer
    db.executescript(SCHEMA)
    return db


Snapshot = tuple[str, str, tuple[Quote, FundingRate] | tuple[str, str]]


async def _snapshot(venue: VenueAdapter, instrument: str, usd: Decimal) -> Snapshot:
    """(venue, instrument, (quote, funding) or (event kind, message)); never raises."""
    try:
        quote = await quote_for_notional(venue, instrument, usd)
        funding = await venue.get_funding(instrument)
    except UnknownInstrumentError as exc:  # a config mapping the venue does not list
        return venue.name, instrument, ("unknown_instrument", str(exc))
    except Exception as exc:  # one venue's failure is recorded, not propagated
        return venue.name, instrument, ("unavailable", f"{type(exc).__name__}: {exc}")
    return venue.name, instrument, (quote, funding)


async def collect_tick(
    db: sqlite3.Connection,
    venues: Mapping[str, VenueAdapter],
    pairs: Mapping[str, list[str]],
    notional_usd: Decimal,
    max_divergence: Decimal,
    clock: Callable[[], datetime] = _utc_now,
    *,
    deadline_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """One snapshot of every pair on every venue that lists it, written as one transaction.

    A snapshot is only worth storing if its quotes are close in time. A tick that runs past
    `deadline_seconds` (a hung request, or the machine sleeping mid-tick) keeps its `ticks` row
    and a `late_tick` event, but none of its quotes (FAILURES.md, 2026-11-04).
    """
    started, t0 = clock(), monotonic()
    jobs = [
        _snapshot(venues[v], instrument, notional_usd)
        for instrument, names in sorted(pairs.items())
        for v in names
        if v in venues
    ]
    try:
        results: list[Snapshot] | None = await asyncio.wait_for(
            asyncio.gather(*jobs), deadline_seconds
        )
    except TimeoutError:
        results = None
    elapsed = monotonic() - t0
    # Checked on the clock too: after a sleep, the wait and the requests time out together
    if results is None or (deadline_seconds is not None and elapsed > deadline_seconds):
        return _late_tick(db, started, elapsed, deadline_seconds or 0.0)

    with db:  # one transaction: a tick is stored whole or not at all
        cur = db.execute(
            "INSERT INTO ticks (started_at, duration_ms, quotes, errors) VALUES (?, 0, 0, 0)",
            (started.isoformat(),),
        )
        tick_id = cur.lastrowid
        at = clock().isoformat()
        quotes_by_instrument: dict[str, list[Quote]] = {}
        errors = 0
        for venue, instrument, outcome in results:
            match outcome:
                case (Quote() as quote, FundingRate() as funding):
                    pass
                case (str() as kind, str() as message):
                    errors += 1
                    db.execute(
                        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                        (tick_id, at, kind, venue, instrument, message),
                    )
                    continue
            quotes_by_instrument.setdefault(instrument, []).append(quote)
            db.execute(
                "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tick_id,
                    venue,
                    instrument,
                    str(notional_usd / quote.mid),
                    str(quote.bid),
                    str(quote.ask),
                    quote.ts.isoformat(),
                    int(quote.indicative),
                ),
            )
            db.execute(
                "INSERT INTO funding VALUES (?, ?, ?, ?, ?)",
                (
                    tick_id,
                    venue,
                    instrument,
                    str(funding.rate_pct_per_hour),
                    funding.ts.isoformat(),
                ),
            )
        for instrument, quotes in quotes_by_instrument.items():
            alert = check_divergence(quotes, max_divergence)
            if alert is not None:
                log.warning("price divergence: %s", alert.message())
                db.execute(
                    "INSERT INTO events VALUES (?, ?, 'price_divergence', NULL, ?, ?)",
                    (tick_id, at, instrument, alert.message()),
                )
        written = sum(len(q) for q in quotes_by_instrument.values())
        duration_ms = int((time.monotonic() - t0) * 1000)
        db.execute(
            "UPDATE ticks SET duration_ms = ?, quotes = ?, errors = ? WHERE id = ?",
            (duration_ms, written, errors, tick_id),
        )
    summary = {"tick": tick_id or 0, "quotes": written, "errors": errors, "ms": duration_ms}
    log.info("tick %s", json.dumps(summary))
    return summary


def _late_tick(
    db: sqlite3.Connection, started: datetime, elapsed: float, deadline: float
) -> dict[str, int]:
    duration_ms = int(elapsed * 1000)
    message = f"tick took {elapsed:.1f}s, over its {deadline:.0f}s deadline; quotes discarded"
    with db:
        cur = db.execute(
            "INSERT INTO ticks (started_at, duration_ms, quotes, errors) VALUES (?, ?, 0, 1)",
            (started.isoformat(), duration_ms),
        )
        db.execute(
            "INSERT INTO events VALUES (?, ?, 'late_tick', NULL, NULL, ?)",
            (cur.lastrowid, _utc_now().isoformat(), message),
        )
    log.warning("late tick: %s", message)
    return {"tick": cur.lastrowid or 0, "quotes": 0, "errors": 1, "ms": duration_ms}


def pairs_from_config(config: AppConfig) -> dict[str, list[str]]:
    """instrument -> the enabled venues that map it."""
    enabled = {n for n, v in config.venues.items() if v.enabled}
    return {i: sorted(enabled & m.keys()) for i, m in config.instruments.items()}


async def run(
    config: AppConfig,
    *,
    max_ticks: int | None = None,
    venues: Mapping[str, VenueAdapter] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    venues = venues if venues is not None else build_venues(config)
    db = open_db(config.collector.db_path)
    interval = config.collector.interval_seconds
    pairs = pairs_from_config(config)
    log.info("collector start %s", json.dumps({"pairs": pairs, "interval_s": interval}))
    start, k = monotonic(), 0
    try:
        while max_ticks is None or k < max_ticks:
            await collect_tick(
                db,
                venues,
                pairs,
                config.sizing.notional_per_leg_usd,
                config.risk.max_price_divergence,
                deadline_seconds=interval,
                monotonic=monotonic,
            )
            k += 1
            behind = monotonic() - (start + k * interval)
            if behind > 0:  # this tick ran past the next slot: skip the missed slots
                skipped = int(behind // interval) + 1
                k += skipped
                with db:
                    db.execute(
                        "INSERT INTO events VALUES (NULL, ?, 'overrun', NULL, NULL, ?)",
                        (_utc_now().isoformat(), f"tick overran its slot; skipped {skipped}"),
                    )
            if max_ticks is None or k < max_ticks:
                await sleep(max(0.0, start + k * interval - monotonic()))
    finally:
        for venue in venues.values():
            await venue.aclose()
        db.close()


def report(db: sqlite3.Connection, interval_seconds: int) -> str:
    """Coverage of the collected data: the exit check of slice 2.4 reads this.

    A gap is the time between consecutive snapshots, and a snapshot exists when a tick with quotes
    *ends*. Measuring between tick starts hid a 13h sleep inside one tick (FAILURES.md, 2026-11-04).
    """
    rows = db.execute("SELECT started_at, duration_ms, quotes FROM ticks ORDER BY id").fetchall()
    if not rows:
        return "no ticks yet"
    times = [datetime.fromisoformat(r[0]) for r in rows]
    ends = [t + timedelta(milliseconds=ms) for t, (_, ms, q) in zip(times, rows, strict=True) if q]
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(ends)]
    if len(ends) < len(rows):  # time after the last snapshot is a gap too
        last_end = times[-1] + timedelta(milliseconds=rows[-1][1])
        gaps.append((last_end - (ends[-1] if ends else times[0])).total_seconds())
    max_gap = max(gaps, default=0.0)
    lines = [
        f"ticks {len(times)} from {times[0]:%Y-%m-%d %H:%M:%S} "
        f"to {times[-1]:%Y-%m-%d %H:%M:%S} UTC",
        f"max gap {max_gap:.0f}s (limit {2 * interval_seconds}s): "
        + ("OK" if max_gap <= 2 * interval_seconds else "GAP"),
        "",
        "coverage per pair (ticks with a quote / all ticks):",
    ]
    for venue, instrument, n in db.execute(
        "SELECT venue, instrument, COUNT(*) FROM quotes GROUP BY venue, instrument ORDER BY 2, 1"
    ):
        lines.append(f"  {instrument:<12} {venue:<12} {n}/{len(times)} ({n / len(times):.1%})")
    events = db.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind ORDER BY 1").fetchall()
    lines += ["", "events: " + (", ".join(f"{k} {n}" for k, n in events) or "none")]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("command", choices=["run", "report"])
    parser.add_argument("--config", type=Path, default=Path("config/local.yaml"))
    parser.add_argument("--ticks", type=int, default=None, help="stop after N ticks (run)")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "report":
        db = open_db(config.collector.db_path)
        print(report(db, config.collector.interval_seconds))
        db.close()
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request drowns the ticks
    try:
        asyncio.run(run(config, max_ticks=args.ticks))
    except KeyboardInterrupt:
        log.info("collector stopped by operator")


if __name__ == "__main__":
    main()
