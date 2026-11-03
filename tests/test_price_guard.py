from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from delta_neutral.models import Quote
from delta_neutral.price_guard import check_divergence

LIMIT = Decimal("0.01")
NOW = datetime(2026, 11, 3, tzinfo=UTC)


def _quote(venue: str, mid: str, instrument: str = "US500-USD") -> Quote:
    m = Decimal(mid)
    return Quote(venue=venue, instrument=instrument, bid=m - 1, ask=m + 1, ts=NOW)


def test_venues_in_line_raise_nothing() -> None:
    # Live values from 2026-11-03, with Variational's US500 already scaled x10
    assert check_divergence([_quote("a", "7543"), _quote("b", "7558")], LIMIT) is None


def test_missing_contract_size_is_flagged_with_its_ratio() -> None:
    alert = check_divergence([_quote("a", "754.3"), _quote("b", "7558")], LIMIT)
    assert alert is not None
    assert (alert.low_venue, alert.high_venue) == ("a", "b")
    assert alert.ratio == pytest.approx(Decimal("0.0998"), abs=Decimal("0.0001"))
    assert "contract_size" in alert.message()


def test_just_over_the_limit_alerts_and_at_the_limit_does_not() -> None:
    assert check_divergence([_quote("a", "1000"), _quote("b", "1010")], LIMIT) is None
    assert check_divergence([_quote("a", "1000"), _quote("b", "1011")], LIMIT) is not None


def test_widest_gap_wins_across_three_venues() -> None:
    quotes = [_quote("a", "100"), _quote("b", "1000"), _quote("c", "1001")]
    alert = check_divergence(quotes, LIMIT)
    assert alert is not None and (alert.low_venue, alert.high_venue) == ("a", "c")


def test_fewer_than_two_quotes_cannot_diverge() -> None:
    assert check_divergence([], LIMIT) is None
    assert check_divergence([_quote("a", "100")], LIMIT) is None


def test_mixed_instruments_are_a_caller_bug() -> None:
    with pytest.raises(ValueError, match="single instrument"):
        check_divergence([_quote("a", "100"), _quote("b", "100", "XAU-USD")], LIMIT)
