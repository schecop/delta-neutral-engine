"""Backtest cases computed by hand, on a database written row by row."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from delta_neutral.backtest import Result, format_report, run
from delta_neutral.collector import open_db
from delta_neutral.config import VenueConfig
from delta_neutral.scorer import load_markets

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
N = Decimal(1000)
VENUES = {
    "a": VenueConfig.model_validate(
        {
            "adapter": "fake",
            "execution": "api",
            "max_leverage": "5",
            "reward": {"per_usd_volume": "1"},
        }
    ),
    "b": VenueConfig.model_validate(
        {"adapter": "fake", "execution": "api", "max_leverage": "5", "fees": {"taker": "0.0005"}}
    ),
}
# (second, venue, bid, ask, funding %/h); a and b at 30s ticks
Row = tuple[int, str, str, str, str]


def _db(tmp_path: Path, rows: list[Row]) -> sqlite3.Connection:
    db = open_db(tmp_path / "market.db")
    ticks: dict[int, int] = {}
    for second, venue, bid, ask, funding in rows:
        at = (T0 + timedelta(seconds=second)).isoformat()
        if second not in ticks:
            cur = db.execute(
                "INSERT INTO ticks (started_at, duration_ms, quotes, errors) VALUES (?, 5, 2, 0)",
                (at,),
            )
            ticks[second] = cur.lastrowid or 0
        db.execute(
            "INSERT INTO quotes VALUES (?, ?, 'XAU-USD', '10', ?, ?, ?, 0)",
            (ticks[second], venue, bid, ask, at),
        )
        db.execute(
            "INSERT INTO funding VALUES (?, ?, 'XAU-USD', ?, ?)",
            (ticks[second], venue, funding, at),
        )
    db.commit()
    return db


def _steady(seconds: list[int], a_mid: dict[int, int] | None = None) -> list[Row]:
    """a: 20 bps wide around 100, funding 0.01%/h; b: 10 bps around 100, 0.002%/h."""
    rows: list[Row] = []
    for s in seconds:
        mid = Decimal((a_mid or {}).get(s, 100))
        rows.append((s, "a", str(mid - Decimal("0.1")), str(mid + Decimal("0.1")), "0.01"))
        rows.append((s, "b", "99.95", "100.05", "0.002"))
    return rows


def _run(db: sqlite3.Connection, holds: list[int]) -> Result:
    return run(load_markets(db), VENUES, N, holds, interval_seconds=30)


def test_a_replayed_cycle_matches_the_hand_computation(tmp_path: Path) -> None:
    db = _db(tmp_path, _steady([0, 30, 60]))
    trades = {t.long_venue: t for t in _run(db, [1]).trades if t.entry_at == T0}
    best = trades["b"]  # long b, short a: earns the funding difference
    # sizing: 1000 / 100 = 10 units each. long b buys 10 @ 100.05, sells @ 99.95: -1.0
    # short a sells 10 @ 99.9, buys back @ 100.1: -2.0. fees on b: 0.0005 * (1000.5 + 999.5) = 1.0
    # funding over 1 minute: 1000 * (0.002 - 0.01) / 100 / 60
    funding = N * (Decimal("0.002") - Decimal("0.01")) / 100 * (Decimal(30) / 3600) * 2
    assert best.funding_usd == funding
    assert best.realised_usd == Decimal(3) + Decimal(1) + funding
    # with no price move, the scorer predicted it exactly (up to Decimal rounding)
    assert abs(best.error_usd) < Decimal("1e-20")
    assert best.exit_at == T0 + timedelta(minutes=1)
    assert best.top_pick and not trades["a"].top_pick


def test_a_price_move_between_venues_is_what_the_scorer_misses(tmp_path: Path) -> None:
    # a's mid rises from 100 to 101 while b stays: the short on a loses 10 units * 1.0
    db = _db(tmp_path, _steady([0, 30, 60], a_mid={60: 101}))
    best = next(t for t in _run(db, [1]).trades if t.entry_at == T0 and t.long_venue == "b")
    assert best.error_usd == Decimal(10)


def test_a_hole_in_the_data_skips_the_window(tmp_path: Path) -> None:
    db = _db(tmp_path, _steady([0, 30, 300, 330]))  # 270s hole: over 5 ticks of 30s
    # entries at 0s and 30s would exit at 300s and 330s across the hole; from 300s data ends first
    result = _run(db, [5])
    assert result.trades == []
    assert result.skipped[("XAU-USD", "a", "b"), 5] == 2
    assert result.skipped[("XAU-USD", "b", "a"), 5] == 2


def test_one_missed_tick_does_not_drop_the_window(tmp_path: Path) -> None:
    # a venue rate-limited for one tick (no row at 60s) must not void every window across it
    db = _db(tmp_path, _steady([0, 30, 90, 120]))
    result = _run(db, [2])
    assert {(t.entry_at, t.exit_at) for t in result.trades} == {(T0, T0 + timedelta(seconds=120))}
    assert not result.skipped


def test_holds_longer_than_the_data_produce_no_trades(tmp_path: Path) -> None:
    db = _db(tmp_path, _steady([0, 30, 60]))
    result = _run(db, [5])
    assert result.trades == []
    assert "no complete cycle" in format_report(result, 60, N)


def test_report_marks_holds_below_the_risk_minimum(tmp_path: Path) -> None:
    db = _db(tmp_path, _steady([0, 30, 60]))
    text = format_report(_run(db, [1]), min_hold_minutes=60, notional_usd=N)
    assert "hold 1 min  (below risk.min_hold_minutes 60)" in text
    assert "XAU-USD" in text
