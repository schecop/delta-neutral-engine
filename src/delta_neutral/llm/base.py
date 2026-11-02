"""Model-agnostic LLM interface (NNN rule 7: no default model).

The supervisor depends on this interface, never on a provider SDK. Each provider gets an
adapter that translates to and from its own tool-use format; the provider and model for each
role come from config (`llm:` in the YAML).

The neutral message format is intentionally minimal for now and gets settled in D4, when the
first two adapters exist: design it from what both providers need, not from one of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop: Literal["end", "tool_use", "max_tokens"] = "end"
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0  # time to full response; adapters that stream also report first token


class LLMClient(Protocol):
    provider: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = 1024,
    ) -> LLMResponse: ...
