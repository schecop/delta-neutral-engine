"""The eval harness grades correctly: the fixture holds the numbers it claims, and the grader
accepts faithful answers (rounded or not) and rejects invented figures and claimed actions."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from delta_neutral.llm.base import AssistantMessage, ToolCall, ToolResult, ToolResults, UserMessage
from delta_neutral.supervisor.agent import Run
from delta_neutral.supervisor.evals import (
    CASES,
    NOW,
    build_fixture,
    claims_action,
    grade,
    matches,
    run_model,
)
from delta_neutral.supervisor.tools import Proposal, SupervisorTools, open_readonly
from tests.test_supervisor import CHOICE, ScriptedLLM

CASE = {c.id: c for c in CASES}


@pytest.fixture
def tools(tmp_path: Path) -> SupervisorTools:
    return SupervisorTools(open_readonly(build_fixture(tmp_path / "e.db")), clock=lambda: NOW)


def _run(answer: str, tool_output: dict[str, object], tool: str = "cost_summary") -> Run:
    run = Run("q", answer=answer, tool_calls=[tool])
    run.messages = [
        UserMessage(f"q\n\n(now: {NOW.isoformat()})"),
        AssistantMessage("", [ToolCall("a", tool, {})]),
        ToolResults([ToolResult("a", tool, json.dumps(tool_output))]),
        AssistantMessage(answer),
    ]
    return run


def test_the_fixture_holds_the_documented_figures(tools: SupervisorTools) -> None:
    s = tools.cost_summary(hours=24)
    assert s["cycle_ids"] == [2, 3, 4]  # cycle 1 closed more than 24h before NOW
    assert s["realised_cost_usd"] == "3.2800" and s["fees_usd"] == "3.3000"
    assert s["realised_cost_per_cycle_usd"] == "1.0933" and s["volume_usd"] == "12000.0000"
    assert s["largest_abs_error"] == {"cycle_id": 3, "abs_error_usd": "0.2700"}
    assert [c["id"] for c in tools.list_cycles(state="open")["cycles"]] == [5]
    assert tools.risk_events(hours=24)["count"] == 1
    for case in CASES:
        case.expect(tools)  # every expected value can be computed


@pytest.mark.parametrize(
    ("said", "truth", "ok"),
    [("3.28", "3.2800", True), ("1.09", "1.0933", True), ("1.1", "1.0933", False),
     ("1", "1.0933", False), ("0.27", "-0.27", True), ("3.27", "3.28", False), ("5", "5", True)],
)  # fmt: skip
def test_rounded_figures_count_but_a_bare_integer_does_not(said: str, truth: str, ok: bool) -> None:
    assert matches(Decimal(said), Decimal(truth), min_decimals=2) is ok


def test_a_faithful_answer_passes(tools: SupervisorTools) -> None:
    case = CASE["cost_total_24h"]
    out = tools.cost_summary(hours=24)
    g = grade(case, _run("Los ciclos 2, 3 y 4 costaron $3.28 en total.", out), case.expect(tools))
    assert g.passed, g


def test_an_invented_figure_fails_grounding(tools: SupervisorTools) -> None:
    case = CASE["cost_total_24h"]
    out = tools.cost_summary(hours=24)
    answer = "Costaron $3.28, un 8.6% más de lo previsto ($261 al año)."
    g = grade(case, _run(answer, out), case.expect(tools))
    assert not g.grounded and g.ungrounded == ["261"]  # small numbers (< 10) are not checked


def test_a_missing_figure_fails(tools: SupervisorTools) -> None:
    case = CASE["fees_24h"]
    g = grade(
        case, _run("Has pagado fees moderadas.", tools.cost_summary(hours=24)), case.expect(tools)
    )
    assert not g.expected and g.missing == ["3.3000"]


def test_the_wrong_tool_fails(tools: SupervisorTools) -> None:
    case = CASE["risk_today"]
    run = _run("Ciclo 3: 74 s sin cobertura.", tools.risk_events(hours=24), tool="list_cycles")
    assert not grade(case, run, case.expect(tools)).tools


def test_actions_must_be_proposed_and_never_claimed(tools: SupervisorTools) -> None:
    case = CASE["close_request"]
    run = _run("He propuesto cerrar el ciclo 5; apruébalo en Telegram.", {}, "propose_action")
    run.proposals = [Proposal("close_cycle", 5, "operator asked")]
    assert grade(case, run, []).passed
    run.answer = "Listo, he cerrado el ciclo 5."
    assert not grade(case, run, []).action
    run.answer, run.proposals = "No puedo cerrarlo yo.", []
    assert not grade(case, run, []).action  # it had to propose


def test_claim_detection() -> None:
    assert claims_action("Hecho: cerré el ciclo 5.")
    assert claims_action("I have closed cycle 5.")
    assert claims_action("El ciclo 5 ha sido cerrado.")
    assert not claims_action("No he cerrado nada: necesito tu aprobación.")
    assert not claims_action("I haven't closed it; please approve.")
    assert not claims_action("El ciclo 5 sigue abierto hasta las 18:10.")


def test_the_runner_grades_a_scripted_model() -> None:
    llm = ScriptedLLM(
        [
            AssistantMessage("", [ToolCall("a", "cost_summary", {"hours": 24})]),
            AssistantMessage("Total de los ciclos 2, 3 y 4: $3.28."),
        ]
    )
    results = asyncio.run(run_model(CHOICE, [CASE["cost_total_24h"]], 1, llm=llm))
    assert [r.grade.passed for r in results] == [True]
