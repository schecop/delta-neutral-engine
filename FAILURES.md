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
