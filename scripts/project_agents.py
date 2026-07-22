#!/usr/bin/env python3
"""Validate, compile, and resolve the six-slot Agent-Team topology."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
import sys
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from scripts.agent_team_layout import AgentTeamLayout
    from scripts.agent_team_profiles import (
        PROFESSIONAL_SKILL_ID,
        ProfileCategory,
        ProfessionalProfileCatalog,
        ProfileCatalogError,
    )
    from scripts.project_skills import (
        SkillConfigurationError,
        load_catalog,
        load_mcp_policy,
        resolve_selection,
        validate_catalog,
        validate_mcp_policy,
    )
except ModuleNotFoundError:
    from agent_team_layout import AgentTeamLayout
    from agent_team_profiles import (  # type: ignore[no-redef]
        PROFESSIONAL_SKILL_ID,
        ProfileCategory,
        ProfessionalProfileCatalog,
        ProfileCatalogError,
    )
    from project_skills import (  # type: ignore[no-redef]
        SkillConfigurationError,
        load_catalog,
        load_mcp_policy,
        resolve_selection,
        validate_catalog,
        validate_mcp_policy,
    )


LAYOUT = AgentTeamLayout.discover(Path(__file__))
PROJECT_ROOT = LAYOUT.source_root
TEAM_PATH = LAYOUT.team_path
ALLOWED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
ALLOWED_SANDBOX_MODES = {"read-only", "workspace-write"}
ALLOWED_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
REQUIRED_CAPABILITIES = {
    "pm",
    "ta",
    "pl",
    "developer",
    "qa_sdet",
    "build_release",
    "worker",
    "advisory",
}
REQUIRED_FIXED_SLOT_KEYS = {"pm_ta", "pl", "dev_1", "dev_2", "qa_build"}
REQUIRED_ELASTIC_SLOT_KEYS = {"elastic"}
HANGUL_PATTERN = re.compile(r"[\uac00-\ud7a3]")
KOREAN_NAME_PATTERN = re.compile(r"^[\uac00-\ud7a3]{2,4}$")
KOREAN_FAMILY_NAME_PATTERN = re.compile(r"^[\uac00-\ud7a3]{1,2}$")
KOREAN_GIVEN_NAME_PATTERN = re.compile(r"^[\uac00-\ud7a3]{2,3}$")
SEAT_ID_PATTERN = re.compile(
    r"^(?P<prefix>[A-Z][A-Z0-9_]*?)_(?P<display_name>[\uac00-\ud7a3]{2,4})$"
)
ASCII_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
MESSAGE_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TEMPLATE_FIELDS = {
    "contract_id",
    "contract_ref",
    "contract_sha256",
    "workflow_id",
    "workflow_version",
    "transition_id",
    "from_state",
    "to_state",
    "goal_id",
    "run_id",
    "activation_id",
    "slot_key",
    "actor_identity",
    "capability_id",
    "subject_oid",
    "transition_summary",
    "approval_authorities",
    "merge_control",
    "nested_spawn_allowed",
    "workspace_binding_markdown",
    "clauses_markdown",
    "profile_binding_markdown",
    "skill_bindings_markdown",
    "mcp_bindings_markdown",
    "evidence_requirements_markdown",
    "output_schema_ref",
    "idempotency_key",
}
DETERMINISTIC_SERVICE_IDENTITIES = frozenset(
    {
        "workspace-controller",
        "activation-controller",
        "integration-controller",
        "promotion-controller",
        "recovery-controller",
    }
)


class AgentConfigurationError(RuntimeError):
    """Raised when project-local agent configuration is invalid."""


def _inside_project(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise AgentConfigurationError(f"Path escapes the project root: {resolved}") from exc
    return resolved


def _read_toml(path: Path) -> dict[str, Any]:
    path = _inside_project(path)
    if not path.is_file():
        raise AgentConfigurationError(f"Configuration file not found: {path}")
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise AgentConfigurationError(f"Invalid TOML in {path}: {exc}") from exc


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AgentConfigurationError(f"File is not UTF-8: {path}") from exc


def _configured_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AgentConfigurationError(f"{label} must be a non-empty relative path.")
    logical = PurePosixPath(value.replace("\\", "/"))
    if (
        logical.is_absolute()
        or any(part in {"", ".", ".."} for part in logical.parts)
        or (logical.parts and ":" in logical.parts[0])
    ):
        raise AgentConfigurationError(f"{label} must be project-relative: {value}")
    if logical.parts[:2] == (".codex", "agents"):
        return _inside_project(PROJECT_ROOT.joinpath(*logical.parts))
    return LAYOUT.resolve_source_path(logical)


def _require_ascii_id(path: Path, label: str, value: Any) -> str:
    if not isinstance(value, str) or not ASCII_ID_PATTERN.fullmatch(value):
        raise AgentConfigurationError(f"Invalid {label} in {path}: {value!r}")
    return value


def _string_list(
    path: Path,
    label: str,
    value: Any,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise AgentConfigurationError(f"{label} must be a string list in {path}.")
    if any(not isinstance(item, str) or not item for item in value):
        raise AgentConfigurationError(f"{label} contains an invalid value in {path}.")
    if len(value) != len(set(value)):
        raise AgentConfigurationError(f"{label} contains duplicates in {path}.")
    return value


def _load_profiles(path: Path) -> dict[str, dict[str, str]]:
    profiles = _read_toml(path).get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise AgentConfigurationError(f"Missing [profiles] tables in {path}.")
    result: dict[str, dict[str, str]] = {}
    for profile_id, profile in profiles.items():
        _require_ascii_id(path, "profile id", profile_id)
        if not isinstance(profile, dict) or set(profile) != {
            "model",
            "model_reasoning_effort",
            "sandbox_mode",
        }:
            raise AgentConfigurationError(f"Invalid runtime profile '{profile_id}'.")
        if profile["model"] not in ALLOWED_MODELS:
            raise AgentConfigurationError(f"Invalid model for '{profile_id}'.")
        if profile["model_reasoning_effort"] not in ALLOWED_REASONING_EFFORTS:
            raise AgentConfigurationError(f"Invalid reasoning effort for '{profile_id}'.")
        if profile["sandbox_mode"] not in ALLOWED_SANDBOX_MODES:
            raise AgentConfigurationError(f"Invalid sandbox mode for '{profile_id}'.")
        result[profile_id] = dict(profile)
    return result


def _load_roles(paths: list[Path]) -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    claimed_capabilities: set[str] = set()
    for path in paths:
        role = _read_toml(path).get("role")
        if not isinstance(role, dict):
            raise AgentConfigurationError(f"Missing [role] table in {path}.")
        role_id = _require_ascii_id(path, "role id", role.get("id"))
        if path.stem.replace("-", "_") != role_id or role_id in roles:
            raise AgentConfigurationError(f"Invalid or duplicate role template: {role_id}")
        title = role.get("title")
        instructions = role.get("instructions")
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(instructions, str)
            or not instructions.strip()
            or HANGUL_PATTERN.search(title + instructions)
        ):
            raise AgentConfigurationError(f"Role prose must be non-empty English in {path}.")
        capability_ids = _string_list(path, "role_keys", role.get("role_keys"))
        overlap = claimed_capabilities.intersection(capability_ids)
        if overlap:
            raise AgentConfigurationError(f"Role capabilities overlap: {sorted(overlap)}")
        claimed_capabilities.update(capability_ids)
        authorities = _string_list(
            path,
            "approval_authorities",
            role.get("approval_authorities"),
            allow_empty=True,
        )
        accepted = _string_list(path, "accepted_message_types", role.get("accepted_message_types"))
        emitted = _string_list(path, "emitted_message_types", role.get("emitted_message_types"))
        if any(not MESSAGE_TYPE_PATTERN.fullmatch(item) for item in accepted + emitted):
            raise AgentConfigurationError(f"Invalid message type in {path}.")
        roles[role_id] = {
            "id": role_id,
            "title": title,
            "instructions": instructions.strip(),
            "capability_ids": capability_ids,
            "approval_authorities": authorities,
            "accepted_message_types": accepted,
            "emitted_message_types": emitted,
            "path": path,
        }
    return roles


def _load_capabilities(
    path: Path,
    roles: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    data = _read_toml(path)
    catalog = data.get("catalog")
    entries = data.get("capabilities")
    if not isinstance(catalog, dict) or catalog.get("version") != 1:
        raise AgentConfigurationError("Capability catalog version must be 1.")
    required_false = (
        "physical_identity_grants_authority",
        "profile_grants_authority",
        "skill_grants_authority",
        "model_elevation_allowed",
    )
    if (
        catalog.get("authority_source") != "active-logical-capability"
        or catalog.get("single_capability_per_activation") is not True
        or any(catalog.get(field) is not False for field in required_false)
    ):
        raise AgentConfigurationError("Capability authority boundaries are invalid.")
    if not isinstance(entries, list) or not entries:
        raise AgentConfigurationError("Capability definitions are missing.")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise AgentConfigurationError("Capability entries must be objects.")
        capability_id = _require_ascii_id(path, "capability id", entry.get("id"))
        if capability_id in result:
            raise AgentConfigurationError(f"Duplicate capability: {capability_id}")
        authorities = _string_list(
            path,
            f"{capability_id}.approval_authorities",
            entry.get("approval_authorities"),
            allow_empty=True,
        )
        eligible_slot_types = _string_list(
            path,
            f"{capability_id}.eligible_slot_types",
            entry.get("eligible_slot_types"),
        )
        if not set(eligible_slot_types) <= {"fixed", "elastic"}:
            raise AgentConfigurationError(f"Invalid slot type for {capability_id}.")
        for flag in ("merge_control", "source_write", "exact_oid_required", "nested_spawn_allowed"):
            if not isinstance(entry.get(flag), bool):
                raise AgentConfigurationError(f"{capability_id}.{flag} must be boolean.")
        if entry["nested_spawn_allowed"]:
            raise AgentConfigurationError("Nested spawn is prohibited for every capability.")
        role_template = entry.get("role_template")
        if role_template is not None:
            if role_template not in roles:
                raise AgentConfigurationError(f"Unknown role template: {role_template}")
            role = roles[role_template]
            if capability_id not in role["capability_ids"]:
                raise AgentConfigurationError(
                    f"Role template '{role_template}' does not own '{capability_id}'."
                )
            if authorities != role["approval_authorities"]:
                raise AgentConfigurationError(
                    f"Capability and role authorities differ for '{capability_id}'."
                )
        result[capability_id] = dict(entry)
    if set(result) != REQUIRED_CAPABILITIES:
        raise AgentConfigurationError(
            f"Capability set mismatch: {sorted(set(result) ^ REQUIRED_CAPABILITIES)}"
        )
    for capability_id in ("worker", "advisory"):
        capability = result[capability_id]
        if (
            capability["approval_authorities"]
            or capability["merge_control"]
            or capability.get("standing_approval_authority") is not False
            or capability["eligible_slot_types"] != ["elastic"]
        ):
            raise AgentConfigurationError(f"Elastic capability '{capability_id}' has authority.")
    return result


def _load_slots(
    path: Path,
    capabilities: dict[str, dict[str, Any]],
    profiles: dict[str, dict[str, str]],
) -> dict[str, dict[str, Any]]:
    data = _read_toml(path)
    topology = data.get("topology")
    entries = data.get("slots")
    expected_topology = {
        "version": 1,
        "topology_id": "six-slot-v1",
        "fixed_slot_count": 5,
        "elastic_slot_count": 1,
        "max_runtime_slots": 6,
        "single_active_capability_per_slot": True,
        "max_elastic_workers_per_goal_run": 1,
    }
    if topology != expected_topology:
        raise AgentConfigurationError("Six-slot topology metadata is invalid.")
    if not isinstance(entries, list) or len(entries) != 6:
        raise AgentConfigurationError("Exactly six runtime slots are required.")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise AgentConfigurationError("Slot entries must be objects.")
        slot_key = _require_ascii_id(path, "slot key", entry.get("slot_key"))
        slot_type = entry.get("slot_type")
        if slot_key in result or slot_type not in {"fixed", "elastic"}:
            raise AgentConfigurationError(f"Invalid or duplicate slot: {slot_key}")
        capability_ids = _string_list(path, f"{slot_key}.capabilities", entry.get("capabilities"))
        if not set(capability_ids) <= set(capabilities):
            raise AgentConfigurationError(f"Unknown capability in slot '{slot_key}'.")
        if any(slot_type not in capabilities[item]["eligible_slot_types"] for item in capability_ids):
            raise AgentConfigurationError(f"Capability slot-type mismatch in '{slot_key}'.")
        default_capability = entry.get("default_capability")
        profile_map = entry.get("capability_runtime_profiles")
        mutual = _string_list(
            path,
            f"{slot_key}.mutually_exclusive_capabilities",
            entry.get("mutually_exclusive_capabilities"),
            allow_empty=True,
        )
        if default_capability not in capability_ids or not isinstance(profile_map, dict):
            raise AgentConfigurationError(f"Invalid default/profile mapping for '{slot_key}'.")
        if set(profile_map) != set(capability_ids) or any(
            value not in profiles for value in profile_map.values()
        ):
            raise AgentConfigurationError(f"Invalid runtime profiles for '{slot_key}'.")
        if len(capability_ids) > 1 and set(mutual) != set(capability_ids):
            raise AgentConfigurationError(f"Merged slot '{slot_key}' lacks mutual exclusion.")
        if len(capability_ids) == 1 and mutual:
            raise AgentConfigurationError(f"Single-capability slot '{slot_key}' has mutual exclusion.")
        if slot_type == "fixed":
            prefix = entry.get("id_prefix")
            if (
                not isinstance(prefix, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]*", prefix)
                or entry.get("persistent_identity") is not True
            ):
                raise AgentConfigurationError(f"Invalid fixed identity for '{slot_key}'.")
        else:
            if (
                entry.get("persistent_identity") is not False
                or entry.get("max_active_workers_per_goal_run") != 1
                or entry.get("nested_spawn_allowed") is not False
                or entry.get("standing_approval_authority") is not False
            ):
                raise AgentConfigurationError("Elastic singleton boundaries are invalid.")
            sources = _string_list(
                path,
                "elastic.worker_definition_sources",
                entry.get("worker_definition_sources"),
            )
            if set(sources) != {"agents/custom-agents", "ordinary-subagent"}:
                raise AgentConfigurationError("Elastic worker definition sources are invalid.")
        result[slot_key] = {**entry, "capabilities": capability_ids}
    fixed = {key for key, value in result.items() if value["slot_type"] == "fixed"}
    elastic = {key for key, value in result.items() if value["slot_type"] == "elastic"}
    if fixed != REQUIRED_FIXED_SLOT_KEYS or elastic != REQUIRED_ELASTIC_SLOT_KEYS:
        raise AgentConfigurationError("Runtime slot keys do not match six-slot-v1.")
    assigned = {capability for slot in result.values() for capability in slot["capabilities"]}
    if assigned != REQUIRED_CAPABILITIES:
        raise AgentConfigurationError("Every logical capability must be assigned.")
    if sum("developer" in slot["capabilities"] for slot in result.values()) != 2:
        raise AgentConfigurationError("Exactly two fixed developer slots are required.")
    return result


def _load_name_pool(path: Path) -> dict[str, list[str]]:
    pool = _read_toml(path).get("name_pool")
    if not isinstance(pool, dict):
        raise AgentConfigurationError(f"Missing [name_pool] in {path}.")
    family = _string_list(path, "family_names", pool.get("family_names"))
    given = _string_list(path, "given_names", pool.get("given_names"))
    if any(not KOREAN_FAMILY_NAME_PATTERN.fullmatch(item) for item in family):
        raise AgentConfigurationError("Invalid Korean family name.")
    if any(not KOREAN_GIVEN_NAME_PATTERN.fullmatch(item) for item in given):
        raise AgentConfigurationError("Invalid Korean given name.")
    return {"family_names": family, "given_names": given}


def _load_registry(
    path: Path,
    slots: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    data = _read_toml(path)
    registry = data.get("registry")
    if (
        not isinstance(registry, dict)
        or registry.get("version") != 2
        or registry.get("topology_id") != "six-slot-v1"
        or registry.get("generator") != "scripts/project_agents.py"
        or not isinstance(registry.get("generated_at_utc"), str)
    ):
        raise AgentConfigurationError("Seat registry metadata is invalid.")
    seat_entries = data.get("seats")
    elastic_entries = data.get("elastic_slots")
    if not isinstance(seat_entries, list) or len(seat_entries) != 5:
        raise AgentConfigurationError("Registry must contain exactly five fixed seats.")
    if not isinstance(elastic_entries, list) or len(elastic_entries) != 1:
        raise AgentConfigurationError("Registry must contain exactly one elastic slot.")
    seats: dict[str, dict[str, Any]] = {}
    seen_slots: set[str] = set()
    seen_names: set[str] = set()
    absorbed_ids: set[str] = set()
    for entry in seat_entries:
        slot_key = entry.get("slot_key")
        slot = slots.get(slot_key)
        if slot is None or slot["slot_type"] != "fixed" or slot_key in seen_slots:
            raise AgentConfigurationError(f"Invalid fixed registry slot: {slot_key!r}")
        seat_id = entry.get("seat_id")
        display_name = entry.get("display_name")
        match = SEAT_ID_PATTERN.fullmatch(seat_id) if isinstance(seat_id, str) else None
        if (
            match is None
            or match.group("prefix") != slot["id_prefix"]
            or display_name != match.group("display_name")
            or not isinstance(display_name, str)
            or not KOREAN_NAME_PATTERN.fullmatch(display_name)
            or seat_id in seats
            or display_name in seen_names
        ):
            raise AgentConfigurationError(f"Invalid fixed seat identity: {seat_id!r}")
        legacy_keys = _string_list(path, "legacy_slot_keys", entry.get("legacy_slot_keys"))
        absorbed = _string_list(
            path,
            "absorbed_seat_ids",
            entry.get("absorbed_seat_ids"),
            allow_empty=True,
        )
        if absorbed_ids.intersection(absorbed):
            raise AgentConfigurationError("Absorbed seat aliases must be unique.")
        absorbed_ids.update(absorbed)
        seen_slots.add(slot_key)
        seen_names.add(display_name)
        seats[seat_id] = {
            **slot,
            **entry,
            "legacy_slot_keys": legacy_keys,
            "absorbed_seat_ids": absorbed,
            "enabled": True,
        }
    if seen_slots != REQUIRED_FIXED_SLOT_KEYS:
        raise AgentConfigurationError("Fixed registry coverage is incomplete.")
    elastic_entry = elastic_entries[0]
    if (
        elastic_entry.get("slot_key") != "elastic"
        or elastic_entry.get("slot_id") != "ELASTIC"
        or elastic_entry.get("worker_identity_mode") != "activation-pinned"
        or elastic_entry.get("max_active_workers_per_goal_run") != 1
        or elastic_entry.get("persistent_identity") is not False
        or elastic_entry.get("nested_spawn_allowed") is not False
        or elastic_entry.get("standing_approval_authority") is not False
    ):
        raise AgentConfigurationError("Elastic registry entry is invalid.")
    legacy = data.get("legacy")
    if (
        not isinstance(legacy, dict)
        or legacy.get("archived_seat_ids") != ["DEV_정예은"]
        or set(absorbed_ids) != {"TA_권지호", "BUILD_RELEASE_정서준"}
    ):
        raise AgentConfigurationError("Legacy seat mapping evidence is incomplete.")
    return seats, {"ELASTIC": {**slots["elastic"], **elastic_entry}}, legacy


def _load_context_policy(path: Path) -> dict[str, Any]:
    data = _read_toml(path)
    if data.get("context", {}).get("version") != 2:
        raise AgentConfigurationError("Context profile version must be 2.")
    expected_professional = {
        "runtime_skill_id": PROFESSIONAL_SKILL_ID,
        "compiled_binding_required": True,
        "minimum_reference_count": 4,
        "maximum_reference_count": 5,
        "model_selection_allowed": False,
        "authority_expansion_allowed": False,
    }
    if data.get("professional_profile") != expected_professional:
        raise AgentConfigurationError("Professional profile composition policy is invalid.")
    budget = data.get("contract_budget")
    if (
        not isinstance(budget, dict)
        or budget.get("formula") != "min(role_packet_limit * 0.25, 12000)"
        or budget.get("role_packet_ratio") != 0.25
        or budget.get("ceiling_chars") != 12000
        or budget.get("includes_rendered_contract") is not True
        or budget.get("includes_selected_clauses") is not True
    ):
        raise AgentConfigurationError("Contract budget metadata is invalid.")
    defaults = data.get("capability_defaults")
    profiles = data.get("profiles")
    if not isinstance(defaults, dict) or set(defaults) != REQUIRED_CAPABILITIES:
        raise AgentConfigurationError("Context capability defaults are incomplete.")
    if not isinstance(profiles, dict):
        raise AgentConfigurationError("Context profiles are missing.")
    covered = {
        capability
        for profile in profiles.values()
        if isinstance(profile, dict)
        for capability in profile.get("capabilities", [])
    }
    if covered != REQUIRED_CAPABILITIES:
        raise AgentConfigurationError("Context profiles do not cover every capability.")
    return data


def _validate_definition_layer(
    team: dict[str, Any],
    capabilities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    clause_path = _configured_path(team["clause_catalog"], "clause_catalog")
    clause_data = _read_toml(clause_path)
    clause_catalog = clause_data.get("catalog")
    clauses = clause_data.get("clauses")
    if (
        not isinstance(clause_catalog, dict)
        or clause_catalog.get("version") != 1
        or clause_catalog.get("budget_formula")
        != "min(role_packet_limit * 0.25, 12000)"
        or clause_catalog.get("budget_ratio") != 0.25
        or clause_catalog.get("budget_ceiling_chars") != 12000
        or not isinstance(clauses, list)
        or not clauses
    ):
        raise AgentConfigurationError("Clause catalog metadata is invalid.")
    clause_index: dict[str, dict[str, Any]] = {}
    for clause in clauses:
        if not isinstance(clause, dict):
            raise AgentConfigurationError("Clause entries must be objects.")
        clause_id = clause.get("id")
        text = clause.get("text")
        digest = clause.get("sha256")
        if (
            not isinstance(clause_id, str)
            or clause_id in clause_index
            or not isinstance(text, str)
            or not text
            or not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != digest
        ):
            raise AgentConfigurationError(f"Invalid clause definition: {clause_id!r}")
        clause_index[clause_id] = clause

    mcp_data = load_mcp_policy(_configured_path(team["mcp_policy"], "mcp_policy"))
    mcp = validate_mcp_policy(mcp_data, set(capabilities))
    workflow_catalog_path = _configured_path(team["workflow_catalog"], "workflow_catalog")
    workflow_catalog = _read_toml(workflow_catalog_path)
    catalog_meta = workflow_catalog.get("catalog")
    workflow_entries = workflow_catalog.get("workflows")
    if (
        not isinstance(catalog_meta, dict)
        or catalog_meta.get("version") != 1
        or catalog_meta.get("active_workflow_id") != "delivery-v4"
        or not isinstance(workflow_entries, list)
        or len(workflow_entries) != 1
    ):
        raise AgentConfigurationError("Workflow catalog metadata is invalid.")
    workflow_entry = workflow_entries[0]
    workflow_path = _configured_path(workflow_entry.get("path"), "workflow.path")
    observed = hashlib.sha256(workflow_path.read_bytes()).hexdigest()
    if (
        workflow_entry.get("id") != "delivery-v4"
        or workflow_entry.get("version") != "4.0.0"
        or workflow_entry.get("sha256") != observed
        or workflow_entry.get("active") is not True
    ):
        raise AgentConfigurationError("Workflow digest or registration is invalid.")
    workflow_data = _read_toml(workflow_path)
    workflow = workflow_data.get("workflow")
    states = workflow_data.get("states")
    transitions = workflow_data.get("transitions")
    if (
        not isinstance(workflow, dict)
        or workflow.get("id") != "delivery-v4"
        or workflow.get("version") != "4.0.0"
        or workflow.get("schema_version") != 4
        or workflow.get("contract_budget_formula")
        != "min(role_packet_limit * 0.25, 12000)"
        or workflow.get("contract_budget_ratio") != 0.25
        or workflow.get("contract_budget_ceiling_chars") != 12000
        or not isinstance(states, list)
        or not isinstance(transitions, list)
    ):
        raise AgentConfigurationError("Delivery workflow metadata is invalid.")
    state_ids = {state.get("id") for state in states if isinstance(state, dict)}
    if len(state_ids) != len(states) or workflow["initial_state"] not in state_ids:
        raise AgentConfigurationError("Workflow states are invalid.")
    transition_ids: set[str] = set()
    for transition in transitions:
        if not isinstance(transition, dict):
            raise AgentConfigurationError("Workflow transitions must be objects.")
        transition_id = transition.get("id")
        if not isinstance(transition_id, str) or transition_id in transition_ids:
            raise AgentConfigurationError("Workflow transition ids must be unique.")
        transition_ids.add(transition_id)
        from_states = (
            [transition["from_state"]]
            if "from_state" in transition
            else transition.get("from_states", [])
        )
        if not from_states or not set(from_states) <= state_ids:
            raise AgentConfigurationError(f"Transition '{transition_id}' has invalid source state.")
        if "to_state" in transition and transition["to_state"] not in state_ids:
            raise AgentConfigurationError(f"Transition '{transition_id}' has invalid target state.")
        if transition.get("failure_state") not in state_ids:
            raise AgentConfigurationError(f"Transition '{transition_id}' has invalid failure state.")
        if not set(transition.get("capabilities", [])) <= set(capabilities):
            raise AgentConfigurationError(f"Transition '{transition_id}' has invalid capability.")
        if not set(transition.get("clause_ids", [])) <= set(clause_index):
            raise AgentConfigurationError(f"Transition '{transition_id}' has unknown clause.")
        binding_ids = set(transition.get("mcp_availability_binding_ids", [])) | set(
            transition.get("mcp_required_use_binding_ids", [])
        )
        if not binding_ids <= set(mcp["required_use_bindings"]):
            raise AgentConfigurationError(f"Transition '{transition_id}' has unknown MCP binding.")
    required_transitions = {
        "pm_intake_goal",
        "pl_assign_implementation",
        "dev_submit_revision",
        "dev_submit_rework",
        "ta_review_exact_oid",
        "pl_merge_approved_oid",
        "qa_validate_integration",
        "build_validate_integration",
        "pm_accept_integration",
        "pl_issue_rework",
        "elastic_execute_bounded_task",
    }
    if transition_ids != required_transitions:
        raise AgentConfigurationError("Delivery workflow transition coverage is incomplete.")

    schema_paths: dict[str, Path] = {}
    for field in (
        "activation_contract_schema",
        "admission_receipt_schema",
        "activation_result_schema",
        "contract_violation_schema",
        "migration_manifest_schema",
    ):
        schema_path = _configured_path(team[field], field)
        try:
            schema = json.loads(_read_utf8(schema_path))
        except json.JSONDecodeError as exc:
            raise AgentConfigurationError(f"Invalid JSON Schema in {schema_path}: {exc}") from exc
        if (
            schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
            or schema.get("type") != "object"
            or schema.get("additionalProperties") is not False
            or not isinstance(schema.get("required"), list)
            or not schema["required"]
        ):
            raise AgentConfigurationError(f"JSON Schema is not strict: {schema_path}")
        schema_paths[field] = schema_path
    template_path = _configured_path(team["activation_packet_template"], "activation_packet_template")
    template = _read_utf8(template_path)
    observed_fields = set(re.findall(r"\{\{([a-z0-9_]+)\}\}", template))
    if observed_fields != TEMPLATE_FIELDS:
        raise AgentConfigurationError(
            f"Activation packet template field mismatch: {sorted(observed_fields ^ TEMPLATE_FIELDS)}"
        )
    return {
        "clauses": clause_index,
        "clause_catalog_path": clause_path,
        "mcp": mcp,
        "mcp_policy_path": _configured_path(team["mcp_policy"], "mcp_policy"),
        "workflow": workflow_data,
        "workflow_entry": workflow_entry,
        "workflow_path": workflow_path,
        "schema_paths": schema_paths,
        "template_path": template_path,
    }


def _load_static_configuration() -> dict[str, Any]:
    data = _read_toml(TEAM_PATH)
    team = data.get("team")
    if not isinstance(team, dict):
        raise AgentConfigurationError(f"Missing [team] table in {TEAM_PATH}.")
    required_team = {
        "version": 2,
        "scope": "project",
        "topology_id": "six-slot-v1",
        "fixed_seat_count": 5,
        "elastic_slot_count": 1,
        "max_runtime_slots": 6,
        "max_elastic_workers_per_goal_run": 1,
        "message_transport": "sqlite",
        "model_policy": "profile-pinned",
    }
    if any(team.get(key) != value for key, value in required_team.items()):
        raise AgentConfigurationError("Team six-slot metadata is invalid.")
    source_root = _configured_path(team.get("source_root"), "source_root")
    runtime_root = _configured_path(team.get("runtime_root"), "runtime_root")
    if source_root != LAYOUT.config_root.resolve():
        raise AgentConfigurationError("Canonical agent root does not match layout.")
    if runtime_root != (PROJECT_ROOT / ".codex" / "agents").resolve():
        raise AgentConfigurationError("Runtime agent root must be .codex/agents/.")
    role_values = team.get("role_files")
    if not isinstance(role_values, list) or not role_values:
        raise AgentConfigurationError("role_files must be a non-empty list.")
    role_paths = [_configured_path(value, "role_files entry") for value in role_values]
    roles = _load_roles(role_paths)
    profiles_path = _configured_path(team["runtime_profiles"], "runtime_profiles")
    profiles = _load_profiles(profiles_path)
    capability_path = _configured_path(team["capability_catalog"], "capability_catalog")
    capabilities = _load_capabilities(capability_path, roles)
    slots_path = _configured_path(team["seat_slots"], "seat_slots")
    slots = _load_slots(slots_path, capabilities, profiles)
    context_path = _configured_path(team["context_profiles"], "context_profiles")
    context_policy = _load_context_policy(context_path)
    profile_catalog_path = _configured_path(
        team["professional_profile_catalog"],
        "professional_profile_catalog",
    )
    try:
        profile_catalog = ProfessionalProfileCatalog.load(profile_catalog_path.parent)
        for capability_id in capabilities:
            profile_catalog.select(ProfileCategory.ROLE, capability_id)
    except (ProfileCatalogError, Exception) as exc:
        if isinstance(exc, AgentConfigurationError):
            raise
        raise AgentConfigurationError(str(exc)) from exc
    skill_data = load_catalog(_configured_path(team["skill_catalog"], "skill_catalog"))
    skill_index = validate_catalog(
        skill_data,
        mcp_policy=load_mcp_policy(_configured_path(team["mcp_policy"], "mcp_policy")),
    )
    if set(skill_data["catalog"]["capabilities"]) != set(capabilities):
        raise AgentConfigurationError("Skill capabilities must match logical capabilities.")
    definitions = _validate_definition_layer(team, capabilities)
    base = team.get("base_instructions")
    if not isinstance(base, str) or not base.strip() or HANGUL_PATTERN.search(base):
        raise AgentConfigurationError("base_instructions must be non-empty English.")
    required_markers = {
        "{{seat_id}}",
        "{{slot_key}}",
        "{{eligible_capabilities}}",
    }
    if not required_markers <= set(re.findall(r"\{\{[^}]+\}\}", base)):
        raise AgentConfigurationError("base_instructions placeholders are incomplete.")
    return {
        "team": team,
        "base_instructions": base.strip(),
        "source_root": source_root,
        "runtime_root": runtime_root,
        "registry_path": _configured_path(team["seat_registry"], "seat_registry"),
        "profiles": profiles,
        "roles": roles,
        "capabilities": capabilities,
        "slots": slots,
        "name_pool": _load_name_pool(_configured_path(team["name_pool"], "name_pool")),
        "context_profile_policy": context_policy,
        "professional_profile_catalog": profile_catalog,
        "professional_profile_catalog_path": profile_catalog_path,
        "skill_catalog": skill_data,
        "skill_index": skill_index,
        **definitions,
    }


def load_and_validate() -> dict[str, Any]:
    bundle = _load_static_configuration()
    seats, elastic_slots, legacy = _load_registry(
        bundle["registry_path"],
        bundle["slots"],
    )
    return {
        **bundle,
        "seats": seats,
        "elastic_slots": elastic_slots,
        "legacy_seat_mapping": legacy,
    }


def generate_unique_display_names(
    name_pool: dict[str, list[str]],
    count: int,
    rng: Any | None = None,
) -> list[str]:
    combinations = [
        f"{family}{given}"
        for family in name_pool["family_names"]
        for given in name_pool["given_names"]
    ]
    if len(combinations) < count:
        raise AgentConfigurationError("Korean name pool is too small.")
    return (rng or secrets.SystemRandom()).sample(combinations, count)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_registry(
    slots: dict[str, dict[str, Any]],
    display_names: list[str],
) -> str:
    fixed = [slot for slot in slots.values() if slot["slot_type"] == "fixed"]
    if len(fixed) != len(display_names):
        raise AgentConfigurationError("Fixed slot and name counts differ.")
    lines = [
        "[registry]",
        "version = 2",
        'topology_id = "six-slot-v1"',
        'generator = "scripts/project_agents.py"',
        f"generated_at_utc = {_toml_string(datetime.now(timezone.utc).isoformat())}",
        "",
    ]
    legacy_by_slot = {
        "pm_ta": (["pm", "ta"], ["TA_권지호"]),
        "pl": (["pl"], []),
        "dev_1": (["dev_1"], []),
        "dev_2": (["dev_2"], []),
        "qa_build": (["qa_sdet", "build_release"], ["BUILD_RELEASE_정서준"]),
    }
    for slot, display_name in zip(fixed, display_names, strict=True):
        legacy_keys, absorbed = legacy_by_slot[slot["slot_key"]]
        seat_id = f"{slot['id_prefix']}_{display_name}"
        lines.extend(
            [
                "[[seats]]",
                f"slot_key = {_toml_string(slot['slot_key'])}",
                f"seat_id = {_toml_string(seat_id)}",
                f"display_name = {_toml_string(display_name)}",
                f"legacy_slot_keys = {json.dumps(legacy_keys)}",
                f"absorbed_seat_ids = {json.dumps(absorbed, ensure_ascii=False)}",
                "",
            ]
        )
    lines.extend(
        [
            "[[elastic_slots]]",
            'slot_key = "elastic"',
            'slot_id = "ELASTIC"',
            'worker_identity_mode = "activation-pinned"',
            "max_active_workers_per_goal_run = 1",
            "persistent_identity = false",
            "nested_spawn_allowed = false",
            "standing_approval_authority = false",
            "",
            "[legacy]",
            'archived_seat_ids = ["DEV_정예은"]',
            'archive_reason = "third fixed developer seat removed by six-slot-v1 topology"',
            "",
        ]
    )
    return "\n".join(lines)


def initialize_seats(
    regenerate: bool = False,
    confirm_identity_reset: bool = False,
    rng: Any | None = None,
) -> tuple[dict[str, Any], bool]:
    bundle = _load_static_configuration()
    registry_path = bundle["registry_path"]
    if registry_path.exists() and not regenerate:
        return load_and_validate(), False
    if regenerate and not confirm_identity_reset:
        raise AgentConfigurationError(
            "Regeneration changes durable routing identities; confirm explicitly."
        )
    names = generate_unique_display_names(bundle["name_pool"], 5, rng)
    temporary = _inside_project(registry_path.with_suffix(".toml.tmp"))
    temporary.write_text(
        _render_registry(bundle["slots"], names),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(registry_path)
    return load_and_validate(), True


def _select_capability(
    seat: dict[str, Any],
    capability_id: str | None,
    *,
    allow_default: bool,
) -> str:
    if capability_id is None:
        if len(seat["capabilities"]) > 1 and not allow_default:
            raise AgentConfigurationError(
                f"Merged slot '{seat['slot_key']}' requires an explicit active capability."
            )
        capability_id = seat["default_capability"]
    if capability_id not in seat["capabilities"]:
        raise AgentConfigurationError(
            f"Capability '{capability_id}' is not eligible for slot '{seat['slot_key']}'."
        )
    return capability_id


def compile_runtime_agent(bundle: dict[str, Any], seat_id: str) -> str:
    seat = bundle["seats"].get(seat_id)
    if seat is None:
        raise AgentConfigurationError(f"Unknown fixed seat: {seat_id}")
    capability_id = _select_capability(seat, None, allow_default=True)
    profile = bundle["profiles"][seat["capability_runtime_profiles"][capability_id]]
    base = (
        bundle["base_instructions"]
        .replace("{{seat_id}}", seat_id)
        .replace("{{slot_key}}", seat["slot_key"])
        .replace("{{eligible_capabilities}}", ", ".join(seat["capabilities"]))
    )
    description = (
        f"Project-local fixed slot {seat['slot_key']}; activation must select exactly "
        "one eligible logical capability."
    )
    return (
        f"name = {_toml_string(seat_id)}\n"
        f"description = {_toml_string(description)}\n"
        f"model = {_toml_string(profile['model'])}\n"
        f"model_reasoning_effort = {_toml_string(profile['model_reasoning_effort'])}\n"
        f"sandbox_mode = {_toml_string(profile['sandbox_mode'])}\n"
        'developer_instructions = """\n'
        f"{base}\n"
        '"""\n'
    )


def _registry_retired_seat_ids(bundle: dict[str, Any]) -> list[str]:
    active_seat_ids = set(bundle["seats"])
    absorbed_seat_ids = {
        seat_id
        for seat in bundle["seats"].values()
        for seat_id in seat.get("absorbed_seat_ids", [])
    }
    archived_seat_ids = set(
        bundle.get("legacy_seat_mapping", {}).get("archived_seat_ids", [])
    )
    retired_seat_ids = absorbed_seat_ids | archived_seat_ids
    if any(
        not isinstance(seat_id, str) or SEAT_ID_PATTERN.fullmatch(seat_id) is None
        for seat_id in retired_seat_ids
    ):
        raise AgentConfigurationError("Registry contains an unsafe retired seat identity.")
    overlap = active_seat_ids.intersection(retired_seat_ids)
    if overlap:
        raise AgentConfigurationError(
            f"Active seats cannot be retired by synchronization: {sorted(overlap)}"
        )
    return sorted(retired_seat_ids)


def synchronize(bundle: dict[str, Any]) -> list[str]:
    runtime_root = bundle["runtime_root"]
    runtime_root.mkdir(parents=True, exist_ok=True)
    for seat_id in sorted(bundle["seats"]):
        target = _inside_project(runtime_root / f"{seat_id}.toml")
        target.write_text(
            compile_runtime_agent(bundle, seat_id),
            encoding="utf-8",
            newline="\n",
        )
    pruned: list[str] = []
    for seat_id in _registry_retired_seat_ids(bundle):
        target = _inside_project(runtime_root / f"{seat_id}.toml")
        if not target.exists():
            continue
        if not target.is_file():
            raise AgentConfigurationError(
                f"Refusing to prune non-file retired agent path: {target}"
            )
        try:
            target.unlink()
        except OSError as exc:
            raise AgentConfigurationError(
                f"Cannot prune registry-retired agent file: {target}: {exc}"
            ) from exc
        pruned.append(seat_id)
    return pruned


def list_seats(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for seat_id, seat in bundle["seats"].items():
        result.append(
            {
                "slot_key": seat["slot_key"],
                "slot_type": "fixed",
                "seat_id": seat_id,
                "display_name": seat["display_name"],
                "eligible_capabilities": list(seat["capabilities"]),
                "default_capability": seat["default_capability"],
                "capability_runtime_profiles": dict(
                    seat["capability_runtime_profiles"]
                ),
                "runtime_agent": f".codex/agents/{seat_id}.toml",
            }
        )
    elastic = bundle["elastic_slots"]["ELASTIC"]
    result.append(
        {
            "slot_key": "elastic",
            "slot_type": "elastic",
            "slot_id": "ELASTIC",
            "seat_id": None,
            "eligible_capabilities": list(elastic["capabilities"]),
            "max_active_workers_per_goal_run": 1,
            "standing_approval_authority": False,
            "nested_spawn_allowed": False,
            "runtime_agent": None,
        }
    )
    return result


def resolve_runtime_activation_contract(
    bundle: dict[str, Any],
    seat_id: str,
    capability_id: str | None = None,
) -> dict[str, Any]:
    seat = bundle["seats"].get(seat_id)
    if seat is None:
        raise AgentConfigurationError(f"Unknown fixed seat: {seat_id}")
    active = _select_capability(seat, capability_id, allow_default=False)
    capability = bundle["capabilities"][active]
    role = bundle["roles"].get(capability.get("role_template"))
    profile_id = seat["capability_runtime_profiles"][active]
    profile = bundle["profiles"][profile_id]
    return {
        "slot_key": seat["slot_key"],
        "slot_type": "fixed",
        "seat_id": seat_id,
        "active_capability": active,
        "eligible_capabilities": list(seat["capabilities"]),
        "mutually_exclusive_capabilities": list(
            seat["mutually_exclusive_capabilities"]
        ),
        "organizational_role": role["title"] if role else None,
        "approval_authorities": list(capability["approval_authorities"]),
        "merge_control": capability["merge_control"],
        "nested_spawn_allowed": False,
        "model_policy": bundle["team"]["model_policy"],
        "runtime_profile": profile_id,
        "model": profile["model"],
        "model_reasoning_effort": profile["model_reasoning_effort"],
        "static_sandbox_mode": profile["sandbox_mode"],
        "professional_skill_id": PROFESSIONAL_SKILL_ID,
        "service_identity": False,
        "dynamic_confinement_required": True,
        "dynamic_cwd_required": True,
        "dynamic_root_attestation_required": True,
    }


def resolve_binding(
    bundle: dict[str, Any],
    seat_id: str,
    skill_ids: list[str],
    capability_id: str | None = None,
    transition_id: str | None = None,
) -> dict[str, Any]:
    contract = resolve_runtime_activation_contract(bundle, seat_id, capability_id)
    try:
        skills = resolve_selection(
            bundle["skill_catalog"],
            bundle["skill_index"],
            contract["active_capability"],
            skill_ids,
            transition_id=transition_id,
            mcp_policy=load_mcp_policy(bundle["mcp_policy_path"]),
        )
    except SkillConfigurationError as exc:
        raise AgentConfigurationError(str(exc)) from exc
    return {
        "scope": "project",
        "seat_id": seat_id,
        "slot_key": contract["slot_key"],
        "active_capability": contract["active_capability"],
        "agent_file": f".codex/agents/{seat_id}.toml",
        "runtime_activation_contract": contract,
        "professional_profile_policy": {
            "skill_id": PROFESSIONAL_SKILL_ID,
            "catalog": LAYOUT.logical_path(
                bundle["professional_profile_catalog_path"]
            ).as_posix(),
            "model_invariant": True,
            "authority_invariant": True,
        },
        "skill_packet": skills,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the project-local six-slot Agent-Team topology."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    regenerate = subparsers.add_parser("regenerate")
    regenerate.add_argument("--confirm-identity-reset", action="store_true")
    subparsers.add_parser("validate")
    subparsers.add_parser("sync")
    subparsers.add_parser("list")
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--seat", required=True)
    resolve.add_argument("--capability", required=True)
    resolve.add_argument("--transition")
    resolve.add_argument("--skill", action="append", dest="skills")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "init":
            bundle, created = initialize_seats()
            print(
                f"{'Generated' if created else 'Kept existing'} "
                f"{len(bundle['seats'])} fixed seat identities."
            )
        elif args.command == "regenerate":
            bundle, _ = initialize_seats(
                regenerate=True,
                confirm_identity_reset=args.confirm_identity_reset,
            )
            print(f"Regenerated {len(bundle['seats'])} fixed seat identities.")
        else:
            bundle = load_and_validate()
        if args.command == "validate":
            print(
                "Validated six-slot-v1: 5 fixed seats, 1 elastic slot, "
                f"{len(bundle['capabilities'])} logical capabilities, "
                f"{len(bundle['workflow']['transitions'])} workflow transitions."
            )
        elif args.command == "sync":
            pruned = synchronize(bundle)
            print(
                f"Synchronized {len(bundle['seats'])} fixed Codex agents; "
                f"pruned {len(pruned)} registry-retired agents."
            )
        elif args.command == "list":
            print(json.dumps(list_seats(bundle), ensure_ascii=False, indent=2))
        elif args.command == "resolve":
            print(
                json.dumps(
                    resolve_binding(
                        bundle,
                        args.seat,
                        args.skills or [],
                        capability_id=args.capability,
                        transition_id=args.transition,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    except (AgentConfigurationError, SkillConfigurationError) as exc:
        print(f"Agent configuration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
