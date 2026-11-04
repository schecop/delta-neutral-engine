from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from delta_neutral.collector import collect_tick, open_db, pairs_from_config, report, run
from delta_neutral.config import AppConfig, load_config
from delta_neutral.models import FundingRate
from delta_neutral.venues.base import VenueAdapter, VenueUnavailableError
from delta_neutral.venues.fake import FakeVenue

EXAMPLE = Path(__file__).parent.parent / "config" / "example.yaml"
LIMIT = Decimal("0.01")
USD = Decimal(1000)


class _NoFunding(FakeVenue):
    async def get_funding(self, instrument: str) -> FundingRate:
        raise VenueUnavailableError(f"{self.name}: market data is 45s old")


def _db(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "market.db")


def _tick(
    db: sqlite3.Connection, venues: dict[str, VenueAdapter], pairs: dict[str, list[str]]
) -> dict[str, int]:
    return asyncio.run(collect_tick(db, venues, pairs, USD, LIMIT))


def test_tick_stores_every_pair_sized_at_the_notional(tmp_path: Path) -> None:
    db = _db(tmp_path)
    venues: dict[str, VenueAdapter] = {
        "a": FakeVenue("a", {"XAU-USD": Decimal(2000)}),
        "b": FakeVenue("b", {"XAU-USD": Decimal(2001)}),
    }
    summary = _tick(db, venues, {"XAU-USD": ["a", "b"]})
    assert summary["quotes"] == 2 and summary["errors"] == 0
    rows = db.execute("SELECT venue, size, bid FROM quotes ORDER BY venue").fetchall()
    assert [r[0] for r in rows] == ["a", "b"]
    assert Decimal(rows[0][1]) == USD / Decimal(2000)  # 0.5 units = $1000 at the mid
    assert isinstance(rows[0][2], str)  # Decimals stored as text, never as floats
    assert db.execute("SELECT COUNT(*) FROM funding").fetchone()[0] == 2


def test_one_failing_venue_does_not_stop_the_others(tmp_path: Path) -> None:
    db = _db(tmp_path)
    venues: dict[str, VenueAdapter] = {
        "a": FakeVenue("a", {"XAU-USD": Decimal(2000), "NVDA-USD": Decimal(200)}),
        "b": _NoFunding("b", {"XAU-USD": Decimal(2000)}),  # unavailable, and does not list NVDA
    }
    summary = _tick(db, venues, {"XAU-USD": ["a", "b"], "NVDA-USD": ["a", "b"]})
    assert (summary["quotes"], summary["errors"]) == (2, 2)
    events = db.execute("SELECT kind, venue, instrument FROM events ORDER BY instrument").fetchall()
    assert events == [("unknown_instrument", "b", "NVDA-USD"), ("unavailable", "b", "XAU-USD")]


def test_price_divergence_is_an_event_not_an_exclusion(tmp_path: Path) -> None:
    db = _db(tmp_path)
    venues: dict[str, VenueAdapter] = {
        "a": FakeVenue("a", {"US500-USD": Decimal(755)}),
        "b": FakeVenue("b", {"US500-USD": Decimal(7550)}),
    }
    summary = _tick(db, venues, {"US500-USD": ["a", "b"]})
    assert summary["quotes"] == 2  # both still stored
    (message,) = db.execute("SELECT message FROM events WHERE kind = 'price_divergence'").fetchone()
    assert "contract_size" in message


def test_a_tick_slower_than_its_deadline_stores_no_quotes(tmp_path: Path) -> None:
    # Regression (FAILURES.md, 2026-11-04): a tick spanning a sleep stored a pre-sleep quote next
    # to a post-sleep one, and the guard saw a 2.8% "divergence" that was really 13h of market move
    now = [0.0]

    class _Sleeper(FakeVenue):
        async def get_funding(self, instrument: str) -> FundingRate:
            now[0] += 47_000  # the machine sleeps while this venue's request is in flight
            return await super().get_funding(instrument)

    db = _db(tmp_path)
    venues: dict[str, VenueAdapter] = {
        "a": FakeVenue("a", {"XAU-USD": Decimal(2000)}),
        "b": _Sleeper("b", {"XAU-USD": Decimal(2056)}),
    }
    summary = asyncio.run(
        collect_tick(
            db,
            venues,
            {"XAU-USD": ["a", "b"]},
            USD,
            LIMIT,
            deadline_seconds=30,
            monotonic=lambda: now[0],
        )
    )
    assert summary["quotes"] == 0
    assert db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
    kinds = [k for (k,) in db.execute("SELECT kind FROM events")]
    assert kinds == ["late_tick"]  # no price_divergence from mixed times


def test_a_hung_venue_cannot_hold_a_tick_past_its_deadline(tmp_path: Path) -> None:
    class _Hung(FakeVenue):
        async def get_funding(self, instrument: str) -> FundingRate:
            await asyncio.sleep(60)
            return await super().get_funding(instrument)

    db = _db(tmp_path)
    venues: dict[str, VenueAdapter] = {"a": _Hung("a", {"XAU-USD": Decimal(2000)})}
    summary = asyncio.run(
        collect_tick(db, venues, {"XAU-USD": ["a"]}, USD, LIMIT, deadline_seconds=0.05)
    )
    assert summary["quotes"] == 0 and summary["ms"] < 5_000
    assert db.execute("SELECT kind FROM events").fetchone() == ("late_tick",)


def test_pairs_follow_config_and_enabled_venues() -> None:
    config = load_config(EXAMPLE)
    assert pairs_from_config(config) == {"XAU-USD": ["qfex", "variational"]}
    config.venues["qfex"].enabled = False
    assert pairs_from_config(config) == {"XAU-USD": ["variational"]}


def _config(tmp_path: Path, interval: int = 30) -> AppConfig:
    config = load_config(EXAMPLE)
    config.collector.db_path = tmp_path / "market.db"
    config.collector.interval_seconds = interval
    return config


def _fakes() -> dict[str, VenueAdapter]:
    return {n: FakeVenue(n, {"XAU-USD": Decimal(2000)}) for n in ("variational", "qfex")}


def test_run_keeps_a_fixed_schedule(tmp_path: Path) -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds + 0.25  # each tick also takes 0.25s of work

    asyncio.run(
        run(_config(tmp_path), max_ticks=3, venues=_fakes(), sleep=sleep, monotonic=lambda: now[0])
    )
    db = _db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 3
    # Sleeps absorb the work time, so ticks stay on the 30s grid instead of drifting by 0.25s each
    assert sleeps == [30.0, 29.75]


def test_run_skips_slots_after_an_overrun(tmp_path: Path) -> None:
    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    def monotonic() -> float:
        return now[0]

    class _Slow(FakeVenue):
        async def get_funding(self, instrument: str) -> FundingRate:
            now[0] += 70  # a 70s tick on a 30s interval: slots at 30s and 60s are lost
            return await super().get_funding(instrument)

    venues: dict[str, VenueAdapter] = {
        "variational": _Slow("variational", {"XAU-USD": Decimal(2000)})
    }
    asyncio.run(
        run(_config(tmp_path), max_ticks=4, venues=venues, sleep=sleep, monotonic=monotonic)
    )
    db = _db(tmp_path)
    (message,) = db.execute("SELECT message FROM events WHERE kind = 'overrun'").fetchone()
    assert "skipped 2" in message


def _insert_ticks(db: sqlite3.Connection, ticks: list[tuple[int, int]]) -> None:
    """(start second, duration ms) per tick, each with quotes."""
    t0 = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
    for seconds, ms in ticks:
        db.execute(
            "INSERT INTO ticks (started_at, duration_ms, quotes, errors) VALUES (?, ?, 2, 0)",
            ((t0 + timedelta(seconds=seconds)).isoformat(), ms),
        )
    db.commit()


def test_report_flags_gaps_over_twice_the_interval(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_ticks(db, [(0, 5), (30, 5), (60, 5), (150, 5)])  # 90s gap on a 30s interval
    text = report(db, interval_seconds=30)
    assert "max gap 90s (limit 60s): GAP" in text
    assert "ticks 4" in text


def test_report_sees_a_gap_hidden_inside_a_long_tick(tmp_path: Path) -> None:
    # Regression (FAILURES.md, 2026-11-04): the PC slept mid-tick; starts were 30s apart, so the
    # report said OK while the last tick took 13h to finish
    db = _db(tmp_path)
    _insert_ticks(db, [(0, 400), (30, 400), (60, 47_582_374)])
    assert "GAP" in report(db, interval_seconds=30)


def test_interval_below_the_venue_rate_limit_fails_at_load() -> None:
    config = load_config(EXAMPLE).model_dump(mode="json")
    config["collector"]["interval_seconds"] = 1
    with pytest.raises(ValidationError, match="interval_seconds"):
        AppConfig.model_validate(config)
