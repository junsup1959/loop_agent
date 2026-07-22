"""Transition contract compiler, admission gate, renderer, and result control.

This module extends the existing SQLite v4 control plane.  It deliberately has
no scheduler and no private state database: when a state store is supplied, all
contract, admission, attempt, result, violation, MCP, token, and circuit facts
are written through :class:`agent_team_state.AxStateStore`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - project validation installs jsonschema
    Draft202012Validator = None

try:
    from .agent_team_domain import (
        ContractAttemptKind,
        ContractViolationCode,
        McpHealthStatus,
    )
    from .agent_team_state import AxStateStore, IntentStateError, stable_identifier
    from .serena_project_knowledge import (
        ProjectKnowledgeError,
        required_memories_for_transition,
    )
    from .agent_team_workflow import (
        ActorBinding,
        ClauseBinding,
        EvidenceSet,
        TransitionDefinition,
        WorkflowDefinitionError,
        WorkflowDefinitions,
        WorkflowInstance,
        canonical_json,
        coerce_transition,
        coerce_workflow_instance,
        sha256_bytes,
        sha256_json,
        sha256_text,
    )
except ImportError:  # pragma: no cover - direct script import
    from agent_team_domain import (
        ContractAttemptKind,
        ContractViolationCode,
        McpHealthStatus,
    )
    from agent_team_state import AxStateStore, IntentStateError, stable_identifier
    from serena_project_knowledge import (
        ProjectKnowledgeError,
        required_memories_for_transition,
    )
    from agent_team_workflow import (
        ActorBinding,
        ClauseBinding,
        EvidenceSet,
        TransitionDefinition,
        WorkflowDefinitionError,
        WorkflowDefinitions,
        WorkflowInstance,
        canonical_json,
        coerce_transition,
        coerce_workflow_instance,
        sha256_bytes,
        sha256_json,
        sha256_text,
    )


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")
_TEMPLATE_FIELD_RE = re.compile(r"\{\{([a-z0-9_]+)\}\}")
_QUARANTINE_CATEGORIES = frozenset(
    {"authority", "oid", "write_scope", "nested_spawn"}
)
_REWORK_RESULT_KINDS = frozenset(
    {
        "needs_rework",
        "merge_conflict",
        "broken_integration",
        "need_more_context",
        "blocked",
    }
)


class ContractCompilationError(ValueError):
    """A transition cannot be compiled from the supplied immutable facts."""


class ContractAdmissionError(RuntimeError):
    """An admitted activation invariant was violated."""


@dataclass(frozen=True, slots=True)
class RenderedPacket:
    contract_ref: str
    packet_ref: str
    contract_json: str
    markdown: str
    contract_sha256: str
    packet_sha256: str
    character_count: int
    combined_character_count: int

    def write(self) -> tuple[Path, Path]:
        contract_path = Path(self.contract_ref).expanduser()
        packet_path = Path(self.packet_ref).expanduser()
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(self.contract_json + "\n", encoding="utf-8", newline="\n")
        packet_path.write_text(self.markdown, encoding="utf-8", newline="\n")
        return contract_path.resolve(), packet_path.resolve()


@dataclass(frozen=True, slots=True)
class ActivationContract:
    document: Mapping[str, Any]
    clauses: tuple[ClauseBinding, ...]
    rendered_packet: RenderedPacket
    transition: TransitionDefinition
    actor: ActorBinding
    evidence: EvidenceSet
    workflow_instance: WorkflowInstance
    definitions: WorkflowDefinitions = field(repr=False, compare=False)
    state_store: AxStateStore = field(repr=False, compare=False)
    database_bindings: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.state_store, AxStateStore):
            raise ContractCompilationError(
                "ActivationContract requires one durable AxStateStore"
            )

    @property
    def contract_id(self) -> str:
        return str(self.document["contract_id"])

    @property
    def activation_id(self) -> str:
        return str(self.document["activation_id"])

    @property
    def digest(self) -> str:
        return self.rendered_packet.contract_sha256

    @property
    def effective_budget(self) -> int:
        return int(self.document["budget"]["effective_limit_chars"])

    def as_dict(self) -> dict[str, Any]:
        return json.loads(canonical_json(self.document))


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    receipt_id: str
    contract_id: str
    contract_sha256: str
    accepted: bool
    reason_code: str | None
    model_call_permitted: bool
    checks: Mapping[str, bool]
    violations: tuple[str, ...]
    idempotency_key: str
    recorded_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 4,
            "receipt_id": self.receipt_id,
            "contract_id": self.contract_id,
            "contract_sha256": self.contract_sha256,
            "accepted": self.accepted,
            "reason_code": self.reason_code,
            "model_call_permitted": self.model_call_permitted,
            "checks": dict(self.checks),
            "violations": list(self.violations),
            "idempotency_key": self.idempotency_key,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class ContractViolation:
    violation_id: str
    contract_id: str | None
    activation_id: str | None
    worker_fingerprint: str | None
    category: str
    reason_code: str
    action: str
    backend_call_recorded: bool
    evidence_refs: tuple[str, ...]
    idempotency_key: str
    recorded_at: str
    format_violation_ordinal: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 4,
            "violation_id": self.violation_id,
            "contract_id": self.contract_id,
            "activation_id": self.activation_id,
            "worker_fingerprint": self.worker_fingerprint,
            "category": self.category,
            "reason_code": self.reason_code,
            "action": self.action,
            "backend_call_recorded": self.backend_call_recorded,
            "format_violation_ordinal": self.format_violation_ordinal,
            "evidence_refs": list(self.evidence_refs),
            "idempotency_key": self.idempotency_key,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class ActivationResult:
    contract: ActivationContract
    payload: Mapping[str, Any]
    attempt_id: str
    format_error_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ContractAdmissionError("ActivationResult requires a durable attempt_id")


@dataclass(frozen=True, slots=True)
class TransitionReceipt:
    receipt_id: str
    contract_id: str
    transition_id: str
    from_state: str
    to_state: str
    subject_oid: str
    result_oid: str | None
    evidence_digest: str
    activation_result_id: str = ""
    message_ids: tuple[str, ...] = ()
    outbox_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReworkRoute:
    route_id: str
    contract_id: str
    transition_id: str
    owner_capability: str
    failure_state: str
    subject_oid: str
    failure_oid: str | None
    reason_code: str
    direct_source_repair_allowed: bool = False
    attempt_kind: str | None = None
    activation_result_id: str = ""
    message_ids: tuple[str, ...] = ()
    outbox_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Quarantine:
    quarantine_id: str
    contract_id: str
    category: str
    reason_code: str
    subject_oid: str
    worker_fingerprint: str
    activation_result_id: str = ""
    message_ids: tuple[str, ...] = ()
    outbox_ids: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _coerce_actor(value: ActorBinding | Mapping[str, Any]) -> ActorBinding:
    if isinstance(value, ActorBinding):
        return value
    if not isinstance(value, Mapping):
        raise ContractCompilationError("actor must be ActorBinding or mapping")
    normalized = dict(value)
    for field_name in ("profile_reference_sha256s", "selected_skills"):
        if field_name in normalized and isinstance(normalized[field_name], list):
            normalized[field_name] = tuple(normalized[field_name])
    return ActorBinding(**normalized)


def _coerce_evidence(value: EvidenceSet | Mapping[str, Any]) -> EvidenceSet:
    if isinstance(value, EvidenceSet):
        return value
    if not isinstance(value, Mapping):
        raise ContractCompilationError("evidence must be EvidenceSet or mapping")
    normalized = dict(value)
    repository = normalized.get("repository")
    if isinstance(repository, Mapping):
        try:
            from .agent_team_workflow import RepositoryBinding
        except ImportError:  # pragma: no cover
            from agent_team_workflow import RepositoryBinding
        normalized["repository"] = RepositoryBinding(**dict(repository))
    for field_name in ("mcp_trigger_ids", "evidence_refs"):
        if field_name in normalized and isinstance(normalized[field_name], list):
            normalized[field_name] = tuple(normalized[field_name])
    return EvidenceSet(**normalized)


def _load_json_schema(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractCompilationError(f"Invalid JSON Schema {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("additionalProperties") is not False:
        raise ContractCompilationError(f"JSON Schema is not strict: {path}")
    return value


def _validate_schema(document: Mapping[str, Any], schema_path: Path) -> None:
    schema = _load_json_schema(schema_path)
    if Draft202012Validator is None:
        missing = set(schema.get("required", [])) - set(document)
        unknown = set(document) - set(schema.get("properties", {}))
        if missing or unknown:
            raise ContractCompilationError(
                f"schema field mismatch: missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        return
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(document)),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "$"
        raise ContractCompilationError(f"schema violation at {location}: {first.message}")


def _resolve_path(value: str, *, skill: bool = False) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        root = WorkflowDefinitions.load().template_path.parents[2]
        candidate = root / ("skills" if skill else "") / candidate
    return candidate.resolve()


def _profile_binding(actor: ActorBinding) -> dict[str, Any]:
    if not actor.compiled_profile_ref or not _SHA256_RE.fullmatch(
        actor.compiled_profile_sha256
    ):
        raise ContractCompilationError("one compiled professional profile is required")
    profile_path = Path(actor.compiled_profile_ref).expanduser().resolve()
    if not profile_path.is_file():
        raise ContractCompilationError(f"compiled profile is missing: {profile_path}")
    if sha256_bytes(profile_path.read_bytes()) != actor.compiled_profile_sha256:
        raise ContractCompilationError("compiled professional profile digest changed")
    references = tuple(actor.profile_reference_sha256s)
    if not 4 <= len(references) <= 5 or len(references) != len(set(references)):
        raise ContractCompilationError("compiled profile requires four or five unique references")
    if not all(_SHA256_RE.fullmatch(item) for item in references):
        raise ContractCompilationError("profile reference digest is invalid")
    return {
        "skill_id": "professional-profile-runtime",
        "compiled_profile_ref": str(profile_path),
        "compiled_profile_sha256": actor.compiled_profile_sha256,
        "reference_sha256s": list(references),
    }


def _skill_bindings(actor: ActorBinding, capability_id: str) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    professional_count = 0
    seen: set[str] = set()
    for raw in actor.selected_skills:
        if not isinstance(raw, Mapping):
            raise ContractCompilationError("selected Skill descriptor must be an object")
        skill = dict(raw)
        required = {
            "id",
            "version",
            "path",
            "sha256",
            "content_budget_chars",
            "mcp_prerequisites",
        }
        if required - set(skill):
            raise ContractCompilationError("selected Skill descriptor is incomplete")
        skill_id = str(skill["id"])
        if skill_id in seen:
            raise ContractCompilationError("selected Skills must be unique")
        seen.add(skill_id)
        if skill_id == "professional-profile-runtime":
            professional_count += 1
        eligible = skill.get("eligible_capabilities")
        if eligible is not None and capability_id not in eligible:
            raise ContractCompilationError(
                f"Skill {skill_id} is not eligible for {capability_id}"
            )
        path = _resolve_path(str(skill["path"]), skill=True)
        if not path.is_file() or sha256_bytes(path.read_bytes()) != skill["sha256"]:
            raise ContractCompilationError(f"Skill bytes changed: {skill_id}")
        selected.append(
            {
                "id": skill_id,
                "version": str(skill["version"]),
                "path": str(skill["path"]),
                "sha256": str(skill["sha256"]),
                "content_budget_chars": int(skill["content_budget_chars"]),
                "mcp_prerequisites": list(skill["mcp_prerequisites"]),
            }
        )
    if professional_count != 1:
        raise ContractCompilationError(
            "every activation must select exactly one professional-profile-runtime Skill"
        )
    return selected


def _workspace_binding(evidence: EvidenceSet, kind: str) -> dict[str, Any]:
    raw = dict(evidence.workspace)
    required = {
        "workspace_id",
        "lease_id",
        "sandbox_id",
        "cwd",
        "source_roots",
        "writable_roots",
        "protected_roots",
        "prohibited_roots",
    }
    missing = required - set(raw)
    if missing:
        raise ContractCompilationError(f"workspace binding is missing: {sorted(missing)}")
    if raw["lease_id"] != evidence.lease_id:
        raise ContractCompilationError("workspace lease does not match evidence")
    for key in ("source_roots", "writable_roots", "protected_roots", "prohibited_roots"):
        values = raw[key]
        if not isinstance(values, (list, tuple)) or len(values) != len(set(values)):
            raise ContractCompilationError(f"workspace {key} must be a unique path array")
        raw[key] = [str(item) for item in values]
    authority = {"kind": kind, **{key: raw[key] for key in sorted(required)}}
    observed = raw.get("binding_sha256")
    computed = sha256_json(authority)
    if observed is not None and observed != computed:
        raise ContractCompilationError("workspace binding digest changed")
    return {"kind": kind, **{key: raw[key] for key in required}, "binding_sha256": computed}


def _resolve_mcp_bindings(
    definitions: WorkflowDefinitions,
    transition: TransitionDefinition,
    capability_id: str,
) -> tuple[list[dict[str, Any]], str]:
    policy = definitions.mcp_policy
    metadata = policy.get("policy")
    raw_bindings = policy.get("required_use_bindings")
    if not isinstance(metadata, Mapping) or not isinstance(raw_bindings, list):
        raise ContractCompilationError("MCP policy definition is invalid")
    if metadata.get("enabled") is not True or metadata.get("fallback_allowed") is not False:
        raise ContractCompilationError("required MCP policy was downgraded")
    policy_sha = sha256_json(policy)
    index = {
        item.get("id"): item
        for item in raw_bindings
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    output: list[dict[str, Any]] = []
    for binding_id in transition.mcp_availability_binding_ids:
        raw = index.get(binding_id)
        if raw is None:
            raise ContractCompilationError(f"unknown MCP availability binding: {binding_id}")
        if capability_id not in raw.get("capabilities", []):
            raise ContractCompilationError("MCP availability binding exceeds capability")
        for server_id in raw.get("server_ids", []):
            output.append(
                {
                    "binding_id": binding_id,
                    "server_id": server_id,
                    "tool_ids": [],
                    "required_use": False,
                    "usage_receipt_required": False,
                    "policy_sha256": policy_sha,
                }
            )
    for binding_id in transition.mcp_required_use_binding_ids:
        raw = index.get(binding_id)
        if raw is None:
            raise ContractCompilationError(f"unknown MCP required-use binding: {binding_id}")
        if capability_id not in raw.get("capabilities", []):
            raise ContractCompilationError("MCP required-use binding exceeds capability")
        if transition.transition_id not in raw.get("transition_ids", []):
            raise ContractCompilationError("MCP required-use binding exceeds transition")
        tools = list(raw.get("tool_ids", []))
        for server_id in raw.get("server_ids", []):
            output.append(
                {
                    "binding_id": binding_id,
                    "server_id": server_id,
                    "tool_ids": tools,
                    "required_use": True,
                    "usage_receipt_required": raw.get("usage_receipt_required") is True,
                    "policy_sha256": policy_sha,
                }
            )
    return output, policy_sha


def _serena_binding(
    snapshot: Any,
    *,
    consumption_required: bool,
    expected_names: Sequence[str],
    subject_oid: str,
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    if hasattr(snapshot, "as_contract_binding"):
        value = snapshot.as_contract_binding(consumption_required=consumption_required)
    elif isinstance(snapshot, Mapping):
        value = dict(snapshot)
    else:
        value = {
            key: getattr(snapshot, key)
            for key in (
                "snapshot_id",
                "source_oid",
                "policy_sha256",
                "memory_bindings",
            )
            if hasattr(snapshot, key)
        }
    snapshot_id = value.get("snapshot_id")
    source_oid = value.get("source_oid")
    policy_sha256 = value.get("policy_sha256")
    raw_bindings = value.get("memory_bindings")
    if (
        not isinstance(snapshot_id, str)
        or not snapshot_id
        or source_oid != subject_oid
        or not isinstance(policy_sha256, str)
        or not _SHA256_RE.fullmatch(policy_sha256)
        or not isinstance(raw_bindings, (list, tuple))
    ):
        raise ContractCompilationError(
            "Serena onboarding snapshot identity or source OID is invalid"
        )
    bindings: list[dict[str, str]] = []
    for raw in raw_bindings:
        if isinstance(raw, Mapping):
            name = raw.get("name") or raw.get("memory_name")
            digest = raw.get("sha256") or raw.get("memory_sha256")
        else:
            name = getattr(raw, "name", None)
            digest = getattr(raw, "sha256", None)
        if (
            not isinstance(name, str)
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise ContractCompilationError("Serena memory binding is invalid")
        bindings.append({"name": name, "sha256": digest})
    names = tuple(item["name"] for item in bindings)
    if names != tuple(expected_names) or len(names) != len(set(names)):
        raise ContractCompilationError(
            "Serena memories differ from the transition-specific minimum"
        )
    return {
        "snapshot_id": snapshot_id,
        "source_oid": source_oid,
        "policy_sha256": policy_sha256,
        "memory_bindings": bindings,
        "consumption_receipt_required": consumption_required,
    }


def _render_markdown(
    document: Mapping[str, Any],
    clauses: Sequence[ClauseBinding],
    template: str,
    *,
    contract_ref: str,
    contract_sha256: str,
) -> str:
    transition = document["transition"]
    identity = document["identity"]
    authority = document["authority"]
    workspace = document["workspace"]
    profile = document["professional_profile"]
    skills = document["skills"]
    mcp = document["mcp_bindings"]
    onboarding = document.get("serena_onboarding")
    clause_lines = [
        f"- `{item.clause_id}@{item.version}` `{item.sha256}` — {item.text}"
        for item in clauses
    ]
    skill_lines = [
        f"- `{item['id']}@{item['version']}` `{item['sha256']}` ({item['path']})"
        for item in skills
    ]
    mcp_lines = [
        f"- `{item['binding_id']}` / `{item['server_id']}`: "
        f"tools={','.join(item['tool_ids']) or 'availability-only'}; "
        f"required_use={str(item['required_use']).lower()}; "
        f"receipt={str(item['usage_receipt_required']).lower()}"
        for item in mcp
    ]
    evidence_lines = [
        f"Evidence refs: {', '.join(document.get('_evidence_refs', [])) or 'none'}"
    ]
    if onboarding:
        names = [item["name"] for item in onboarding.get("memory_bindings", [])]
        evidence_lines.append(f"Selected Serena memories: {', '.join(names) or 'none'}")
    values = {
        "contract_id": document["contract_id"],
        "contract_ref": contract_ref,
        "contract_sha256": contract_sha256,
        "workflow_id": document["workflow"]["id"],
        "workflow_version": document["workflow"]["version"],
        "transition_id": transition["id"],
        "from_state": transition["from_state"],
        "to_state": transition["to_state"] or transition["from_state"],
        "goal_id": document["goal_id"],
        "run_id": document["run_id"],
        "activation_id": document["activation_id"],
        "slot_key": identity["slot_key"],
        "actor_identity": identity["seat_id"] or identity["worker_id"],
        "capability_id": identity["capability_id"],
        "subject_oid": document["git"]["subject_oid"],
        "transition_summary": (
            f"Produce one of: {', '.join(document['_result_kinds'])}. "
            f"Failure route: {document['_failure_state']}."
        ),
        "approval_authorities": ", ".join(authority["approval_authorities"]) or "none",
        "merge_control": str(authority["merge_control"]).lower(),
        "nested_spawn_allowed": str(authority["nested_spawn_allowed"]).lower(),
        "workspace_binding_markdown": "```json\n" + canonical_json(workspace) + "\n```",
        "clauses_markdown": "\n".join(clause_lines),
        "profile_binding_markdown": (
            f"Profile: `{profile['compiled_profile_ref']}` "
            f"`{profile['compiled_profile_sha256']}`"
        ),
        "skill_bindings_markdown": "\n".join(skill_lines),
        "mcp_bindings_markdown": "\n".join(mcp_lines),
        "evidence_requirements_markdown": "\n".join(evidence_lines),
        "output_schema_ref": document["output_schema_ref"],
        "idempotency_key": document["idempotency_key"],
    }
    observed = set(_TEMPLATE_FIELD_RE.findall(template))
    if observed != set(values):
        raise ContractCompilationError(
            f"activation template fields differ: {sorted(observed ^ set(values))}"
        )
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    if _TEMPLATE_FIELD_RE.search(rendered):
        raise ContractCompilationError("activation template contains unresolved fields")
    return rendered.replace("\r\n", "\n").rstrip() + "\n"


def _public_document(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if not key.startswith("_")}


def _build_rendered_packet(
    document: dict[str, Any],
    clauses: Sequence[ClauseBinding],
    definitions: WorkflowDefinitions,
    evidence: EvidenceSet,
) -> tuple[dict[str, Any], RenderedPacket]:
    template = definitions.template_path.read_text(encoding="utf-8")
    artifact_root = Path(evidence.artifact_root).expanduser() if evidence.artifact_root else None
    logical_root = artifact_root or Path("activations")
    contract_ref = evidence.contract_ref or str(
        logical_root / document["activation_id"] / "activation-contract.json"
    )
    packet_ref = str(logical_root / document["activation_id"] / "activation-packet.md")
    clause_chars = sum(item.character_count for item in clauses)
    for _ in range(4):
        public = _public_document(document)
        contract_json = canonical_json(public)
        contract_sha = sha256_text(contract_json)
        markdown = _render_markdown(
            document,
            clauses,
            template,
            contract_ref=contract_ref,
            contract_sha256=contract_sha,
        )
        combined = len(markdown) + clause_chars
        if document["budget"]["rendered_and_clause_chars"] == combined:
            break
        document["budget"]["rendered_and_clause_chars"] = combined
    public = _public_document(document)
    contract_json = canonical_json(public)
    contract_sha = sha256_text(contract_json)
    markdown = _render_markdown(
        document,
        clauses,
        template,
        contract_ref=contract_ref,
        contract_sha256=contract_sha,
    )
    combined = len(markdown) + clause_chars
    if combined > document["budget"]["effective_limit_chars"]:
        raise ContractCompilationError(
            f"rendered contract uses {combined} chars above "
            f"{document['budget']['effective_limit_chars']}"
        )
    packet = RenderedPacket(
        contract_ref=contract_ref,
        packet_ref=packet_ref,
        contract_json=contract_json,
        markdown=markdown,
        contract_sha256=contract_sha,
        packet_sha256=sha256_text(markdown),
        character_count=len(markdown),
        combined_character_count=combined,
    )
    return public, packet


def _database_bindings(
    store: AxStateStore,
    instance: WorkflowInstance,
    transition: TransitionDefinition,
) -> dict[str, Any]:
    with store.transaction() as connection:
        row = connection.execute(
            """
            SELECT workflow_instances.workflow_definition_id,
                   workflow_transitions.id AS transition_database_id,
                   workflow_transitions.capability_id,
                   workflow_transitions.output_schema_definition_id,
                   workflow_transitions.requires_serena_onboarding,
                   workflow_instances.current_state_key,
                   workflow_instances.status
            FROM workflow_instances
            JOIN workflow_transitions
              ON workflow_transitions.workflow_definition_id =
                 workflow_instances.workflow_definition_id
            WHERE workflow_instances.id = ?
              AND workflow_transitions.transition_key = ?
              AND workflow_transitions.state = 'ACTIVE'
            """,
            (instance.instance_id, transition.transition_id),
        ).fetchone()
    if row is None:
        raise ContractCompilationError("workflow transition is not registered for this instance")
    if row["current_state_key"] not in transition.from_states or row["status"] != "ACTIVE":
        raise ContractCompilationError("workflow instance is not in a legal active source state")
    return dict(row)


def compile_transition(
    instance: WorkflowInstance | Mapping[str, Any],
    transition: TransitionDefinition | Mapping[str, Any],
    actor: ActorBinding | Mapping[str, Any],
    evidence: EvidenceSet | Mapping[str, Any],
) -> ActivationContract:
    """Compile one legal delivery-v4 transition into immutable JSON and Markdown."""

    instance = coerce_workflow_instance(instance)
    transition = coerce_transition(transition)
    actor = _coerce_actor(actor)
    evidence = _coerce_evidence(evidence)
    definitions = WorkflowDefinitions.load()
    registered = definitions.transition(transition.transition_id)
    if registered.definition_sha256 != transition.definition_sha256:
        raise ContractCompilationError("transition definition digest changed")
    if instance.workflow_id != definitions.workflow["id"]:
        raise ContractCompilationError("workflow instance uses another workflow")
    if instance.workflow_version != definitions.workflow["version"]:
        raise ContractCompilationError("workflow instance version changed")
    if instance.workflow_sha256 != definitions.workflow_sha256:
        raise ContractCompilationError("workflow instance digest changed")
    if instance.status != "ACTIVE" or not transition.applies_to(
        instance.current_state, actor.capability_id
    ):
        raise ContractCompilationError("illegal workflow state or active capability")
    slot = definitions.slots.get(actor.slot_key)
    capability = definitions.capabilities.get(actor.capability_id)
    if slot is None or capability is None:
        raise ContractCompilationError("actor slot or capability is unknown")
    if actor.slot_type != slot["slot_type"] or actor.capability_id not in slot["capabilities"]:
        raise ContractCompilationError("actor capability is not eligible for its physical slot")
    if actor.slot_type == "fixed" and not actor.seat_id:
        raise ContractCompilationError("fixed activation requires one physical seat")
    if actor.slot_type == "elastic" and actor.seat_id is not None:
        raise ContractCompilationError("elastic activation cannot claim a physical seat")
    if not _SHA256_RE.fullmatch(actor.worker_fingerprint):
        raise ContractCompilationError("worker fingerprint is invalid")
    profile = _profile_binding(actor)
    skills = _skill_bindings(actor, actor.capability_id)
    workspace = _workspace_binding(evidence, transition.workspace_kind)
    mcp_bindings, _ = _resolve_mcp_bindings(definitions, transition, actor.capability_id)
    clauses = definitions.selected_clauses(transition)
    packet_limit = definitions.role_packet_limit(actor.capability_id)
    effective_limit = min(int(packet_limit * 0.25), 12_000)
    contract_id = stable_identifier(
        "contract",
        instance.instance_id,
        transition.transition_id,
        actor.activation_id,
        actor.worker_fingerprint,
        evidence.subject_oid,
        profile["compiled_profile_sha256"],
        [item["sha256"] for item in skills],
    )
    expected_serena_names: tuple[str, ...] = ()
    if (
        transition.serena_onboarding
        or transition.serena_consumption_receipt_required
    ):
        try:
            expected_serena_names = required_memories_for_transition(
                transition.transition_id
            )
        except ProjectKnowledgeError as exc:
            raise ContractCompilationError(str(exc)) from exc
    serena = _serena_binding(
        evidence.serena_snapshot,
        consumption_required=transition.serena_consumption_receipt_required,
        expected_names=expected_serena_names,
        subject_oid=evidence.subject_oid,
    )
    document: dict[str, Any] = {
        "schema_version": 4,
        "contract_id": contract_id,
        "activation_id": actor.activation_id,
        "target_id": instance.target_id,
        "goal_id": instance.goal_id,
        "run_id": instance.run_id,
        "workflow": {
            "id": instance.workflow_id,
            "version": instance.workflow_version,
            "sha256": instance.workflow_sha256,
        },
        "transition": {
            "id": transition.transition_id,
            "from_state": instance.current_state,
            "to_state": transition.to_state,
            "definition_sha256": transition.definition_sha256,
        },
        "identity": {
            "slot_key": actor.slot_key,
            "slot_type": actor.slot_type,
            "seat_id": actor.seat_id,
            "worker_id": actor.worker_id,
            "worker_fingerprint": actor.worker_fingerprint,
            "capability_id": actor.capability_id,
            "elastic_lease_id": actor.elastic_lease_id,
            "agent_definition_id": actor.agent_definition_id,
            "agent_definition_sha256": actor.agent_definition_sha256,
            "parent_activation_id": actor.parent_activation_id,
        },
        "authority": {
            "approval_authorities": list(capability["approval_authorities"]),
            "merge_control": capability["merge_control"] is True,
            "source_write": capability["source_write"] is True,
            "nested_spawn_allowed": False,
        },
        "git": {
            "base_oid": evidence.base_oid,
            "head_oid": evidence.head_oid,
            "subject_oid": evidence.subject_oid,
            "integration_oid": evidence.integration_oid,
            "failure_oid": evidence.failure_oid,
        },
        "workspace": workspace,
        "professional_profile": profile,
        "skills": skills,
        "mcp_bindings": mcp_bindings,
        "clauses": [item.contract_value() for item in clauses],
        "serena_onboarding": serena,
        "budget": {
            "role_packet_limit_chars": packet_limit,
            "ratio": 0.25,
            "ceiling_chars": 12_000,
            "effective_limit_chars": effective_limit,
            "rendered_and_clause_chars": 0,
        },
        "output_schema_ref": "agents/contracts/schemas/activation-result.schema.json",
        "idempotency_key": f"activation-contract:{contract_id}",
        "issued_at": evidence.issued_at,
        "_evidence_refs": list(evidence.evidence_refs),
        "_result_kinds": list(transition.result_kinds),
        "_failure_state": transition.failure_state,
    }
    public, packet = _build_rendered_packet(document, clauses, definitions, evidence)
    _validate_schema(public, definitions.activation_contract_schema_path)
    instance_store = instance.state_store
    repository_store = evidence.repository.state_store
    if not isinstance(instance_store, AxStateStore):
        raise ContractCompilationError(
            "workflow instance must bind one durable AxStateStore"
        )
    if not isinstance(repository_store, AxStateStore):
        raise ContractCompilationError(
            "repository evidence must bind one durable AxStateStore"
        )
    if instance_store is not repository_store:
        raise ContractCompilationError(
            "workflow instance and repository must share the same AxStateStore instance"
        )
    store = instance_store
    database = _database_bindings(store, instance, transition)
    required_database_bindings = {
        "workflow_definition_id",
        "transition_database_id",
        "capability_id",
        "output_schema_definition_id",
    }
    if not required_database_bindings <= set(database):
        raise ContractCompilationError(
            "v4 database relation bindings are incomplete"
        )
    return ActivationContract(
        document=MappingProxyType(public),
        clauses=clauses,
        rendered_packet=packet,
        transition=transition,
        actor=actor,
        evidence=evidence,
        workflow_instance=instance,
        definitions=definitions,
        state_store=store,
        database_bindings=database,
    )


def render(
    contract: ActivationContract,
    clauses: Sequence[ClauseBinding],
    template_version: str,
) -> RenderedPacket:
    if template_version != contract.definitions.template_version:
        raise ContractCompilationError("activation packet template version changed")
    expected = tuple(item.clause_id for item in contract.clauses)
    supplied = tuple(item.clause_id for item in clauses)
    if supplied != expected:
        raise ContractCompilationError("renderer clauses differ from the contract")
    public, packet = _build_rendered_packet(
        {**contract.as_dict(), "_evidence_refs": list(contract.evidence.evidence_refs),
         "_result_kinds": list(contract.transition.result_kinds),
         "_failure_state": contract.transition.failure_state},
        clauses,
        contract.definitions,
        contract.evidence,
    )
    if canonical_json(public) != canonical_json(contract.document):
        raise ContractCompilationError("renderer would change authoritative contract JSON")
    return packet


def _health_value(contract: ActivationContract, server_id: str) -> Mapping[str, Any] | None:
    raw = contract.evidence.mcp_health.get(server_id)
    return raw if isinstance(raw, Mapping) else None


def _admission_checks(contract: ActivationContract) -> tuple[dict[str, bool], list[str]]:
    checks = {
        "definition_digests": True,
        "authority": True,
        "oid": True,
        "workspace": True,
        "profile": True,
        "skills": True,
        "budget": True,
        "mcp_health": True,
        "mcp_tools": True,
        "serena_onboarding": True,
    }
    violations: list[str] = []
    if contract.digest != sha256_text(canonical_json(contract.document)):
        checks["definition_digests"] = False
        violations.append("contract-digest-mismatch")
    if contract.document["identity"]["capability_id"] not in contract.transition.capabilities:
        checks["authority"] = False
        violations.append("active-capability-mismatch")
    if contract.transition.exact_oid_required and not _OID_RE.fullmatch(
        contract.document["git"]["subject_oid"]
    ):
        checks["oid"] = False
        violations.append("exact-subject-oid-required")
    if contract.rendered_packet.combined_character_count > contract.effective_budget:
        checks["budget"] = False
        violations.append("contract-budget-exceeded")
    for binding in contract.document["mcp_bindings"]:
        health = _health_value(contract, binding["server_id"])
        if health is None or str(health.get("status", "")).upper() != "HEALTHY":
            checks["mcp_health"] = False
            violations.append(f"mcp-unhealthy-{binding['server_id']}")
            continue
        tools = set(health.get("tools", []))
        missing = set(binding["tool_ids"]) - tools
        if missing:
            checks["mcp_tools"] = False
            violations.append(
                f"mcp-tools-missing-{binding['server_id']}-{'-'.join(sorted(missing))}"
            )
    if (
        contract.transition.serena_onboarding
        or contract.transition.serena_consumption_receipt_required
    ) and contract.document.get("serena_onboarding") is None:
        checks["serena_onboarding"] = False
        violations.append("serena-onboarding-binding-missing")
    onboarding = contract.document.get("serena_onboarding")
    if onboarding is not None and (
        onboarding.get("source_oid") != contract.evidence.subject_oid
        or onboarding.get("consumption_receipt_required")
        is not contract.transition.serena_consumption_receipt_required
    ):
        checks["serena_onboarding"] = False
        violations.append("serena-onboarding-binding-changed")
    return checks, list(dict.fromkeys(violations))


def _register_contract_graph(contract: ActivationContract) -> None:
    store = contract.state_store
    database = contract.database_bindings
    if not database:
        raise ContractAdmissionError("v4 database relation bindings are missing")
    contract_schema_digest = sha256_bytes(
        contract.definitions.activation_contract_schema_path.read_bytes()
    )
    contract_definition_id = store.register_definition(
        kind="SCHEMA",
        version="agent-team-activation-contract-v4",
        sha256=contract_schema_digest,
        source_ref="agents/contracts/schemas/activation-contract.schema.json",
    )
    profile_definition_id = contract.actor.profile_definition_id or store.register_definition(
        kind="PROFILE",
        version=f"compiled-profile-{contract.actor.compiled_profile_sha256[:16]}",
        sha256=contract.actor.compiled_profile_sha256,
        source_ref=contract.actor.compiled_profile_ref,
    )
    clause_definition_ids: list[str] = []
    for item in contract.clauses:
        clause_definition_ids.append(
            store.register_definition(
                kind="CLAUSE",
                version=f"{item.clause_id}@{item.version}",
                sha256=item.sha256,
                source_ref="agents/contracts/clause-catalog.toml",
            )
        )
    skill_definition_ids: list[str] = []
    for item in contract.document["skills"]:
        skill_definition_ids.append(
            store.register_definition(
                kind="SKILL",
                version=f"{item['id']}@{item['version']}",
                sha256=item["sha256"],
                source_ref=f"skills/{item['path']}",
            )
        )
    output_schema_definition_id = database["output_schema_definition_id"]
    store.register_activation_contract(
        contract_id=contract.contract_id,
        workflow_instance_id=contract.workflow_instance.instance_id,
        workflow_transition_id=database["transition_database_id"],
        goal_id=contract.workflow_instance.goal_id,
        run_id=contract.workflow_instance.run_id,
        physical_seat_id=contract.actor.physical_seat_id,
        capability_id=database["capability_id"],
        seat_capability_activation_id=contract.actor.seat_capability_activation_id,
        worker_id=contract.actor.worker_id,
        worker_fingerprint_id=contract.actor.worker_fingerprint_id,
        slot_id=contract.actor.slot_id,
        worker_assignment_id=contract.actor.worker_assignment_id,
        repository_id=contract.evidence.repository.repository_id,
        lease_id=contract.evidence.lease_id,
        sandbox_binding_id=contract.evidence.sandbox_binding_id,
        oid_authority_id=contract.evidence.oid_authority_id,
        base_oid=contract.evidence.base_oid,
        subject_oid=contract.evidence.subject_oid,
        contract_definition_id=contract_definition_id,
        output_schema_definition_id=output_schema_definition_id,
        contract_digest=contract.digest,
        packet_digest=contract.rendered_packet.packet_sha256,
        context_char_budget=contract.effective_budget,
        max_attempts=2,
        idempotency_key=contract.document["idempotency_key"],
    )
    store.bind_contract_profile(
        contract_id=contract.contract_id,
        profile_definition_id=profile_definition_id,
        compiled_profile_ref=contract.actor.compiled_profile_ref,
        compiled_profile_digest=contract.actor.compiled_profile_sha256,
    )
    for ordinal, (item, definition_id) in enumerate(
        zip(contract.clauses, clause_definition_ids, strict=True)
    ):
        store.bind_contract_clause(
            contract_id=contract.contract_id,
            ordinal=ordinal,
            definition_id=definition_id,
            clause_digest=item.sha256,
            character_count=item.character_count,
        )
    for ordinal, (item, definition_id) in enumerate(
        zip(contract.document["skills"], skill_definition_ids, strict=True)
    ):
        store.bind_contract_skill(
            contract_id=contract.contract_id,
            skill_definition_id=definition_id,
            capability_id=database["capability_id"],
            ordinal=ordinal,
            bound_digest=item["sha256"],
            content_character_count=item["content_budget_chars"],
        )
    mcp_database_bindings: dict[tuple[str, str, str], str] = {}
    for item in contract.document["mcp_bindings"]:
        tools = item["tool_ids"] or ["server-health"]
        for tool in tools:
            definition_digest = sha256_json(
                {"binding": item["binding_id"], "server": item["server_id"], "tool": tool,
                 "policy": item["policy_sha256"]}
            )
            definition_id = store.register_mcp_definition(
                server_name=item["server_id"],
                tool_name=tool,
                version=f"{item['binding_id']}@1",
                sha256=definition_digest,
            )
            binding_id = store.bind_contract_mcp(
                contract_id=contract.contract_id,
                mcp_definition_id=definition_id,
                required_availability=True,
                invocation_required=item["required_use"],
                trigger_rule=item["binding_id"],
            )
            mcp_database_bindings[(item["binding_id"], item["server_id"], tool)] = binding_id
            health = _health_value(contract, item["server_id"])
            if health is not None:
                evidence_digest = health.get("evidence_digest") or sha256_json(health)
                try:
                    health_status = McpHealthStatus(
                        str(health.get("status", "UNKNOWN")).upper()
                    )
                except ValueError:
                    health_status = McpHealthStatus.UNKNOWN
                store.record_mcp_health_observation(
                    mcp_definition_id=definition_id,
                    contract_id=contract.contract_id,
                    status=health_status,
                    evidence_digest=evidence_digest,
                    idempotency_key=(
                        f"mcp-health:{contract.contract_id}:{definition_id}:{evidence_digest}"
                    ),
                )
    onboarding = contract.document.get("serena_onboarding")
    serena_database_bindings: dict[str, str] = {}
    if onboarding:
        for ordinal, item in enumerate(onboarding["memory_bindings"]):
            serena_database_bindings[item["name"]] = store.bind_contract_serena_memory(
                contract_id=contract.contract_id,
                snapshot_id=onboarding["snapshot_id"],
                memory_name=item["name"],
                ordinal=ordinal,
            )
    contract.database_bindings["mcp_bindings"] = mcp_database_bindings
    contract.database_bindings["serena_bindings"] = serena_database_bindings


def _build_violation(
    contract: ActivationContract,
    *,
    category: str,
    reason_code: str,
    action: str = "reject",
    backend_call_recorded: bool = False,
    attempt_id: str | None = None,
    ordinal: int | None = None,
    recorded_at: str | None = None,
) -> ContractViolation:
    key = f"contract-violation:{contract.contract_id}:{category}:{reason_code}:{attempt_id or 'admission'}"
    return ContractViolation(
        violation_id=stable_identifier("contract-violation", key),
        contract_id=contract.contract_id,
        activation_id=contract.activation_id,
        worker_fingerprint=contract.actor.worker_fingerprint,
        category=category,
        reason_code=reason_code,
        action=action,
        backend_call_recorded=backend_call_recorded,
        format_violation_ordinal=ordinal,
        evidence_refs=contract.evidence.evidence_refs,
        idempotency_key=key,
        recorded_at=recorded_at or _now(),
    )


def _violation(
    contract: ActivationContract,
    *,
    category: str,
    reason_code: str,
    action: str = "reject",
    backend_call_recorded: bool = False,
    attempt_id: str | None = None,
    ordinal: int | None = None,
) -> ContractViolation:
    violation = _build_violation(
        contract,
        category=category,
        reason_code=reason_code,
        action=action,
        backend_call_recorded=backend_call_recorded,
        attempt_id=attempt_id,
        ordinal=ordinal,
    )
    code_map = {
        "format": ContractViolationCode.FORMAT,
        "authority": ContractViolationCode.AUTHORITY,
        "oid": ContractViolationCode.OID,
        "write_scope": ContractViolationCode.WRITE_ROOT,
        "nested_spawn": ContractViolationCode.NESTED_SPAWN,
        "mcp_health": ContractViolationCode.MCP_HEALTH,
        "mcp_tool": ContractViolationCode.MCP_HEALTH,
        "mcp_receipt": ContractViolationCode.MCP_USAGE,
        "serena_onboarding": ContractViolationCode.SERENA_ONBOARDING,
        "serena_receipt": ContractViolationCode.SERENA_CONSUMPTION,
    }
    contract.state_store.record_contract_violation(
        contract_id=contract.contract_id,
        attempt_id=attempt_id,
        violation_code=code_map.get(category, ContractViolationCode.OTHER),
        evidence_digest=sha256_json(violation.as_dict()),
        details=violation.as_dict(),
        idempotency_key=violation.idempotency_key,
    )
    return violation


def admit(contract: ActivationContract) -> AdmissionReceipt | ContractViolation:
    """Fail closed before an attempt or model call can be created."""

    checks, violations = _admission_checks(contract)
    if violations:
        reason = violations[0]
        category = (
            "mcp_health" if reason.startswith("mcp-unhealthy")
            else "mcp_tool" if reason.startswith("mcp-tools")
            else "serena_onboarding" if reason.startswith("serena")
            else "budget" if reason.startswith("contract-budget")
            else "definition_digest"
        )
        _register_contract_graph(contract)
        contract.state_store.record_contract_admission(
            contract_id=contract.contract_id,
            accepted=False,
            reason_code=reason,
        )
        return _violation(contract, category=category, reason_code=reason)
    _register_contract_graph(contract)
    admission_id = contract.state_store.record_contract_admission(
        contract_id=contract.contract_id,
        accepted=True,
        reason_code=None,
    )
    receipt = AdmissionReceipt(
        receipt_id=admission_id,
        contract_id=contract.contract_id,
        contract_sha256=contract.digest,
        accepted=True,
        reason_code=None,
        model_call_permitted=True,
        checks=MappingProxyType(checks),
        violations=(),
        idempotency_key=f"admission:{contract.contract_id}",
        recorded_at=_now(),
    )
    return receipt


def begin_attempt(
    contract: ActivationContract,
    admission: AdmissionReceipt,
    *,
    backend: str,
    model: str,
    input_digest: str,
    attempt_kind: ContractAttemptKind | str = ContractAttemptKind.PRIMARY,
) -> str:
    if not admission.accepted or not admission.model_call_permitted:
        raise ContractAdmissionError("backend attempt requires accepted admission")
    if admission.contract_id != contract.contract_id or admission.contract_sha256 != contract.digest:
        raise ContractAdmissionError("admission does not match contract bytes")
    if not _SHA256_RE.fullmatch(input_digest):
        raise ContractAdmissionError("attempt input digest is invalid")
    return contract.state_store.record_contract_attempt(
        contract_id=contract.contract_id,
        backend=backend,
        model=model,
        input_digest=input_digest,
        attempt_kind=attempt_kind,
    )


def _result_violation(
    result: ActivationResult,
    category: str,
    reason: str,
) -> Quarantine | ReworkRoute:
    contract = result.contract
    quarantine = category in _QUARANTINE_CATEGORIES
    violation = _build_violation(
        contract,
        category=category,
        reason_code=reason,
        action="quarantine" if quarantine else "reject",
        backend_call_recorded=True,
        attempt_id=result.attempt_id,
    )
    code_map = {
        "authority": ContractViolationCode.AUTHORITY,
        "oid": ContractViolationCode.OID,
        "write_scope": ContractViolationCode.WRITE_ROOT,
        "nested_spawn": ContractViolationCode.NESTED_SPAWN,
        "mcp_receipt": ContractViolationCode.MCP_USAGE,
        "serena_receipt": ContractViolationCode.SERENA_CONSUMPTION,
    }
    invalid_payload = {
        "schema_version": 4,
        "contract_id": contract.contract_id,
        "activation_id": contract.activation_id,
        "attempt_id": result.attempt_id,
        "category": category,
        "reason_code": reason,
        "submitted_result": dict(result.payload),
    }
    route_receipt_id = stable_identifier(
        "transition-receipt", contract.contract_id, result.attempt_id, reason
    )
    result_key = (
        f"invalid-result:{contract.contract_id}:{result.attempt_id}:{category}:{reason}"
    )
    message_type = "activation_quarantined" if quarantine else "rework_required"
    outgoing_messages = (
        {
            "thread_id": f"{contract.workflow_instance.goal_id}:{contract.workflow_instance.run_id}",
            "work_item_id": contract.activation_id,
            "from_role": contract.actor.capability_id,
            "to_role": "pl",
            "type": message_type,
            "priority": 100 if quarantine else 50,
            "max_attempts": 5,
            "dedupe_key": f"{result_key}:pl-route",
            "payload": {
                "contract_id": contract.contract_id,
                "activation_id": contract.activation_id,
                "attempt_id": result.attempt_id,
                "reason_code": reason,
                "category": category,
                "subject_oid": contract.evidence.subject_oid,
                "failure_state": contract.transition.failure_state,
            },
        },
    )
    durable = contract.state_store.commit_invalid_activation_result_transaction(
        activation_id=contract.activation_id,
        contract_id=contract.contract_id,
        attempt_id=result.attempt_id,
        disposition=(
            "QUARANTINED" if quarantine else "REJECTED"
        ),
        result_kind="invalid-result",
        output_digest=sha256_json(dict(result.payload)),
        evidence_digest=sha256_json(result.payload.get("evidence_refs", [])),
        payload=invalid_payload,
        violation_code=code_map.get(category, ContractViolationCode.OTHER),
        violation_evidence_digest=sha256_json(violation.as_dict()),
        violation_details=violation.as_dict(),
        violation_idempotency_key=violation.idempotency_key,
        workflow_instance_id=contract.workflow_instance.instance_id,
        workflow_transition_id=contract.database_bindings["transition_database_id"],
        from_state=contract.workflow_instance.current_state,
        failure_state=None if quarantine else contract.transition.failure_state,
        transition_receipt_id=None if quarantine else route_receipt_id,
        result_idempotency_key=result_key,
        transition_idempotency_key=(
            None if quarantine else f"transition:{result_key}"
        ),
        outgoing_messages=outgoing_messages,
    )
    if quarantine:
        return Quarantine(
            quarantine_id=stable_identifier("quarantine", contract.contract_id, reason),
            contract_id=contract.contract_id,
            category=category,
            reason_code=reason,
            subject_oid=contract.evidence.subject_oid,
            worker_fingerprint=contract.actor.worker_fingerprint,
            activation_result_id=durable.activation_result_id,
            message_ids=durable.message_ids,
            outbox_ids=durable.outbox_ids,
        )
    return ReworkRoute(
        route_id=stable_identifier("rework", contract.contract_id, reason),
        contract_id=contract.contract_id,
        transition_id="pl_issue_rework",
        owner_capability="pl",
        failure_state=contract.transition.failure_state,
        subject_oid=contract.evidence.subject_oid,
        failure_oid=result.payload.get("result_oid") or contract.evidence.subject_oid,
        reason_code=reason,
        direct_source_repair_allowed=False,
        activation_result_id=durable.activation_result_id,
        message_ids=durable.message_ids,
        outbox_ids=durable.outbox_ids,
    )


def _format_route(result: ActivationResult, reason: str) -> ReworkRoute | Quarantine:
    contract = result.contract
    with contract.state_store.transaction() as connection:
        row = connection.execute(
            """
            SELECT attempt_kind, started_at
            FROM contract_attempts WHERE id = ? AND contract_id = ?
            """,
            (result.attempt_id, contract.contract_id),
        ).fetchone()
    if row is None:
        raise ContractAdmissionError("format violation attempt is not durable")
    ordinal = 2 if row["attempt_kind"] == "FORMAT_REPAIR" else 1
    violation = _build_violation(
        contract,
        category="format",
        reason_code=reason,
        action="output_only_repair",
        backend_call_recorded=True,
        attempt_id=result.attempt_id,
        ordinal=ordinal,
        recorded_at=row["started_at"],
    )
    quarantine_messages: tuple[Mapping[str, Any], ...] = ()
    if ordinal == 2:
        quarantine_messages = (
            {
                "thread_id": (
                    f"{contract.workflow_instance.goal_id}:"
                    f"{contract.workflow_instance.run_id}"
                ),
                "work_item_id": contract.activation_id,
                "from_role": contract.actor.capability_id,
                "to_role": "pl",
                "type": "activation_quarantined",
                "priority": 100,
                "max_attempts": 5,
                "dedupe_key": (
                    f"format-circuit:{contract.contract_id}:{result.attempt_id}:pl"
                ),
                "payload": {
                    "contract_id": contract.contract_id,
                    "activation_id": contract.activation_id,
                    "attempt_id": result.attempt_id,
                    "reason_code": "format-circuit-open",
                    "subject_oid": contract.evidence.subject_oid,
                },
            },
        )
    (
        activation_result_id,
        _,
        disposition,
        message_ids,
        outbox_ids,
    ) = contract.state_store.record_format_invalid_result_and_violation(
        activation_id=contract.activation_id,
        attempt_id=result.attempt_id,
        result_kind="activation-result",
        output_digest=sha256_json(dict(result.payload)),
        evidence_digest=sha256_json(dict(result.payload).get("evidence_refs", [])),
        payload=dict(result.payload),
        violation_evidence_digest=sha256_json(violation.as_dict()),
        violation_details=violation.as_dict(),
        violation_idempotency_key=violation.idempotency_key,
        quarantine_messages=quarantine_messages,
    )
    if disposition == "CIRCUIT_OPEN":
        return Quarantine(
            quarantine_id=stable_identifier("circuit", contract.contract_id, reason),
            contract_id=contract.contract_id,
            category="format",
            reason_code="format-circuit-open",
            subject_oid=contract.evidence.subject_oid,
            worker_fingerprint=contract.actor.worker_fingerprint,
            activation_result_id=activation_result_id,
            message_ids=message_ids,
            outbox_ids=outbox_ids,
        )
    return ReworkRoute(
        route_id=stable_identifier("format-repair", contract.contract_id),
        contract_id=contract.contract_id,
        transition_id=contract.transition.transition_id,
        owner_capability=contract.actor.capability_id,
        failure_state=contract.workflow_instance.current_state,
        subject_oid=contract.evidence.subject_oid,
        failure_oid=contract.evidence.subject_oid,
        reason_code=reason,
        direct_source_repair_allowed=False,
        attempt_kind=ContractAttemptKind.FORMAT_REPAIR.value,
        activation_result_id=activation_result_id,
        message_ids=message_ids,
        outbox_ids=outbox_ids,
    )


def _validated_mcp_receipts(
    contract: ActivationContract,
    payload: Mapping[str, Any],
    attempt_id: str | None,
) -> tuple[list[dict[str, str]], str | None]:
    raw_receipts = payload.get("mcp_usage_receipts", [])
    if not isinstance(raw_receipts, list):
        return [], "mcp-receipts-not-an-array"
    allowed: set[tuple[str, str]] = set()
    required: set[tuple[str, str]] = set()
    for binding in contract.document["mcp_bindings"]:
        for tool in binding["tool_ids"]:
            pair = (binding["server_id"], tool)
            allowed.add(pair)
            if binding["required_use"] and binding["usage_receipt_required"]:
                required.add(pair)
    normalized: list[dict[str, str]] = []
    seen_receipt_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for raw in raw_receipts:
        if not isinstance(raw, Mapping):
            return [], "mcp-receipt-not-an-object"
        receipt = {
            key: raw.get(key)
            for key in (
                "receipt_id",
                "server_id",
                "tool_id",
                "activation_id",
                "evidence_sha256",
            )
        }
        pair = (receipt["server_id"], receipt["tool_id"])
        if (
            not all(isinstance(value, str) and value for value in receipt.values())
            or not _SHA256_RE.fullmatch(str(receipt["evidence_sha256"]))
            or receipt["activation_id"] != contract.activation_id
            or pair not in allowed
            or receipt["receipt_id"] in seen_receipt_ids
            or pair in seen_pairs
        ):
            return [], "mcp-receipt-does-not-match-contract-binding"
        seen_receipt_ids.add(str(receipt["receipt_id"]))
        seen_pairs.add((str(pair[0]), str(pair[1])))
        normalized.append({key: str(value) for key, value in receipt.items()})
    if not required <= seen_pairs:
        return [], "required-mcp-receipt-missing"
    if attempt_id is None:
        return [], "trusted-mcp-receipt-attempt-missing"
    try:
        contract.state_store.validate_trusted_mcp_receipt_references(
            contract_id=contract.contract_id,
            attempt_id=attempt_id,
            receipts=normalized,
        )
    except (IntentStateError, KeyError, ValueError):
        return [], "trusted-mcp-receipt-invalid"
    return normalized, None


def _validated_serena_receipts(
    contract: ActivationContract,
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, str]], str | None]:
    raw_receipts = payload.get("serena_consumption_receipts", [])
    if not isinstance(raw_receipts, list):
        return [], "serena-receipts-not-an-array"
    onboarding = contract.document.get("serena_onboarding")
    if onboarding is None:
        if raw_receipts:
            return [], "unexpected-serena-consumption-receipt"
        return [], None
    expected = {
        item["name"]: item["sha256"]
        for item in onboarding["memory_bindings"]
    }
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_receipts:
        if not isinstance(raw, Mapping):
            return [], "serena-receipt-not-an-object"
        receipt = {
            key: raw.get(key)
            for key in (
                "snapshot_id",
                "memory_name",
                "memory_sha256",
                "consumed_at",
            )
        }
        if not all(isinstance(value, str) and value for value in receipt.values()):
            return [], "serena-receipt-is-incomplete"
        try:
            datetime.fromisoformat(str(receipt["consumed_at"]).replace("Z", "+00:00"))
        except ValueError:
            return [], "serena-receipt-consumed-at-invalid"
        name = str(receipt["memory_name"])
        if (
            receipt["snapshot_id"] != onboarding["snapshot_id"]
            or expected.get(name) != receipt["memory_sha256"]
            or name in seen
        ):
            return [], "serena-receipt-does-not-match-contract-binding"
        seen.add(name)
        normalized.append({key: str(value) for key, value in receipt.items()})
    if onboarding["consumption_receipt_required"] and seen != set(expected):
        return [], "required-serena-consumption-receipt-missing"
    return normalized, None


def _serena_receipt_specs(
    result: ActivationResult,
    serena_receipts: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    contract = result.contract
    store = contract.state_store
    serena_bindings = contract.database_bindings.get("serena_bindings", {})
    onboarding = contract.document.get("serena_onboarding")
    specs: list[dict[str, str]] = []
    for receipt in serena_receipts:
        binding_id = serena_bindings.get(receipt["memory_name"])
        if binding_id is None or onboarding is None:
            raise ContractAdmissionError(
                "Serena result receipt has no durable contract binding"
            )
        specs.append(
            {
                "memory_binding_id": binding_id,
                "receipt_digest": sha256_json(dict(receipt)),
                "idempotency_key": (
                    f"serena-consumption:{contract.contract_id}:"
                    f"{onboarding['snapshot_id']}:{receipt['memory_name']}"
                ),
            }
        )
    return specs


def commit_result(
    result: ActivationResult | Mapping[str, Any],
) -> TransitionReceipt | ReworkRoute | Quarantine:
    """Validate one result and route it through v4 result/violation state."""

    if isinstance(result, Mapping):
        contract = result.get("contract")
        if not isinstance(contract, ActivationContract):
            raise ContractAdmissionError("mapping result requires ActivationContract")
        result = ActivationResult(
            contract=contract,
            payload=dict(result.get("payload", {})),
            attempt_id=result.get("attempt_id"),
            format_error_only=result.get("format_error_only") is True,
        )
    contract = result.contract
    payload = dict(result.payload)
    try:
        _validate_schema(payload, contract.definitions.activation_result_schema_path)
    except ContractCompilationError as exc:
        if result.format_error_only:
            return _format_route(result, "activation-result-format-invalid")
        return _result_violation(
            result, "schema", f"result-schema-invalid-{type(exc).__name__}"
        )
    expected = {
        "contract_id": contract.contract_id,
        "activation_id": contract.activation_id,
        "transition_id": contract.transition.transition_id,
        "capability_id": contract.actor.capability_id,
    }
    for field_name, value in expected.items():
        if payload.get(field_name) != value:
            return _result_violation(result, "authority", f"result-{field_name}-mismatch")
    if payload.get("subject_oid") != contract.evidence.subject_oid:
        return _result_violation(result, "oid", "result-subject-oid-mismatch")
    if payload.get("idempotency_key") != contract.document["idempotency_key"]:
        return _result_violation(result, "authority", "result-idempotency-mismatch")
    if payload.get("result_kind") not in contract.transition.result_kinds:
        return _result_violation(result, "authority", "result-kind-not-allowed")
    lease = payload.get("payload", {}).get("lease_id")
    if lease is not None and lease != contract.evidence.lease_id:
        return _result_violation(result, "write_scope", "result-lease-mismatch")
    mcp_receipts, mcp_error = _validated_mcp_receipts(
        contract, payload, result.attempt_id
    )
    if mcp_error is not None:
        return _result_violation(result, "mcp_receipt", mcp_error)
    serena_receipts, serena_error = _validated_serena_receipts(contract, payload)
    if serena_error is not None:
        return _result_violation(result, "serena_receipt", serena_error)
    output_digest = sha256_json(payload)
    evidence_digest = sha256_json(payload.get("evidence_refs", []))
    is_rework = payload["result_kind"] in _REWORK_RESULT_KINDS
    to_state = (
        contract.transition.failure_state
        if is_rework
        else contract.transition.to_state or contract.workflow_instance.current_state
    )
    receipt = TransitionReceipt(
        receipt_id=stable_identifier(
            "transition-receipt", contract.contract_id, payload["result_id"]
        ),
        contract_id=contract.contract_id,
        transition_id=contract.transition.transition_id,
        from_state=contract.workflow_instance.current_state,
        to_state=to_state,
        subject_oid=contract.evidence.subject_oid,
        result_oid=payload.get("result_oid"),
        evidence_digest=evidence_digest,
    )
    inner_payload = payload.get("payload")
    if not isinstance(inner_payload, Mapping):
        return _result_violation(result, "schema", "result-payload-not-an-object")
    outgoing_raw = inner_payload.get("outgoing_messages", [])
    if not isinstance(outgoing_raw, list) or any(
        not isinstance(item, Mapping) for item in outgoing_raw
    ):
        return _result_violation(
            result, "schema", "result-outgoing-messages-invalid"
        )
    outgoing_messages: list[Mapping[str, Any]] = [dict(item) for item in outgoing_raw]
    if is_rework and not any(
        str(item.get("to_role", "")).lower() == "pl" for item in outgoing_messages
    ):
        outgoing_messages.append(
            {
                "thread_id": (
                    f"{contract.workflow_instance.goal_id}:"
                    f"{contract.workflow_instance.run_id}"
                ),
                "work_item_id": contract.activation_id,
                "from_role": contract.actor.capability_id,
                "to_role": "pl",
                "type": "rework_required",
                "priority": 50,
                "max_attempts": 5,
                "dedupe_key": (
                    f"valid-rework:{contract.contract_id}:{result.attempt_id}:pl"
                ),
                "payload": {
                    "contract_id": contract.contract_id,
                    "activation_id": contract.activation_id,
                    "attempt_id": result.attempt_id,
                    "reason_code": payload["result_kind"],
                    "subject_oid": contract.evidence.subject_oid,
                    "failure_state": contract.transition.failure_state,
                },
            }
        )
    accounting = payload["token_accounting"]
    try:
        durable = contract.state_store.commit_activation_result_transaction(
            activation_id=contract.activation_id,
            contract_id=contract.contract_id,
            attempt_id=result.attempt_id,
            result_kind=payload["result_kind"],
            output_digest=output_digest,
            evidence_digest=evidence_digest,
            payload=payload,
            input_tokens=accounting["input_tokens"],
            output_tokens=(accounting["output_tokens"] + accounting["repair_tokens"]),
            model_calls=1,
            mcp_receipts=mcp_receipts,
            serena_receipts=_serena_receipt_specs(result, serena_receipts),
            workflow_instance_id=contract.workflow_instance.instance_id,
            workflow_transition_id=contract.database_bindings["transition_database_id"],
            from_state=receipt.from_state,
            to_state=receipt.to_state,
            transition_receipt_id=receipt.receipt_id,
            result_idempotency_key=payload["idempotency_key"],
            token_idempotency_key=f"tokens:{payload['result_id']}",
            transition_idempotency_key=f"transition:{payload['idempotency_key']}",
            outgoing_messages=outgoing_messages,
        )
    except IntentStateError as exc:
        raise ContractAdmissionError(str(exc)) from exc
    receipt = replace(
        receipt,
        activation_result_id=durable.activation_result_id,
        message_ids=durable.message_ids,
        outbox_ids=durable.outbox_ids,
    )
    if is_rework:
        return ReworkRoute(
            route_id=stable_identifier(
                "rework", contract.contract_id, payload["result_id"]
            ),
            contract_id=contract.contract_id,
            transition_id="pl_issue_rework",
            owner_capability="pl",
            failure_state=contract.transition.failure_state,
            subject_oid=contract.evidence.subject_oid,
            failure_oid=payload.get("result_oid") or contract.evidence.subject_oid,
            reason_code=payload["result_kind"],
            direct_source_repair_allowed=False,
            activation_result_id=durable.activation_result_id,
            message_ids=durable.message_ids,
            outbox_ids=durable.outbox_ids,
        )
    return receipt


__all__ = [
    "ActivationContract",
    "ActivationResult",
    "AdmissionReceipt",
    "ContractAdmissionError",
    "ContractCompilationError",
    "ContractViolation",
    "Quarantine",
    "RenderedPacket",
    "ReworkRoute",
    "TransitionReceipt",
    "admit",
    "begin_attempt",
    "commit_result",
    "compile_transition",
    "render",
]
