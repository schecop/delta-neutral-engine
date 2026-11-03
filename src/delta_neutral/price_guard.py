"""Cross-venue price guard: the same instrument should cost about the same everywhere.

Two venues' mids for one canonical instrument differ by a fraction of a percent in normal
conditions. A larger gap almost always means a configuration error, most often a venue whose
contract is a fraction of the instrument without a `contract_size` in config. It skews every
price comparison the scorer makes, so it is surfaced as an alert; it does not exclude the pair.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from delta_neutral.models import Quote


class PriceDivergence(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument: str
    low_venue: str
    high_venue: str
    ratio: Decimal  # low mid / high mid: 0.1 suggests a missing contract_size of 0.1
    divergence: Decimal  # (high - low) / low

    def message(self) -> str:
        return (
            f"{self.instrument}: {self.low_venue} mid is {self.ratio:.4f}x {self.high_venue}'s "
            f"({self.divergence:.2%} apart). Check contract_size for this pair."
        )


def check_divergence(quotes: Iterable[Quote], max_divergence: Decimal) -> PriceDivergence | None:
    """Quotes of one instrument from several venues; returns the widest gap if over the limit."""
    by_mid = sorted(quotes, key=lambda q: q.mid)
    if len({q.instrument for q in by_mid}) > 1:
        raise ValueError("check_divergence compares quotes of a single instrument")
    if len(by_mid) < 2:
        return None
    low, high = by_mid[0], by_mid[-1]
    divergence = (high.mid - low.mid) / low.mid
    if divergence <= max_divergence:
        return None
    return PriceDivergence(
        instrument=low.instrument,
        low_venue=low.venue,
        high_venue=high.venue,
        ratio=low.mid / high.mid,
        divergence=divergence,
    )
