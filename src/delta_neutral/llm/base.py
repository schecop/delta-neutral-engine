"""Model-agnostic LLM interface (NNN rule 7: no default model).

The supervisor depends on this interface, never on a provider API. Each provider adapter
translates the neutral messages below to and from its own wire format; the provider and model
for each role come from config (`llm:` in the YAML).

The conversation is append-only: `UserMessage`, then for each model turn an `AssistantMessage`
(what the model said and the tools it called) and a `ToolResults` with one result per call.
An assistant turn keeps the provider's own message in `raw`, and the same provider gets it back
verbatim: some providers attach opaque state to a turn (Anthropic thinking blocks, Gemini
thought signatures) that must be replayed unchanged.
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
    # the raw text, when the model sent arguments that are not JSON
    bad_arguments: str | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str  # JSON text, produced by the tool
    is_error: bool = False


@dataclass(frozen=True)
class UserMessage:
    content: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    raw: Any = None  # the provider's own message, replayed verbatim to that provider


@dataclass(frozen=True)
class ToolResults:
    results: list[ToolResult]


Message = UserMessage | AssistantMessage | ToolResults

Stop = Literal["end", "tool_use", "max_tokens", "refusal"]


@dataclass(frozen=True)
class LLMResponse:
    message: AssistantMessage
    stop: Stop = "end"
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0  # time to the full response

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def tool_calls(self) -> list[ToolCall]:
        return self.message.tool_calls


class LLMError(RuntimeError):
    """A provider call that failed after retries; the message never holds the API key."""


class LLMClient(Protocol):
    provider: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = 1024,
    ) -> LLMResponse: ...

    async def aclose(self) -> None: ...
