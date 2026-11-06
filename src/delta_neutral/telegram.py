"""A minimal Telegram Bot API client over httpx: the four calls the engine bot needs.

Official API: https://core.telegram.org/bots/api. Every method is a POST to
`https://api.telegram.org/bot<token>/<method>` returning `{"ok": ..., "result" | "description"}`.

The token is part of every URL, so it must never reach a log or an exception message: httpx's
request log is silenced by the callers, and errors here are re-raised with the method name only.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

API_URL = "https://api.telegram.org"

# rows of (label, callback_data); callback_data is 1-64 bytes
Buttons = list[list[tuple[str, str]]]


class TelegramError(RuntimeError):
    """A failed Bot API call, described without the URL (which holds the token)."""


class TelegramClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        # long polling holds a request open for up to `timeout`; give it room on top
        self._client = client or httpx.AsyncClient(
            base_url=f"{API_URL}/bot{token}", timeout=httpx.Timeout(10.0, read=40.0)
        )

    async def call(self, method: str, **params: Any) -> Any:
        for _ in range(3):
            try:
                response = await self._client.post(f"/{method}", json=params)
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise TelegramError(f"{method}: {type(exc).__name__}") from None
            if body.get("ok"):
                return body["result"]
            retry = (body.get("parameters") or {}).get("retry_after")
            if response.status_code == 429 and retry:
                await asyncio.sleep(float(retry))  # Telegram's own flood-control wait
                continue
            raise TelegramError(f"{method}: {body.get('description', response.status_code)}")
        raise TelegramError(f"{method}: still rate limited")

    async def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        result: list[dict[str, Any]] = await self.call("getUpdates", **params)
        return result

    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> None:
        """`text` is Telegram HTML: the caller escapes what it did not write (`cards.esc`)."""
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            params["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row]
                    for row in buttons
                ]
            }
        await self.call("sendMessage", **params)

    async def answer(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
