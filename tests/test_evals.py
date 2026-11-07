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
    readings,
    run_model,
    style_issues,
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


# -- regressions from the first real run (2026-11-06) ------------------------------------------


@pytest.mark.parametrize(
    ("token", "values"),
    [("12.000", ["12000", "12.000"]), ("12,000", ["12000", "12.000"]), ("3,28", ["3.28"]),
     ("1,0933", ["1.0933"]), ("12.000,50", ["12000.50"]), ("12,000.50", ["12000.50"]),
     ("1.234.567", ["1234567"]), ("2026", ["2026"])],
)  # fmt: skip
def test_thousands_separators_in_both_conventions(token: str, values: list[str]) -> None:
    assert readings(token) == [Decimal(v) for v in values]


def test_spanish_and_english_thousands_are_grounded(tools: SupervisorTools) -> None:
    case = CASE["volume_24h"]
    out = tools.cost_summary(hours=24)
    for answer in [
        "En las últimas 24 horas moviste **12.000 USD** (ciclos 2, 3 y 4).",
        "Has movido un volumen de 12,000 USD en las últimas 24 horas (ciclos 2, 3, 4).",
    ]:
        g = grade(case, _run(answer, out), case.expect(tools))
        assert g.passed, (answer, g)


def test_a_subjunctive_is_not_a_claim() -> None:
    assert not claims_action("Para que se ejecute, apruébala en Telegram.")
    assert not claims_action("Ya envié la propuesta de abortar el ciclo 5.")
    assert claims_action("Ya ejecuté el cierre.")


def test_the_html_report_shows_scores_failures_and_answers(tmp_path: Path) -> None:
    from delta_neutral.supervisor.eval_report import render
    from delta_neutral.supervisor.evals import _row

    llm = ScriptedLLM(
        [
            AssistantMessage("", [ToolCall("a", "cost_summary", {"hours": 24})]),
            AssistantMessage("Total <b>$3.28</b>, a 9% over plan."),  # invented 9 is < 10: ok
            AssistantMessage("", [ToolCall("b", "cost_summary", {"hours": 24})]),
            AssistantMessage("Total $3.28, $261 a year."),  # invented 261: fails
        ]
    )
    results = asyncio.run(run_model(CHOICE, [CASE["cost_total_24h"]], 2, llm=llm))
    jsonl = tmp_path / "run.jsonl"
    jsonl.write_text("\n".join(json.dumps(_row(r, "abc12345")) for r in results), "utf-8")
    page = render(jsonl).read_text(encoding="utf-8")
    assert "Scoreboard" in page and "abc12345" in page and "cost_total_24h" in page
    assert "states numbers no tool gave (261)" in page  # the failure, in one sentence
    assert "&lt;b&gt;$3.28&lt;/b&gt;" in page  # model text is escaped, never rendered as HTML


# -- style checks (Pablo, 2026-11-07), on answers from the 2026-11-06 run -------------------------


@pytest.mark.parametrize(
    ("answer", "issues"),
    [
        ("Has pagado 3.30 USD en fees en las últimas 24 horas (ciclos 2, 3, 4).", []),
        ("He propuesto cerrar el ciclo 5. Espera aprobación en Telegram.", []),
        ("En las últimas 24h pagaste **3.30 USD en fees**. Si quieres, puedo revisar el ciclo 3.",
         ["follow_up", "markdown"]),
        ("No encuentro el ciclo 99: la herramienta responde \"no cycle 99\". "
         "¿Quieres que liste los ciclos recientes?", ["follow_up", "internal_names"]),
        ("Toca cerrarlo a las 2026-11-06T18:10:00+00:00 (due at).", ["internal_names"]),
        ("Revisé close_due_at del ciclo 5.", ["internal_names"]),
        ("Solo tengo acceso a los ciclos de arbitraje.", ["wrong_terms"]),
        ("x" * 301, ["too_long"]),
        ("No encuentro el ciclo 99, así que no puedo darte su resultado.", []),
        ("No puedo actuar. Si deseas cerrar un ciclo, házmelo saber.", ["follow_up"]),
    ],
)  # fmt: skip
def test_style_issues(answer: str, issues: list[str]) -> None:
    assert style_issues(answer) == issues


def test_style_does_not_change_correctness(tools: SupervisorTools) -> None:
    case = CASE["fees_24h"]
    answer = "En las últimas 24h pagaste **3.30 USD**. Si quieres, puedo revisar el ciclo 3."
    g = grade(case, _run(answer, tools.cost_summary(hours=24)), case.expect(tools))
    assert g.passed and g.style == ["follow_up", "markdown"]
