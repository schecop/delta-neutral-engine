"""Executor: opens and closes hedged cycles, one leg per venue, in dry-run.

    uv run python -m delta_neutral.executor plan [--instrument XAU-USD]  # score, open the best
    uv run python -m delta_neutral.executor fill CYCLE --price P [--size S]  # confirm manual leg
    uv run python -m delta_neutral.executor close CYCLE                  # close before hold ends
    uv run python -m delta_neutral.executor due                          # close cycles past hold
    uv run python -m delta_neutral.executor abort CYCLE --reason TEXT    # cancel before any fill
    uv run python -m delta_neutral.executor status

DRY-RUN ONLY: reads live quotes, places no orders. Manual legs are done by the operator in the
venue's app and confirmed here with the real fill price. API legs are filled on paper at the
live quote (buy at the ask, sell at the bid) plus the configured taker fee. `mode: live` is
refused: live order entry lands in D5, behind Pablo's explicit go-ahead.

A cycle is a state machine stored in SQLite, so it survives a restart at any step:

    planned -> opening -> open -> closing -> closed        (aborted: cancelled before any fill)

In each phase the manual legs go first, because they are the slow and uncertain ones; API legs
are sent only once every manual leg of the phase is confirmed, which keeps the unhedged time to
the API leg's latency. Which legs are manual comes from each venue's `execution` capability,
never from its name (ADR-0001). Each leg is sized in USD from its own venue's quote.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from delta_neutral.collector import pairs_from_config, snapshot_pair
from delta_neutral.config import AppConfig, load_config
from delta_neutral.instruments import InstrumentRegistry
from delta_neutral.models import Execution, FundingRate, Quote, Side
from delta_neutral.risk import RiskMonitor, paper_positions, weekend_blocks
from delta_neutral.scorer import Candidate, Market, score
from delta_neutral.venues.base import VenueAdapter, quote_for_notional
from delta_neutral.venues.factory import build_venues

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY,
    instrument TEXT NOT NULL,
    long_venue TEXT NOT NULL,
    short_venue TEXT NOT NULL,
    notional_usd TEXT NOT NULL,     -- per leg
    hold_minutes INTEGER NOT NULL,
    predicted_cost_usd TEXT NOT NULL,
    funding_pct_per_hour TEXT NOT NULL,  -- long rate minus short rate at planning
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    opened_at TEXT,
    close_due_at TEXT,
    closed_at TEXT,
    summary TEXT                    -- JSON, written when the cycle ends
);
CREATE TABLE IF NOT EXISTS legs (
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    phase TEXT NOT NULL,            -- open | close
    venue TEXT NOT NULL,
    side TEXT NOT NULL,             -- long = buy, short = sell
    execution TEXT NOT NULL,        -- api | manual
    size TEXT NOT NULL,             -- canonical units
    requested_at TEXT NOT NULL,
    price TEXT,                     -- canonical units; NULL until filled
    fee_usd TEXT,
    filled_at TEXT,
    PRIMARY KEY (cycle_id, phase, venue)
);
CREATE TABLE IF NOT EXISTS cycle_events (
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL              -- JSON
);
"""


class CycleState(StrEnum):
    PLANNED = "planned"
    OPENING = "opening"  # waiting for the manual open leg(s)
    OPEN = "open"
    CLOSING = "closing"  # waiting for the manual close leg(s)
    CLOSED = "closed"
    ABORTED = "aborted"


class CycleError(RuntimeError):
    """An operation that the cycle's current state does not allow."""


@dataclass(frozen=True)
class ManualInstruction:
    """What the operator must do in a venue's app, in that venue's own symbol and units."""

    cycle_id: int
    phase: str
    venue: str
    symbol: str
    action: str  # BUY | SELL
    venue_size: Decimal
    venue_price: Decimal  # indicative, from the latest quote
    notional_usd: Decimal

    def message(self) -> str:
        return (
            f"cycle {self.cycle_id} {self.phase}: {self.action} {self.venue_size:.6f} "
            f"{self.symbol} on {self.venue} "
            f"(~${self.notional_usd:.0f}, quote ~{self.venue_price}). "
            f"Then confirm: fill {self.cycle_id} --price <venue fill price>"
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


# (now, hold minutes) -> reasons a new cycle may not open; empty means it may
Gate = Callable[[datetime, int], Awaitable[list[str]]]


def open_engine_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")  # the supervisor reads while the executor writes
    db.executescript(SCHEMA)
    return db


def _buys(phase: str, side: str) -> bool:
    """A long buys to open and sells to close; a short does the opposite."""
    return (side == Side.LONG) == (phase == "open")


class Executor:
    def __init__(
        self,
        config: AppConfig,
        venues: Mapping[str, VenueAdapter],
        db: sqlite3.Connection,
        clock: Callable[[], datetime] = _utc_now,
        gate: Gate | None = None,
    ) -> None:
        """`gate` adds the risk monitor's checks; the weekend rule applies even without one."""
        if config.mode != "dry_run":
            raise CycleError("the executor runs in dry-run only; live order entry lands in D5")
        self._config = config
        self._venues = venues
        self._db = db
        self._clock = clock
        self._registry = InstrumentRegistry(config.instruments)
        self.gate = gate

    # -- operations ---------------------------------------------------------------------------

    async def plan(self, candidate: Candidate) -> list[ManualInstruction]:
        """Create a cycle for a scored candidate and start opening it."""
        now = self._clock()
        hold = int(candidate.hold_hours * 60)
        blocks = weekend_blocks(now, hold, self._config.risk.weekend)
        if self.gate is not None:
            blocks += await self.gate(now, hold)
        if blocks:
            raise CycleError("not opening: " + "; ".join(blocks))
        notional = candidate.notional_usd
        funding = await self._funding_spread(candidate)
        with self._db:
            cur = self._db.execute(
                "INSERT INTO cycles (instrument, long_venue, short_venue, notional_usd, "
                "hold_minutes, predicted_cost_usd, funding_pct_per_hour, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.instrument,
                    candidate.long_venue,
                    candidate.short_venue,
                    str(notional),
                    hold,
                    str(candidate.cost_usd),
                    str(funding),
                    CycleState.PLANNED,
                    now.isoformat(),
                ),
            )
            cycle_id = cur.lastrowid or 0
            for venue, side in (
                (candidate.long_venue, Side.LONG),
                (candidate.short_venue, Side.SHORT),
            ):
                # each leg sized in USD from its own venue's quote (D4 rule)
                quote = await quote_for_notional(
                    self._venues[venue], candidate.instrument, notional
                )
                self._insert_leg(cycle_id, "open", venue, side, notional / quote.mid, now)
            self._event(cycle_id, "planned", {"candidate": _candidate_json(candidate)})
            self._set_state(cycle_id, CycleState.OPENING)
        return await self._advance(cycle_id)

    async def confirm_manual(
        self, cycle_id: int, venue_price: Decimal, venue_size: Decimal | None = None
    ) -> list[ManualInstruction]:
        """The operator filled a manual leg: record it in canonical units and move on."""
        cycle = self._cycle(cycle_id)
        phase = self._phase(cycle)
        pending = [leg for leg in self._legs(cycle_id, phase) if leg["price"] is None]
        manual = [leg for leg in pending if leg["execution"] == Execution.MANUAL]
        if not manual:
            raise CycleError(f"cycle {cycle_id} is {cycle['state']}: no manual leg is waiting")
        leg = manual[0]
        scale = self._registry.contract_size(cycle["instrument"], leg["venue"])
        size = Decimal(leg["size"]) if venue_size is None else venue_size * scale
        price = venue_price / scale  # venue unit -> canonical unit
        fee = self._fee(cycle["instrument"], leg["venue"]) * size * price
        with self._db:
            self._fill(cycle_id, phase, leg["venue"], price, fee, size)
        return await self._advance(cycle_id)

    async def request_close(self, cycle_id: int) -> list[ManualInstruction]:
        cycle = self._cycle(cycle_id)
        if cycle["state"] != CycleState.OPEN:
            raise CycleError(f"cycle {cycle_id} is {cycle['state']}; only an open cycle can close")
        now = self._clock()
        with self._db:
            for leg in self._legs(cycle_id, "open"):
                self._insert_leg(
                    cycle_id, "close", leg["venue"], leg["side"], Decimal(leg["size"]), now
                )
            self._set_state(cycle_id, CycleState.CLOSING)
        return await self._advance(cycle_id)

    async def close_due(self) -> list[ManualInstruction]:
        """Start closing every open cycle whose hold has ended."""
        now = self._clock().isoformat()
        due = self._db.execute(
            "SELECT id FROM cycles WHERE state = ? AND close_due_at <= ? ORDER BY id",
            (CycleState.OPEN, now),
        ).fetchall()
        out: list[ManualInstruction] = []
        for row in due:
            out += await self.request_close(row["id"])
        return out

    async def resume(self, cycle_id: int) -> list[ManualInstruction]:
        """Retry whatever the current phase is waiting for (the risk monitor calls this)."""
        self._phase(self._cycle(cycle_id))
        return await self._advance(cycle_id)

    def abort(self, cycle_id: int, reason: str) -> None:
        """Cancel a cycle before any leg filled. After a fill, the only way out is to close."""
        cycle = self._cycle(cycle_id)
        filled = self._db.execute(
            "SELECT COUNT(*) FROM legs WHERE cycle_id = ? AND price IS NOT NULL", (cycle_id,)
        ).fetchone()[0]
        if cycle["state"] not in (CycleState.PLANNED, CycleState.OPENING) or filled:
            raise CycleError(f"cycle {cycle_id} has fills; close it instead of aborting")
        with self._db:
            self._set_state(cycle_id, CycleState.ABORTED, reason=reason)

    # -- the state machine ----------------------------------------------------------------------

    async def _advance(self, cycle_id: int) -> list[ManualInstruction]:
        """Drive the current phase as far as it can go without the operator."""
        cycle = self._cycle(cycle_id)
        phase = self._phase(cycle)
        pending = [leg for leg in self._legs(cycle_id, phase) if leg["price"] is None]
        manual = [leg for leg in pending if leg["execution"] == Execution.MANUAL]
        if manual:  # manual legs first: wait for the operator
            return [await self._instruction(cycle, phase, leg) for leg in manual]
        for leg in pending:  # every manual leg is in: fill the API legs on paper
            await self._paper_fill(cycle, phase, leg)
        with self._db:
            if phase == "open":
                opened = self._last_fill(cycle_id, "open")
                due = opened + timedelta(minutes=cycle["hold_minutes"])
                self._db.execute(
                    "UPDATE cycles SET opened_at = ?, close_due_at = ? WHERE id = ?",
                    (opened.isoformat(), due.isoformat(), cycle_id),
                )
                self._set_state(cycle_id, CycleState.OPEN, **self._timing(cycle_id, "open"))
            else:
                summary = self._summary(self._cycle(cycle_id))
                self._db.execute(
                    "UPDATE cycles SET closed_at = ?, summary = ? WHERE id = ?",
                    (self._last_fill(cycle_id, "close").isoformat(), json.dumps(summary), cycle_id),
                )
                self._set_state(cycle_id, CycleState.CLOSED, **summary)
        return []

    async def _paper_fill(self, cycle: sqlite3.Row, phase: str, leg: sqlite3.Row) -> None:
        size = Decimal(leg["size"])
        quote = await self._venues[leg["venue"]].get_quote(cycle["instrument"], size)
        price = quote.ask if _buys(phase, leg["side"]) else quote.bid
        fee = self._fee(cycle["instrument"], leg["venue"]) * size * price
        with self._db:
            self._fill(cycle["id"], phase, leg["venue"], price, fee, size)

    def _summary(self, cycle: sqlite3.Row) -> dict[str, Any]:
        """Realised cost from the fills, next to the scorer's prediction (as in the backtest)."""
        legs = {
            (r["phase"], r["side"]): r
            for r in self._db.execute("SELECT * FROM legs WHERE cycle_id = ?", (cycle["id"],))
        }
        long_in, long_out = legs["open", Side.LONG], legs["close", Side.LONG]
        short_in, short_out = legs["open", Side.SHORT], legs["close", Side.SHORT]
        pnl = Decimal(long_in["size"]) * (Decimal(long_out["price"]) - Decimal(long_in["price"]))
        pnl += Decimal(short_in["size"]) * (
            Decimal(short_in["price"]) - Decimal(short_out["price"])
        )
        fees = sum((Decimal(r["fee_usd"]) for r in legs.values()), start=Decimal(0))
        held = self._last_fill(cycle["id"], "close") - datetime.fromisoformat(cycle["opened_at"])
        hours = Decimal(held.total_seconds()) / 3600
        # estimate: the planning-time rates over the hold; venues pay at discrete times
        funding = (
            Decimal(cycle["notional_usd"]) * Decimal(cycle["funding_pct_per_hour"]) / 100 * hours
        )
        realised = fees - pnl + funding
        return {
            "predicted_cost_usd": cycle["predicted_cost_usd"],
            "realised_cost_usd": str(realised),
            "error_usd": str(realised - Decimal(cycle["predicted_cost_usd"])),
            "price_pnl_usd": str(pnl),
            "fees_usd": str(fees),
            "funding_usd_estimated": str(funding),
            "held_minutes": round(held.total_seconds() / 60, 1),
            **{f"close_{k}": v for k, v in self._timing(cycle["id"], "close").items()},
        }

    def _timing(self, cycle_id: int, phase: str) -> dict[str, Any]:
        """Leg skew: time between the first and last fill of a phase, i.e. the unhedged time."""
        fills = [
            datetime.fromisoformat(r["filled_at"])
            for r in self._db.execute(
                "SELECT filled_at FROM legs WHERE cycle_id = ? AND phase = ?", (cycle_id, phase)
            )
        ]
        skew = (max(fills) - min(fills)).total_seconds()
        return {"leg_skew_s": skew, "unhedged_s": skew}

    # -- helpers ------------------------------------------------------------------------------

    async def _funding_spread(self, c: Candidate) -> Decimal:
        long, short = await asyncio.gather(
            self._venues[c.long_venue].get_funding(c.instrument),
            self._venues[c.short_venue].get_funding(c.instrument),
        )
        return long.rate_pct_per_hour - short.rate_pct_per_hour

    async def _instruction(
        self, cycle: sqlite3.Row, phase: str, leg: sqlite3.Row
    ) -> ManualInstruction:
        instrument, venue = cycle["instrument"], leg["venue"]
        size = Decimal(leg["size"])
        quote = await self._venues[venue].get_quote(instrument, size)
        scale = self._registry.contract_size(instrument, venue)
        buys = _buys(phase, leg["side"])
        instruction = ManualInstruction(
            cycle_id=cycle["id"],
            phase=phase,
            venue=venue,
            symbol=self._registry.venue_symbol(instrument, venue),
            action="BUY" if buys else "SELL",
            venue_size=size / scale,  # canonical units -> venue units
            venue_price=(quote.ask if buys else quote.bid) * scale,
            notional_usd=Decimal(cycle["notional_usd"]),
        )
        with self._db:
            self._event(cycle["id"], "manual_instruction", {"message": instruction.message()})
        return instruction

    def _fee(self, instrument: str, venue: str) -> Decimal:
        return self._config.venues[venue].fees.taker_for(instrument)

    def _insert_leg(
        self, cycle_id: int, phase: str, venue: str, side: str, size: Decimal, at: datetime
    ) -> None:
        self._db.execute(
            "INSERT INTO legs (cycle_id, phase, venue, side, execution, size, requested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cycle_id,
                phase,
                venue,
                side,
                self._venues[venue].execution,
                str(size),
                at.isoformat(),
            ),
        )

    def _fill(
        self, cycle_id: int, phase: str, venue: str, price: Decimal, fee: Decimal, size: Decimal
    ) -> None:
        at = self._clock().isoformat()
        self._db.execute(
            "UPDATE legs SET price = ?, fee_usd = ?, size = ?, filled_at = ? "
            "WHERE cycle_id = ? AND phase = ? AND venue = ?",
            (str(price), str(fee), str(size), at, cycle_id, phase, venue),
        )
        self._event(
            cycle_id,
            "leg_filled",
            {
                "phase": phase,
                "venue": venue,
                "price": str(price),
                "size": str(size),
                "fee_usd": str(fee),
            },
        )

    def _set_state(self, cycle_id: int, state: CycleState, **data: Any) -> None:
        self._db.execute("UPDATE cycles SET state = ? WHERE id = ?", (state, cycle_id))
        self._event(cycle_id, state, data)

    def _event(self, cycle_id: int, kind: str, data: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO cycle_events VALUES (?, ?, ?, ?)",
            (cycle_id, self._clock().isoformat(), kind, json.dumps(data)),
        )
        log.info("cycle %s", json.dumps({"cycle": cycle_id, "event": kind, **data}))

    def _cycle(self, cycle_id: int) -> sqlite3.Row:
        row: sqlite3.Row | None = self._db.execute(
            "SELECT * FROM cycles WHERE id = ?", (cycle_id,)
        ).fetchone()
        if row is None:
            raise CycleError(f"no cycle {cycle_id}")
        return row

    def _phase(self, cycle: sqlite3.Row) -> str:
        if cycle["state"] in (CycleState.PLANNED, CycleState.OPENING):
            return "open"
        if cycle["state"] == CycleState.CLOSING:
            return "close"
        raise CycleError(f"cycle {cycle['id']} is {cycle['state']}: nothing to fill")

    def _legs(self, cycle_id: int, phase: str) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM legs WHERE cycle_id = ? AND phase = ? ORDER BY venue", (cycle_id, phase)
        ).fetchall()

    def _last_fill(self, cycle_id: int, phase: str) -> datetime:
        (at,) = self._db.execute(
            "SELECT MAX(filled_at) FROM legs WHERE cycle_id = ? AND phase = ?", (cycle_id, phase)
        ).fetchone()
        return datetime.fromisoformat(at)


def _candidate_json(c: Candidate) -> dict[str, str]:
    return {
        "instrument": c.instrument,
        "long": c.long_venue,
        "short": c.short_venue,
        "cost_usd": str(c.cost_usd),
        "flags": " ".join(c.flags),
    }


async def fetch_market(
    venues: Mapping[str, VenueAdapter], pairs: Mapping[str, list[str]], notional: Decimal
) -> Market:
    """A live market snapshot for the scorer; venues that fail are left out, as in the collector."""
    jobs = [snapshot_pair(venues[v], i, notional) for i, names in pairs.items() for v in names]
    market: dict[str, dict[str, tuple[Quote, FundingRate]]] = {}
    for venue, instrument, outcome in await asyncio.gather(*jobs):
        if isinstance(outcome[0], Quote) and isinstance(outcome[1], FundingRate):
            market.setdefault(instrument, {})[venue] = (outcome[0], outcome[1])
    return market


def _status(db: sqlite3.Connection) -> str:
    rows = db.execute("SELECT * FROM cycles ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        return "no cycles yet"
    lines = []
    for r in rows:
        extra = ""
        if r["summary"]:
            s = json.loads(r["summary"])
            predicted, realised = Decimal(s["predicted_cost_usd"]), Decimal(s["realised_cost_usd"])
            extra = f" predicted ${predicted:.3f} realised ${realised:.3f}"
        elif r["close_due_at"]:
            extra = f" close due {r['close_due_at'][:19]}"
        lines.append(
            f"#{r['id']:<4} {r['state']:<8} {r['instrument']:<10} long {r['long_venue']:<12} "
            f"short {r['short_venue']:<12}{extra}"
        )
    return "\n".join(lines)


async def _main(args: argparse.Namespace, config: AppConfig) -> None:
    db = open_engine_db(config.executor.db_path)
    if args.command == "status":
        print(_status(db))
        return
    venues = build_venues(config)
    try:
        executor = Executor(config, venues, db)
        RiskMonitor(config, executor, db, positions=paper_positions(db))  # gates plan()
        instructions: list[ManualInstruction] = []
        if args.command == "plan":
            notional = config.sizing.notional_per_leg_usd
            pairs = pairs_from_config(config)
            if args.instrument:
                pairs = {args.instrument: pairs[args.instrument]}
            market = await fetch_market(venues, pairs, notional)
            ranked = score(
                market,
                venues={n: config.venues[n] for n in venues},
                notional_usd=notional,
                hold_minutes=config.scoring.hold_minutes,
            )
            if not ranked:
                print("no candidate: no instrument has quotes on two venues right now")
                return
            instructions = await executor.plan(ranked[0])
        elif args.command == "fill":
            instructions = await executor.confirm_manual(
                args.cycle, Decimal(args.price), Decimal(args.size) if args.size else None
            )
        elif args.command == "close":
            instructions = await executor.request_close(args.cycle)
        elif args.command == "due":
            instructions = await executor.close_due()
        elif args.command == "abort":
            executor.abort(args.cycle, args.reason)
        for instruction in instructions:
            print("ACTION:", instruction.message())
        print(_status(db))
    finally:
        for venue in venues.values():
            await venue.aclose()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("command", choices=["plan", "fill", "close", "due", "abort", "status"])
    parser.add_argument("cycle", type=int, nargs="?")
    parser.add_argument("--config", type=Path, default=Path("config/local.yaml"))
    parser.add_argument("--instrument")
    parser.add_argument("--price")
    parser.add_argument("--size", help="venue units, if the fill differs from the instruction")
    parser.add_argument("--reason", default="operator")
    args = parser.parse_args()
    if args.command in ("fill", "close", "abort") and args.cycle is None:
        parser.error(f"{args.command} needs a cycle id")
    if args.command == "fill" and args.price is None:
        parser.error("fill needs --price")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_main(args, load_config(args.config)))


if __name__ == "__main__":
    main()
