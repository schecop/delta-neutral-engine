"""The secrets hook (.claude/hooks/protect_secrets.py), fed real hook payloads via stdin."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HOOK = Path(__file__).parents[1] / ".claude" / "hooks" / "protect_secrets.py"


def _denied(tool: str, **tool_input: Any) -> bool:
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input})
    out = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, check=True
    ).stdout
    return bool(out) and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": r"C:\Users\pablo\PP\delta-neutral\.env"}),
        ("Read", {"file_path": "/c/x/.env.local"}),
        ("Grep", {"pattern": "KEY", "path": ".env"}),
        ("Glob", {"pattern": "**/.env"}),
        ("Bash", {"command": "cat .env"}),
        ("Bash", {"command": "grep KEY .env | sed s/=.*//"}),
        ("Bash", {"command": "echo $ANTHROPIC_API_KEY"}),
        ("Bash", {"command": "echo ${TELEGRAM_BOT_TOKEN}"}),
        ("Bash", {"command": "env"}),
        ("Bash", {"command": "printenv | grep KEY"}),
        ("Bash", {"command": "uv run python -c 'import os; print(os.environ)'"}),
        ("PowerShell", {"command": "Get-ChildItem env:"}),
        ("PowerShell", {"command": "$env:OPENAI_API_KEY"}),
        ("PowerShell", {"command": "Get-Content .env"}),
    ],
)
def test_blocked(tool: str, tool_input: dict[str, Any]) -> None:
    assert _denied(tool, **tool_input)


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": r"C:\x\.env.example"}),
        ("Read", {"file_path": r"C:\x\.venv\lib\site.py"}),
        ("Read", {"file_path": r"C:\x\src\delta_neutral\env.py"}),
        ("Edit", {"file_path": "src/x.py", "new_string": 'key_env = "OPENAI_API_KEY"'}),
        ("Bash", {"command": "cat .env.example"}),
        ("Bash", {"command": "uv run pytest -q"}),
        ("Bash", {"command": "uv run python -m delta_neutral.supervisor.evals --only anthropic"}),
        ("Bash", {"command": "git status --short && ls src"}),
        ("Bash", {"command": "grep -rn load_env src"}),
    ],
)
def test_allowed(tool: str, tool_input: dict[str, Any]) -> None:
    assert not _denied(tool, **tool_input)


def test_unreadable_input_is_blocked() -> None:
    out = subprocess.run(
        [sys.executable, str(HOOK)], input="not json", capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
