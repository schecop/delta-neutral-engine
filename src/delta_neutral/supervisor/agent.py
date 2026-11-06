"""The supervisor: a hand-written tool-use loop over read-only tools.

    question -> model -> (tool calls -> results -> model)* -> answer

The loop is ours, not a framework's: each turn is one `LLMClient.complete`, every tool call is
run by `SupervisorTools.call`, and the conversation is append-only. It stops when the model
answers without calling a tool, or after MAX_TURNS model calls (then it says so: a run that
loops is a bug to see, not to hide). Every model call and tool call is a span in one trace.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from delta_neutral.config import ModelChoice
from delta_neutral.llm.base import (
    LLMClient,
    Message,
    ToolResult,
    ToolResults,
    UserMessage,
)
from delta_neutral.llm.factory import cost_usd
from delta_neutral.supervisor.tools import SPECS, Proposal, SupervisorTools
from delta_neutral.tracing import LangfuseExporter, Trace

log = logging.getLogger(__name__)

MAX_TURNS = 6
MAX_TOKENS = 1024

SYSTEM = """You are the read-only supervisor of a delta-neutral trading engine: it opens two \
opposite legs of the same instrument on two perpetual-futures venues, holds them, and closes \
both. The operator asks you questions on Telegram.

Rules:
- Every number you state must come from a tool result in this conversation, copied as given \
(you may round to fewer decimals). Never compute totals, averages or differences yourself: \
cost_summary does that. If no tool gives a number, say you don't have it.
- You cannot act. To suggest closing or aborting a cycle, call propose_action; the operator \
approves it on Telegram. Never say an action was done or will be done without approval.
- Answer in the operator's language, short, plain text for a phone screen. Name the cycle ids \
your numbers come from.
- Times in tool results are UTC."""


@dataclass
class Run:
    question: str
    answer: str = ""
    proposals: list[Proposal] = field(default_factory=list)
    turns: int = 0
    tool_calls: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: float = 0.0  # model time only
    stop: str = ""
    trace_id: str = ""
    messages: list[Message] = field(default_factory=list)


class Supervisor:
    def __init__(
        self,
        llm: LLMClient,
        choice: ModelChoice,
        tools: SupervisorTools,
        *,
        exporter: LangfuseExporter | None = None,
        system: str = SYSTEM,
    ) -> None:
        self._llm = llm
        self._choice = choice
        self._tools = tools
        self._exporter = exporter
        self._system = system

    async def ask(self, question: str) -> Run:
        run = Run(question)
        self._tools.proposals = []
        trace = Trace(
            "supervisor.ask",
            langfuse__observation__input=question,
            provider=self._llm.provider,
            model=self._llm.model,
        )
        run.trace_id = trace.trace_id
        messages: list[Message] = [UserMessage(f"{question}\n\n(now: {_now()})")]
        try:
            for _ in range(MAX_TURNS):
                with trace.span(
                    f"llm {self._llm.model}",
                    langfuse__observation__type="generation",
                    gen_ai__request__model=self._llm.model,
                    gen_ai__system=self._llm.provider,
                ) as span:
                    response = await self._llm.complete(
                        system=self._system, messages=messages, tools=SPECS, max_tokens=MAX_TOKENS
                    )
                    cost = cost_usd(self._choice, response.input_tokens, response.output_tokens)
                    span.set(
                        gen_ai__usage__input_tokens=response.input_tokens,
                        gen_ai__usage__output_tokens=response.output_tokens,
                        gen_ai__usage__cost=float(cost),
                        langfuse__observation__output={
                            "text": response.text,
                            "tool_calls": [
                                {"name": c.name, "arguments": c.arguments}
                                for c in response.tool_calls
                            ],
                        },
                        stop=response.stop,
                        latency_ms=response.latency_ms,
                    )
                run.turns += 1
                run.input_tokens += response.input_tokens
                run.output_tokens += response.output_tokens
                run.cost_usd += cost
                run.latency_ms += response.latency_ms
                run.stop = response.stop
                messages.append(response.message)
                if not response.tool_calls:
                    run.answer = response.text.strip() or _empty(response.stop)
                    break
                results = []
                for call in response.tool_calls:
                    run.tool_calls.append(call.name)
                    with trace.span(
                        f"tool {call.name}",
                        langfuse__observation__type="tool",
                        langfuse__observation__input=call.arguments,
                    ) as span:
                        if call.bad_arguments is not None:
                            output = {"error": "arguments were not valid JSON; call again"}
                        else:
                            output = self._tools.call(call.name, call.arguments)
                        span.set(langfuse__observation__output=output)
                    results.append(
                        ToolResult(call.id, call.name, json.dumps(output), "error" in output)
                    )
                messages.append(ToolResults(results))
            else:
                run.answer = f"(stopped after {MAX_TURNS} model calls without an answer)"
                run.stop = "max_turns"
        except Exception as exc:
            trace.root.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            run.proposals = list(self._tools.proposals)
            run.messages = messages
            trace.finish(
                langfuse__observation__output=run.answer,
                turns=run.turns,
                tool_calls=run.tool_calls,
                cost_usd=float(run.cost_usd),
                proposals=[p.__dict__ for p in run.proposals],
            )
            log.info(
                "supervisor %s",
                json.dumps(
                    {
                        "trace_id": run.trace_id,
                        "model": self._llm.model,
                        "turns": run.turns,
                        "tools": run.tool_calls,
                        "input_tokens": run.input_tokens,
                        "output_tokens": run.output_tokens,
                        "cost_usd": str(run.cost_usd),
                        "latency_ms": round(run.latency_ms),
                        "stop": run.stop,
                        "proposals": len(run.proposals),
                    }
                ),
            )
            if self._exporter:
                await self._exporter.export(trace)
        return run


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _empty(stop: str) -> str:
    return "(the model refused to answer)" if stop == "refusal" else "(no answer)"
