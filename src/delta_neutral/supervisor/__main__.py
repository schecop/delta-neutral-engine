"""Ask the supervisor from the terminal (the bot answers the same way on Telegram).

    uv run python -m delta_neutral.supervisor "how much did yesterday's cycles cost?"

Calls the model in config (`llm.supervisor`); needs that provider's key in .env. Traces go to
Langfuse when LANGFUSE_* keys are set.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from delta_neutral.config import load_config
from delta_neutral.env import load_env
from delta_neutral.llm.factory import build_llm
from delta_neutral.supervisor.agent import Supervisor
from delta_neutral.supervisor.tools import SupervisorTools, open_readonly
from delta_neutral.tracing import LangfuseExporter


async def _ask(config_path: Path, question: str) -> None:
    config = load_config(config_path)
    choice = config.llm["supervisor"]
    llm = build_llm(choice)
    exporter = LangfuseExporter.from_env()
    db = open_readonly(config.executor.db_path)
    try:
        run = await Supervisor(llm, choice, SupervisorTools(db), exporter=exporter).ask(question)
    finally:
        await llm.aclose()
        if exporter:
            await exporter.aclose()
        db.close()
    print(run.answer)
    for p in run.proposals:
        print(f"[proposal] {p.action} cycle {p.cycle_id}: {p.reason}")
    print(
        f"\n-- {llm.provider}/{llm.model}: {run.turns} model calls, tools {run.tool_calls}, "
        f"{run.input_tokens} in / {run.output_tokens} out tokens, ${run.cost_usd:.5f}, "
        f"{run.latency_ms / 1000:.1f}s model time, trace {run.trace_id}"
        + ("" if exporter else " (not exported: no LANGFUSE_* keys)")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("question")
    parser.add_argument("--config", type=Path, default=Path("config/local.yaml"))
    args = parser.parse_args()
    load_env()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(_ask(args.config, args.question))


if __name__ == "__main__":
    main()
