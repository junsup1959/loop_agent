from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    from .agent_team_context import (
        ContextCompiler,
        RepositoryRegistry,
        materialize_skill_instructions,
    )
    from .agent_team_dispatcher import CompositeWakeHook, UDPWakeHook
    from .agent_team_queue import ShellEchoHook, SQLiteMessageQueue
except ImportError:
    from agent_team_context import (
        ContextCompiler,
        RepositoryRegistry,
        materialize_skill_instructions,
    )
    from agent_team_dispatcher import CompositeWakeHook, UDPWakeHook
    from agent_team_queue import ShellEchoHook, SQLiteMessageQueue


AIRFLOW_IMPORT_ERROR: Exception | None = None

try:
    from airflow.sdk import dag, get_current_context, task
except Exception as airflow_sdk_error:
    AIRFLOW_IMPORT_ERROR = airflow_sdk_error
    dag = None
    task = None
    get_current_context = None


REQUIRED_CONF = {
    "goal_id",
    "work_item_id",
    "thread_id",
    "iteration",
    "actor_role",
    "repo_id",
    "base_oid",
    "head_oid",
    "context_profile",
    "db_path",
    "registry_path",
    "artifact_root",
}


def validate_runtime_conf(value: Mapping[str, Any]) -> dict[str, Any]:
    conf = dict(value)
    missing = sorted(key for key in REQUIRED_CONF if conf.get(key) in (None, ""))
    if missing:
        raise ValueError(f"DagRun conf is missing required keys: {', '.join(missing)}")
    if not isinstance(conf["iteration"], int) or conf["iteration"] < 1:
        raise ValueError("iteration must be a positive integer")
    for field in ("context_action", "actor_seat_id"):
        if field in conf and (not isinstance(conf[field], str) or not conf[field]):
            raise ValueError(f"{field} must be a non-empty string when supplied")
    for field in ("context_paths", "selected_skill_ids"):
        if field not in conf:
            continue
        entries = conf[field]
        if (
            not isinstance(entries, list)
            or not all(isinstance(entry, str) and entry for entry in entries)
            or len(entries) != len(set(entries))
        ):
            raise ValueError(f"{field} must be a unique array of non-empty strings")
    for field in ("max_messages", "max_diff_chars"):
        if field in conf and (
            not isinstance(conf[field], int) or conf[field] < 0
        ):
            raise ValueError(f"{field} must be a non-negative integer when supplied")
    conf["db_path"] = str(Path(conf["db_path"]).expanduser().resolve())
    conf["registry_path"] = str(Path(conf["registry_path"]).expanduser().resolve())
    conf["artifact_root"] = str(Path(conf["artifact_root"]).expanduser().resolve())
    return conf


def queue_with_wake_hook(db_path: str) -> SQLiteMessageQueue:
    host = os.environ.get("AGENT_TEAM_WAKE_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_TEAM_WAKE_PORT", "8765"))
    echo_script = Path(
        os.environ.get(
            "AGENT_TEAM_MESSAGE_ECHO_SCRIPT",
            str(Path(__file__).with_name("message_echo_hook.sh")),
        )
    )
    hooks = [UDPWakeHook(host=host, port=port)]
    if os.environ.get("AGENT_TEAM_MESSAGE_ECHO", "1") != "0":
        hooks.append(
            ShellEchoHook(
                echo_script,
                shell_executable=os.environ.get("AGENT_TEAM_SH", "sh"),
                log_path=os.environ.get("AGENT_TEAM_MESSAGE_LOG"),
            )
        )
    return SQLiteMessageQueue(db_path, wake_hook=CompositeWakeHook(*hooks))


def compile_context_to_artifact(
    conf: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    validated = validate_runtime_conf(conf)
    compiler = ContextCompiler(
        queue=SQLiteMessageQueue(validated["db_path"]),
        registry=RepositoryRegistry(validated["registry_path"]),
    )
    context = compiler.compile(
        thread_id=validated["thread_id"],
        work_item_id=validated["work_item_id"],
        target_role=validated["actor_role"],
        repo_id=validated["repo_id"],
        base_oid=validated["base_oid"],
        head_oid=validated["head_oid"],
        context_profile=validated["context_profile"],
        context_action=validated.get("context_action"),
        actor_seat_id=validated.get("actor_seat_id"),
        selected_skill_ids=validated.get("selected_skill_ids"),
        max_messages=validated.get("max_messages"),
        max_diff_chars=validated.get("max_diff_chars"),
        paths=validated.get("context_paths"),
    )

    artifact_root = Path(validated["artifact_root"])
    context_directory = artifact_root / "contexts" / validated["work_item_id"]
    context_directory.mkdir(parents=True, exist_ok=True)
    safe_run_id = "".join(character if character.isalnum() else "_" for character in run_id)
    context_path = context_directory / (
        f"iteration-{validated['iteration']}-{safe_run_id}.json"
    )
    context_text = json.dumps(context, ensure_ascii=False, indent=2)
    context_path.write_text(
        context_text,
        encoding="utf-8",
    )
    return {
        "context_path": str(context_path),
        "thread_id": validated["thread_id"],
        "work_item_id": validated["work_item_id"],
        "base_oid": context["git_context"]["base_oid"],
        "head_oid": context["git_context"]["head_oid"],
        "diff_truncated": context["git_context"]["diff_truncated"],
        "selected_paths": context["git_context"]["selected_paths"],
        "context_budget": context["context_budget"],
        "skill_packet": context["skill_packet"],
        "agent_binding": context["agent_binding"],
        "context_chars": context["context_chars"],
        "injected_context_chars": context["injected_context_chars"],
        "context_sha256": hashlib.sha256(context_text.encode("utf-8")).hexdigest(),
    }


def runner_command_from_environment() -> list[str]:
    raw = os.environ.get("AGENT_TEAM_RUNNER_COMMAND_JSON")
    if not raw:
        raise RuntimeError(
            "AGENT_TEAM_RUNNER_COMMAND_JSON must contain a JSON array command, "
            'for example ["python", "local_agent_runner.py"]'
        )
    parsed = json.loads(raw)
    if (
        not isinstance(parsed, list)
        or not parsed
        or not all(isinstance(item, str) and item for item in parsed)
    ):
        raise ValueError("AGENT_TEAM_RUNNER_COMMAND_JSON must be a non-empty string array")
    return parsed


def execute_agent_runner(
    *,
    conf: Mapping[str, Any],
    context_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    command = runner_command_from_environment()
    context_path = Path(str(context_artifact["context_path"]))
    context_text = context_path.read_text(encoding="utf-8")
    expected_hash = context_artifact.get("context_sha256")
    actual_hash = hashlib.sha256(context_text.encode("utf-8")).hexdigest()
    if expected_hash is not None and expected_hash != actual_hash:
        raise RuntimeError("Context artifact hash does not match the compiled artifact.")
    try:
        context_packet = json.loads(context_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Context artifact must contain one JSON object.") from exc
    if not isinstance(context_packet, dict):
        raise RuntimeError("Context artifact must contain a JSON object.")
    selected_skills = materialize_skill_instructions(context_packet["skill_packet"])
    request = {
        "goal_id": conf["goal_id"],
        "work_item_id": conf["work_item_id"],
        "thread_id": conf["thread_id"],
        "iteration": conf["iteration"],
        "actor_role": conf["actor_role"],
        "context_profile": conf["context_profile"],
        "context_action": context_packet["context_action"],
        "context_path": context_artifact["context_path"],
        "context_sha256": actual_hash,
        "context": {
            "path": context_artifact["context_path"],
            "sha256": actual_hash,
            "chars": context_packet["context_chars"],
            "injected_chars": context_packet["injected_context_chars"],
            "selected_paths": context_packet["git_context"]["selected_paths"],
            "budget": context_packet["context_budget"],
            "omitted_context": context_packet["omitted_context"],
        },
        "selected_skills": selected_skills,
    }
    binding = context_packet.get("agent_binding")
    if isinstance(binding, dict):
        request["actor_seat_id"] = binding["seat_id"]
        request["agent_file"] = binding["agent_file"]
    timeout_seconds = int(conf.get("runner_timeout_seconds", 1_800))
    result = subprocess.run(
        command,
        input=json.dumps(request, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"agent runner exited with {result.returncode}: {result.stderr.strip()}"
        )
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("agent runner stdout must be one JSON object") from exc
    if not isinstance(output, dict):
        raise RuntimeError("agent runner output must be a JSON object")
    if "status" not in output:
        raise RuntimeError("agent runner output must contain status")
    outgoing = output.get("outgoing_messages", [])
    if not isinstance(outgoing, list):
        raise RuntimeError("outgoing_messages must be a JSON array")
    return output


def persist_agent_result(
    *,
    conf: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    queue = queue_with_wake_hook(str(conf["db_path"]))
    message_ids: list[str] = []
    for index, outgoing in enumerate(result.get("outgoing_messages", [])):
        if not isinstance(outgoing, dict):
            raise ValueError(f"outgoing_messages[{index}] must be an object")
        required = {"to_role", "type", "payload"}
        missing = sorted(required - outgoing.keys())
        if missing:
            raise ValueError(
                f"outgoing_messages[{index}] is missing: {', '.join(missing)}"
            )
        payload = dict(outgoing["payload"])
        payload.setdefault("goal_id", conf["goal_id"])
        payload.setdefault("iteration", conf["iteration"])
        payload.setdefault("repo_id", conf["repo_id"])
        payload.setdefault("base_oid", conf["base_oid"])
        if result.get("head_oid"):
            payload.setdefault("head_oid", result["head_oid"])
        dedupe_key = outgoing.get(
            "dedupe_key",
            (
                f"{conf['work_item_id']}:{conf['iteration']}:{conf['actor_role']}:"
                f"{outgoing['to_role']}:{outgoing['type']}:{index}"
            ),
        )
        message = queue.enqueue(
            thread_id=conf["thread_id"],
            work_item_id=conf["work_item_id"],
            parent_message_id=outgoing.get("parent_message_id"),
            from_role=conf["actor_role"],
            to_role=outgoing["to_role"],
            message_type=outgoing["type"],
            payload=payload,
            priority=int(outgoing.get("priority", 0)),
            max_attempts=int(outgoing.get("max_attempts", 5)),
            dedupe_key=dedupe_key,
        )
        message_ids.append(message.id)

    result_artifact = Path(conf["artifact_root"]) / "results" / conf["work_item_id"]
    result_artifact.mkdir(parents=True, exist_ok=True)
    result_path = result_artifact / (
        f"iteration-{conf['iteration']}-{uuid.uuid4().hex}.json"
    )
    result_path.write_text(
        json.dumps(dict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "status": result["status"],
        "head_oid": result.get("head_oid"),
        "message_ids": message_ids,
        "result_artifact": str(result_path),
    }


if AIRFLOW_IMPORT_ERROR is None:

    @dag(
        dag_id="agent_team_module_iteration",
        schedule=None,
        catchup=False,
        tags=["agent-team", "module-loop"],
    )
    def module_iteration_flow():
        @task(task_id="load_runtime_conf")
        def load_runtime_conf_task() -> dict[str, Any]:
            context = get_current_context()
            dag_run = context.get("dag_run")
            if dag_run is None:
                raise RuntimeError("module iteration requires a DagRun")
            return validate_runtime_conf(dag_run.conf or {})

        @task(task_id="compile_role_context")
        def compile_role_context_task(conf: dict[str, Any]) -> dict[str, Any]:
            context = get_current_context()
            return compile_context_to_artifact(conf, run_id=context["run_id"])

        @task(task_id="execute_role_agent", retries=2)
        def execute_role_agent_task(
            conf: dict[str, Any],
            context_artifact: dict[str, Any],
        ) -> dict[str, Any]:
            return execute_agent_runner(
                conf=conf,
                context_artifact=context_artifact,
            )

        @task(task_id="persist_result_and_messages")
        def persist_result_and_messages_task(
            conf: dict[str, Any],
            result: dict[str, Any],
        ) -> dict[str, Any]:
            return persist_agent_result(conf=conf, result=result)

        runtime_conf = load_runtime_conf_task()
        context_artifact = compile_role_context_task(runtime_conf)
        agent_result = execute_role_agent_task(runtime_conf, context_artifact)
        persist_result_and_messages_task(runtime_conf, agent_result)

    module_iteration_dag = module_iteration_flow()
else:
    module_iteration_dag = None


def main() -> int:
    if AIRFLOW_IMPORT_ERROR is not None:
        print(
            "Airflow TaskFlow is unavailable in this Python environment: "
            f"{AIRFLOW_IMPORT_ERROR}. The SQLite queue and context compiler "
            "remain runnable. Use a supported POSIX Airflow runtime to load "
            "agent_team_module_iteration.",
            file=sys.stderr,
        )
        return 2
    print("Airflow TaskFlow DAG loaded: agent_team_module_iteration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
