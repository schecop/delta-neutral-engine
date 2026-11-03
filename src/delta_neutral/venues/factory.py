"""Builds venue adapters from config: the only place that maps an `adapter:` name to a class.

Adding a venue means one adapter module plus one line in `_BUILDERS`. The declared `execution`
in config must match what the adapter is, so a config typo can never make a manual venue look
tradable (or the reverse).
"""

from __future__ import annotations

from collections.abc import Callable

from delta_neutral.config import AppConfig
from delta_neutral.instruments import InstrumentRegistry
from delta_neutral.venues.base import VenueAdapter
from delta_neutral.venues.qfex import QfexVenue
from delta_neutral.venues.variational import VariationalVenue

_BUILDERS: dict[str, Callable[[str, InstrumentRegistry], VenueAdapter]] = {
    "variational": lambda name, registry: VariationalVenue(registry, name=name),
    "qfex": lambda name, registry: QfexVenue(registry, name=name),
}


class AdapterNotAvailableError(LookupError):
    """Config names an adapter that has not been written yet."""


def build_venues(config: AppConfig, *, skip_missing: bool = False) -> dict[str, VenueAdapter]:
    """Enabled venues by name. Raises if any enabled venue cannot be built faithfully.

    `skip_missing` leaves out venues whose adapter does not exist yet (read-only tools during
    the build); the executor never passes it.
    """
    registry = InstrumentRegistry(config.instruments)
    venues: dict[str, VenueAdapter] = {}
    for name, venue_config in config.venues.items():
        if not venue_config.enabled:
            continue
        builder = _BUILDERS.get(venue_config.adapter)
        if builder is None:
            if skip_missing:
                continue
            raise AdapterNotAvailableError(f"{name}: no adapter named {venue_config.adapter!r}")
        venue = builder(name, registry)
        if venue.execution is not venue_config.execution:
            raise ValueError(
                f"{name}: config says execution={venue_config.execution}, "
                f"adapter is {venue.execution}"
            )
        venues[name] = venue
    return venues
