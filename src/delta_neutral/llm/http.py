"""The one POST every provider adapter makes: JSON in, JSON out, retried on 429 and any 5xx.

On 429 the wait is the provider's own: the Retry-After header, or the `retryDelay` Gemini puts
in its error body ("37s"); otherwise exponential backoff. Waits are capped at MAX_WAIT.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from delta_neutral.llm.base import LLMError

RETRYABLE = {408, 409, 429}  # plus every 5xx, including CDN codes like Cloudflare's 520-524
MAX_WAIT = 60.0
_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def retry_wait(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        header = response.headers.get("retry-after", "")
        hinted = _RETRY_DELAY.search(response.text)
        for value in (header, hinted.group(1) if hinted else ""):
            try:
                return min(float(value) + 1, MAX_WAIT)
            except ValueError:
                continue
    return min(float(2**attempt), MAX_WAIT)


async def post_json(
    client: httpx.AsyncClient, path: str, body: dict[str, Any], *, attempts: int = 4
) -> tuple[dict[str, Any], float]:
    """Returns (response body, latency in ms of the successful attempt)."""
    for attempt in range(attempts):
        started = time.perf_counter()
        response: httpx.Response | None = None
        try:
            response = await client.post(path, json=body)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}"
        else:
            latency = (time.perf_counter() - started) * 1000
            if response.status_code == 200:
                return response.json(), latency
            error = f"HTTP {response.status_code}: {_summary(response)}"
            if not _retryable(response.status_code):
                raise LLMError(error)
        if attempt + 1 < attempts:
            await asyncio.sleep(retry_wait(response, attempt))
    raise LLMError(f"after {attempts} attempts: {error}")


def _retryable(status: int) -> bool:
    return status in RETRYABLE or 500 <= status < 600


def _summary(response: httpx.Response) -> str:
    """The provider's error body names the problem (it never echoes the key); a CDN error page
    is HTML, so only its title is kept."""
    text = response.text
    if "<html" in text[:500].lower():
        title = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        return f"HTML error page ({title.group(1).strip()[:120] if title else 'no title'})"
    return text[:300]
