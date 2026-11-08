"""Review real supervisor runs: pull the latest traces from Langfuse and check every figure.

    uv run python -m delta_neutral.supervisor.review            # last 20 questions, last 48h
    uv run python -m delta_neutral.supervisor.review --limit 5 --hours 72

For each question asked on Telegram it shows the answer, the tools called with their arguments,
proposals, time and cost, and runs the same grounding check as the evals: every number in the
answer must appear in what the tools returned from engine.db. A flagged number is not
necessarily wrong (a time the model derived from "now", for example), but it is the place to
look. The output also goes to runs/review/<time>.txt. It needs the Langfuse keys, so the
operator runs it.

Reads GET /api/public/v2/observations (the v1 traces endpoints are retired on Langfuse Cloud):
observation rows for the window, grouped by trace.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from delta_neutral.env import load_env
from delta_neutral.supervisor.evals import style_issues, ungrounded_numbers

ROOT = "supervisor.ask"


@dataclass
class Reviewed:
    trace_id: str
    when: str
    model: str
    question: str
    answer: str
    tools: list[tuple[str, Any]] = field(default_factory=list)  # (name, arguments)
    outputs: list[str] = field(default_factory=list)  # what the tools returned, as text
    inputs: list[str] = field(default_factory=list)  # the arguments, e.g. {"hours": 24}
    proposals: Any = None
    seconds: float | None = None
    cost_usd: float | None = None

    @property
    def ungrounded(self) -> list[str]:
        """Numbers not in the question, the tool arguments or the tool results. The "now" the
        model is given is not traced, so a time it derives from it shows up here."""
        return ungrounded_numbers(self.answer, [self.question, *self.inputs, *self.outputs])


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _metadata(obs: dict[str, Any], key: str) -> Any:
    meta = obs.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return None
    if not isinstance(meta, dict):
        return None
    if f"attributes.{key}" in meta:  # how Langfuse returns OTel span attributes (flat keys)
        return meta[f"attributes.{key}"]
    attrs = meta.get("attributes", meta)
    return attrs.get(key, meta.get(key)) if isinstance(attrs, dict) else meta.get(key)


def _seconds(obs: dict[str, Any]) -> float | None:
    try:
        start = datetime.fromisoformat(obs["startTime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(obs["endTime"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return None
    return round((end - start).total_seconds(), 3)


def parse(observations: list[dict[str, Any]]) -> Reviewed:
    """The observations of one trace (rows of the v2 observations API) as a reviewed run.
    Input and output come back as raw strings; tool outputs are compared as text."""
    observations = sorted(observations, key=lambda o: o.get("startTime") or "")
    root: dict[str, Any] = next((o for o in observations if o.get("name") == ROOT), {})
    cost = _metadata(root, "cost_usd")  # our own figure; Langfuse leaves totalCost empty
    first = observations[0] if observations else {}
    run = Reviewed(
        trace_id=root.get("traceId") or first.get("traceId", ""),
        when=(root.get("startTime") or first.get("startTime") or "")[:16].replace("T", " "),
        model=_text(_metadata(root, "model") or ""),
        question=_text(root.get("input") or ""),
        answer=_text(root.get("output") or ""),
        proposals=_metadata(root, "proposals"),
        seconds=_seconds(root),
        cost_usd=float(cost) if isinstance(cost, int | float | str) and cost != "" else None,
    )
    for o in observations:
        name = o.get("name") or ""
        if name.startswith("tool "):
            run.tools.append((name.removeprefix("tool "), o.get("input")))
            run.outputs.append(_text(o.get("output") or ""))
            run.inputs.append(_text(o.get("input") or ""))
    return run


def is_eval(root: dict[str, Any]) -> bool:
    return _metadata(root, "eval_case") is not None


RAW = Path("runs/review/raw.json")  # observation rows as Langfuse returned them (--raw)


async def fetch(limit: int, hours: int, raw: bool = False) -> list[Reviewed]:
    public, secret = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not (public and secret):
        sys.exit("Langfuse keys are not set.")
    host = os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com"
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    roots_query: dict[str, Any] = {  # the filter wins over fixed parameters, so time goes in it
        "filter": json.dumps(
            [
                {"type": "string", "column": "name", "operator": "=", "value": ROOT},
                {"type": "datetime", "column": "startTime", "operator": ">=", "value": since},
            ]
        ),
        "fields": "core,basic,io,metadata",
        "limit": 1000,
    }
    async with httpx.AsyncClient(
        base_url=host, headers={"Authorization": f"Basic {auth}"}, timeout=30
    ) as client:
        roots = [r for r in await _all(client, roots_query) if not is_eval(r)]
        roots.sort(key=lambda r: r.get("startTime") or "", reverse=True)  # newest first
        traces = [
            await _all(client, {"traceId": r["traceId"], "fields": "core,basic,io,metadata"})
            for r in roots[:limit]
        ]
    if raw:
        RAW.parent.mkdir(parents=True, exist_ok=True)
        RAW.write_text(json.dumps(traces, indent=1, ensure_ascii=False), encoding="utf-8")
    return [parse(rows) for rows in traces]


async def _all(client: httpx.AsyncClient, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every row of a v2 observations query, following the cursor."""
    params, rows = dict(params), []
    while True:  # a page may be short and still carry a cursor
        response = await client.get("/api/public/v2/observations", params=params)
        if response.status_code >= 300:
            sys.exit(f"Langfuse: HTTP {response.status_code} {response.text[:300]}")
        page = response.json()
        rows += page.get("data", [])
        cursor = (page.get("meta") or {}).get("cursor")
        if not cursor:
            return rows
        params["cursor"] = cursor


def render(runs: list[Reviewed]) -> str:
    flagged = sum(bool(r.ungrounded) for r in runs)
    lines = [f"{len(runs)} questions · {flagged} with figures no tool gave", ""]
    for r in reversed(runs):  # oldest first, as they were asked
        cost = f" · ${r.cost_usd:.5f}" if r.cost_usd is not None else ""
        secs = f" · {r.seconds:.1f}s" if r.seconds is not None else ""
        lines.append(f"── {r.when} UTC · {r.model}{secs}{cost} · trace {r.trace_id}")
        lines.append(f"Q: {r.question}")
        lines.append(f"A: {r.answer}")
        tools = ", ".join(f"{n}({_text(a) if a else ''})" for n, a in r.tools) or "none"
        lines.append(f"   tools: {tools}")
        if r.proposals and r.proposals != "[]":
            lines.append(f"   proposals: {_text(r.proposals)}")
        checks = []
        if r.ungrounded:
            checks.append(f"FIGURES NOT FROM TOOLS: {', '.join(r.ungrounded)}")
        if issues := style_issues(r.answer):
            checks.append(f"style: {', '.join(issues)}")
        lines.append(f"   check: {'; '.join(checks) or 'ok'}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--hours", type=int, default=48, help="how far back to look")
    parser.add_argument("--raw", action="store_true", help=f"also save the rows to {RAW}")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    text = render(asyncio.run(fetch(args.limit, args.hours, args.raw)))
    out = Path("runs/review") / f"{datetime.now(UTC):%Y%m%dT%H%M%S}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"saved to {out}")


if __name__ == "__main__":
    main()
