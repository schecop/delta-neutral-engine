# Failure log

Every real failure found while building or running the engine. These become the "debugging a live system" stories.

<!-- Template (copy above the latest entry):
## YYYY-MM-DD · <short title>
- **Symptom:** what was observed
- **Found by:** alert / log / trace / test / by eye
- **Root cause:**
- **Fix:**
- **Regression test / eval added:**
- **Class:** venue API · data · timing/latency · risk rule · config · supervisor (prompt/tool/model) · other
-->

## 2026-11-03 · Flaky test caused by a feed that did not wait for its own shutdown
- **Symptom:** a Variational quote test, which never touches a WebSocket, failed about one full run in two and always passed alone.
- **Found by:** test. Looping the suite until it failed and reading the full traceback: `ResourceWarning: unclosed socket` on 127.0.0.1, raised as an error by `filterwarnings = error` inside whichever test the garbage collector happened to run in. A first detection loop gave a false positive because `grep failed` also matches `xfailed`.
- **Root cause:** `WebSocketFeed.aclose()` cancelled the background task but did not await it. It returned while the connection was still closing; the local test server then shut down under it, and the half-closed socket was only reclaimed later by the GC. In production the same bug would leave a dangling connection on every collector stop.
- **Fix:** `aclose()` cancels and then awaits the task, so the feed is stopped for real when the call returns.
- **Regression test / eval added:** `tests/venues/test_qfex.py::test_aclose_waits_for_the_connection_to_close` asserts the task is done when `aclose()` returns. Checked against the old code, where it fails. A first version of the test slept before asserting on the server's connection count; that sleep gave the cancelled task time to clean up, so the test passed on the buggy code and was discarded.
- **Class:** timing/latency

## 2026-11-03 · Adapters never closed their HTTP clients
- **Symptom:** the live tests reported "2 passed", but pytest then raised `ResourceWarning: unclosed socket` to both venues' public IPs at exit.
- **Found by:** test, reading the full output of `pytest -m live` instead of only its last line, which had hidden it in earlier runs.
- **Root cause:** each adapter creates its own `httpx.AsyncClient` (and QFEX its WebSocket feed), but the venue contract had no way to release them, so nothing ever did. A long-running collector would have leaked a connection per restart of an adapter.
- **Fix:** `aclose()` is now part of `VenueAdapter`. Each adapter closes what it created and leaves injected clients to their owner; the quotes command closes every venue in a `finally`.
- **Regression test / eval added:** `test_aclose_releases_the_adapter` in the conformance suite (every adapter); the live tests run inside one event loop and close the adapter in `finally`, and now exit with no warnings.
- **Class:** venue API

## 2026-11-03 · Cache stampede: the adapter tripped the venue's rate limit by itself
- **Symptom:** the first live collector run logged a burst of `429 Too Many Requests` from the read-only stats endpoint in every tick, and ticks took about 4.4s.
- **Found by:** log. The collector's request log showed several stats requests within the same second, in a tick that should need one.
- **Root cause:** the collector reads every pair on every venue at once, and the adapter's snapshot cache was checked without a lock. Each of the nine concurrent reads (three pairs × probe quote, sized quote, funding) found the cache cold and fetched on its own. That is far past the endpoint's 10 requests per 10s per IP, so the adapter was rate-limiting itself. The QFEX refdata cache had the same pattern on its first load.
- **Fix:** single-flight caching. A per-event-loop lock (`venues/_sync.py`) wraps the cache check and the fetch: the first read fetches, the others wait and reuse its result. Ticks dropped to about 0.4s with no 429s.
- **Regression test / eval added:** `test_concurrent_reads_share_one_request` (Variational) and `test_concurrent_reads_share_one_refdata_request` (QFEX) assert one HTTP request for many concurrent reads. Their first version passed on the buggy code: a synchronous mock handler never yields to the event loop, so requests never overlapped. The handler now awaits a short sleep to model network latency, and both tests fail on the old code (6 and 3 requests).
- **Class:** venue API

## 2026-11-04 · A machine sleep hid inside one tick, and the report called it OK
- **Symptom:** a one-hour unattended collector run stopped writing ticks after about 17 minutes, yet `collector report` printed "max gap 30s: OK". The last tick had a duration of about 13 hours and a price-divergence alert of nearly 3% on a pair that normally agrees within a small fraction of a percent.
- **Found by:** reviewing the run by hand: the `ticks` table's duration column and the timestamps of the events, all written at the same moment the next morning.
- **Root cause:** the laptop went to sleep in the middle of a tick. One venue's read had already returned (before the sleep); the other's HTTP request was still in flight and completed, or timed out, only on wake. That tick therefore stored two quotes taken about 13 hours apart as one snapshot, and the price guard correctly flagged the overnight market move as a divergence. Two bugs of ours let this through: ticks had no deadline, so a tick could last any length of time; and the report measured gaps between tick *starts*, so time spent inside one tick was invisible to it.
- **Fix:** each tick has a deadline equal to the collection interval. It is enforced by a timeout on the gather and also checked on the monotonic clock afterwards, because after a sleep every timer fires at once. A late tick keeps its `ticks` row and a `late_tick` event but none of its quotes. The report now measures the time between the *ends* of ticks with data, and counts trailing time with no data as a gap too. On the real database it now reports a 47,612s gap. Operationally, unattended runs need the machine kept awake.
- **Regression test / eval added:** `test_report_sees_a_gap_hidden_inside_a_long_tick` (the real durations), `test_a_tick_slower_than_its_deadline_stores_no_quotes` (simulated sleep: no quotes stored and no false divergence alert), and `test_a_hung_venue_cannot_hold_a_tick_past_its_deadline`. All three fail on the old code.
- **Class:** timing/latency

## 2026-11-05 · The first real cycle was asked to trade sizes no venue accepts
- **Symptom:** in the first hand-placed cycle the two open instructions asked for six-decimal sizes that differed between the legs. Neither venue accepts those sizes: their order size step is coarser. The operator placed the nearest size the venues accept, the same on both legs, and confirmed with the button. The engine recorded the instruction sizes instead, so the close instructions it would send next asked to close more than was open on both venues.
- **Found by:** the operator, comparing the venues' order histories with the cycle in `engine.db` before the close.
- **Root cause:** sizing ended at "USD notional divided by each venue's own mid". That rule is right for safety (a wrong `contract_size` cannot skew a hedge sized in venue units), but it produced raw numbers: nothing knew a venue's size step or minimum, and nothing made both legs equal. The confirm flow then trusted the instruction size unless the operator typed another, and there was no way to correct a fill from the chat.
- **Fix:** the instrument registry holds each venue's lot size and minimum (venue units; one venue publishes them in its reference data, the other only shows them in its app). Both legs now get one size: each leg's own-quote size is computed as before; if they disagree beyond the price-divergence limit, the cycle is refused (a wrong `contract_size`, previously only an alert); otherwise the smaller is rounded down to the coarsest lot step and checked against each venue's minimum. Instructions show the notional after rounding. `/amend` corrects a recorded fill (size, price or time) from the chat, resizes an unfilled close leg, recomputes a closed cycle's summary, and keeps the old values in the event log. The real cycle was corrected by hand before its close, with an audit event.
- **Regression test / eval added:** `test_both_legs_get_one_size_rounded_to_the_coarsest_lot`, `test_legs_that_disagree_on_size_refuse_the_cycle`, `test_a_notional_below_a_venues_minimum_refuses_the_cycle`, `test_amending_an_open_legs_size_resizes_its_close`, `test_amending_a_closed_cycles_fill_recomputes_its_summary`, and `test_amend_corrects_a_fill_from_the_chat`.
- **Class:** venue API

## 2026-11-06 · The unhedged alarm measured the operator's typing, not the market
- **Symptom:** in a hand-placed cycle the risk monitor raised "unhedged for 74s (max 60s)" at the open and "unhedged for 105s" at the close. The venues' histories showed both legs filled 1s apart at the open and 3s apart at the close: there was never real exposure.
- **Found by:** alert, then comparing the fill times recorded in `engine.db` with the time of the risk event.
- **Root cause:** the rule measures from the first recorded fill until every leg of the phase is recorded, and treats an unrecorded leg as unfilled. For an API leg that is true: the engine records the fill when the venue returns it. For a manual leg it is not: the fill is recorded only when the operator types it, and typing two confirmations with prices and times takes longer than 60s. So the alarm fired on every manual cycle. Its action, retrying the missing leg, also does nothing for a leg only a human can place. An alarm that fires every time trains the operator to ignore it, which is the real danger.
- **Fix:** when every leg still missing in a phase is manual, the breach keeps the 60s limit but becomes `confirm_fill`: it names the unconfirmed venue and says "if it filled, /fill it; if not, you are unhedged", and retries nothing. Missing API legs keep `resume` and the escalation to an unwind. The limit stays at 60s because a manual leg that truly did not fill is real exposure. The full fix needs live position reads: once a venue reports positions by API, a leg it already holds counts as filled without waiting for the operator. Reviewing this exposed the opposite gap: when the manual leg goes first and the other leg is API, the API leg is placed only on the confirmation, so a slow confirmation is real unhedged time that no pass ever sees (nothing is recorded until both legs fill at once). A completed phase whose fill times are further apart than the limit is now recorded once as `exceeded` and alerted. Instructions now say to tap the confirm button the moment the venue fills and correct price or time later with `/amend`, which turns minutes of exposure into seconds.
- **Regression test / eval added:** `test_an_unconfirmed_manual_leg_asks_for_its_fill_instead_of_retrying`, `test_a_late_confirmation_is_recorded_once_as_unhedged_time_exceeded`.
- **Class:** risk rule

## 2026-11-06 · The eval grader failed correct answers
- **Symptom:** the first supervisor eval run marked right answers as wrong: "12.000 USD" of volume was "not grounded", and "Para que se ejecute, apruébala en Telegram" counted as claiming an action had been done.
- **Found by:** by eye, reading the failed answers in the report instead of trusting the score. Every model "failed" the same cases, which pointed at the grader, not the models.
- **Root cause:** the grader assumed English number formats and matched action verbs without grammar. In Spanish "12.000" is twelve thousand and "3,28" is 3.28, so the number parser read the wrong value. The action check matched "ejecute" (subjunctive, "so that it runs") as "executed". A code grader is only as good as its parsing, and a grader bug looks exactly like a model bug.
- **Fix:** `readings()` returns every value a written number can mean in either convention ("12.000" is 12000 or 12.000) and a match on either counts. `claims_action()` matches the accented past tense ("ejecuté"), not the subjunctive ("ejecute"), and skips negated claims ("no he cerrado").
- **Regression test / eval added:** `test_thousands_separators_in_both_conventions`, `test_spanish_and_english_thousands_are_grounded`, `test_a_subjunctive_is_not_a_claim`, `test_claim_detection`.
- **Class:** supervisor (prompt/tool/model)

## 2026-11-08 · Reviewing real supervisor runs: a retired API, then false alarms
- **Symptom:** the first script to pull real Telegram runs from Langfuse got `410 Gone`. Once it ran, every run showed "tools: none", the eval runs were mixed with the real ones, and correct answers were flagged for figures "not from tools" ("24 horas", "30 días").
- **Found by:** running the review on the operator's first real questions and checking them by hand against `engine.db`.
- **Root cause:** four separate faults. The v1 traces read API is retired on Langfuse Cloud. The v2 observations query filtered by trace name returns only the root span, so the tool spans were never fetched. Metadata comes back with flat keys (`attributes.model`), not nested. And the grounding check only looked at tool results, but "24" was a tool argument the model chose, and "30 days" was the model converting 720 hours by itself.
- **Fix:** read `GET /api/public/v2/observations` with cursor pagination; fetch the roots first, then every observation of each trace; read flat metadata keys; drop runs tagged `eval_case`. Grounding now counts the question and the tool arguments as sources. `cost_summary` returns `window_days`, so the day count is a tool figure instead of the model's arithmetic.
- **Regression test / eval added:** `test_review_parses_a_langfuse_trace_and_flags_figures_no_tool_gave` (v2 rows in any order, flat metadata, eval runs filtered); `window_days` asserted in `test_the_fixture_holds_the_documented_figures`.
- **Class:** supervisor (prompt/tool/model)
