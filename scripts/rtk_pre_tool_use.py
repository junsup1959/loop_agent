#!/usr/bin/env python3
"""Enforce the project-local RTK policy for Codex PreToolUse shell calls."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

try:
    from scripts.agent_team_layout import AgentTeamLayout
except ModuleNotFoundError:
    from agent_team_layout import AgentTeamLayout


POLICY_PATH = AgentTeamLayout.discover().resolve_source_path("agents/RTK.md")
RULE_MARKER = "Every native shell command must start with `rtk`."
RTK_PREFIX = re.compile(r"^\s*rtk(?:\.exe)?(?:\s|$)", re.IGNORECASE)
SIMPLE_COMMAND = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\s+[^\r\n;|&<>`$()]*)?\s*$")
SUPPORTED_DIRECT_COMMANDS = {
    "cargo",
    "cmake",
    "dotnet",
    "git",
    "go",
    "java",
    "mvn",
    "node",
    "npm",
    "npx",
    "pip",
    "py",
    "pytest",
    "python",
    "ruff",
    "uv",
}


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False))


def _deny(reason: str) -> None:
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def _rewrite(command: str) -> None:
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"command": command},
            }
        }
    )


def _policy_is_active() -> bool:
    try:
        return RULE_MARKER in POLICY_PATH.read_text(encoding="utf-8")
    except OSError:
        return False


def _command_from_hook_input(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    tool_input = value.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    return command if isinstance(command, str) else None


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        _deny("RTK hook received invalid PreToolUse input.")
        return 0

    command = _command_from_hook_input(hook_input)
    if command is None:
        return 0
    if not _policy_is_active():
        _deny(f"RTK policy is missing or invalid: {POLICY_PATH}")
        return 0
    if not command.strip() or command.lstrip().startswith("#"):
        return 0
    if RTK_PREFIX.match(command):
        return 0

    match = SIMPLE_COMMAND.fullmatch(command)
    if match is not None and match.group(1).lower() in SUPPORTED_DIRECT_COMMANDS:
        _rewrite(f"rtk {command.strip()}")
        return 0

    _deny(
        "RTK policy requires this command to start with 'rtk'. "
        "Use a supported form such as 'rtk git status', or use "
        "'rtk proxy <executable> <arguments>' for a complex command."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
