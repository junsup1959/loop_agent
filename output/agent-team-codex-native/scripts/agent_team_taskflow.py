"""Airflow TaskFlow integration for confined Agent-Team worktree activations.

This is the execution detail of the existing Agent-Team architecture.  It does
not introduce a second scheduler, role model, or approval path: Airflow carries
immutable identifiers between the existing workspace, profile, activation,
context, runner, gate, integration, promotion, and recovery services.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast

try:
    from .agent_team_contracts import (
        ActivationContract,
        ActivationResult,
        AdmissionReceipt,
        ContractViolation,
        admit as admit_canonical_contract,
        begin_attempt as begin_canonical_attempt,
        commit_result as commit_canonical_result,
        compile_transition as compile_canonical_transition,
        render as render_canonical_contract,
    )
    from .agent_team_context import (
        ContextCompiler,
        RepositoryRegistry,
        materialize_skill_instructions,
    )
    from .agent_team_dispatcher import CompositeWakeHook, UDPWakeHook
    from .agent_team_domain import (
        ActivationSpec,
        ContractAttemptKind,
        SourceIntegrity,
    )
    from .agent_team_profiles import (
        PROFESSIONAL_SKILL_ID,
        ProfessionalProfileCompiler,
        ProfileResolutionRequest,
    )
    from .agent_team_queue import ShellEchoHook, SQLiteMessageQueue
    from .agent_team_runtime import (
        AgentRuntime,
        ExecutionKind,
        OpaqueArtifactRef,
        OutputPolicy,
        RunnerRequest,
        RunnerResult,
        RuntimeSandboxBinding,
        ToolPolicy,
        canonical_json,
        environment_fingerprint,
        minimal_environment,
        sha256_json,
    )
    from .agent_team_state import AxStateStore
    from .agent_team_workflow import (
        ActorBinding,
        EvidenceSet,
        RepositoryBinding,
        TransitionDefinition,
        WorkflowDefinitions,
        WorkflowInstance,
    )
    from .project_agents import load_and_validate, load_mcp_policy
    from .project_skills import resolve_selection
except ImportError:
    from agent_team_contracts import (
        ActivationContract,
        ActivationResult,
        AdmissionReceipt,
        ContractViolation,
        admit as admit_canonical_contract,
        begin_attempt as begin_canonical_attempt,
        commit_result as commit_canonical_result,
        compile_transition as compile_canonical_transition,
        render as render_canonical_contract,
    )
    from agent_team_context import (
        ContextCompiler,
        RepositoryRegistry,
        materialize_skill_instructions,
    )
    from agent_team_dispatcher import CompositeWakeHook, UDPWakeHook
    from agent_team_domain import (
        ActivationSpec,
        ContractAttemptKind,
        SourceIntegrity,
    )
    from agent_team_profiles import (
        PROFESSIONAL_SKILL_ID,
        ProfessionalProfileCompiler,
        ProfileResolutionRequest,
    )
    from agent_team_queue import ShellEchoHook, SQLiteMessageQueue
    from agent_team_runtime import (
        AgentRuntime,
        ExecutionKind,
        OpaqueArtifactRef,
        OutputPolicy,
        RunnerRequest,
        RunnerResult,
        RuntimeSandboxBinding,
        ToolPolicy,
        canonical_json,
        environment_fingerprint,
        minimal_environment,
        sha256_json,
    )
    from agent_team_state import AxStateStore
    from agent_team_workflow import (
        ActorBinding,
        EvidenceSet,
        RepositoryBinding,
        TransitionDefinition,
        WorkflowDefinitions,
        WorkflowInstance,
    )
    from project_agents import load_and_validate, load_mcp_policy
    from project_skills import resolve_selection


AIRFLOW_IMPORT_ERROR: Exception | None = None
try:
    from airflow.sdk import dag, get_current_context, task
except Exception as airflow_sdk_error:  # pragma: no cover - normal outside Airflow
    AIRFLOW_IMPORT_ERROR = airflow_sdk_error
    dag = None
    task = None
    get_current_context = None


TASKFLOW_STAGE_ORDER = (
    "immutable_conf",
    "allocate_bind_activation",
    "resolve_compile_profile",
    "compile_admit_contract",
    "bounded_context",
    "confined_execute",
    "integrity_verify",
    "deterministic_persistence",
    "revoke_release_terminate",
)

_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CONF_DIGEST_FIELD = "_immutable_conf_digest"
_REQUIRED_CONF = frozenset(
    {
        "target_id",
        "goal_id",
        "work_item_id",
        "thread_id",
        "revision",
        "run_id",
        "activation_id",
        "idempotency_key",
        "execution_kind",
        "actor_role",
        "actor_seat_id",
        "gate_or_task",
        "repo_id",
        "base_oid",
        "head_oid",
        "subject_oid",
        "context_profile",
        "db_path",
        "registry_path",
        "artifact_root",
        "target_paths",
        "source_write_scope",
        "generated_write_scope",
        "repository_manifests",
        "build_evidence",
        "command",
        "command_prefixes",
        "tool_policy_id",
        "allowed_tools",
    }
)
_OPTIONAL_CONF = frozenset(
    {
        "context_action",
        "context_paths",
        "selected_skill_ids",
        "max_messages",
        "max_diff_chars",
        "lease_seconds",
        "generated_paths",
        "environment",
        "runner_timeout_seconds",
        "stdout_limit_bytes",
        "stderr_limit_bytes",
        "redaction_literals",
        "network_allowed",
        "shell_allowed",
        "validated_memory_refs",
        "workflow_instance_id",
        "transition_id",
        "worker_assignment_id",
        "repository_registration_id",
        "runtime_lease_id",
        "sandbox_binding_id",
        "oid_authority_id",
    }
)
_SERVICE_FORBIDDEN_FIELDS = frozenset(
    {
        "actor_role",
        "actor_seat_id",
        "compiled_profile_digest",
        "compiled_profile_ref",
        "context_ref",
        "model",
        "model_reasoning_effort",
        "professional_skill_id",
        "seat_id",
    }
)
_SERVICE_IDENTITIES = frozenset(
    {
        "integration-controller",
        "promotion-controller",
        "recovery-controller",
    }
)


class TaskFlowContractError(ValueError):
    """DagRun input or an inter-stage receipt violated the immutable contract."""


class TaskFlowIntegrityError(RuntimeError):
    """A review sandbox changed in a way that cannot produce gate evidence."""


@dataclass(frozen=True)
class CanonicalTaskFlowContractInputs:
    """Authoritative v4 objects needed to compile one activation contract."""

    instance: WorkflowInstance
    transition: TransitionDefinition
    actor: ActorBinding
    evidence: EvidenceSet

    def __post_init__(self) -> None:
        expected = (
            ("instance", self.instance, WorkflowInstance),
            ("transition", self.transition, TransitionDefinition),
            ("actor", self.actor, ActorBinding),
            ("evidence", self.evidence, EvidenceSet),
        )
        for field_name, value, value_type in expected:
            if not isinstance(value, value_type):
                raise TaskFlowContractError(
                    f"canonical contract provider returned invalid {field_name}"
                )


@dataclass(frozen=True)
class CanonicalTaskFlowAttemptInputs:
    """Authoritative backend identity and request binding for begin_attempt."""

    backend: str
    model: str
    input_digest: str
    attempt_kind: ContractAttemptKind | str = ContractAttemptKind.PRIMARY

    def __post_init__(self) -> None:
        if not isinstance(self.backend, str) or not _IDENTIFIER_RE.fullmatch(
            self.backend
        ):
            raise TaskFlowContractError(
                "canonical attempt backend must be a stable identifier"
            )
        if not isinstance(self.model, str) or not _IDENTIFIER_RE.fullmatch(
            self.model
        ):
            raise TaskFlowContractError(
                "canonical attempt model must be a stable identifier"
            )
        if not isinstance(self.input_digest, str) or not _SHA256_RE.fullmatch(
            self.input_digest
        ):
            raise TaskFlowContractError(
                "canonical attempt input_digest must be a lowercase sha256 digest"
            )


class CanonicalTaskFlowInputProvider(Protocol):
    """Deployment boundary for authoritative v4 compile/attempt/result facts.

    Implementations must read these facts from the canonical control plane and
    trusted runtime broker.  The TaskFlow adapter deliberately does not derive
    missing identity, authority, MCP, attempt, or result facts from DagRun input.
    """

    def contract_inputs(
        self,
        *,
        conf: Mapping[str, Any],
        allocation: Mapping[str, Any],
        profile: Mapping[str, Any],
    ) -> CanonicalTaskFlowContractInputs:
        ...

    def attempt_inputs(
        self,
        *,
        conf: Mapping[str, Any],
        allocation: Mapping[str, Any],
        profile: Mapping[str, Any],
        context_artifact: Mapping[str, Any],
        request: RunnerRequest,
        contract: ActivationContract,
        admission: AdmissionReceipt,
    ) -> CanonicalTaskFlowAttemptInputs:
        ...

    def runtime_binding(
        self,
        *,
        contract: ActivationContract,
        admission: AdmissionReceipt,
        attempt_id: str,
        request: RunnerRequest,
    ) -> RuntimeSandboxBinding:
        ...

    def activation_result(
        self,
        *,
        contract: ActivationContract,
        admission: AdmissionReceipt,
        attempt_id: str,
        runner_result: Mapping[str, Any],
        integrity: Mapping[str, Any],
    ) -> ActivationResult:
        ...


@dataclass(frozen=True)
class AxStateStoreCanonicalTaskFlowInputProvider:
    """Resolve every activation-contract fact from the canonical v4 store."""

    state_store: AxStateStore
    backend_id: str
    definitions: WorkflowDefinitions = field(default_factory=WorkflowDefinitions.load)

    def __post_init__(self) -> None:
        if not isinstance(self.state_store, AxStateStore):
            raise TypeError("state_store must be an AxStateStore")
        _stable_identifier(self.backend_id, "backend_id")
        if not isinstance(self.definitions, WorkflowDefinitions):
            raise TypeError("definitions must be WorkflowDefinitions")
        self.state_store.initialize()

    @staticmethod
    def _one(rows: Sequence[Any], label: str) -> Any:
        if len(rows) != 1:
            raise TaskFlowContractError(
                f"canonical state requires exactly one {label}; found {len(rows)}"
            )
        return rows[0]

    @staticmethod
    def _json_paths(value: Any, label: str) -> tuple[str, ...]:
        try:
            decoded = json.loads(str(value))
        except (TypeError, json.JSONDecodeError) as exc:
            raise TaskFlowContractError(
                f"canonical {label} is not valid JSON"
            ) from exc
        if (
            not isinstance(decoded, list)
            or not all(isinstance(item, str) and item for item in decoded)
            or len(decoded) != len(set(decoded))
        ):
            raise TaskFlowContractError(
                f"canonical {label} must be a unique path array"
            )
        return tuple(decoded)

    def _mcp_health(
        self,
        connection: Any,
        transition: TransitionDefinition,
    ) -> dict[str, Any]:
        policy = self.definitions.mcp_policy
        raw_bindings = policy.get("required_use_bindings")
        if not isinstance(raw_bindings, list):
            raise TaskFlowContractError("canonical MCP policy has no bindings")
        binding_index = {
            item.get("id"): item
            for item in raw_bindings
            if isinstance(item, Mapping)
        }
        required: dict[str, set[str]] = {}
        for binding_id in (
            *transition.mcp_availability_binding_ids,
            *transition.mcp_required_use_binding_ids,
        ):
            binding = binding_index.get(binding_id)
            if not isinstance(binding, Mapping):
                raise TaskFlowContractError(
                    f"canonical MCP binding is missing: {binding_id}"
                )
            tools = tuple(str(item) for item in binding.get("tool_ids", ()))
            for server_name in binding.get("server_ids", ()):
                required.setdefault(str(server_name), set()).update(
                    tools or ("server-health",)
                )
        health: dict[str, Any] = {}
        for server_name, tool_names in sorted(required.items()):
            observations: list[dict[str, str]] = []
            exposed_tools: list[str] = []
            for tool_name in sorted(tool_names):
                rows = connection.execute(
                    """
                    SELECT md.id AS definition_id, md.tool_name,
                           mh.id AS observation_id, mh.status,
                           mh.evidence_digest, mh.observed_at
                    FROM mcp_definitions AS md
                    JOIN mcp_health_observations AS mh
                      ON mh.mcp_definition_id = md.id
                    WHERE md.server_name = ?
                      AND md.tool_name = ?
                      AND md.state = 'ACTIVE'
                      AND mh.contract_id IS NULL
                    ORDER BY mh.observed_at DESC, mh.id DESC
                    """,
                    (server_name, tool_name),
                ).fetchall()
                if not rows:
                    raise TaskFlowContractError(
                        f"canonical MCP health is missing: {server_name}/{tool_name}"
                    )
                latest = rows[0]
                if latest["status"] != "HEALTHY":
                    raise TaskFlowContractError(
                        f"canonical MCP health is not healthy: {server_name}/{tool_name}"
                    )
                observations.append(
                    {
                        "definition_id": latest["definition_id"],
                        "observation_id": latest["observation_id"],
                        "evidence_digest": latest["evidence_digest"],
                    }
                )
                if tool_name != "server-health":
                    exposed_tools.append(tool_name)
            health[server_name] = {
                "status": "HEALTHY",
                "tools": exposed_tools,
                "evidence_digest": sha256_json(observations),
            }
        return health

    def contract_inputs(
        self,
        *,
        conf: Mapping[str, Any],
        allocation: Mapping[str, Any],
        profile: Mapping[str, Any],
    ) -> CanonicalTaskFlowContractInputs:
        if Path(str(conf["db_path"])).resolve() != self.state_store.db_path:
            raise TaskFlowContractError(
                "DagRun db_path differs from the canonical AxStateStore"
            )
        transition_key = str(conf.get("transition_id", conf["gate_or_task"]))
        canonical_transition = self.definitions.transition(transition_key)
        with self.state_store.transaction() as connection:
            if conf.get("workflow_instance_id"):
                workflow_rows = connection.execute(
                    """
                    SELECT wi.*, wd.version AS workflow_version,
                           wd.definition_id, rd.sha256 AS workflow_sha256,
                           g.target_id
                    FROM workflow_instances AS wi
                    JOIN workflow_definitions AS wd
                      ON wd.id = wi.workflow_definition_id
                    JOIN registered_definitions AS rd
                      ON rd.id = wd.definition_id
                    JOIN goals AS g ON g.id = wi.goal_id
                    WHERE wi.id = ?
                    """,
                    (conf["workflow_instance_id"],),
                ).fetchall()
            else:
                workflow_rows = connection.execute(
                    """
                    SELECT wi.*, wd.version AS workflow_version,
                           wd.definition_id, rd.sha256 AS workflow_sha256,
                           g.target_id
                    FROM workflow_instances AS wi
                    JOIN workflow_definitions AS wd
                      ON wd.id = wi.workflow_definition_id
                    JOIN registered_definitions AS rd
                      ON rd.id = wd.definition_id
                    JOIN goals AS g ON g.id = wi.goal_id
                    WHERE wi.run_id = ? AND wi.goal_id = ?
                    """,
                    (conf["run_id"], conf["goal_id"]),
                ).fetchall()
            workflow = self._one(workflow_rows, "active workflow instance")
            transition_rows = connection.execute(
                """
                SELECT wt.*, lc.capability_key, rd.sha256 AS output_schema_sha256
                FROM workflow_transitions AS wt
                JOIN logical_capabilities AS lc ON lc.id = wt.capability_id
                JOIN registered_definitions AS rd
                  ON rd.id = wt.output_schema_definition_id
                WHERE wt.workflow_definition_id = ?
                  AND wt.transition_key = ?
                  AND wt.state = 'ACTIVE'
                """,
                (workflow["workflow_definition_id"], transition_key),
            ).fetchall()
            transition_row = self._one(
                transition_rows, "active workflow transition"
            )

            assignment_sql = """
                SELECT wsa.*, wi.worker_key, wi.kind AS worker_kind,
                       wi.physical_seat_id AS worker_physical_seat_id,
                       wi.state AS worker_state,
                       wf.fingerprint_sha256, wf.runtime_profile_digest,
                       wf.state AS fingerprint_state,
                       rs.slot_key, rs.kind AS slot_kind,
                       rs.physical_seat_id AS slot_physical_seat_id,
                       rs.state AS slot_state,
                       ps.seat_key, ps.state AS seat_state
                FROM worker_slot_assignments AS wsa
                JOIN worker_identities AS wi ON wi.id = wsa.worker_id
                JOIN worker_fingerprints AS wf
                  ON wf.id = wsa.worker_fingerprint_id
                JOIN runtime_slots AS rs ON rs.id = wsa.slot_id
                LEFT JOIN physical_seats AS ps
                  ON ps.id = rs.physical_seat_id
                WHERE wsa.run_id = ? AND wsa.state = 'ACTIVE'
                  AND (
                    (rs.kind = 'FIXED' AND ps.seat_key = ?)
                    OR (rs.kind = 'ELASTIC' AND wi.worker_key = ?)
                  )
            """
            assignment_parameters: list[Any] = [
                conf["run_id"],
                conf["actor_seat_id"],
                conf["actor_seat_id"],
            ]
            if conf.get("worker_assignment_id"):
                assignment_sql += " AND wsa.id = ?"
                assignment_parameters.append(conf["worker_assignment_id"])
            assignments = connection.execute(
                assignment_sql, tuple(assignment_parameters)
            ).fetchall()
            assignment = self._one(assignments, "active worker assignment")

            capability_rows = connection.execute(
                """
                SELECT * FROM logical_capabilities
                WHERE id = ? AND capability_key = ? AND state = 'ACTIVE'
                """,
                (transition_row["capability_id"], conf["actor_role"]),
            ).fetchall()
            capability = self._one(
                capability_rows, "active logical capability"
            )
            seat_activation = None
            if assignment["slot_kind"] == "FIXED":
                ownership = self._one(
                    connection.execute(
                        """
                        SELECT * FROM seat_capability_ownerships
                        WHERE physical_seat_id = ? AND capability_id = ?
                          AND state = 'ENABLED'
                        """,
                        (
                            assignment["slot_physical_seat_id"],
                            capability["id"],
                        ),
                    ).fetchall(),
                    "enabled physical-seat capability ownership",
                )
                del ownership
                seat_activation = self._one(
                    connection.execute(
                        """
                        SELECT * FROM seat_capability_activations
                        WHERE physical_seat_id = ? AND capability_id = ?
                          AND slot_id = ? AND worker_assignment_id = ?
                          AND goal_id = ? AND run_id = ? AND state = 'ACTIVE'
                        """,
                        (
                            assignment["slot_physical_seat_id"],
                            capability["id"],
                            assignment["slot_id"],
                            assignment["id"],
                            conf["goal_id"],
                            conf["run_id"],
                        ),
                    ).fetchall(),
                    "active seat capability activation",
                )

            activation_rows = connection.execute(
                """
                SELECT a.*, pb.professional_skill_id,
                       pb.compiled_profile_ref, pb.compiled_profile_digest,
                       pb.state AS profile_state
                FROM activations AS a
                JOIN profile_bindings AS pb ON pb.activation_id = a.id
                WHERE a.id = ?
                """,
                (conf["activation_id"],),
            ).fetchall()
            activation = self._one(
                activation_rows, "profile-bound activation"
            )
            profile_references = connection.execute(
                """
                SELECT * FROM profile_reference_bindings
                WHERE activation_id = ? ORDER BY ordinal
                """,
                (conf["activation_id"],),
            ).fetchall()
            if len(profile_references) not in {4, 5}:
                raise TaskFlowContractError(
                    "canonical profile requires four or five references"
                )

            repository_id = str(
                conf.get("repository_registration_id", conf["repo_id"])
            )
            graph_sql = """
                SELECT rr.id AS repository_id, rr.target_id,
                       rr.canonical_path, rr.git_common_dir,
                       rr.source_oid AS repository_source_oid,
                       rr.state AS repository_state,
                       rl.id AS lease_id, rl.goal_id AS lease_goal_id,
                       rl.run_id AS lease_run_id, rl.slot_id AS lease_slot_id,
                       rl.worker_assignment_id AS lease_assignment_id,
                       rl.lease_kind, rl.worktree_path, rl.base_oid,
                       rl.expected_head_oid, rl.write_roots_json,
                       rl.protected_roots_json, rl.state AS lease_state,
                       sb.id AS sandbox_binding_id, sb.subject_oid,
                       sb.cwd, sb.source_root, sb.source_read_only,
                       sb.writable_roots_json, sb.backend,
                       sb.attestation_digest, sb.state AS sandbox_state,
                       sb.bound_at,
                       oa.id AS oid_authority_id, oa.oid AS authority_oid,
                       oa.evidence_digest AS authority_evidence_digest,
                       oa.state AS authority_state
                FROM repository_registrations AS rr
                JOIN runtime_leases AS rl ON rl.repository_id = rr.id
                JOIN sandbox_bindings AS sb ON sb.lease_id = rl.id
                JOIN oid_authorities AS oa
                  ON oa.repository_id = rr.id
                 AND oa.lease_id = rl.id
                 AND oa.sandbox_binding_id = sb.id
                WHERE rr.id = ? AND rr.target_id = ?
                  AND rl.run_id = ? AND rl.goal_id = ?
                  AND rl.slot_id = ? AND rl.worker_assignment_id = ?
                  AND oa.run_id = rl.run_id AND oa.goal_id = rl.goal_id
                  AND oa.authority_kind = 'SUBJECT'
            """
            graph_parameters: list[Any] = [
                repository_id,
                conf["target_id"],
                conf["run_id"],
                conf["goal_id"],
                assignment["slot_id"],
                assignment["id"],
            ]
            lease_selector = conf.get("runtime_lease_id")
            if lease_selector is None and allocation.get("lease_id"):
                lease_selector = allocation["lease_id"]
            if lease_selector is not None:
                graph_sql += " AND rl.id = ?"
                graph_parameters.append(lease_selector)
            sandbox_selector = conf.get("sandbox_binding_id")
            if sandbox_selector is None and allocation.get("sandbox_id"):
                sandbox_selector = allocation["sandbox_id"]
            if sandbox_selector is not None:
                graph_sql += " AND sb.id = ?"
                graph_parameters.append(sandbox_selector)
            if conf.get("oid_authority_id"):
                graph_sql += " AND oa.id = ?"
                graph_parameters.append(conf["oid_authority_id"])
            runtime_graph = self._one(
                connection.execute(
                    graph_sql, tuple(graph_parameters)
                ).fetchall(),
                "active repository lease sandbox OID graph",
            )
            mcp_health = self._mcp_health(connection, canonical_transition)

            serena_snapshot: dict[str, Any] | None = None
            if (
                canonical_transition.serena_onboarding
                or canonical_transition.serena_consumption_receipt_required
            ):
                snapshots = connection.execute(
                    """
                    SELECT * FROM serena_onboarding_snapshots
                    WHERE repository_id = ? AND source_oid = ?
                      AND state = 'ACCEPTED'
                    ORDER BY created_at DESC, id DESC
                    """,
                    (runtime_graph["repository_id"], runtime_graph["subject_oid"]),
                ).fetchall()
                snapshot = self._one(snapshots, "accepted Serena snapshot")
                memories = connection.execute(
                    """
                    SELECT * FROM serena_snapshot_memory_bindings
                    WHERE snapshot_id = ? ORDER BY ordinal
                    """,
                    (snapshot["id"],),
                ).fetchall()
                serena_snapshot = {
                    "snapshot_id": snapshot["id"],
                    "source_oid": snapshot["source_oid"],
                    "policy_sha256": snapshot["policy_digest"],
                    "memory_bindings": [
                        {
                            "name": item["memory_name"],
                            "memory_ref": item["memory_ref"],
                            "sha256": item["memory_sha256"],
                        }
                        for item in memories
                    ],
                }

        expected_workflow_version = str(self.definitions.workflow["version"])
        output_schema_digest = hashlib.sha256(
            self.definitions.activation_result_schema_path.read_bytes()
        ).hexdigest()
        transition_mismatch = (
            workflow["status"] != "ACTIVE"
            or workflow["goal_id"] != conf["goal_id"]
            or workflow["run_id"] != conf["run_id"]
            or workflow["target_id"] != conf["target_id"]
            or workflow["workflow_version"] != expected_workflow_version
            or workflow["workflow_sha256"] != self.definitions.workflow_sha256
            or workflow["current_state_key"] not in canonical_transition.from_states
            or transition_row["from_state_key"] != workflow["current_state_key"]
            or transition_row["to_state_key"] != canonical_transition.to_state
            or transition_row["failure_route"] != canonical_transition.failure_state
            or transition_row["capability_key"] != conf["actor_role"]
            or transition_row["result_kind"] not in canonical_transition.result_kinds
            or bool(transition_row["requires_serena_onboarding"])
            != bool(
                canonical_transition.serena_onboarding
                or canonical_transition.serena_consumption_receipt_required
            )
            or transition_row["output_schema_sha256"] != output_schema_digest
        )
        if transition_mismatch:
            raise TaskFlowContractError(
                "DagRun transition differs from canonical workflow state"
            )
        if (
            assignment["worker_state"] != "ACTIVE"
            or assignment["fingerprint_state"] != "ACTIVE"
            or assignment["slot_state"] in {"QUARANTINED", "RETIRED"}
            or (
                assignment["slot_kind"] == "FIXED"
                and assignment["seat_state"] != "ACTIVE"
            )
        ):
            raise TaskFlowContractError(
                "canonical worker, fingerprint, or slot is not active"
            )
        if (
            activation["target_id"] != conf["target_id"]
            or activation["goal_id"] != conf["goal_id"]
            or activation["run_id"] != conf["run_id"]
            or activation["subject_oid"] != conf["subject_oid"]
            or activation["role"] != conf["actor_role"]
            or activation["gate_or_task"] != conf["gate_or_task"]
            or activation["profile_state"] != "BOUND"
            or activation["professional_skill_id"] != PROFESSIONAL_SKILL_ID
            or activation["compiled_profile_ref"] != profile["compiled_profile_ref"]
            or activation["compiled_profile_digest"]
            != profile["compiled_profile_digest"]
        ):
            raise TaskFlowContractError(
                "TaskFlow activation/profile differs from canonical state"
            )
        writable_roots = self._json_paths(
            runtime_graph["writable_roots_json"], "sandbox writable roots"
        )
        protected_roots = self._json_paths(
            runtime_graph["protected_roots_json"], "lease protected roots"
        )
        if (
            runtime_graph["repository_state"] != "ACTIVE"
            or runtime_graph["lease_state"] != "ACTIVE"
            or runtime_graph["sandbox_state"] != "ACTIVE"
            or runtime_graph["authority_state"] != "ACTIVE"
            or runtime_graph["base_oid"] != conf["base_oid"]
            or runtime_graph["expected_head_oid"] != conf["head_oid"]
            or runtime_graph["subject_oid"] != conf["subject_oid"]
            or runtime_graph["authority_oid"] != conf["subject_oid"]
            or Path(runtime_graph["cwd"]).resolve()
            != Path(str(allocation["cwd"])).resolve()
            or tuple(str(item) for item in allocation["writable_roots"])
            != writable_roots
        ):
            raise TaskFlowContractError(
                "allocation differs from canonical repository sandbox authority"
            )

        bundle = load_and_validate()
        selected = resolve_selection(
            bundle["skill_catalog"],
            bundle["skill_index"],
            str(conf["actor_role"]),
            list(conf.get("selected_skill_ids", ())),
            transition_id=transition_key,
            mcp_policy=load_mcp_policy(bundle["mcp_policy_path"]),
        )
        transition = replace(
            canonical_transition,
            database_id=transition_row["id"],
            output_schema_definition_id=(
                transition_row["output_schema_definition_id"]
            ),
        )
        instance = WorkflowInstance(
            instance_id=workflow["id"],
            goal_id=workflow["goal_id"],
            run_id=workflow["run_id"],
            target_id=workflow["target_id"],
            current_state=workflow["current_state_key"],
            workflow_id=str(self.definitions.workflow["id"]),
            workflow_version=expected_workflow_version,
            workflow_sha256=self.definitions.workflow_sha256,
            status=workflow["status"],
            workflow_definition_id=workflow["workflow_definition_id"],
            state_store=self.state_store,
        )
        actor = ActorBinding(
            activation_id=str(conf["activation_id"]),
            capability_id=capability["capability_key"],
            slot_key=assignment["slot_key"],
            slot_type=assignment["slot_kind"].lower(),
            worker_id=assignment["worker_id"],
            worker_fingerprint=assignment["fingerprint_sha256"],
            worker_fingerprint_id=assignment["worker_fingerprint_id"],
            slot_id=assignment["slot_id"],
            worker_assignment_id=assignment["id"],
            seat_id=(
                assignment["seat_key"]
                if assignment["slot_kind"] == "FIXED"
                else None
            ),
            physical_seat_id=assignment["slot_physical_seat_id"],
            seat_capability_activation_id=(
                seat_activation["id"] if seat_activation is not None else None
            ),
            elastic_lease_id=(
                runtime_graph["lease_id"]
                if assignment["slot_kind"] == "ELASTIC"
                else None
            ),
            compiled_profile_ref=activation["compiled_profile_ref"],
            compiled_profile_sha256=activation["compiled_profile_digest"],
            profile_reference_sha256s=tuple(
                item["reference_sha256"] for item in profile_references
            ),
            selected_skills=tuple(selected["skills"]),
        )
        evidence = EvidenceSet(
            repository=RepositoryBinding(
                runtime_graph["repository_id"],
                runtime_graph["repository_source_oid"],
                runtime_graph["canonical_path"],
                self.state_store,
            ),
            lease_id=runtime_graph["lease_id"],
            sandbox_binding_id=runtime_graph["sandbox_binding_id"],
            oid_authority_id=runtime_graph["oid_authority_id"],
            base_oid=runtime_graph["base_oid"],
            subject_oid=runtime_graph["subject_oid"],
            head_oid=runtime_graph["expected_head_oid"],
            workspace={
                "workspace_id": runtime_graph["sandbox_binding_id"],
                "lease_id": runtime_graph["lease_id"],
                "sandbox_id": runtime_graph["sandbox_binding_id"],
                "cwd": runtime_graph["cwd"],
                "source_roots": [runtime_graph["source_root"]],
                "writable_roots": list(writable_roots),
                "protected_roots": list(protected_roots),
                "prohibited_roots": list(protected_roots),
            },
            mcp_health=mcp_health,
            serena_snapshot=serena_snapshot,
            evidence_refs=(
                f"state://oid-authorities/{runtime_graph['oid_authority_id']}",
                f"state://sandbox-bindings/{runtime_graph['sandbox_binding_id']}",
            ),
            artifact_root=str(Path(str(conf["artifact_root"])).resolve()),
            issued_at=runtime_graph["bound_at"],
        )
        return CanonicalTaskFlowContractInputs(
            instance=instance,
            transition=transition,
            actor=actor,
            evidence=evidence,
        )

    def attempt_inputs(
        self,
        *,
        conf: Mapping[str, Any],
        allocation: Mapping[str, Any],
        profile: Mapping[str, Any],
        context_artifact: Mapping[str, Any],
        request: RunnerRequest,
        contract: ActivationContract,
        admission: AdmissionReceipt,
    ) -> CanonicalTaskFlowAttemptInputs:
        del conf, allocation, profile, context_artifact
        if contract.state_store is not self.state_store:
            raise TaskFlowContractError(
                "activation contract is bound to another AxStateStore"
            )
        if (
            admission.contract_id != contract.contract_id
            or admission.contract_sha256 != contract.digest
        ):
            raise TaskFlowContractError(
                "admission differs from the canonical activation contract"
            )
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT ac.state AS contract_state, sb.backend,
                       sb.state AS sandbox_state, sb.cwd,
                       sb.writable_roots_json, sb.attestation_digest,
                       rl.state AS lease_state, rl.base_oid,
                       rl.expected_head_oid, oa.oid AS authority_oid,
                       oa.state AS authority_state
                FROM activation_contracts AS ac
                JOIN sandbox_bindings AS sb ON sb.id = ac.sandbox_binding_id
                JOIN runtime_leases AS rl ON rl.id = ac.lease_id
                JOIN oid_authorities AS oa ON oa.id = ac.oid_authority_id
                WHERE ac.id = ?
                """,
                (contract.contract_id,),
            ).fetchone()
        if row is None:
            raise TaskFlowContractError(
                "canonical activation contract has no runtime graph"
            )
        if (
            row["contract_state"] != "ADMITTED"
            or row["backend"] != self.backend_id
            or row["sandbox_state"] != "ACTIVE"
            or row["lease_state"] != "ACTIVE"
            or row["authority_state"] != "ACTIVE"
            or request.activation_id != contract.activation_id
            or request.base_oid != row["base_oid"]
            or request.head_oid != row["expected_head_oid"]
            or request.subject_oid != row["authority_oid"]
            or Path(request.cwd).resolve() != Path(row["cwd"]).resolve()
            or tuple(request.writable_roots)
            != self._json_paths(
                row["writable_roots_json"], "sandbox writable roots"
            )
        ):
            raise TaskFlowContractError(
                "runner request differs from admitted canonical runtime authority"
            )
        return CanonicalTaskFlowAttemptInputs(
            backend=row["backend"],
            model=request.model,
            input_digest=request.request_digest,
        )

    def runtime_binding(
        self,
        *,
        contract: ActivationContract,
        admission: AdmissionReceipt,
        attempt_id: str,
        request: RunnerRequest,
    ) -> RuntimeSandboxBinding:
        if contract.state_store is not self.state_store:
            raise TaskFlowContractError(
                "runtime binding contract uses another AxStateStore"
            )
        with self.state_store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT ac.id AS contract_id, ac.repository_id, ac.lease_id,
                       ac.sandbox_binding_id, ac.oid_authority_id,
                       ac.slot_id, ac.capability_id,
                       lc.capability_key, ac.state AS contract_state,
                       ca.decision AS admission_decision,
                       ca.contract_digest AS admission_digest,
                       ct.state AS attempt_state,
                       sb.backend, sb.attestation_digest,
                       sb.state AS sandbox_state
                FROM activation_contracts AS ac
                JOIN logical_capabilities AS lc ON lc.id = ac.capability_id
                JOIN contract_admissions AS ca ON ca.contract_id = ac.id
                JOIN contract_attempts AS ct
                  ON ct.contract_id = ac.id AND ct.id = ?
                JOIN sandbox_bindings AS sb ON sb.id = ac.sandbox_binding_id
                WHERE ac.id = ? AND ca.id = ?
                """,
                (attempt_id, contract.contract_id, admission.receipt_id),
            ).fetchall()
        row = self._one(rows, "active canonical contract attempt")
        if (
            row["admission_decision"] != "ACCEPTED"
            or row["admission_digest"] != contract.digest
            or row["backend"] != self.backend_id
            or row["contract_state"] != "RUNNING"
            or row["attempt_state"] not in {"CREATED", "RUNNING"}
            or row["sandbox_state"] != "ACTIVE"
        ):
            raise TaskFlowContractError(
                "canonical attempt is not executable"
            )
        return RuntimeSandboxBinding(
            contract_id=row["contract_id"],
            activation_id=contract.activation_id,
            attempt_id=attempt_id,
            repository_id=row["repository_id"],
            lease_id=row["lease_id"],
            sandbox_binding_id=row["sandbox_binding_id"],
            oid_authority_id=row["oid_authority_id"],
            slot_id=row["slot_id"],
            capability_id=row["capability_id"],
            attestation_digest=row["attestation_digest"],
            request=request,
        )

    @staticmethod
    def _normalize_outgoing_messages(value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TaskFlowContractError(
                "activation result outgoing_messages must be an array"
            )
        required = {
            "thread_id",
            "work_item_id",
            "from_role",
            "to_role",
            "type",
            "payload",
        }
        optional = {
            "parent_message_id",
            "priority",
            "max_attempts",
            "dedupe_key",
        }
        normalized: list[dict[str, Any]] = []
        for raw in value:
            if not isinstance(raw, Mapping) or set(raw) - required - optional:
                raise TaskFlowContractError(
                    "activation result outgoing message has unknown fields"
                )
            if required - set(raw):
                raise TaskFlowContractError(
                    "activation result outgoing message is incomplete"
                )
            item = dict(raw)
            for field_name in required - {"payload"}:
                item[field_name] = _stable_identifier(
                    item[field_name], f"outgoing_messages.{field_name}"
                )
            if not isinstance(item["payload"], Mapping):
                raise TaskFlowContractError(
                    "activation result outgoing message payload must be an object"
                )
            item["payload"] = dict(item["payload"])
            if "parent_message_id" in item and item["parent_message_id"] is not None:
                item["parent_message_id"] = _stable_identifier(
                    item["parent_message_id"],
                    "outgoing_messages.parent_message_id",
                )
            if "dedupe_key" in item:
                item["dedupe_key"] = _stable_identifier(
                    item["dedupe_key"], "outgoing_messages.dedupe_key"
                )
            for field_name, default, minimum in (
                ("priority", 0, 0),
                ("max_attempts", 5, 1),
            ):
                item[field_name] = item.get(field_name, default)
                if (
                    not isinstance(item[field_name], int)
                    or isinstance(item[field_name], bool)
                    or item[field_name] < minimum
                ):
                    raise TaskFlowContractError(
                        f"outgoing_messages.{field_name} is invalid"
                    )
            normalized.append(item)
        return normalized

    def activation_result(
        self,
        *,
        contract: ActivationContract,
        admission: AdmissionReceipt,
        attempt_id: str,
        runner_result: Mapping[str, Any],
        integrity: Mapping[str, Any],
    ) -> ActivationResult:
        if contract.state_store is not self.state_store:
            raise TaskFlowContractError(
                "activation result contract uses another AxStateStore"
            )
        runner = RunnerResult.from_mapping(runner_result)
        if runner.activation_id != contract.activation_id:
            raise TaskFlowContractError(
                "RunnerResult activation differs from the activation contract"
            )
        try:
            raw_payload = json.loads(runner.receipt.stdout)
        except json.JSONDecodeError as exc:
            raise TaskFlowContractError(
                "RunnerResult stdout is not an activation-result JSON object"
            ) from exc
        if not isinstance(raw_payload, Mapping):
            raise TaskFlowContractError(
                "RunnerResult stdout must contain an activation-result object"
            )
        payload = dict(raw_payload)
        authoritative = {
            "schema_version": 4,
            "result_id": runner.result_id,
            "contract_id": contract.contract_id,
            "activation_id": contract.activation_id,
            "transition_id": contract.transition.transition_id,
            "capability_id": contract.actor.capability_id,
            "subject_oid": contract.evidence.subject_oid,
            "idempotency_key": contract.document["idempotency_key"],
        }
        for field_name, expected in authoritative.items():
            observed = payload.get(field_name)
            if observed is not None and observed != expected:
                raise TaskFlowContractError(
                    f"RunnerResult {field_name} differs from canonical authority"
                )
            payload[field_name] = expected
        payload["mcp_usage_receipts"] = [
            asdict(receipt) for receipt in runner.trusted_mcp_receipts
        ]
        nested = payload.get("payload", {})
        if not isinstance(nested, Mapping):
            raise TaskFlowContractError("activation result payload must be an object")
        normalized_nested = dict(nested)
        normalized_nested["outgoing_messages"] = self._normalize_outgoing_messages(
            normalized_nested.get("outgoing_messages")
        )
        normalized_nested["source_integrity"] = dict(integrity)
        payload["payload"] = normalized_nested
        if admission.contract_id != contract.contract_id:
            raise TaskFlowContractError(
                "activation result admission differs from its contract"
            )
        return ActivationResult(
            contract=contract,
            payload=payload,
            attempt_id=attempt_id,
        )

class TaskFlowContractController(Protocol):
    """Required controller surface for one fail-closed TaskFlow execution."""

    def prepare(
        self,
        *,
        conf: Mapping[str, Any],
        allocation: Mapping[str, Any],
        profile: Mapping[str, Any],
    ) -> Any:
        ...

    def begin_runner_attempt(
        self,
        *,
        conf: Mapping[str, Any],
        allocation: Mapping[str, Any],
        profile: Mapping[str, Any],
        context_artifact: Mapping[str, Any],
        request: RunnerRequest,
        contract: Any,
        admission: Any,
    ) -> RuntimeSandboxBinding:
        ...

    def commit_runner_result(
        self,
        *,
        contract: Any,
        admission: Any,
        attempt_id: str,
        runner_result: Mapping[str, Any],
        integrity: Mapping[str, Any],
    ) -> Any:
        ...


def _require_provider(
    provider: Any,
) -> CanonicalTaskFlowInputProvider:
    if provider is None:
        raise TaskFlowContractError(
            "canonical contract input provider is required"
        )
    missing = [
        method_name
        for method_name in (
            "contract_inputs",
            "attempt_inputs",
            "runtime_binding",
            "activation_result",
        )
        if not callable(getattr(provider, method_name, None))
    ]
    if missing:
        raise TaskFlowContractError(
            "canonical contract input provider is misconfigured; missing "
            + ", ".join(missing)
        )
    return cast(CanonicalTaskFlowInputProvider, provider)


def _write_immutable_artifact(
    path_value: str,
    payload: bytes,
    digest: str,
) -> OpaqueArtifactRef:
    path = Path(path_value).expanduser().resolve(strict=False)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise TaskFlowContractError("activation artifact digest is inconsistent")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if (
            not path.is_file()
            or path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise TaskFlowContractError(
                f"immutable activation artifact already differs: {path}"
            )
    return OpaqueArtifactRef(ref=str(path), digest=digest)


@dataclass(frozen=True)
class CanonicalTaskFlowContractController:
    """Wire TaskFlow to the canonical delivery-v4 contract lifecycle."""

    input_provider: CanonicalTaskFlowInputProvider

    def __post_init__(self) -> None:
        _require_provider(self.input_provider)

    def prepare(
        self,
        *,
        conf: Mapping[str, Any],
        allocation: Mapping[str, Any],
        profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        provider = _require_provider(self.input_provider)
        inputs = provider.contract_inputs(
            conf=conf,
            allocation=allocation,
            profile=profile,
        )
        if not isinstance(inputs, CanonicalTaskFlowContractInputs):
            raise TaskFlowContractError(
                "canonical contract provider must return CanonicalTaskFlowContractInputs"
            )
        contract = compile_canonical_transition(
            inputs.instance,
            inputs.transition,
            inputs.actor,
            inputs.evidence,
        )
        packet = render_canonical_contract(
            contract,
            contract.clauses,
            contract.definitions.template_version,
        )
        provider_store = getattr(provider, "state_store", None)
        required_database_bindings = {
            "transition_database_id",
            "capability_id",
            "output_schema_definition_id",
        }
        if (
            not isinstance(provider_store, AxStateStore)
            or contract.state_store is not provider_store
            or required_database_bindings - set(contract.database_bindings)
        ):
            raise TaskFlowContractError(
                "canonical contract is missing its AxStateStore relation bindings"
            )
        admission = admit_canonical_contract(contract)
        if isinstance(admission, AdmissionReceipt):
            if (
                contract.state_store is not provider_store
                or "mcp_bindings" not in contract.database_bindings
                or "serena_bindings" not in contract.database_bindings
            ):
                raise TaskFlowContractError(
                    "admitted contract has incomplete durable database bindings"
                )
        contract_ref = _write_immutable_artifact(
            packet.contract_ref,
            packet.contract_json.encode("utf-8"),
            packet.contract_sha256,
        )
        packet_ref = _write_immutable_artifact(
            packet.packet_ref,
            packet.markdown.encode("utf-8"),
            packet.packet_sha256,
        )
        return {
            "contract": contract,
            "admission": admission,
            "packet": packet,
            "contract_ref": asdict(contract_ref),
            "packet_ref": asdict(packet_ref),
        }

    def begin_runner_attempt(
        self,
        *,
        conf: Mapping[str, Any],
        allocation: Mapping[str, Any],
        profile: Mapping[str, Any],
        context_artifact: Mapping[str, Any],
        request: RunnerRequest,
        contract: Any,
        admission: Any,
    ) -> RuntimeSandboxBinding:
        if not isinstance(contract, ActivationContract):
            raise TaskFlowContractError(
                "canonical begin_attempt requires ActivationContract"
            )
        if not isinstance(admission, AdmissionReceipt):
            raise TaskFlowContractError(
                "canonical begin_attempt requires accepted AdmissionReceipt"
            )
        provider = _require_provider(self.input_provider)
        inputs = provider.attempt_inputs(
            conf=conf,
            allocation=allocation,
            profile=profile,
            context_artifact=context_artifact,
            request=request,
            contract=contract,
            admission=admission,
        )
        if not isinstance(inputs, CanonicalTaskFlowAttemptInputs):
            raise TaskFlowContractError(
                "canonical contract provider must return CanonicalTaskFlowAttemptInputs"
            )
        if inputs.model != request.model:
            raise TaskFlowContractError(
                "canonical attempt model does not match the runner request"
            )
        if inputs.input_digest != request.request_digest:
            raise TaskFlowContractError(
                "canonical attempt digest does not match the runner request"
            )
        attempt_id = begin_canonical_attempt(
            contract,
            admission,
            backend=inputs.backend,
            model=inputs.model,
            input_digest=inputs.input_digest,
            attempt_kind=inputs.attempt_kind,
        )
        binding = provider.runtime_binding(
            contract=contract,
            admission=admission,
            attempt_id=attempt_id,
            request=request,
        )
        if not isinstance(binding, RuntimeSandboxBinding):
            raise TaskFlowContractError(
                "canonical provider must return RuntimeSandboxBinding"
            )
        return binding

    def commit_runner_result(
        self,
        *,
        contract: Any,
        admission: Any,
        attempt_id: str,
        runner_result: Mapping[str, Any],
        integrity: Mapping[str, Any],
    ) -> Any:
        if not isinstance(contract, ActivationContract):
            raise TaskFlowContractError(
                "canonical commit_result requires ActivationContract"
            )
        if not isinstance(admission, AdmissionReceipt):
            raise TaskFlowContractError(
                "canonical commit_result requires accepted AdmissionReceipt"
            )
        provider = _require_provider(self.input_provider)
        result = provider.activation_result(
            contract=contract,
            admission=admission,
            attempt_id=attempt_id,
            runner_result=runner_result,
            integrity=integrity,
        )
        if not isinstance(result, ActivationResult):
            raise TaskFlowContractError(
                "canonical contract provider must return ActivationResult"
            )
        if (
            result.contract.contract_id != contract.contract_id
            or result.contract.digest != contract.digest
            or result.attempt_id != attempt_id
        ):
            raise TaskFlowContractError(
                "canonical activation result is bound to another contract or attempt"
            )
        return commit_canonical_result(result)


def _require_contract_controller(
    controller: Any,
) -> TaskFlowContractController:
    if controller is None:
        raise TaskFlowContractError(
            "contract controller is required; backend execution is disabled"
        )
    missing = [
        method_name
        for method_name in (
            "prepare",
            "begin_runner_attempt",
            "commit_runner_result",
        )
        if not callable(getattr(controller, method_name, None))
    ]
    if missing:
        raise TaskFlowContractError(
            "contract controller is misconfigured; missing "
            + ", ".join(missing)
        )
    return cast(TaskFlowContractController, controller)


@dataclass(frozen=True)
class RuntimeTaskServices:
    workspace_manager: Any
    review_materializer: Any
    activation_manager: Any
    profile_resolver: Any
    profile_compiler: Any
    context_compiler: ContextCompiler
    seat_policy_provider: Any
    runtime: AgentRuntime
    contract_controller: TaskFlowContractController

    def __post_init__(self) -> None:
        _require_contract_controller(self.contract_controller)


def _contract_document(contract: Any) -> Mapping[str, Any] | None:
    if contract is None:
        return None
    document = getattr(contract, "document", None)
    if isinstance(document, Mapping):
        return document
    if isinstance(contract, Mapping):
        nested = contract.get("document")
        if isinstance(nested, Mapping):
            return nested
        return contract
    return None


def _validated_memory_refs_for_contract(
    conf: Mapping[str, Any],
    contract_control: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    refs = conf.get("validated_memory_refs", [])
    if not isinstance(refs, list):
        raise TaskFlowContractError("validated_memory_refs must be an array")
    if not contract_control or contract_control.get("accepted") is not True:
        if refs:
            raise TaskFlowContractError(
                "Serena memory refs require an accepted activation contract"
            )
        return []
    document = _contract_document(contract_control.get("contract"))
    if document is None:
        if refs:
            raise TaskFlowContractError(
                "Serena memory refs require compiled contract bytes"
            )
        return []
    onboarding = document.get("serena_onboarding")
    if onboarding is None:
        if refs:
            raise TaskFlowContractError(
                "contract has no selected Serena memory bindings"
            )
        return []
    if (
        onboarding.get("source_oid") != conf.get("subject_oid")
        or not isinstance(onboarding.get("policy_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", onboarding["policy_sha256"])
    ):
        raise TaskFlowContractError(
            "Serena onboarding source OID or policy digest changed"
        )
    expected = list(onboarding.get("memory_bindings", []))
    if not expected or len(expected) > 3 or len(refs) != len(expected):
        raise TaskFlowContractError(
            "Serena refs must equal the transition-specific minimum"
        )
    normalized: list[dict[str, Any]] = []
    authoritative_refs: dict[str, str] = {}
    contract_object = contract_control.get("contract")
    snapshot = getattr(getattr(contract_object, "evidence", None), "serena_snapshot", None)
    snapshot_bindings = getattr(snapshot, "memory_bindings", ())
    for binding in snapshot_bindings:
        binding_name = getattr(binding, "name", None)
        binding_ref = getattr(binding, "memory_ref", None)
        if isinstance(binding_name, str) and isinstance(binding_ref, str):
            authoritative_refs[binding_name] = binding_ref
    seen_names: set[str] = set()
    seen_refs: set[str] = set()
    for raw, binding in zip(refs, expected, strict=True):
        if not isinstance(raw, Mapping) or raw.get("validated") is not True:
            raise TaskFlowContractError("Serena ref is not validated")
        name = raw.get("name") or raw.get("memory_name")
        reference = raw.get("ref") or raw.get("memory_ref")
        digest = raw.get("sha256") or raw.get("memory_sha256")
        normalized_ref = str(reference).replace("\\", "/").casefold()
        if (
            name != binding.get("name")
            or digest != binding.get("sha256")
            or not isinstance(reference, str)
            or not reference
            or (
                authoritative_refs
                and authoritative_refs.get(str(name)) != reference
            )
            or normalized_ref in {"*", "all"}
            or normalized_ref.startswith("docs/")
            or "://docs/" in normalized_ref
            or "/docs/" in normalized_ref
            or name in seen_names
            or reference in seen_refs
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise TaskFlowContractError(
                "Serena ref does not match its contract binding"
            )
        seen_names.add(str(name))
        seen_refs.add(reference)
        normalized.append(dict(raw))
    return normalized


def _materialize_pre_mutation_serena_consumption(
    conf: Mapping[str, Any],
    contract_control: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    refs = _validated_memory_refs_for_contract(conf, contract_control)
    if not contract_control:
        return []
    contract = contract_control.get("contract")
    document = _contract_document(contract)
    if document is None:
        return []
    onboarding = document.get("serena_onboarding")
    if (
        document.get("identity", {}).get("capability_id") != "developer"
        or not isinstance(onboarding, Mapping)
        or onboarding.get("consumption_receipt_required") is not True
    ):
        return []
    if not refs:
        raise TaskFlowContractError(
            "developer must consume named Serena refs before source mutation"
        )
    receipts: list[dict[str, str]] = []
    for raw in refs:
        consumed_at = raw.get("consumed_at")
        if (
            raw.get("consumed_before_mutation") is not True
            or not isinstance(consumed_at, str)
            or not consumed_at
        ):
            raise TaskFlowContractError(
                "developer Serena consumption must precede source mutation"
            )
        try:
            datetime.fromisoformat(consumed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TaskFlowContractError(
                "developer Serena consumption timestamp is invalid"
            ) from exc
        receipt = {
            "snapshot_id": str(onboarding["snapshot_id"]),
            "memory_name": str(raw.get("name") or raw.get("memory_name")),
            "memory_sha256": str(raw.get("sha256") or raw.get("memory_sha256")),
            "consumed_at": consumed_at,
        }
        if raw.get("receipt_sha256") != sha256_json(receipt):
            raise TaskFlowContractError(
                "developer Serena consumption receipt digest changed"
            )
        receipts.append(receipt)
    store = getattr(contract, "state_store", None)
    if store is not None:
        database_bindings = getattr(contract, "database_bindings", {})
        memory_bindings = database_bindings.get("serena_bindings", {})
        for receipt in receipts:
            binding_id = memory_bindings.get(receipt["memory_name"])
            if binding_id is None:
                raise TaskFlowContractError(
                    "developer Serena receipt has no durable contract binding"
                )
            store.record_serena_consumption_receipt(
                contract_id=str(document["contract_id"]),
                memory_binding_id=binding_id,
                receipt_digest=sha256_json(receipt),
                idempotency_key=(
                    f"serena-consumption:{document['contract_id']}:"
                    f"{onboarding['snapshot_id']}:{receipt['memory_name']}"
                ),
            )
    return receipts


def _admission_value(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(field, default)
    return getattr(value, field, default)


def _admission_flags(admission: Any) -> tuple[bool, bool]:
    if isinstance(admission, ContractViolation):
        return False, False
    accepted = _admission_value(admission, "accepted")
    permitted = _admission_value(admission, "model_call_permitted")
    if not isinstance(accepted, bool) or not isinstance(permitted, bool):
        raise TaskFlowContractError(
            "contract admission must contain boolean accepted and "
            "model_call_permitted fields"
        )
    return accepted, permitted


def _require_accepted_contract_control(
    contract_control: Mapping[str, Any] | None,
    *,
    activation_id: str,
) -> Mapping[str, Any]:
    if not isinstance(contract_control, Mapping):
        raise TaskFlowContractError(
            "accepted activation contract control is required before backend execution"
        )
    if (
        contract_control.get("stage") != "compile_admit_contract"
        or contract_control.get("activation_id") != activation_id
        or contract_control.get("enforced") is not True
    ):
        raise TaskFlowContractError(
            "enforced activation contract control is required before backend execution"
        )
    if (
        contract_control.get("contract") is None
        or contract_control.get("admission") is None
        or contract_control.get("packet") is None
        or not isinstance(contract_control.get("contract_ref"), Mapping)
        or not isinstance(contract_control.get("packet_ref"), Mapping)
    ):
        raise TaskFlowContractError(
            "activation contract control is incomplete; backend execution is disabled"
        )
    if not (
        contract_control.get("accepted") is True
        and contract_control.get("model_call_permitted") is True
    ):
        raise TaskFlowContractError(
            "backend execution is forbidden after rejected admission"
        )
    return contract_control


def compile_admit_contract(
    conf: Mapping[str, Any],
    allocation: Mapping[str, Any],
    profile: Mapping[str, Any],
    services: RuntimeTaskServices,
) -> dict[str, Any]:
    """Run deterministic contract admission before any backend attempt."""

    sealed = assert_immutable_conf(conf)
    controller = _require_contract_controller(services.contract_controller)
    prepared = controller.prepare(
        conf=sealed,
        allocation=dict(allocation),
        profile=dict(profile),
    )
    if isinstance(prepared, Mapping):
        contract = prepared.get("contract")
        admission = prepared.get("admission")
        packet = prepared.get("packet")
        contract_ref = prepared.get("contract_ref")
        packet_ref = prepared.get("packet_ref")
    else:
        contract = getattr(prepared, "contract", None)
        admission = getattr(prepared, "admission", None)
        packet = getattr(prepared, "packet", None)
        contract_ref = getattr(prepared, "contract_ref", None)
        packet_ref = getattr(prepared, "packet_ref", None)
    if (
        contract is None
        or admission is None
        or packet is None
        or not isinstance(contract_ref, Mapping)
        or not isinstance(packet_ref, Mapping)
    ):
        raise TaskFlowContractError(
            "contract controller returned an incomplete preparation"
        )
    accepted, permitted = _admission_flags(admission)
    if accepted != permitted:
        raise TaskFlowContractError(
            "contract admission and model-call permission differ"
        )
    return {
        "stage": "compile_admit_contract",
        "activation_id": sealed["activation_id"],
        "enforced": True,
        "accepted": accepted,
        "model_call_permitted": permitted,
        "contract": contract,
        "admission": admission,
        "packet": packet,
        "contract_ref": dict(contract_ref),
        "packet_ref": dict(packet_ref),
    }


@dataclass(frozen=True)
class DeterministicServiceCommand:
    command_id: str
    service_identity: str
    operation: str
    payload: Mapping[str, Any]
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name in ("command_id", "operation", "idempotency_key"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise TaskFlowContractError(f"{field_name} must be non-empty")
        if self.service_identity not in _SERVICE_IDENTITIES:
            raise TaskFlowContractError("unknown deterministic service identity")
        _reject_service_actor_fields(self.payload)

    @property
    def command_digest(self) -> str:
        return sha256_json(
            {
                "command_id": self.command_id,
                "service_identity": self.service_identity,
                "operation": self.operation,
                "payload": dict(self.payload),
                "idempotency_key": self.idempotency_key,
            }
        )


class DeterministicServiceDispatcher:
    """Call deterministic controllers without allocating an LLM seat or profile."""

    def __init__(
        self,
        handlers: Mapping[str, Callable[[str, Mapping[str, Any], str], Any]],
    ) -> None:
        unknown = set(handlers) - _SERVICE_IDENTITIES
        if unknown:
            raise TaskFlowContractError(
                f"unknown service handlers: {sorted(unknown)}"
            )
        self._handlers = dict(handlers)

    def execute(self, command: DeterministicServiceCommand) -> dict[str, Any]:
        handler = self._handlers.get(command.service_identity)
        if handler is None:
            raise TaskFlowContractError(
                f"no handler for {command.service_identity}"
            )
        result = handler(
            command.operation,
            dict(command.payload),
            command.idempotency_key,
        )
        if hasattr(result, "__dataclass_fields__"):
            result_payload: Any = asdict(result)
        elif isinstance(result, Mapping):
            result_payload = dict(result)
        else:
            result_payload = {"value": str(result)}
        return {
            "command_id": command.command_id,
            "command_digest": command.command_digest,
            "service_identity": command.service_identity,
            "result": result_payload,
        }


def _reject_service_actor_fields(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        forbidden = set(map(str, value)) & _SERVICE_FORBIDDEN_FIELDS
        if forbidden:
            raise TaskFlowContractError(
                f"{path} contains LLM-seat fields: {sorted(forbidden)}"
            )
        for key, nested in value.items():
            _reject_service_actor_fields(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_service_actor_fields(nested, f"{path}[{index}]")


def _stable_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise TaskFlowContractError(f"{field} must be a stable identifier")
    return value


def _full_oid(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _OID_RE.fullmatch(value):
        raise TaskFlowContractError(
            f"{field} must be a full lowercase 40-64 character OID"
        )
    return value


def _unique_strings(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
        or (not allow_empty and not value)
    ):
        raise TaskFlowContractError(
            f"{field} must be a unique array of non-empty strings"
        )
    return list(value)


def _conf_digest(conf: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in conf.items() if key != _CONF_DIGEST_FIELD})


def validate_runtime_conf(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate once and seal every scheduler-visible runtime input."""

    if not isinstance(value, Mapping):
        raise TaskFlowContractError("DagRun conf must be an object")
    conf = dict(value)
    supplied_digest = conf.pop(_CONF_DIGEST_FIELD, None)
    unknown = set(conf) - _REQUIRED_CONF - _OPTIONAL_CONF
    missing = _REQUIRED_CONF - set(conf)
    if unknown or missing:
        raise TaskFlowContractError(
            f"DagRun conf field mismatch: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    for field in (
        "target_id",
        "goal_id",
        "work_item_id",
        "thread_id",
        "run_id",
        "activation_id",
        "idempotency_key",
        "actor_role",
        "actor_seat_id",
        "gate_or_task",
        "repo_id",
        "context_profile",
        "tool_policy_id",
    ):
        conf[field] = _stable_identifier(conf[field], field)
    for field in (
        "workflow_instance_id",
        "transition_id",
        "worker_assignment_id",
        "repository_registration_id",
        "runtime_lease_id",
        "sandbox_binding_id",
        "oid_authority_id",
    ):
        if field in conf:
            conf[field] = _stable_identifier(conf[field], field)
    if not isinstance(conf["revision"], int) or isinstance(conf["revision"], bool) or conf["revision"] < 1:
        raise TaskFlowContractError("revision must be a positive integer")
    if conf["execution_kind"] not in {kind.value for kind in ExecutionKind}:
        raise TaskFlowContractError("execution_kind must be development or review")
    for field in ("base_oid", "head_oid", "subject_oid"):
        conf[field] = _full_oid(conf[field], field)
    if conf["execution_kind"] == ExecutionKind.DEVELOPMENT.value:
        if conf["subject_oid"] != conf["head_oid"]:
            raise TaskFlowContractError(
                "development subject_oid must equal the allocated head_oid"
            )
    elif conf["subject_oid"] != conf["head_oid"]:
        raise TaskFlowContractError(
            "review head_oid must identify the exact submitted subject_oid"
        )

    conf["target_paths"] = _unique_strings(conf["target_paths"], "target_paths", allow_empty=False)
    conf["source_write_scope"] = _unique_strings(
        conf["source_write_scope"], "source_write_scope", allow_empty=False
    )
    conf["generated_write_scope"] = _unique_strings(
        conf["generated_write_scope"], "generated_write_scope"
    )
    conf["command"] = _unique_strings(conf["command"], "command", allow_empty=False)
    conf["allowed_tools"] = _unique_strings(conf["allowed_tools"], "allowed_tools")
    prefixes = conf["command_prefixes"]
    if (
        not isinstance(prefixes, list)
        or not prefixes
        or any(
            not isinstance(prefix, list)
            or not prefix
            or not all(isinstance(item, str) and item for item in prefix)
            for prefix in prefixes
        )
    ):
        raise TaskFlowContractError(
            "command_prefixes must contain non-empty command arrays"
        )
    conf["command_prefixes"] = [list(prefix) for prefix in prefixes]
    for field in ("selected_skill_ids", "context_paths", "generated_paths", "redaction_literals"):
        if field in conf:
            conf[field] = _unique_strings(conf[field], field)
    for field in ("repository_manifests", "build_evidence", "environment"):
        if field in conf and not isinstance(conf[field], Mapping):
            raise TaskFlowContractError(f"{field} must be an object")
        if field in conf:
            conf[field] = dict(conf[field])
    if not isinstance(conf["repository_manifests"], Mapping):
        raise TaskFlowContractError("repository_manifests must be an object")
    if not isinstance(conf["build_evidence"], Mapping):
        raise TaskFlowContractError("build_evidence must be an object")
    if "validated_memory_refs" in conf:
        if not isinstance(conf["validated_memory_refs"], list) or not all(
            isinstance(item, Mapping) for item in conf["validated_memory_refs"]
        ):
            raise TaskFlowContractError(
                "validated_memory_refs must be an array of opaque bindings"
            )
        conf["validated_memory_refs"] = [
            dict(item) for item in conf["validated_memory_refs"]
        ]
    for field in ("max_messages", "max_diff_chars"):
        if field in conf and (
            not isinstance(conf[field], int)
            or isinstance(conf[field], bool)
            or conf[field] < 0
        ):
            raise TaskFlowContractError(f"{field} must be non-negative")
    conf["lease_seconds"] = int(conf.get("lease_seconds", 3600))
    if not 60 <= conf["lease_seconds"] <= 86_400:
        raise TaskFlowContractError("lease_seconds must be in [60, 86400]")
    conf["runner_timeout_seconds"] = float(conf.get("runner_timeout_seconds", 1800))
    if not 0 < conf["runner_timeout_seconds"] <= 3600:
        raise TaskFlowContractError("runner_timeout_seconds must be in (0, 3600]")
    for field, default in (
        ("stdout_limit_bytes", 262_144),
        ("stderr_limit_bytes", 262_144),
    ):
        conf[field] = int(conf.get(field, default))
        if not 1 <= conf[field] <= 4_194_304:
            raise TaskFlowContractError(f"{field} must be in [1, 4194304]")
    for field in ("network_allowed", "shell_allowed"):
        conf[field] = bool(conf.get(field, False))
    for field in ("db_path", "registry_path", "artifact_root"):
        raw = conf[field]
        if not isinstance(raw, str) or not raw:
            raise TaskFlowContractError(f"{field} must be a non-empty path")
        conf[field] = str(Path(raw).expanduser().resolve(strict=False))
    if "context_action" in conf:
        conf["context_action"] = _stable_identifier(conf["context_action"], "context_action")

    # This call also rejects credential, Git-authority, and non-allowlisted keys.
    environment = minimal_environment(conf.get("environment", {}))
    conf["environment"] = dict(environment)
    sealed_digest = _conf_digest(conf)
    if supplied_digest is not None and supplied_digest != sealed_digest:
        raise TaskFlowContractError("immutable DagRun conf changed between tasks")
    conf[_CONF_DIGEST_FIELD] = sealed_digest
    return conf


def assert_immutable_conf(value: Mapping[str, Any]) -> dict[str, Any]:
    if _CONF_DIGEST_FIELD not in value:
        raise TaskFlowContractError("runtime conf has not been sealed")
    return validate_runtime_conf(value)


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


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _resource_receipt_digest(receipt: Mapping[str, Any]) -> str:
    return sha256_json(
        {
            key: value
            for key, value in receipt.items()
            if key not in {"stage", "resource_receipt_digest"}
        }
    )


def allocate_and_bind_activation(
    conf: Mapping[str, Any],
    services: RuntimeTaskServices,
) -> dict[str, Any]:
    """Allocate one worktree/sandbox and bind its immutable activation envelope."""

    sealed = assert_immutable_conf(conf)
    if sealed["execution_kind"] == ExecutionKind.DEVELOPMENT.value:
        lease = services.workspace_manager.allocate_development(
            goal_id=sealed["goal_id"],
            work_item_id=sealed["work_item_id"],
            revision=sealed["revision"],
            owner_seat_id=sealed["actor_seat_id"],
            target_id=sealed["target_id"],
            base_oid=sealed["base_oid"],
            source_write_scope=tuple(sealed["source_write_scope"]),
            generated_write_scope=tuple(sealed["generated_write_scope"]),
            lease_seconds=sealed["lease_seconds"],
            idempotency_key=f"workspace:{sealed['idempotency_key']}",
        )
        contract = services.workspace_manager.execution_contract(lease.lease_id)
        if (
            contract.target_id != sealed["target_id"]
            or contract.goal_id != sealed["goal_id"]
            or contract.work_item_id != sealed["work_item_id"]
            or contract.revision != sealed["revision"]
            or contract.owner != sealed["actor_seat_id"]
            or contract.expected_head_oid != sealed["head_oid"]
        ):
            raise TaskFlowContractError(
                "workspace lease differs from immutable DagRun conf"
            )
        cwd = Path(contract.cwd).resolve(strict=True)
        source_roots = tuple(
            Path(path).resolve(strict=False) for path in contract.source_write_paths
        )
        generated_roots = tuple(
            Path(path).resolve(strict=False) for path in contract.generated_write_paths
        )
        for path in source_roots + generated_roots:
            if not _within(path, cwd):
                raise TaskFlowContractError(
                    "workspace execution scope escapes the assigned worktree"
                )
            path.mkdir(parents=True, exist_ok=True)
        receipt: dict[str, Any] = {
            "stage": "allocate_bind_activation",
            "execution_kind": ExecutionKind.DEVELOPMENT.value,
            "activation_id": sealed["activation_id"],
            "activation_binding_state": "RESOURCE_BOUND_PENDING_PROFILE",
            "workspace_id": lease.workspace_id,
            "lease_id": lease.lease_id,
            "sandbox_id": None,
            "cwd": str(cwd),
            "resource_root": str(cwd),
            "source_roots": [str(path) for path in source_roots],
            "generated_roots": [str(path) for path in generated_roots],
            "ephemeral_writable_roots": [],
            "writable_roots": [
                str(path) for path in source_roots + generated_roots
            ],
            "protected_roots": [],
            "prohibited_roots": [
                str(Path(path).resolve(strict=False))
                for path in contract.prohibited_roots
            ],
            "subject_oid": sealed["subject_oid"],
        }
    else:
        sandbox = services.review_materializer.materialize(
            activation_id=sealed["activation_id"],
            target_id=sealed["target_id"],
            subject_oid=sealed["subject_oid"],
            generated_paths=tuple(sealed.get("generated_paths", ())),
        )
        contract = services.review_materializer.runner_contract(sandbox.sandbox_id)
        if (
            contract.activation_id != sealed["activation_id"]
            or contract.sandbox_id != sandbox.sandbox_id
            or contract.subject_oid != sealed["subject_oid"]
        ):
            raise TaskFlowContractError(
                "review sandbox differs from immutable DagRun conf"
            )
        cwd = Path(contract.cwd).resolve(strict=True)
        source_roots = (Path(contract.analysis_source_root).resolve(strict=True),)
        generated_roots = tuple(
            Path(path).resolve(strict=False)
            for path in contract.generated_write_roots
        )
        ephemeral_roots = tuple(
            Path(path).resolve(strict=False)
            for path in contract.ephemeral_writable_roots
        )
        for path in generated_roots + ephemeral_roots:
            if not _within(path, cwd):
                raise TaskFlowContractError(
                    "review writable scope escapes the sandbox"
                )
            path.mkdir(parents=True, exist_ok=True)
        receipt = {
            "stage": "allocate_bind_activation",
            "execution_kind": ExecutionKind.REVIEW.value,
            "activation_id": sealed["activation_id"],
            "activation_binding_state": "RESOURCE_BOUND_PENDING_PROFILE",
            "workspace_id": sandbox.sandbox_id,
            "lease_id": None,
            "sandbox_id": sandbox.sandbox_id,
            "cwd": str(cwd),
            "resource_root": str(cwd),
            "source_roots": [str(path) for path in source_roots],
            "generated_roots": [str(path) for path in generated_roots],
            "ephemeral_writable_roots": [str(path) for path in ephemeral_roots],
            "writable_roots": [
                str(path) for path in generated_roots + ephemeral_roots
            ],
            "protected_roots": [
                str(Path(path).resolve(strict=False))
                for path in contract.protected_metadata_roots
            ],
            "prohibited_roots": [
                str(Path(path).resolve(strict=False))
                for path in contract.prohibited_authority_roots
            ],
            "subject_oid": contract.subject_oid,
            "sandbox_metadata_digest": sandbox.metadata_digest,
        }
    receipt["resource_receipt_digest"] = _resource_receipt_digest(receipt)
    return receipt


def resolve_compile_profile(
    conf: Mapping[str, Any],
    allocation: Mapping[str, Any],
    services: RuntimeTaskServices,
) -> dict[str, Any]:
    """Resolve external profile refs, then create and bind the durable activation."""

    sealed = assert_immutable_conf(conf)
    if (
        allocation.get("stage") != "allocate_bind_activation"
        or allocation.get("activation_id") != sealed["activation_id"]
        or allocation.get("resource_receipt_digest")
        != _resource_receipt_digest(allocation)
    ):
        raise TaskFlowContractError("allocation receipt is missing, stale, or changed")
    activation_root = (
        services.activation_manager.path_authority.activation_root(
            sealed["goal_id"], sealed["activation_id"]
        )
        .resolve(strict=False)
    )
    activation_root.mkdir(parents=True, exist_ok=True)
    resolution = services.profile_resolver.resolve(
        ProfileResolutionRequest(
            activation_id=sealed["activation_id"],
            role=sealed["actor_role"],
            gate_or_task=sealed["gate_or_task"],
            subject_oid=sealed["subject_oid"],
            target_paths=tuple(sealed["target_paths"]),
            write_scope=tuple(
                sealed["source_write_scope"] + sealed["generated_write_scope"]
            ),
            repository_manifests=dict(sealed["repository_manifests"]),
            build_evidence=dict(sealed["build_evidence"]),
        )
    )

    # ActivationSpec needs the immutable digest.  Bootstrap deterministically
    # without state, create the activation, then let the canonical stateful
    # compiler persist the exact same binding.
    catalog = getattr(services.profile_resolver, "catalog", None)
    if catalog is None:
        bootstrap_compiler = services.profile_compiler
    else:
        bootstrap_compiler = ProfessionalProfileCompiler(catalog)
    bootstrap = bootstrap_compiler.compile(
        resolution,
        activation_root=activation_root,
    )
    spec = ActivationSpec(
        activation_id=sealed["activation_id"],
        subject_oid=sealed["subject_oid"],
        workspace_or_sandbox_id=str(allocation["workspace_id"]),
        professional_skill_id=PROFESSIONAL_SKILL_ID,
        compiled_profile_ref=str(bootstrap.compiled_path),
        compiled_profile_digest=bootstrap.compiled_digest,
        allowed_tools=tuple(sealed["allowed_tools"]),
        commands=tuple(sealed["command"]),
    )
    services.activation_manager.create(
        spec,
        target_id=sealed["target_id"],
        goal_id=sealed["goal_id"],
        run_id=sealed["run_id"],
        role=sealed["actor_role"],
        gate_or_task=sealed["gate_or_task"],
        idempotency_key=f"activation:{sealed['idempotency_key']}",
    )
    compiled = services.profile_compiler.compile(
        resolution,
        activation_root=activation_root,
    )
    if (
        Path(compiled.compiled_path).resolve(strict=True)
        != Path(bootstrap.compiled_path).resolve(strict=True)
        or compiled.compiled_digest != bootstrap.compiled_digest
    ):
        services.activation_manager.quarantine(
            sealed["activation_id"],
            "professional profile changed between bootstrap and durable binding",
        )
        raise TaskFlowContractError("professional profile compilation is not deterministic")
    services.activation_manager.bind_profile(
        sealed["activation_id"], compiled.compiled_digest
    )
    services.activation_manager.bind_workspace(
        sealed["activation_id"], str(allocation["workspace_id"])
    )
    return {
        "stage": "resolve_compile_profile",
        "activation_id": sealed["activation_id"],
        "activation_root": str(activation_root),
        "professional_skill_id": PROFESSIONAL_SKILL_ID,
        "compiled_profile_ref": str(Path(compiled.compiled_path).resolve(strict=True)),
        "compiled_profile_digest": compiled.compiled_digest,
        "reference_ids": [
            reference.reference_id
            for _, reference in resolution.ordered_references
        ],
    }


def _runtime_binding(
    conf: Mapping[str, Any],
    allocation: Mapping[str, Any],
    environment_digest: str,
) -> dict[str, Any]:
    return {
        "activation_id": conf["activation_id"],
        "workspace_id": allocation["workspace_id"],
        "lease_id": allocation["lease_id"],
        "sandbox_id": allocation["sandbox_id"],
        "execution_kind": allocation["execution_kind"],
        "base_oid": conf["base_oid"],
        "head_oid": conf["head_oid"],
        "subject_oid": conf["subject_oid"],
        "seat_id": conf["actor_seat_id"],
        "role_key": conf["actor_role"],
        "cwd": allocation["cwd"],
        "source_roots": list(allocation["source_roots"]),
        "generated_roots": list(allocation["generated_roots"]),
        "ephemeral_writable_roots": list(allocation["ephemeral_writable_roots"]),
        "protected_roots": list(allocation["protected_roots"]),
        "prohibited_roots": list(allocation["prohibited_roots"]),
        "environment_digest": environment_digest,
        "tool_policy_id": conf["tool_policy_id"],
    }


def compile_context_to_artifact(
    conf: Mapping[str, Any],
    allocation: Mapping[str, Any],
    profile: Mapping[str, Any],
    services: RuntimeTaskServices,
    contract_control: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sealed = assert_immutable_conf(conf)
    if (
        profile.get("stage") != "resolve_compile_profile"
        or profile.get("activation_id") != sealed["activation_id"]
        or profile.get("professional_skill_id") != PROFESSIONAL_SKILL_ID
    ):
        raise TaskFlowContractError("professional profile receipt is not bound")
    environment = tuple(sorted(dict(sealed["environment"]).items()))
    environment_digest = environment_fingerprint(environment)
    runtime_binding = _runtime_binding(sealed, allocation, environment_digest)
    validated_memory_refs = _validated_memory_refs_for_contract(
        sealed, contract_control
    )
    context = services.context_compiler.compile(
        thread_id=sealed["thread_id"],
        work_item_id=sealed["work_item_id"],
        target_role=sealed["actor_role"],
        repo_id=sealed["repo_id"],
        base_oid=sealed["base_oid"],
        head_oid=sealed["head_oid"],
        context_profile=sealed["context_profile"],
        context_action=sealed.get("context_action"),
        actor_seat_id=sealed["actor_seat_id"],
        selected_skill_ids=sealed.get("selected_skill_ids"),
        compiled_profile_ref=profile["compiled_profile_ref"],
        compiled_profile_digest=profile["compiled_profile_digest"],
        runtime_binding=runtime_binding,
        activation_contract_packet=(
            contract_control.get("contract") if contract_control else None
        ),
        validated_memory_refs=(
            validated_memory_refs or None
        ),
        max_messages=sealed.get("max_messages"),
        max_diff_chars=sealed.get("max_diff_chars"),
        paths=sealed.get("context_paths"),
    )
    activation_root = Path(str(profile["activation_root"])).resolve(strict=True)
    context_path = activation_root / "context.json"
    context_bytes = (canonical_json(context) + "\n").encode("utf-8")
    if context_path.exists():
        if context_path.read_bytes() != context_bytes:
            raise TaskFlowContractError(
                "deterministic context replay differs for this activation"
            )
    else:
        temporary = activation_root / ".context.json.tmp"
        temporary.write_bytes(context_bytes)
        os.replace(temporary, context_path)
    return {
        "stage": "bounded_context",
        "activation_id": sealed["activation_id"],
        "context_ref": str(context_path),
        "context_digest": hashlib.sha256(context_bytes).hexdigest(),
        "context_chars": context["context_chars"],
        "injected_context_chars": context["injected_context_chars"],
        "runtime_binding": runtime_binding,
    }


def _runner_request(
    conf: Mapping[str, Any],
    allocation: Mapping[str, Any],
    profile: Mapping[str, Any],
    context_artifact: Mapping[str, Any],
    services: RuntimeTaskServices,
) -> RunnerRequest:
    policy = dict(services.seat_policy_provider.resolve(conf["actor_seat_id"]))
    if policy.get("service_identity") is not False:
        raise TaskFlowContractError("LLM execution requires a configured human-role seat")
    active_capability = policy.get("active_capability", policy.get("role_key"))
    if active_capability != conf["actor_role"]:
        raise TaskFlowContractError("seat role differs from immutable DagRun conf")
    if policy.get("professional_skill_id") != PROFESSIONAL_SKILL_ID:
        raise TaskFlowContractError("seat has the wrong professional runtime skill")
    environment = tuple(sorted(dict(conf["environment"]).items()))
    tool_policy = ToolPolicy(
        policy_id=conf["tool_policy_id"],
        command_prefixes=tuple(
            tuple(prefix) for prefix in conf["command_prefixes"]
        ),
        network_allowed=conf["network_allowed"],
        shell_allowed=conf["shell_allowed"],
    )
    context_path = Path(str(context_artifact["context_ref"])).resolve(strict=True)
    packet = json.loads(context_path.read_text(encoding="utf-8"))
    selected_skills = materialize_skill_instructions(packet["skill_packet"])
    invocation = {
        "activation_id": conf["activation_id"],
        "goal_id": conf["goal_id"],
        "work_item_id": conf["work_item_id"],
        "thread_id": conf["thread_id"],
        "revision": conf["revision"],
        "seat_id": conf["actor_seat_id"],
        "role_key": conf["actor_role"],
        "context_ref": str(context_path),
        "context_digest": context_artifact["context_digest"],
        "compiled_profile_ref": profile["compiled_profile_ref"],
        "compiled_profile_digest": profile["compiled_profile_digest"],
        "selected_skills": selected_skills,
        "serena_memory_refs": list(conf.get("validated_memory_refs", [])),
    }
    return RunnerRequest(
        target_id=conf["target_id"],
        goal_id=conf["goal_id"],
        work_item_id=conf["work_item_id"],
        revision=conf["revision"],
        activation_id=conf["activation_id"],
        workspace_id=allocation["workspace_id"],
        lease_id=allocation["lease_id"],
        sandbox_id=allocation["sandbox_id"],
        execution_kind=ExecutionKind(allocation["execution_kind"]),
        base_oid=conf["base_oid"],
        head_oid=conf["head_oid"],
        subject_oid=conf["subject_oid"],
        seat_id=conf["actor_seat_id"],
        role_key=conf["actor_role"],
        gate_id=conf["gate_or_task"],
        model=str(policy["model"]),
        model_reasoning_effort=str(policy["model_reasoning_effort"]),
        cwd=allocation["cwd"],
        resource_root=allocation["resource_root"],
        activation_root=profile["activation_root"],
        source_scope=tuple(conf["source_write_scope"]),
        generated_scope=tuple(conf["generated_write_scope"]),
        source_roots=tuple(allocation["source_roots"]),
        generated_roots=tuple(allocation["generated_roots"]),
        ephemeral_writable_roots=tuple(
            allocation["ephemeral_writable_roots"]
        ),
        writable_roots=tuple(allocation["writable_roots"]),
        protected_roots=tuple(allocation["protected_roots"]),
        prohibited_roots=tuple(allocation["prohibited_roots"]),
        professional_skill_id=PROFESSIONAL_SKILL_ID,
        compiled_profile_ref=profile["compiled_profile_ref"],
        compiled_profile_digest=profile["compiled_profile_digest"],
        context_ref=str(context_path),
        context_digest=context_artifact["context_digest"],
        tool_policy=tool_policy,
        idempotency_key=f"runner:{conf['idempotency_key']}",
        environment=environment,
        environment_digest=environment_fingerprint(environment),
        command=tuple(conf["command"]),
        stdin=canonical_json(invocation),
        timeout_seconds=conf["runner_timeout_seconds"],
        output_policy=OutputPolicy(
            stdout_limit_bytes=conf["stdout_limit_bytes"],
            stderr_limit_bytes=conf["stderr_limit_bytes"],
            redaction_literals=tuple(conf.get("redaction_literals", ())),
        ),
    )


def execute_agent_runner(
    conf: Mapping[str, Any],
    allocation: Mapping[str, Any],
    profile: Mapping[str, Any],
    context_artifact: Mapping[str, Any],
    services: RuntimeTaskServices,
    contract_control: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sealed = assert_immutable_conf(conf)
    controller = _require_contract_controller(services.contract_controller)
    admitted = _require_accepted_contract_control(
        contract_control,
        activation_id=sealed["activation_id"],
    )
    if context_artifact.get("stage") != "bounded_context":
        raise TaskFlowContractError("bounded context receipt is required")
    pre_mutation_serena_receipts = _materialize_pre_mutation_serena_consumption(
        sealed, admitted
    )
    if allocation["execution_kind"] == ExecutionKind.REVIEW.value:
        preflight = services.review_materializer.verify_integrity(
            allocation["sandbox_id"]
        )
        if preflight.classification is not SourceIntegrity.CLEAN:
            services.activation_manager.quarantine(
                sealed["activation_id"],
                "review sandbox was not CLEAN before execution",
            )
            raise TaskFlowIntegrityError(
                "review sandbox must be CLEAN before confined execution"
            )
    try:
        request = _runner_request(
            sealed, allocation, profile, context_artifact, services
        )
        runtime_binding = controller.begin_runner_attempt(
            conf=sealed,
            allocation=dict(allocation),
            profile=dict(profile),
            context_artifact=dict(context_artifact),
            request=request,
            contract=admitted["contract"],
            admission=admitted["admission"],
        )
        if not isinstance(runtime_binding, RuntimeSandboxBinding):
            raise TaskFlowContractError(
                "contract controller returned an invalid runtime sandbox binding"
            )
        attempt_id = runtime_binding.attempt_id
        services.activation_manager.mark_running(
            sealed["activation_id"], attempt_id
        )
        contract_ref = OpaqueArtifactRef(**dict(admitted["contract_ref"]))
        packet_ref = OpaqueArtifactRef(**dict(admitted["packet_ref"]))
        result = services.runtime.execute(
            contract_ref,
            packet_ref,
            runtime_binding,
        )
    except Exception as exc:
        services.activation_manager.quarantine(
            sealed["activation_id"],
            f"confined execution failed: {type(exc).__name__}: {exc}",
        )
        raise
    return {
        "stage": "confined_execute",
        "activation_id": sealed["activation_id"],
        "contract_attempt_id": attempt_id,
        "runner_result": result.as_mapping(),
        "serena_pre_mutation_receipts": pre_mutation_serena_receipts,
    }


def verify_execution_integrity(
    conf: Mapping[str, Any],
    allocation: Mapping[str, Any],
    execution: Mapping[str, Any],
    services: RuntimeTaskServices,
) -> dict[str, Any]:
    sealed = assert_immutable_conf(conf)
    if (
        execution.get("stage") != "confined_execute"
        or execution.get("activation_id") != sealed["activation_id"]
    ):
        raise TaskFlowContractError("confined execution receipt is required")
    if allocation["execution_kind"] == ExecutionKind.DEVELOPMENT.value:
        return {
            "stage": "integrity_verify",
            "activation_id": sealed["activation_id"],
            "classification": SourceIntegrity.CLEAN.value,
            "gate_evidence_eligible": True,
            "clean_rerun_required": False,
            "integrity_evidence": None,
        }

    observed = services.review_materializer.verify_integrity(
        allocation["sandbox_id"]
    )
    classification = observed.classification
    if hasattr(observed, "__dataclass_fields__"):
        evidence = asdict(observed)
    elif isinstance(observed, Mapping):
        evidence = dict(observed)
    else:
        evidence = {
            key: value
            for key, value in vars(observed).items()
            if not key.startswith("_")
        }
    evidence["classification"] = classification.value
    if classification is SourceIntegrity.CLEAN:
        eligible = True
        clean_rerun = False
    elif classification is SourceIntegrity.ANALYSIS_DIRTY:
        # Exploratory evidence is retained, but it cannot satisfy TA/QA gates.
        eligible = False
        clean_rerun = True
    else:
        eligible = False
        clean_rerun = False
    return {
        "stage": "integrity_verify",
        "activation_id": sealed["activation_id"],
        "classification": classification.value,
        "gate_evidence_eligible": eligible,
        "clean_rerun_required": clean_rerun,
        "integrity_evidence": evidence,
    }


def _runner_output(result: Mapping[str, Any]) -> dict[str, Any]:
    receipt = result.get("receipt")
    if not isinstance(receipt, Mapping):
        raise TaskFlowContractError("runner result is missing its backend receipt")
    stdout = receipt.get("stdout", "")
    if not isinstance(stdout, str) or not stdout.strip():
        return {}
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        raise TaskFlowContractError("structured runner stdout must be one object")
    return value


def _enqueue_outgoing_messages(
    conf: Mapping[str, Any],
    runner_result: Mapping[str, Any],
) -> list[str]:
    output = _runner_output(runner_result)
    outgoing_messages = output.get("outgoing_messages", [])
    if not isinstance(outgoing_messages, list):
        raise TaskFlowContractError("outgoing_messages must be an array")
    if not outgoing_messages:
        return []
    queue = queue_with_wake_hook(str(conf["db_path"]))
    message_ids: list[str] = []
    for index, outgoing in enumerate(outgoing_messages):
        if not isinstance(outgoing, Mapping):
            raise TaskFlowContractError(
                f"outgoing_messages[{index}] must be an object"
            )
        required = {"to_role", "type", "payload"}
        missing = required - set(outgoing)
        if missing or not isinstance(outgoing["payload"], Mapping):
            raise TaskFlowContractError(
                f"outgoing_messages[{index}] is missing a valid "
                f"{sorted(missing or {'payload'})}"
            )
        payload = dict(outgoing["payload"])
        payload.setdefault("goal_id", conf["goal_id"])
        payload.setdefault("iteration", conf["revision"])
        payload.setdefault("repo_id", conf["repo_id"])
        payload.setdefault("base_oid", conf["base_oid"])
        payload.setdefault("head_oid", conf["head_oid"])
        dedupe_key = str(
            outgoing.get(
                "dedupe_key",
                f"{conf['idempotency_key']}:message:{index}",
            )
        )
        message = queue.enqueue(
            thread_id=conf["thread_id"],
            work_item_id=conf["work_item_id"],
            parent_message_id=outgoing.get("parent_message_id"),
            from_role=conf["actor_role"],
            to_role=str(outgoing["to_role"]),
            message_type=str(outgoing["type"]),
            payload=payload,
            priority=int(outgoing.get("priority", 0)),
            max_attempts=int(outgoing.get("max_attempts", 5)),
            dedupe_key=dedupe_key,
        )
        message_ids.append(message.id)
    return message_ids


def persist_agent_result(
    conf: Mapping[str, Any],
    execution: Mapping[str, Any],
    integrity: Mapping[str, Any],
    services: RuntimeTaskServices,
    contract_control: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sealed = assert_immutable_conf(conf)
    controller = _require_contract_controller(services.contract_controller)
    admitted = _require_accepted_contract_control(
        contract_control,
        activation_id=sealed["activation_id"],
    )
    attempt_id = execution.get("contract_attempt_id")
    if not isinstance(attempt_id, str) or not _IDENTIFIER_RE.fullmatch(
        attempt_id
    ):
        raise TaskFlowContractError(
            "contract attempt id is required for deterministic persistence"
        )
    runner_result = execution.get("runner_result")
    if not isinstance(runner_result, Mapping):
        raise TaskFlowContractError("runner result mapping is required")
    if integrity.get("stage") != "integrity_verify":
        raise TaskFlowContractError("integrity receipt is required")
    contract_result = controller.commit_runner_result(
        contract=admitted["contract"],
        admission=admitted["admission"],
        attempt_id=attempt_id,
        runner_result=dict(runner_result),
        integrity=dict(integrity),
    )
    activation_result_id = getattr(
        contract_result, "activation_result_id", None
    )
    message_ids = getattr(contract_result, "message_ids", None)
    outbox_ids = getattr(contract_result, "outbox_ids", None)
    if (
        not isinstance(activation_result_id, str)
        or not isinstance(message_ids, tuple)
        or not all(isinstance(item, str) for item in message_ids)
        or not isinstance(outbox_ids, tuple)
        or not all(isinstance(item, str) for item in outbox_ids)
    ):
        raise TaskFlowContractError(
            "canonical result commit returned no durable result/outbox identifiers"
        )
    return {
        "stage": "deterministic_persistence",
        "activation_id": sealed["activation_id"],
        "status": runner_result["status"],
        "result_id": runner_result["result_id"],
        "result_artifact": runner_result["artifact_ref"],
        "activation_result_id": activation_result_id,
        "message_ids": list(message_ids),
        "outbox_ids": list(outbox_ids),
        "classification": integrity["classification"],
        "gate_evidence_eligible": integrity["gate_evidence_eligible"],
        "clean_rerun_required": integrity["clean_rerun_required"],
        "contract_result": contract_result,
    }


def revoke_release_terminate(
    conf: Mapping[str, Any],
    persistence: Mapping[str, Any],
    services: RuntimeTaskServices,
) -> dict[str, Any]:
    sealed = assert_immutable_conf(conf)
    if persistence.get("stage") != "deterministic_persistence":
        raise TaskFlowContractError("durable persistence receipt is required")
    if persistence["classification"] == SourceIntegrity.INVALIDATED.value:
        # Preserve the quarantined sandbox and exact failed OID for recovery.
        return {
            "stage": "revoke_release_terminate",
            "activation_id": sealed["activation_id"],
            "state": "QUARANTINED",
            "resource_preserved": True,
        }
    record = services.activation_manager.revoke_and_terminate(
        sealed["activation_id"]
    )
    state = getattr(record.state, "value", str(record.state))
    return {
        "stage": "revoke_release_terminate",
        "activation_id": sealed["activation_id"],
        "state": state,
        "resource_preserved": state in {"QUARANTINED", "REVOKE_FAILED"},
    }


def run_module_iteration(
    conf: Mapping[str, Any],
    services: RuntimeTaskServices,
) -> dict[str, Any]:
    """Synchronous entrypoint used by tests and non-Airflow execution."""

    sealed = validate_runtime_conf(conf)
    allocation = allocate_and_bind_activation(sealed, services)
    profile = resolve_compile_profile(sealed, allocation, services)
    contract_control = compile_admit_contract(
        sealed, allocation, profile, services
    )
    if not contract_control["accepted"]:
        return {
            "stage_order": list(TASKFLOW_STAGE_ORDER),
            "conf_digest": sealed[_CONF_DIGEST_FIELD],
            "allocation": allocation,
            "profile": profile,
            "contract": contract_control,
            "context": None,
            "execution": None,
            "integrity": None,
            "persistence": None,
            "cleanup": None,
        }
    try:
        context_artifact = compile_context_to_artifact(
            sealed, allocation, profile, services, contract_control
        )
    except Exception as exc:
        services.activation_manager.quarantine(
            sealed["activation_id"],
            f"bounded context failed: {type(exc).__name__}: {exc}",
        )
        raise
    execution = execute_agent_runner(
        sealed, allocation, profile, context_artifact, services, contract_control
    )
    integrity = verify_execution_integrity(
        sealed, allocation, execution, services
    )
    persistence = persist_agent_result(
        sealed, execution, integrity, services, contract_control
    )
    cleanup = revoke_release_terminate(sealed, persistence, services)
    return {
        "stage_order": list(TASKFLOW_STAGE_ORDER),
        "conf_digest": sealed[_CONF_DIGEST_FIELD],
        "allocation": allocation,
        "profile": profile,
        "contract": contract_control,
        "context": context_artifact,
        "execution": execution,
        "integrity": integrity,
        "persistence": persistence,
        "cleanup": cleanup,
    }


_RUNTIME_SERVICES_FACTORY: (
    Callable[[Mapping[str, Any]], RuntimeTaskServices] | None
) = None


def configure_runtime_services(
    factory: Callable[[Mapping[str, Any]], RuntimeTaskServices],
) -> None:
    """Register deployment wiring without importing Airflow in local tooling."""

    global _RUNTIME_SERVICES_FACTORY
    if not callable(factory):
        raise TypeError("runtime services factory must be callable")
    _RUNTIME_SERVICES_FACTORY = factory


def configure_canonical_runtime_services(
    *,
    state_store: AxStateStore,
    workspace_manager: Any,
    review_materializer: Any,
    activation_manager: Any,
    profile_resolver: Any,
    profile_compiler: Any,
    context_compiler: ContextCompiler,
    seat_policy_provider: Any,
    runtime: AgentRuntime,
) -> RuntimeTaskServices:
    """Install the production TaskFlow factory over one canonical state store."""

    if not isinstance(state_store, AxStateStore):
        raise TypeError("state_store must be an AxStateStore")
    required_components = {
        "workspace_manager": workspace_manager,
        "review_materializer": review_materializer,
        "activation_manager": activation_manager,
        "profile_resolver": profile_resolver,
        "profile_compiler": profile_compiler,
        "context_compiler": context_compiler,
        "seat_policy_provider": seat_policy_provider,
        "runtime": runtime,
    }
    missing = [name for name, component in required_components.items() if component is None]
    if missing:
        raise TaskFlowContractError(
            "canonical runtime services are missing: " + ", ".join(missing)
        )
    if not isinstance(runtime, AgentRuntime):
        raise TypeError("runtime must be an AgentRuntime")
    capabilities = runtime.backend_capabilities
    if not capabilities.proves_production_confinement:
        raise TaskFlowContractError(
            "canonical runtime backend lacks production confinement evidence"
        )
    for name, component in (
        ("workspace_manager", workspace_manager),
        ("review_materializer", review_materializer),
        ("activation_manager", activation_manager),
        ("profile_compiler", profile_compiler),
    ):
        if getattr(component, "state_store", None) is not state_store:
            raise TaskFlowContractError(
                f"{name} is not bound to the canonical AxStateStore"
            )
    if runtime.state_store is not state_store:
        raise TaskFlowContractError(
            "runtime is not bound to the canonical AxStateStore"
        )
    if (
        runtime.mcp_broker is None
        or runtime.mcp_broker.state_store is not state_store
    ):
        raise TaskFlowContractError(
            "runtime MCP broker is not bound to the canonical AxStateStore"
        )
    queue_store = getattr(getattr(context_compiler, "queue", None), "state_store", None)
    if queue_store is not state_store:
        raise TaskFlowContractError(
            "context compiler queue uses another state database"
        )
    provider = AxStateStoreCanonicalTaskFlowInputProvider(
        state_store,
        backend_id=capabilities.backend_id,
    )
    services = RuntimeTaskServices(
        workspace_manager=workspace_manager,
        review_materializer=review_materializer,
        activation_manager=activation_manager,
        profile_resolver=profile_resolver,
        profile_compiler=profile_compiler,
        context_compiler=context_compiler,
        seat_policy_provider=seat_policy_provider,
        runtime=runtime,
        contract_controller=CanonicalTaskFlowContractController(provider),
    )

    def canonical_factory(conf: Mapping[str, Any]) -> RuntimeTaskServices:
        if Path(str(conf.get("db_path", ""))).resolve() != state_store.db_path:
            raise TaskFlowContractError(
                "DagRun db_path differs from registered canonical runtime services"
            )
        sandbox_selector = conf.get("sandbox_binding_id")
        lease_selector = conf.get("runtime_lease_id")
        if sandbox_selector is not None or lease_selector is not None:
            query = """
                SELECT sb.backend
                FROM sandbox_bindings AS sb
                JOIN runtime_leases AS rl ON rl.id = sb.lease_id
                WHERE sb.run_id = ?
            """
            parameters: list[Any] = [conf.get("run_id")]
            if sandbox_selector is not None:
                query += " AND sb.id = ?"
                parameters.append(sandbox_selector)
            if lease_selector is not None:
                query += " AND rl.id = ?"
                parameters.append(lease_selector)
            with state_store.transaction() as connection:
                rows = connection.execute(query, tuple(parameters)).fetchall()
            if len(rows) != 1 or rows[0]["backend"] != capabilities.backend_id:
                raise TaskFlowContractError(
                    "DagRun sandbox backend differs from the registered runtime backend"
                )
        return services

    configure_runtime_services(canonical_factory)
    return services


def _runtime_services(conf: Mapping[str, Any]) -> RuntimeTaskServices:
    if _RUNTIME_SERVICES_FACTORY is None:
        raise RuntimeError(
            "Agent-Team runtime services are not configured in this Airflow deployment"
        )
    services = _RUNTIME_SERVICES_FACTORY(conf)
    if not isinstance(services, RuntimeTaskServices):
        raise TypeError("runtime services factory returned the wrong type")
    _require_contract_controller(services.contract_controller)
    return services


if AIRFLOW_IMPORT_ERROR is None:

    @dag(
        dag_id="agent_team_module_iteration",
        schedule=None,
        catchup=False,
        tags=["agent-team", "module-loop", "worktree-runtime"],
    )
    def module_iteration_flow():
        @task(task_id="immutable_conf")
        def immutable_conf_task() -> dict[str, Any]:
            context = get_current_context()
            dag_run = context.get("dag_run")
            if dag_run is None:
                raise RuntimeError("module iteration requires a DagRun")
            return validate_runtime_conf(dag_run.conf or {})

        @task(task_id="allocate_bind_activation")
        def allocate_bind_activation_task(conf: dict[str, Any]) -> dict[str, Any]:
            return allocate_and_bind_activation(conf, _runtime_services(conf))

        @task(task_id="resolve_compile_profile")
        def resolve_compile_profile_task(
            conf: dict[str, Any],
            allocation: dict[str, Any],
        ) -> dict[str, Any]:
            return resolve_compile_profile(
                conf, allocation, _runtime_services(conf)
            )

        @task(task_id="bounded_context")
        def bounded_context_task(
            conf: dict[str, Any],
            allocation: dict[str, Any],
            profile: dict[str, Any],
            contract_control: dict[str, Any],
        ) -> dict[str, Any]:
            return compile_context_to_artifact(
                conf, allocation, profile, _runtime_services(conf), contract_control
            )

        @task(task_id="compile_admit_contract")
        def compile_admit_contract_task(
            conf: dict[str, Any],
            allocation: dict[str, Any],
            profile: dict[str, Any],
        ) -> dict[str, Any]:
            return compile_admit_contract(
                conf, allocation, profile, _runtime_services(conf)
            )

        @task(task_id="confined_execute", retries=2)
        def confined_execute_task(
            conf: dict[str, Any],
            allocation: dict[str, Any],
            profile: dict[str, Any],
            context_artifact: dict[str, Any],
            contract_control: dict[str, Any],
        ) -> dict[str, Any]:
            return execute_agent_runner(
                conf,
                allocation,
                profile,
                context_artifact,
                _runtime_services(conf),
                contract_control,
            )

        @task(task_id="integrity_verify")
        def integrity_verify_task(
            conf: dict[str, Any],
            allocation: dict[str, Any],
            execution: dict[str, Any],
        ) -> dict[str, Any]:
            return verify_execution_integrity(
                conf, allocation, execution, _runtime_services(conf)
            )

        @task(task_id="deterministic_persistence")
        def deterministic_persistence_task(
            conf: dict[str, Any],
            execution: dict[str, Any],
            integrity: dict[str, Any],
            contract_control: dict[str, Any],
        ) -> dict[str, Any]:
            return persist_agent_result(
                conf,
                execution,
                integrity,
                _runtime_services(conf),
                contract_control,
            )

        @task(task_id="revoke_release_terminate")
        def revoke_release_terminate_task(
            conf: dict[str, Any],
            persistence: dict[str, Any],
        ) -> dict[str, Any]:
            return revoke_release_terminate(
                conf, persistence, _runtime_services(conf)
            )

        immutable = immutable_conf_task()
        allocation = allocate_bind_activation_task(immutable)
        profile = resolve_compile_profile_task(immutable, allocation)
        contract_control = compile_admit_contract_task(
            immutable, allocation, profile
        )
        context_artifact = bounded_context_task(
            immutable, allocation, profile, contract_control
        )
        execution = confined_execute_task(
            immutable,
            allocation,
            profile,
            context_artifact,
            contract_control,
        )
        integrity = integrity_verify_task(
            immutable, allocation, execution
        )
        persistence = deterministic_persistence_task(
            immutable, execution, integrity, contract_control
        )
        revoke_release_terminate_task(immutable, persistence)

    module_iteration_dag = module_iteration_flow()
else:
    module_iteration_dag = None


def main() -> int:
    if AIRFLOW_IMPORT_ERROR is not None:
        print(
            "Airflow TaskFlow is unavailable in this Python environment: "
            f"{AIRFLOW_IMPORT_ERROR}. Runtime contracts remain importable and "
            "testable without a scheduler.",
            file=sys.stderr,
        )
        return 2
    print("Airflow TaskFlow DAG loaded: agent_team_module_iteration")
    return 0


__all__ = [
    "AIRFLOW_IMPORT_ERROR",
    "AxStateStoreCanonicalTaskFlowInputProvider",
    "CanonicalTaskFlowAttemptInputs",
    "CanonicalTaskFlowContractController",
    "CanonicalTaskFlowContractInputs",
    "CanonicalTaskFlowInputProvider",
    "DeterministicServiceCommand",
    "DeterministicServiceDispatcher",
    "RuntimeTaskServices",
    "TASKFLOW_STAGE_ORDER",
    "TaskFlowContractError",
    "TaskFlowContractController",
    "TaskFlowIntegrityError",
    "allocate_and_bind_activation",
    "assert_immutable_conf",
    "compile_context_to_artifact",
    "compile_admit_contract",
    "configure_canonical_runtime_services",
    "configure_runtime_services",
    "execute_agent_runner",
    "module_iteration_dag",
    "persist_agent_result",
    "resolve_compile_profile",
    "revoke_release_terminate",
    "run_module_iteration",
    "validate_runtime_conf",
    "verify_execution_integrity",
]


if __name__ == "__main__":
    raise SystemExit(main())
