"""PreToolUse hook: keep Claude Code away from secrets.

Blocks, before the tool runs:
- any file tool (Read, Edit, Write, Grep, Glob, NotebookEdit...) whose path or pattern names a
  secrets file: `.env` or `.env.<anything>` except `.env.example`;
- any shell command (Bash, PowerShell) that names such a file, expands a secret-looking variable
  ($X_API_KEY, ${X_SECRET}, $env:X_TOKEN, %X_PASSWORD%), or dumps the environment (env,
  printenv, set, export -p, Get-ChildItem env:, os.environ, getenv...).

File *contents* written by Edit/Write are not scanned: code legitimately names variables such as
OPENAI_API_KEY. Reads the hook payload (JSON) on stdin; prints a deny decision on stdout.
Standard library only, so it runs on any Python.
"""

from __future__ import annotations

import json
import re
import sys

SECRET_FILE = re.compile(r"(^|[\\/\s\"'=:<>|;&(])\.env(?!\.example\b)(\.[\w.-]+)?(?=$|[\s\"'\\/;&|)<>])")
SECRET_VAR = re.compile(
    r"\$\{?\w*(API_KEY|SECRET|TOKEN|PASSWORD)\w*\}?"  # bash
    r"|\$env:\w*(API_KEY|SECRET|TOKEN|PASSWORD)"  # PowerShell
    r"|%\w*(API_KEY|SECRET|TOKEN|PASSWORD)\w*%",  # cmd
    re.IGNORECASE,
)
ENV_DUMP = re.compile(
    r"(^|[;&|(`{]\s*)(printenv|env|set|export\s+-p|declare\s+-[px]|compgen\s+-e)\s*($|[;&|>)}])"
    r"|\b(Get-ChildItem|gci|ls|dir|Get-Item|gi)\s+env:"
    r"|\[(System\.)?Environment\]::GetEnvironmentVariables?"
    r"|\bos\.environ\b|\bgetenv\b|/proc/\S*/environ",
    re.IGNORECASE | re.MULTILINE,
)
PATH_FIELDS = ("file_path", "path", "notebook_path", "glob", "pattern")
SHELLS = {"Bash", "PowerShell"}


def reason(tool: str, tool_input: dict[str, object]) -> str | None:
    if tool in SHELLS:
        command = str(tool_input.get("command", ""))
        if SECRET_FILE.search(command):
            return "the command names a secrets file (.env)"
        if SECRET_VAR.search(command):
            return "the command expands a secret environment variable"
        if ENV_DUMP.search(command):
            return "the command reads or dumps the environment"
        return None
    for name in PATH_FIELDS:
        value = tool_input.get(name)
        if isinstance(value, str) and SECRET_FILE.search(value):
            return f"{name} refers to a secrets file (.env)"
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        tool = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input") or {}
        why = reason(tool, tool_input if isinstance(tool_input, dict) else {})
    except Exception as exc:  # fail closed: an input we cannot check is not let through
        why = f"the hook could not check this call ({type(exc).__name__})"
    if why:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"Blocked by protect_secrets hook: {why}. "
                        "Secrets stay out of Claude's context; the operator runs commands "
                        "that need keys.",
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
