"""Risk monitor: the risk rules, enforced in code.

    uv run python -m delta_neutral.risk check          one pass: close due cycles, apply the rules
    uv run python -m delta_neutral.risk watch [--every 10]   the same, in a loop (the engine loop)
    uv run python -m delta_neutral.risk events         recent risk events

DRY-RUN: acts through the executor, which places no orders.

Four rules, each a pure check that returns breaches; the monitor then applies each breach's action
through the executor and records it in `risk_events`, so a human (and the supervisor) can audit
every decision. No LLM is involved anywhere here.

    unhedged     a phase has some legs filled and others not for over max_unhedged_seconds:
                 retry the missing legs; if that fails, the filled leg must be unwound (urgent)
    leg_missing  a venue no longer holds a leg the engine expects (closed or liquidated):
                 close the cycle, i.e. the other leg
    margin       free margin below min_free_margin: no new cycles; below
                 rebalance_below_free_margin: also ask for a rebalance
    weekend      from before_us_close_minutes before the US close on Friday (16:00 New York)
                 until the US open on Monday (09:30): no cycle may be open; `close` closes them
                 and refuses new ones whose hold would cross into the window

Rules 2 and 3 need a venue that reports positions and margin. In dry-run the engine's own paper
book is the position source and there is no margin source; tests inject both. A venue without an
account API (execution: manual) cannot be checked here: its state comes from the operator.
US market holidays are not modelled yet.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from delta_neutral.config import AppConfig, RiskConfig, WeekendConfig, load_config
from delta_neutral.models import MarginState

if TYPE_CHECKING:
    from delta_neutral.executor import Executor

log = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
US_CLOSE, US_OPEN = time(16, 0), time(9, 30)
REPEAT_AFTER = timedelta(minutes=5)  # a breach that persists is recorded again after this

SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_events (
    at TEXT NOT NULL,
    rule TEXT NOT NULL,         -- unhedged | leg_missing | margin | weekend
    action TEXT NOT NULL,       -- what the monitor did or asks for
    cycle_id INTEGER,
    venue TEXT,
    message TEXT NOT NULL
);
"""

# venue -> instrument -> signed size actually held (canonical units)
Positions = Mapping[str, Mapping[str, Decimal]]
PositionSource = Callable[[], Awaitable[Positions]]
MarginSource = Callable[[], Awaitable[Mapping[str, MarginState]]]


@dataclass(frozen=True)
class Breach:
    rule: str
    action: str  # resume | close_cycle | block_new | rebalance
    message: str
    cycle_id: int | None = None
    venue: str | None = None


@dataclass(frozen=True)
class LegView:
    phase: str
    venue: str
    instrument: str
    side: str
    size: Decimal
    filled_at: datetime | None


@dataclass(frozen=True)
class CycleView:
    id: int
    state: str
    instrument: str
    legs: tuple[LegView, ...]


# -- the rules (pure) -------------------------------------------------------------------------


def _weekend_windows(now: datetime, before_minutes: int) -> list[tuple[datetime, datetime]]:
    """This week's and last week's windows, so a Monday before the open is still inside one."""
    local = now.astimezone(NEW_YORK)
    friday = local.date() + timedelta(days=4 - local.weekday())
    out = []
    for f in (friday - timedelta(days=7), friday):
        start = datetime.combine(f, US_CLOSE, NEW_YORK) - timedelta(minutes=before_minutes)
        end = datetime.combine(f + timedelta(days=3), US_OPEN, NEW_YORK)
        out.append((start, end))
    return out


def in_weekend(now: datetime, weekend: WeekendConfig) -> bool:
    return any(s <= now < e for s, e in _weekend_windows(now, weekend.before_us_close_minutes))


def weekend_blocks(now: datetime, hold_minutes: int, weekend: WeekendConfig) -> list[str]:
    """Why a cycle opened now for `hold_minutes` would break the weekend rule (empty: allowed)."""
    if weekend.action == "none":
        return []
    end_of_hold = now + timedelta(minutes=hold_minutes)
    for start, end in _weekend_windows(now, weekend.before_us_close_minutes):
        if now < end and end_of_hold > start:
            return [f"weekend rule: no cycles from {start:%a %H:%M} to {end:%a %H:%M} New York"]
    return []


def check_unhedged(now: datetime, cycles: list[CycleView], risk: RiskConfig) -> list[Breach]:
    out = []
    for c in cycles:
        phase = {"opening": "open", "closing": "close"}.get(c.state)
        legs = [leg for leg in c.legs if leg.phase == phase]
        filled = [leg.filled_at for leg in legs if leg.filled_at is not None]
        if not filled or len(filled) == len(legs):
            continue  # nothing exposed yet, or the phase is complete
        seconds = (now - min(filled)).total_seconds()
        if seconds > risk.max_unhedged_seconds:
            out.append(
                Breach(
                    "unhedged",
                    "resume",
                    f"cycle {c.id} {phase} phase unhedged for "
                    f"{seconds:.0f}s (max {risk.max_unhedged_seconds}s)",
                    c.id,
                )
            )
    return out


def check_legs(cycles: list[CycleView], positions: Positions) -> list[Breach]:
    """Every open leg must be held on its venue; a venue that reports less lost a leg."""
    expected: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    owners: dict[tuple[str, str], list[int]] = defaultdict(list)
    for c in cycles:
        if c.state != "open":
            continue
        for leg in c.legs:
            if leg.phase == "open":
                key = (leg.venue, leg.instrument)
                expected[key] += leg.size if leg.side == "long" else -leg.size
                owners[key].append(c.id)
    out = []
    for (venue, instrument), size in expected.items():
        if venue not in positions:
            continue  # this venue cannot report positions: the operator watches it
        held = positions[venue].get(instrument, Decimal(0))
        if abs(held - size) > abs(size) * Decimal("1e-6"):
            for cycle_id in owners[venue, instrument]:
                out.append(
                    Breach(
                        "leg_missing",
                        "close_cycle",
                        f"{venue} holds {held} {instrument}, the engine expects {size}",
                        cycle_id,
                        venue,
                    )
                )
    return out


def check_margin(margins: Mapping[str, MarginState], risk: RiskConfig) -> list[Breach]:
    out = []
    for venue, m in margins.items():
        free = m.free_fraction
        if free < risk.rebalance_below_free_margin:
            out.append(
                Breach(
                    "margin",
                    "rebalance",
                    f"{venue} free margin {free:.0%} is below "
                    f"{risk.rebalance_below_free_margin:.0%}: rebalance",
                    venue=venue,
                )
            )
        if free < risk.min_free_margin:
            out.append(
                Breach(
                    "margin",
                    "block_new",
                    f"{venue} free margin {free:.0%} is below "
                    f"{risk.min_free_margin:.0%}: no new cycles",
                    venue=venue,
                )
            )
    return out


def check_weekend(now: datetime, cycles: list[CycleView], weekend: WeekendConfig) -> list[Breach]:
    if weekend.action != "close" or not in_weekend(now, weekend):
        return []
    return [
        Breach("weekend", "close_cycle", f"cycle {c.id} open into the weekend window", c.id)
        for c in cycles
        if c.state == "open"
    ]


def evaluate(
    now: datetime,
    cycles: list[CycleView],
    positions: Positions,
    margins: Mapping[str, MarginState],
    risk: RiskConfig,
) -> list[Breach]:
    return (
        check_unhedged(now, cycles, risk)
        + check_legs(cycles, positions)
        + check_margin(margins, risk)
        + check_weekend(now, cycles, risk.weekend)
    )


# -- the monitor ------------------------------------------------------------------------------


def load_cycles(db: sqlite3.Connection) -> list[CycleView]:
    """Every cycle that is not finished, from the executor's database."""
    rows = db.execute(
        "SELECT c.id, c.state, c.instrument, l.phase, l.venue, l.side, l.size, l.filled_at "
        "FROM cycles c JOIN legs l ON l.cycle_id = c.id "
        "WHERE c.state NOT IN ('closed', 'aborted') ORDER BY c.id"
    ).fetchall()
    legs: dict[tuple[int, str, str], list[LegView]] = defaultdict(list)
    for cid, state, instrument, phase, venue, side, size, filled_at in rows:
        legs[cid, state, instrument].append(
            LegView(
                phase,
                venue,
                instrument,
                side,
                Decimal(size),
                datetime.fromisoformat(filled_at) if filled_at else None,
            )
        )
    return [CycleView(cid, state, i, tuple(ls)) for (cid, state, i), ls in legs.items()]


def paper_positions(db: sqlite3.Connection) -> PositionSource:
    """Dry-run position source: what the engine's own fills say it holds."""

    async def source() -> Positions:
        held: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for c in load_cycles(db):
            for leg in c.legs:
                if leg.filled_at is None:
                    continue
                opening = leg.phase == "open"
                sign = 1 if (leg.side == "long") == opening else -1
                held[leg.venue][leg.instrument] += sign * leg.size
        return held

    return source


async def _no_margins() -> Mapping[str, MarginState]:
    return {}


class RiskMonitor:
    def __init__(
        self,
        config: AppConfig,
        executor: Executor,
        db: sqlite3.Connection,
        *,
        positions: PositionSource,
        margins: MarginSource = _no_margins,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._risk = config.risk
        self._executor = executor
        self._db = db
        self._positions = positions
        self._margins = margins
        self._clock = clock
        db.executescript(SCHEMA)
        executor.gate = self.opening_blocks  # no cycle opens past this monitor's checks

    async def opening_blocks(self, now: datetime, hold_minutes: int) -> list[str]:
        """The executor's gate: reasons not to open a new cycle right now."""
        return [
            b.message
            for b in check_margin(await self._margins(), self._risk)
            if b.action == "block_new"
        ]

    async def check(self) -> list[Breach]:
        """One pass: evaluate every rule, apply each breach's action, record it."""
        now = self._clock()
        cycles = load_cycles(self._db)
        breaches = evaluate(now, cycles, await self._positions(), await self._margins(), self._risk)
        for breach in breaches:
            action = breach.action
            if breach.cycle_id is not None:
                try:
                    if action == "resume":
                        await self._executor.resume(breach.cycle_id)
                    elif action == "close_cycle":
                        await self._executor.request_close(breach.cycle_id)
                except Exception as exc:  # the action failed: escalate to the operator
                    action = "unwind_required" if action == "resume" else "close_failed"
                    breach = Breach(
                        breach.rule,
                        action,
                        f"{breach.message}; {action}: {type(exc).__name__}: {exc}",
                        breach.cycle_id,
                        breach.venue,
                    )
            self._record(now, breach)
        return breaches

    def _record(self, now: datetime, b: Breach) -> None:
        recent = self._db.execute(
            "SELECT 1 FROM risk_events WHERE rule = ? AND action = ? AND cycle_id IS ? "
            "AND venue IS ? AND at > ?",
            (b.rule, b.action, b.cycle_id, b.venue, (now - REPEAT_AFTER).isoformat()),
        ).fetchone()
        if recent:
            return  # the same breach, already recorded and alerted a moment ago
        with self._db:
            self._db.execute(
                "INSERT INTO risk_events VALUES (?, ?, ?, ?, ?, ?)",
                (now.isoformat(), b.rule, b.action, b.cycle_id, b.venue, b.message),
            )
        log.warning(
            "risk %s",
            json.dumps(
                {
                    "rule": b.rule,
                    "action": b.action,
                    "cycle": b.cycle_id,
                    "venue": b.venue,
                    "message": b.message,
                }
            ),
        )


async def _main(args: argparse.Namespace, config: AppConfig) -> None:
    from delta_neutral.executor import Executor, open_engine_db
    from delta_neutral.venues.factory import build_venues

    db = open_engine_db(config.executor.db_path)
    db.executescript(SCHEMA)
    if args.command == "events":
        for r in db.execute("SELECT * FROM risk_events ORDER BY rowid DESC LIMIT 30"):
            print(
                f"{r['at'][:19]} {r['rule']:<11} {r['action']:<15} "
                f"cycle {r['cycle_id'] or '-'}  {r['message']}"
            )
        db.close()
        return
    venues = build_venues(config)
    try:
        executor = Executor(config, venues, db)
        monitor = RiskMonitor(config, executor, db, positions=paper_positions(db))
        while True:
            for instruction in await executor.close_due():
                print("ACTION:", instruction.message())
            breaches = await monitor.check()
            print(f"{datetime.now(UTC):%H:%M:%S} {len(breaches)} breach(es)")
            if args.command == "check":
                break
            await asyncio.sleep(args.every)
    finally:
        for venue in venues.values():
            await venue.aclose()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("command", choices=["check", "watch", "events"])
    parser.add_argument("--config", type=Path, default=Path("config/local.yaml"))
    parser.add_argument("--every", type=float, default=10.0, help="seconds between passes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main(args, load_config(args.config)))


if __name__ == "__main__":
    main()
