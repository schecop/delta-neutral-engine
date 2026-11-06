"""The supervisor without a real model: provider wire formats, the read-only tools, the loop
with a scripted model, the trace payload, and the Telegram approve flow."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from delta_neutral.config import ModelChoice
from delta_neutral.executor import open_engine_db
from delta_neutral.llm.anthropic import AnthropicClient
from delta_neutral.llm.base import (
    AssistantMessage,
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
    ToolResults,
    ToolSpec,
    UserMessage,
)
from delta_neutral.llm.openai_compat import OpenAICompatClient
from delta_neutral.risk import SCHEMA as RISK_SCHEMA
from delta_neutral.supervisor.agent import MAX_TURNS, Supervisor
from delta_neutral.supervisor.tools import SupervisorTools, open_readonly
from delta_neutral.tracing import LangfuseExporter, Trace

NOW = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
CHOICE = ModelChoice(
    provider="fake",
    model="scripted",
    usd_per_mtok_in=Decimal("0.10"),
    usd_per_mtok_out=Decimal("0.50"),
)
TOOL = ToolSpec("get_cycle", "one cycle", {"type": "object", "properties": {}})


# -- provider wire formats --------------------------------------------------------------------


def _openai() -> OpenAICompatClient:
    return OpenAICompatClient(
        provider="openai",
        model="m",
        api_key="k",
        base_url="https://x",
        options={"reasoning_effort": "minimal"},
    )


def test_openai_parses_tool_calls_and_keeps_bad_json_as_bad_arguments() -> None:
    raw = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "get_cycle", "arguments": '{"cycle_id": 8}'},
            },
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "get_cycle", "arguments": '{"cycle_id": '},
            },
        ],
    }
    body = {
        "choices": [{"message": raw, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    r = _openai().parse(body, 12.0)
    assert r.stop == "tool_use" and (r.input_tokens, r.output_tokens) == (100, 20)
    assert r.tool_calls[0] == ToolCall("c1", "get_cycle", {"cycle_id": 8})
    assert r.tool_calls[1].bad_arguments == '{"cycle_id": '


def test_openai_request_replays_its_own_turn_verbatim_and_sends_one_message_per_result() -> None:
    raw: dict[str, Any] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [],
        "extra_content": {"google": {"thought_signature": "sig"}},
    }
    messages: list[Message] = [
        UserMessage("q"),
        AssistantMessage("", [ToolCall("c1", "get_cycle", {})], "openai", raw),
        ToolResults([ToolResult("c1", "get_cycle", '{"a": 1}')]),
    ]
    body = _openai().request_body("sys", messages, [TOOL], 500)
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][2] is raw  # opaque provider state goes back untouched
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": '{"a": 1}'}
    assert body["max_completion_tokens"] == 500 and body["reasoning_effort"] == "minimal"
    assert body["tools"][0]["function"]["name"] == "get_cycle"


def test_anthropic_keeps_thinking_blocks_and_groups_results_in_one_user_message() -> None:
    client = AnthropicClient(
        model="claude-haiku-5-5", api_key="k", options={"output_config": {"effort": "low"}}
    )
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "opaque"},
        {"type": "text", "text": "Let me look."},
        {"type": "tool_use", "id": "t1", "name": "get_cycle", "input": {"cycle_id": 8}},
        {"type": "tool_use", "id": "t2", "name": "risk_events", "input": {}},
    ]
    r = client.parse(
        {
            "content": blocks,
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 50, "output_tokens": 9},
        },
        5.0,
    )
    assert r.text == "Let me look." and [c.id for c in r.tool_calls] == ["t1", "t2"]
    body = client.request_body(
        "sys",
        [
            UserMessage("q"),
            r.message,
            ToolResults(
                [ToolResult("t1", "get_cycle", "{}"), ToolResult("t2", "risk_events", "{}", True)]
            ),
        ],
        [TOOL],
        300,
    )
    assert body["system"] == "sys" and body["output_config"] == {"effort": "low"}
    assert body["messages"][1] == {
        "role": "assistant",
        "content": blocks,
    }  # thinking block included
    results = body["messages"][2]
    assert results["role"] == "user" and [b["tool_use_id"] for b in results["content"]] == [
        "t1",
        "t2",
    ]
    assert results["content"][1]["is_error"] is True


def test_a_turn_from_another_provider_is_translated_not_replayed() -> None:
    foreign = AssistantMessage(
        "hi", [ToolCall("x", "get_cycle", {"cycle_id": 1})], "openai", {"role": "assistant"}
    )
    body = AnthropicClient(model="m", api_key="k").request_body(
        "s", [UserMessage("q"), foreign], [], 10
    )
    assert body["messages"][1]["content"] == [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "x", "name": "get_cycle", "input": {"cycle_id": 1}},
    ]


def test_retries_a_429_then_succeeds() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )

    async def go() -> LLMResponse:
        http = httpx.AsyncClient(
            base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
        )
        client = AnthropicClient(model="m", api_key="k", client=http)
        return await client.complete(system="s", messages=[UserMessage("q")])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("delta_neutral.llm.http.asyncio.sleep", _no_sleep)
        assert asyncio.run(go()).text == "ok"
    assert len(calls) == 2 and calls[1].url.path == "/v1/messages"


async def _no_sleep(_: float) -> None:
    return None


# -- the read-only tools ----------------------------------------------------------------------


def _engine_db(tmp_path: Path) -> Path:
    path = tmp_path / "engine.db"
    if path.exists():
        return path
    db = open_engine_db(path)
    db.executescript(RISK_SCHEMA)
    summary = {
        "predicted_cost_usd": "0.40",
        "realised_cost_usd": "0.43",
        "error_usd": "0.03",
        "price_pnl_usd": "-0.13",
        "fees_usd": "0.30",
        "funding_usd_estimated": "-0.002",
        "held_minutes": 42.2,
        "close_leg_skew_s": 3.0,
        "close_unhedged_s": 3.0,
    }
    rows = [
        (
            6,
            "0.12",
            "0.119",
            "2026-11-06T10:00:00+00:00",
            {
                **summary,
                "predicted_cost_usd": "0.119",
                "realised_cost_usd": "0.120",
                "error_usd": "0.001",
                "fees_usd": "0.09",
            },
        ),
        (8, "0.40", "0.43", "2026-11-06T13:20:00+00:00", summary),
    ]
    for cycle_id, predicted, _, closed_at, s in rows:
        db.execute(
            "INSERT INTO cycles VALUES (?, 'XAU-USD', 'variational', 'qfex', '300', 30, ?, '0', "
            "'closed', ?, ?, NULL, ?, ?)",
            (cycle_id, predicted, closed_at, closed_at, closed_at, json.dumps(s)),
        )
    db.execute(
        "INSERT INTO cycles VALUES (9, 'XAU-USD', 'qfex', 'variational', '300', 30, '0.44', '0', "
        "'open', '2026-11-06T16:50:00+00:00', '2026-11-06T16:51:00+00:00', "
        "'2026-11-06T17:21:00+00:00', NULL, NULL)"
    )
    db.execute(
        "INSERT INTO risk_events VALUES "
        "('2026-11-06T13:19:10+00:00', 'unhedged', 'resume', 8, NULL, "
        "'cycle 8 close phase unhedged for 105s (max 60s)')"
    )
    db.commit()
    db.close()
    return path


def _tools(tmp_path: Path) -> SupervisorTools:
    return SupervisorTools(open_readonly(_engine_db(tmp_path)), clock=lambda: NOW)


def test_cost_summary_does_the_arithmetic_in_decimal(tmp_path: Path) -> None:
    s = _tools(tmp_path).cost_summary(hours=24)
    assert s["closed_cycles"] == 2 and s["cycle_ids"] == [6, 8]
    assert s["predicted_cost_usd"] == "0.5190" and s["realised_cost_usd"] == "0.5500"
    assert s["error_usd"] == "0.0310" and s["fees_usd"] == "0.3900"
    assert s["volume_usd"] == "2400.0000" and s["realised_cost_per_cycle_usd"] == "0.2750"
    assert s["realised_cost_bps_of_volume"] == "2.29"
    assert s["largest_abs_error"] == {"cycle_id": 8, "abs_error_usd": "0.0300"}
    assert _tools(tmp_path).cost_summary(hours=1)["closed_cycles"] == 0


def test_cycles_and_risk_events(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    assert [c["id"] for c in tools.list_cycles()["cycles"]] == [9, 8, 6]
    assert [c["id"] for c in tools.list_cycles(state="open")["cycles"]] == [9]
    assert tools.get_cycle(8)["result"]["realised_cost_usd"] == "0.4300"
    assert tools.get_cycle(99) == {"error": "no cycle 99"}
    events = tools.risk_events(hours=24)
    assert events["count"] == 1 and events["events"][0]["cycle_id"] == 8


def test_the_tools_cannot_write(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        tools._db.execute("UPDATE cycles SET state = 'closed' WHERE id = 9")


def test_a_proposal_is_recorded_not_executed(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    out = tools.call("propose_action", {"action": "close_cycle", "cycle_id": 9, "reason": "test"})
    assert out["status"] == "proposed, NOT executed" and len(tools.proposals) == 1
    assert tools.list_cycles(state="open")["cycles"][0]["id"] == 9  # still open
    assert "error" in tools.call(
        "propose_action", {"action": "kill_switch", "cycle_id": 9, "reason": ""}
    )
    assert "error" in tools.call("drop_tables", {})
    assert "error" in tools.call("get_cycle", {"cycle": 9})  # wrong argument name


# -- the loop, with a scripted model ----------------------------------------------------------


class ScriptedLLM:
    provider = "fake"
    model = "scripted"

    def __init__(self, turns: list[AssistantMessage]) -> None:
        self._turns = turns
        self.seen: list[list[Message]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = 1024,
    ) -> LLMResponse:
        self.seen.append(list(messages))
        msg = (
            self._turns.pop(0)
            if self._turns
            else AssistantMessage("", [ToolCall("again", "list_cycles", {})])
        )
        return LLMResponse(msg, "tool_use" if msg.tool_calls else "end", 1000, 100, 50.0)

    async def aclose(self) -> None:
        return None


def test_the_loop_runs_tools_and_returns_the_answer(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            AssistantMessage(
                "",
                [
                    ToolCall("a", "cost_summary", {"hours": 24}),
                    ToolCall("b", "get_cycle", {"cycle_id": 8, "extra": 1}),  # a wrong argument
                ],
            ),
            AssistantMessage("Cost $0.55 over cycles 6 and 8."),
        ]
    )
    run = asyncio.run(Supervisor(llm, CHOICE, _tools(tmp_path)).ask("cost today?"))
    assert run.answer == "Cost $0.55 over cycles 6 and 8." and run.turns == 2
    assert run.tool_calls == ["cost_summary", "get_cycle"]
    assert run.cost_usd == Decimal("0.0003")  # 2 x (1000 in at $0.10/M + 100 out at $0.50/M)
    results = llm.seen[1][-1]
    assert isinstance(results, ToolResults)
    assert json.loads(results.results[0].content)["realised_cost_usd"] == "0.5500"
    assert results.results[1].is_error  # a bad call is answered with an error, the loop goes on


def test_a_bad_json_call_is_answered_with_an_error(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [AssistantMessage("", [ToolCall("a", "get_cycle", {}, "{oops")]), AssistantMessage("done")]
    )
    asyncio.run(Supervisor(llm, CHOICE, _tools(tmp_path)).ask("q"))
    results = llm.seen[1][-1]
    assert isinstance(results, ToolResults) and results.results[0].is_error


def test_the_loop_stops_after_max_turns(tmp_path: Path) -> None:
    run = asyncio.run(Supervisor(ScriptedLLM([]), CHOICE, _tools(tmp_path)).ask("q"))
    assert run.turns == MAX_TURNS and run.stop == "max_turns" and "stopped after" in run.answer


def test_proposals_come_back_with_the_run(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "a",
                        "propose_action",
                        {"action": "close_cycle", "cycle_id": 9, "reason": "risk"},
                    )
                ],
            ),
            AssistantMessage("I proposed closing cycle 9; approve it on Telegram."),
        ]
    )
    run = asyncio.run(Supervisor(llm, CHOICE, _tools(tmp_path)).ask("close 9"))
    assert [(p.action, p.cycle_id) for p in run.proposals] == [("close_cycle", 9)]


# -- tracing ----------------------------------------------------------------------------------


def test_trace_payload_is_otlp_json_with_a_generation_span() -> None:
    trace = Trace("supervisor.ask", langfuse__observation__input="q")
    with trace.span("llm m", langfuse__observation__type="generation") as span:
        span.set(
            gen_ai__usage__input_tokens=10,
            gen_ai__usage__cost=0.001,
            langfuse__observation__output={"a": 1},
        )
    trace.finish(turns=1)
    spans = trace.to_otlp()["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root, gen = spans
    assert len(root["traceId"]) == 32 and len(root["spanId"]) == 16 and "parentSpanId" not in root
    assert gen["parentSpanId"] == root["spanId"] and gen["traceId"] == root["traceId"]
    attrs = {a["key"]: a["value"] for a in gen["attributes"]}
    assert attrs["langfuse.observation.type"] == {"stringValue": "generation"}
    assert attrs["gen_ai.usage.input_tokens"] == {"intValue": "10"}
    assert attrs["langfuse.observation.output"] == {"stringValue": '{"a": 1}'}


def test_exporter_posts_to_langfuse_with_basic_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async def go() -> bool:
        http = httpx.AsyncClient(
            base_url="https://cloud.langfuse.com", transport=httpx.MockTransport(handler)
        )
        exporter = LangfuseExporter("https://cloud.langfuse.com", "pk", "sk", client=http)
        return await exporter.export(Trace("t"))

    assert asyncio.run(go())
    assert seen[0].url.path == "/api/public/otel/v1/traces"
    assert seen[0].headers["authorization"] == "Basic cGs6c2s="  # base64("pk:sk")
    assert seen[0].headers["x-langfuse-ingestion-version"] == "4"
    body: dict[str, Any] = json.loads(seen[0].content)
    assert "resourceSpans" in body
