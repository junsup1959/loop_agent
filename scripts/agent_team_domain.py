from __future__ import annotations

"""Immutable domain contracts for the Agent-Team worktree execution detail.

The records in this module extend the existing Agent-Team control plane.  They
do not define a second role model or workflow engine.  Identifiers, Git object
IDs, refs, and JSON evidence are validated at the boundary so later Git and
SQLite services can fail closed before mutating authoritative state.
"""

import re
import uuid
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence


DOMAIN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
GIT_OID_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class DomainValidationError(ValueError):
    """Raised when a worktree-domain record violates its boundary contract."""


class DomainStateError(RuntimeError):
    """Raised when a state transition conflicts with immutable domain state."""


class ValueEnum(str, Enum):
    """String-valued enum whose serialized value is stable."""

    def __str__(self) -> str:
        return self.value


class TargetState(ValueEnum):
    REGISTERED = "REGISTERED"
    ACTIVE = "ACTIVE"
    RESYNC_REQUIRED = "RESYNC_REQUIRED"
    QUARANTINED = "QUARANTINED"
    RETIRED = "RETIRED"


class ManagedRepositoryState(ValueEnum):
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    RESYNC_REQUIRED = "RESYNC_REQUIRED"
    QUARANTINED = "QUARANTINED"


class GoalState(ValueEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class RunState(ValueEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WorkItemState(ValueEnum):
    PLANNED = "PLANNED"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW_PENDING = "REVIEW_PENDING"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    ACCEPTED = "ACCEPTED"
    CANCELLED = "CANCELLED"


class WorkRevisionState(ValueEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    SUBMITTED = "SUBMITTED"
    REVIEWED = "REVIEWED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class WorkspaceKind(ValueEnum):
    DEVELOPMENT = "DEVELOPMENT"
    INTEGRATION = "INTEGRATION"
    REVIEW = "REVIEW"


class WorkspaceState(ValueEnum):
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    RELEASING = "RELEASING"
    RELEASED = "RELEASED"
    QUARANTINED = "QUARANTINED"


class LeaseState(ValueEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    QUARANTINED = "QUARANTINED"


class ActivationState(ValueEnum):
    CREATED = "CREATED"
    PROFILE_BOUND = "PROFILE_BOUND"
    WORKSPACE_BOUND = "WORKSPACE_BOUND"
    RUNNING = "RUNNING"
    RESULT_PERSISTED = "RESULT_PERSISTED"
    PROFILE_REVOKED = "PROFILE_REVOKED"
    RESOURCES_RELEASED = "RESOURCES_RELEASED"
    TERMINATED = "TERMINATED"
    REVOKE_FAILED = "REVOKE_FAILED"
    QUARANTINED = "QUARANTINED"
    RECOVERY_CLEANED = "RECOVERY_CLEANED"


class CandidateState(ValueEnum):
    SUBMITTED = "SUBMITTED"
    REVIEW_PENDING = "REVIEW_PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class ReviewType(ValueEnum):
    CODE_QUALITY = "CODE_QUALITY"
    ARCHITECTURE = "ARCHITECTURE"
    QUALITY = "QUALITY"
    BUILD = "BUILD"
    REQUIREMENTS = "REQUIREMENTS"


class ReviewDecision(ValueEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NEEDS_REWORK = "NEEDS_REWORK"
    INVALIDATED = "INVALIDATED"


class GateType(ValueEnum):
    TA_CODE_QUALITY = "TA_CODE_QUALITY"
    TA_ARCHITECTURE = "TA_ARCHITECTURE"
    QA_QUALITY = "QA_QUALITY"
    BUILD = "BUILD"
    PL_CANDIDATE_SELECTION = "PL_CANDIDATE_SELECTION"
    PL_INTEGRATION = "PL_INTEGRATION"
    PM_REQUIREMENTS = "PM_REQUIREMENTS"


class GateDecisionValue(ValueEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NEEDS_REWORK = "NEEDS_REWORK"


class SourceIntegrity(ValueEnum):
    CLEAN = "CLEAN"
    ANALYSIS_DIRTY = "ANALYSIS_DIRTY"
    INVALIDATED = "INVALIDATED"


class IntegrationPlanState(ValueEnum):
    PLANNED = "PLANNED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    SUPERSEDED = "SUPERSEDED"


class IntegrationAttemptState(ValueEnum):
    PLANNED = "PLANNED"
    PREFLIGHTING = "PREFLIGHTING"
    MERGING = "MERGING"
    CONFLICTED = "CONFLICTED"
    EVIDENCE_PERSISTED = "EVIDENCE_PERSISTED"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    INTERRUPTED = "INTERRUPTED"
    QUARANTINED = "QUARANTINED"
    RECREATED = "RECREATED"
    MERGED = "MERGED"
    QA_PENDING = "QA_PENDING"
    QA_FAILED = "QA_FAILED"
    QA_PASSED = "QA_PASSED"
    BUILD_PENDING = "BUILD_PENDING"
    BUILD_FAILED = "BUILD_FAILED"
    BUILD_PASSED = "BUILD_PASSED"
    GATE_PENDING = "GATE_PENDING"
    APPROVED = "APPROVED"


class QualityRunState(ValueEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    INVALIDATED = "INVALIDATED"


class BuildRunState(ValueEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    INVALIDATED = "INVALIDATED"


class PromotionState(ValueEnum):
    REQUESTED = "REQUESTED"
    VALIDATING = "VALIDATING"
    PROMOTED = "PROMOTED"
    BLOCKED = "BLOCKED"
    ROLLED_BACK = "ROLLED_BACK"


class MigrationState(ValueEnum):
    PLANNED = "PLANNED"
    FROZEN = "FROZEN"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    CUT_OVER = "CUT_OVER"
    COMPLETED = "COMPLETED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


class MigrationStepState(ValueEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class ArtifactKind(ValueEnum):
    CONTEXT = "CONTEXT"
    LOG = "LOG"
    TEST = "TEST"
    BUILD = "BUILD"
    REVIEW = "REVIEW"
    INTEGRATION = "INTEGRATION"
    MIGRATION = "MIGRATION"
    AUDIT = "AUDIT"
    OTHER = "OTHER"


class IntentStatus(ValueEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


class FindingSeverity(ValueEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class FindingState(ValueEnum):
    OPEN = "OPEN"
    RECONCILING = "RECONCILING"
    RESOLVED = "RESOLVED"
    QUARANTINED = "QUARANTINED"


class ServiceIdentity(ValueEnum):
    """Deterministic services; these values are not Codex LLM seat IDs."""

    INTEGRATION_CONTROLLER = "service:integration-controller"
    PROMOTION_CONTROLLER = "service:promotion-controller"
    RECOVERY_RECONCILER = "service:recovery-reconciler"
    MIGRATION_CONTROLLER = "service:migration-controller"


class DefinitionKind(ValueEnum):
    WORKFLOW = "WORKFLOW"
    CLAUSE = "CLAUSE"
    SCHEMA = "SCHEMA"
    TEMPLATE = "TEMPLATE"
    PROFILE = "PROFILE"
    SKILL = "SKILL"
    MCP_POLICY = "MCP_POLICY"
    SERENA_POLICY = "SERENA_POLICY"
    OTHER = "OTHER"


class PhysicalSeatState(ValueEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class LogicalCapabilityState(ValueEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class OwnershipState(ValueEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class RuntimeSlotKind(ValueEnum):
    FIXED = "FIXED"
    ELASTIC = "ELASTIC"


class RuntimeSlotState(ValueEnum):
    AVAILABLE = "AVAILABLE"
    OCCUPIED = "OCCUPIED"
    QUARANTINED = "QUARANTINED"
    RETIRED = "RETIRED"


class WorkerKind(ValueEnum):
    FIXED = "FIXED"
    ELASTIC = "ELASTIC"


class WorkerState(ValueEnum):
    REGISTERED = "REGISTERED"
    ACTIVE = "ACTIVE"
    QUARANTINED = "QUARANTINED"
    RETIRED = "RETIRED"


class WorkerFingerprintState(ValueEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    QUARANTINED = "QUARANTINED"


class RuntimeBindingState(ValueEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    QUARANTINED = "QUARANTINED"


class WorkflowDefinitionState(ValueEnum):
    REGISTERED = "REGISTERED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class WorkflowInstanceStatus(ValueEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    QUARANTINED = "QUARANTINED"


class RuntimeLeaseKind(ValueEnum):
    DEVELOPMENT = "DEVELOPMENT"
    INTEGRATION = "INTEGRATION"
    REVIEW = "REVIEW"
    ADVISORY = "ADVISORY"


class ContractState(ValueEnum):
    ISSUED = "ISSUED"
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"
    RUNNING = "RUNNING"
    RESULT_RECORDED = "RESULT_RECORDED"
    COMPLETED = "COMPLETED"
    QUARANTINED = "QUARANTINED"
    CANCELLED = "CANCELLED"


class AdmissionDecision(ValueEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ContractAttemptKind(ValueEnum):
    PRIMARY = "PRIMARY"
    FORMAT_REPAIR = "FORMAT_REPAIR"


class ContractAttemptState(ValueEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"
    CANCELLED = "CANCELLED"


class ResultDisposition(ValueEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    FORMAT_INVALID = "FORMAT_INVALID"
    QUARANTINED = "QUARANTINED"


class ContractViolationCode(ValueEnum):
    ADMISSION = "ADMISSION"
    FORMAT = "FORMAT"
    AUTHORITY = "AUTHORITY"
    OID = "OID"
    WRITE_ROOT = "WRITE_ROOT"
    NESTED_SPAWN = "NESTED_SPAWN"
    MCP_HEALTH = "MCP_HEALTH"
    MCP_USAGE = "MCP_USAGE"
    SERENA_ONBOARDING = "SERENA_ONBOARDING"
    SERENA_CONSUMPTION = "SERENA_CONSUMPTION"
    OTHER = "OTHER"


class ViolationDisposition(ValueEnum):
    REJECTED = "REJECTED"
    FORMAT_REPAIR = "FORMAT_REPAIR"
    QUARANTINED = "QUARANTINED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"


class CircuitState(ValueEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TokenLedgerEntryKind(ValueEnum):
    BUDGET = "BUDGET"
    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    ADMISSION_REJECTED = "ADMISSION_REJECTED"


class McpHealthStatus(ValueEnum):
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class SerenaSnapshotState(ValueEnum):
    ACCEPTED = "ACCEPTED"
    STALE = "STALE"
    QUARANTINED = "QUARANTINED"


class EvidenceDisposition(ValueEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"


def require_nonempty(value: str | None, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field} must be a non-empty string")
    return value.strip()


def require_identifier(value: str | None, field: str) -> str:
    result = require_nonempty(value, field)
    if not DOMAIN_ID_PATTERN.fullmatch(result):
        raise DomainValidationError(
            f"{field} must match {DOMAIN_ID_PATTERN.pattern!r}"
        )
    return result


def generated_identifier(prefix: str) -> str:
    safe_prefix = require_identifier(prefix, "prefix")
    return f"{safe_prefix}-{uuid.uuid4().hex}"


def require_oid(value: str | None, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    result = require_nonempty(value, field)
    if not GIT_OID_PATTERN.fullmatch(result):
        raise DomainValidationError(
            f"{field} must be a full 40- or 64-hex-character Git object ID"
        )
    return result.lower()


def require_git_ref(value: str | None, field: str) -> str:
    result = require_nonempty(value, field)
    forbidden = ("..", "@{", "\\", " ", "~", "^", ":", "?", "*", "[")
    if (
        not result.startswith("refs/")
        or result.endswith("/")
        or result.endswith(".")
        or "//" in result
        or any(token in result for token in forbidden)
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
    ):
        raise DomainValidationError(f"{field} is not a safe fully qualified Git ref")
    return result


def require_positive(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DomainValidationError(f"{field} must be a positive integer")
    return value


def require_nonnegative(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DomainValidationError(f"{field} must be a non-negative integer")
    return value


def require_boolean(value: bool, field: str) -> bool:
    if not isinstance(value, bool):
        raise DomainValidationError(f"{field} must be a boolean")
    return value


def require_sha256(value: str | None, field: str) -> str:
    digest = require_nonempty(value, field).lower()
    if not SHA256_PATTERN.fullmatch(digest):
        raise DomainValidationError(f"{field} must be 64 hexadecimal characters")
    return digest


def require_definition_kind(value: DefinitionKind | str, field: str = "kind") -> DefinitionKind:
    if isinstance(value, DefinitionKind):
        return value
    normalized = require_nonempty(value, field).upper().replace("-", "_")
    try:
        return DefinitionKind(normalized)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in DefinitionKind)
        raise DomainValidationError(f"{field} must be one of: {allowed}") from exc


def _enum_value(value: Any, enum_type: type[ValueEnum], field: str) -> ValueEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise DomainValidationError(f"{field} must be one of: {allowed}") from exc


def _string_tuple(
    values: Sequence[str],
    field: str,
    *,
    allow_empty: bool = True,
    unique: bool = True,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise DomainValidationError(f"{field} must be a sequence of strings")
    result = tuple(require_nonempty(value, field) for value in values)
    if not allow_empty and not result:
        raise DomainValidationError(f"{field} must not be empty")
    if unique and len(set(result)) != len(result):
        raise DomainValidationError(f"{field} must not contain duplicates")
    return result


def _oid_tuple(
    values: Sequence[str], field: str, *, allow_empty: bool = True
) -> tuple[str, ...]:
    raw = _string_tuple(values, field, allow_empty=allow_empty)
    result = tuple(str(require_oid(value, field)) for value in raw)
    if len(set(result)) != len(result):
        raise DomainValidationError(f"{field} must not contain duplicate OIDs")
    return result


def _freeze_json(value: Any, field: str = "value") -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise DomainValidationError(f"{field} object keys must be strings")
            normalized[key] = _freeze_json(child, f"{field}.{key}")
        return MappingProxyType(normalized)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(child, field) for child in value)
    raise DomainValidationError(
        f"{field} must contain only JSON-compatible immutable values"
    )


def freeze_mapping(value: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DomainValidationError(f"{field} must be an object")
    return _freeze_json(value, field)


def thaw_json(value: Any) -> Any:
    """Convert frozen domain evidence to ordinary JSON-compatible containers."""

    if isinstance(value, Mapping):
        return {key: thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    if isinstance(value, ValueEnum):
        return value.value
    return value


@dataclass(frozen=True, slots=True)
class TargetRegistration:
    target_id: str
    canonical_worktree_path: str
    git_common_dir: str
    source_ref: str
    observed_source_oid: str
    managed_repository_path: str
    state: TargetState = TargetState.REGISTERED

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", require_identifier(self.target_id, "target_id"))
        for field in (
            "canonical_worktree_path",
            "git_common_dir",
            "managed_repository_path",
        ):
            object.__setattr__(self, field, require_nonempty(getattr(self, field), field))
        object.__setattr__(self, "source_ref", require_git_ref(self.source_ref, "source_ref"))
        object.__setattr__(
            self,
            "observed_source_oid",
            require_oid(self.observed_source_oid, "observed_source_oid"),
        )
        object.__setattr__(self, "state", _enum_value(self.state, TargetState, "state"))


@dataclass(frozen=True, slots=True)
class ManagedRepositoryRecord:
    managed_repository_id: str
    target_id: str
    repository_path: str
    state: ManagedRepositoryState
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "managed_repository_id",
            require_identifier(self.managed_repository_id, "managed_repository_id"),
        )
        object.__setattr__(self, "target_id", require_identifier(self.target_id, "target_id"))
        object.__setattr__(
            self, "repository_path", require_nonempty(self.repository_path, "repository_path")
        )
        object.__setattr__(
            self, "state", _enum_value(self.state, ManagedRepositoryState, "state")
        )
        object.__setattr__(self, "created_at", require_nonempty(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    snapshot_id: str
    target_id: str
    managed_repository_id: str
    source_ref: str
    source_oid: str
    imported_oid: str
    idempotency_key: str
    created_at: str

    def __post_init__(self) -> None:
        for field in ("snapshot_id", "target_id", "managed_repository_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "source_ref", require_git_ref(self.source_ref, "source_ref"))
        for field in ("source_oid", "imported_oid"):
            object.__setattr__(self, field, require_oid(getattr(self, field), field))
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )
        object.__setattr__(self, "created_at", require_nonempty(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class GoalRecord:
    goal_id: str
    target_id: str
    base_oid: str
    state: GoalState = GoalState.CREATED

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal_id", require_identifier(self.goal_id, "goal_id"))
        object.__setattr__(self, "target_id", require_identifier(self.target_id, "target_id"))
        object.__setattr__(self, "base_oid", require_oid(self.base_oid, "base_oid"))
        object.__setattr__(self, "state", _enum_value(self.state, GoalState, "state"))


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    goal_id: str
    target_id: str
    base_oid: str
    state: RunState
    idempotency_key: str

    def __post_init__(self) -> None:
        for field in ("run_id", "goal_id", "target_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "base_oid", require_oid(self.base_oid, "base_oid"))
        object.__setattr__(self, "state", _enum_value(self.state, RunState, "state"))
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )


@dataclass(frozen=True, slots=True)
class WorkItemRecord:
    work_item_id: str
    goal_id: str
    title: str
    assigned_owner: str
    source_write_scope: tuple[str, ...]
    state: WorkItemState = WorkItemState.PLANNED

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "work_item_id", require_identifier(self.work_item_id, "work_item_id")
        )
        object.__setattr__(self, "goal_id", require_identifier(self.goal_id, "goal_id"))
        object.__setattr__(self, "title", require_nonempty(self.title, "title"))
        object.__setattr__(
            self, "assigned_owner", require_identifier(self.assigned_owner, "assigned_owner")
        )
        object.__setattr__(
            self,
            "source_write_scope",
            _string_tuple(self.source_write_scope, "source_write_scope", allow_empty=False),
        )
        object.__setattr__(self, "state", _enum_value(self.state, WorkItemState, "state"))


@dataclass(frozen=True, slots=True)
class WorkRevision:
    revision_id: str
    work_item_id: str
    revision: int
    owner: str
    base_oid: str
    head_oid: str | None
    state: WorkRevisionState
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "revision_id", require_identifier(self.revision_id, "revision_id")
        )
        object.__setattr__(
            self, "work_item_id", require_identifier(self.work_item_id, "work_item_id")
        )
        object.__setattr__(self, "revision", require_positive(self.revision, "revision"))
        object.__setattr__(self, "owner", require_identifier(self.owner, "owner"))
        object.__setattr__(self, "base_oid", require_oid(self.base_oid, "base_oid"))
        object.__setattr__(
            self, "head_oid", require_oid(self.head_oid, "head_oid", optional=True)
        )
        object.__setattr__(
            self, "state", _enum_value(self.state, WorkRevisionState, "state")
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    lease_id: str
    workspace_id: str
    target_id: str
    goal_id: str
    work_item_id: str
    revision_id: str
    revision: int
    owner: str
    branch_ref: str
    worktree_path: str
    base_oid: str
    expected_head_oid: str
    source_write_scope: tuple[str, ...]
    generated_write_scope: tuple[str, ...]
    expires_at: str
    state: LeaseState = LeaseState.ACTIVE

    def __post_init__(self) -> None:
        for field in (
            "lease_id",
            "workspace_id",
            "target_id",
            "goal_id",
            "work_item_id",
            "revision_id",
        ):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "revision", require_positive(self.revision, "revision"))
        object.__setattr__(self, "owner", require_identifier(self.owner, "owner"))
        object.__setattr__(
            self, "branch_ref", require_git_ref(self.branch_ref, "branch_ref")
        )
        object.__setattr__(
            self, "worktree_path", require_nonempty(self.worktree_path, "worktree_path")
        )
        for field in ("base_oid", "expected_head_oid"):
            object.__setattr__(self, field, require_oid(getattr(self, field), field))
        object.__setattr__(
            self,
            "source_write_scope",
            _string_tuple(self.source_write_scope, "source_write_scope", allow_empty=False),
        )
        object.__setattr__(
            self,
            "generated_write_scope",
            _string_tuple(self.generated_write_scope, "generated_write_scope"),
        )
        object.__setattr__(self, "expires_at", require_nonempty(self.expires_at, "expires_at"))
        object.__setattr__(self, "state", _enum_value(self.state, LeaseState, "state"))


@dataclass(frozen=True, slots=True)
class ActivationSpec:
    activation_id: str
    subject_oid: str
    workspace_or_sandbox_id: str
    professional_skill_id: str
    compiled_profile_ref: str
    compiled_profile_digest: str
    allowed_tools: tuple[str, ...]
    commands: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "activation_id", require_identifier(self.activation_id, "activation_id")
        )
        object.__setattr__(self, "subject_oid", require_oid(self.subject_oid, "subject_oid"))
        object.__setattr__(
            self,
            "workspace_or_sandbox_id",
            require_identifier(self.workspace_or_sandbox_id, "workspace_or_sandbox_id"),
        )
        object.__setattr__(
            self,
            "professional_skill_id",
            require_identifier(self.professional_skill_id, "professional_skill_id"),
        )
        object.__setattr__(
            self,
            "compiled_profile_ref",
            require_nonempty(self.compiled_profile_ref, "compiled_profile_ref"),
        )
        object.__setattr__(
            self,
            "compiled_profile_digest",
            require_nonempty(self.compiled_profile_digest, "compiled_profile_digest"),
        )
        object.__setattr__(
            self, "allowed_tools", _string_tuple(self.allowed_tools, "allowed_tools")
        )
        object.__setattr__(
            self, "commands", _string_tuple(self.commands, "commands")
        )


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    activation_id: str
    target_id: str
    goal_id: str
    run_id: str
    workspace_or_sandbox_id: str
    subject_oid: str
    role: str
    gate_or_task: str
    state: ActivationState
    idempotency_key: str
    compiled_profile_digest: str | None = None
    process_id: int | None = None

    def __post_init__(self) -> None:
        for field in (
            "activation_id",
            "target_id",
            "goal_id",
            "run_id",
            "workspace_or_sandbox_id",
        ):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "subject_oid", require_oid(self.subject_oid, "subject_oid"))
        object.__setattr__(self, "role", require_identifier(self.role, "role"))
        object.__setattr__(
            self, "gate_or_task", require_identifier(self.gate_or_task, "gate_or_task")
        )
        object.__setattr__(
            self, "state", _enum_value(self.state, ActivationState, "state")
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )
        if self.compiled_profile_digest is not None:
            object.__setattr__(
                self,
                "compiled_profile_digest",
                require_nonempty(
                    self.compiled_profile_digest, "compiled_profile_digest"
                ),
            )
        if self.process_id is not None and (
            not isinstance(self.process_id, int)
            or isinstance(self.process_id, bool)
            or self.process_id < 1
        ):
            raise DomainValidationError("process_id must be a positive integer")


@dataclass(frozen=True, slots=True)
class CandidateSubmission:
    candidate_id: str
    goal_id: str
    work_item_id: str
    revision_id: str
    revision: int
    lease_id: str
    branch_ref: str
    expected_previous_oid: str
    candidate_oid: str
    self_test_evidence: tuple[str, ...]
    idempotency_key: str
    state: CandidateState = CandidateState.SUBMITTED

    def __post_init__(self) -> None:
        for field in (
            "candidate_id",
            "goal_id",
            "work_item_id",
            "revision_id",
            "lease_id",
        ):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "revision", require_positive(self.revision, "revision"))
        object.__setattr__(
            self, "branch_ref", require_git_ref(self.branch_ref, "branch_ref")
        )
        for field in ("expected_previous_oid", "candidate_oid"):
            object.__setattr__(self, field, require_oid(getattr(self, field), field))
        object.__setattr__(
            self,
            "self_test_evidence",
            _string_tuple(self.self_test_evidence, "self_test_evidence"),
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )
        object.__setattr__(self, "state", _enum_value(self.state, CandidateState, "state"))


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    review_id: str
    goal_id: str
    activation_id: str
    reviewer_role: str
    review_type: ReviewType
    subject_oid: str
    decision: ReviewDecision
    profile_digest: str
    evidence_ids: tuple[str, ...]
    integrity: SourceIntegrity
    idempotency_key: str
    candidate_id: str | None = None

    def __post_init__(self) -> None:
        for field in ("review_id", "goal_id", "activation_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        if self.candidate_id is not None:
            object.__setattr__(
                self, "candidate_id", require_identifier(self.candidate_id, "candidate_id")
            )
        object.__setattr__(
            self, "reviewer_role", require_identifier(self.reviewer_role, "reviewer_role")
        )
        object.__setattr__(
            self, "review_type", _enum_value(self.review_type, ReviewType, "review_type")
        )
        object.__setattr__(self, "subject_oid", require_oid(self.subject_oid, "subject_oid"))
        object.__setattr__(
            self, "decision", _enum_value(self.decision, ReviewDecision, "decision")
        )
        object.__setattr__(
            self, "profile_digest", require_nonempty(self.profile_digest, "profile_digest")
        )
        object.__setattr__(
            self, "evidence_ids", _string_tuple(self.evidence_ids, "evidence_ids")
        )
        object.__setattr__(
            self, "integrity", _enum_value(self.integrity, SourceIntegrity, "integrity")
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )


@dataclass(frozen=True, slots=True)
class GateDecision:
    decision_id: str
    goal_id: str
    activation_id: str
    gate_type: GateType
    actor_role: str
    subject_oid: str
    decision: GateDecisionValue
    profile_digest: str
    evidence_ids: tuple[str, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        for field in ("decision_id", "goal_id", "activation_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(
            self, "gate_type", _enum_value(self.gate_type, GateType, "gate_type")
        )
        object.__setattr__(self, "actor_role", require_identifier(self.actor_role, "actor_role"))
        object.__setattr__(self, "subject_oid", require_oid(self.subject_oid, "subject_oid"))
        object.__setattr__(
            self,
            "decision",
            _enum_value(self.decision, GateDecisionValue, "decision"),
        )
        object.__setattr__(
            self, "profile_digest", require_nonempty(self.profile_digest, "profile_digest")
        )
        object.__setattr__(
            self,
            "evidence_ids",
            _string_tuple(self.evidence_ids, "evidence_ids", allow_empty=False),
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )


@dataclass(frozen=True, slots=True)
class IntegrationPlan:
    plan_id: str
    attempt_id: str
    goal_id: str
    base_oid: str
    ordered_candidate_oids: tuple[str, ...]
    merge_strategy: str
    pl_decision_id: str
    idempotency_key: str
    state: IntegrationPlanState = IntegrationPlanState.PLANNED

    def __post_init__(self) -> None:
        for field in ("plan_id", "attempt_id", "goal_id", "pl_decision_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "base_oid", require_oid(self.base_oid, "base_oid"))
        object.__setattr__(
            self,
            "ordered_candidate_oids",
            _oid_tuple(
                self.ordered_candidate_oids,
                "ordered_candidate_oids",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self, "merge_strategy", require_identifier(self.merge_strategy, "merge_strategy")
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )
        object.__setattr__(
            self, "state", _enum_value(self.state, IntegrationPlanState, "state")
        )


@dataclass(frozen=True, slots=True)
class IntegrationAttempt:
    attempt_id: str
    plan_id: str
    goal_id: str
    base_oid: str
    ordered_candidate_oids: tuple[str, ...]
    merge_strategy: str
    state: IntegrationAttemptState
    idempotency_key: str
    result_oid: str | None = None
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("attempt_id", "plan_id", "goal_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "base_oid", require_oid(self.base_oid, "base_oid"))
        object.__setattr__(
            self,
            "ordered_candidate_oids",
            _oid_tuple(
                self.ordered_candidate_oids,
                "ordered_candidate_oids",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self, "merge_strategy", require_identifier(self.merge_strategy, "merge_strategy")
        )
        object.__setattr__(
            self,
            "state",
            _enum_value(self.state, IntegrationAttemptState, "state"),
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )
        object.__setattr__(
            self, "result_oid", require_oid(self.result_oid, "result_oid", optional=True)
        )
        object.__setattr__(
            self, "evidence_ids", _string_tuple(self.evidence_ids, "evidence_ids")
        )


@dataclass(frozen=True, slots=True)
class QualityRun:
    quality_run_id: str
    goal_id: str
    attempt_id: str
    activation_id: str
    subject_oid: str
    state: QualityRunState
    evidence_ids: tuple[str, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        for field in ("quality_run_id", "goal_id", "attempt_id", "activation_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "subject_oid", require_oid(self.subject_oid, "subject_oid"))
        object.__setattr__(
            self, "state", _enum_value(self.state, QualityRunState, "state")
        )
        object.__setattr__(
            self, "evidence_ids", _string_tuple(self.evidence_ids, "evidence_ids")
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )


@dataclass(frozen=True, slots=True)
class BuildRun:
    build_run_id: str
    goal_id: str
    attempt_id: str
    activation_id: str
    subject_oid: str
    state: BuildRunState
    evidence_ids: tuple[str, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        for field in ("build_run_id", "goal_id", "attempt_id", "activation_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "subject_oid", require_oid(self.subject_oid, "subject_oid"))
        object.__setattr__(self, "state", _enum_value(self.state, BuildRunState, "state"))
        object.__setattr__(
            self, "evidence_ids", _string_tuple(self.evidence_ids, "evidence_ids")
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )


@dataclass(frozen=True, slots=True)
class PromotionRequest:
    promotion_id: str
    goal_id: str
    target_id: str
    approved_oid: str
    expected_source_oid: str
    destination_ref: str
    expected_destination_oid: str | None
    required_gate_decision_ids: tuple[str, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        for field in ("promotion_id", "goal_id", "target_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        for field in ("approved_oid", "expected_source_oid"):
            object.__setattr__(self, field, require_oid(getattr(self, field), field))
        object.__setattr__(
            self,
            "expected_destination_oid",
            require_oid(
                self.expected_destination_oid,
                "expected_destination_oid",
                optional=True,
            ),
        )
        destination = require_git_ref(self.destination_ref, "destination_ref")
        if not destination.startswith("refs/agentic-ax/approved/"):
            raise DomainValidationError(
                "destination_ref must be under refs/agentic-ax/approved/"
            )
        object.__setattr__(self, "destination_ref", destination)
        object.__setattr__(
            self,
            "required_gate_decision_ids",
            _string_tuple(
                self.required_gate_decision_ids,
                "required_gate_decision_ids",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )


@dataclass(frozen=True, slots=True)
class PromotionRecord:
    promotion_id: str
    goal_id: str
    target_id: str
    approved_oid: str
    destination_ref: str
    state: PromotionState
    idempotency_key: str
    promoted_oid: str | None = None

    def __post_init__(self) -> None:
        for field in ("promotion_id", "goal_id", "target_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "approved_oid", require_oid(self.approved_oid, "approved_oid"))
        object.__setattr__(
            self, "destination_ref", require_git_ref(self.destination_ref, "destination_ref")
        )
        object.__setattr__(
            self, "state", _enum_value(self.state, PromotionState, "state")
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )
        object.__setattr__(
            self, "promoted_oid", require_oid(self.promoted_oid, "promoted_oid", optional=True)
        )


@dataclass(frozen=True, slots=True)
class MigrationRun:
    migration_id: str
    target_id: str
    legacy_root: str
    runtime_root: str
    state: MigrationState
    manifest_digest: str | None
    idempotency_key: str

    def __post_init__(self) -> None:
        for field in ("migration_id", "target_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        for field in ("legacy_root", "runtime_root"):
            object.__setattr__(self, field, require_nonempty(getattr(self, field), field))
        object.__setattr__(
            self, "state", _enum_value(self.state, MigrationState, "state")
        )
        if self.manifest_digest is not None:
            object.__setattr__(
                self,
                "manifest_digest",
                require_nonempty(self.manifest_digest, "manifest_digest"),
            )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )


@dataclass(frozen=True, slots=True)
class MigrationStepRecord:
    step_id: str
    migration_id: str
    ordinal: int
    name: str
    state: MigrationStepState
    idempotency_key: str
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        for field in ("step_id", "migration_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "ordinal", require_positive(self.ordinal, "ordinal"))
        object.__setattr__(self, "name", require_identifier(self.name, "name"))
        object.__setattr__(
            self, "state", _enum_value(self.state, MigrationStepState, "state")
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )
        object.__setattr__(self, "evidence", freeze_mapping(self.evidence, "evidence"))


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    goal_id: str | None
    kind: ArtifactKind
    relative_path: str
    sha256: str
    byte_count: int
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "artifact_id", require_identifier(self.artifact_id, "artifact_id")
        )
        if self.goal_id is not None:
            object.__setattr__(self, "goal_id", require_identifier(self.goal_id, "goal_id"))
        object.__setattr__(self, "kind", _enum_value(self.kind, ArtifactKind, "kind"))
        object.__setattr__(
            self, "relative_path", require_nonempty(self.relative_path, "relative_path")
        )
        if self.relative_path.startswith(("/", "\\")) or ".." in re.split(
            r"[/\\]", self.relative_path
        ):
            raise DomainValidationError("relative_path must not be absolute or escape")
        digest = require_nonempty(self.sha256, "sha256").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DomainValidationError("sha256 must be 64 hexadecimal characters")
        object.__setattr__(self, "sha256", digest)
        if (
            not isinstance(self.byte_count, int)
            or isinstance(self.byte_count, bool)
            or self.byte_count < 0
        ):
            raise DomainValidationError("byte_count must be a non-negative integer")
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    event_type: str
    actor: str
    subject_type: str
    subject_id: str
    payload: Mapping[str, Any]
    occurred_at: str
    idempotency_key: str | None = None
    goal_id: str | None = None
    run_id: str | None = None
    activation_id: str | None = None
    subject_oid: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", require_identifier(self.event_id, "event_id"))
        for field in ("event_type", "actor", "subject_type", "subject_id"):
            object.__setattr__(self, field, require_nonempty(getattr(self, field), field))
        object.__setattr__(self, "payload", freeze_mapping(self.payload, "payload"))
        object.__setattr__(
            self, "occurred_at", require_nonempty(self.occurred_at, "occurred_at")
        )
        if self.idempotency_key is not None:
            object.__setattr__(
                self,
                "idempotency_key",
                require_nonempty(self.idempotency_key, "idempotency_key"),
            )
        for field in ("goal_id", "run_id", "activation_id"):
            if getattr(self, field) is not None:
                object.__setattr__(
                    self, field, require_identifier(getattr(self, field), field)
                )
        object.__setattr__(
            self,
            "subject_oid",
            require_oid(self.subject_oid, "subject_oid", optional=True),
        )


@dataclass(frozen=True, slots=True)
class IntentRecord:
    intent_id: str
    operation: str
    idempotency_key: str
    expected_state: str
    expected_oid: str | None
    payload: Mapping[str, Any]
    status: IntentStatus
    created_at: str
    resulting_state: str | None = None
    resulting_oid: str | None = None
    evidence: Mapping[str, Any] = MappingProxyType({})
    completed_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", require_identifier(self.intent_id, "intent_id"))
        object.__setattr__(self, "operation", require_identifier(self.operation, "operation"))
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )
        object.__setattr__(
            self, "expected_state", require_nonempty(self.expected_state, "expected_state")
        )
        object.__setattr__(
            self,
            "expected_oid",
            require_oid(self.expected_oid, "expected_oid", optional=True),
        )
        object.__setattr__(self, "payload", freeze_mapping(self.payload, "payload"))
        object.__setattr__(self, "status", _enum_value(self.status, IntentStatus, "status"))
        object.__setattr__(self, "created_at", require_nonempty(self.created_at, "created_at"))
        if self.resulting_state is not None:
            object.__setattr__(
                self,
                "resulting_state",
                require_nonempty(self.resulting_state, "resulting_state"),
            )
        object.__setattr__(
            self,
            "resulting_oid",
            require_oid(self.resulting_oid, "resulting_oid", optional=True),
        )
        object.__setattr__(self, "evidence", freeze_mapping(self.evidence, "evidence"))
        if self.completed_at is not None:
            object.__setattr__(
                self, "completed_at", require_nonempty(self.completed_at, "completed_at")
            )


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    finding_id: str
    resource_type: str
    resource_id: str
    severity: FindingSeverity
    state: FindingState
    expected: Mapping[str, Any]
    observed: Mapping[str, Any]
    detected_at: str
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "finding_id", require_identifier(self.finding_id, "finding_id")
        )
        for field in ("resource_type", "resource_id"):
            object.__setattr__(self, field, require_nonempty(getattr(self, field), field))
        object.__setattr__(
            self, "severity", _enum_value(self.severity, FindingSeverity, "severity")
        )
        object.__setattr__(self, "state", _enum_value(self.state, FindingState, "state"))
        object.__setattr__(self, "expected", freeze_mapping(self.expected, "expected"))
        object.__setattr__(self, "observed", freeze_mapping(self.observed, "observed"))
        object.__setattr__(
            self, "detected_at", require_nonempty(self.detected_at, "detected_at")
        )
        object.__setattr__(
            self, "idempotency_key", require_nonempty(self.idempotency_key, "idempotency_key")
        )


@dataclass(frozen=True, slots=True)
class PhysicalSeatIdentity:
    """A schedulable physical identity; it is not an expertise capability."""

    physical_seat_id: str
    seat_key: str
    state: PhysicalSeatState = PhysicalSeatState.ACTIVE
    merged: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "physical_seat_id",
            require_identifier(self.physical_seat_id, "physical_seat_id"),
        )
        object.__setattr__(self, "seat_key", require_identifier(self.seat_key, "seat_key"))
        object.__setattr__(
            self,
            "state",
            _enum_value(self.state, PhysicalSeatState, "state"),
        )
        object.__setattr__(self, "merged", require_boolean(self.merged, "merged"))


@dataclass(frozen=True, slots=True)
class LogicalCapability:
    """Workflow authority attached to a capability, never to a profile or Skill."""

    capability_id: str
    capability_key: str
    state: LogicalCapabilityState = LogicalCapabilityState.ACTIVE
    approval_authority: bool = False
    merge_authority: bool = False
    nested_spawn_authority: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capability_id",
            require_identifier(self.capability_id, "capability_id"),
        )
        object.__setattr__(
            self,
            "capability_key",
            require_identifier(self.capability_key, "capability_key"),
        )
        object.__setattr__(
            self,
            "state",
            _enum_value(self.state, LogicalCapabilityState, "state"),
        )
        for field in (
            "approval_authority",
            "merge_authority",
            "nested_spawn_authority",
        ):
            object.__setattr__(
                self,
                field,
                require_boolean(getattr(self, field), field),
            )


@dataclass(frozen=True, slots=True)
class WorkerFingerprint:
    worker_fingerprint_id: str
    worker_id: str
    fingerprint_sha256: str
    runtime_profile_digest: str
    state: WorkerFingerprintState = WorkerFingerprintState.ACTIVE

    def __post_init__(self) -> None:
        for field in ("worker_fingerprint_id", "worker_id"):
            object.__setattr__(
                self,
                field,
                require_identifier(getattr(self, field), field),
            )
        object.__setattr__(
            self,
            "fingerprint_sha256",
            require_sha256(self.fingerprint_sha256, "fingerprint_sha256"),
        )
        object.__setattr__(
            self,
            "runtime_profile_digest",
            require_sha256(self.runtime_profile_digest, "runtime_profile_digest"),
        )
        object.__setattr__(
            self,
            "state",
            _enum_value(self.state, WorkerFingerprintState, "state"),
        )


@dataclass(frozen=True, slots=True)
class RegisteredDefinition:
    definition_id: str
    kind: DefinitionKind
    version: str
    sha256: str
    source_ref: str
    registered_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "definition_id",
            require_identifier(self.definition_id, "definition_id"),
        )
        object.__setattr__(
            self,
            "kind",
            require_definition_kind(self.kind),
        )
        object.__setattr__(self, "version", require_nonempty(self.version, "version"))
        object.__setattr__(self, "sha256", require_sha256(self.sha256, "sha256"))
        object.__setattr__(
            self,
            "source_ref",
            require_nonempty(self.source_ref, "source_ref"),
        )
        object.__setattr__(
            self,
            "registered_at",
            require_nonempty(self.registered_at, "registered_at"),
        )


@dataclass(frozen=True, slots=True)
class ContractAdmissionRecord:
    admission_id: str
    contract_id: str
    decision: AdmissionDecision
    contract_digest: str
    admitted_at: str
    reason_code: str | None = None

    def __post_init__(self) -> None:
        for field in ("admission_id", "contract_id"):
            object.__setattr__(
                self,
                field,
                require_identifier(getattr(self, field), field),
            )
        object.__setattr__(
            self,
            "decision",
            _enum_value(self.decision, AdmissionDecision, "decision"),
        )
        object.__setattr__(
            self,
            "contract_digest",
            require_sha256(self.contract_digest, "contract_digest"),
        )
        object.__setattr__(
            self,
            "admitted_at",
            require_nonempty(self.admitted_at, "admitted_at"),
        )
        if self.decision is AdmissionDecision.ACCEPTED:
            if self.reason_code is not None:
                raise DomainValidationError(
                    "accepted admission must not include reason_code"
                )
        else:
            object.__setattr__(
                self,
                "reason_code",
                require_identifier(self.reason_code, "reason_code"),
            )


@dataclass(frozen=True, slots=True)
class ContractAttemptRecord:
    attempt_id: str
    contract_id: str
    admission_id: str
    worker_fingerprint_id: str
    attempt_number: int
    kind: ContractAttemptKind
    input_digest: str
    state: ContractAttemptState

    def __post_init__(self) -> None:
        for field in (
            "attempt_id",
            "contract_id",
            "admission_id",
            "worker_fingerprint_id",
        ):
            object.__setattr__(
                self,
                field,
                require_identifier(getattr(self, field), field),
            )
        object.__setattr__(
            self,
            "attempt_number",
            require_positive(self.attempt_number, "attempt_number"),
        )
        object.__setattr__(
            self,
            "kind",
            _enum_value(self.kind, ContractAttemptKind, "kind"),
        )
        object.__setattr__(
            self,
            "input_digest",
            require_sha256(self.input_digest, "input_digest"),
        )
        object.__setattr__(
            self,
            "state",
            _enum_value(self.state, ContractAttemptState, "state"),
        )
        expected_number = 1 if self.kind is ContractAttemptKind.PRIMARY else 2
        if self.attempt_number != expected_number:
            raise DomainValidationError(
                f"{self.kind.value} attempt_number must be {expected_number}"
            )


@dataclass(frozen=True, slots=True)
class ContractViolationRecord:
    violation_id: str
    contract_id: str
    worker_fingerprint_id: str
    code: ContractViolationCode
    disposition: ViolationDisposition
    evidence_digest: str
    occurred_at: str
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        for field in ("violation_id", "contract_id", "worker_fingerprint_id"):
            object.__setattr__(
                self,
                field,
                require_identifier(getattr(self, field), field),
            )
        if self.attempt_id is not None:
            object.__setattr__(
                self,
                "attempt_id",
                require_identifier(self.attempt_id, "attempt_id"),
            )
        object.__setattr__(
            self,
            "code",
            _enum_value(self.code, ContractViolationCode, "code"),
        )
        object.__setattr__(
            self,
            "disposition",
            _enum_value(self.disposition, ViolationDisposition, "disposition"),
        )
        object.__setattr__(
            self,
            "evidence_digest",
            require_sha256(self.evidence_digest, "evidence_digest"),
        )
        object.__setattr__(
            self,
            "occurred_at",
            require_nonempty(self.occurred_at, "occurred_at"),
        )


@dataclass(frozen=True, slots=True)
class SerenaMemoryBinding:
    memory_name: str
    memory_ref: str
    memory_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "memory_name",
            require_identifier(self.memory_name, "memory_name"),
        )
        object.__setattr__(
            self,
            "memory_ref",
            require_nonempty(self.memory_ref, "memory_ref"),
        )
        object.__setattr__(
            self,
            "memory_sha256",
            require_sha256(self.memory_sha256, "memory_sha256"),
        )


@dataclass(frozen=True, slots=True)
class SerenaOnboardingSnapshot:
    snapshot_id: str
    repository_id: str
    source_oid: str
    policy_digest: str
    manifest_digest: str
    memories: tuple[SerenaMemoryBinding, ...]
    state: SerenaSnapshotState = SerenaSnapshotState.ACCEPTED

    def __post_init__(self) -> None:
        for field in ("snapshot_id", "repository_id"):
            object.__setattr__(
                self,
                field,
                require_identifier(getattr(self, field), field),
            )
        object.__setattr__(
            self,
            "source_oid",
            require_oid(self.source_oid, "source_oid"),
        )
        for field in ("policy_digest", "manifest_digest"):
            object.__setattr__(
                self,
                field,
                require_sha256(getattr(self, field), field),
            )
        if not isinstance(self.memories, tuple) or not self.memories:
            raise DomainValidationError("memories must be a non-empty tuple")
        if any(not isinstance(item, SerenaMemoryBinding) for item in self.memories):
            raise DomainValidationError(
                "memories must contain SerenaMemoryBinding values"
            )
        names = tuple(item.memory_name for item in self.memories)
        if len(set(names)) != len(names):
            raise DomainValidationError("memories must not contain duplicate names")
        object.__setattr__(
            self,
            "state",
            _enum_value(self.state, SerenaSnapshotState, "state"),
        )
