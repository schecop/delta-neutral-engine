"""The eval run as one HTML page, for tuning the prompt: which answers are ideal, which pass,
which fail and why.

    uv run python -m delta_neutral.supervisor.evals --render runs/evals/<run>.jsonl

Sections: a scoreboard per model; a question x model matrix (one dot per repeat, mean answer
length); "needs attention" with each failure explained in one sentence; and every question with
the models' answers side by side, shortest passing answer first, all runs folded below.
Self-contained (inline CSS, no scripts), light and dark. Built from the .jsonl alone, so any
past run renders the same way.
"""

from __future__ import annotations

import json
import statistics
import tempfile
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any

from delta_neutral.supervisor.evals import (
    CASES,
    MAX_CHARS,
    NOW,
    STYLE_CHECKS,
    Case,
    build_fixture,
    style_issues,
)
from delta_neutral.supervisor.tools import SupervisorTools, open_readonly

Row = dict[str, Any]

CSS = """
:root { --bg:#f7f7f5; --card:#fff; --ink:#1d1d1b; --muted:#6b6b66; --line:#e3e2dd;
  --ok:#1f7a4d; --ok-bg:#e3f3ea; --bad:#b3261e; --bad-bg:#fbe7e5; --warn:#8a6100;
  --warn-bg:#fdf1d6; --code:#efeee9; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --card:#1f1f1d; --ink:#ecebe6;
  --muted:#a3a29b; --line:#33322f; --ok:#5fcf93; --ok-bg:#16301f; --bad:#ff8a80;
  --bad-bg:#3a1a17; --warn:#f0c060; --warn-bg:#352a10; --code:#2a2a27; } }
* { box-sizing:border-box; }
body { margin:0; padding:24px 16px 64px; background:var(--bg); color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1200px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:32px 0 10px; }
.meta { color:var(--muted); font-size:13px; }
code { background:var(--code); padding:1px 5px; border-radius:4px; font-size:12.5px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; margin:10px 0; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; background:var(--card);
  border:1px solid var(--line); border-radius:10px; overflow:hidden; }
th, td { padding:8px 10px; border-bottom:1px solid var(--line); text-align:left;
  vertical-align:top; }
th { font-size:12px; text-transform:uppercase; letter-spacing:.03em; color:var(--muted); }
tr:last-child td { border-bottom:0; }
td.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
.pill { display:inline-block; padding:1px 8px; border-radius:999px; font-weight:600;
  font-size:12.5px; }
.good { color:var(--ok); background:var(--ok-bg); } .bad { color:var(--bad);
  background:var(--bad-bg); } .warn { color:var(--warn); background:var(--warn-bg); }
.dots { letter-spacing:2px; font-size:15px; white-space:nowrap; }
.d-ok { color:var(--ok); } .d-bad { color:var(--bad); } .d-style { color:var(--warn); }
.d-err { color:var(--muted); }
.len { color:var(--muted); font-size:12px; margin-left:4px; }
a { color:inherit; } .q { color:var(--muted); font-size:12.5px; }
.answers { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:10px; }
.answer { border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
.answer h4 { margin:0 0 6px; font-size:13px; display:flex; gap:8px; align-items:center; }
.answer p { margin:0; white-space:pre-wrap; }
.reason { font-weight:600; } blockquote { margin:4px 0 10px; padding:6px 10px;
  border-left:3px solid var(--line); color:var(--muted); white-space:pre-wrap; }
details { margin-top:8px; } summary { cursor:pointer; color:var(--muted); font-size:12.5px; }
.run { border-top:1px dashed var(--line); padding-top:6px; margin-top:6px; }
.run p { white-space:pre-wrap; margin:2px 0 0; }
"""


def expected_text(case: Case, figures: list[str]) -> str:
    wants = []
    if figures:
        wants.append("figures " + ", ".join(figures))
    if case.tools:
        wants.append("tool " + " or ".join(sorted(case.tools)))
    if case.kind == "action":
        wants.append(
            "propose " + " or ".join(f"{a} #{c}" for a, c in sorted(case.proposals))
            if case.proposals
            else "no proposal"
        )
        wants.append("never claim it was done")
    elif case.kind == "missing":
        wants.append("say it is not available; invent nothing")
    return " · ".join(wants)


def why_failed(case: Case, row: Row) -> str:
    """One plain sentence per failed check."""
    if row.get("error"):
        return "provider error: " + str(row["error"]).split("\n")[0][:80]
    g, reasons = row["grade"], []
    if not g["grounded"]:
        reasons.append(f"states numbers no tool gave ({', '.join(g['ungrounded'])})")
    if not g["expected"]:
        reasons.append(f"misses the figure ({', '.join(g['missing'])})")
    if not g["tools"]:
        reasons.append(f"did not use {' or '.join(sorted(case.tools))}")
    if not g["action"]:
        proposed = {(p["action"], p["cycle_id"]) for p in row["proposals"]}
        if case.kind != "action":
            reasons.append("proposed an action nobody asked for")
        elif case.proposals and not proposed:
            reasons.append("did not call propose_action")
        elif proposed - case.proposals:
            reasons.append("proposed the wrong action")
        else:
            reasons.append("claims the action was done")
    return "; ".join(reasons)


def _pct_pill(xs: list[Row], test: Callable[[Row], bool]) -> str:
    if not xs:
        return "-"
    share = sum(map(test, xs)) / len(xs)
    cls = "good" if share == 1 else ("warn" if share >= 0.9 else "bad")
    return f'<span class="pill {cls}">{100 * share:.0f}%</span>'


def _p50_p95(ms: list[float]) -> str:
    if not ms:
        return "-"
    s = sorted(x / 1000 for x in ms)
    p95 = s[min(len(s) - 1, int(0.95 * len(s)))]
    cls = "good" if p95 <= 5 else "bad"  # the 5 s gate in docs/model-research.md
    return f'{statistics.median(s):.1f}s / <span class="pill {cls}">{p95:.1f}s</span>'


def _style(row: Row) -> list[str]:
    """Computed from the answer text, so runs from before style checks existed score too."""
    return style_issues(row["answer"] or "")


def _dot(row: Row) -> str:
    if row.get("error"):
        cls, tip = "d-err", "provider error"
    elif not row["passed"]:
        cls, tip = "d-bad", "fail"
    elif _style(row):
        cls, tip = "d-style", "correct; style: " + ", ".join(_style(row))
    else:
        cls, tip = "d-ok", "correct and clean"
    return f'<span class="{cls}" title="{escape(tip)}">●</span>'


def _dots(rows: list[Row]) -> str:
    marks = "".join(_dot(r) for r in rows)
    chars = round(statistics.mean(len(r["answer"] or "") for r in rows)) if rows else 0
    return f'<span class="dots">{marks}</span><span class="len">{chars}c</span>'


def _wall(row: Row) -> float:
    """End-to-end ms; runs from before it was recorded fall back to model time."""
    return float(row.get("wall_ms") or row["latency_ms"])


def _quote(text: str) -> str:
    return escape(text or "(empty)")


def render(jsonl: Path) -> Path:
    rows: list[Row] = [
        json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line
    ]
    models = list(dict.fromkeys(r["model"] for r in rows))
    name = {m: escape(m.split("/")[-1]) for m in models}
    cases = [c for c in CASES if any(r["case"] == c.id for r in rows)]
    with tempfile.TemporaryDirectory() as tmp:
        db = open_readonly(build_fixture(Path(tmp) / "engine.db"))
        figures = {c.id: c.expect(SupervisorTools(db, clock=lambda: NOW)) for c in cases}
        db.close()

    def of(case: Case, model: str) -> list[Row]:
        return [r for r in rows if r["case"] == case.id and r["model"] == model]

    action_ids = {c.id for c in cases if c.kind == "action"}
    prompts = ", ".join(sorted({str(r.get("prompt", "?")) for r in rows}))
    h: list[str] = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Supervisor eval {escape(jsonl.stem)}</title><style>{CSS}</style></head>",
        "<body><main>",
        f"<h1>Supervisor eval · {escape(jsonl.stem)}</h1>",
        f"<div class='meta'>prompt <code>{escape(prompts)}</code> · fixture clock "
        f"{NOW:%Y-%m-%d %H:%M} UTC · {len(cases)} questions · "
        "<span class='d-ok'>●</span> correct and clean "
        "<span class='d-style'>●</span> correct, style issues "
        "<span class='d-bad'>●</span> wrong <span class='d-err'>●</span> provider error "
        "· Nc = mean answer length · hover a dot for details</div>",
    ]

    # scoreboard
    h += [
        "<h2>Scoreboard</h2><div class='scroll'><table><tr><th>Model</th><th>Correct</th>"
        "<th>Figures</th><th>Tools</th><th>Actions</th><th>Style</th>"
        "<th>End-to-end p50 / p95</th><th>Calls/q</th><th>Mean length</th>"
        "<th>$/question</th></tr>"
    ]
    for m in models:
        rs = [r for r in rows if r["model"] == m]
        figs = [r for r in rs if r["case"] not in action_ids]
        acts = [r for r in rs if r["case"] in action_ids]
        ok = [r for r in rs if not r.get("error")]
        cost = sum(Decimal(r["cost_usd"]) for r in rs) / len(rs)
        h.append(
            f"<tr><td><b>{name[m]}</b></td><td>{_pct_pill(rs, lambda r: r['passed'])}</td>"
            f"<td>{_pct_pill(figs, lambda r: r['grade']['grounded'] and r['grade']['expected'])}"
            f"</td><td>{_pct_pill(rs, lambda r: r['grade']['tools'])}</td>"
            f"<td>{_pct_pill(acts, lambda r: r['grade']['action'])}</td>"
            f"<td>{_pct_pill(ok, lambda r: not _style(r))}</td>"
            f"<td class='num'>{_p50_p95([_wall(r) for r in rs])}</td>"
            f"<td class='num'>{statistics.mean(r['turns'] for r in ok) if ok else 0:.1f}</td>"
            f"<td class='num'>{statistics.mean(len(r['answer'] or '') for r in rs):.0f}c</td>"
            f"<td class='num'>${cost:.5f}</td></tr>"
        )
    h.append("</table></div>")

    # matrix
    h.append("<h2>Matrix</h2><div class='scroll'><table><tr><th>Question</th>")
    h += [f"<th>{name[m]}</th>" for m in models]
    h.append("</tr>")
    for c in cases:
        h.append(
            f"<tr><td><a href='#{c.id}'><code>{c.id}</code></a>"
            f"<div class='q'>{escape(c.question)}</div></td>"
        )
        h += [f"<td>{_dots(of(c, m))}</td>" for m in models]
        h.append("</tr>")
    h.append("</table></div>")

    # needs attention
    h.append("<h2>Needs attention</h2>")
    failing = [c for c in cases if any(not r["passed"] for r in rows if r["case"] == c.id)]
    if not failing:
        h.append("<div class='card'>Nothing: every answer passed.</div>")
    for c in failing:
        bad = [(m, r) for m in models for r in of(c, m) if not r["passed"]]
        grouped = Counter((m, why_failed(c, r), r["answer"] or "") for m, r in bad)
        h.append(
            f"<div class='card'><a href='#{c.id}'><code>{c.id}</code></a> "
            f"{escape(c.question)}<div class='q'>expected: "
            f"{escape(expected_text(c, figures[c.id]))}</div>"
        )
        for (m, reason, answer), times in grouped.items():
            count = f" ({times} runs)" if times > 1 else ""
            h.append(
                f"<div><b>{name[m]}</b>{count}: <span class='reason'>{escape(reason)}</span>"
                f"</div><blockquote>{_quote(answer)}</blockquote>"
            )
        h.append("</div>")

    # style: which checks each model breaks, with one example each
    checks = ["too_long", *STYLE_CHECKS]
    h.append(
        f"<h2>Style</h2><div class='q'>too_long: over {MAX_CHARS} characters · follow_up: "
        "offers to continue or asks back · internal_names: tool or field names, raw "
        "timestamps · markdown: ** ` # · wrong_terms: e.g. arbitrage</div>"
        "<div class='scroll'><table><tr><th>Model</th>"
        + "".join(f"<th>{c}</th>" for c in checks)
        + "</tr>"
    )
    for m in models:
        rs = [r for r in rows if r["model"] == m and not r.get("error")]
        cells = []
        for check in checks:
            hits = sum(check in _style(r) for r in rs)
            cls = "good" if hits == 0 else ("warn" if hits <= len(rs) // 10 else "bad")
            cells.append(f"<td><span class='pill {cls}'>{hits}/{len(rs)}</span></td>")
        h.append(f"<tr><td><b>{name[m]}</b></td>{''.join(cells)}</tr>")
    h.append("</table></div>")
    for m in models:
        examples = []
        for check in checks:
            hit = next(
                (r for r in rows if r["model"] == m and check in _style(r)),
                None,
            )
            if hit:
                examples.append(
                    f"<div><b>{check}</b> · <code>{escape(hit['case'])}</code></div>"
                    f"<blockquote>{_quote(hit['answer'])}</blockquote>"
                )
        if examples:
            h.append(
                f"<details class='card'><summary><b>{name[m]}</b>: one example per check"
                f"</summary>{''.join(examples)}</details>"
            )

    # every question, models side by side
    h.append("<h2>Answers by question</h2>")
    for c in cases:
        h.append(
            f"<div class='card' id='{c.id}'><code>{c.id}</code> · {escape(c.kind)} "
            f"<div><b>{escape(c.question)}</b></div><div class='q'>expected: "
            f"{escape(expected_text(c, figures[c.id]))}</div><div class='answers'>"
        )
        for m in models:
            runs = of(c, m)
            passing = sorted((r for r in runs if r["passed"]), key=lambda r: len(r["answer"]))
            shown = passing[0] if passing else (runs[0] if runs else None)
            verdict = f"<span class='pill {'good' if passing else 'bad'}'>" + (
                f"{len(passing)}/{len(runs)}</span>"
            )
            body = _quote(shown["answer"]) if shown else "(no answer)"
            label = "shortest passing" if passing else "failing"
            h.append(
                f"<div class='answer'><h4>{name[m]} {verdict}"
                f"<span class='q'>{label}</span></h4><p>{body}</p><details>"
                f"<summary>all {len(runs)} runs</summary>"
            )
            for i, r in enumerate(runs, 1):
                mark = "✓" if r["passed"] else "✗ " + escape(why_failed(c, r))
                if _style(r):
                    mark += " · style: " + escape(", ".join(_style(r)))
                calls = r.get("tool_args") or [{"name": t, "arguments": None} for t in r["tools"]]
                tools = ", ".join(
                    f"{t['name']}({json.dumps(t['arguments'])})"
                    if t["arguments"] is not None
                    else t["name"]
                    for t in calls
                )
                h.append(
                    f"<div class='run'><span class='q'>run {i} · {mark} · "
                    f"{escape(tools or 'no tools')} · {_wall(r) / 1000:.1f}s</span>"
                    f"<p>{_quote(r['answer'])}</p></div>"
                )
            h.append("</details></div>")
        h.append("</div></div>")

    h.append("</main></body></html>")
    out = jsonl.with_suffix(".html")
    out.write_text("\n".join(h), encoding="utf-8")
    return out
