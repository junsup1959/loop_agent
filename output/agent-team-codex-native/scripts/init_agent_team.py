#!/usr/bin/env python3
"""Initialize or verify the complete project-local agent team control plane."""

from __future__ import annotations

import argparse
from http.client import HTTPConnection, HTTPException
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from typing import Any, Sequence

try:
    from scripts.agent_team_layout import AgentTeamLayout
    from scripts.agent_team_context import ContextProfileCatalog, ContextSelectionError
    from scripts.agent_team_state import AxStateStore, LATEST_SCHEMA_VERSION
    from scripts.project_agents import (
        AgentConfigurationError,
        compile_runtime_agent,
        initialize_seats,
        list_seats,
        load_and_validate as load_and_validate_agents,
        synchronize as synchronize_agents,
    )
    from scripts.project_skills import (
        SkillConfigurationError,
        load_catalog,
        synchronize as synchronize_skills,
        validate_catalog,
    )
    from scripts.serena_project_knowledge import ProjectKnowledgeError, load_policy
except ModuleNotFoundError:
    from agent_team_layout import AgentTeamLayout
    from agent_team_context import ContextProfileCatalog, ContextSelectionError  # type: ignore[no-redef]
    from agent_team_state import (  # type: ignore[no-redef]
        AxStateStore,
        LATEST_SCHEMA_VERSION,
    )
    from project_agents import (  # type: ignore[no-redef]
        AgentConfigurationError,
        compile_runtime_agent,
        initialize_seats,
        list_seats,
        load_and_validate as load_and_validate_agents,
        synchronize as synchronize_agents,
    )
    from project_skills import (  # type: ignore[no-redef]
        SkillConfigurationError,
        load_catalog,
        synchronize as synchronize_skills,
        validate_catalog,
    )
    from serena_project_knowledge import (  # type: ignore[no-redef]
        ProjectKnowledgeError,
        load_policy,
    )


LAYOUT = AgentTeamLayout.discover(Path(__file__))
PROJECT_ROOT = LAYOUT.source_root
DEFAULT_RUNTIME_ROOT = (
    Path(os.environ.get("LOCALAPPDATA", PROJECT_ROOT.parent))
    / "agent-team-ax"
    / PROJECT_ROOT.name
)
CODEX_CONFIG_PATH = PROJECT_ROOT / ".codex" / "config.toml"
TEAM_CONFIG_PATH = LAYOUT.team_path
PYTHON_REQUIREMENTS_PATH = PROJECT_ROOT / "scripts" / "requirements.txt"
LEGACY_MCP_PACKAGE_PATH = PROJECT_ROOT / "mcp-package"
SEQUENTIAL_THINKING_PACKAGE = "@modelcontextprotocol/server-sequential-thinking"
SEQUENTIAL_THINKING_VERSION = "2026.7.4"
SEQUENTIAL_THINKING_PACKAGE_PATH = Path("@modelcontextprotocol") / "server-sequential-thinking"
SEQUENTIAL_THINKING_ENTRYPOINT = Path("dist") / "index.js"
SUPPORTED_MCP_CAPABILITIES = frozenset({"serena", "sequentialthinking"})
SERENA_REQUIRED_TOOL = "initial_instructions"
MAX_AGENT_THREADS = 6
FIXED_SEAT_COUNT = 5
MANAGED_CONFIG_MARKER = "# agent-team-managed: init_agent_team.py"
MCP_PROBE_ID = "agent-team-initializer-probe"
MCP_PROTOCOL_VERSION = "2025-03-26"
RUNTIME_DIRECTORIES = (
    "state",
    "state/locks",
    "state/repositories",
    "repositories",
    "workspaces",
    "activations",
    "artifacts",
    "artifacts/contexts",
    "artifacts/results",
    "artifacts/builds",
    "artifacts/tests",
    "artifacts/reviews",
    "artifacts/integrations",
    "artifacts/releases",
    "artifacts/serena",
    "state/project-knowledge",
    "logs",
)
SERENA_ENABLED_TOOLS = (
    "initial_instructions",
    "check_onboarding_performed",
    "onboarding",
    "list_memories",
    "read_memory",
    "write_memory",
    "find_file",
    "list_dir",
    "read_file",
    "search_for_pattern",
    "get_symbols_overview",
    "find_symbol",
    "find_referencing_symbols",
)
REQUIRED_QUEUE_TABLES = {
    "messages",
    "outbox",
    "project_knowledge_state",
    "thread_snapshots",
}
REQUIRED_AGENT_FIELDS = {
    "name",
    "description",
    "developer_instructions",
    "model",
    "model_reasoning_effort",
    "sandbox_mode",
}
PINNED_REQUIREMENT_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s;#]+)$"
)


class InitializationError(RuntimeError):
    """Raised when project initialization or verification fails."""


def _inside_project(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise InitializationError(
            f"Runtime path must remain inside the project: {resolved}"
        ) from exc
    return resolved


def _resolve_runtime_root(value: str | None) -> Path:
    configured = Path(value) if value is not None else DEFAULT_RUNTIME_ROOT
    if value is not None and not configured.is_absolute():
        raise InitializationError("AX_ROOT must be an absolute path.")
    resolved = configured.expanduser().resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        return resolved
    raise InitializationError(
        f"AX_ROOT must be independent from the Agent-Team source root: {resolved}"
    )


def _inside_runtime(runtime_root: Path, path: Path) -> Path:
    root = runtime_root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise InitializationError(f"Path escapes AX_ROOT: {resolved}") from exc
    return resolved


def _configured_team_path(setting: str) -> Path:
    if not TEAM_CONFIG_PATH.is_file():
        raise InitializationError(f"Project team configuration is missing: {TEAM_CONFIG_PATH}")
    try:
        with TEAM_CONFIG_PATH.open("rb") as stream:
            data = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise InitializationError(f"Invalid project team configuration: {TEAM_CONFIG_PATH}") from exc
    team = data.get("team")
    value = team.get(setting) if isinstance(team, dict) else None
    if not isinstance(value, str) or not value:
        raise InitializationError(
            f"Project team configuration must declare team.{setting} as a relative path."
        )
    configured = Path(value)
    if configured.is_absolute():
        raise InitializationError(f"Project team setting {setting} must be relative: {value}")
    normalized = value.replace("\\", "/")
    if normalized.split("/", 1)[0] in {"agents", "skills", "profile", "scripts"}:
        return LAYOUT.resolve_source_path(normalized)
    return _inside_project(PROJECT_ROOT / configured)


def _runtime_paths(runtime_root: Path) -> list[Path]:
    return [
        _inside_runtime(runtime_root, runtime_root / relative)
        for relative in RUNTIME_DIRECTORIES
    ]


def _initialize_runtime_layout(runtime_root: Path) -> None:
    for path in _runtime_paths(runtime_root):
        path.mkdir(parents=True, exist_ok=True)


def _check_runtime_layout(runtime_root: Path) -> None:
    missing = [path for path in _runtime_paths(runtime_root) if not path.is_dir()]
    if missing:
        raise InitializationError(
            f"Missing runtime directories: {[str(path) for path in missing]}"
        )


def _run_checked(
    command: list[str],
    *,
    cwd: Path = PROJECT_ROOT,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_environment = os.environ.copy()
    if environment:
        merged_environment.update(environment)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=merged_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise InitializationError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n{detail}"
        )
    return completed


def _command_path(command: str) -> str:
    resolved = shutil.which(command)
    if resolved is None:
        raise InitializationError(f"Required command is not available: {command}")
    return resolved


def _npm_command() -> str:
    return _command_path("npm.cmd" if os.name == "nt" else "npm")


def _global_npm_root() -> Path:
    completed = _run_checked([_npm_command(), "root", "--global"])
    roots = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(roots) != 1:
        raise InitializationError(
            "npm root --global did not return exactly one global module directory."
        )
    root = Path(roots[0])
    if not root.is_absolute():
        raise InitializationError(f"npm global module directory is not absolute: {root}")
    return root


def _serena_stdio_args() -> list[str]:
    return [
        "start-mcp-server",
        "--project-from-cwd",
        "--context",
        "codex",
        "--transport",
        "stdio",
        "--enable-web-dashboard",
        "false",
        "--open-web-dashboard",
        "false",
    ]


def _serena_stdio_capability() -> dict[str, Any]:
    return {
        "transport": "stdio",
        "command": _command_path("serena"),
        "args": _serena_stdio_args(),
        "cwd": str(PROJECT_ROOT),
    }


def _serena_config_path() -> Path:
    profile_root = os.environ.get("USERPROFILE")
    if profile_root:
        return Path(profile_root) / ".serena" / "serena_config.yml"
    return Path.home() / ".serena" / "serena_config.yml"


def _ensure_serena_initialized() -> dict[str, Any]:
    """Verify setup performed by the project-local Serena setup skill."""
    return _check_serena_initialized()


def _close_mcp_session(host: str, port: int, endpoint_path: str, session_id: str) -> None:
    connection: HTTPConnection | None = None
    try:
        connection = HTTPConnection(host, port, timeout=0.75)
        connection.request("DELETE", endpoint_path, headers={"Mcp-Session-Id": session_id})
        response = connection.getresponse()
        response.read()
    except (HTTPException, OSError):
        return
    finally:
        if connection is not None:
            connection.close()


def _mcp_endpoint_is_ready(host: str, port: int, endpoint_path: str) -> bool:
    payload = {
        "jsonrpc": "2.0",
        "id": MCP_PROBE_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agent-team-initializer", "version": "1"},
        },
    }
    connection: HTTPConnection | None = None
    session_id: str | None = None
    try:
        connection = HTTPConnection(host, port, timeout=0.75)
        connection.request(
            "POST",
            endpoint_path,
            body=json.dumps(payload),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        session_id = response.getheader("Mcp-Session-Id")
        response_payload = json.loads(response.read().decode("utf-8"))
        return (
            response.status == 200
            and isinstance(response_payload, dict)
            and response_payload.get("jsonrpc") == "2.0"
            and response_payload.get("id") == MCP_PROBE_ID
            and isinstance(response_payload.get("result"), dict)
        )
    except (HTTPException, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    finally:
        if connection is not None:
            connection.close()
        if session_id:
            _close_mcp_session(host, port, endpoint_path, session_id)


def _check_serena_initialized() -> dict[str, Any]:
    executable = _command_path("serena")
    config_path = _serena_config_path()
    if not config_path.is_file():
        raise InitializationError(
            "Serena CLI configuration is missing. Run the project-local "
            "serena-project-setup skill before initializing the team."
        )
    project_config = PROJECT_ROOT / ".serena" / "project.yml"
    if not project_config.is_file():
        raise InitializationError(
            "Missing project Serena configuration. Run the project-local "
            "serena-project-setup skill before initializing the team."
        )
    project_text = project_config.read_text(encoding="utf-8")
    if re.search(r"(?m)^languages:\s*\[\s*\]\s*$", project_text):
        raise InitializationError(
            "Serena project languages are not configured. Use the project-local "
            "serena-project-setup skill to set languages and index the project."
        )
    maintenance = PROJECT_ROOT / ".serena" / "memories" / "memory_maintenance.md"
    if not maintenance.is_file():
        raise InitializationError(
            "Serena memory layout is not initialized. Run 'serena memories initialize' "
            "through the project-local serena-project-setup skill."
        )
    return {
        "command": executable,
        "global_config": str(config_path),
        "initialized_now": False,
        "project_config": str(project_config),
        "setup_owner": "project-local serena-project-setup skill",
    }


def _load_serena_service_endpoint_from_path(
    config_path: Path, runtime_root: Path
) -> dict[str, Any]:
    try:
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise InitializationError(f"Invalid Serena service TOML: {config_path}: {exc}") from exc
    service = data.get("service")
    if not isinstance(service, dict) or service.get("version") != 1:
        raise InitializationError("Serena service configuration must declare version = 1.")
    if service.get("transport") != "streamable-http":
        raise InitializationError("Serena service transport must be streamable-http.")
    if service.get("port_strategy") != "random_persisted":
        raise InitializationError("Serena service port strategy must be random_persisted.")
    host = service.get("host")
    endpoint_path = service.get("endpoint_path")
    state_file = service.get("state_file")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise InitializationError("Serena service host must be a loopback address.")
    if not isinstance(endpoint_path, str) or not endpoint_path.startswith("/"):
        raise InitializationError("Serena service endpoint_path must start with '/'.")
    if not isinstance(state_file, str) or not state_file:
        raise InitializationError("Serena service state_file must be a relative path.")
    state_path = _inside_project(PROJECT_ROOT / state_file)
    expected_state_path = _inside_runtime(
        runtime_root, runtime_root / "state" / "serena-service.json"
    )
    if state_path != expected_state_path:
        raise InitializationError(
            "Serena service state_file must match the selected runtime root: "
            f"expected {expected_state_path}, actual {state_path}"
        )
    if not state_path.is_file():
        raise InitializationError(
            "Shared Serena HTTP service state is missing. Run the project-local "
            "serena-project-setup skill before initializing or refreshing MCP config."
        )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InitializationError(f"Invalid Serena service state JSON: {state_path}") from exc
    if not isinstance(state, dict) or state.get("version") != 1:
        raise InitializationError("Serena service state has an unsupported schema.")
    port = state.get("port")
    if not isinstance(port, int) or port < 1024 or port > 65535:
        raise InitializationError("Serena service state must contain an unprivileged TCP port.")
    expected_url = f"http://{host}:{port}{endpoint_path}"
    if state.get("url") != expected_url:
        raise InitializationError(
            "Serena service URL does not match the configured loopback endpoint."
        )
    if state.get("transport") != "streamable-http":
        raise InitializationError("Serena service state must use streamable-http.")
    if state.get("health_status") != "passed" or not isinstance(
        state.get("health_checked_at"), str
    ):
        raise InitializationError(
            "Shared Serena HTTP service has no successful health-check record. Run the "
            "project-local serena-project-setup skill before initializing the team."
        )
    if not isinstance(state.get("project_path"), str) or not state["project_path"]:
        raise InitializationError("Serena service state must name its active project path.")
    try:
        active_project = Path(state["project_path"]).resolve()
    except OSError as exc:
        raise InitializationError("Serena service state has an invalid active project path.") from exc
    if active_project != PROJECT_ROOT:
        raise InitializationError(
            "Shared Serena HTTP service targets a different project. Run the project-local "
            "serena-project-setup skill to start the service for this project."
        )
    if not _mcp_endpoint_is_ready(host, port, endpoint_path):
        raise InitializationError(
            "Shared Serena HTTP service is not a ready Streamable MCP endpoint. Run the "
            "project-local serena-project-setup skill to start or repair it."
        )
    return {
        "config_path": str(config_path),
        "state_path": str(state_path),
        "url": expected_url,
        "host": host,
        "port": port,
        "project_path": state["project_path"],
        "pid": state.get("pid"),
    }


def _load_serena_service_endpoint(
    agent_bundle: dict[str, Any], runtime_root: Path
) -> dict[str, Any]:
    return _load_serena_service_endpoint_from_path(
        agent_bundle["serena_service_path"], runtime_root
    )


def _pinned_python_requirements() -> dict[str, str]:
    if not PYTHON_REQUIREMENTS_PATH.is_file():
        raise InitializationError(f"Requirements file not found: {PYTHON_REQUIREMENTS_PATH}")
    requirements: dict[str, str] = {}
    for raw_line in PYTHON_REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = PINNED_REQUIREMENT_PATTERN.fullmatch(line)
        if match is None:
            raise InitializationError(
                "Only exact package pins are supported by the initializer: "
                f"{raw_line!r}"
            )
        name = match.group("name").lower().replace("_", "-")
        if name in requirements:
            raise InitializationError(f"Duplicate Python requirement: {name}")
        requirements[name] = match.group("version")
    if not requirements:
        raise InitializationError("Requirements file must contain at least one package.")
    return requirements


def _installed_python_requirements(
    requirements: dict[str, str],
) -> dict[str, str | None]:
    installed: dict[str, str | None] = {}
    for name in requirements:
        try:
            installed[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed[name] = None
    return installed


def _airflow_constraint_url(requirements: dict[str, str]) -> str | None:
    airflow_version = requirements.get("apache-airflow")
    if airflow_version is None:
        return None
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return (
        "https://raw.githubusercontent.com/apache/airflow/constraints-"
        f"{airflow_version}/constraints-{python_version}.txt"
    )


def _run_pip_check() -> None:
    _run_checked([sys.executable, "-m", "pip", "check"])


def _ensure_python_dependencies() -> dict[str, Any]:
    requirements = _pinned_python_requirements()
    installed = _installed_python_requirements(requirements)
    mismatched = {
        name: {"expected": expected, "actual": installed[name]}
        for name, expected in requirements.items()
        if installed[name] != expected
    }
    installed_now = False
    if mismatched:
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
        ]
        constraint_url = _airflow_constraint_url(requirements)
        if constraint_url is not None:
            command.extend(["--constraint", constraint_url])
        command.extend(["-r", str(PYTHON_REQUIREMENTS_PATH)])
        _run_checked(command)
        installed_now = True
    _run_pip_check()
    final_installed = _installed_python_requirements(requirements)
    final_mismatched = {
        name: {"expected": expected, "actual": final_installed[name]}
        for name, expected in requirements.items()
        if final_installed[name] != expected
    }
    if final_mismatched:
        raise InitializationError(
            f"Python dependency versions do not match requirements: {final_mismatched}"
        )
    return {
        "requirements": requirements,
        "installed_now": installed_now,
        "python": sys.executable,
    }


def _check_python_dependencies() -> dict[str, Any]:
    requirements = _pinned_python_requirements()
    installed = _installed_python_requirements(requirements)
    mismatched = {
        name: {"expected": expected, "actual": installed[name]}
        for name, expected in requirements.items()
        if installed[name] != expected
    }
    if mismatched:
        raise InitializationError(
            f"Python dependency versions do not match requirements: {mismatched}"
        )
    _run_pip_check()
    return {
        "requirements": requirements,
        "installed_now": False,
        "python": sys.executable,
    }


def _sequential_thinking_paths() -> dict[str, Path]:
    package_root = _global_npm_root() / SEQUENTIAL_THINKING_PACKAGE_PATH
    return {
        "root": package_root,
        "entrypoint": package_root / SEQUENTIAL_THINKING_ENTRYPOINT,
        "package_manifest": package_root / "package.json",
    }


def _check_sequential_thinking() -> dict[str, Any]:
    if LEGACY_MCP_PACKAGE_PATH.exists():
        raise InitializationError(
            "Legacy mcp-package directory must be removed before verification: "
            f"{LEGACY_MCP_PACKAGE_PATH}"
        )
    paths = _sequential_thinking_paths()
    for label in ("package_manifest", "entrypoint"):
        if not paths[label].is_file():
            raise InitializationError(
                f"Sequential Thinking {label} is missing: {paths[label]}"
            )
    try:
        manifest = json.loads(paths["package_manifest"].read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InitializationError(
            f"Invalid Sequential Thinking package metadata: {paths['package_manifest']}"
        ) from exc
    if manifest.get("version") != SEQUENTIAL_THINKING_VERSION:
        raise InitializationError(
            "Sequential Thinking version mismatch: "
            f"expected {SEQUENTIAL_THINKING_VERSION}, "
            f"actual {manifest.get('version')!r}"
        )
    node = _command_path("node")
    _run_checked([node, "--check", str(paths["entrypoint"])])
    return {
        "package": SEQUENTIAL_THINKING_PACKAGE,
        "version": SEQUENTIAL_THINKING_VERSION,
        "entrypoint": str(paths["entrypoint"]),
        "installed_now": False,
    }


def _ensure_sequential_thinking() -> dict[str, Any]:
    if LEGACY_MCP_PACKAGE_PATH.exists():
        raise InitializationError(
            "Legacy mcp-package directory must be removed before initialization: "
            f"{LEGACY_MCP_PACKAGE_PATH}"
        )
    try:
        return _check_sequential_thinking()
    except InitializationError:
        _run_checked(
            [
                _npm_command(),
                "install",
                "--global",
                "--no-audit",
                "--no-fund",
                "--ignore-scripts",
                f"{SEQUENTIAL_THINKING_PACKAGE}@{SEQUENTIAL_THINKING_VERSION}",
            ]
        )
    result = _check_sequential_thinking()
    result["installed_now"] = True
    return result


def install_mcp_dependencies(runtime_root: Path) -> dict[str, Any]:
    """Install MCP executable dependencies without changing Codex configuration."""

    return {
        "status": "ok",
        "operation": "install-mcp-dependencies",
        "runtime_root": str(runtime_root),
        "serena": _serena_stdio_capability(),
        "sequentialthinking": _ensure_sequential_thinking(),
    }


def check_mcp_dependencies(runtime_root: Path) -> dict[str, Any]:
    """Verify MCP executable dependencies without changing configuration."""

    return {
        "status": "ok",
        "operation": "check-mcp-dependencies",
        "runtime_root": str(runtime_root),
        "serena": _serena_stdio_capability(),
        "sequentialthinking": _check_sequential_thinking(),
    }


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_string_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _render_codex_config(
    agent_bundle: dict[str, Any], runtime_root: Path
) -> str:
    seats = list_seats(agent_bundle)
    fixed = [seat for seat in seats if seat.get("slot_type") == "fixed"]
    elastic = [seat for seat in seats if seat.get("slot_type") == "elastic"]
    if len(fixed) != FIXED_SEAT_COUNT or len(elastic) != 1:
        raise InitializationError(
            "Expected five fixed seats and one elastic slot, found "
            f"fixed={len(fixed)}, elastic={len(elastic)}."
        )
    seat_lines = [
        f"# - {seat.get('seat_id') or seat.get('slot_id')} -> "
        f"{seat['slot_key']} ({', '.join(seat['eligible_capabilities'])})"
        for seat in seats
    ]
    project_cwd = str(PROJECT_ROOT)
    sequential_entrypoint = str(_sequential_thinking_paths()["entrypoint"])
    return "\n".join(
        [
            MANAGED_CONFIG_MARKER,
            "# Generated by scripts/init_agent_team.py. Do not edit manually.",
            "# Custom seat agents are auto-discovered from .codex/agents/.",
            "# MCP server definitions follow sample_config.toml: Serena stdio and global npm.",
            "# Current seat assignments:",
            *seat_lines,
            "",
            "[agents]",
            f"max_threads = {MAX_AGENT_THREADS}",
            "max_depth = 1",
            "interrupt_message = true",
            "",
            "[mcp_servers.serena]",
            'command = "serena"',
            "args = " + _toml_string_list(_serena_stdio_args()),
            "cwd = " + _toml_string(project_cwd),
            "enabled = true",
            "required = true",
            "enabled_tools = " + _toml_string_list(list(SERENA_ENABLED_TOOLS)),
            "startup_timeout_sec = 45",
            "tool_timeout_sec = 120",
            "",
            "[mcp_servers.sequentialthinking]",
            'command = "node"',
            "args = " + _toml_string_list([sequential_entrypoint]),
            "cwd = " + _toml_string(project_cwd),
            "enabled = true",
            "required = true",
            "startup_timeout_sec = 30",
            "tool_timeout_sec = 120",
            "",
        ]
    )


def _ensure_codex_config(agent_bundle: dict[str, Any], runtime_root: Path) -> None:
    CODEX_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    expected = _render_codex_config(agent_bundle, runtime_root)
    if CODEX_CONFIG_PATH.is_file():
        current = CODEX_CONFIG_PATH.read_text(encoding="utf-8")
        if MANAGED_CONFIG_MARKER not in current:
            raise InitializationError(
                "Refusing to overwrite an unmanaged project Codex config: "
                f"{CODEX_CONFIG_PATH}"
            )
        if current == expected:
            return
    CODEX_CONFIG_PATH.write_text(expected, encoding="utf-8", newline="\n")


def _check_serena_tool_allowlist(server: dict[str, Any]) -> None:
    enabled_tools = server.get("enabled_tools")
    if enabled_tools != list(SERENA_ENABLED_TOOLS):
        raise InitializationError(
            "Serena enabled_tools must be the explicit source-controlled "
            f"allowlist and include {SERENA_REQUIRED_TOOL!r}."
        )


def _check_codex_config(agent_bundle: dict[str, Any], runtime_root: Path) -> None:
    if not CODEX_CONFIG_PATH.is_file():
        raise InitializationError(f"Project Codex config not found: {CODEX_CONFIG_PATH}")
    with CODEX_CONFIG_PATH.open("rb") as stream:
        config = tomllib.load(stream)
    agents = config.get("agents")
    if not isinstance(agents, dict) or agents.get("max_threads") != MAX_AGENT_THREADS:
        raise InitializationError(
            f"Project Codex config must set agents.max_threads to {MAX_AGENT_THREADS}."
        )
    mcp_servers = config.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        raise InitializationError("Project Codex config must define MCP servers.")
    expected_mcp_servers = {"serena", "sequentialthinking"}
    missing_mcp_servers = expected_mcp_servers - set(mcp_servers)
    if missing_mcp_servers:
        raise InitializationError(
            "Project Codex config is missing required MCP servers: "
            f"{sorted(missing_mcp_servers)}"
        )
    project_cwd = str(PROJECT_ROOT)
    sequential_entrypoint = str(_sequential_thinking_paths()["entrypoint"])
    expected_servers = {
        "serena": {
            "command": "serena",
            "args": _serena_stdio_args(),
            "cwd": project_cwd,
            "enabled": True,
            "required": True,
            "enabled_tools": list(SERENA_ENABLED_TOOLS),
        },
        "sequentialthinking": {
            "command": "node",
            "args": [sequential_entrypoint],
            "cwd": project_cwd,
            "enabled": True,
            "required": True,
        },
    }
    for name, expected in expected_servers.items():
        server = mcp_servers[name]
        if not isinstance(server, dict):
            raise InitializationError(f"Project Codex MCP server must be a table: {name}")
        mismatched = {
            field: {"expected": value, "actual": server.get(field)}
            for field, value in expected.items()
            if server.get(field) != value
        }
        if mismatched:
            raise InitializationError(
                f"Project Codex MCP server does not match sample_config.toml semantics: "
                f"{name}: {mismatched}"
            )
        if name == "serena":
            _check_serena_tool_allowlist(server)


def _check_skill_mirror(
    catalog: dict[str, Any],
    skill_index: dict[str, dict[str, Any]],
) -> None:
    del catalog
    source_root = LAYOUT.skill_root
    for skill_id in skill_index:
        for relative in (Path("SKILL.md"), Path("agents/openai.yaml")):
            source = _inside_project(source_root / skill_id / relative)
            if not source.is_file():
                raise InitializationError(f"Missing canonical skill file: {source}")


def _check_agent_mirror(bundle: dict[str, Any]) -> None:
    runtime_root: Path = bundle["runtime_root"]
    expected_seat_ids = set(bundle["seats"])
    actual_paths = list(runtime_root.glob("*.toml")) if runtime_root.is_dir() else []
    actual_seat_ids = {path.stem for path in actual_paths}
    if actual_seat_ids != expected_seat_ids:
        raise InitializationError(
            "Runtime agent set mismatch. "
            f"Missing={sorted(expected_seat_ids - actual_seat_ids)}, "
            f"extra={sorted(actual_seat_ids - expected_seat_ids)}"
        )

    for seat_id in expected_seat_ids:
        path = _inside_project(runtime_root / f"{seat_id}.toml")
        expected = compile_runtime_agent(bundle, seat_id)
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            raise InitializationError(f"Runtime agent mirror mismatch: {path}")
        with path.open("rb") as stream:
            parsed = tomllib.load(stream)
        missing_fields = REQUIRED_AGENT_FIELDS - set(parsed)
        if missing_fields:
            raise InitializationError(
                f"Runtime agent is missing fields {sorted(missing_fields)}: {path}"
            )


def _check_database(db_path: Path) -> None:
    if not db_path.is_file():
        raise InitializationError(f"SQLite database not found: {db_path}")
    uri = f"{db_path.as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            schema_row = connection.execute(
                "SELECT schema_version FROM ax_schema_meta WHERE singleton = 1"
            ).fetchone()
    except sqlite3.Error as exc:
        raise InitializationError(f"Cannot read SQLite database {db_path}: {exc}") from exc
    actual_tables = {row[0] for row in rows}
    missing_tables = REQUIRED_QUEUE_TABLES - actual_tables
    if missing_tables:
        raise InitializationError(
            f"SQLite database is missing tables: {sorted(missing_tables)}"
        )
    if schema_row is None or schema_row[0] != LATEST_SCHEMA_VERSION:
        raise InitializationError(
            "SQLite control state must use AX schema "
            f"v{LATEST_SCHEMA_VERSION}."
        )


def _check_context_profiles(agent_bundle: dict[str, Any]) -> dict[str, Any]:
    try:
        catalog = ContextProfileCatalog()
        role_defaults = {
            role: catalog.resolve(target_role=role, requested_profile="auto").profile
            for role in sorted(agent_bundle["capabilities"])
        }
    except ContextSelectionError as exc:
        raise InitializationError(f"Invalid context profile catalog: {exc}") from exc
    return {
        "path": str(catalog.path),
        "default_profiles": role_defaults,
    }


def _check_serena_knowledge_policy(agent_bundle: dict[str, Any]) -> dict[str, Any]:
    policy_path = _configured_team_path("serena_knowledge_policy")
    try:
        policy = load_policy(policy_path)
    except ProjectKnowledgeError as exc:
        raise InitializationError(f"Invalid Serena knowledge policy: {exc}") from exc
    role_keys = set(agent_bundle["capabilities"])
    if policy.owner_role not in role_keys:
        raise InitializationError(
            f"Serena knowledge owner role is not configured: {policy.owner_role}"
        )
    if policy.architecture_evidence_role not in role_keys:
        raise InitializationError(
            "Serena architecture evidence role is not configured: "
            f"{policy.architecture_evidence_role}"
        )
    if set(policy.role_memory_refs) != role_keys:
        raise InitializationError(
            "Serena role memory references must cover every team role exactly. "
            f"Missing={sorted(role_keys - set(policy.role_memory_refs))}, "
            f"extra={sorted(set(policy.role_memory_refs) - role_keys)}"
        )
    return {
        "path": str(policy_path),
        "owner_role": policy.owner_role,
        "required_memory_names": list(policy.required_memory_names),
    }


def _result(
    *,
    operation: str,
    runtime_root: Path,
    db_path: Path,
    seat_created: bool,
    agent_bundle: dict[str, Any],
    skill_count: int,
    serena: dict[str, Any],
    python_dependencies: dict[str, Any],
    sequential_thinking: dict[str, Any],
    context_profiles: dict[str, Any],
    serena_service: dict[str, Any],
    serena_knowledge_policy: dict[str, Any],
) -> dict[str, Any]:
    seats = list_seats(agent_bundle)
    return {
        "status": "ok",
        "operation": operation,
        "project_root": str(PROJECT_ROOT),
        "runtime_root": str(runtime_root),
        "database": str(db_path),
        "seat_registry_created": seat_created,
        "skill_count": skill_count,
        "agent_count": len(seats),
        "fixed_seat_count": sum(
            seat.get("slot_type") == "fixed" for seat in seats
        ),
        "elastic_slot_count": sum(
            seat.get("slot_type") == "elastic" for seat in seats
        ),
        "agents_max_threads": MAX_AGENT_THREADS,
        "codex_config": str(CODEX_CONFIG_PATH),
        "serena": serena,
        "python_dependencies": python_dependencies,
        "sequential_thinking": sequential_thinking,
        "context_profiles": context_profiles,
        "serena_service": serena_service,
        "serena_knowledge_policy": serena_knowledge_policy,
        "seats": [
            {
                "seat_id": seat.get("seat_id"),
                "slot_id": seat.get("slot_id"),
                "slot_key": seat["slot_key"],
                "slot_type": seat["slot_type"],
                "eligible_capabilities": list(seat["eligible_capabilities"]),
            }
            for seat in seats
        ],
    }


def initialize(runtime_root: Path) -> dict[str, Any]:
    _initialize_runtime_layout(runtime_root)
    python_dependencies = _ensure_python_dependencies()
    serena = _serena_stdio_capability()
    sequential_thinking = _ensure_sequential_thinking()

    catalog = load_catalog()
    skill_index = validate_catalog(catalog)
    synchronize_skills(catalog, skill_index)

    agent_bundle, seat_created = initialize_seats()
    synchronize_agents(agent_bundle)
    context_profiles = _check_context_profiles(agent_bundle)
    serena_service = serena
    serena_knowledge_policy = _check_serena_knowledge_policy(agent_bundle)
    _ensure_codex_config(agent_bundle, runtime_root)

    db_path = _inside_runtime(
        runtime_root, runtime_root / "state" / "agent-team.db"
    )
    AxStateStore(db_path).initialize()

    return _result(
        operation="initialize",
        runtime_root=runtime_root,
        db_path=db_path,
        seat_created=seat_created,
        agent_bundle=agent_bundle,
        skill_count=len(skill_index),
        serena=serena,
        python_dependencies=python_dependencies,
        sequential_thinking=sequential_thinking,
        context_profiles=context_profiles,
        serena_service=serena_service,
        serena_knowledge_policy=serena_knowledge_policy,
    )


def check(runtime_root: Path) -> dict[str, Any]:
    _check_runtime_layout(runtime_root)
    python_dependencies = _check_python_dependencies()
    serena = _serena_stdio_capability()
    sequential_thinking = _check_sequential_thinking()

    catalog = load_catalog()
    skill_index = validate_catalog(catalog)
    _check_skill_mirror(catalog, skill_index)

    agent_bundle = load_and_validate_agents()
    _check_agent_mirror(agent_bundle)
    context_profiles = _check_context_profiles(agent_bundle)
    serena_service = serena
    serena_knowledge_policy = _check_serena_knowledge_policy(agent_bundle)
    _check_codex_config(agent_bundle, runtime_root)

    db_path = _inside_runtime(
        runtime_root, runtime_root / "state" / "agent-team.db"
    )
    _check_database(db_path)

    return _result(
        operation="check",
        runtime_root=runtime_root,
        db_path=db_path,
        seat_created=False,
        agent_bundle=agent_bundle,
        skill_count=len(skill_index),
        serena=serena,
        python_dependencies=python_dependencies,
        sequential_thinking=sequential_thinking,
        context_profiles=context_profiles,
        serena_service=serena_service,
        serena_knowledge_policy=serena_knowledge_policy,
    )


def check_mcp(runtime_root: Path, names: Sequence[str]) -> dict[str, Any]:
    """Check only the requested MCP configuration and executable dependency.

    This intentionally avoids the unrelated team database, skill mirror, Serena HTTP
    service, and full generated-config checks so a focused MCP diagnosis is usable
    before the rest of the control plane is initialized.
    """

    if not CODEX_CONFIG_PATH.is_file():
        raise InitializationError(f"Project Codex config not found: {CODEX_CONFIG_PATH}")
    with CODEX_CONFIG_PATH.open("rb") as stream:
        config = tomllib.load(stream)
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        raise InitializationError("Project Codex config must define MCP servers.")

    project_cwd = str(PROJECT_ROOT)
    expected = {
        "serena": {
            "command": "serena",
            "args": _serena_stdio_args(),
            "cwd": project_cwd,
            "enabled": True,
            "required": True,
            "enabled_tools": list(SERENA_ENABLED_TOOLS),
        },
    }
    checked: dict[str, Any] = {}
    for name in names:
        if name == "sequentialthinking":
            expected[name] = {
                "command": "node",
                "args": [str(_sequential_thinking_paths()["entrypoint"])],
                "cwd": project_cwd,
                "enabled": True,
                "required": True,
            }
        server = servers.get(name)
        if not isinstance(server, dict):
            raise InitializationError(f"Project Codex config does not define MCP server: {name}")
        mismatched = {
            field: {"expected": value, "actual": server.get(field)}
            for field, value in expected[name].items()
            if server.get(field) != value
        }
        if mismatched:
            raise InitializationError(
                f"MCP server does not match sample_config.toml semantics: "
                f"{name}: {mismatched}"
            )
        if name == "serena":
            _check_serena_tool_allowlist(server)
            checked[name] = {"available": True, **_serena_stdio_capability()}
        else:
            checked[name] = {"available": True, **_check_sequential_thinking()}
    return {
        "status": "ok",
        "operation": "check-mcp",
        "runtime_root": str(runtime_root),
        "checked": checked,
    }


def refresh_mcp_config(runtime_root: Path) -> dict[str, Any]:
    """Regenerate the managed MCP configuration from the stdio sample contract."""
    agent_bundle = load_and_validate_agents()
    serena = _serena_stdio_capability()
    _check_sequential_thinking()
    _ensure_codex_config(agent_bundle, runtime_root)
    return {
        "status": "ok",
        "operation": "refresh-mcp-config",
        "codex_config": str(CODEX_CONFIG_PATH),
        "serena": serena,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize the project-local agent team control plane."
    )
    parser.add_argument(
        "--ax-root",
        "--runtime-root",
        dest="ax_root",
        help=(
            "Absolute independent AX_ROOT. Defaults to "
            "the LOCALAPPDATA/agent-team-ax/<source-name> directory."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the initialized control plane without modifying it.",
    )
    parser.add_argument(
        "--install-mcp-dependencies",
        action="store_true",
        help="Install Serena/Sequential Thinking dependencies without changing Codex configuration.",
    )
    parser.add_argument(
        "--check-mcp-dependencies",
        action="store_true",
        help="Verify Serena/Sequential Thinking dependencies without changing configuration.",
    )
    parser.add_argument(
        "--check-mcp",
        action="append",
        choices=sorted(SUPPORTED_MCP_CAPABILITIES),
        help="Check one MCP only. Repeat for multiple MCPs.",
    )
    parser.add_argument(
        "--refresh-mcp-config",
        action="store_true",
        help="Regenerate the managed MCP configuration from the Serena stdio sample contract.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the complete result as JSON.",
    )
    return parser


def _print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"Agent team {result['operation']} completed: "
        f"{result['agent_count']} agents, "
        f"{result['skill_count']} skills, "
        f"max_threads={result['agents_max_threads']}, "
        f"database={result['database']}"
    )
    print(
        "Dependencies: "
        f"Serena initialized_now={result['serena']['initialized_now']}, "
        "Python installed_now="
        f"{result['python_dependencies']['installed_now']}, "
        "Sequential Thinking installed_now="
        f"{result['sequential_thinking']['installed_now']}"
    )
    for seat in result["seats"]:
        print(
            f"- {seat['seat_id'] or seat['slot_id']}: {seat['slot_key']} | "
            f"{','.join(seat['eligible_capabilities'])} | {seat['slot_type']}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        selected_operations = sum(
            bool(value)
            for value in (
                args.check,
                args.install_mcp_dependencies,
                args.check_mcp_dependencies,
                args.check_mcp,
                args.refresh_mcp_config,
            )
        )
        if selected_operations > 1:
            raise InitializationError(
                "Use only one of --check, --install-mcp-dependencies, "
                "--check-mcp-dependencies, --check-mcp, or --refresh-mcp-config "
                "per invocation."
            )
        runtime_root = _resolve_runtime_root(args.ax_root)
        if args.install_mcp_dependencies:
            result = install_mcp_dependencies(runtime_root)
        elif args.check_mcp_dependencies:
            result = check_mcp_dependencies(runtime_root)
        elif args.check_mcp:
            result = check_mcp(runtime_root, args.check_mcp)
        elif args.refresh_mcp_config:
            result = refresh_mcp_config(runtime_root)
        else:
            result = check(runtime_root) if args.check else initialize(runtime_root)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif result["operation"] == "refresh-mcp-config":
            print("Agent team MCP configuration refreshed: Serena stdio")
        elif result["operation"] in {
            "install-mcp-dependencies",
            "check-mcp-dependencies",
            "check-mcp",
        }:
            print(f"Agent team {result['operation']} completed.")
        else:
            _print_result(result, False)
        return 0
    except (
        AgentConfigurationError,
        SkillConfigurationError,
        InitializationError,
        ProjectKnowledgeError,
        OSError,
        sqlite3.Error,
    ) as exc:
        print(f"Agent team initialization error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
