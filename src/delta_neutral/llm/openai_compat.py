"""Chat Completions adapter: OpenAI, and Gemini through its OpenAI-compatible endpoint.

    POST {base_url}/chat/completions   Authorization: Bearer <key>

Wire format (OpenAI API reference, Chat Completions):
- tools: [{"type": "function", "function": {name, description, parameters}}]
- an assistant turn that calls tools: {"role": "assistant", "content": ..., "tool_calls":
  [{"id", "type": "function", "function": {"name", "arguments": "<JSON text>"}}]}
- each result: {"role": "tool", "tool_call_id": ..., "content": ...}
- finish_reason "tool_calls" | "stop" | "length"; usage.prompt_tokens / completion_tokens

`arguments` arrives as JSON *text* written by the model, so it can be invalid: that becomes a
ToolCall with `bad_arguments`, and the loop answers it with an error result instead of crashing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from delta_neutral.llm.base import (
    AssistantMessage,
    LLMResponse,
    Message,
    Stop,
    ToolCall,
    ToolResults,
    ToolSpec,
    UserMessage,
)
from delta_neutral.llm.http import post_json

_STOPS: dict[str, Stop] = {"tool_calls": "tool_use", "length": "max_tokens"}


class OpenAICompatClient:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        max_tokens_field: str = "max_completion_tokens",
        options: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self._max_tokens_field = max_tokens_field
        self._options = options or {}
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0),
        )

    def request_body(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
    ) -> dict[str, Any]:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if isinstance(m, UserMessage):
                wire.append({"role": "user", "content": m.content})
            elif isinstance(m, AssistantMessage):
                wire.append(m.raw if m.provider == self.provider and m.raw else _assistant(m))
            elif isinstance(m, ToolResults):
                wire += [
                    {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
                    for r in m.results
                ]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": wire,
            self._max_tokens_field: max_tokens,
            **self._options,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        return body

    def parse(self, body: dict[str, Any], latency_ms: float) -> LLMResponse:
        choice = body["choices"][0]
        raw = choice["message"]
        calls = []
        for call in raw.get("tool_calls") or []:
            text = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(text)
                if not isinstance(args, dict):
                    raise ValueError
                calls.append(ToolCall(call["id"], call["function"]["name"], args))
            except ValueError:
                calls.append(ToolCall(call["id"], call["function"]["name"], {}, text))
        usage = body.get("usage") or {}
        stop = _STOPS.get(choice.get("finish_reason") or "", "end")
        if calls:
            stop = "tool_use"  # some compatible endpoints report "stop" alongside tool calls
        return LLMResponse(
            message=AssistantMessage(raw.get("content") or "", calls, self.provider, raw),
            stop=stop,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
        )

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = 1024,
    ) -> LLMResponse:
        body = self.request_body(system, messages, tools, max_tokens)
        response, latency = await post_json(self._client, "/chat/completions", body)
        return self.parse(response, latency)

    async def aclose(self) -> None:
        await self._client.aclose()


def _assistant(m: AssistantMessage) -> dict[str, Any]:
    """An assistant turn from another provider, in this format (no opaque state to carry)."""
    out: dict[str, Any] = {"role": "assistant", "content": m.text or None}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.name,
                    "arguments": c.bad_arguments or json.dumps(c.arguments),
                },
            }
            for c in m.tool_calls
        ]
    return out
