#!/usr/bin/env python3
"""Start and inspect the project-local shared Serena Streamable HTTP service."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Any, Sequence


class SerenaServiceError(RuntimeError):
    """Raised when the local Serena HTTP service cannot be prepared safely."""


MCP_PROBE_ID = "agent-team-service-probe"
MCP_PROTOCOL_VERSION = "2025-03-26"


@dataclass(frozen=True)
class ServiceConfig:
    root: Path
    host: str
    endpoint_path: str
    context: str
    enable_web_dashboard: bool
    open_web_dashboard: bool
    startup_timeout_seconds: int
    state_path: Path
    log_path: Path


def _inside_root(root: Path, value: str, label: str) -> Path:
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SerenaServiceError(f"{label} must stay inside the project root: {value}") from exc
    return candidate


def load_service_config(path: str | Path) -> ServiceConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise SerenaServiceError(f"Service configuration is missing: {config_path}")
    if config_path.parent.name != "agents":
        raise SerenaServiceError("Service configuration must live beneath the project agents directory")
    root = config_path.parent.parent.resolve()
    with config_path.open("rb") as stream:
        data = tomllib.load(stream)
    service = data.get("service")
    if not isinstance(service, dict) or service.get("version") != 1:
        raise SerenaServiceError("Service configuration must contain [service] version = 1")
    if service.get("transport") != "streamable-http":
        raise SerenaServiceError("Service transport must be streamable-http")
    if service.get("port_strategy") != "random_persisted":
        raise SerenaServiceError("Service port_strategy must be random_persisted")
    host = service.get("host")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SerenaServiceError("Shared Serena service must bind to a loopback host")
    endpoint_path = service.get("endpoint_path")
    if not isinstance(endpoint_path, str) or not endpoint_path.startswith("/"):
        raise SerenaServiceError("Service endpoint_path must begin with '/'")
    context = service.get("context")
    if not isinstance(context, str) or not context:
        raise SerenaServiceError("Service context must be a non-empty string")
    timeout = service.get("startup_timeout_seconds")
    if not isinstance(timeout, int) or timeout < 1 or timeout > 120:
        raise SerenaServiceError("Service startup_timeout_seconds must be between 1 and 120")
    if not isinstance(service.get("enable_web_dashboard"), bool):
        raise SerenaServiceError("Service enable_web_dashboard must be a boolean")
    if not isinstance(service.get("open_web_dashboard"), bool):
        raise SerenaServiceError("Service open_web_dashboard must be a boolean")
    state_file = service.get("state_file")
    log_file = service.get("log_file")
    if not isinstance(state_file, str) or not isinstance(log_file, str):
        raise SerenaServiceError("Service state_file and log_file must be strings")
    return ServiceConfig(
        root=root,
        host=host,
        endpoint_path=endpoint_path,
        context=context,
        enable_web_dashboard=service["enable_web_dashboard"],
        open_web_dashboard=service["open_web_dashboard"],
        startup_timeout_seconds=timeout,
        state_path=_inside_root(root, state_file, "state_file"),
        log_path=_inside_root(root, log_file, "log_file"),
    )


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SerenaServiceError(f"Service state is invalid JSON: {path}") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise SerenaServiceError(f"Service state has an unsupported schema: {path}")
    return value


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


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
    request_payload = {
        "jsonrpc": "2.0",
        "id": MCP_PROBE_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agent-team-service-manager", "version": "1"},
        },
    }
    connection: HTTPConnection | None = None
    session_id: str | None = None
    try:
        connection = HTTPConnection(host, port, timeout=0.75)
        connection.request(
            "POST",
            endpoint_path,
            body=json.dumps(request_payload),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        session_id = response.getheader("Mcp-Session-Id")
        response_payload = json.loads(response.read().decode("utf-8"))
        if response.status != 200 or not isinstance(response_payload, dict):
            return False
        if (
            response_payload.get("jsonrpc") != "2.0"
            or response_payload.get("id") != MCP_PROBE_ID
            or not isinstance(response_payload.get("result"), dict)
        ):
            return False
        return True
    except (HTTPException, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    finally:
        if connection is not None:
            connection.close()
        if session_id:
            _close_mcp_session(host, port, endpoint_path, session_id)


def _select_random_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.bind((host, 0, 0, 0) if family == socket.AF_INET6 else (host, 0))
        return int(listener.getsockname()[1])


def _require_serena() -> str:
    executable = shutil.which("serena")
    if executable is None:
        raise SerenaServiceError("Serena is not on PATH. Run the project setup skill first.")
    return executable


def _health_failure_summary(output: str, returncode: int) -> str:
    details: list[str] = []
    failure = re.search(r"health check failed:\s*([^\r\n]+)", output, re.IGNORECASE)
    if failure:
        details.append(failure.group(1).strip())
    log_path = re.search(r"Log saved to:\s*([^\r\n]+)", output, re.IGNORECASE)
    if log_path:
        details.append(f"Log: {log_path.group(1).strip()}")
    if not details:
        details.append(f"Serena exited with code {returncode}.")
    return " ".join(details)


def _run_health_check(executable: str, project: Path) -> str:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            [executable, "project", "health-check"],
            cwd=project,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except UnicodeError as exc:
        raise SerenaServiceError(
            "Serena project health check could not be rendered with UTF-8. "
            "Repair the Serena environment through the project setup skill before "
            "starting the agent team."
        ) from exc
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0 or "health check failed" in output.casefold():
        raise SerenaServiceError(
            "Serena project health check failed. Resolve it through the project setup "
            "skill before starting the agent team. "
            f"{_health_failure_summary(output, completed.returncode)}"
        )
    return "passed"


def _server_command(
    executable: str,
    config: ServiceConfig,
    project: Path,
    port: int,
) -> list[str]:
    return [
        executable,
        "start-mcp-server",
        "--project",
        str(project),
        "--context",
        config.context,
        "--transport",
        "streamable-http",
        "--host",
        config.host,
        "--port",
        str(port),
        "--enable-web-dashboard",
        str(config.enable_web_dashboard).lower(),
        "--open-web-dashboard",
        str(config.open_web_dashboard).lower(),
    ]


def _wait_for_mcp_endpoint(
    host: str, port: int, endpoint_path: str, timeout_seconds: int
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _mcp_endpoint_is_ready(host, port, endpoint_path):
            return True
        time.sleep(0.2)
    return False


def start(args: argparse.Namespace, config: ServiceConfig) -> dict[str, Any]:
    project = Path(args.project).expanduser().resolve()
    if project != config.root:
        raise SerenaServiceError(
            "The active Serena project must be the project that owns the service configuration."
        )
    if not (project / ".serena" / "project.yml").is_file():
        raise SerenaServiceError(
            "Target project has no .serena/project.yml. Run the project setup workflow first."
        )
    executable = _require_serena()
    health_check = _run_health_check(executable, project)
    current = _read_state(config.state_path)
    if current and isinstance(current.get("port"), int) and _mcp_endpoint_is_ready(
        config.host, current["port"], config.endpoint_path
    ):
        if current.get("project_path") != str(project):
            raise SerenaServiceError(
                "A live shared Serena service is already recorded for a different project. "
                "Do not start a second project from this service configuration."
            )
        if args.replace:
            raise SerenaServiceError(
                "Refusing to replace a live shared Serena service. Stop its owning process "
                "first, then start the service to rotate the endpoint safely."
            )
        current["health_status"] = health_check
        current["health_checked_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _write_state(config.state_path, current)
        return {"started": False, "reused": True, "state": current}

    requested_port = args.port if args.port is not None else 0
    if requested_port < 0 or requested_port > 65535:
        raise SerenaServiceError("--port must be between 0 and 65535")
    attempts = 1 if requested_port else 5
    last_error = ""
    for _ in range(attempts):
        port = requested_port or _select_random_port(config.host)
        command = _server_command(executable, config, project, port)
        if args.dry_run:
            return {
                "started": False,
                "dry_run": True,
                "project_path": str(project),
                "port": port,
                "url": f"http://{config.host}:{port}{config.endpoint_path}",
                "command": command,
                "health_check": health_check,
            }
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        with config.log_path.open("a", encoding="utf-8", newline="\n") as log_stream:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                command,
                cwd=project,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        if _wait_for_mcp_endpoint(
            config.host, port, config.endpoint_path, config.startup_timeout_seconds
        ):
            state = {
                "version": 1,
                "project_path": str(project),
                "host": config.host,
                "port": port,
                "url": f"http://{config.host}:{port}{config.endpoint_path}",
                "transport": "streamable-http",
                "context": config.context,
                "pid": process.pid,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "command": command,
                "health_status": health_check,
                "health_checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "log_path": str(config.log_path),
            }
            _write_state(config.state_path, state)
            return {"started": True, "reused": False, "state": state}
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        last_error = (
            f"Serena did not listen on {config.host}:{port} within "
            f"{config.startup_timeout_seconds} seconds. See {config.log_path}."
        )
        if requested_port:
            break
    raise SerenaServiceError(last_error)


def status(config: ServiceConfig) -> dict[str, Any]:
    state = _read_state(config.state_path)
    if state is None:
        return {"configured": True, "running": False, "state_path": str(config.state_path)}
    port = state.get("port")
    running = isinstance(port, int) and _mcp_endpoint_is_ready(
        config.host, port, config.endpoint_path
    )
    return {
        "configured": True,
        "running": running,
        "state_path": str(config.state_path),
        "state": state,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the shared project-local Serena Streamable HTTP service."
    )
    parser.add_argument(
        "--service-config",
        default="agents/serena-service.toml",
        help="Project-local Serena service TOML.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start", help="Start or reuse the shared service.")
    start_parser.add_argument("--project", required=True, help="Active Serena project path.")
    start_parser.add_argument(
        "--port",
        type=int,
        help="Explicit loopback port. Omit or pass 0 to choose a random available port.",
    )
    start_parser.add_argument("--replace", action="store_true")
    start_parser.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("status", help="Inspect persisted endpoint and TCP reachability.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_service_config(args.service_config)
        result = start(args, config) if args.command == "start" else status(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, SerenaServiceError, ValueError) as exc:
        print(f"Serena service error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
