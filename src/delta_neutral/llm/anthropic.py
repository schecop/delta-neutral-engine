"""Anthropic Messages adapter.

    POST {base_url}/v1/messages   x-api-key: <key>   anthropic-version: 2026-10-21

Wire format (Claude API reference, Create a Message):
- system is a top-level field; tools: [{name, description, input_schema}]
- the response `content` is a list of blocks: text, tool_use {id, name, input}, and thinking
  blocks, which must be sent back unchanged: so the whole list is kept as `raw`
- results go back in one user message: [{"type": "tool_result", "tool_use_id", "content",
  "is_error"}], all results of a turn together
- stop_reason "tool_use" | "end_turn" | "max_tokens" | "refusal"; usage.input_tokens /
  output_tokens

Thinking and effort are not set here: they are model-specific request fields that come from
config (`llm.<role>.options`), e.g. `output_config: {effort: low}`.
"""

from __future__ import annotations

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

API_VERSION = "2026-10-21"
_STOPS: dict[str, Stop] = {"tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal"}


class AnthropicClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        options: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider = "anthropic"
        self.model = model
        self._options = options or {}
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"x-api-key": api_key, "anthropic-version": API_VERSION},
            timeout=httpx.Timeout(60.0),
        )

    def request_body(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
    ) -> dict[str, Any]:
        wire: list[dict[str, Any]] = []
        for m in messages:
            if isinstance(m, UserMessage):
                wire.append({"role": "user", "content": m.content})
            elif isinstance(m, AssistantMessage):
                content = m.raw if m.provider == self.provider and m.raw else _blocks(m)
                wire.append({"role": "assistant", "content": content})
            elif isinstance(m, ToolResults):
                wire.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": r.call_id,
                                "content": r.content,
                                "is_error": r.is_error,
                            }
                            for r in m.results
                        ],
                    }
                )
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": wire,
            **self._options,
        }
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        return body

    def parse(self, body: dict[str, Any], latency_ms: float) -> LLMResponse:
        blocks: list[dict[str, Any]] = body.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = [
            ToolCall(b["id"], b["name"], dict(b.get("input") or {}))
            for b in blocks
            if b.get("type") == "tool_use"
        ]
        usage = body.get("usage") or {}
        return LLMResponse(
            message=AssistantMessage(text, calls, self.provider, blocks),
            stop=_STOPS.get(body.get("stop_reason") or "", "end"),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
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
        response, latency = await post_json(self._client, "/v1/messages", body)
        return self.parse(response, latency)

    async def aclose(self) -> None:
        await self._client.aclose()


def _blocks(m: AssistantMessage) -> list[dict[str, Any]]:
    """An assistant turn from another provider, as Messages content blocks."""
    out: list[dict[str, Any]] = [{"type": "text", "text": m.text}] if m.text else []
    out += [
        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls
    ]
    return out
