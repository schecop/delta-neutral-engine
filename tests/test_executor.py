"""Executor cycles on fake venues, with the numbers computed by hand."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from delta_neutral.config import AppConfig, load_config
from delta_neutral.executor import CycleError, CycleState, Executor, fetch_market, open_engine_db
from delta_neutral.models import Execution, Quote
from delta_neutral.scorer import Candidate, score
from delta_neutral.venues.base import VenueAdapter
from delta_neutral.venues.fake import FakeVenue

EXAMPLE = Path(__file__).parent.parent / "config" / "example.yaml"
T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
N = Decimal(1000)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _SlowApi(FakeVenue):
    """An API venue whose quote takes 2s: the gap between the two legs' fills."""

    def __init__(self, *args: Any, on_quote: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_quote = on_quote

    async def get_quote(self, instrument: str, size: Decimal) -> Quote:
        self._on_quote()
        return await super().get_quote(instrument, size)


def _config(**changes: Any) -> AppConfig:
    data = load_config(EXAMPLE).model_dump(mode="json")
    data["sizing"]["notional_per_leg_usd"] = str(N)
    data["scoring"]["hold_minutes"] = 30
    data["risk"]["min_hold_minutes"] = 30
    data["instruments"]["US500-USD"] = {
        "variational": {"symbol": "US500-PERP", "contract_size": "0.1"},
        "qfex": "US500-USD",
    }
    for key, value in changes.items():
        data[key] = value
    return AppConfig.model_validate(data)


def _setup(
    tmp_path: Path, clock: Clock, manual: bool = True
) -> tuple[AppConfig, dict[str, VenueAdapter], sqlite3.Connection]:
    mids = {"XAU-USD": Decimal(2000), "US500-USD": Decimal(2000)}
    # variational: 20 bps -> 1998 / 2002; qfex: 10 bps -> 1999 / 2001; funding equal on both
    venues: dict[str, VenueAdapter] = {
        "variational": FakeVenue(
            "variational",
            mids,
            spread_bps=Decimal(20),
            execution=Execution.MANUAL if manual else Execution.API,
        ),
        "qfex": _SlowApi("qfex", mids, spread_bps=Decimal(10), on_quote=lambda: clock.advance(2)),
    }
    config = _config(
        venues={
            **load_config(EXAMPLE).model_dump(mode="json")["venues"],
            "variational": {
                **load_config(EXAMPLE).model_dump(mode="json")["venues"]["variational"],
                "execution": "manual" if manual else "api",
            },
        }
    )
    return config, venues, open_engine_db(tmp_path / "engine.db")


def _candidate(config: AppConfig, venues: dict[str, VenueAdapter], instrument: str) -> Candidate:
    market = asyncio.run(fetch_market(venues, {instrument: ["qfex", "variational"]}, N))
    ranked = score(market, config.venues, N, hold_minutes=30)
    return next(c for c in ranked if c.long_venue == "qfex")  # long qfex, short variational


def _state(db: sqlite3.Connection, cycle_id: int = 1) -> str:
    return str(db.execute("SELECT state FROM cycles WHERE id = ?", (cycle_id,)).fetchone()[0])


def _fills(db: sqlite3.Connection, phase: str) -> dict[str, Decimal | None]:
    rows = db.execute("SELECT venue, price FROM legs WHERE phase = ?", (phase,)).fetchall()
    return {r["venue"]: None if r["price"] is None else Decimal(r["price"]) for r in rows}


def test_a_full_dry_run_cycle_matches_the_hand_computation(tmp_path: Path) -> None:
    clock = Clock()
    config, venues, db = _setup(tmp_path, clock)
    executor = Executor(config, venues, db, clock=clock)
    candidate = _candidate(config, venues, "XAU-USD")
    clock.now = T0

    # open: the manual leg is asked for first, and the API leg waits for it
    (instruction,) = asyncio.run(executor.plan(candidate))
    assert (instruction.venue, instruction.action, instruction.symbol) == (
        "variational",
        "SELL",
        "XAU-PERP",
    )
    assert instruction.venue_size == Decimal("0.5")  # $1000 / mid 2000
    assert instruction.venue_price == Decimal(1998)  # a sell goes at the bid
    assert _state(db) == CycleState.OPENING
    assert _fills(db, "open") == {"variational": None, "qfex": None}

    clock.advance(10)
    assert asyncio.run(executor.confirm_manual(1, Decimal(1998))) == []
    assert _state(db) == CycleState.OPEN
    assert _fills(db, "open") == {
        "variational": Decimal(1998),
        "qfex": Decimal(2001),
    }  # paper buy at ask

    # nothing is due before the hold ends; at 30 min the close starts, manual leg first again
    clock.advance(29 * 60)
    assert asyncio.run(executor.close_due()) == []
    clock.advance(60)
    (close,) = asyncio.run(executor.close_due())
    assert (close.action, close.venue_price) == ("BUY", Decimal(2002))
    assert _state(db) == CycleState.CLOSING

    asyncio.run(executor.confirm_manual(1, Decimal(2002)))
    assert _state(db) == CycleState.CLOSED
    summary = json.loads(db.execute("SELECT summary FROM cycles").fetchone()[0])
    # price: long 0.5 * (1999 - 2001) = -1; short 0.5 * (1998 - 2002) = -2
    # fees: qfex 0.0005 * 0.5 * (2001 + 1999) = 1; variational 0; funding: equal rates, 0
    assert Decimal(summary["price_pnl_usd"]) == -3
    assert Decimal(summary["fees_usd"]) == 1
    assert Decimal(summary["realised_cost_usd"]) == 4
    assert Decimal(summary["predicted_cost_usd"]) == 4  # the scorer saw the same spreads
    assert Decimal(summary["error_usd"]) == 0
    assert summary["close_unhedged_s"] == 2.0  # the API leg's 2s quote latency
    kinds = [r[0] for r in db.execute("SELECT kind FROM cycle_events ORDER BY rowid")]
    assert kinds[:2] == ["planned", "opening"] and kinds[-1] == "closed"


def test_a_restart_mid_cycle_continues_from_the_database(tmp_path: Path) -> None:
    clock = Clock()
    config, venues, db = _setup(tmp_path, clock)
    asyncio.run(
        Executor(config, venues, db, clock=clock).plan(_candidate(config, venues, "XAU-USD"))
    )
    db.close()
    db = open_engine_db(tmp_path / "engine.db")  # a new process, the same file
    asyncio.run(Executor(config, venues, db, clock=clock).confirm_manual(1, Decimal(1998)))
    assert _state(db) == CycleState.OPEN


def test_abort_only_before_any_fill(tmp_path: Path) -> None:
    clock = Clock()
    config, venues, db = _setup(tmp_path, clock)
    executor = Executor(config, venues, db, clock=clock)
    asyncio.run(executor.plan(_candidate(config, venues, "XAU-USD")))
    executor.abort(1, "quote moved")
    assert _state(db) == CycleState.ABORTED

    asyncio.run(executor.plan(_candidate(config, venues, "XAU-USD")))
    asyncio.run(executor.confirm_manual(2, Decimal(1998)))
    with pytest.raises(CycleError, match="close it instead"):
        executor.abort(2, "too late")


def test_operations_out_of_order_are_refused(tmp_path: Path) -> None:
    clock = Clock()
    config, venues, db = _setup(tmp_path, clock)
    executor = Executor(config, venues, db, clock=clock)
    asyncio.run(executor.plan(_candidate(config, venues, "XAU-USD")))
    with pytest.raises(CycleError, match="only an open cycle"):
        asyncio.run(executor.request_close(1))
    asyncio.run(executor.confirm_manual(1, Decimal(1998)))
    with pytest.raises(CycleError, match="nothing to fill"):
        asyncio.run(executor.confirm_manual(1, Decimal(1998)))
    with pytest.raises(CycleError, match="no cycle"):
        asyncio.run(executor.confirm_manual(99, Decimal(1)))


def test_live_mode_is_refused(tmp_path: Path) -> None:
    clock = Clock()
    config, venues, db = _setup(tmp_path, clock)
    with pytest.raises(CycleError, match="dry-run only"):
        Executor(config.model_copy(update={"mode": "live"}), venues, db, clock=clock)


def test_manual_instructions_use_the_venue_units(tmp_path: Path) -> None:
    # variational quotes US500 at a tenth: the operator sees its symbol, size and price
    clock = Clock()
    config, venues, db = _setup(tmp_path, clock)
    executor = Executor(config, venues, db, clock=clock)
    (instruction,) = asyncio.run(executor.plan(_candidate(config, venues, "US500-USD")))
    assert instruction.symbol == "US500-PERP"
    assert instruction.venue_size == Decimal(5)  # 0.5 canonical units = 5 venue units
    assert instruction.venue_price == Decimal("199.8")
    asyncio.run(executor.confirm_manual(1, Decimal("199.8"), venue_size=Decimal(5)))
    row = db.execute("SELECT price, size FROM legs WHERE venue = 'variational'").fetchone()
    assert (Decimal(row["price"]), Decimal(row["size"])) == (Decimal(1998), Decimal("0.5"))


def test_two_api_venues_open_without_the_operator(tmp_path: Path) -> None:
    clock = Clock()
    config, venues, db = _setup(tmp_path, clock, manual=False)
    executor = Executor(config, venues, db, clock=clock)
    assert asyncio.run(executor.plan(_candidate(config, venues, "XAU-USD"))) == []
    assert _state(db) == CycleState.OPEN
