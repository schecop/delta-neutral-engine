# Delta-Neutral Engine

A venue-agnostic engine for **hedged execution across perpetual-futures venues**: it opens the same instrument on opposite sides of two venues, holds, and closes both legs, keeping market exposure near zero.

- **One interface, N venues.** Each venue is an adapter behind `VenueAdapter`; a conformance suite must pass before an adapter may trade.
- **Deterministic core.** Scoring, execution and risk rules are plain, auditable code. No LLM in the execution path.
- **Read-only AI supervisor.** An agent answers questions about positions, costs and risk, and proposes actions that a human approves.

**Status:** Week 1 build, in progress (November 2026). Dry-run first; live only after a clean dry run.

## Quick start

```bash
uv sync
uv run pytest            # unit + conformance suite (against the fake venue)
uv run ruff check . && uv run mypy
```

Config: copy `config/example.yaml` to `config/local.yaml` and edit it (local config is gitignored).

## Layout

```
src/delta_neutral/
  models.py        venue-agnostic domain types (Quote, FundingRate, OrderRequest, Position, MarginState)
  instruments.py   canonical symbols <-> venue symbols
  config.py        typed, validated config (risk rules, venues, model per role)
  venues/base.py   the VenueAdapter interface every venue implements
  venues/fake.py   in-memory venue for tests and dry runs
  llm/base.py      model-agnostic LLM interface (the model per role comes from config)
tests/conformance/ the suite every adapter must pass
docs/              model research, architecture decisions (ADRs)
```
