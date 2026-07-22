"""Versioned workflow and transition definitions for Agent-Team contracts.

The TOML files under ``agents/`` are the definition authority.  This module
turns those immutable bytes into small typed values; it does not schedule work
or grant authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

try:
    from .agent_team_layout import AgentTeamLayout
except ImportError:  # pragma: no cover - direct script import
    from agent_team_layout import AgentTeamLayout


LAYOUT = AgentTeamLayout.discover(Path(__file__))
PROJECT_ROOT = LAYOUT.source_root
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")


class WorkflowDefinitionError(ValueError):
    """A versioned workflow input is missing, changed, or inconsistent."""


def canonical_json(value: Any) -> str:
    def thaw(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): thaw(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [thaw(child) for child in item]
        return item

    return json.dumps(
        thaw(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkflowDefinitionError(f"Cannot read TOML definition {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowDefinitionError(f"TOML definition must be an object: {path}")
    return value


def _inside_project(value: str, *, label: str) -> Path:
    path = (PROJECT_ROOT / value).resolve()
    try:
        path.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise WorkflowDefinitionError(f"{label} escapes the project root: {value}") from exc
    if not path.is_file():
        raise WorkflowDefinitionError(f"{label} is missing: {path}")
    return path


def _strings(value: Any, *, label: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise WorkflowDefinitionError(f"{label} must be a string array")
    result = tuple(value)
    if not all(isinstance(item, str) and item for item in result):
        raise WorkflowDefinitionError(f"{label} must contain non-empty strings")
    if len(result) != len(set(result)):
        raise WorkflowDefinitionError(f"{label} must not contain duplicates")
    return result


def _digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise WorkflowDefinitionError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _oid(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _OID_RE.fullmatch(value):
        raise WorkflowDefinitionError(f"{label} must be one full lowercase Git OID")
    return value


@dataclass(frozen=True, slots=True)
class ClauseBinding:
    clause_id: str
    version: str
    sha256: str
    text: str
    source_refs: tuple[str, ...] = ()
    definition_id: str | None = None

    @property
    def character_count(self) -> int:
        return len(self.text)

    def contract_value(self) -> dict[str, str]:
        return {"id": self.clause_id, "version": self.version, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class TransitionDefinition:
    transition_id: str
    from_states: tuple[str, ...]
    to_state: str | None
    state_effect: str
    capabilities: tuple[str, ...]
    result_kinds: tuple[str, ...]
    failure_state: str
    clause_ids: tuple[str, ...]
    mcp_availability_binding_ids: tuple[str, ...]
    mcp_required_use_binding_ids: tuple[str, ...]
    exact_oid_required: bool
    workspace_kind: str
    serena_onboarding: str | None = None
    serena_consumption_receipt_required: bool = False
    developer_consumption_receipt_required: bool = False
    max_active_workers_per_goal_run: int | None = None
    definition_sha256: str = ""
    database_id: str | None = None
    output_schema_definition_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TransitionDefinition":
        transition_id = value.get("id")
        if not isinstance(transition_id, str) or not transition_id:
            raise WorkflowDefinitionError("workflow transition id must be non-empty")
        if "from_state" in value:
            from_states = (str(value["from_state"]),)
        else:
            from_states = _strings(value.get("from_states"), label=f"{transition_id}.from_states")
        capabilities = _strings(value.get("capabilities"), label=f"{transition_id}.capabilities")
        result_kinds = _strings(value.get("result_kinds"), label=f"{transition_id}.result_kinds")
        clause_ids = _strings(value.get("clause_ids"), label=f"{transition_id}.clause_ids")
        availability = _strings(
            value.get("mcp_availability_binding_ids", []),
            label=f"{transition_id}.mcp_availability_binding_ids",
            allow_empty=True,
        )
        required_use = _strings(
            value.get("mcp_required_use_binding_ids", []),
            label=f"{transition_id}.mcp_required_use_binding_ids",
            allow_empty=True,
        )
        normalized = dict(value)
        definition_sha256 = sha256_json(normalized)
        to_state = value.get("to_state")
        if to_state is not None and (not isinstance(to_state, str) or not to_state):
            raise WorkflowDefinitionError(f"{transition_id}.to_state is invalid")
        failure_state = value.get("failure_state")
        if not isinstance(failure_state, str) or not failure_state:
            raise WorkflowDefinitionError(f"{transition_id}.failure_state is invalid")
        workspace_kind = value.get("workspace_kind")
        if not isinstance(workspace_kind, str) or not workspace_kind:
            raise WorkflowDefinitionError(f"{transition_id}.workspace_kind is invalid")
        return cls(
            transition_id=transition_id,
            from_states=from_states,
            to_state=to_state,
            state_effect=str(value.get("state_effect", "advance")),
            capabilities=capabilities,
            result_kinds=result_kinds,
            failure_state=failure_state,
            clause_ids=clause_ids,
            mcp_availability_binding_ids=availability,
            mcp_required_use_binding_ids=required_use,
            exact_oid_required=value.get("exact_oid_required") is True,
            workspace_kind=workspace_kind,
            serena_onboarding=value.get("serena_onboarding"),
            serena_consumption_receipt_required=(
                value.get("serena_consumption_receipt_required") is True
            ),
            developer_consumption_receipt_required=(
                value.get("developer_consumption_receipt_required") is True
            ),
            max_active_workers_per_goal_run=value.get("max_active_workers_per_goal_run"),
            definition_sha256=definition_sha256,
        )

    @property
    def from_state(self) -> str:
        return self.from_states[0]

    def applies_to(self, state: str, capability_id: str) -> bool:
        return state in self.from_states and capability_id in self.capabilities


@dataclass(frozen=True, slots=True)
class WorkflowInstance:
    instance_id: str
    goal_id: str
    run_id: str
    target_id: str
    current_state: str
    workflow_id: str = "delivery-v4"
    workflow_version: str = "4.0.0"
    workflow_sha256: str = "923417b7a391d76062ed0625bc07c71ccdfe37f594c2a0d966fbe209c4510aa0"
    status: str = "ACTIVE"
    workflow_definition_id: str | None = None
    state_store: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class RepositoryBinding:
    repository_id: str
    source_oid: str
    canonical_path: str | None = None
    state_store: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _oid(self.source_oid, label="repository.source_oid")


@dataclass(frozen=True, slots=True)
class ActorBinding:
    activation_id: str
    capability_id: str
    slot_key: str
    slot_type: str
    worker_id: str
    worker_fingerprint: str
    worker_fingerprint_id: str
    slot_id: str
    worker_assignment_id: str
    seat_id: str | None = None
    physical_seat_id: str | None = None
    seat_capability_activation_id: str | None = None
    elastic_lease_id: str | None = None
    agent_definition_id: str | None = None
    agent_definition_sha256: str | None = None
    parent_activation_id: str | None = None
    compiled_profile_ref: str = ""
    compiled_profile_sha256: str = ""
    profile_reference_sha256s: tuple[str, ...] = ()
    profile_definition_id: str | None = None
    selected_skills: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceSet:
    repository: RepositoryBinding
    lease_id: str
    sandbox_binding_id: str
    oid_authority_id: str
    base_oid: str
    subject_oid: str
    workspace: Mapping[str, Any]
    mcp_health: Mapping[str, Any]
    mcp_trigger_ids: tuple[str, ...] = ()
    serena_snapshot: Any = None
    head_oid: str | None = None
    integration_oid: str | None = None
    failure_oid: str | None = None
    evidence_refs: tuple[str, ...] = ()
    contract_ref: str | None = None
    artifact_root: str | None = None
    issued_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    def __post_init__(self) -> None:
        _oid(self.base_oid, label="evidence.base_oid")
        _oid(self.subject_oid, label="evidence.subject_oid")
        if self.head_oid is not None:
            _oid(self.head_oid, label="evidence.head_oid")


@dataclass(frozen=True, slots=True)
class WorkflowDefinitions:
    workflow: Mapping[str, Any]
    workflow_sha256: str
    transitions: Mapping[str, TransitionDefinition]
    clauses: Mapping[str, ClauseBinding]
    capabilities: Mapping[str, Mapping[str, Any]]
    slots: Mapping[str, Mapping[str, Any]]
    context_profiles: Mapping[str, Any]
    mcp_policy: Mapping[str, Any]
    template_path: Path
    template_version: str
    activation_contract_schema_path: Path
    activation_result_schema_path: Path

    @classmethod
    def load(cls, root: Path | None = None) -> "WorkflowDefinitions":
        project = (root or PROJECT_ROOT).resolve()
        workflow_catalog_path = project / "agents" / "workflows" / "catalog.toml"
        workflow_catalog = _read_toml(workflow_catalog_path)
        entries = workflow_catalog.get("workflows")
        if not isinstance(entries, list) or len(entries) != 1:
            raise WorkflowDefinitionError("workflow catalog must contain one active workflow")
        entry = entries[0]
        if not isinstance(entry, dict) or entry.get("active") is not True:
            raise WorkflowDefinitionError("workflow catalog has no active workflow")
        workflow_path = (project / str(entry.get("path"))).resolve()
        try:
            workflow_path.relative_to(project)
        except ValueError as exc:
            raise WorkflowDefinitionError("workflow path escapes the project root") from exc
        observed_workflow_digest = sha256_bytes(workflow_path.read_bytes())
        expected_workflow_digest = _digest(entry.get("sha256"), label="workflow.sha256")
        if observed_workflow_digest != expected_workflow_digest:
            raise WorkflowDefinitionError("active workflow bytes do not match the catalog digest")
        workflow_data = _read_toml(workflow_path)
        workflow = workflow_data.get("workflow")
        if not isinstance(workflow, dict):
            raise WorkflowDefinitionError("workflow metadata is missing")
        if workflow.get("id") != entry.get("id") or workflow.get("version") != entry.get("version"):
            raise WorkflowDefinitionError("workflow catalog identity does not match its bytes")
        transition_values = workflow_data.get("transitions")
        if not isinstance(transition_values, list) or not transition_values:
            raise WorkflowDefinitionError("workflow transitions are missing")
        transitions = {
            item.transition_id: item
            for item in (TransitionDefinition.from_mapping(raw) for raw in transition_values)
        }
        if len(transitions) != len(transition_values):
            raise WorkflowDefinitionError("workflow transition ids must be unique")

        clause_data = _read_toml(project / "agents" / "contracts" / "clause-catalog.toml")
        raw_clauses = clause_data.get("clauses")
        if not isinstance(raw_clauses, list) or not raw_clauses:
            raise WorkflowDefinitionError("clause catalog is empty")
        clauses: dict[str, ClauseBinding] = {}
        for raw in raw_clauses:
            if not isinstance(raw, dict):
                raise WorkflowDefinitionError("clause entry must be an object")
            text = raw.get("text")
            clause_id = raw.get("id")
            if not isinstance(text, str) or not isinstance(clause_id, str):
                raise WorkflowDefinitionError("clause entry is incomplete")
            expected = _digest(raw.get("sha256"), label=f"clause.{clause_id}.sha256")
            if sha256_text(text) != expected:
                raise WorkflowDefinitionError(f"clause bytes changed: {clause_id}")
            clauses[clause_id] = ClauseBinding(
                clause_id=clause_id,
                version=str(raw.get("version")),
                sha256=expected,
                text=text,
                source_refs=tuple(raw.get("source_refs", [])),
            )
        for transition in transitions.values():
            unknown = set(transition.clause_ids) - set(clauses)
            if unknown:
                raise WorkflowDefinitionError(
                    f"transition {transition.transition_id} references unknown clauses: {sorted(unknown)}"
                )

        capability_data = _read_toml(project / "agents" / "capabilities.toml")
        raw_capabilities = capability_data.get("capabilities")
        if not isinstance(raw_capabilities, list):
            raise WorkflowDefinitionError("capability catalog is missing")
        capabilities = {str(item["id"]): dict(item) for item in raw_capabilities}
        slot_data = _read_toml(project / "agents" / "seat-slots.toml")
        raw_slots = slot_data.get("slots")
        if not isinstance(raw_slots, list):
            raise WorkflowDefinitionError("slot catalog is missing")
        slots = {str(item["slot_key"]): dict(item) for item in raw_slots}
        context_profiles = _read_toml(project / "agents" / "context-profiles.toml")
        mcp_policy = _read_toml(project / "agents" / "mcp-policy.toml")
        template_path = project / str(workflow.get("packet_template"))
        if not template_path.is_file():
            raise WorkflowDefinitionError("activation packet template is missing")
        contract_schema = project / "agents" / "contracts" / "schemas" / "activation-contract.schema.json"
        result_schema = project / "agents" / "contracts" / "schemas" / "activation-result.schema.json"
        return cls(
            workflow=MappingProxyType(dict(workflow)),
            workflow_sha256=observed_workflow_digest,
            transitions=MappingProxyType(transitions),
            clauses=MappingProxyType(clauses),
            capabilities=MappingProxyType(capabilities),
            slots=MappingProxyType(slots),
            context_profiles=MappingProxyType(context_profiles),
            mcp_policy=MappingProxyType(mcp_policy),
            template_path=template_path,
            template_version=f"{workflow['id']}@{workflow['version']}",
            activation_contract_schema_path=contract_schema,
            activation_result_schema_path=result_schema,
        )

    def transition(self, transition_id: str) -> TransitionDefinition:
        try:
            return self.transitions[transition_id]
        except KeyError as exc:
            raise WorkflowDefinitionError(f"unknown workflow transition: {transition_id}") from exc

    def selected_clauses(self, transition: TransitionDefinition) -> tuple[ClauseBinding, ...]:
        return tuple(self.clauses[item] for item in transition.clause_ids)

    def role_packet_limit(self, capability_id: str) -> int:
        defaults = self.context_profiles.get("capability_defaults")
        profiles = self.context_profiles.get("profiles")
        if not isinstance(defaults, Mapping) or not isinstance(profiles, Mapping):
            raise WorkflowDefinitionError("context capability defaults are missing")
        profile_id = defaults.get(capability_id)
        profile = profiles.get(profile_id) if isinstance(profile_id, str) else None
        if not isinstance(profile, Mapping):
            raise WorkflowDefinitionError(
                f"no context budget exists for capability {capability_id}"
            )
        limit = profile.get("max_packet_chars")
        if not isinstance(limit, int) or limit <= 0:
            raise WorkflowDefinitionError("context packet limit is invalid")
        return limit


def coerce_workflow_instance(value: WorkflowInstance | Mapping[str, Any]) -> WorkflowInstance:
    if isinstance(value, WorkflowInstance):
        return value
    if not isinstance(value, Mapping):
        raise WorkflowDefinitionError("instance must be WorkflowInstance or mapping")
    return WorkflowInstance(**dict(value))


def coerce_transition(value: TransitionDefinition | Mapping[str, Any]) -> TransitionDefinition:
    if isinstance(value, TransitionDefinition):
        return value
    if not isinstance(value, Mapping):
        raise WorkflowDefinitionError("transition must be TransitionDefinition or mapping")
    return TransitionDefinition.from_mapping(value)


__all__ = [
    "ActorBinding",
    "ClauseBinding",
    "EvidenceSet",
    "RepositoryBinding",
    "TransitionDefinition",
    "WorkflowDefinitionError",
    "WorkflowDefinitions",
    "WorkflowInstance",
    "canonical_json",
    "coerce_transition",
    "coerce_workflow_instance",
    "sha256_bytes",
    "sha256_json",
    "sha256_text",
]
