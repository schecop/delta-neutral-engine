from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from delta_neutral.config import AppConfig, load_config
from delta_neutral.instruments import InstrumentRegistry, UnknownInstrumentError
from delta_neutral.models import Execution
from delta_neutral.venues.base import NotTradableError, as_trading
from delta_neutral.venues.factory import AdapterNotAvailableError, build_venues

EXAMPLE = Path(__file__).parent.parent / "config" / "example.yaml"


def _example_dict() -> dict[str, Any]:
    with EXAMPLE.open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


def test_example_config_loads() -> None:
    config = load_config(EXAMPLE)
    assert config.mode == "dry_run"
    assert config.risk.max_unhedged_seconds == 60
    assert config.risk.min_free_margin == Decimal("0.40")


def test_rebalance_threshold_must_be_below_min_free_margin() -> None:
    data = _example_dict()
    data["risk"]["rebalance_below_free_margin"] = "0.60"
    with pytest.raises(ValidationError, match="rebalance_below_free_margin"):
        AppConfig.model_validate(data)


def test_leverage_cannot_exceed_venue_max() -> None:
    data = _example_dict()
    data["risk"]["leverage_per_leg"] = "10"
    with pytest.raises(ValidationError, match="exceeds"):
        AppConfig.model_validate(data)


def test_typo_in_config_is_an_error() -> None:
    data = _example_dict()
    data["risk"]["max_unhedged_secs"] = 60
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_registry_round_trip_and_common_pairs() -> None:
    registry = InstrumentRegistry(
        {"XAU-USD": {"a": "GOLD", "b": "XAUUSD"}, "TSLA-USD": {"a": "TSLA"}}
    )
    assert registry.venue_symbol("XAU-USD", "b") == "XAUUSD"
    assert registry.canonical("a", "GOLD") == "XAU-USD"
    assert registry.common(["a", "b"]) == ["XAU-USD"]
    with pytest.raises(UnknownInstrumentError):
        registry.venue_symbol("TSLA-USD", "b")


def test_registry_rejects_ambiguous_mapping() -> None:
    with pytest.raises(ValueError, match="maps to both"):
        InstrumentRegistry({"XAU-USD": {"a": "GOLD"}, "XAG-USD": {"a": "GOLD"}})


def test_venue_execution_is_required() -> None:
    data = _example_dict()
    del data["venues"]["qfex"]["execution"]
    with pytest.raises(ValidationError, match="execution"):
        AppConfig.model_validate(data)


def test_example_declares_execution_per_venue() -> None:
    config = load_config(EXAMPLE)
    assert config.venues["variational"].execution is Execution.MANUAL
    assert config.venues["qfex"].execution is Execution.API


def test_factory_rejects_execution_mismatch() -> None:
    data = _example_dict()
    data["venues"]["variational"]["execution"] = "api"
    with pytest.raises(ValueError, match="execution"):
        build_venues(AppConfig.model_validate(data), skip_missing=True)


def test_config_may_run_an_api_venue_by_hand() -> None:
    data = _example_dict()
    data["venues"]["qfex"]["execution"] = "manual"
    venues = build_venues(AppConfig.model_validate(data))
    assert venues["qfex"].execution is Execution.MANUAL
    with pytest.raises(NotTradableError):
        as_trading(venues["qfex"])  # no code path can place an order on it


def test_factory_requires_every_adapter_unless_told() -> None:
    data = _example_dict()
    data["venues"]["qfex"]["adapter"] = "not-written-yet"
    config = AppConfig.model_validate(data)
    with pytest.raises(AdapterNotAvailableError, match="not-written-yet"):
        build_venues(config)
    assert list(build_venues(config, skip_missing=True)) == ["variational"]


def test_factory_builds_every_configured_venue() -> None:
    venues = build_venues(load_config(EXAMPLE))
    assert {n: v.execution for n, v in venues.items()} == {
        "variational": Execution.MANUAL,
        "qfex": Execution.API,
    }


def test_contract_size_is_optional_and_defaults_to_one() -> None:
    data = _example_dict()
    data["instruments"]["US500-USD"] = {
        "variational": {"symbol": "US500-PERP", "contract_size": "0.1"},
        "qfex": "US500-USD",
    }
    registry = InstrumentRegistry(AppConfig.model_validate(data).instruments)
    assert registry.venue_symbol("US500-USD", "variational") == "US500-PERP"
    assert registry.contract_size("US500-USD", "variational") == Decimal("0.1")
    assert registry.contract_size("US500-USD", "qfex") == Decimal(1)
    assert registry.canonical("variational", "US500-PERP") == "US500-USD"


@pytest.mark.parametrize(
    "listing",
    [
        {"symbol": "US500-PERP", "contract_size": "0"},
        {"symbol": "US500-PERP", "contract_size": "-0.1"},
        {"symbol": "US500-PERP", "contract_sise": "0.1"},  # typo
    ],
)
def test_bad_contract_size_fails_at_load(listing: dict[str, str]) -> None:
    data = _example_dict()
    data["instruments"]["US500-USD"] = {"variational": listing}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_price_divergence_limit_is_a_fraction() -> None:
    data = _example_dict()
    data["risk"]["max_price_divergence"] = "1"  # 100%: almost certainly meant 1%
    with pytest.raises(ValidationError, match="max_price_divergence"):
        AppConfig.model_validate(data)
