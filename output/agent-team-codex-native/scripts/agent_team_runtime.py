"""Fail-closed execution contracts for Agent Team worktree activations.

The scheduler and TaskFlow layer may decide *what* to run.  This module is the
only boundary allowed to decide whether a submitted activation is sufficiently
bound and confined to run it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

try:
    from scripts.agent_team_profiles import PROFESSIONAL_SKILL_ID
    from scripts.agent_team_state import (
        AxStateStore,
        _MCP_RECEIPT_BROKER_AUTHORITY,
        _mcp_receipt_writer_for_broker,
    )
except ModuleNotFoundError:
    from agent_team_profiles import PROFESSIONAL_SKILL_ID  # type: ignore[no-redef]
    from agent_team_state import (  # type: ignore[no-redef]
        AxStateStore,
        _MCP_RECEIPT_BROKER_AUTHORITY,
        _mcp_receipt_writer_for_broker,
    )


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SAFE_ENV_KEYS = frozenset(
    {
        "CI",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TZ",
        "WINDIR",
    }
)
_FORBIDDEN_ENV_PARTS = (
    "ACCESS_KEY",
    "AUTH",
    "BEARER",
    "COOKIE",
    "CREDENTIAL",
    "GITHUB_",
    "GITLAB_",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)
_FORBIDDEN_GIT_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_ASKPASS",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_EXEC_PATH",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_WORK_TREE",
    }
)


class RunnerContractError(ValueError):
    """The immutable runner contract is incomplete or internally inconsistent."""


class BackendCapabilityError(RuntimeError):
    """The selected backend cannot prove the required confinement."""


class RunnerExecutionError(RuntimeError):
    """The backend violated its receipt contract or could not execute."""


class RunnerReplayConflictError(RuntimeError):
    """An idempotency key was reused for a different immutable request."""


class ExecutionKind(str, Enum):
    DEVELOPMENT = "development"
    REVIEW = "review"


class RunnerStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise RunnerContractError(f"{field} must be a non-empty stable identifier")
    return value


def _digest(value: str, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise RunnerContractError(f"{field} must be a lowercase sha256 digest")
    return value


def _oid(value: str, field: str) -> str:
    if not isinstance(value, str) or not _OID_RE.fullmatch(value):
        raise RunnerContractError(f"{field} must be a full 40-64 character object id")
    return value


def _absolute(path: str, field: str, *, directory: bool = True) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise RunnerContractError(f"{field} must be absolute")
    resolved = candidate.resolve(strict=False)
    if directory and (not resolved.exists() or not resolved.is_dir()):
        raise RunnerContractError(f"{field} must name an existing directory")
    return str(resolved)


def _is_within(path: str, root: str) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except ValueError:
        return False


def _paths_disjoint(left: Sequence[str], right: Sequence[str]) -> bool:
    for left_path in left:
        for right_path in right:
            if _is_within(left_path, right_path) or _is_within(right_path, left_path):
                return False
    return True


def _review_write_carveouts_are_safe(
    source_roots: Sequence[str],
    writable_roots: Sequence[str],
) -> bool:
    """Require review outputs to be disjoint from immutable source trees."""

    return _paths_disjoint(source_roots, writable_roots)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def environment_fingerprint(environment: Sequence[tuple[str, str]]) -> str:
    return sha256_json([[key, value] for key, value in environment])


def minimal_environment(
    overrides: Mapping[str, str] | None = None,
    *,
    source: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Build an explicit allowlisted environment without credentials or Git authority."""

    ambient = os.environ if source is None else source
    values = {key: ambient[key] for key in _SAFE_ENV_KEYS if key in ambient}
    for key, value in (overrides or {}).items():
        values[str(key)] = str(value)
    _validate_environment(values.items())
    return tuple(sorted(values.items()))


def _validate_environment(environment: Sequence[tuple[str, str]]) -> None:
    seen: set[str] = set()
    for raw_key, raw_value in environment:
        key = str(raw_key).upper()
        if not key or key in seen:
            raise RunnerContractError("environment keys must be non-empty and unique")
        seen.add(key)
        if key in _FORBIDDEN_GIT_ENV or any(part in key for part in _FORBIDDEN_ENV_PARTS):
            raise RunnerContractError(f"credential or Git authority environment is prohibited: {key}")
        if key not in _SAFE_ENV_KEYS:
            raise RunnerContractError(f"environment key is not allowlisted: {key}")
        if "\x00" in str(raw_value):
            raise RunnerContractError(f"environment value contains NUL: {key}")


def _redact(text: str, patterns: Sequence[str]) -> str:
    result = text
    for pattern in patterns:
        if pattern:
            result = result.replace(pattern, "[REDACTED]")
    return result


def _bounded_text(raw: bytes, limit: int, patterns: Sequence[str]) -> str:
    text = _redact(raw[:limit].decode("utf-8", "replace"), patterns)
    while text and len(text.encode("utf-8")) > limit:
        text = text[:-1]
    return text


@dataclass(frozen=True)
class ToolPolicy:
    policy_id: str
    command_prefixes: tuple[tuple[str, ...], ...]
    network_allowed: bool = False
    shell_allowed: bool = False

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "tool_policy.policy_id")
        if not self.command_prefixes:
            raise RunnerContractError("tool policy requires at least one command prefix")
        for prefix in self.command_prefixes:
            if not prefix or any(not item or "\x00" in item for item in prefix):
                raise RunnerContractError("tool command prefixes must contain non-empty arguments")

    def permits(self, command: Sequence[str]) -> bool:
        return any(tuple(command[: len(prefix)]) == prefix for prefix in self.command_prefixes)


@dataclass(frozen=True)
class OutputPolicy:
    stdout_limit_bytes: int = 262_144
    stderr_limit_bytes: int = 262_144
    redaction_literals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.stdout_limit_bytes < 1 or self.stderr_limit_bytes < 1:
            raise RunnerContractError("output limits must be positive")
        if self.stdout_limit_bytes > 4_194_304 or self.stderr_limit_bytes > 4_194_304:
            raise RunnerContractError("output limits may not exceed 4 MiB")


@dataclass(frozen=True)
class RunnerRequest:
    target_id: str
    goal_id: str
    work_item_id: str
    revision: int
    activation_id: str
    workspace_id: str
    lease_id: str | None
    sandbox_id: str | None
    execution_kind: ExecutionKind
    base_oid: str
    head_oid: str
    subject_oid: str
    seat_id: str
    role_key: str
    gate_id: str
    model: str
    model_reasoning_effort: str
    cwd: str
    resource_root: str
    activation_root: str
    source_scope: tuple[str, ...]
    generated_scope: tuple[str, ...]
    source_roots: tuple[str, ...]
    generated_roots: tuple[str, ...]
    ephemeral_writable_roots: tuple[str, ...]
    writable_roots: tuple[str, ...]
    protected_roots: tuple[str, ...]
    prohibited_roots: tuple[str, ...]
    professional_skill_id: str
    compiled_profile_ref: str
    compiled_profile_digest: str
    context_ref: str
    context_digest: str
    tool_policy: ToolPolicy
    idempotency_key: str
    environment: tuple[tuple[str, str], ...]
    environment_digest: str
    command: tuple[str, ...]
    stdin: str
    timeout_seconds: float
    output_policy: OutputPolicy

    def __post_init__(self) -> None:
        for field_name in (
            "target_id",
            "goal_id",
            "work_item_id",
            "activation_id",
            "workspace_id",
            "seat_id",
            "role_key",
            "gate_id",
            "model",
            "model_reasoning_effort",
            "professional_skill_id",
            "idempotency_key",
        ):
            _identifier(str(getattr(self, field_name)), field_name)
        if self.revision < 1:
            raise RunnerContractError("revision must be positive")
        _oid(self.base_oid, "base_oid")
        _oid(self.head_oid, "head_oid")
        _oid(self.subject_oid, "subject_oid")
        _digest(self.compiled_profile_digest, "compiled_profile_digest")
        _digest(self.context_digest, "context_digest")
        if self.execution_kind == ExecutionKind.DEVELOPMENT:
            if not self.lease_id or self.sandbox_id is not None:
                raise RunnerContractError("development execution requires lease_id and forbids sandbox_id")
            _identifier(self.lease_id, "lease_id")
            if self.subject_oid != self.head_oid:
                raise RunnerContractError("development subject_oid must equal head_oid")
        elif self.execution_kind == ExecutionKind.REVIEW:
            if not self.sandbox_id or self.lease_id is not None:
                raise RunnerContractError("review execution requires sandbox_id and forbids lease_id")
            _identifier(self.sandbox_id, "sandbox_id")
            if self.workspace_id != self.sandbox_id:
                raise RunnerContractError("review workspace_id must equal sandbox_id")
        else:
            raise RunnerContractError("unsupported execution kind")

        directory_fields = ("cwd", "resource_root", "activation_root")
        normalized = {name: _absolute(getattr(self, name), name) for name in directory_fields}
        if self.execution_kind == ExecutionKind.DEVELOPMENT:
            if normalized["cwd"] != normalized["resource_root"]:
                raise RunnerContractError("developer cwd must be the assigned worktree")
        elif not _is_within(normalized["cwd"], normalized["resource_root"]):
            raise RunnerContractError("review cwd must be inside its activation root")
        for field_name in (
            "source_roots",
            "generated_roots",
            "ephemeral_writable_roots",
            "writable_roots",
            "protected_roots",
            "prohibited_roots",
        ):
            paths = getattr(self, field_name)
            if len(paths) != len(set(paths)):
                raise RunnerContractError(f"{field_name} contains duplicates")
            for index, path in enumerate(paths):
                _absolute(path, f"{field_name}[{index}]")
        if not self.source_roots:
            raise RunnerContractError("at least one source root is required")
        if not all(_is_within(path, self.resource_root) for path in self.source_roots):
            raise RunnerContractError("source roots must be confined to resource_root")
        authority_roots = self.source_roots + self.generated_roots + self.ephemeral_writable_roots
        expected_writable = self.generated_roots + self.ephemeral_writable_roots
        if self.execution_kind == ExecutionKind.DEVELOPMENT:
            expected_writable = self.source_roots + expected_writable
        if set(self.writable_roots) != set(expected_writable):
            raise RunnerContractError(
                "writable_roots do not match the execution-kind authority roots"
            )
        if not all(_is_within(path, self.resource_root) for path in authority_roots):
            raise RunnerContractError("all execution roots must be confined to resource_root")
        if (
            self.execution_kind == ExecutionKind.REVIEW
            and not _review_write_carveouts_are_safe(
                self.source_roots, self.writable_roots
            )
        ):
            raise RunnerContractError(
                "review writable roots must be disjoint from read-only source roots"
            )
        if not _paths_disjoint(authority_roots, self.protected_roots + self.prohibited_roots):
            raise RunnerContractError("execution authority overlaps a protected or prohibited root")
        if self.professional_skill_id != PROFESSIONAL_SKILL_ID:
            raise RunnerContractError(
                f"exactly the {PROFESSIONAL_SKILL_ID} skill must own the profile binding"
            )

        profile_path = Path(self.compiled_profile_ref).resolve(strict=False)
        context_path = Path(self.context_ref).resolve(strict=False)
        for path, field_name in (
            (profile_path, "compiled_profile_ref"),
            (context_path, "context_ref"),
        ):
            if not path.is_absolute() or not path.is_file() or path.is_symlink():
                raise RunnerContractError(f"{field_name} must be an existing regular file")
            if not _is_within(str(path), self.activation_root):
                raise RunnerContractError(f"{field_name} must be inside activation_root")

        _validate_environment(self.environment)
        if self.environment != tuple(sorted(self.environment)):
            raise RunnerContractError("environment must be sorted for deterministic execution")
        _digest(self.environment_digest, "environment_digest")
        if environment_fingerprint(self.environment) != self.environment_digest:
            raise RunnerContractError("environment fingerprint mismatch")
        if not self.command or any(not item or "\x00" in item for item in self.command):
            raise RunnerContractError("command must contain non-empty arguments")
        if not self.tool_policy.permits(self.command):
            raise RunnerContractError("command is outside the compiled tool policy")
        if not 0 < self.timeout_seconds <= 3600:
            raise RunnerContractError("timeout_seconds must be in (0, 3600]")

    def canonical_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["execution_kind"] = self.execution_kind.value
        return payload

    @property
    def request_digest(self) -> str:
        return sha256_json(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class OpaqueArtifactRef:
    """An immutable Phase-owned artifact reference; contents remain opaque."""

    ref: str
    digest: str

    def __post_init__(self) -> None:
        path = _absolute(self.ref, "artifact ref", directory=False)
        digest = _digest(self.digest, "artifact digest")
        object.__setattr__(self, "ref", path)
        object.__setattr__(self, "digest", digest)


@dataclass(frozen=True, slots=True)
class RuntimeSandboxBinding:
    contract_id: str
    activation_id: str
    attempt_id: str
    repository_id: str
    lease_id: str
    sandbox_binding_id: str
    oid_authority_id: str
    slot_id: str
    capability_id: str
    attestation_digest: str
    request: RunnerRequest

    def __post_init__(self) -> None:
        for field_name in (
            "contract_id",
            "activation_id",
            "attempt_id",
            "repository_id",
            "lease_id",
            "sandbox_binding_id",
            "oid_authority_id",
            "slot_id",
            "capability_id",
        ):
            object.__setattr__(
                self, field_name, _identifier(getattr(self, field_name), field_name)
            )
        object.__setattr__(
            self,
            "attestation_digest",
            _digest(self.attestation_digest, "attestation_digest"),
        )
        if not isinstance(self.request, RunnerRequest):
            raise RunnerContractError("sandbox binding request must be RunnerRequest")


@dataclass(frozen=True)
class BackendCapabilities:
    backend_id: str
    attestation_id: str
    production: bool
    trusted_test_fixture: bool
    enforces_cwd: bool
    enforces_writable_roots: bool
    enforces_protected_roots: bool
    enforces_prohibited_roots: bool
    enforces_minimal_environment: bool
    enforces_timeout: bool
    bounds_output: bool

    def __post_init__(self) -> None:
        _identifier(self.backend_id, "backend_id")
        _identifier(self.attestation_id, "attestation_id")
        if self.production and self.trusted_test_fixture:
            raise RunnerContractError("a production backend cannot be a trusted test fixture")

    @property
    def proves_production_confinement(self) -> bool:
        return self.production and all(
            (
                self.enforces_cwd,
                self.enforces_writable_roots,
                self.enforces_protected_roots,
                self.enforces_prohibited_roots,
                self.enforces_minimal_environment,
                self.enforces_timeout,
                self.bounds_output,
            )
        )


@dataclass(frozen=True)
class BackendExecutionReceipt:
    backend_id: str
    attestation_id: str
    observed_cwd: str
    observed_environment_digest: str
    enforced_writable_roots: tuple[str, ...]
    enforced_protected_roots: tuple[str, ...]
    enforced_prohibited_roots: tuple[str, ...]
    started_at_ns: int
    finished_at_ns: int
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


@dataclass(frozen=True, slots=True)
class McpInvocationReceiptRef:
    """Immutable reference to evidence written by the runtime MCP broker."""

    receipt_id: str
    server_id: str
    tool_id: str
    activation_id: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        for field_name in ("receipt_id", "server_id", "tool_id", "activation_id"):
            object.__setattr__(
                self, field_name, _identifier(getattr(self, field_name), field_name)
            )
        object.__setattr__(
            self,
            "evidence_sha256",
            _digest(self.evidence_sha256, "evidence_sha256"),
        )


@dataclass(frozen=True, slots=True)
class McpInvocationContext:
    """Durable attempt identity consumed by the broker invocation boundary."""

    contract_id: str
    activation_id: str
    attempt_id: str
    repository_id: str
    request_digest: str

    def __post_init__(self) -> None:
        for field_name in (
            "contract_id",
            "activation_id",
            "attempt_id",
            "repository_id",
        ):
            object.__setattr__(
                self, field_name, _identifier(getattr(self, field_name), field_name)
            )
        object.__setattr__(
            self,
            "request_digest",
            _digest(self.request_digest, "request_digest"),
        )


@dataclass(frozen=True)
class RunnerResult:
    result_id: str
    request_digest: str
    activation_id: str
    idempotency_key: str
    status: RunnerStatus
    artifact_ref: str
    backend_capabilities: BackendCapabilities
    receipt: BackendExecutionReceipt
    trusted_mcp_receipts: tuple[McpInvocationReceiptRef, ...] = ()
    replayed: bool = False

    def as_mapping(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RunnerResult:
        """Strictly restore the signed runner result crossing an XCom boundary."""

        if not isinstance(value, Mapping):
            raise ValueError("runner result must be an object")
        try:
            capabilities_raw = value["backend_capabilities"]
            receipt_raw = value["receipt"]
            trusted_raw = value.get("trusted_mcp_receipts", ())
            if not isinstance(capabilities_raw, Mapping):
                raise ValueError("backend_capabilities must be an object")
            if not isinstance(receipt_raw, Mapping):
                raise ValueError("receipt must be an object")
            if not isinstance(trusted_raw, Sequence) or isinstance(
                trusted_raw, (str, bytes)
            ):
                raise ValueError("trusted_mcp_receipts must be an array")
            capabilities = BackendCapabilities(**dict(capabilities_raw))
            receipt_mapping = dict(receipt_raw)
            for field_name in (
                "enforced_writable_roots",
                "enforced_protected_roots",
                "enforced_prohibited_roots",
            ):
                roots = receipt_mapping[field_name]
                if not isinstance(roots, Sequence) or isinstance(roots, (str, bytes)):
                    raise ValueError(f"{field_name} must be an array")
                receipt_mapping[field_name] = tuple(str(item) for item in roots)
            receipt = BackendExecutionReceipt(**receipt_mapping)
            trusted_receipts = tuple(
                item
                if isinstance(item, McpInvocationReceiptRef)
                else McpInvocationReceiptRef(**dict(item))
                for item in trusted_raw
                if isinstance(item, (Mapping, McpInvocationReceiptRef))
            )
            if len(trusted_receipts) != len(trusted_raw):
                raise ValueError("each trusted MCP receipt must be an object")
            result = cls(
                result_id=_identifier(value["result_id"], "result_id"),
                request_digest=_digest(value["request_digest"], "request_digest"),
                activation_id=_identifier(value["activation_id"], "activation_id"),
                idempotency_key=str(value["idempotency_key"]),
                status=RunnerStatus(str(value["status"])),
                artifact_ref=str(value["artifact_ref"]),
                backend_capabilities=capabilities,
                receipt=receipt,
                trusted_mcp_receipts=trusted_receipts,
                replayed=value.get("replayed", False) is True,
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("runner result is incomplete or malformed") from exc
        if not result.idempotency_key:
            raise ValueError("runner result idempotency_key is required")
        if len({item.receipt_id for item in result.trusted_mcp_receipts}) != len(
            result.trusted_mcp_receipts
        ):
            raise ValueError("trusted MCP receipt IDs must be unique")
        return result


class ExecutionBackend(Protocol):
    def capabilities(self) -> BackendCapabilities:
        ...

    def execute(self, request: RunnerRequest) -> BackendExecutionReceipt:
        ...


class SeatPolicyProvider(Protocol):
    def resolve(self, seat_id: str) -> Mapping[str, Any]:
        ...


class McpToolInvoker(Protocol):
    """Invoke one concrete MCP server/tool through the trusted runtime path."""

    def invoke(
        self,
        server_name: str,
        tool_name: str,
        input_payload: Mapping[str, Any],
    ) -> Any:
        ...


def _invocation_evidence_digest(value: Any) -> str:
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    try:
        return sha256_json(value)
    except (TypeError, ValueError) as exc:
        raise RunnerExecutionError(
            "MCP invocation output is not canonical evidence"
        ) from exc


class McpInvocationBroker:
    """Own required MCP calls and write receipts from observed invocation bytes."""

    def __init__(self, *, state_store: AxStateStore, invoker: McpToolInvoker) -> None:
        if not isinstance(state_store, AxStateStore):
            raise TypeError("MCP broker requires an AxStateStore")
        self._state_store = state_store
        self._invoker = invoker
        self.__receipt_writer = _mcp_receipt_writer_for_broker(
            state_store,
            _MCP_RECEIPT_BROKER_AUTHORITY,
        )

    @property
    def state_store(self) -> AxStateStore:
        return self._state_store

    def invoke_required(
        self, binding: RuntimeSandboxBinding
    ) -> tuple[McpInvocationReceiptRef, ...]:
        if not isinstance(binding, RuntimeSandboxBinding):
            raise RunnerContractError("MCP broker requires a runtime sandbox binding")
        return self.invoke_required_context(
            McpInvocationContext(
                contract_id=binding.contract_id,
                activation_id=binding.activation_id,
                attempt_id=binding.attempt_id,
                repository_id=binding.repository_id,
                request_digest=binding.request.request_digest,
            )
        )

    def invoke_required_context(
        self, context: McpInvocationContext
    ) -> tuple[McpInvocationReceiptRef, ...]:
        if not isinstance(context, McpInvocationContext):
            raise RunnerContractError("MCP broker requires an invocation context")
        with self._state_store.transaction() as connection:
            scope = connection.execute(
                """
                SELECT contract_attempts.state AS attempt_state,
                       activation_contracts.state AS contract_state,
                       activation_contracts.repository_id
                FROM contract_attempts
                JOIN activation_contracts
                  ON activation_contracts.id = contract_attempts.contract_id
                WHERE contract_attempts.id = ?
                  AND contract_attempts.contract_id = ?
                """,
                (context.attempt_id, context.contract_id),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT contract_mcp_bindings.id AS binding_id,
                       contract_mcp_bindings.mcp_definition_id,
                       contract_mcp_bindings.trigger_rule,
                       mcp_definitions.server_name,
                       mcp_definitions.tool_name,
                       mcp_definitions.state AS definition_state,
                       (
                           SELECT mcp_health_observations.status
                           FROM mcp_health_observations
                           WHERE mcp_health_observations.mcp_definition_id =
                                 contract_mcp_bindings.mcp_definition_id
                             AND mcp_health_observations.contract_id =
                                 contract_mcp_bindings.contract_id
                           ORDER BY mcp_health_observations.observed_at DESC,
                                    mcp_health_observations.rowid DESC
                           LIMIT 1
                       ) AS current_health_status
                FROM contract_mcp_bindings
                LEFT JOIN mcp_definitions
                  ON mcp_definitions.id = contract_mcp_bindings.mcp_definition_id
                WHERE contract_mcp_bindings.contract_id = ?
                  AND contract_mcp_bindings.invocation_required = 1
                ORDER BY contract_mcp_bindings.id
                """,
                (context.contract_id,),
            ).fetchall()
        if scope is None:
            raise RunnerContractError("MCP invocation attempt scope is missing")
        if (
            scope["attempt_state"] not in {"CREATED", "RUNNING"}
            or scope["contract_state"] != "RUNNING"
            or scope["repository_id"] != context.repository_id
        ):
            raise RunnerContractError("MCP invocation attempt scope is not active")

        invocation_specs: list[tuple[str, str, str]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for row in rows:
            if (
                row["binding_id"] is None
                or row["mcp_definition_id"] is None
                or row["server_name"] is None
                or row["tool_name"] is None
            ):
                raise RunnerContractError(
                    "required MCP binding has no complete definition"
                )
            if row["definition_state"] != "ACTIVE":
                raise RunnerContractError("required MCP definition is not active")
            pair = (row["server_name"], row["tool_name"])
            if pair in seen_pairs:
                raise RunnerContractError(
                    "required MCP binding is duplicated or ambiguous"
                )
            seen_pairs.add(pair)
            if row["current_health_status"] != "HEALTHY":
                raise RunnerContractError(
                    "required MCP binding lacks current contract-bound healthy evidence"
                )
            invocation_specs.append((*pair, row["trigger_rule"]))

        refs: list[McpInvocationReceiptRef] = []
        for server_name, tool_name, trigger_rule in invocation_specs:
            invocation_input = {
                "schema_version": 4,
                "contract_id": context.contract_id,
                "activation_id": context.activation_id,
                "attempt_id": context.attempt_id,
                "repository_id": context.repository_id,
                "request_digest": context.request_digest,
                "trigger_rule": trigger_rule,
            }
            input_digest = sha256_json(invocation_input)
            output = self._invoker.invoke(
                server_name, tool_name, invocation_input
            )
            output_digest = _invocation_evidence_digest(output)
            receipt_id = self.__receipt_writer.record(
                contract_id=context.contract_id,
                attempt_id=context.attempt_id,
                server_name=server_name,
                tool_name=tool_name,
                input_digest=input_digest,
                output_digest=output_digest,
                idempotency_key=(
                    f"mcp-broker:{context.contract_id}:{context.attempt_id}:"
                    f"{server_name}:{tool_name}:{input_digest}"
                ),
            )
            refs.append(
                McpInvocationReceiptRef(
                    receipt_id=receipt_id,
                    server_id=server_name,
                    tool_id=tool_name,
                    activation_id=context.activation_id,
                    evidence_sha256=output_digest,
                )
            )
        if invocation_specs and len(refs) != len(invocation_specs):
            raise RunnerExecutionError(
                "required MCP invocation did not produce durable broker receipts"
            )
        return tuple(refs)


class ProjectAgentSeatPolicyProvider:
    """Adapter from the existing six-role/eight-seat project-agent bundle."""

    def __init__(self, bundle: Mapping[str, Any]) -> None:
        self._bundle = bundle

    def resolve(self, seat_id: str) -> Mapping[str, Any]:
        try:
            from scripts.project_agents import resolve_runtime_activation_contract
        except ModuleNotFoundError:
            from project_agents import resolve_runtime_activation_contract

        return resolve_runtime_activation_contract(self._bundle, seat_id)


class TrustedSubprocessTestBackend:
    """Real subprocess fixture for cwd/env/timeout/output tests.

    It deliberately does *not* claim filesystem isolation and is rejected unless
    AgentRuntime was explicitly created with ``allow_trusted_test_backend=True``.
    """

    _CAPABILITIES = BackendCapabilities(
        backend_id="trusted-subprocess-test",
        attestation_id="direct-subprocess-test-only",
        production=False,
        trusted_test_fixture=True,
        enforces_cwd=True,
        enforces_writable_roots=False,
        enforces_protected_roots=False,
        enforces_prohibited_roots=False,
        enforces_minimal_environment=True,
        enforces_timeout=True,
        bounds_output=True,
    )

    def capabilities(self) -> BackendCapabilities:
        return self._CAPABILITIES

    def execute(self, request: RunnerRequest) -> BackendExecutionReceipt:
        started_at_ns = time.time_ns()
        process = subprocess.Popen(
            request.command,
            cwd=request.cwd,
            env=dict(request.environment),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
        )
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = process.communicate(
                request.stdin.encode("utf-8"),
                timeout=request.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout_bytes, stderr_bytes = process.communicate()
        stdout_truncated = len(stdout_bytes) > request.output_policy.stdout_limit_bytes
        stderr_truncated = len(stderr_bytes) > request.output_policy.stderr_limit_bytes
        stdout = _bounded_text(
            stdout_bytes,
            request.output_policy.stdout_limit_bytes,
            request.output_policy.redaction_literals,
        )
        stderr = _bounded_text(
            stderr_bytes,
            request.output_policy.stderr_limit_bytes,
            request.output_policy.redaction_literals,
        )
        return BackendExecutionReceipt(
            backend_id=self._CAPABILITIES.backend_id,
            attestation_id=self._CAPABILITIES.attestation_id,
            observed_cwd=str(Path(request.cwd).resolve(strict=False)),
            observed_environment_digest=environment_fingerprint(request.environment),
            enforced_writable_roots=(),
            enforced_protected_roots=(),
            enforced_prohibited_roots=(),
            started_at_ns=started_at_ns,
            finished_at_ns=time.time_ns(),
            exit_code=None if timed_out else process.returncode,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


class AgentRuntime:
    """Verify immutable bindings, execute once, and persist a deterministic result."""

    def __init__(
        self,
        *,
        backend: ExecutionBackend,
        seat_policy_provider: SeatPolicyProvider,
        result_root: str | Path,
        allow_trusted_test_backend: bool = False,
        state_store: AxStateStore | None = None,
        mcp_broker: McpInvocationBroker | None = None,
    ) -> None:
        self._backend = backend
        self._seat_policy_provider = seat_policy_provider
        self._result_root = Path(result_root).resolve(strict=False)
        self._result_root.mkdir(parents=True, exist_ok=True)
        self._allow_trusted_test_backend = allow_trusted_test_backend
        self._state_store = state_store
        self._mcp_broker = mcp_broker

    @property
    def backend_capabilities(self) -> BackendCapabilities:
        """Expose immutable backend identity/attestation for factory validation."""

        return self._backend.capabilities()

    @property
    def state_store(self) -> AxStateStore | None:
        """Expose the runtime's durable authority for identity comparison only."""

        return self._state_store

    @property
    def mcp_broker(self) -> McpInvocationBroker | None:
        """Expose the broker object so factories can enforce shared ownership."""

        return self._mcp_broker

    def execute(
        self,
        contract_ref: OpaqueArtifactRef,
        packet_ref: OpaqueArtifactRef,
        binding: RuntimeSandboxBinding,
    ) -> RunnerResult:
        """Execute only through the fully durable v4 production boundary."""

        if not isinstance(contract_ref, OpaqueArtifactRef):
            raise RunnerContractError("contract_ref must be an OpaqueArtifactRef")
        if not isinstance(packet_ref, OpaqueArtifactRef):
            raise RunnerContractError("packet_ref must be an OpaqueArtifactRef")
        if not isinstance(binding, RuntimeSandboxBinding):
            raise RunnerContractError("binding must be a RuntimeSandboxBinding")
        if not isinstance(self._state_store, AxStateStore):
            raise RunnerContractError("production execution requires an AxStateStore")
        if not isinstance(self._mcp_broker, McpInvocationBroker):
            raise RunnerContractError("production execution requires an MCP broker")
        if self._mcp_broker.state_store is not self._state_store:
            raise RunnerContractError(
                "runtime and MCP broker must share the same AxStateStore instance"
            )
        self._validate_v4_preflight(contract_ref, packet_ref, binding)
        request = binding.request
        artifact_path = self._artifact_path(request)
        if artifact_path.exists():
            replay = self._load_replay(artifact_path, request)
            self._state_store.validate_trusted_mcp_receipt_references(
                contract_id=binding.contract_id,
                attempt_id=binding.attempt_id,
                receipts=[asdict(item) for item in replay.trusted_mcp_receipts],
            )
            return replay
        trusted_receipts = self._mcp_broker.invoke_required(binding)
        self._validate_v4_preflight(contract_ref, packet_ref, binding)
        self._state_store.validate_trusted_mcp_receipt_references(
            contract_id=binding.contract_id,
            attempt_id=binding.attempt_id,
            receipts=[asdict(item) for item in trusted_receipts],
        )
        return self._execute_request(
            request,
            trusted_mcp_receipts=trusted_receipts,
            production=True,
        )

    def execute_test_only(self, request: RunnerRequest) -> RunnerResult:
        """Execute an explicitly non-production trusted subprocess fixture."""

        if not isinstance(request, RunnerRequest):
            raise RunnerContractError("test request must be a RunnerRequest")
        return self._execute_request(
            request,
            trusted_mcp_receipts=(),
            production=False,
        )

    def _execute_request(
        self,
        request: RunnerRequest,
        *,
        trusted_mcp_receipts: tuple[McpInvocationReceiptRef, ...],
        production: bool,
    ) -> RunnerResult:
        capabilities = self._backend.capabilities()
        self._validate_capabilities(capabilities, production=production)
        self._validate_seat_binding(request)
        self._validate_bound_artifacts(request)

        artifact_path = self._artifact_path(request)
        if artifact_path.exists():
            return self._load_replay(artifact_path, request)

        receipt = self._backend.execute(request)
        self._validate_receipt(request, capabilities, receipt)
        status = (
            RunnerStatus.TIMED_OUT
            if receipt.timed_out
            else RunnerStatus.SUCCEEDED
            if receipt.exit_code == 0
            else RunnerStatus.FAILED
        )
        result_id = "result-" + sha256_json(
            {
                "activation_id": request.activation_id,
                "idempotency_key": request.idempotency_key,
                "request_digest": request.request_digest,
            }
        )[:32]
        result = RunnerResult(
            result_id=result_id,
            request_digest=request.request_digest,
            activation_id=request.activation_id,
            idempotency_key=request.idempotency_key,
            status=status,
            artifact_ref=str(artifact_path),
            backend_capabilities=capabilities,
            receipt=receipt,
            trusted_mcp_receipts=trusted_mcp_receipts,
        )
        self._persist_once(artifact_path, result)
        return result

    @staticmethod
    def _verify_opaque_ref(reference: OpaqueArtifactRef, label: str) -> None:
        path = Path(reference.ref)
        if not path.is_file() or path.is_symlink():
            raise RunnerContractError(f"{label} artifact is missing or linked")
        if hashlib.sha256(path.read_bytes()).hexdigest() != reference.digest:
            raise RunnerContractError(f"{label} digest does not match its immutable file")

    def _validate_v4_preflight(
        self,
        contract_ref: OpaqueArtifactRef,
        packet_ref: OpaqueArtifactRef,
        binding: RuntimeSandboxBinding,
    ) -> None:
        self._verify_opaque_ref(contract_ref, "contract")
        self._verify_opaque_ref(packet_ref, "packet")
        if self._state_store is None:
            raise RunnerContractError("v4 execution requires an AxStateStore")
        try:
            contract_document = json.loads(Path(contract_ref.ref).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerContractError("contract artifact must be readable JSON") from exc
        if (
            contract_document.get("contract_id") != binding.contract_id
            or contract_document.get("activation_id") != binding.activation_id
        ):
            raise RunnerContractError(
                "contract artifact identity differs from runtime binding"
            )
        with self._state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT ac.*, sb.subject_oid AS bound_subject_oid,
                       sb.cwd AS bound_cwd, sb.source_root,
                       sb.source_read_only, sb.writable_roots_json,
                       sb.attestation_digest, sb.state AS sandbox_state,
                       rl.base_oid AS lease_base_oid,
                       rl.expected_head_oid, rl.protected_roots_json,
                       rl.state AS lease_state,
                       oa.oid AS authority_oid, oa.state AS authority_state,
                       rr.state AS repository_state, rs.state AS slot_state,
                       wsa.state AS assignment_state,
                       lc.capability_key,
                       sca.state AS seat_capability_state,
                       ca.contract_id AS attempt_contract_id,
                       ca.state AS attempt_state,
                       a.target_id AS activation_target_id,
                       a.goal_id AS activation_goal_id,
                       a.run_id AS activation_run_id,
                       a.subject_oid AS activation_subject_oid,
                       a.state AS activation_state,
                       rr.target_id AS repository_target_id
                FROM activation_contracts AS ac
                JOIN contract_attempts AS ca
                  ON ca.id = ? AND ca.contract_id = ac.id
                JOIN activations AS a ON a.id = ?
                JOIN sandbox_bindings AS sb ON sb.id = ac.sandbox_binding_id
                JOIN runtime_leases AS rl ON rl.id = ac.lease_id
                JOIN oid_authorities AS oa ON oa.id = ac.oid_authority_id
                JOIN repository_registrations AS rr ON rr.id = ac.repository_id
                JOIN runtime_slots AS rs ON rs.id = ac.slot_id
                JOIN worker_slot_assignments AS wsa
                  ON wsa.id = ac.worker_assignment_id
                JOIN logical_capabilities AS lc ON lc.id = ac.capability_id
                LEFT JOIN seat_capability_activations AS sca
                  ON sca.id = ac.seat_capability_activation_id
                WHERE ac.id = ?
                """,
                (binding.attempt_id, binding.activation_id, binding.contract_id),
            ).fetchone()
        if row is None:
            raise RunnerContractError("activation contract binding graph is missing")
        exact = {
            "repository_id": binding.repository_id,
            "lease_id": binding.lease_id,
            "sandbox_binding_id": binding.sandbox_binding_id,
            "oid_authority_id": binding.oid_authority_id,
            "slot_id": binding.slot_id,
            "capability_id": binding.capability_id,
        }
        if any(row[key] != value for key, value in exact.items()):
            raise RunnerContractError("runtime binding IDs differ from activation contract")
        if row["contract_digest"] != contract_ref.digest:
            raise RunnerContractError("contract ref digest differs from v4 contract")
        if row["packet_digest"] != packet_ref.digest:
            raise RunnerContractError("packet ref digest differs from v4 contract")
        if row["attestation_digest"] != binding.attestation_digest:
            raise RunnerContractError("sandbox attestation digest changed")
        if row["state"] not in {"ADMITTED", "RUNNING"}:
            raise RunnerContractError("activation contract is not admitted for execution")
        if (
            row["attempt_contract_id"] != binding.contract_id
            or row["attempt_state"] not in {"CREATED", "RUNNING"}
        ):
            raise RunnerContractError("runtime attempt is not active for this contract")
        if (
            row["activation_state"] != "RUNNING"
            or row["activation_goal_id"] != row["goal_id"]
            or row["activation_run_id"] != row["run_id"]
            or row["activation_target_id"] != row["repository_target_id"]
            or row["activation_subject_oid"] != row["subject_oid"]
        ):
            raise RunnerContractError("durable activation scope differs from contract")
        for key in (
            "sandbox_state",
            "lease_state",
            "authority_state",
            "repository_state",
            "assignment_state",
        ):
            if row[key] != "ACTIVE":
                raise RunnerContractError(f"v4 runtime relation is not active: {key}")
        if row["slot_state"] in {"QUARANTINED", "RETIRED"}:
            raise RunnerContractError("runtime slot is not executable")
        if row["seat_capability_activation_id"] is not None and row[
            "seat_capability_state"
        ] != "ACTIVE":
            raise RunnerContractError("merged-seat capability activation is not active")
        request = binding.request
        request_binding_mismatch = (
            request.lease_id != binding.lease_id
            if request.execution_kind is ExecutionKind.DEVELOPMENT
            else request.sandbox_id != binding.sandbox_binding_id
        )
        if (
            request.activation_id != binding.activation_id
            or request_binding_mismatch
            or request.base_oid != row["base_oid"]
            or row["lease_base_oid"] != row["base_oid"]
            or request.subject_oid != row["subject_oid"]
            or request.head_oid != row["expected_head_oid"]
            or row["authority_oid"] != row["subject_oid"]
            or row["bound_subject_oid"] != row["subject_oid"]
        ):
            raise RunnerContractError("request OID or runtime identity binding changed")
        if Path(request.cwd).resolve() != Path(row["bound_cwd"]).resolve():
            raise RunnerContractError("request cwd differs from sandbox binding")
        if Path(row["source_root"]).resolve() not in {
            Path(value).resolve() for value in request.source_roots
        }:
            raise RunnerContractError("sandbox source root is not request-bound")
        writable = tuple(json.loads(row["writable_roots_json"]))
        if tuple(request.writable_roots) != writable:
            raise RunnerContractError("request writable roots differ from sandbox binding")
        if row["source_read_only"] and any(
            _is_within(path, row["source_root"]) for path in request.writable_roots
        ):
            raise RunnerContractError("read-only review source appears writable")
        protected = tuple(json.loads(row["protected_roots_json"]))
        enforced_nonwritable = (*request.protected_roots, *request.prohibited_roots)
        if any(
            not (
                row["source_read_only"]
                and Path(value).resolve() == Path(row["source_root"]).resolve()
            )
            and not any(
                Path(value).resolve() == Path(bound).resolve()
                for bound in enforced_nonwritable
            )
            for value in protected
        ):
            raise RunnerContractError("lease protected roots are not fully confined")
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=request.cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if head.returncode != 0 or head.stdout.strip() != request.head_oid:
            raise RunnerContractError("sandbox HEAD changed before backend execution")

    def _validate_capabilities(
        self, capabilities: BackendCapabilities, *, production: bool
    ) -> None:
        if production and capabilities.proves_production_confinement:
            return
        if (
            not production
            and capabilities.trusted_test_fixture
            and not capabilities.production
            and self._allow_trusted_test_backend
        ):
            return
        raise BackendCapabilityError(
            "production execution requires full confinement attestation; test-only "
            "execution requires an explicitly allowed non-production fixture"
        )

    def _validate_seat_binding(self, request: RunnerRequest) -> None:
        policy = dict(self._seat_policy_provider.resolve(request.seat_id))
        required = {
            "seat_id": request.seat_id,
            "role_key": request.role_key,
            "model": request.model,
            "model_reasoning_effort": request.model_reasoning_effort,
            "professional_skill_id": request.professional_skill_id,
            "service_identity": False,
            "dynamic_confinement_required": True,
        }
        for key, expected in required.items():
            if policy.get(key) != expected:
                raise RunnerContractError(
                    f"seat activation mismatch for {key}: expected {expected!r}, "
                    f"received {policy.get(key)!r}"
                )

    def _validate_bound_artifacts(self, request: RunnerRequest) -> None:
        profile_path = Path(request.compiled_profile_ref)
        context_path = Path(request.context_ref)
        profile_bytes = profile_path.read_bytes()
        context_bytes = context_path.read_bytes()
        if hashlib.sha256(profile_bytes).hexdigest() != request.compiled_profile_digest:
            raise RunnerContractError("compiled profile digest does not match its immutable file")
        if hashlib.sha256(context_bytes).hexdigest() != request.context_digest:
            raise RunnerContractError("context digest does not match its immutable file")
        try:
            profile = json.loads(profile_bytes)
            context = json.loads(context_bytes)
        except json.JSONDecodeError as exc:
            raise RunnerContractError("profile and context artifacts must be JSON") from exc

        if profile.get("professional_skill_id") != request.professional_skill_id:
            raise RunnerContractError("compiled profile skill mismatch")
        refs = profile.get("references")
        if not isinstance(refs, list) or not 4 <= len(refs) <= 5:
            raise RunnerContractError("compiled profile must bind four or five profile references")
        reference_ids = [
            item.get("id") if isinstance(item, Mapping) else None for item in refs
        ]
        if any(not isinstance(value, str) or not value for value in reference_ids):
            raise RunnerContractError("compiled profile references must have stable IDs")
        if len(set(reference_ids)) != len(reference_ids):
            raise RunnerContractError("compiled profile reference IDs must be unique")

        professional = context.get("professional_profile")
        if not isinstance(professional, Mapping):
            raise RunnerContractError("context is missing the professional profile binding")
        if professional.get("skill_id") != request.professional_skill_id:
            raise RunnerContractError("context professional skill mismatch")
        if professional.get("compiled_profile_ref") != request.compiled_profile_ref:
            raise RunnerContractError("context compiled profile reference mismatch")
        if professional.get("compiled_profile_digest") != request.compiled_profile_digest:
            raise RunnerContractError("context compiled profile digest mismatch")
        if context.get("target_seat_id") != request.seat_id:
            raise RunnerContractError("context seat binding mismatch")
        if context.get("target_role") != request.role_key:
            raise RunnerContractError("context role binding mismatch")

        runtime_binding = context.get("runtime_binding")
        if not isinstance(runtime_binding, Mapping):
            raise RunnerContractError("context is missing its runtime binding")
        expected = {
            "activation_id": request.activation_id,
            "workspace_id": request.workspace_id,
            "lease_id": request.lease_id,
            "sandbox_id": request.sandbox_id,
            "execution_kind": request.execution_kind.value,
            "base_oid": request.base_oid,
            "head_oid": request.head_oid,
            "subject_oid": request.subject_oid,
            "seat_id": request.seat_id,
            "role_key": request.role_key,
            "cwd": request.cwd,
            "source_roots": list(request.source_roots),
            "generated_roots": list(request.generated_roots),
            "ephemeral_writable_roots": list(request.ephemeral_writable_roots),
            "protected_roots": list(request.protected_roots),
            "prohibited_roots": list(request.prohibited_roots),
            "environment_digest": request.environment_digest,
            "tool_policy_id": request.tool_policy.policy_id,
        }
        for key, value in expected.items():
            if runtime_binding.get(key) != value:
                raise RunnerContractError(f"context runtime binding mismatch for {key}")

    def _validate_receipt(
        self,
        request: RunnerRequest,
        capabilities: BackendCapabilities,
        receipt: BackendExecutionReceipt,
    ) -> None:
        if receipt.backend_id != capabilities.backend_id:
            raise RunnerExecutionError("backend receipt identity mismatch")
        if receipt.attestation_id != capabilities.attestation_id:
            raise RunnerExecutionError("backend receipt attestation mismatch")
        if Path(receipt.observed_cwd).resolve(strict=False) != Path(request.cwd).resolve(strict=False):
            raise RunnerExecutionError("backend executed outside the assigned cwd")
        if receipt.observed_environment_digest != request.environment_digest:
            raise RunnerExecutionError("backend environment differs from the requested minimal environment")
        if receipt.finished_at_ns < receipt.started_at_ns:
            raise RunnerExecutionError("backend receipt has an invalid clock interval")
        if receipt.timed_out != (receipt.exit_code is None):
            raise RunnerExecutionError("backend timeout and exit-code receipt disagree")
        if len(receipt.stdout.encode("utf-8")) > request.output_policy.stdout_limit_bytes:
            raise RunnerExecutionError("backend returned stdout beyond the configured bound")
        if len(receipt.stderr.encode("utf-8")) > request.output_policy.stderr_limit_bytes:
            raise RunnerExecutionError("backend returned stderr beyond the configured bound")
        if any(
            literal and (literal in receipt.stdout or literal in receipt.stderr)
            for literal in request.output_policy.redaction_literals
        ):
            raise RunnerExecutionError("backend returned an unredacted configured literal")
        if capabilities.proves_production_confinement:
            comparisons = (
                ("writable", receipt.enforced_writable_roots, request.writable_roots),
                ("protected", receipt.enforced_protected_roots, request.protected_roots),
                ("prohibited", receipt.enforced_prohibited_roots, request.prohibited_roots),
            )
            for label, actual, expected in comparisons:
                if tuple(actual) != tuple(expected):
                    raise RunnerExecutionError(f"backend did not attest exact {label} roots")

    def _artifact_path(self, request: RunnerRequest) -> Path:
        activation_dir = self._result_root / request.activation_id
        activation_dir.mkdir(parents=True, exist_ok=True)
        key_digest = hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()
        return activation_dir / f"{key_digest}.json"

    def _load_replay(self, path: Path, request: RunnerRequest) -> RunnerResult:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerReplayConflictError("existing deterministic result is unreadable") from exc
        if payload.get("request_digest") != request.request_digest:
            raise RunnerReplayConflictError(
                "idempotency key already belongs to a different immutable request"
            )
        result = self._result_from_mapping(payload)
        return RunnerResult(
            result_id=result.result_id,
            request_digest=result.request_digest,
            activation_id=result.activation_id,
            idempotency_key=result.idempotency_key,
            status=result.status,
            artifact_ref=result.artifact_ref,
            backend_capabilities=result.backend_capabilities,
            receipt=result.receipt,
            trusted_mcp_receipts=result.trusted_mcp_receipts,
            replayed=True,
        )

    @staticmethod
    def _persist_once(path: Path, result: RunnerResult) -> None:
        payload = canonical_json(result.as_mapping()) + "\n"
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists():
                existing = path.read_text(encoding="utf-8")
                if existing != payload:
                    raise RunnerReplayConflictError("deterministic result path already differs")
                Path(temp_name).unlink(missing_ok=True)
                return
            os.replace(temp_name, path)
        finally:
            Path(temp_name).unlink(missing_ok=True)

    @staticmethod
    def _result_from_mapping(payload: Mapping[str, Any]) -> RunnerResult:
        return RunnerResult.from_mapping(payload)


__all__ = [
    "AgentRuntime",
    "BackendCapabilities",
    "BackendCapabilityError",
    "BackendExecutionReceipt",
    "ExecutionBackend",
    "ExecutionKind",
    "McpInvocationBroker",
    "McpInvocationContext",
    "McpInvocationReceiptRef",
    "McpToolInvoker",
    "OutputPolicy",
    "OpaqueArtifactRef",
    "ProjectAgentSeatPolicyProvider",
    "RunnerContractError",
    "RunnerExecutionError",
    "RunnerReplayConflictError",
    "RunnerRequest",
    "RunnerResult",
    "RunnerStatus",
    "RuntimeSandboxBinding",
    "SeatPolicyProvider",
    "ToolPolicy",
    "TrustedSubprocessTestBackend",
    "canonical_json",
    "environment_fingerprint",
    "minimal_environment",
    "sha256_json",
]
