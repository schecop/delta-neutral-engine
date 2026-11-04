"""Typed, validated configuration.

Risk rules are checked when the config loads, so an inconsistent file fails at startup instead
of halfway through a trading cycle.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from delta_neutral.instruments import VenueSymbol
from delta_neutral.models import Execution


class _Strict(BaseModel):
    # A typo in the YAML is an error, not a silent default
    model_config = ConfigDict(extra="forbid")


class FeesConfig(_Strict):
    """Fees per fill, as a fraction of notional (0.0005 = 5 bps)."""

    taker: Decimal = Field(default=Decimal(0), ge=0, lt=1)
    by_instrument: dict[str, Decimal] = {}  # canonical symbol -> taker fee, where it differs

    def taker_for(self, instrument: str) -> Decimal:
        return self.by_instrument.get(instrument, self.taker)


class RewardConfig(_Strict):
    """What a venue pays back for activity, in the objective's own units (linear, per venue)."""

    per_usd_volume: Decimal = Field(default=Decimal(0), ge=0)  # per USD traded (open + close)
    per_usd_hour: Decimal = Field(default=Decimal(0), ge=0)  # per USD of notional held per hour
    instrument_multiplier: dict[str, Decimal] = {}  # canonical symbol -> multiplier, default 1

    def multiplier(self, instrument: str) -> Decimal:
        return self.instrument_multiplier.get(instrument, Decimal(1))


class VenueConfig(_Strict):
    enabled: bool = True
    adapter: str
    execution: Execution  # required: a forgotten line must not silently mean "api"
    max_leverage: Decimal = Field(gt=0)
    fees: FeesConfig = FeesConfig()
    reward: RewardConfig = RewardConfig()


class WeekendConfig(_Strict):
    action: Literal["close", "reduce_half", "none"]
    before_us_close_minutes: int = Field(ge=0)


class RiskConfig(_Strict):
    leverage_per_leg: Decimal = Field(gt=0)
    min_free_margin: Decimal = Field(ge=0, le=1)
    rebalance_below_free_margin: Decimal = Field(ge=0, le=1)
    max_unhedged_seconds: int = Field(gt=0)
    # Alert when venues' mids for one instrument differ by more than this fraction. Alert only:
    # the usual cause is a missing contract_size, which skews price comparison, not hedge size.
    max_price_divergence: Decimal = Field(gt=0, lt=1)
    min_hold_minutes: int = Field(ge=0)
    max_hold_minutes: int = Field(gt=0)
    weekend: WeekendConfig

    @model_validator(mode="after")
    def _consistent(self) -> RiskConfig:
        if self.rebalance_below_free_margin >= self.min_free_margin:
            raise ValueError("rebalance_below_free_margin must be below min_free_margin")
        if self.min_hold_minutes > self.max_hold_minutes:
            raise ValueError("min_hold_minutes must not exceed max_hold_minutes")
        return self


class SizingConfig(_Strict):
    notional_per_leg_usd: Decimal = Field(gt=0)


class ModelChoice(_Strict):
    """Rule 7: provider and model per role come from config, never from code."""

    provider: str
    model: str


class AlertsConfig(_Strict):
    telegram_enabled: bool = True


class ScoringConfig(_Strict):
    hold_minutes: int = Field(gt=0)  # the hold each candidate is costed at; within risk limits


class CollectorConfig(_Strict):
    interval_seconds: int = Field(ge=5)  # Variational's read API allows 10 req/10s per IP
    db_path: Path


class AppConfig(_Strict):
    mode: Literal["dry_run", "live"]
    venues: dict[str, VenueConfig]
    instruments: dict[str, dict[str, str | VenueSymbol]]
    risk: RiskConfig
    sizing: SizingConfig
    llm: dict[str, ModelChoice]
    alerts: AlertsConfig
    collector: CollectorConfig
    scoring: ScoringConfig

    @model_validator(mode="after")
    def _cross_checks(self) -> AppConfig:
        for name, venue in self.venues.items():
            if venue.enabled and self.risk.leverage_per_leg > venue.max_leverage:
                raise ValueError(
                    f"leverage_per_leg {self.risk.leverage_per_leg} exceeds {name} "
                    f"max_leverage {venue.max_leverage}"
                )
        for canonical, mapping in self.instruments.items():
            unknown = mapping.keys() - self.venues.keys()
            if unknown:
                raise ValueError(f"{canonical} maps to unconfigured venues: {sorted(unknown)}")
        if (
            not self.risk.min_hold_minutes
            <= self.scoring.hold_minutes
            <= self.risk.max_hold_minutes
        ):
            raise ValueError("scoring.hold_minutes must be within risk min/max_hold_minutes")
        return self


def load_config(path: Path) -> AppConfig:
    with path.open(encoding="utf-8") as f:
        return AppConfig.model_validate(yaml.safe_load(f))
