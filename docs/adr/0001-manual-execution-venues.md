# ADR-0001: Manual execution is a venue capability, not a special case

- **Date:** 2026-11-02
- **Status:** accepted

## Context
Not every venue offers a trading API. One of the first two integrated venues exposes only a public read-only market-data endpoint. Its indicative quotes are cached for up to 600s, and orders can only be placed in its web app. The other venue has a full REST and WebSocket trading API, plus a UAT environment.

The engine must stay venue-agnostic. The pair of venues in use is expected to change, and adding a venue must mean only an adapter plus config.

## Decision
- Each venue declares its **execution capability** in config: `execution: api | manual`.
- For a `manual` venue, the adapter is read-only (market data, plus positions and fills that a human confirms). The executor sends a Telegram alert with the exact order, then waits for the human to confirm the fill.
- **Order of legs:** the manual leg opens first, and the API leg fires only after the manual leg is confirmed. This way the automated leg never sits unhedged while it waits for a person. The risk monitor alerts if the two legs diverge.
- The conformance suite has a variant for manual venues: read-side checks only, and no order placement.
- If a manual venue later ships a trading API, only its adapter and its `execution` line change. The executor does not.

## Alternatives considered
- **Branch on the venue name inside the executor:** quicker today, but it breaks the "adapter plus config" rule the first time the venue pair changes.
- **Automate the venue's web UI:** brittle, usually against the venue's terms, and it puts the account at risk.
- **Integrate only venues that have an API:** it shrinks the venue universe for no architectural gain.

## Consequences
- A 60s max-unhedged rule cannot cover a human leg. The rule applies from the moment the manual fill is confirmed, and a separate timeout covers a missing confirmation.
- Throughput on manual venues is limited by the operator's time.
- Revisit when a manual venue gains a trading API.
