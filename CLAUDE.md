# Delta-Neutral Engine — instructions for Claude Code

## What this is
A venue-agnostic engine that opens hedged positions (same asset, opposite sides) on two perpetual-futures venues, holds them for a configurable time and closes both legs, keeping market exposure near zero. It optimises a configurable objective per euro of cost, not raw volume. Adding a venue means writing one adapter plus config, and passing the conformance suite, without touching the core.

A one-week build (November 2026). **Lead skills:** API integrations (N venues behind one interface), real-time systems, and debugging a live system. **Also practised:** a hand-written agent loop (the supervisor), evals, and observability.

**Done when:** a third venue can be added with only an adapter and config, and the conformance suite validates it; and the supervisor answers on Telegram with numbers that match the logs, with its runs traced.

Key files: `FAILURES.md` (real failures and fixes), `docs/model-research.md` (model choice), `docs/adr/` (lasting decisions), `config/example.yaml` (all tunables, with example values).

Operator context (plan, progress, venue verification, strategy) lives in `private/`, a separate private repo that is gitignored here:

@private/CLAUDE.md

## Architecture rules (non-negotiable)
- **No LLM in the execution path.** Scoring, execution and risk are deterministic and auditable.
- **The supervisor agent is read-only.** It is a hand-written tool-use loop with read-only tools (positions, cycles, risk events, funding, costs). It answers questions and writes the daily report. Any action (close, kill switch) is only **proposed**: a human approves it on Telegram, and then deterministic code executes it.
- **Numbers come from tools, never from the model.** The supervisor's evals check every figure it states against the logs.
- **Every adapter passes `tests/conformance/` before it may trade live.**
- **Execution is a venue capability** (`execution: api | manual`, see ADR-0001). The core never branches on a venue name.
- **Dry-run is mandatory before live.** `mode: live` only after a clean dry-run period and the operator's explicit go-ahead.

## How we work: fast mode, demo first
Pablo learns by seeing things work, then reasoning about why, then rebuilding with more understanding. Speed matters, but nothing should stay a black box.

1. **Claude builds most of the code; Pablo directs, runs, breaks and fixes.** No tutor mode, no guided exercises.
2. **Demo first, then why.** When a piece works, show it running (a command, a test, an output). Then explain in 5-10 lines how it works and *why it's built this way*: the main alternative and why not. Point to the 2-3 lines that matter most.
3. **Small vertical slices.** Each step ends in something runnable. Prefer a rough end-to-end path over a polished half.
4. **Ask before big forks.** For library, provider, data model or architecture choices, give a recommendation with one line of trade-offs and wait for a yes. Everything else: proceed.
5. **Must before stretch.** Track the must scope, and propose cutting stretch items when the budget is at risk.

## No default model
Latency and cost matter as much as quality. Any model is allowed: open-source, free, cheap or frontier.
- **Research first:** `docs/model-research.md` lists the candidates per role with price in/out, latency, context, tool-use quality and hosting. Search for current data; never answer from memory.
- **Model-agnostic code:** everything goes through `delta_neutral.llm.LLMClient`. The model per role lives in `config/*.yaml` (`llm:`), never in code. Provider adapters normalise tool-use formats.
- **Organise for cost:** the supervisor is not latency-critical and the tools do the maths, so start from the smallest model that passes the evals. The daily report can run in batch.
- **Decide with evals:** compare the shortlisted models on the supervisor eval set (correct figures, correct tool use, refuses to act without approval), latency and cost, and record the result in an ADR `model-choice`.
- **Product cost metric:** supervisor cost per day vs trading cost per cycle.

## Records (keep them as you go, not at the end)
- **`FAILURES.md`:** every real failure: symptom, how it was found, root cause, fix, regression test added. It is public, so describe the engineering and never sizes, pairs or strategy parameters.
- **`docs/adr/`:** one short ADR per lasting decision (copy `0000-template.md`). Public ADRs cover architecture only; venue-terms or strategy decisions go to `private/adr/`.

## Conventions
- Python 3.12+, uv, ruff, mypy (strict), pytest, `src/` layout. Money and sizes are `Decimal`, never `float`.
- Venue-native payloads never leave an adapter: everything above `venues/` speaks `delta_neutral.models` and canonical instrument symbols.
- Observability from day 1: structured logs per cycle (leg timestamps, leg skew, unhedged seconds, costs, funding), and traces for every supervisor LLM and tool call (Langfuse or OpenTelemetry).
- Tests never call live venues; use `venues/fake.py` or recorded responses. Live checks are separate, marked, and run on purpose.
- Never commit secrets. Keys go in `.env` (gitignored) and are listed in `.env.example`.
- Never commit on your own: show `git status`, propose a conventional commit (`feat:`, `fix:`, `test:`, `docs:`, `chore:`) with a why-body, and commit on Pablo's go-ahead.

## Safety (real money)
- Only official domains and official API endpoints, each verified independently. Never use a URL found in a chat, forum or third-party post without verifying it.
- A dedicated wallet only. Private keys and API secrets never appear in code, logs, traces, prompts or commits.
- Claude never places, modifies or closes a live order, changes `mode` to `live`, or raises size or leverage limits without Pablo's explicit confirmation in that moment.
- Risk rules are enforced in code, not in prompts: max 60s unhedged, close the other leg if one leg closes or is liquidated, minimum free margin, the weekend rule.
