#!/usr/bin/env python3
"""Manage PL-owned Serena project knowledge and compact semantic evidence."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from .agent_team_layout import AgentTeamLayout
    from .agent_team_queue import SQLiteMessageQueue
    from .project_agents import AgentConfigurationError, load_and_validate
except ImportError:
    from agent_team_layout import AgentTeamLayout
    from agent_team_queue import SQLiteMessageQueue
    from project_agents import AgentConfigurationError, load_and_validate


LAYOUT = AgentTeamLayout.discover(Path(__file__))
PROJECT_ROOT = LAYOUT.source_root
DEFAULT_POLICY_PATH = LAYOUT.config_root / "serena-knowledge-policy.toml"
SERENA_CONFIG_RELATIVE_PATH = Path(".serena") / "project.yml"
SERENA_MEMORY_RELATIVE_PATH = Path(".serena") / "memories"
SUPPORTED_EVIDENCE_KINDS = frozenset(
    {
        "project_overview",
        "symbol_overview",
        "reference_trace",
        "impact_summary",
        "module_map",
    }
)
SUPPORTED_CONFIDENCE = frozenset({"confirmed", "likely", "unknown"})
EVIDENCE_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_-]{2,119}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_OID_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")


class ProjectKnowledgeError(RuntimeError):
    """Raised when project knowledge lifecycle input is invalid or incomplete."""


@dataclass(frozen=True)
class SerenaKnowledgePolicy:
    policy_path: Path
    policy_sha256: str
    owner_role: str
    owner_capability: str
    architecture_evidence_role: str
    state_store: str
    allow_all_roles_read: bool
    allow_all_roles_semantic_exploration: bool
    shared_memory_publish_mode: str
    required_memory_names: tuple[str, ...]
    initialize_memory_layout: bool
    require_language_configuration: bool
    apply_on: str
    auto_write: bool
    require_memory_check: bool
    accepted_decisions: tuple[str, ...]
    trigger_paths: tuple[str, ...]
    max_memory_refs: int
    max_semantic_items: int
    max_semantic_chars: int
    require_target_oid: bool
    role_memory_refs: dict[str, tuple[str, ...]]
    initial_instructions_required: bool
    source_oid_match_required: bool
    developer_consumption_before_mutation: bool
    refresh_trigger_ids: tuple[str, ...]
    maximum_age_hours: int
    allowed_content_classes: tuple[str, ...]
    prohibited_terms: tuple[str, ...]
    transition_memory_refs: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class SerenaMemoryReference:
    name: str
    memory_ref: str
    sha256: str


@dataclass(frozen=True)
class SerenaOnboardingSnapshot:
    snapshot_id: str
    repository_id: str
    source_oid: str
    policy_sha256: str
    memory_bindings: tuple[SerenaMemoryReference, ...]
    evidence_refs: tuple[str, ...]
    trigger_ids: tuple[str, ...]
    initial_instructions_receipt_sha256: str
    refreshed: bool
    state_store: Any = field(default=None, repr=False, compare=False)

    def as_contract_binding(
        self, *, consumption_required: bool
    ) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "source_oid": self.source_oid,
            "policy_sha256": self.policy_sha256,
            "memory_bindings": [
                {"name": item.name, "sha256": item.sha256}
                for item in self.memory_bindings
            ],
            "consumption_receipt_required": consumption_required,
        }


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ProjectKnowledgeError(f"Knowledge policy is missing: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ProjectKnowledgeError(f"Knowledge policy is invalid TOML: {path}: {exc}") from exc


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectKnowledgeError(f"{label} must be a non-empty string")
    return value


def _require_string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ProjectKnowledgeError(f"{label} must be a non-empty string array")
    if not all(isinstance(item, str) and item for item in value):
        raise ProjectKnowledgeError(f"{label} contains an invalid item")
    if len(value) != len(set(value)):
        raise ProjectKnowledgeError(f"{label} contains duplicate items")
    return tuple(value)


def _require_positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or value < 1:
        raise ProjectKnowledgeError(f"{label} must be a positive integer")
    return value


def load_policy(path: str | Path = DEFAULT_POLICY_PATH) -> SerenaKnowledgePolicy:
    """Load the project-local policy that separates shared access from PL ownership."""
    policy_path = Path(path).expanduser().resolve()
    data = _read_toml(policy_path)
    knowledge = data.get("knowledge")
    bootstrap = data.get("bootstrap")
    refresh = data.get("refresh")
    detection = data.get("detection")
    context = data.get("context")
    role_memory_refs = data.get("role_memory_refs")
    onboarding = data.get("onboarding")
    freshness = data.get("freshness")
    content_boundary = data.get("content_boundary")
    transition_memory_refs = data.get("transition_memory_refs")
    if not all(
        isinstance(section, dict)
        for section in (
            knowledge,
            bootstrap,
            refresh,
            detection,
            context,
            role_memory_refs,
            onboarding,
            freshness,
            content_boundary,
            transition_memory_refs,
        )
    ):
        raise ProjectKnowledgeError("Knowledge policy is missing a required table")
    if knowledge.get("version") != 2:
        raise ProjectKnowledgeError("Knowledge policy version must be 2")
    if knowledge.get("state_store") != "sqlite":
        raise ProjectKnowledgeError("Knowledge policy state_store must be 'sqlite'")
    if knowledge.get("shared_memory_publish_mode") != "pl_acknowledged":
        raise ProjectKnowledgeError(
            "Knowledge policy shared_memory_publish_mode must be 'pl_acknowledged'"
        )
    if refresh.get("apply_on") != "approved_integration_oid":
        raise ProjectKnowledgeError(
            "Knowledge policy refresh.apply_on must be 'approved_integration_oid'"
        )
    if refresh.get("auto_write") is not False:
        raise ProjectKnowledgeError("Knowledge policy refresh.auto_write must be false")
    if not isinstance(knowledge.get("allow_all_roles_read"), bool):
        raise ProjectKnowledgeError("knowledge.allow_all_roles_read must be a boolean")
    if not isinstance(knowledge.get("allow_all_roles_semantic_exploration"), bool):
        raise ProjectKnowledgeError(
            "knowledge.allow_all_roles_semantic_exploration must be a boolean"
        )
    if not isinstance(bootstrap.get("initialize_memory_layout"), bool):
        raise ProjectKnowledgeError("bootstrap.initialize_memory_layout must be a boolean")
    if not isinstance(bootstrap.get("require_language_configuration"), bool):
        raise ProjectKnowledgeError(
            "bootstrap.require_language_configuration must be a boolean"
        )
    if not isinstance(refresh.get("require_memory_check"), bool):
        raise ProjectKnowledgeError("refresh.require_memory_check must be a boolean")
    if not isinstance(context.get("require_target_oid"), bool):
        raise ProjectKnowledgeError("context.require_target_oid must be a boolean")

    refs: dict[str, tuple[str, ...]] = {}
    for role, names in role_memory_refs.items():
        refs[_require_string(role, "role_memory_refs key")] = _require_string_list(
            names, f"role_memory_refs.{role}"
        )

    required_memory_names = _require_string_list(
        bootstrap.get("required_memory_names"), "bootstrap.required_memory_names"
    )
    known_memory_names = set(required_memory_names)
    unknown_refs = {
        name
        for names in refs.values()
        for name in names
        if name not in known_memory_names
    }
    if unknown_refs:
        raise ProjectKnowledgeError(
            "role_memory_refs must use required memory names: "
            f"{sorted(unknown_refs)}"
        )

    accepted_decisions = _require_string_list(
        refresh.get("accepted_decisions"), "refresh.accepted_decisions"
    )
    if set(accepted_decisions) != {"refreshed", "no_change", "deferred"}:
        raise ProjectKnowledgeError(
            "refresh.accepted_decisions must contain refreshed, no_change, and deferred"
        )

    transition_refs: dict[str, tuple[str, ...]] = {}
    for transition_id, names in transition_memory_refs.items():
        transition_refs[_require_string(transition_id, "transition_memory_refs key")] = (
            _require_string_list(names, f"transition_memory_refs.{transition_id}")
        )
    unknown_transition_refs = {
        name
        for names in transition_refs.values()
        for name in names
        if name not in known_memory_names
    }
    if unknown_transition_refs:
        raise ProjectKnowledgeError(
            f"transition memory refs are unknown: {sorted(unknown_transition_refs)}"
        )
    if onboarding.get("initial_instructions_required") is not True:
        raise ProjectKnowledgeError("Serena initial_instructions must be required")
    if onboarding.get("source_oid_match_required") is not True:
        raise ProjectKnowledgeError("Serena snapshots must match the source OID")
    if onboarding.get("developer_consumption_before_mutation") is not True:
        raise ProjectKnowledgeError("developer consumption-before-mutation is required")

    return SerenaKnowledgePolicy(
        policy_path=policy_path,
        policy_sha256=hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        owner_role=_require_string(knowledge.get("owner_role"), "knowledge.owner_role"),
        owner_capability=_require_string(
            knowledge.get("owner_capability"), "knowledge.owner_capability"
        ),
        architecture_evidence_role=_require_string(
            knowledge.get("architecture_evidence_role"),
            "knowledge.architecture_evidence_role",
        ),
        state_store=knowledge["state_store"],
        allow_all_roles_read=knowledge["allow_all_roles_read"],
        allow_all_roles_semantic_exploration=knowledge[
            "allow_all_roles_semantic_exploration"
        ],
        shared_memory_publish_mode=knowledge["shared_memory_publish_mode"],
        required_memory_names=required_memory_names,
        initialize_memory_layout=bootstrap["initialize_memory_layout"],
        require_language_configuration=bootstrap["require_language_configuration"],
        apply_on=refresh["apply_on"],
        auto_write=refresh["auto_write"],
        require_memory_check=refresh["require_memory_check"],
        accepted_decisions=accepted_decisions,
        trigger_paths=_require_string_list(detection.get("trigger_paths"), "detection.trigger_paths"),
        max_memory_refs=_require_positive_integer(
            context.get("max_memory_refs"), "context.max_memory_refs"
        ),
        max_semantic_items=_require_positive_integer(
            context.get("max_semantic_items"), "context.max_semantic_items"
        ),
        max_semantic_chars=_require_positive_integer(
            context.get("max_semantic_chars"), "context.max_semantic_chars"
        ),
        require_target_oid=context["require_target_oid"],
        role_memory_refs=refs,
        initial_instructions_required=True,
        source_oid_match_required=True,
        developer_consumption_before_mutation=True,
        refresh_trigger_ids=_require_string_list(
            freshness.get("trigger_ids"), "freshness.trigger_ids"
        ),
        maximum_age_hours=_require_positive_integer(
            freshness.get("maximum_age_hours"), "freshness.maximum_age_hours"
        ),
        allowed_content_classes=_require_string_list(
            content_boundary.get("allowed_content_classes"),
            "content_boundary.allowed_content_classes",
        ),
        prohibited_terms=_require_string_list(
            content_boundary.get("prohibited_terms"),
            "content_boundary.prohibited_terms",
        ),
        transition_memory_refs=transition_refs,
    )


def required_memories_for_transition(
    transition_id: str,
    *,
    policy_path: str | Path = DEFAULT_POLICY_PATH,
) -> tuple[str, ...]:
    """Return the policy-pinned minimum named memories for one transition."""

    transition_id = _require_string(transition_id, "transition_id")
    policy = load_policy(policy_path)
    try:
        return policy.transition_memory_refs[transition_id]
    except KeyError as exc:
        raise ProjectKnowledgeError(
            f"transition has no Serena memory selection: {transition_id}"
        ) from exc


def _evidence_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise ProjectKnowledgeError("Serena onboarding evidence must be an object")


def _repository_values(repo: Any) -> tuple[str, str, Any]:
    if isinstance(repo, Mapping):
        repository_id = repo.get("repository_id") or repo.get("repo_id")
        source_oid = repo.get("source_oid")
        state_store = repo.get("state_store")
    else:
        repository_id = getattr(repo, "repository_id", None) or getattr(
            repo, "repo_id", None
        )
        source_oid = getattr(repo, "source_oid", None)
        state_store = getattr(repo, "state_store", None)
    repository_id = _require_string(repository_id, "repository_id")
    if not isinstance(source_oid, str) or not _OID_PATTERN.fullmatch(source_oid):
        raise ProjectKnowledgeError("repository source_oid must be a full lowercase OID")
    return repository_id, source_oid, state_store


def _prohibited_memory_reference(reference: str) -> bool:
    normalized = reference.replace("\\", "/").casefold()
    return (
        normalized in {"*", "all"}
        or normalized.startswith("docs/")
        or "://docs/" in normalized
        or "/docs/" in normalized
    )


def _validate_shared_memory_content(
    *,
    name: str,
    content: str,
    content_class: str,
    policy: SerenaKnowledgePolicy,
) -> None:
    if content_class == "rapid_code_summary":
        raise ProjectKnowledgeError(
            f"{name} is rapidly changing code knowledge; store it as an activation artifact"
        )
    if content_class not in policy.allowed_content_classes:
        raise ProjectKnowledgeError(f"unsupported Serena content class: {content_class}")
    lowered = content.casefold()
    for term in policy.prohibited_terms:
        if term.casefold() in lowered:
            raise ProjectKnowledgeError(
                f"shared Serena memory contains prohibited live/team state: {term}"
            )
    if re.search(r"(?<![0-9a-f])[0-9a-f]{40,64}(?![0-9a-f])", lowered):
        raise ProjectKnowledgeError("shared Serena memory must not contain a current Git OID")


def ensure_serena_onboarding(
    repo: Any,
    evidence: Any,
    required_memories: tuple[str, ...],
    *,
    policy_path: str | Path = DEFAULT_POLICY_PATH,
) -> SerenaOnboardingSnapshot:
    """Validate and persist one PL-owned, source-pinned onboarding snapshot."""

    policy = load_policy(policy_path)
    repository_id, source_oid, state_store = _repository_values(repo)
    values = _evidence_mapping(evidence)
    if values.get("publisher_capability") != policy.owner_capability:
        raise ProjectKnowledgeError("only the PL capability may publish shared Serena memory")
    if values.get("source_oid") != source_oid:
        raise ProjectKnowledgeError("Serena onboarding evidence source OID changed")
    if values.get("policy_sha256") != policy.policy_sha256:
        raise ProjectKnowledgeError("Serena onboarding policy digest changed")
    if not required_memories or len(required_memories) != len(set(required_memories)):
        raise ProjectKnowledgeError("required_memories must be a unique non-empty tuple")
    unknown = set(required_memories) - set(policy.required_memory_names)
    if unknown:
        raise ProjectKnowledgeError(f"unknown required Serena memories: {sorted(unknown)}")
    if len(required_memories) > policy.max_memory_refs:
        raise ProjectKnowledgeError("required Serena memories exceed the minimum-reference cap")
    transition_id = values.get("transition_id")
    if transition_id is not None:
        expected_memories = policy.transition_memory_refs.get(str(transition_id))
        if expected_memories is None:
            raise ProjectKnowledgeError(
                f"transition has no Serena memory selection: {transition_id}"
            )
        if tuple(required_memories) != expected_memories:
            raise ProjectKnowledgeError(
                "required_memories must equal the transition-specific minimum"
            )

    initial = values.get("initial_instructions")
    if not isinstance(initial, Mapping):
        raise ProjectKnowledgeError("initial_instructions evidence is required")
    if (
        initial.get("tool_name") != "initial_instructions"
        or initial.get("available") is not True
        or initial.get("invoked") is not True
        or not isinstance(initial.get("evidence_sha256"), str)
        or not _SHA256_PATTERN.fullmatch(initial["evidence_sha256"])
    ):
        raise ProjectKnowledgeError(
            "initial_instructions must be available, invoked, and digest receipted"
        )

    raw_memories = values.get("memory_bindings")
    if not isinstance(raw_memories, list):
        raise ProjectKnowledgeError("memory_bindings must be an array")
    memory_index: dict[str, Mapping[str, Any]] = {}
    for raw in raw_memories:
        if not isinstance(raw, Mapping):
            raise ProjectKnowledgeError("memory binding must be an object")
        name = raw.get("name") or raw.get("memory_name")
        if (
            not isinstance(name, str)
            or name not in policy.required_memory_names
            or name in {"*", "all"}
            or name in memory_index
        ):
            raise ProjectKnowledgeError("memory names must be unique")
        memory_index[name] = raw
    missing = set(required_memories) - set(memory_index)
    trigger_ids = set(values.get("trigger_ids", []))
    if values.get("previous_snapshot_id") is None:
        trigger_ids.add("new-repository")
    if missing:
        trigger_ids.add("missing-required-memory")
    if (
        values.get("material_project_change") is True
        or values.get("material_config_change") is True
        or values.get("source_fingerprint_changed") is True
        or (
            values.get("previous_source_oid") is not None
            and values.get("previous_source_oid") != source_oid
        )
        or (
            values.get("previous_policy_sha256") is not None
            and values.get("previous_policy_sha256") != policy.policy_sha256
        )
    ):
        trigger_ids.add("material-project-change")
    snapshot_age_hours = values.get("snapshot_age_hours")
    if snapshot_age_hours is not None and (
        not isinstance(snapshot_age_hours, (int, float))
        or isinstance(snapshot_age_hours, bool)
        or snapshot_age_hours < 0
    ):
        raise ProjectKnowledgeError("snapshot_age_hours must be non-negative")
    if values.get("stale") is True or (
        snapshot_age_hours is not None
        and snapshot_age_hours > policy.maximum_age_hours
    ):
        trigger_ids.add("stale-project-knowledge")
    unknown_triggers = trigger_ids - set(policy.refresh_trigger_ids)
    if unknown_triggers:
        raise ProjectKnowledgeError(f"unknown onboarding triggers: {sorted(unknown_triggers)}")
    if trigger_ids and values.get("refresh_completed") is not True:
        raise ProjectKnowledgeError(
            f"Serena onboarding refresh is required for: {sorted(trigger_ids)}"
        )
    if missing:
        raise ProjectKnowledgeError(f"required Serena memories are missing: {sorted(missing)}")

    selected: list[SerenaMemoryReference] = []
    selected_references: set[str] = set()
    for name in required_memories:
        raw = memory_index[name]
        reference = raw.get("ref") or raw.get("memory_ref")
        digest = raw.get("sha256") or raw.get("memory_sha256")
        content = raw.get("content")
        content_class = raw.get("content_class")
        if (
            not isinstance(reference, str)
            or not reference
            or _prohibited_memory_reference(reference)
            or reference in selected_references
            or not isinstance(content, str)
            or not isinstance(digest, str)
            or not _SHA256_PATTERN.fullmatch(digest)
            or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest
        ):
            raise ProjectKnowledgeError(f"Serena memory binding is invalid: {name}")
        _validate_shared_memory_content(
            name=name,
            content=content,
            content_class=str(content_class),
            policy=policy,
        )
        selected.append(SerenaMemoryReference(name, reference, digest))
        selected_references.add(reference)
    evidence_refs = _require_string_list(values.get("evidence_refs"), "evidence_refs")
    memory_bindings = [
        {
            "memory_name": item.name,
            "memory_ref": item.memory_ref,
            "memory_sha256": item.sha256,
        }
        for item in selected
    ]
    if state_store is not None:
        snapshot_id = state_store.record_serena_onboarding_snapshot(
            repo_id=repository_id,
            source_oid=source_oid,
            policy_digest=policy.policy_sha256,
            memory_bindings=memory_bindings,
        )
    else:
        manifest_digest = hashlib.sha256(
            json.dumps(memory_bindings, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        snapshot_id = f"serena-snapshot-{manifest_digest[:32]}"
    previous_snapshot_id = values.get("previous_snapshot_id")
    if (
        previous_snapshot_id is not None
        and not trigger_ids
        and snapshot_id != previous_snapshot_id
    ):
        raise ProjectKnowledgeError(
            "fresh Serena snapshot bytes changed without a refresh trigger"
        )
    return SerenaOnboardingSnapshot(
        snapshot_id=snapshot_id,
        repository_id=repository_id,
        source_oid=source_oid,
        policy_sha256=policy.policy_sha256,
        memory_bindings=tuple(selected),
        evidence_refs=evidence_refs,
        trigger_ids=tuple(sorted(trigger_ids)),
        initial_instructions_receipt_sha256=initial["evidence_sha256"],
        refreshed=bool(trigger_ids),
        state_store=state_store,
    )


def _command_path(command: str) -> str:
    executable = shutil.which(command)
    if executable is None:
        raise ProjectKnowledgeError(f"Required command is unavailable: {command}")
    return executable


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.setdefault("PYTHONUTF8", "1")
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ProjectKnowledgeError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n{detail}"
        )
    return completed


def _global_serena_config_path() -> Path:
    profile_root = os.environ.get("USERPROFILE")
    if profile_root:
        return Path(profile_root) / ".serena" / "serena_config.yml"
    return Path.home() / ".serena" / "serena_config.yml"


def _require_serena_setup(project: Path) -> str:
    executable = _command_path("serena")
    config_path = _global_serena_config_path()
    if not config_path.is_file():
        raise ProjectKnowledgeError(
            "Serena CLI configuration is missing. Run the project-local "
            "serena-project-setup skill before the project knowledge lifecycle."
        )
    project_config = project / SERENA_CONFIG_RELATIVE_PATH
    if not project_config.is_file():
        raise ProjectKnowledgeError(
            "Serena project configuration is missing. Run the project-local "
            "serena-project-setup skill before the project knowledge lifecycle."
        )
    maintenance = project / SERENA_MEMORY_RELATIVE_PATH / "memory_maintenance.md"
    if not maintenance.is_file():
        raise ProjectKnowledgeError(
            "Serena memory layout is missing. Run 'serena memories initialize' "
            "through the project-local serena-project-setup skill."
        )
    return executable


def _normalize_relative_path(value: str) -> str:
    raw = value.replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise ProjectKnowledgeError(
            f"Path must be a normalized repository-relative path: {value!r}"
        )
    return path.as_posix()


def _git(repo: Path, *arguments: str, check: bool = True) -> str:
    completed = _run(["git", "-C", str(repo), *arguments], cwd=repo, check=check)
    return completed.stdout


def _resolve_commit(repo: Path, oid: str) -> str:
    if not isinstance(oid, str) or not oid:
        raise ProjectKnowledgeError("Git OID must be a non-empty string")
    resolved = _git(repo, "rev-parse", "--verify", f"{oid}^{{commit}}")
    resolved = resolved.strip()
    if not resolved:
        raise ProjectKnowledgeError(f"Git OID cannot be resolved: {oid}")
    return resolved


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ProjectKnowledgeError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _matches_policy_pattern(path: str, pattern: str) -> bool:
    normalized = _normalize_relative_path(path)
    candidates = {pattern}
    if pattern.startswith("**/"):
        candidates.add(pattern[3:])
    return any(
        fnmatch.fnmatchcase(normalized, candidate)
        or PurePosixPath(normalized).match(candidate)
        for candidate in candidates
    )


def _configured_paths_at_oid(
    repo: Path,
    oid: str,
    policy: SerenaKnowledgePolicy,
) -> list[str]:
    files = _git(repo, "ls-tree", "-r", "--name-only", oid).splitlines()
    return [
        path
        for path in files
        if any(_matches_policy_pattern(path, pattern) for pattern in policy.trigger_paths)
    ]


def _source_fingerprint(
    repo: Path,
    oid: str,
    policy: SerenaKnowledgePolicy,
) -> tuple[str, list[str]]:
    paths = _configured_paths_at_oid(repo, oid, policy)
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(_git_bytes(repo, "show", f"{oid}:{path}")).digest())
        digest.update(b"\0")
    return digest.hexdigest(), paths


def _changed_paths(
    repo: Path,
    base_oid: str,
    head_oid: str,
) -> list[dict[str, str]]:
    output = _git(repo, "diff", "--name-status", "--find-renames", base_oid, head_oid)
    changes: list[dict[str, str]] = []
    for line in output.splitlines():
        fields = line.split("\t")
        if not fields or not fields[0]:
            continue
        status = fields[0]
        if status.startswith(("R", "C")) and len(fields) >= 3:
            changes.append({"status": status, "old_path": fields[1], "path": fields[2]})
        elif len(fields) >= 2:
            changes.append({"status": status, "path": fields[1]})
    return changes


def _triggered_changes(
    changes: Sequence[Mapping[str, str]], policy: SerenaKnowledgePolicy
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for change in changes:
        candidate_paths = [change["path"]]
        if change.get("old_path"):
            candidate_paths.append(change["old_path"])
        if any(
            _matches_policy_pattern(path, pattern)
            for path in candidate_paths
            for pattern in policy.trigger_paths
        ):
            result.append(dict(change))
    return result


def _project_memory_names(project: Path) -> list[str]:
    memory_root = project / SERENA_MEMORY_RELATIVE_PATH
    if not memory_root.is_dir():
        return []
    return sorted(
        path.relative_to(memory_root).with_suffix("").as_posix()
        for path in memory_root.rglob("*.md")
    )


def _detect_refresh(
    *,
    queue: SQLiteMessageQueue,
    repo_id: str,
    repo: Path,
    head_oid: str,
    policy: SerenaKnowledgePolicy,
    base_oid: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    inspected_oid = _resolve_commit(repo, head_oid)
    state = queue.get_project_knowledge_state(repo_id)
    baseline_oid = _resolve_commit(repo, base_oid) if base_oid else None
    if baseline_oid is None and state and state.baseline_oid:
        baseline_oid = _resolve_commit(repo, state.baseline_oid)

    fingerprint, configured_paths = _source_fingerprint(repo, inspected_oid, policy)
    if baseline_oid is None:
        affected_paths = [{"status": "A", "path": path} for path in configured_paths]
        trigger_kind = "initial_onboarding"
        required = True
    else:
        affected_paths = _triggered_changes(
            _changed_paths(repo, baseline_oid, inspected_oid), policy
        )
        fingerprint_changed = (
            state is not None
            and state.source_fingerprint is not None
            and state.source_fingerprint != fingerprint
        )
        if affected_paths or fingerprint_changed:
            trigger_kind = "configuration_change"
            required = True
        else:
            trigger_kind = "no_change"
            required = False
    if state is None or state.state in {"new", "refresh_required", "deferred"}:
        required = True
        if trigger_kind == "no_change":
            trigger_kind = "knowledge_state_incomplete"
    if force:
        required = True
        trigger_kind = "manual_refresh"

    return {
        "repo_id": repo_id,
        "project_path": str(repo),
        "baseline_oid": baseline_oid,
        "inspected_oid": inspected_oid,
        "source_fingerprint": fingerprint,
        "configured_paths": configured_paths,
        "affected_paths": affected_paths,
        "trigger_kind": trigger_kind,
        "required": required,
        "required_memory_names": list(policy.required_memory_names),
        "role_memory_refs": {key: list(value) for key, value in policy.role_memory_refs.items()},
        "previous_state": asdict(state) if state else None,
    }


def _enqueue_refresh_request(
    *,
    queue: SQLiteMessageQueue,
    detection: Mapping[str, Any],
    policy: SerenaKnowledgePolicy,
    thread_id: str,
    work_item_id: str,
    from_role: str,
    priority: int,
) -> dict[str, Any]:
    if not detection["required"]:
        return {"enqueued": False, "reason": "not_required"}
    inspected_oid = str(detection["inspected_oid"])
    payload = {
        "repo_id": detection["repo_id"],
        "trigger_kind": detection["trigger_kind"],
        "baseline_oid": detection["baseline_oid"],
        "inspected_oid": inspected_oid,
        "affected_paths": detection["affected_paths"],
        "affected_memory_names": detection["required_memory_names"],
        "role_memory_refs": detection["role_memory_refs"],
        "project_path": detection["project_path"],
        "source_fingerprint": detection["source_fingerprint"],
        "required_action": "Refresh shared Serena project knowledge through the PL lifecycle.",
    }
    message = queue.enqueue(
        thread_id=thread_id,
        work_item_id=work_item_id,
        from_role=from_role,
        to_role=policy.owner_role,
        message_type="PROJECT_KNOWLEDGE_REFRESH_REQUIRED",
        payload=payload,
        priority=priority,
        dedupe_key=f"project-knowledge:{detection['repo_id']}:{inspected_oid}",
    )
    current = queue.get_project_knowledge_state(str(detection["repo_id"]))
    queue.upsert_project_knowledge_state(
        repo_id=str(detection["repo_id"]),
        project_path=str(detection["project_path"]),
        baseline_oid=current.baseline_oid if current else detection["baseline_oid"],
        inspected_oid=inspected_oid,
        source_fingerprint=str(detection["source_fingerprint"]),
        state="refresh_required",
        memory_manifest=current.memory_manifest if current else {},
        owner_seat_id=current.owner_seat_id if current else None,
        evidence_artifact_ref=current.evidence_artifact_ref if current else None,
        memory_manifest_sha256=(
            current.memory_manifest_sha256 if current else None
        ),
        last_request_message_id=message.id,
        acknowledged_at=current.acknowledged_at if current else None,
    )
    return {"enqueued": True, "message_id": message.id}


def _validate_pl_seat(owner_seat_id: str, policy: SerenaKnowledgePolicy) -> None:
    try:
        bundle = load_and_validate()
    except AgentConfigurationError as exc:
        raise ProjectKnowledgeError(f"Cannot validate PL ownership: {exc}") from exc
    seat = bundle["seats"].get(owner_seat_id)
    if seat is None:
        raise ProjectKnowledgeError(f"Unknown owner seat: {owner_seat_id}")
    if policy.owner_capability not in seat.get("capabilities", []):
        raise ProjectKnowledgeError(
            f"Shared Serena memory acknowledgement requires capability "
            f"{policy.owner_capability!r}"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def _default_manifest_path(db_path: Path, repo_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", repo_id)
    return db_path.parent / "project-knowledge" / f"{safe_id}.json"


def _write_json(path: Path, value: Mapping[str, Any]) -> tuple[Path, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _acknowledge_refresh(args: argparse.Namespace, policy: SerenaKnowledgePolicy) -> dict[str, Any]:
    queue = SQLiteMessageQueue(args.db)
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        raise ProjectKnowledgeError(f"Repository directory does not exist: {repo}")
    _validate_pl_seat(args.owner_seat, policy)
    inspected_oid = _resolve_commit(repo, args.head_oid)
    decision = args.decision
    if decision not in policy.accepted_decisions:
        raise ProjectKnowledgeError(f"Unsupported acknowledgement decision: {decision}")
    state = queue.get_project_knowledge_state(args.repo_id)
    fingerprint, _ = _source_fingerprint(repo, inspected_oid, policy)
    memory_names = _project_memory_names(repo)
    missing_memories = sorted(set(policy.required_memory_names) - set(memory_names))
    evidence_path = Path(args.evidence_artifact).expanduser().resolve() if args.evidence_artifact else None
    if decision == "refreshed":
        if missing_memories:
            raise ProjectKnowledgeError(
                "Shared Serena memory refresh is incomplete. Missing memories: "
                f"{missing_memories}"
            )
        if evidence_path is None or not evidence_path.is_file():
            raise ProjectKnowledgeError(
                "A local evidence artifact is required to acknowledge a refresh."
            )
    elif evidence_path is not None and not evidence_path.is_file():
        raise ProjectKnowledgeError(f"Evidence artifact is missing: {evidence_path}")

    memory_check: dict[str, Any] | None = None
    if policy.require_memory_check:
        executable = _require_serena_setup(repo)
        checked = _run(
            [
                executable,
                "memories",
                "check",
                "--include-unmarked",
                "--fuzzy-matching",
                str(repo),
            ],
            cwd=repo,
            check=False,
        )
        memory_check = {
            "returncode": checked.returncode,
            "output": (checked.stdout or checked.stderr).strip(),
        }

    acknowledged_at = datetime.now(UTC).isoformat(timespec="seconds")
    evidence_sha256 = _sha256_file(evidence_path) if evidence_path else None
    manifest = {
        "version": 1,
        "repo_id": args.repo_id,
        "project_path": str(repo),
        "baseline_oid": inspected_oid if decision != "deferred" else (state.baseline_oid if state else None),
        "inspected_oid": inspected_oid,
        "source_fingerprint": fingerprint,
        "state": "ready" if decision in {"refreshed", "no_change"} else "deferred",
        "decision": decision,
        "owner_role": policy.owner_role,
        "owner_seat_id": args.owner_seat,
        "acknowledged_at": acknowledged_at,
        "memory_names": memory_names,
        "role_memory_refs": {
            key: list(value) for key, value in policy.role_memory_refs.items()
        },
        "evidence_artifact_ref": str(evidence_path) if evidence_path else None,
        "evidence_sha256": evidence_sha256,
        "memory_check": memory_check,
        "reason": args.reason,
    }
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else _default_manifest_path(Path(args.db).expanduser().resolve(), args.repo_id)
    )
    written_manifest_path, manifest_sha256 = _write_json(manifest_path, manifest)
    persisted_state = queue.upsert_project_knowledge_state(
        repo_id=args.repo_id,
        project_path=str(repo),
        baseline_oid=manifest["baseline_oid"],
        inspected_oid=inspected_oid,
        source_fingerprint=fingerprint,
        state=manifest["state"],
        memory_manifest={**manifest, "manifest_path": str(written_manifest_path)},
        owner_seat_id=args.owner_seat,
        evidence_artifact_ref=str(evidence_path) if evidence_path else None,
        memory_manifest_sha256=manifest_sha256,
        last_request_message_id=state.last_request_message_id if state else None,
        acknowledged_at=acknowledged_at,
    )
    message_type = {
        "refreshed": "PROJECT_KNOWLEDGE_REFRESHED",
        "no_change": "PROJECT_KNOWLEDGE_NO_CHANGE",
        "deferred": "PROJECT_KNOWLEDGE_DEFERRED",
    }[decision]
    message = queue.enqueue(
        thread_id=args.thread,
        work_item_id=args.work_item,
        from_role=policy.owner_role,
        to_role="*",
        message_type=message_type,
        payload={
            "repo_id": args.repo_id,
            "decision": decision,
            "baseline_oid": manifest["baseline_oid"],
            "inspected_oid": inspected_oid,
            "affected_memory_names": memory_names,
            "evidence_artifact_ref": str(evidence_path) if evidence_path else None,
            "memory_manifest_ref": str(written_manifest_path),
            "memory_manifest_sha256": manifest_sha256,
            "owner_seat_id": args.owner_seat,
            "reason": args.reason,
        },
        priority=args.priority,
        dedupe_key=f"project-knowledge:{args.repo_id}:{inspected_oid}:{decision}",
    )
    return {
        "acknowledged": True,
        "message_id": message.id,
        "manifest_path": str(written_manifest_path),
        "manifest_sha256": manifest_sha256,
        "state": asdict(persisted_state),
    }


def _validate_evidence_input(
    value: Any,
    *,
    repo_id: str,
    source_oid: str,
    producer_role: str,
    policy: SerenaKnowledgePolicy,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectKnowledgeError("Evidence input must be one JSON object")
    evidence_id = value.get("evidence_id")
    if not isinstance(evidence_id, str) or not EVIDENCE_ID_PATTERN.fullmatch(evidence_id):
        raise ProjectKnowledgeError("evidence_id must use uppercase ID syntax")
    kind = value.get("kind")
    if kind not in SUPPORTED_EVIDENCE_KINDS:
        raise ProjectKnowledgeError(f"Unsupported Serena evidence kind: {kind!r}")
    if value.get("repo_id") != repo_id:
        raise ProjectKnowledgeError("Evidence repo_id does not match the requested repository")
    if value.get("source_oid") != source_oid:
        raise ProjectKnowledgeError("Evidence source_oid does not match the verified target OID")
    scope = value.get("scope")
    if not isinstance(scope, Mapping):
        raise ProjectKnowledgeError("Evidence scope must be an object")
    raw_paths = scope.get("paths", [])
    raw_symbols = scope.get("symbols", [])
    if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
        raise ProjectKnowledgeError("Evidence scope.paths must be a string array")
    if not isinstance(raw_symbols, list) or not all(
        isinstance(symbol, str) and symbol for symbol in raw_symbols
    ):
        raise ProjectKnowledgeError("Evidence scope.symbols must be a string array")
    paths = [_normalize_relative_path(path) for path in raw_paths]
    if len(paths) != len(set(paths)) or len(raw_symbols) != len(set(raw_symbols)):
        raise ProjectKnowledgeError("Evidence scope contains duplicate paths or symbols")
    facts = value.get("facts")
    if (
        not isinstance(facts, list)
        or not facts
        or len(facts) > 32
        or not all(isinstance(fact, str) and fact and len(fact) <= 1_000 for fact in facts)
    ):
        raise ProjectKnowledgeError(
            "Evidence facts must contain one to 32 non-empty strings of at most 1000 characters"
        )
    confidence = value.get("confidence")
    if confidence not in SUPPORTED_CONFIDENCE:
        raise ProjectKnowledgeError(f"Unsupported evidence confidence: {confidence!r}")
    memory_refs = value.get("memory_refs", [])
    if not isinstance(memory_refs, list) or not all(
        isinstance(name, str) and name for name in memory_refs
    ):
        raise ProjectKnowledgeError("Evidence memory_refs must be a string array")
    unknown_memory_refs = set(memory_refs) - set(policy.required_memory_names)
    if unknown_memory_refs:
        raise ProjectKnowledgeError(
            f"Evidence references unknown shared memories: {sorted(unknown_memory_refs)}"
        )
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "repo_id": repo_id,
        "source_oid": source_oid,
        "scope": {"paths": paths, "symbols": list(raw_symbols)},
        "facts": list(facts),
        "confidence": confidence,
        "memory_refs": list(memory_refs),
        "producer_role": producer_role,
    }


def _record_evidence(args: argparse.Namespace, policy: SerenaKnowledgePolicy) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        raise ProjectKnowledgeError(f"Repository directory does not exist: {repo}")
    source_oid = _resolve_commit(repo, args.source_oid)
    source = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
    evidence = _validate_evidence_input(
        source,
        repo_id=args.repo_id,
        source_oid=source_oid,
        producer_role=args.producer_role,
        policy=policy,
    )
    if args.producer_seat:
        try:
            bundle = load_and_validate()
        except AgentConfigurationError as exc:
            raise ProjectKnowledgeError(f"Cannot validate evidence producer: {exc}") from exc
        seat = bundle["seats"].get(args.producer_seat)
        if seat is None or args.producer_role not in seat.get("capabilities", []):
            raise ProjectKnowledgeError(
                "producer_seat must belong to producer_role for Serena evidence publication"
            )

    artifact_root = Path(args.artifact_root).expanduser().resolve()
    artifact_path = (
        artifact_root
        / "serena"
        / args.repo_id
        / source_oid
        / f"{evidence['evidence_id']}.json"
    )
    artifact = {
        "version": 1,
        **evidence,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    written_artifact_path, artifact_sha256 = _write_json(artifact_path, artifact)
    artifact["artifact_ref"] = str(written_artifact_path)
    artifact["sha256"] = artifact_sha256
    written_artifact_path, artifact_sha256 = _write_json(written_artifact_path, artifact)

    index_path = Path(args.index).expanduser().resolve()
    if index_path.is_file():
        catalog = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        catalog = {"version": 1, "repo_id": args.repo_id, "items": []}
    if catalog.get("version") != 1 or catalog.get("repo_id") != args.repo_id:
        raise ProjectKnowledgeError("Semantic evidence catalog does not match the requested repository")
    items = catalog.get("items")
    if not isinstance(items, list):
        raise ProjectKnowledgeError("Semantic evidence catalog items must be an array")
    descriptor = {
        key: artifact[key]
        for key in (
            "evidence_id",
            "kind",
            "repo_id",
            "source_oid",
            "scope",
            "facts",
            "confidence",
            "memory_refs",
            "producer_role",
            "artifact_ref",
            "sha256",
        )
    }
    matching = [item for item in items if item.get("evidence_id") == evidence["evidence_id"]]
    if matching and matching[0] != descriptor:
        raise ProjectKnowledgeError(
            f"Semantic evidence ID already exists with different content: {evidence['evidence_id']}"
        )
    if not matching:
        items.append(descriptor)
        items.sort(key=lambda item: (str(item.get("source_oid")), str(item.get("evidence_id"))))
    _write_json(index_path, catalog)

    message_id: str | None = None
    if args.db:
        if not args.thread or not args.work_item:
            raise ProjectKnowledgeError("--thread and --work-item are required when --db is supplied")
        queue = SQLiteMessageQueue(args.db)
        message = queue.enqueue(
            thread_id=args.thread,
            work_item_id=args.work_item,
            from_role=args.producer_role,
            to_role=args.to_role,
            message_type="SERENA_EVIDENCE_READY",
            payload={
                "repo_id": args.repo_id,
                "source_oid": source_oid,
                "evidence_ids": [evidence["evidence_id"]],
                "artifact_ref": str(written_artifact_path),
                "sha256": artifact_sha256,
            },
            priority=args.priority,
            dedupe_key=(
                f"serena-evidence:{args.repo_id}:{source_oid}:{evidence['evidence_id']}"
            ),
        )
        message_id = message.id
    return {
        "recorded": True,
        "evidence_id": evidence["evidence_id"],
        "artifact_path": str(written_artifact_path),
        "artifact_sha256": artifact_sha256,
        "index_path": str(index_path),
        "message_id": message_id,
    }


def _request_refresh(args: argparse.Namespace, policy: SerenaKnowledgePolicy) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve()
    queue = SQLiteMessageQueue(args.db)
    detection = _detect_refresh(
        queue=queue,
        repo_id=args.repo_id,
        repo=repo,
        head_oid=args.head_oid,
        policy=policy,
        base_oid=args.base_oid,
        force=args.force,
    )
    request = _enqueue_refresh_request(
        queue=queue,
        detection=detection,
        policy=policy,
        thread_id=args.thread,
        work_item_id=args.work_item,
        from_role=args.from_role,
        priority=args.priority,
    )
    return {"detection": detection, "request": request}


def _status(args: argparse.Namespace, policy: SerenaKnowledgePolicy) -> dict[str, Any]:
    queue = SQLiteMessageQueue(args.db)
    state = queue.get_project_knowledge_state(args.repo_id)
    result: dict[str, Any] = {
        "repo_id": args.repo_id,
        "state": asdict(state) if state else None,
        "policy": {
            "owner_role": policy.owner_role,
            "required_memory_names": list(policy.required_memory_names),
            "role_memory_refs": {
                key: list(value) for key, value in policy.role_memory_refs.items()
            },
        },
    }
    if args.repo:
        repo = Path(args.repo).expanduser().resolve()
        memory_names = _project_memory_names(repo)
        result["project_path"] = str(repo)
        result["memory_names"] = memory_names
        result["missing_required_memory_names"] = sorted(
            set(policy.required_memory_names) - set(memory_names)
        )
    return result


def _add_policy_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--policy",
        default=str(DEFAULT_POLICY_PATH),
        help="Project-local Serena knowledge policy TOML.",
    )


def _add_queue_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="SQLite team message database.")
    parser.add_argument("--repo-id", required=True, help="Repository registry identifier.")


def _add_repo_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="Local repository checkout path.")
    parser.add_argument("--head-oid", required=True, help="Target Git commit OID.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage PL-owned Serena project knowledge and compact semantic evidence."
    )
    _add_policy_argument(parser)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    detect = subparsers.add_parser(
        "detect", help="Detect whether an approved integration needs a PL knowledge refresh."
    )
    _add_queue_arguments(detect)
    _add_repo_arguments(detect)
    detect.add_argument("--base-oid")
    detect.add_argument("--force", action="store_true")

    request = subparsers.add_parser(
        "request-refresh", help="Detect and enqueue a PL knowledge refresh request."
    )
    _add_queue_arguments(request)
    _add_repo_arguments(request)
    request.add_argument("--base-oid")
    request.add_argument("--thread", required=True)
    request.add_argument("--work-item", required=True)
    request.add_argument("--from-role", default="system")
    request.add_argument("--priority", type=int, default=50)
    request.add_argument("--force", action="store_true")

    acknowledge = subparsers.add_parser(
        "ack",
        help="Record the PL decision after the shared Serena memory refresh workflow.",
    )
    _add_queue_arguments(acknowledge)
    _add_repo_arguments(acknowledge)
    acknowledge.add_argument("--thread", required=True)
    acknowledge.add_argument("--work-item", required=True)
    acknowledge.add_argument("--owner-seat", required=True)
    acknowledge.add_argument(
        "--decision", choices=("refreshed", "no_change", "deferred"), required=True
    )
    acknowledge.add_argument("--evidence-artifact")
    acknowledge.add_argument("--manifest")
    acknowledge.add_argument("--reason", required=True)
    acknowledge.add_argument("--priority", type=int, default=50)

    evidence = subparsers.add_parser(
        "record-evidence",
        help="Validate and persist a compact Serena-derived evidence artifact.",
    )
    evidence.add_argument("--repo", required=True)
    evidence.add_argument("--repo-id", required=True)
    evidence.add_argument("--source-oid", required=True)
    evidence.add_argument("--producer-role", required=True)
    evidence.add_argument("--producer-seat")
    evidence.add_argument("--input", required=True, help="Structured evidence JSON source.")
    evidence.add_argument("--artifact-root", required=True)
    evidence.add_argument("--index", required=True, help="Semantic evidence catalog JSON.")
    evidence.add_argument("--db")
    evidence.add_argument("--thread")
    evidence.add_argument("--work-item")
    evidence.add_argument("--to-role", default="pl")
    evidence.add_argument("--priority", type=int, default=25)

    status = subparsers.add_parser("status", help="Show project knowledge state and memory coverage.")
    _add_queue_arguments(status)
    status.add_argument("--repo")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        if args.command == "detect":
            queue = SQLiteMessageQueue(args.db)
            repo = Path(args.repo).expanduser().resolve()
            result = _detect_refresh(
                queue=queue,
                repo_id=args.repo_id,
                repo=repo,
                head_oid=args.head_oid,
                policy=policy,
                base_oid=args.base_oid,
                force=args.force,
            )
        elif args.command == "request-refresh":
            result = _request_refresh(args, policy)
        elif args.command == "ack":
            result = _acknowledge_refresh(args, policy)
        elif args.command == "record-evidence":
            result = _record_evidence(args, policy)
        elif args.command == "status":
            result = _status(args, policy)
        else:
            parser.error(f"Unknown command: {args.command}")
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (
        AgentConfigurationError,
        ProjectKnowledgeError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"Serena project knowledge error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
