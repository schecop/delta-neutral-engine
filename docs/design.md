# Why it's built this way

The design decisions behind the engine, as questions an engineer would ask about it. The lasting ones also have an ADR in [`adr/`](adr/), and real failures are in [`FAILURES.md`](../FAILURES.md).

## 1. Why one adapter interface plus a conformance suite?

It is what makes the engine scale to more venues. A venue plugs in like a socket: write an adapter that follows `VenueAdapter` and passes the conformance suite, and nothing else changes. Above the adapters everything speaks canonical models and symbols, and what differs between venues (for example whether orders go by API or by hand) is a property in config, never an `if` on a venue's name.

Without it, every place where the core talks to a venue (scorer, executor, risk) would need per-venue checks and translations. That is error-prone and does not scale: each new venue could break the ones before it.

To check that the suite really catches a bad adapter, it was run against 7 deliberately broken adapters, and each one failed the test for its rule. The real test came at the end of the week: Hyperliquid, the third venue, went in with an adapter and config, without touching the core.

## 2. How does it guarantee no more than 60 seconds unhedged?

With checks in code. A risk loop checks every open cycle every few seconds, applies the rules and records each decision, so a human (and the supervisor) can audit it. No prompt or LLM is involved.

- **A leg still missing after 60 s** is retried. A leg on a manual venue is not retried, because only the operator knows whether it filled, so the engine asks them to confirm it.
- **If the retry fails or times out**, an urgent alert says the filled leg must be closed.
- **Legs that filled more than 60 s apart**, for example because a manual confirmation came late, are still recorded as an exceeded limit. Exposure is never hidden.
- **If a leg is closed or liquidated**, the rule closes the other one. This needs a venue that reports positions by API.
- **Free margin and the weekend rule** complete the set: no new cycles below the minimum margin, and nothing open across the weekend.
- **Partial fills are not handled in this PoC.** Today a leg is filled or not, and nothing compares the filled sizes of the two legs (see *Limitations* in the README).

## 3. Polling or streaming for market data, and what is the latency budget?

It depends on what each venue offers. Streaming always has the latest price, but it has a cost: reconnects, state and a clean shutdown. One of the week's real bugs came from not waiting for a connection to close ([FAILURES.md](../FAILURES.md), 2026-11-03).

| Venue | Market data | Why |
|---|---|---|
| QFEX | WebSocket | The venue pushes a full market snapshot; the adapter reads it from memory. |
| Variational | REST polling | The only public API is one stats endpoint, limited to 10 requests per 10 s. Reads go through a short cache and a single shared request. The venue's own quotes are indicative and cached for up to 600 s, which makes it a poor fit for automated execution. |
| Hyperliquid | REST polling | Order book and market context with caches of a few seconds. It has a WebSocket, but for one snapshot every 30 s REST is enough and simpler. |

From a price change to a decision:

| Step | Time |
|---|---|
| Data age | 1-2 s on QFEX and Hyperliquid; about 17 s median on Variational, up to 600 s |
| Capture | One snapshot every 30 s; the scorer decides on the latest |
| Execution with a manual leg | About 50 s from the instruction to the open |

Latency matters between API venues. When a manual venue is involved it is only indicative, because the human step is far slower than the data. Today data latency changes no decision. It would between two API venues: there both feeds would move to WebSocket and the time between the two legs would be measured.

## 4. Why no LLM in the execution path, and what does the supervisor do?

For execution an LLM is not needed, adds no value and adds unpredictability. What runs is deterministic code. The LLM only fills the arguments of read-only tools when it decides to call them.

- **It proposes, it does not act.** It can propose closing or aborting a cycle. The proposal (which cycle and why) shows on a Telegram card, a human approves it, and deterministic code executes it.
- **Safety is in the architecture, not in the prompt.** However much the model wants to act, or if a prompt injection tells it to, the system gives it no way to. In the evals one model gave in to an injection and said it would abort a cycle. Nothing happened.
- **Read-only by construction.** The tools open the database in read-only mode, so no malicious instruction can change the data.
- **No maths in the model.** The tools do every total, average and conversion in `Decimal`; the supervisor copies the results.
- **Checked by evals.** Every figure in an answer must appear in what the tools returned. The evals also check that the expected figure is there, the right tool was used, and an action is proposed and never claimed as done. The same check runs on a fixed question set against a fixture database and on real Telegram answers pulled from the traces.

## 5. Which model runs the supervisor, and why?

Gemini 3.5 Flash-Lite. Its answers are accurate, its style is the cleanest and its latency the lowest (after tuning the prompt one change at a time), and it runs on a free tier.

Three small models were compared on the same eval set, against a rule fixed before seeing results: 100% correct figures, 100% of actions never claimed as done, end-to-end p95 of at most 5 s, then the cheapest.

| Model | Result | p95 | Cost per question |
|---|---|---|---|
| Claude Haiku 5.5 | The only one at 100% on everything | 4.5 s | $0.00048 |
| gpt-4o-mini | Cheapest paid option, but gave in to the prompt injection in every run: it said it would act and proposed nothing, the worst failure for this role | 3.6 s | $0.00029 |
| Gemini 3.5 Flash-Lite | 97.2% on figures: one omitted duration (a missing figure, not a wrong one). After tuning on real Telegram questions: 57/57, then 98% in the final run | 2.1 s | $0.00075, or $0 on the free tier |

Under the strict rule Haiku wins. Choosing Gemini is a deliberate, documented exception ([ADR-0002](adr/0002-model-choice.md)): it is free, its one miss is an omission, and action safety does not depend on the model.

**What would make me switch:** the free tier or its terms changing, hitting its limits (15 requests per minute, 500 per day), sustained worse latency, or real answers starting to miss key facts. The replacement would be Haiku, not the cheapest, because gpt-4o-mini is ruled out by the injection result. Switching is a config change, and the evals re-run in one command.
