"""One test per risk rule that triggers it, on the executor and fake venues."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from delta_neutral.config import WeekendConfig
from delta_neutral.executor import CycleError, CycleState, Executor, open_engine_db
from delta_neutral.models import Execution, MarginState, Quote
from delta_neutral.risk import Positions, RiskMonitor, paper_positions, weekend_blocks
from delta_neutral.venues.base import VenueAdapter, VenueUnavailableError
from delta_neutral.venues.fake import FakeVenue
from tests.test_executor import Clock, _candidate, _config

THURSDAY = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
# US close Friday 2026-11-06 16:00 New York = 21:00 UTC (EST); example config: 30 min before
WINDOW_START = datetime(2026, 11, 6, 20, 30, tzinfo=UTC)
MONDAY_OPEN = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)


class _Breakable(FakeVenue):
    broken = False

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        if self.broken:
            raise VenueUnavailableError(f"{self.name}: market data is 45s old")
        return await super().get_quote(instrument, size)


class Engine:
    def __init__(self, tmp_path: Path, *, margin_free: Decimal | None = None) -> None:
        self.clock = Clock()
        self.clock.now = THURSDAY
        mids = {"XAU-USD": Decimal(2000)}
        self.qfex = _Breakable("qfex", mids, spread_bps=Decimal(10))
        self.venues: dict[str, VenueAdapter] = {
            "variational": FakeVenue(
                "variational", mids, spread_bps=Decimal(20), execution=Execution.MANUAL
            ),
            "qfex": self.qfex,
        }
        self.config = _config()
        self.db: sqlite3.Connection = open_engine_db(tmp_path / "engine.db")
        self.executor = Executor(self.config, self.venues, self.db, clock=self.clock)
        self.reported: dict[str, dict[str, Decimal]] | None = None  # None: use the paper book
        self.margin_free = margin_free
        self.monitor = RiskMonitor(
            self.config,
            self.executor,
            self.db,
            positions=self._positions,
            margins=self._margins,
            clock=self.clock,
        )

    async def _positions(self) -> Positions:
        if self.reported is not None:
            return self.reported
        return await paper_positions(self.db)()

    async def _margins(self) -> Mapping[str, MarginState]:
        if self.margin_free is None:
            return {}
        used = Decimal(1000) * (1 - self.margin_free)
        return {
            "qfex": MarginState(
                venue="qfex", equity_usd=Decimal(1000), used_margin_usd=used, ts=self.clock.now
            )
        }

    def plan(self) -> None:
        asyncio.run(self.executor.plan(_candidate(self.config, self.venues, "XAU-USD")))

    def open_cycle(self) -> None:
        self.plan()
        asyncio.run(self.executor.confirm_manual(1, Decimal(1998)))

    def check(self) -> list[tuple[str, str]]:
        return [(b.rule, b.action) for b in asyncio.run(self.monitor.check())]

    def state(self) -> str:
        return str(self.db.execute("SELECT state FROM cycles WHERE id = 1").fetchone()[0])

    def events(self) -> list[tuple[str, str]]:
        return [
            (r["rule"], r["action"])
            for r in self.db.execute("SELECT rule, action FROM risk_events ORDER BY rowid")
        ]


def test_the_paper_book_alone_raises_no_breach(tmp_path: Path) -> None:
    engine = Engine(tmp_path)
    engine.open_cycle()
    assert engine.check() == [] and engine.events() == []


def test_unhedged_too_long_retries_the_missing_leg(tmp_path: Path) -> None:
    engine = Engine(tmp_path)
    engine.plan()
    engine.qfex.broken = True  # the API leg fails right after the manual leg is confirmed
    with pytest.raises(VenueUnavailableError):
        asyncio.run(engine.executor.confirm_manual(1, Decimal(1998)))
    assert engine.state() == CycleState.OPENING  # one leg filled, one not: exposed

    engine.clock.advance(30)
    assert engine.check() == []  # within max_unhedged_seconds (60)
    engine.qfex.broken = False
    engine.clock.advance(31)
    assert engine.check() == [("unhedged", "resume")]
    assert engine.state() == CycleState.OPEN


def test_unhedged_that_cannot_be_fixed_asks_for_an_unwind(tmp_path: Path) -> None:
    engine = Engine(tmp_path)
    engine.plan()
    engine.qfex.broken = True
    with pytest.raises(VenueUnavailableError):
        asyncio.run(engine.executor.confirm_manual(1, Decimal(1998)))
    engine.clock.advance(61)
    engine.check()
    assert engine.events() == [("unhedged", "unwind_required")]


def test_a_lost_leg_closes_the_cycle(tmp_path: Path) -> None:
    engine = Engine(tmp_path)
    engine.open_cycle()
    # qfex reports no position (liquidated); variational cannot report, so it is not checked
    engine.reported = {"qfex": {}}
    assert engine.check() == [("leg_missing", "close_cycle")]
    assert engine.state() == CycleState.CLOSING  # the other leg's close is asked for


def test_low_margin_blocks_new_cycles_and_very_low_asks_for_a_rebalance(tmp_path: Path) -> None:
    engine = Engine(tmp_path, margin_free=Decimal("0.30"))  # min 0.40, rebalance below 0.25
    with pytest.raises(CycleError, match="free margin 30%"):
        engine.plan()
    engine.margin_free = Decimal("0.20")
    assert sorted(engine.check()) == [("margin", "block_new"), ("margin", "rebalance")]


def test_the_weekend_window_closes_open_cycles(tmp_path: Path) -> None:
    engine = Engine(tmp_path)
    engine.open_cycle()
    engine.clock.now = WINDOW_START + timedelta(minutes=5)
    # the cycle may already be past its hold; the weekend rule closes it either way
    assert ("weekend", "close_cycle") in engine.check()
    assert engine.state() == CycleState.CLOSING


def test_the_weekend_window_refuses_cycles_that_would_cross_it(tmp_path: Path) -> None:
    engine = Engine(tmp_path)
    engine.clock.now = WINDOW_START - timedelta(minutes=20)  # a 30-min hold would end inside
    with pytest.raises(CycleError, match="weekend rule"):
        engine.plan()


def test_the_weekend_window_bounds() -> None:
    close = WeekendConfig(action="close", before_us_close_minutes=30)

    def blocked(at: datetime, hold: int = 30) -> bool:
        return bool(weekend_blocks(at, hold, close))

    assert not blocked(WINDOW_START - timedelta(minutes=31))  # ends a minute before the window
    assert blocked(WINDOW_START - timedelta(minutes=29))
    assert blocked(datetime(2026, 11, 8, 12, 0, tzinfo=UTC))  # Sunday
    assert blocked(MONDAY_OPEN - timedelta(minutes=1))  # Monday before the US open
    assert not blocked(MONDAY_OPEN)
    assert not weekend_blocks(WINDOW_START, 30, close.model_copy(update={"action": "none"}))


def test_a_persisting_breach_is_recorded_once_per_five_minutes(tmp_path: Path) -> None:
    engine = Engine(tmp_path, margin_free=Decimal("0.30"))
    engine.check()
    engine.clock.advance(60)
    engine.check()
    assert engine.events() == [("margin", "block_new")]
    engine.clock.advance(5 * 60)
    engine.check()
    assert engine.events() == [("margin", "block_new")] * 2
