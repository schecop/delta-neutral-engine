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

from delta_neutral.models import Execution


class _Strict(BaseModel):
    # A typo in the YAML is an error, not a silent default
    model_config = ConfigDict(extra="forbid")


class VenueConfig(_Strict):
    enabled: bool = True
    adapter: str
    execution: Execution  # required: a forgotten line must not silently mean "api"
    max_leverage: Decimal = Field(gt=0)


class WeekendConfig(_Strict):
    action: Literal["close", "reduce_half", "none"]
    before_us_close_minutes: int = Field(ge=0)


class RiskConfig(_Strict):
    leverage_per_leg: Decimal = Field(gt=0)
    min_free_margin: Decimal = Field(ge=0, le=1)
    rebalance_below_free_margin: Decimal = Field(ge=0, le=1)
    max_unhedged_seconds: int = Field(gt=0)
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


class AppConfig(_Strict):
    mode: Literal["dry_run", "live"]
    venues: dict[str, VenueConfig]
    instruments: dict[str, dict[str, str]]
    risk: RiskConfig
    sizing: SizingConfig
    llm: dict[str, ModelChoice]
    alerts: AlertsConfig

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
        return self


def load_config(path: Path) -> AppConfig:
    with path.open(encoding="utf-8") as f:
        return AppConfig.model_validate(yaml.safe_load(f))
