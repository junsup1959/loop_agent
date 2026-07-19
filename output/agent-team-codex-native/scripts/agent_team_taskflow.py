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
        materialize_activation_instructions,
        materialize_artifact_evidence,
        materialize_recommended_tools,
        materialize_skill_instructions,
        safe_path_component,
    )
    from .agent_team_dispatcher import CompositeWakeHook, UDPWakeHook
    from .agent_team_queue import ShellEchoHook, SQLiteMessageQueue
    from .agent_team_research import ResearchLedger
except ImportError:
    from agent_team_context import (
        ContextCompiler,
        RepositoryRegistry,
        materialize_activation_instructions,
        materialize_artifact_evidence,
        materialize_recommended_tools,
        materialize_skill_instructions,
        safe_path_component,
    )
    from agent_team_dispatcher import CompositeWakeHook, UDPWakeHook
    from agent_team_queue import ShellEchoHook, SQLiteMessageQueue
    from agent_team_research import ResearchLedger


AIRFLOW_IMPORT_ERROR: Exception | None = None
PROJECT_ROOT = Path(__file__).resolve().parents[1]

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
    "actor_seat_id",
    "db_path",
    "registry_path",
    "artifact_root",
}
RESEARCH_MESSAGE_TYPES = frozenset(
    {
        "RESEARCH_REQUESTED",
        "RESEARCH_SOURCE_READY",
        "RESEARCH_SHARD_ASSIGNED",
        "RESEARCH_SHARD_COMPLETED",
        "RESEARCH_MERGE_READY",
        "RESEARCH_CONFLICT",
        "RESEARCH_CONFLICT_OPENED",
        "RESEARCH_CONFLICT_RESOLUTION",
        "RESEARCH_RESOLUTION_PROPOSED",
        "RESEARCH_REVIEW_REQUEST",
        "RESEARCH_CONCLUSION_READY",
        "RESEARCH_READY_FOR_GATE",
        "RESEARCH_EVIDENCE_REQUEST",
        "NEED_MORE_CONTEXT",
        "BLOCKED",
    }
)
RESEARCH_REFERENCE_KEYS = frozenset(
    {
        "artifact_ref",
        "artifact_refs",
        "claim_id",
        "claim_ids",
        "conflict_id",
        "source_id",
        "source_ids",
        "shard_id",
        "shard_ids",
        "summary_id",
        "summary_ids",
    }
)
RESEARCH_CONTENT_KEYS = frozenset(
    {
        "body",
        "content",
        "conclusion",
        "description",
        "excerpt",
        "quote",
        "rationale",
        "raw",
        "source_text",
        "summary",
        "text",
    }
)
RESEARCH_PAYLOAD_KEYS = frozenset(
    {
        "goal_id",
        "iteration",
        "repo_id",
        "base_oid",
        "head_oid",
        "research_id",
        "research_phase",
        "artifact_ref",
        "artifact_refs",
        "claim_id",
        "claim_ids",
        "conflict_id",
        "source_id",
        "source_ids",
        "shard_id",
        "shard_ids",
        "summary_id",
        "summary_ids",
        "requested_action",
        "reason_code",
        "status",
    }
)
RESEARCH_ARTIFACT_REF_KEYS = frozenset(
    {
        "artifact_id",
        "uri",
        "kind",
        "relative_path",
        "sha256",
        "byte_count",
        "char_count",
        "content_type",
    }
)


def validate_runtime_conf(value: Mapping[str, Any]) -> dict[str, Any]:
    conf = dict(value)
    missing = sorted(key for key in REQUIRED_CONF if conf.get(key) in (None, ""))
    if missing:
        raise ValueError(f"DagRun conf is missing required keys: {', '.join(missing)}")
    if not isinstance(conf["iteration"], int) or conf["iteration"] < 1:
        raise ValueError("iteration must be a positive integer")
    safe_path_component(conf["work_item_id"], field="work_item_id")
    for field in ("context_action", "actor_seat_id"):
        if field in conf and (not isinstance(conf[field], str) or not conf[field]):
            raise ValueError(f"{field} must be a non-empty string when supplied")
    for field in ("context_paths", "selected_skill_ids", "artifact_paths"):
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
    raw_artifact_root = Path(conf["artifact_root"]).expanduser()
    artifact_root = (
        raw_artifact_root.resolve()
        if raw_artifact_root.is_absolute()
        else (PROJECT_ROOT / raw_artifact_root).resolve()
    )
    try:
        artifact_root.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError("artifact_root must remain inside the project root") from exc
    conf["artifact_root"] = str(artifact_root)
    return conf


def validate_research_runtime_conf(value: Mapping[str, Any]) -> dict[str, Any]:
    conf = validate_runtime_conf(value)
    for field in ("research_id", "research_phase"):
        current = conf.get(field)
        if not isinstance(current, str) or not current:
            raise ValueError(f"Research DagRun conf requires non-empty {field}")
    safe_path_component(conf["research_id"], field="research_id")
    if conf.get("artifact_paths"):
        raise ValueError(
            "Research DagRun conf must not provide raw artifact_paths. "
            "Resolve the research selection through the local ledger."
        )
    for field in ("research_claim_ids", "research_source_ids"):
        if field not in conf:
            conf[field] = []
            continue
        entries = conf[field]
        if (
            not isinstance(entries, list)
            or not all(isinstance(entry, str) and entry for entry in entries)
            or len(entries) != len(set(entries))
        ):
            raise ValueError(f"{field} must be a unique array of non-empty strings")
    for field in ("research_include_conflicts", "research_include_raw_shards"):
        if field in conf and not isinstance(conf[field], bool):
            raise ValueError(f"{field} must be a boolean when supplied")
        conf.setdefault(field, False)
    research_db_path = conf.get("research_db_path", conf["db_path"])
    if not isinstance(research_db_path, str) or not research_db_path:
        raise ValueError("research_db_path must be a non-empty string when supplied")
    conf["research_db_path"] = str(Path(research_db_path).expanduser().resolve())
    return conf


def _validate_research_message(
    *,
    message_type: Any,
    payload: Mapping[str, Any],
    research_id: str,
) -> None:
    if message_type not in RESEARCH_MESSAGE_TYPES:
        raise ValueError(f"Research iteration cannot emit message type: {message_type!r}")
    if payload.get("research_id") != research_id:
        raise ValueError("Research message must target the active research_id")
    unknown_keys = set(payload) - RESEARCH_PAYLOAD_KEYS
    if unknown_keys:
        raise ValueError(
            "Research message payload has unsupported keys: "
            f"{sorted(unknown_keys)}"
        )
    if not any(key in payload for key in RESEARCH_REFERENCE_KEYS):
        raise ValueError(
            "Research message must include an artifact, claim, conflict, source, shard, or summary reference."
        )
    compact_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(compact_payload) > 4_000:
        raise ValueError("Research message payload exceeds the compact reference-only limit")

    def inspect(value: Any, *, artifact_reference: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if isinstance(key, str) and key.lower() in RESEARCH_CONTENT_KEYS:
                    raise ValueError(
                        f"Research message payload may not carry material content field: {key}"
                    )
                if artifact_reference and key not in RESEARCH_ARTIFACT_REF_KEYS:
                    raise ValueError(
                        f"Research artifact reference has unsupported key: {key}"
                    )
                inspect(child, artifact_reference=artifact_reference)
        elif isinstance(value, list):
            for child in value:
                inspect(child, artifact_reference=artifact_reference)
        elif isinstance(value, str) and len(value) > 512:
            raise ValueError("Research message strings must remain compact references or codes")

    for key, value in payload.items():
        inspect(value, artifact_reference=key in {"artifact_ref", "artifact_refs"})


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


def _artifact_child(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Artifact path escapes artifact_root") from exc
    return candidate


def compile_context_to_artifact(
    conf: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    validated = (
        validate_research_runtime_conf(conf)
        if "research_id" in conf
        else validate_runtime_conf(conf)
    )
    compiler = ContextCompiler(
        queue=SQLiteMessageQueue(validated["db_path"]),
        registry=RepositoryRegistry(validated["registry_path"]),
    )
    approved_artifact_paths: list[str] | None = None
    research_selection: dict[str, Any] | None = None
    if "research_id" in validated:
        budget = compiler.profile_catalog.resolve(
            target_role=validated["actor_role"],
            requested_profile=validated["context_profile"],
        )
        ledger = ResearchLedger(
            validated["research_db_path"],
            validated["artifact_root"],
        )
        research_selection = ledger.select_context(
            run_id=validated["research_id"],
            role=validated["actor_role"],
            max_content_chars=budget.max_artifact_chars,
            max_artifacts=budget.max_artifacts,
            claim_ids=validated["research_claim_ids"],
            source_ids=validated["research_source_ids"],
            include_conflicts=validated["research_include_conflicts"],
            include_raw_shards=validated["research_include_raw_shards"],
        )
        approved_artifact_paths = list(
            research_selection["context_compiler_artifact_paths"]
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
        artifact_root=validated["artifact_root"],
        artifact_paths=(
            approved_artifact_paths
            if approved_artifact_paths is not None
            else validated.get("artifact_paths")
        ),
        approved_artifact_paths=approved_artifact_paths,
    )

    artifact_root = Path(validated["artifact_root"])
    context_directory = _artifact_child(
        artifact_root,
        "contexts",
        validated["work_item_id"],
    )
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
    result = {
        "context_path": str(context_path),
        "thread_id": validated["thread_id"],
        "work_item_id": validated["work_item_id"],
        "base_oid": context["git_context"]["base_oid"],
        "head_oid": context["git_context"]["head_oid"],
        "diff_truncated": context["git_context"]["diff_truncated"],
        "selected_paths": context["git_context"]["selected_paths"],
        "context_budget": context["context_budget"],
        "skill_packet": context["skill_packet"],
        "artifact_packet": context["artifact_packet"],
        "activation_instruction_packet": context["activation_instruction_packet"],
        "recommended_tool_packet": context["recommended_tool_packet"],
        "agent_binding": context["agent_binding"],
        "context_chars": context["context_chars"],
        "injected_context_chars": context["injected_context_chars"],
        "context_sha256": hashlib.sha256(context_text.encode("utf-8")).hexdigest(),
    }
    if research_selection is not None:
        result["research_context_selection"] = research_selection
    return result


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
    selected_artifacts = materialize_artifact_evidence(context_packet["artifact_packet"])
    activation_instructions = materialize_activation_instructions(
        context_packet["activation_instruction_packet"]
    )
    recommended_tools = materialize_recommended_tools(
        context_packet["recommended_tool_packet"]
    )
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
            "selected_artifact_paths": [
                artifact["path"] for artifact in context_packet["artifact_refs"]
            ],
            "budget": context_packet["context_budget"],
            "omitted_context": context_packet["omitted_context"],
        },
        "selected_skills": selected_skills,
        "selected_artifacts": selected_artifacts,
        "activation_instructions": activation_instructions,
        "recommended_tools": recommended_tools,
    }
    if "research_id" in conf:
        request["research"] = {
            "id": conf["research_id"],
            "phase": conf.get("research_phase"),
            "artifact_paths": [
                artifact["path"] for artifact in context_packet["artifact_refs"]
            ],
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
        if "research_id" in conf:
            payload.setdefault("research_id", conf["research_id"])
            payload.setdefault("research_phase", conf.get("research_phase"))
            _validate_research_message(
                message_type=outgoing["type"],
                payload=payload,
                research_id=conf["research_id"],
            )
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

    result_artifact = _artifact_child(
        Path(conf["artifact_root"]),
        "results",
        safe_path_component(conf["work_item_id"], field="work_item_id"),
    )
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

    @dag(
        dag_id="agent_team_research_iteration",
        schedule=None,
        catchup=False,
        tags=["agent-team", "research-loop"],
    )
    def research_iteration_flow():
        @task(task_id="load_research_runtime_conf")
        def load_research_runtime_conf_task() -> dict[str, Any]:
            context = get_current_context()
            dag_run = context.get("dag_run")
            if dag_run is None:
                raise RuntimeError("research iteration requires a DagRun")
            return validate_research_runtime_conf(dag_run.conf or {})

        @task(task_id="compile_research_context")
        def compile_research_context_task(conf: dict[str, Any]) -> dict[str, Any]:
            context = get_current_context()
            return compile_context_to_artifact(conf, run_id=context["run_id"])

        @task(task_id="execute_research_agent", retries=2)
        def execute_research_agent_task(
            conf: dict[str, Any],
            context_artifact: dict[str, Any],
        ) -> dict[str, Any]:
            return execute_agent_runner(conf=conf, context_artifact=context_artifact)

        @task(task_id="persist_research_result_and_messages")
        def persist_research_result_and_messages_task(
            conf: dict[str, Any],
            result: dict[str, Any],
        ) -> dict[str, Any]:
            return persist_agent_result(conf=conf, result=result)

        runtime_conf = load_research_runtime_conf_task()
        context_artifact = compile_research_context_task(runtime_conf)
        agent_result = execute_research_agent_task(runtime_conf, context_artifact)
        persist_research_result_and_messages_task(runtime_conf, agent_result)

    research_iteration_dag = research_iteration_flow()
else:
    module_iteration_dag = None
    research_iteration_dag = None


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
    print(
        "Airflow TaskFlow DAGs loaded: agent_team_module_iteration, "
        "agent_team_research_iteration"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
