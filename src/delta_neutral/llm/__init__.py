"""Model-agnostic LLM access (NNN rule 7)."""

from delta_neutral.llm.base import LLMClient, LLMResponse, ToolCall, ToolSpec

__all__ = ["LLMClient", "LLMResponse", "ToolCall", "ToolSpec"]
