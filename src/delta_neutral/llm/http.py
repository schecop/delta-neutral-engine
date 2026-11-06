"""The one POST every provider adapter makes: JSON in, JSON out, retried on 429 and 5xx."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from delta_neutral.llm.base import LLMError

RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}


async def post_json(
    client: httpx.AsyncClient, path: str, body: dict[str, Any], *, attempts: int = 3
) -> tuple[dict[str, Any], float]:
    """Returns (response body, latency in ms of the successful attempt)."""
    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            response = await client.post(path, json=body)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}"
        else:
            latency = (time.perf_counter() - started) * 1000
            if response.status_code == 200:
                return response.json(), latency
            # the provider's error body names the problem; it never echoes the key
            error = f"HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code not in RETRYABLE:
                raise LLMError(error)
        if attempt + 1 < attempts:
            await asyncio.sleep(2**attempt)
    raise LLMError(f"after {attempts} attempts: {error}")
