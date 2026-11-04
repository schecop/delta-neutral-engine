"""Scorer cases computed by hand: the expected numbers are written out, not recomputed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from delta_neutral.collector import collect_tick, open_db
from delta_neutral.config import AppConfig, VenueConfig, load_config
from delta_neutral.models import FundingRate, Quote
from delta_neutral.scorer import Market, format_ranking, load_market, score
from delta_neutral.venues.base import VenueAdapter
from delta_neutral.venues.fake import FakeVenue

EXAMPLE = Path(__file__).parent.parent / "config" / "example.yaml"
TS = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
N = Decimal(1000)


def _venue(**kwargs: Any) -> VenueConfig:
    return VenueConfig.model_validate(
        {"adapter": "fake", "execution": "api", "max_leverage": "5", **kwargs}
    )


def _leg(venue: str, bid: str, ask: str, funding: str, indicative: bool = False) -> Any:
    quote = Quote(
        venue=venue,
        instrument="XAU-USD",
        bid=Decimal(bid),
        ask=Decimal(ask),
        ts=TS,
        indicative=indicative,
    )
    rate = FundingRate(venue=venue, instrument="XAU-USD", rate_pct_per_hour=Decimal(funding), ts=TS)
    return quote, rate


# a: spread 0.2 on a mid of 100 (20 bps), no fee, funding 0.01%/h
# b: spread 0.1 on a mid of 100 (10 bps), 5 bps taker, funding 0.002%/h
MARKET: Market = {
    "XAU-USD": {
        "a": _leg("a", "99.9", "100.1", "0.01"),
        "b": _leg("b", "99.95", "100.05", "0.002"),
    }
}
VENUES = {
    "a": _venue(reward={"per_usd_volume": "1"}),
    "b": _venue(
        fees={"taker": "0.0005"},
        reward={"per_usd_hour": "0.5", "instrument_multiplier": {"XAU-USD": "2"}},
    ),
}


def test_cost_and_reward_match_the_hand_computation() -> None:
    best, worst = score(MARKET, VENUES, N, hold_minutes=60)
    # spread: 1000 * 0.2/100 + 1000 * 0.1/100 = 2 + 1 = 3; fees: 2 * 1000 * 0.0005 = 1 (b only)
    # reward: a 1 * 2000 = 2000; b 0.5 * 1000 * 1h * 2 = 1000; total 3000 in both directions
    for c in (best, worst):
        assert (c.spread_usd, c.fees_usd, c.reward) == (Decimal(3), Decimal(1), Decimal(3000))
    # long b, short a: pays 0.002%, receives 0.01%: 1000 * (0.002 - 0.01) / 100 = -0.08
    assert (best.long_venue, best.short_venue) == ("b", "a")
    assert best.funding_usd == Decimal("-0.08")
    assert best.cost_usd == Decimal("3.92")
    assert best.score == Decimal(3000) / Decimal("3.92")
    # the other direction pays the funding instead: +0.08, cost 4.08, and ranks second
    assert worst.funding_usd == Decimal("0.08") and worst.cost_usd == Decimal("4.08")


def test_funding_scales_with_the_hold() -> None:
    best = score(MARKET, VENUES, N, hold_minutes=180)[0]
    assert best.funding_usd == Decimal("-0.24")  # 3h of -0.08
    assert best.reward == 2000 + 3000  # b's hold reward triples: 0.5 * 1000 * 3h * 2


def test_per_instrument_fee_overrides_the_default() -> None:
    venues = dict(VENUES, b=_venue(fees={"taker": "0.0005", "by_instrument": {"XAU-USD": "0.001"}}))
    assert score(MARKET, venues, N, hold_minutes=60)[0].fees_usd == 2  # 2 * 1000 * 0.001


def test_a_cycle_that_pays_ranks_first_without_a_ratio() -> None:
    market: Market = {
        "XAU-USD": {
            "a": _leg("a", "99.9", "100.1", "0.5"),  # longs pay 0.5%/h: $5 an hour
            "b": _leg("b", "99.95", "100.05", "0"),
        },
        "NVDA-USD": MARKET["XAU-USD"],
    }
    first = score(market, VENUES, N, hold_minutes=60)[0]
    assert (first.instrument, first.long_venue) == ("XAU-USD", "b")
    assert first.cost_usd == Decimal(-1)  # 3 spread + 1 fees - 5 funding
    assert first.score is None and "no_cost" in first.flags


def test_indicative_quotes_are_flagged_not_repriced() -> None:
    market: Market = {
        "XAU-USD": {
            "a": _leg("a", "99.9", "100.1", "0.01", indicative=True),
            "b": MARKET["XAU-USD"]["b"],
        }
    }
    best = score(market, VENUES, N, hold_minutes=60)[0]
    assert best.flags == ("indicative:a",) and best.spread_usd == 3


def test_an_instrument_on_one_venue_or_a_disabled_venue_has_no_candidates() -> None:
    one_venue: Market = {"XAU-USD": {"a": MARKET["XAU-USD"]["a"]}}
    assert score(one_venue, VENUES, N, hold_minutes=60) == []
    assert score(MARKET, {"a": VENUES["a"]}, N, hold_minutes=60) == []


def test_hold_outside_the_risk_limits_fails_at_load() -> None:
    data = load_config(EXAMPLE).model_dump(mode="json")
    data["scoring"]["hold_minutes"] = data["risk"]["max_hold_minutes"] + 1
    with pytest.raises(ValidationError, match="hold_minutes"):
        AppConfig.model_validate(data)


def test_a_collected_tick_round_trips_into_the_scorer(tmp_path: Path) -> None:
    db = open_db(tmp_path / "market.db")
    venues: dict[str, VenueAdapter] = {
        "a": FakeVenue("a", {"XAU-USD": Decimal(2000)}, spread_bps=Decimal(20)),
        "b": FakeVenue("b", {"XAU-USD": Decimal(2000)}, spread_bps=Decimal(10)),
    }
    asyncio.run(collect_tick(db, venues, {"XAU-USD": ["a", "b"]}, N, Decimal("0.01")))
    tick_id, market = load_market(db)
    candidates = score(market, VENUES, N, hold_minutes=60)
    assert tick_id == 1 and len(candidates) == 2
    assert candidates[0].spread_usd == 3  # 20 bps + 10 bps of $1000, read back exactly
    assert "XAU-USD" in format_ranking(candidates)
