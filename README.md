# Delta-Neutral Engine

A venue-agnostic engine for **hedged execution across perpetual-futures venues**: it opens the same instrument on opposite sides of two venues, holds for a configurable time and closes both legs, keeping market exposure near zero. It ranks candidates by a configurable objective per unit of cost, not by raw volume.

- **One interface, N venues.** Each venue is an adapter behind `VenueAdapter`. A conformance suite validates any adapter before it may trade; adding a venue means one adapter plus config, without touching the core.
- **Deterministic core.** Scoring, execution and risk rules are plain, auditable code. No LLM in the execution path.
- **Read-only AI supervisor.** A hand-written tool-use loop answers questions on Telegram about cycles, costs and risk. It cannot act: it proposes, a human approves, deterministic code executes. Every figure it states comes from a tool, and evals check that.

**Status:** one-week build (November 2026), dry-run. Three venues behind one interface: two for trading (one by API, one manual) and a third added at the end of the week with only an adapter and config, validated by the conformance suite. Supervisor in use on Telegram. Live trading only after a clean dry run and an explicit go-ahead.

## How it works

```
venues/  (adapters: quotes, funding, refdata, orders)
   │  canonical models only; venue payloads never leave the adapter
   ▼
collector ──► SQLite ──► scorer (expected cost per cycle) ──► executor (cycle state machine, dry-run)
                              ▲                                    │
                          backtest                             risk monitor (max unhedged time,
                     (predicted vs realised)                   lost leg, margin, weekend rule)
                                                                   │
                         Telegram bot ◄────────────────────────────┘
                              │
                         supervisor (read-only tools, proposes actions) ──► Langfuse traces
```

- **Execution is a venue capability** (`execution: api | manual`, [ADR-0001](docs/adr/0001-manual-execution-venues.md)). A venue without a trading API gets its leg placed by the operator from a Telegram card; the core never branches on a venue name.
- **Money is `Decimal`**, never `float`. Both legs get one size from the USD notional, rounded to the coarser lot step of the two venues and checked against their minimums.
- **Risk rules live in code**, one test per rule: at most 60 s unhedged, close the other leg if one is closed or liquidated, minimum free margin, weekend rule.

## The supervisor agent

- **Loop:** question → model → (tool calls → results → model)\* → answer, capped at 6 model calls ([`supervisor/agent.py`](src/delta_neutral/supervisor/agent.py)). No framework: one file of about 200 lines, prompt included, for any provider.
- **Model-agnostic:** one `LLMClient` interface with httpx adapters for OpenAI-compatible APIs (OpenAI, Gemini) and Anthropic. The model per role is config. Provider turns are replayed verbatim, so reasoning blocks and thought signatures survive.
- **Read-only by construction:** the database is opened `mode=ro`; `propose_action` only records a proposal, shown on Telegram with Approve/Reject.
- **Evals** ([`supervisor/evals.py`](src/delta_neutral/supervisor/evals.py)): 19 questions against a fixture database with a fixed clock, graded by code (figures grounded in tool output, expected figure present, right tool, actions proposed and never claimed, one prompt-injection case), plus style checks kept apart from correctness. Repeats per question and a prompt fingerprint per run.
- **Model choice** ([ADR-0002](docs/adr/0002-model-choice.md)): three small models compared on the evals, latency and cost against a rule set before the results.
- **Production review** ([`supervisor/review.py`](src/delta_neutral/supervisor/review.py)): pulls real runs from Langfuse and applies the same grounding check to every answer.

## Quick start

```bash
uv sync
uv run pytest            # unit, conformance and eval-grader tests (no live calls)
uv run ruff check . && uv run mypy src
```

Config: copy `config/example.yaml` to `config/local.yaml` (gitignored) and put keys in `.env` (see `.env.example`).

| Command | What it does |
|---|---|
| `uv run python -m delta_neutral.quotes` | Live quotes from every venue, side by side (public endpoints) |
| `uv run python -m delta_neutral.collector run` / `report` | Snapshot quotes and funding into SQLite / coverage report |
| `uv run python -m delta_neutral.scorer` | Rank candidate cycles by expected cost |
| `uv run python -m delta_neutral.backtest` | Replay cycles on collected data, predicted vs realised cost |
| `uv run python -m delta_neutral.executor plan` / `status` | Open the best cycle (dry-run) / show cycles |
| `uv run python -m delta_neutral.risk watch` | The risk loop |
| `uv run python -m delta_neutral.bot run` | Telegram bot: cards, confirmations, supervisor |
| `uv run python -m delta_neutral.supervisor "question"` | Ask the supervisor from the terminal |
| `uv run python -m delta_neutral.supervisor.evals --repeats 3` | Run the evals; `--render <run>.jsonl` for the HTML report |

Tests never call live venues; live checks are marked `live` and run on purpose (`-m live`).

## Layout

```
src/delta_neutral/
  models.py, instruments.py   venue-agnostic types, canonical symbols <-> venue symbols
  config.py                   typed, validated config (risk rules, venues, model per role)
  venues/                     VenueAdapter interface, fake venue, one adapter per venue
  collector.py, scorer.py, backtest.py, executor.py, risk.py, price_guard.py
  bot.py, telegram.py, cards.py   Telegram bot and its cards
  llm/                        LLMClient and provider adapters, retries
  supervisor/                 agent loop, read-only tools, evals, eval report, run review
  tracing.py                  OTLP spans to Langfuse
tests/conformance/            the suite every adapter must pass
docs/                         model research, architecture decisions (ADRs)
FAILURES.md                   real failures: symptom, root cause, fix, regression test
```
