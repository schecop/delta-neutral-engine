# Model research (rule 7)

No default model. Each role gets the model that wins on its own job, on quality, latency and cost. Data must come from a current search (provider pricing pages, benchmarks), with the date checked.

## Roles in this project

| Role | What it does | What matters most |
|---|---|---|
| Supervisor | Answers questions on Telegram using read-only tools; proposes actions for approval | Reliable tool use; never invents numbers; cost per day. Latency: seconds are fine |
| Daily report | Summarises the day's cycles, costs and risk events | Cost (batch is fine); faithful numbers |

**What the task needs.** The supervisor's job is direct: pick the right read-only tool, then repeat its figures faithfully. It needs no planning or deep reasoning, so we start from small, cheap models and run them with reasoning off or at the lowest effort. The main failure to guard against is a misquoted or invented figure, not weak reasoning. The evals measure exactly that.

## Candidates — Supervisor (researched on 2026-11-02)

Prices are USD per 1M tokens, standard tier, from the official pricing pages on 2026-11-02. Speed figures come from Artificial Analysis, with the setting noted.

| Model | Provider / hosting | Price in / out (per 1M tokens) | Time to first token, tokens/s | Context | Tool use | Notes |
|---|---|---|---|---|---|---|
| **gpt-4o-mini** | OpenAI API | $0.15 / $0.60 · cache $0.075 · batch $0.075 / $0.30 | Not measured | 128k | Mature function calling | **Older-generation baseline.** Any newer model has to beat it. |
| **gpt-5-nano** | OpenAI API | **$0.05 / $0.40** · cache $0.005 · batch $0.025 / $0.20 | Not measured | 400k | Function calling | **Cheapest candidate.** It is a reasoning model, so set reasoning effort to minimal. |
| **Gemini 3.5 Flash-Lite** | Google AI Studio / Vertex | $0.30 / $2.50 · batch $0.15 / $1.25 · free tier available | Not measured | 1M | Function calling | The newest Flash-Lite. Google is replacing 2.5 Flash with it from 2026-12-01. The free tier helps for evals, but free-tier data may be used by Google. |
| **Claude Haiku 5.5** | Anthropic API | $0.10 / $0.50 for prompts ≤100k (×5 above that) · cache $0.01 · batch $0.05 / $0.25 | 137 tok/s, TTFT 12.6s at medium effort. **Use reasoning off.** | 1M (price ×5 past 100k) | Haiku 4.5 scored 68.7 on BFCL; no 2026 score yet | Cheap and recent. Keep prompts well under 100k. |
| *Reserve:* DeepSeek V4.1 Flash (`deepseek-flash`) | DeepSeek API (open weights) | $0.15 / $0.60 off-peak, $0.30 / $1.20 peak | 216 tok/s, TTFT 1.1s | 1M | Native tool calling | Fast. The trade-off is that data goes to a China-hosted API. |
| *Reserve:* Gemini 2.5 Flash-Lite | Google AI Studio | $0.10 / $0.40 · free tier | Not measured | 1M | Function calling | Cheap fallback if 3.5 Flash-Lite is not needed. |

**Considered and dropped:**
- **Gemini 2.5 Flash:** deprecated. From 2026-12-01 its requests route to 3.5 Flash-Lite, so it would disappear mid-project.
- **deepseek-v3:** no longer on DeepSeek's official API, which only offers `deepseek-flash` (V4.1) and `deepseek-v4-pro`. The legacy `deepseek-chat` and `deepseek-reasoner` endpoints were retired in 2026-07. It would only be available through third-party hosts.
- **Claude Sonnet 5.5, Gemini 3.8 Flash, GPT-5.4 mini:** too much model for a direct tool-lookup task in a PoC. Revisit only if every small model fails the evals.
- **Local open-weight models through Ollama:** these cost $0 in tokens. They are the weakest at reliable tool use, and the hardware hasn't been checked yet. They are an option only if privacy becomes the priority.

Sources (all checked 2026-11-02):
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
- [Google AI Studio pricing](https://aistudio.google.com/docs/pricing), which also states the deprecation notice for Gemini 2.5 Flash
- [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing)
- Speed: [Artificial Analysis](https://artificialanalysis.ai/models/comparisons/claude-haiku-5-5-medium-vs-deepseek-v4-1-flash)
- Tool-use benchmarks for the newest versions are not available independently, so **our own evals decide.**

### Rough cost per day (supervisor)

Assumptions: 30 questions a day, about 3 model turns each, about 8k input tokens per turn (system prompt, tools and tool results), and 400 output tokens per turn. That gives roughly **720k input and 36k output tokens a day**, before caching.

| Model | Cost per day | Cost per month |
|---|---|---|
| gpt-5-nano | ≈ $0.05 | ≈ $1.50 |
| Claude Haiku 5.5 | ≈ $0.09 | ≈ $2.70 |
| Gemini 2.5 Flash-Lite (reserve) | ≈ $0.09 | ≈ $2.70 |
| gpt-4o-mini | ≈ $0.13 | ≈ $3.90 |
| DeepSeek V4.1 Flash (reserve) | ≈ $0.15 | ≈ $4.50 |
| Gemini 3.5 Flash-Lite | ≈ $0.31 | ≈ $9.30 |

For comparison, at taker fees of 0.05% to 0.10%, each $1,000 of notional per leg costs about $1 to $2 per round trip in fees alone, plus spread. Every shortlisted model costs less per day than the fees on a single $1,000 round trip.

### Shortlist for the D4 eval
1. **gpt-4o-mini:** the older-generation baseline.
2. **Gemini 3.5 Flash-Lite** (`gemini-3.5-flash-lite`, via the OpenAI-compatible endpoint)
3. **Claude Haiku 5.5** (`claude-haiku-5-5`), at effort `low`

**Update 2026-11-06:** gpt-5-nano is now listed as *Deprecated* on [its model page](https://developers.openai.com/api/docs/models/gpt-5-nano); OpenAI recommends GPT-5.6 Luna for new low-cost work ($0.20 / $1.20). Decision: drop gpt-5-nano and add no replacement; the eval runs on the three models above.

**Rule:** the cheapest model that scores 100% on figures correct and 100% on refusing unapproved actions wins. Ties go to lower p95 latency. Daily report: the same shortlist, run through each provider's batch API (half price).

## Comparison on evals (fill in D4)

| Model | Figures correct (%) | Correct tool use (%) | Refuses unapproved actions (%) | Latency p50 / p95 | Cost per question | Cost per day (est.) |
|---|---|---|---|---|---|---|
| gpt-4o-mini | | | | | | |
| Gemini 3.5 Flash-Lite | | | | | | |
| Claude Haiku 5.5 | | | | | | |

**Choice:** → record in `docs/adr/` as `model-choice`.
