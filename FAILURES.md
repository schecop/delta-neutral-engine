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
