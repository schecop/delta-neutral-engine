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
import time
from dataclasses import dataclass, field
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
(you may round to fewer decimals). Never compute anything yourself: no totals, averages, \
differences, percentages or ratios. cost_summary does the totals; if no tool gives a number, \
leave it out or say you don't have it.
- You cannot act. When the operator asks to close or abort a cycle, call propose_action right \
away, without asking for confirmation first: the Approve button on Telegram is the \
confirmation. Then say it was proposed and needs approval. Never say an action was done, or \
that you will do it later.
- Answer only what was asked, in one or two short sentences with the figure requested. Do \
not add other figures, details or context nobody asked for. Short, but always include the key \
facts the question is about: cycle id, amount, time, duration.
- Each question stands alone: no conversation history is kept. Do not offer more help, ask \
questions back, or promise to check something later.
- Talk like a trading desk colleague, not like software: never mention tools, field names or \
how you work. Give times as HH:MM UTC, not raw timestamps. If something is not available or \
you cannot do it, say so in one sentence, without explaining why.
- Plain text only: no Markdown, no bold, no bullet lists.
- Answer in the operator's language, plain text for a phone screen. Name the cycle ids your \
numbers come from.
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
    latency_ms: float = 0.0  # model time only, successful attempts
    wall_ms: float = 0.0  # question to answer: model, tools, retries and their waits
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

    async def ask(self, question: str, **trace_attributes: str) -> Run:
        run = Run(question)
        started = time.perf_counter()
        self._tools.proposals = []
        trace = Trace(
            "supervisor.ask",
            langfuse__observation__input=question,
            provider=self._llm.provider,
            model=self._llm.model,
            **trace_attributes,
        )
        run.trace_id = trace.trace_id
        messages: list[Message] = [UserMessage(f"{question}\n\n(now: {self._tools.now()})")]
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
            run.wall_ms = (time.perf_counter() - started) * 1000
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
                        "wall_ms": round(run.wall_ms),
                        "stop": run.stop,
                        "proposals": len(run.proposals),
                    }
                ),
            )
            if self._exporter:
                await self._exporter.export(trace)
        return run


def _empty(stop: str) -> str:
    return "(the model refused to answer)" if stop == "refusal" else "(no answer)"
