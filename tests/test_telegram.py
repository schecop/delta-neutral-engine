from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from delta_neutral.telegram import API_URL, TelegramClient, TelegramError

TOKEN = "123456:SECRET-TOKEN-VALUE"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> TelegramClient:
    http = httpx.AsyncClient(
        base_url=f"{API_URL}/bot{TOKEN}", transport=httpx.MockTransport(handler)
    )
    return TelegramClient(TOKEN, client=http)


def _call(client: TelegramClient, method: str) -> object:
    async def go() -> object:
        try:
            return await client.call(method)
        finally:
            await client._client.aclose()

    return asyncio.run(go())


def test_buttons_are_sent_as_an_inline_keyboard() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/bot{TOKEN}/sendMessage"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _client(handler)

    async def go() -> None:
        await client.send(42, "hi", [[("Filled at ~1", "fill:1:1"), ("Abort", "abort:1")]])
        await client._client.aclose()

    asyncio.run(go())
    assert sent == [
        {
            "chat_id": 42,
            "text": "hi",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "Filled at ~1", "callback_data": "fill:1:1"},
                        {"text": "Abort", "callback_data": "abort:1"},
                    ]
                ]
            },
        }
    ]


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("boom", request=r)),
        lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}),
    ],
    ids=["network", "api-error"],
)
def test_errors_never_carry_the_token(handler: Callable[[httpx.Request], httpx.Response]) -> None:
    with pytest.raises(TelegramError) as caught:
        _call(_client(handler), "getMe")
    assert TOKEN not in str(caught.value) and "SECRET" not in repr(caught.value)
    assert caught.value.__cause__ is None  # the httpx error (with the URL) is not chained


def test_flood_control_waits_retry_after_then_succeeds() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0.01}})
        return httpx.Response(200, json={"ok": True, "result": "done"})

    assert _call(_client(handler), "sendMessage") == "done"
    assert len(calls) == 2
