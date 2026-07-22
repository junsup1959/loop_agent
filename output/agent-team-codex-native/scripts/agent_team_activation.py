from __future__ import annotations

"""Exact-OID review sandboxes and disposable activation lifecycle.

Review sandboxes intentionally have independent Git metadata and no remote or
alternates route back to the managed bare authority.  Activation cleanup is a
separate, persisted state machine; a failed revocation or cleanup quarantines
the activation and never silently returns it to reusable capacity.
"""

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

try:
    from .agent_team_domain import (
        ActivationRecord,
        ActivationSpec,
        ActivationState,
        AuditEvent,
        SourceIntegrity,
        require_identifier,
        require_nonempty,
        require_oid,
        thaw_json,
    )
    from .agent_team_git import ManagedRepositoryService
    from .agent_team_paths import AxPathAuthority
    from .agent_team_profiles import PROFESSIONAL_SKILL_ID
    from .agent_team_state import AxStateStore, compact_json, utc_now
except ImportError:
    from agent_team_domain import (
        ActivationRecord,
        ActivationSpec,
        ActivationState,
        AuditEvent,
        SourceIntegrity,
        require_identifier,
        require_nonempty,
        require_oid,
        thaw_json,
    )
    from agent_team_git import ManagedRepositoryService
    from agent_team_paths import AxPathAuthority
    from agent_team_profiles import PROFESSIONAL_SKILL_ID
    from agent_team_state import AxStateStore, compact_json, utc_now


class ActivationError(RuntimeError):
    """Base error for review sandbox and activation lifecycle operations."""


class ReviewSandboxError(ActivationError):
    """Raised when an independent exact-OID sandbox cannot be materialized."""


class ReviewSandboxNotFoundError(ReviewSandboxError, KeyError):
    """Raised when a sandbox receipt does not exist."""


class ActivationStateError(ActivationError):
    """Raised when an activation attempts an invalid state transition."""


class ActivationIdentityError(ActivationError):
    """Raised when a PID cannot be proven to be the originally bound process."""


class ActivationQuarantinedError(ActivationStateError):
    """Raised when a quarantined activation is presented for reuse."""


class ProcessIdentityState(str, Enum):
    RUNNING_MATCH = "RUNNING_MATCH"
    EXITED = "EXITED"
    RUNNING_MISMATCH = "RUNNING_MISMATCH"


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    identity_token: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.pid, int)
            or isinstance(self.pid, bool)
            or self.pid < 1
        ):
            raise ActivationIdentityError("pid must be a positive integer")
        object.__setattr__(
            self,
            "identity_token",
            require_nonempty(self.identity_token, "identity_token"),
        )


class ProcessController(Protocol):
    def capture(self, pid: int) -> ProcessIdentity: ...

    def inspect(self, identity: ProcessIdentity) -> ProcessIdentityState: ...

    def terminate(self, identity: ProcessIdentity) -> None: ...


class ActivationResourceController(Protocol):
    def release(self, activation: ActivationRecord) -> None: ...

    def quarantine(self, activation: ActivationRecord, reason: str) -> None: ...

    def recovery_cleanup(self, activation: ActivationRecord) -> None: ...


class ProfileAccessController(Protocol):
    def revoke(
        self,
        *,
        activation_id: str,
        goal_id: str,
        compiled_profile_ref: Path,
        compiled_profile_digest: str,
    ) -> None: ...


class NoProcessController:
    """Fail-closed default; Phase 7 must inject a real PID identity controller."""

    def capture(self, pid: int) -> ProcessIdentity:
        raise ActivationIdentityError(
            "a process controller is required before binding a process ID"
        )

    def inspect(self, identity: ProcessIdentity) -> ProcessIdentityState:
        raise ActivationIdentityError(
            "a process controller is required before inspecting a process"
        )

    def terminate(self, identity: ProcessIdentity) -> None:
        raise ActivationIdentityError(
            "a process controller is required before terminating a process"
        )


class NoExternalResourceController:
    """Fail closed until Phase 7 supplies a lease/sandbox resource controller."""

    def release(self, activation: ActivationRecord) -> None:
        raise ActivationError(
            "a resource controller is required before releasing a bound activation"
        )

    def quarantine(self, activation: ActivationRecord, reason: str) -> None:
        return None

    def recovery_cleanup(self, activation: ActivationRecord) -> None:
        return None


class FilesystemProfileAccessController:
    """Revoke the activation-scoped compiled profile without touching sources."""

    def __init__(self, path_authority: AxPathAuthority) -> None:
        self.path_authority = path_authority

    def revoke(
        self,
        *,
        activation_id: str,
        goal_id: str,
        compiled_profile_ref: Path,
        compiled_profile_digest: str,
    ) -> None:
        expected_root = self.path_authority.activation_root(
            goal_id, activation_id
        ).resolve(strict=False)
        path = Path(compiled_profile_ref).expanduser().resolve(strict=False)
        if not _within(expected_root, path) or path == expected_root:
            raise ActivationError(
                "compiled profile ref is outside its activation authority"
            )
        if not path.is_file() or path.is_symlink():
            raise ActivationError(
                f"compiled profile artifact is missing or unsafe: {path}"
            )
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed != compiled_profile_digest:
            raise ActivationError(
                "compiled profile digest changed before revocation"
            )
        path.unlink()
        if path.exists():
            raise ActivationError("compiled profile artifact remains after revocation")


@dataclass(frozen=True, slots=True)
class ReviewRunnerContract:
    activation_id: str
    sandbox_id: str
    subject_oid: str
    cwd: Path
    analysis_source_root: Path
    ephemeral_writable_roots: tuple[Path, ...]
    generated_write_roots: tuple[Path, ...]
    protected_metadata_roots: tuple[Path, ...]
    prohibited_authority_roots: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class ReviewSandboxReceipt:
    sandbox_id: str
    activation_id: str
    target_id: str
    goal_id: str
    subject_oid: str
    subject_tree_oid: str
    sandbox_root: Path
    source_root: Path
    git_dir: Path
    scratch_root: Path
    build_root: Path
    cache_root: Path
    log_root: Path
    test_database_root: Path
    install_root: Path
    output_root: Path
    generated_paths: tuple[str, ...]
    prohibited_authority_roots: tuple[Path, ...]
    metadata_digest: str
    idempotency_key: str

    @property
    def runner_contract(self) -> ReviewRunnerContract:
        generated = tuple(
            self.source_root.joinpath(*PurePosixPath(path).parts).resolve(
                strict=False
            )
            for path in self.generated_paths
        )
        return ReviewRunnerContract(
            activation_id=self.activation_id,
            sandbox_id=self.sandbox_id,
            subject_oid=self.subject_oid,
            cwd=self.source_root,
            analysis_source_root=self.source_root,
            ephemeral_writable_roots=(
                self.build_root,
                self.test_database_root,
                self.cache_root,
                self.scratch_root,
                self.install_root,
            ),
            generated_write_roots=generated,
            protected_metadata_roots=(self.git_dir,),
            prohibited_authority_roots=self.prohibited_authority_roots,
        )


# Public architecture name; the persisted receipt is the sandbox authority.
ReviewSandbox = ReviewSandboxReceipt


@dataclass(frozen=True, slots=True)
class SourceIntegrityResult:
    sandbox_id: str
    subject_oid: str
    classification: SourceIntegrity
    observed_head_oid: str | None
    observed_tree_oid: str | None
    tracked_changes: tuple[str, ...]
    untracked_source_changes: tuple[str, ...]
    ignored_generated_paths: tuple[str, ...]
    reasons: tuple[str, ...]
    checked_at: str

    @property
    def gate_eligible(self) -> bool:
        return self.classification is SourceIntegrity.CLEAN


@dataclass(frozen=True, slots=True)
class ReviewSandboxDestroyReceipt:
    sandbox_id: str
    subject_oid: str
    destroyed_at: str


@dataclass(frozen=True, slots=True)
class ActivationTeardownReceipt:
    activation_id: str
    state: ActivationState
    profile_revoked: bool
    resources_released: bool
    process_terminated_or_absent: bool
    quarantined_reason: str | None


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _path_key(path: str | Path) -> str:
    value = os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))
    if os.name == "nt":
        value = value.casefold()
    return value.replace("\\", "/").rstrip("/")


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _normalize_generated_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewSandboxError("generated path must be non-empty")
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
        or (path.parts and path.parts[0].casefold() == ".git")
        or "\x00" in raw
    ):
        raise ReviewSandboxError(
            f"generated path must be repository-relative and safe: {value!r}"
        )
    return path.as_posix()


def _generated_contains(scope: str, path: str) -> bool:
    parent = PurePosixPath(scope)
    candidate = PurePosixPath(path)
    return candidate == parent or parent in candidate.parents


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return

    def onerror(function: Any, target: str, error: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            function(target)
        except OSError:
            raise error[1]

    shutil.rmtree(path, onerror=onerror)


class ReviewSandboxMaterializer:
    """Materialize independent Git metadata for one exact review subject."""

    def __init__(
        self,
        *,
        state_store: AxStateStore,
        path_authority: AxPathAuthority,
        repository_service: ManagedRepositoryService,
    ) -> None:
        if not isinstance(state_store, AxStateStore):
            raise TypeError("state_store must be an AxStateStore")
        if not isinstance(path_authority, AxPathAuthority):
            raise TypeError("path_authority must be an AxPathAuthority")
        if not isinstance(repository_service, ManagedRepositoryService):
            raise TypeError(
                "repository_service must be a ManagedRepositoryService"
            )
        self.state_store = state_store
        self.path_authority = path_authority
        self.repository_service = repository_service
        self.state_store.initialize()

    def create_review_sandbox(
        self,
        repo_id: str,
        subject_oid: str,
        capability: str,
        activation_id: str,
    ) -> ReviewSandboxReceipt:
        """Create one exact-OID, source-read-only v4 review sandbox."""

        repository = require_identifier(repo_id, "repo_id")
        subject = str(require_oid(subject_oid, "subject_oid"))
        capability_key = require_identifier(capability, "capability")
        activation = require_identifier(activation_id, "activation_id")
        if capability_key not in {"ta", "qa_sdet", "build_release"}:
            raise ReviewSandboxError(
                "review sandbox capability must be ta, qa_sdet, or build_release"
            )
        with self.state_store.transaction() as connection:
            graph = connection.execute(
                """
                SELECT rr.target_id, rr.canonical_path, rr.git_common_dir,
                       m.repository_path, sca.goal_id, sca.run_id, sca.slot_id,
                       sca.worker_assignment_id, lc.capability_key
                FROM repository_registrations AS rr
                JOIN managed_repositories AS m
                  ON m.id = rr.managed_repository_id
                JOIN seat_capability_activations AS sca ON sca.id = ?
                JOIN logical_capabilities AS lc
                  ON lc.id = sca.capability_id AND lc.state = 'ACTIVE'
                JOIN worker_slot_assignments AS wsa
                  ON wsa.id = sca.worker_assignment_id
                 AND wsa.state = 'ACTIVE'
                JOIN runs AS r
                  ON r.id = sca.run_id AND r.goal_id = sca.goal_id
                 AND r.target_id = rr.target_id AND r.state = 'RUNNING'
                WHERE rr.id = ? AND rr.state = 'ACTIVE'
                  AND sca.state = 'ACTIVE'
                """,
                (activation, repository),
            ).fetchone()
        if graph is None or graph["capability_key"] != capability_key:
            raise ReviewSandboxError(
                "v4 repository/run/slot/capability activation graph is not active"
            )
        lease_id = _stable_id("review-lease", repository, activation, subject)
        sandbox_root = self.path_authority.review_sandbox(activation)
        source_root = self.path_authority.review_source_root(activation)
        writable = tuple(
            str((sandbox_root / name).resolve(strict=False))
            for name in ("build", "test", "cache", "temp", "install")
        )
        protected = tuple(
            str(Path(value).resolve(strict=False))
            for value in (
                source_root,
                graph["canonical_path"],
                graph["git_common_dir"],
                graph["repository_path"],
            )
        )
        now = utc_now()
        expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat(
            timespec="microseconds"
        )
        with self.state_store.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM runtime_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO runtime_leases (
                        id, repository_id, goal_id, run_id, slot_id,
                        worker_assignment_id, lease_kind, branch_ref,
                        worktree_path, base_oid, expected_head_oid,
                        write_roots_json, protected_roots_json, state,
                        expires_at, idempotency_key, created_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'REVIEW', NULL, ?, ?, ?, ?, ?,
                              'ACTIVE', ?, ?, ?, NULL)
                    """,
                    (
                        lease_id,
                        repository,
                        graph["goal_id"],
                        graph["run_id"],
                        graph["slot_id"],
                        graph["worker_assignment_id"],
                        str(source_root),
                        subject,
                        subject,
                        compact_json(writable),
                        compact_json(protected),
                        expires,
                        f"review-runtime-lease:{lease_id}",
                        now,
                    ),
                )
            elif (
                existing["repository_id"] != repository
                or existing["run_id"] != graph["run_id"]
                or existing["slot_id"] != graph["slot_id"]
                or existing["expected_head_oid"] != subject
                or existing["state"] != "ACTIVE"
            ):
                raise ReviewSandboxError("review runtime lease identity conflicts")
        try:
            receipt = self.materialize(
                activation_id=activation,
                target_id=graph["target_id"],
                subject_oid=subject,
                generated_paths=(),
                _v4_context={
                    "target_id": graph["target_id"],
                    "goal_id": graph["goal_id"],
                    "subject_oid": subject,
                    "state": "PROFILE_BOUND",
                },
            )
        except Exception:
            with self.state_store.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE runtime_leases
                    SET state = 'QUARANTINED', released_at = ?
                    WHERE id = ? AND state = 'ACTIVE'
                    """,
                    (utc_now(), lease_id),
                )
            raise
        binding_id = _stable_id("sandbox-binding", lease_id)
        authority_id = _stable_id("oid-authority", binding_id, subject)
        attestation = hashlib.sha256(
            compact_json(
                {
                    "cwd": str(receipt.source_root),
                    "source_read_only": True,
                    "writable_roots": writable,
                    "subject_oid": subject,
                }
            ).encode("utf-8")
        ).hexdigest()
        with self.state_store.transaction(immediate=True) as connection:
            if connection.execute(
                "SELECT 1 FROM sandbox_bindings WHERE id = ?", (binding_id,)
            ).fetchone() is None:
                connection.execute(
                    """
                    INSERT INTO sandbox_bindings (
                        id, lease_id, repository_id, run_id, slot_id,
                        subject_oid, cwd, source_root, source_read_only,
                        writable_roots_json, backend, attestation_digest,
                        state, idempotency_key, bound_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?,
                              'production-confinement-required', ?, 'ACTIVE',
                              ?, ?, NULL)
                    """,
                    (
                        binding_id,
                        lease_id,
                        repository,
                        graph["run_id"],
                        graph["slot_id"],
                        subject,
                        str(receipt.source_root),
                        str(receipt.source_root),
                        compact_json(writable),
                        attestation,
                        f"sandbox-binding:{binding_id}",
                        utc_now(),
                    ),
                )
            if connection.execute(
                "SELECT 1 FROM oid_authorities WHERE id = ?", (authority_id,)
            ).fetchone() is None:
                connection.execute(
                    """
                    INSERT INTO oid_authorities (
                        id, repository_id, goal_id, run_id, lease_id,
                        sandbox_binding_id, authority_kind, oid,
                        evidence_digest, state, idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'SUBJECT', ?, ?, 'ACTIVE', ?, ?)
                    """,
                    (
                        authority_id,
                        repository,
                        graph["goal_id"],
                        graph["run_id"],
                        lease_id,
                        binding_id,
                        subject,
                        receipt.metadata_digest,
                        f"oid-authority:{authority_id}",
                        utc_now(),
                    ),
                )
        return receipt

    def materialize(
        self,
        *,
        activation_id: str,
        target_id: str,
        subject_oid: str,
        generated_paths: Sequence[str],
        _v4_context: Mapping[str, Any] | None = None,
    ) -> ReviewSandboxReceipt:
        activation = require_identifier(activation_id, "activation_id")
        target = require_identifier(target_id, "target_id")
        subject = str(require_oid(subject_oid, "subject_oid"))
        generated = tuple(
            _normalize_generated_path(path) for path in generated_paths
        )
        if len(generated) != len(set(generated)):
            raise ReviewSandboxError("generated_paths contains duplicates")
        activation_row = (
            dict(_v4_context)
            if _v4_context is not None
            else self._activation_row(activation)
        )
        if activation_row["target_id"] != target:
            raise ReviewSandboxError("activation target does not match sandbox target")
        if activation_row["subject_oid"] != subject:
            raise ReviewSandboxError(
                "activation subject OID does not match sandbox subject"
            )
        if _v4_context is None and activation_row["state"] != "PROFILE_BOUND":
            raise ReviewSandboxError(
                "review sandbox can bind only a PROFILE_BOUND activation"
            )
        goal_id = activation_row["goal_id"]
        sandbox_id = _stable_id("sandbox", activation, target, subject)
        sandbox_root = self.path_authority.review_sandbox(activation)
        source_root = self.path_authority.review_source_root(activation)
        key = (
            f"review-sandbox:{activation}:"
            f"{hashlib.sha256(compact_json(generated).encode('utf-8')).hexdigest()}"
        )
        payload = {
            "sandbox_id": sandbox_id,
            "activation_id": activation,
            "target_id": target,
            "goal_id": goal_id,
            "subject_oid": subject,
            "sandbox_root": str(sandbox_root),
            "source_root": str(source_root),
            "generated_paths": list(generated),
        }
        intent = self.state_store.begin_intent(
            operation="materialize-review-sandbox",
            idempotency_key=key,
            expected_state="SANDBOX_ABSENT",
            expected_oid=subject,
            payload=payload,
        )
        if intent.status.value == "COMPLETED":
            receipt = self._receipt_from_intent(intent)
            if not receipt.sandbox_root.is_dir():
                raise ReviewSandboxError(
                    "completed sandbox receipt points to a missing sandbox; "
                    "create a new activation for a clean rerun"
                )
            return receipt
        if sandbox_root.exists():
            raise ReviewSandboxError(
                f"sandbox path already exists without a receipt: {sandbox_root}"
            )
        registration = self.repository_service._load_target(target)
        managed = Path(registration.managed_repository_path).resolve()
        self.repository_service.resolve_commit(target, subject)
        sandbox_root.mkdir(parents=True)
        try:
            self.repository_service.command_runner.run(
                [
                    "clone",
                    "--no-local",
                    "--no-hardlinks",
                    "--no-checkout",
                    "--no-tags",
                    str(managed),
                    str(source_root),
                ],
                cwd=sandbox_root,
            )
            self.repository_service.command_runner.run(
                [
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--no-recurse-submodules",
                    "origin",
                    subject,
                ],
                cwd=source_root,
            )
            self.repository_service.command_runner.run(
                ["checkout", "--detach", "--force", subject],
                cwd=source_root,
            )
            self.repository_service.command_runner.run(
                ["remote", "remove", "origin"],
                cwd=source_root,
            )
            git_dir = source_root / ".git"
            if not git_dir.is_dir() or git_dir.is_symlink():
                raise ReviewSandboxError(
                    "review source must own an independent .git directory"
                )
            disabled_hooks = git_dir / "ax-disabled-hooks"
            disabled_hooks.mkdir()
            self.repository_service.command_runner.run(
                ["config", "--local", "core.hooksPath", str(disabled_hooks)],
                cwd=source_root,
            )
            self.repository_service.command_runner.run(
                ["config", "--local", "--replace-all", "credential.helper", ""],
                cwd=source_root,
            )
            self.repository_service.command_runner.run(
                ["config", "--local", "protocol.file.allow", "never"],
                cwd=source_root,
            )
            scratch = sandbox_root / "temp"
            build = sandbox_root / "build"
            cache = sandbox_root / "cache"
            logs = sandbox_root / "logs"
            test_db = sandbox_root / "test"
            install = sandbox_root / "install"
            outputs = sandbox_root / "outputs"
            for path in (scratch, build, cache, logs, test_db, install, outputs):
                path.mkdir()
            for relative in generated:
                generated_root = source_root.joinpath(
                    *PurePosixPath(relative).parts
                )
                resolved = generated_root.resolve(strict=False)
                if not _within(source_root, resolved):
                    raise ReviewSandboxError(
                        f"generated path escapes review source: {relative}"
                    )
                generated_root.mkdir(parents=True, exist_ok=True)
            head = self._git_oid(source_root, ["rev-parse", "--verify", "HEAD"])
            if head != subject:
                raise ReviewSandboxError(
                    f"review clone materialized {head}, expected {subject}"
                )
            tree = self._git_oid(
                source_root, ["rev-parse", "--verify", "HEAD^{tree}"]
            )
            metadata_digest = self._metadata_digest(source_root, managed)
            receipt = ReviewSandboxReceipt(
                sandbox_id=sandbox_id,
                activation_id=activation,
                target_id=target,
                goal_id=goal_id,
                subject_oid=subject,
                subject_tree_oid=tree,
                sandbox_root=sandbox_root.resolve(),
                source_root=source_root.resolve(),
                git_dir=git_dir.resolve(),
                scratch_root=scratch.resolve(),
                build_root=build.resolve(),
                cache_root=cache.resolve(),
                log_root=logs.resolve(),
                test_database_root=test_db.resolve(),
                install_root=install.resolve(),
                output_root=outputs.resolve(),
                generated_paths=generated,
                prohibited_authority_roots=(
                    managed,
                    Path(registration.canonical_worktree_path).resolve(),
                    Path(registration.git_common_dir).resolve(),
                ),
                metadata_digest=metadata_digest,
                idempotency_key=key,
            )
            self._persist_workspace(receipt)
            completed = self.state_store.complete_intent(
                intent.intent_id,
                resulting_state="SANDBOX_READY",
                resulting_oid=subject,
                evidence={"receipt": self._receipt_json(receipt)},
            )
            self._record_audit(
                event_type="REVIEW_SANDBOX_MATERIALIZED",
                activation_id=activation,
                goal_id=goal_id,
                subject_oid=subject,
                subject_id=sandbox_id,
                idempotency_key=f"audit:{completed.intent_id}",
                payload={
                    "subject_tree_oid": tree,
                    "metadata_digest": metadata_digest,
                    "generated_paths": list(generated),
                },
            )
            integrity = self.verify_integrity(sandbox_id)
            if not integrity.gate_eligible:
                raise ReviewSandboxError(
                    "new review sandbox did not pass clean integrity verification"
                )
            return receipt
        except Exception:
            self._quarantine_workspace(sandbox_id)
            raise

    def verify_integrity(self, sandbox_id: str) -> SourceIntegrityResult:
        sandbox = require_identifier(sandbox_id, "sandbox_id")
        receipt = self._load_receipt(sandbox)
        reasons: list[str] = []
        observed_head: str | None = None
        observed_tree: str | None = None
        tracked: tuple[str, ...] = ()
        untracked_source: tuple[str, ...] = ()
        ignored_generated: tuple[str, ...] = ()
        invalidated = False
        registration = self.repository_service._load_target(receipt.target_id)
        managed = Path(registration.managed_repository_path).resolve()
        try:
            self._validate_receipt_paths(receipt)
            if receipt.metadata_digest != self._metadata_digest(
                receipt.source_root, managed
            ):
                reasons.append("protected Git metadata digest changed")
                invalidated = True
            authority_reasons = self._authority_link_reasons(
                receipt.source_root, receipt.git_dir, managed
            )
            if authority_reasons:
                reasons.extend(authority_reasons)
                invalidated = True
            escapes = self._path_escapes(receipt)
            if escapes:
                reasons.extend(f"path escape: {path}" for path in escapes)
                invalidated = True
            observed_head = self._git_oid(
                receipt.source_root, ["rev-parse", "--verify", "HEAD"]
            )
            observed_tree = self._git_oid(
                receipt.source_root,
                ["rev-parse", "--verify", "HEAD^{tree}"],
            )
            if observed_head != receipt.subject_oid:
                reasons.append(
                    f"HEAD is {observed_head}, expected {receipt.subject_oid}"
                )
                invalidated = True
            if observed_tree != receipt.subject_tree_oid:
                reasons.append(
                    "HEAD tree does not match the materialized subject tree"
                )
                invalidated = True
            staged = self._git_paths(
                receipt.source_root,
                ["diff", "--cached", "--name-only", "-z"],
            )
            unstaged = self._git_paths(
                receipt.source_root,
                ["diff", "--name-only", "-z"],
            )
            tracked = tuple(sorted(set((*staged, *unstaged))))
            untracked = self._git_paths(
                receipt.source_root,
                ["ls-files", "--others", "--exclude-standard", "-z"],
            )
            ignored_generated = tuple(
                path
                for path in untracked
                if any(
                    _generated_contains(scope, path)
                    for scope in receipt.generated_paths
                )
            )
            untracked_source = tuple(
                path for path in untracked if path not in ignored_generated
            )
        except Exception as exc:
            reasons.append(f"integrity inspection failed: {type(exc).__name__}: {exc}")
            invalidated = True
        if invalidated:
            classification = SourceIntegrity.INVALIDATED
        elif tracked or untracked_source:
            classification = SourceIntegrity.ANALYSIS_DIRTY
            reasons.append("tracked or untracked source differs from subject OID")
        else:
            classification = SourceIntegrity.CLEAN
        return SourceIntegrityResult(
            sandbox_id=sandbox,
            subject_oid=receipt.subject_oid,
            classification=classification,
            observed_head_oid=observed_head,
            observed_tree_oid=observed_tree,
            tracked_changes=tracked,
            untracked_source_changes=untracked_source,
            ignored_generated_paths=ignored_generated,
            reasons=tuple(reasons),
            checked_at=utc_now(),
        )

    def destroy(self, sandbox_id: str) -> ReviewSandboxDestroyReceipt:
        sandbox = require_identifier(sandbox_id, "sandbox_id")
        receipt = self._load_receipt(sandbox)
        key = f"destroy-review-sandbox:{sandbox}"
        intent = self.state_store.begin_intent(
            operation="destroy-review-sandbox",
            idempotency_key=key,
            expected_state="SANDBOX_READY",
            expected_oid=receipt.subject_oid,
            payload={
                "sandbox_id": sandbox,
                "sandbox_root": str(receipt.sandbox_root),
            },
        )
        if intent.status.value == "COMPLETED":
            return ReviewSandboxDestroyReceipt(
                sandbox_id=sandbox,
                subject_oid=receipt.subject_oid,
                destroyed_at=str(intent.evidence["destroyed_at"]),
            )
        expected_root = self.path_authority.review_sandbox(
            receipt.activation_id
        ).resolve(strict=False)
        if _path_key(expected_root) != _path_key(receipt.sandbox_root):
            raise ReviewSandboxError("sandbox receipt path conflicts with authority")
        _remove_tree(receipt.sandbox_root)
        if receipt.sandbox_root.exists():
            raise ReviewSandboxError("sandbox root remains after destruction")
        destroyed_at = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE workspaces SET state = 'RELEASED', updated_at = ?
                WHERE id = ? AND kind = 'REVIEW'
                  AND state IN ('ACTIVE', 'QUARANTINED')
                """,
                (destroyed_at, sandbox),
            )
            binding = connection.execute(
                "SELECT id, lease_id FROM sandbox_bindings WHERE cwd = ?",
                (str(receipt.source_root),),
            ).fetchone()
            if binding is not None:
                connection.execute(
                    """
                    UPDATE oid_authorities SET state = 'SUPERSEDED'
                    WHERE sandbox_binding_id = ? AND state = 'ACTIVE'
                    """,
                    (binding["id"],),
                )
                connection.execute(
                    """
                    UPDATE sandbox_bindings
                    SET state = 'RELEASED', released_at = ?
                    WHERE id = ? AND state = 'ACTIVE'
                    """,
                    (destroyed_at, binding["id"]),
                )
                connection.execute(
                    """
                    UPDATE runtime_leases
                    SET state = 'RELEASED', released_at = ?
                    WHERE id = ? AND state = 'ACTIVE'
                    """,
                    (destroyed_at, binding["lease_id"]),
                )
        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="SANDBOX_DESTROYED",
            resulting_oid=receipt.subject_oid,
            evidence={"destroyed_at": destroyed_at},
        )
        return ReviewSandboxDestroyReceipt(
            sandbox_id=sandbox,
            subject_oid=receipt.subject_oid,
            destroyed_at=str(completed.evidence["destroyed_at"]),
        )

    def runner_contract(self, sandbox_id: str) -> ReviewRunnerContract:
        receipt = self._load_receipt(require_identifier(sandbox_id, "sandbox_id"))
        return receipt.runner_contract

    def _activation_row(self, activation_id: str) -> Any:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM activations WHERE id = ?",
                (activation_id,),
            ).fetchone()
        if row is None:
            raise ReviewSandboxError(
                f"activation does not exist: {activation_id}"
            )
        return row

    def _persist_workspace(self, receipt: ReviewSandboxReceipt) -> None:
        now = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?",
                (receipt.sandbox_id,),
            ).fetchone()
            signature = (
                receipt.target_id,
                receipt.goal_id,
                "REVIEW",
                _path_key(receipt.sandbox_root),
                receipt.subject_oid,
                "ACTIVE",
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO workspaces (
                        id, target_id, goal_id, kind, path, branch_ref,
                        subject_oid, state, created_at, updated_at
                    ) VALUES (?, ?, ?, 'REVIEW', ?, NULL, ?, 'ACTIVE', ?, ?)
                    """,
                    (
                        receipt.sandbox_id,
                        receipt.target_id,
                        receipt.goal_id,
                        str(receipt.sandbox_root),
                        receipt.subject_oid,
                        now,
                        now,
                    ),
                )
            else:
                actual = (
                    existing["target_id"],
                    existing["goal_id"],
                    existing["kind"],
                    _path_key(existing["path"]),
                    existing["subject_oid"],
                    existing["state"],
                )
                if actual != signature:
                    raise ReviewSandboxError(
                        "sandbox workspace identity conflicts with persisted state"
                    )

    def _quarantine_workspace(self, sandbox_id: str) -> None:
        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE workspaces SET state = 'QUARANTINED', updated_at = ?
                WHERE id = ? AND state IN ('PROVISIONING', 'ACTIVE')
                """,
                (utc_now(), sandbox_id),
            )

    def _load_receipt(self, sandbox_id: str) -> ReviewSandboxReceipt:
        with self.state_store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT evidence_json
                FROM operation_intents
                WHERE operation = 'materialize-review-sandbox'
                  AND status = 'COMPLETED'
                """
            ).fetchall()
        for row in rows:
            raw = json.loads(row["evidence_json"]).get("receipt")
            if isinstance(raw, Mapping) and raw.get("sandbox_id") == sandbox_id:
                return self._receipt_from_json(raw)
        raise ReviewSandboxNotFoundError(sandbox_id)

    def _receipt_from_intent(self, intent: Any) -> ReviewSandboxReceipt:
        raw = intent.evidence.get("receipt")
        if not isinstance(raw, Mapping):
            raise ReviewSandboxError("completed sandbox intent lacks a receipt")
        return self._receipt_from_json(thaw_json(raw))

    @staticmethod
    def _receipt_json(receipt: ReviewSandboxReceipt) -> dict[str, Any]:
        result = asdict(receipt)
        for key, value in tuple(result.items()):
            if isinstance(value, Path):
                result[key] = str(value)
            elif key == "prohibited_authority_roots":
                result[key] = [str(path) for path in value]
        return result

    @staticmethod
    def _receipt_from_json(raw: Mapping[str, Any]) -> ReviewSandboxReceipt:
        data = dict(raw)
        for field in (
            "sandbox_root",
            "source_root",
            "git_dir",
            "scratch_root",
            "build_root",
            "cache_root",
            "log_root",
            "test_database_root",
            "install_root",
            "output_root",
        ):
            data[field] = Path(data[field]).resolve(strict=False)
        data["generated_paths"] = tuple(data["generated_paths"])
        data["prohibited_authority_roots"] = tuple(
            Path(path).resolve(strict=False)
            for path in data["prohibited_authority_roots"]
        )
        return ReviewSandboxReceipt(**data)

    def _validate_receipt_paths(self, receipt: ReviewSandboxReceipt) -> None:
        expected = self.path_authority.review_sandbox(
            receipt.activation_id
        ).resolve(strict=False)
        if _path_key(expected) != _path_key(receipt.sandbox_root):
            raise ReviewSandboxError("sandbox root differs from path authority")
        if not receipt.sandbox_root.is_dir() or receipt.sandbox_root.is_symlink():
            raise ReviewSandboxError("sandbox root is missing or linked")
        for path in (
            receipt.source_root,
            receipt.git_dir,
            receipt.scratch_root,
            receipt.build_root,
            receipt.cache_root,
            receipt.log_root,
            receipt.test_database_root,
            receipt.output_root,
        ):
            if not path.exists() or not _within(receipt.sandbox_root, path.resolve()):
                raise ReviewSandboxError(
                    f"sandbox receipt path is missing or escaped: {path}"
                )
        if not receipt.git_dir.is_dir() or receipt.git_dir.is_symlink():
            raise ReviewSandboxError("review Git metadata is not independent")

    def _metadata_digest(self, source_root: Path, managed: Path) -> str:
        git_dir = source_root / ".git"
        if not git_dir.is_dir() or git_dir.is_symlink():
            raise ReviewSandboxError("independent .git directory is missing")
        if (git_dir / "commondir").exists():
            raise ReviewSandboxError("review Git metadata reintroduced commondir")
        alternates = git_dir / "objects" / "info" / "alternates"
        if alternates.exists():
            raise ReviewSandboxError("review Git metadata contains alternates")
        config = git_dir / "config"
        head = git_dir / "HEAD"
        if not config.is_file() or not head.is_file():
            raise ReviewSandboxError("review Git config or HEAD is missing")
        config_bytes = config.read_bytes()
        normalized_config = config_bytes.decode(
            "utf-8", errors="replace"
        ).casefold().replace("\\", "/")
        managed_key = _path_key(managed).casefold()
        if managed_key and managed_key in normalized_config:
            raise ReviewSandboxError(
                "review Git config contains managed authority path"
            )
        remotes = self.repository_service.command_runner.run(
            ["remote"], cwd=source_root
        ).stdout.strip()
        if remotes:
            raise ReviewSandboxError("review Git metadata contains a remote")
        hooks = self.repository_service.command_runner.run(
            ["config", "--local", "--get", "core.hooksPath"],
            cwd=source_root,
        ).stdout.strip()
        hooks_path = Path(hooks).expanduser().resolve(strict=False)
        expected_hooks = (git_dir / "ax-disabled-hooks").resolve(strict=False)
        if _path_key(hooks_path) != _path_key(expected_hooks):
            raise ReviewSandboxError("review hooks path is not disabled locally")
        if not expected_hooks.is_dir() or any(expected_hooks.iterdir()):
            raise ReviewSandboxError("disabled review hooks directory was modified")
        payload = b"\x00".join(
            (
                head.read_bytes(),
                config_bytes,
                str(git_dir.resolve()).encode("utf-8"),
                str(expected_hooks).encode("utf-8"),
            )
        )
        return hashlib.sha256(payload).hexdigest()

    def _authority_link_reasons(
        self,
        source_root: Path,
        git_dir: Path,
        managed: Path,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        git_dir_result = self.repository_service.command_runner.run(
            ["rev-parse", "--path-format=absolute", "--git-dir"],
            cwd=source_root,
        ).stdout.strip()
        common_result = self.repository_service.command_runner.run(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=source_root,
        ).stdout.strip()
        if _path_key(git_dir_result) != _path_key(git_dir):
            reasons.append("Git directory is not sandbox-local")
        if _path_key(common_result) != _path_key(git_dir):
            reasons.append("Git common directory is not sandbox-local")
        if (git_dir / "objects" / "info" / "alternates").exists():
            reasons.append("Git alternates reintroduced authority linkage")
        if (git_dir / "commondir").exists():
            reasons.append("Git commondir reintroduced authority linkage")
        remotes = self.repository_service.command_runner.run(
            ["remote"], cwd=source_root
        ).stdout.strip()
        if remotes:
            reasons.append("writable Git remote was reintroduced")
        config = (git_dir / "config").read_text(
            encoding="utf-8", errors="replace"
        )
        if _path_key(managed).casefold() in config.casefold().replace("\\", "/"):
            reasons.append("managed authority path appears in Git config")
        return tuple(reasons)

    @staticmethod
    def _path_escapes(receipt: ReviewSandboxReceipt) -> tuple[str, ...]:
        escapes: list[str] = []
        root = receipt.sandbox_root.resolve()
        for current, directories, files in os.walk(
            receipt.source_root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            if current_path == receipt.git_dir:
                directories[:] = []
                continue
            directories[:] = [
                name for name in directories if current_path / name != receipt.git_dir
            ]
            for name in (*directories, *files):
                candidate = current_path / name
                is_junction = bool(
                    getattr(os.path, "isjunction", lambda _: False)(candidate)
                )
                if candidate.is_symlink() or is_junction:
                    resolved = candidate.resolve(strict=False)
                    if not _within(root, resolved):
                        escapes.append(
                            candidate.relative_to(receipt.sandbox_root).as_posix()
                        )
        return tuple(sorted(set(escapes)))

    def _git_oid(self, cwd: Path, arguments: Sequence[str]) -> str:
        result = self.repository_service.command_runner.run(arguments, cwd=cwd)
        return str(require_oid(result.stdout.strip(), "Git OID"))

    def _git_paths(self, cwd: Path, arguments: Sequence[str]) -> tuple[str, ...]:
        result = self.repository_service.command_runner.run(arguments, cwd=cwd)
        paths = []
        for raw in result.stdout.split("\x00"):
            if not raw:
                continue
            paths.append(_normalize_generated_path(raw))
        return tuple(sorted(set(paths)))

    def _record_audit(
        self,
        *,
        event_type: str,
        activation_id: str,
        goal_id: str,
        subject_oid: str,
        subject_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=_stable_id("audit", idempotency_key),
                event_type=event_type,
                actor="service:review-sandbox",
                subject_type="review-sandbox",
                subject_id=subject_id,
                payload=payload,
                occurred_at=utc_now(),
                idempotency_key=idempotency_key,
                goal_id=goal_id,
                activation_id=activation_id,
                subject_oid=subject_oid,
            )
        )


class ActivationManager:
    """Persist the exact disposable activation state machine."""

    def __init__(
        self,
        *,
        state_store: AxStateStore,
        path_authority: AxPathAuthority,
        process_controller: ProcessController | None = None,
        resource_controller: ActivationResourceController | None = None,
        profile_controller: ProfileAccessController | None = None,
    ) -> None:
        if not isinstance(state_store, AxStateStore):
            raise TypeError("state_store must be an AxStateStore")
        if not isinstance(path_authority, AxPathAuthority):
            raise TypeError("path_authority must be an AxPathAuthority")
        self.state_store = state_store
        self.path_authority = path_authority
        self.process_controller = process_controller or NoProcessController()
        self.resource_controller = (
            resource_controller or NoExternalResourceController()
        )
        self.profile_controller = profile_controller or FilesystemProfileAccessController(
            path_authority
        )
        self.state_store.initialize()

    def create(
        self,
        spec: ActivationSpec,
        *,
        target_id: str,
        goal_id: str,
        run_id: str,
        role: str,
        gate_or_task: str,
        idempotency_key: str,
    ) -> ActivationRecord:
        if not isinstance(spec, ActivationSpec):
            raise TypeError("spec must be an ActivationSpec")
        if spec.professional_skill_id != PROFESSIONAL_SKILL_ID:
            raise ActivationStateError(
                f"activation must bind exactly {PROFESSIONAL_SKILL_ID}"
            )
        target = require_identifier(target_id, "target_id")
        goal = require_identifier(goal_id, "goal_id")
        run = require_identifier(run_id, "run_id")
        activation_role = require_identifier(role, "role")
        task = require_identifier(gate_or_task, "gate_or_task")
        key = require_nonempty(idempotency_key, "idempotency_key")
        self._validate_run_graph(target, goal, run, spec.subject_oid)
        payload = {
            "activation_id": spec.activation_id,
            "target_id": target,
            "goal_id": goal,
            "run_id": run,
            "role": activation_role,
            "gate_or_task": task,
            "spec": asdict(spec),
        }
        intent = self.state_store.begin_intent(
            operation="create-activation",
            idempotency_key=key,
            expected_state="ABSENT",
            expected_oid=spec.subject_oid,
            payload=payload,
        )
        if intent.status.value == "COMPLETED":
            return self._load_record(spec.activation_id)
        now = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM activations WHERE id = ? OR idempotency_key = ?",
                (spec.activation_id, key),
            ).fetchone()
            signature = (
                spec.activation_id,
                target,
                goal,
                run,
                spec.subject_oid,
                activation_role,
                task,
                key,
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO activations (
                        id, target_id, goal_id, run_id, subject_oid,
                        role, gate_or_task, state, idempotency_key,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?)
                    """,
                    (*signature, now, now),
                )
            else:
                actual = (
                    existing["id"],
                    existing["target_id"],
                    existing["goal_id"],
                    existing["run_id"],
                    existing["subject_oid"],
                    existing["role"],
                    existing["gate_or_task"],
                    existing["idempotency_key"],
                )
                if actual != signature:
                    raise ActivationStateError(
                        "activation ID or idempotency key was reused"
                    )
        self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="CREATED",
            resulting_oid=spec.subject_oid,
            evidence={"activation_id": spec.activation_id},
        )
        return self._load_record(spec.activation_id)

    def bind_profile(
        self,
        activation_id: str,
        compiled_digest: str,
    ) -> ActivationRecord:
        activation = require_identifier(activation_id, "activation_id")
        digest = require_nonempty(compiled_digest, "compiled_digest")
        spec = self._creation_spec(activation)
        if spec.compiled_profile_digest != digest:
            raise ActivationStateError(
                "compiled digest differs from immutable activation spec"
            )
        with self.state_store.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT a.state AS activation_state, a.goal_id,
                       p.professional_skill_id, p.compiled_profile_ref,
                       p.compiled_profile_digest, p.state AS profile_state
                FROM activations AS a
                LEFT JOIN profile_bindings AS p ON p.activation_id = a.id
                WHERE a.id = ?
                """,
                (activation,),
            ).fetchone()
            if row is None:
                raise ActivationStateError(f"activation does not exist: {activation}")
            if row["activation_state"] in {"QUARANTINED", "REVOKE_FAILED"}:
                raise ActivationQuarantinedError(activation)
            if row["profile_state"] != "BOUND":
                raise ActivationStateError(
                    "Phase 3 compiler must persist the BOUND profile first"
                )
            if (
                row["professional_skill_id"] != PROFESSIONAL_SKILL_ID
                or row["compiled_profile_digest"] != digest
                or row["compiled_profile_ref"] != spec.compiled_profile_ref
            ):
                raise ActivationStateError(
                    "persisted professional profile conflicts with activation spec"
                )
            profile_path = Path(row["compiled_profile_ref"]).resolve(strict=False)
            expected_root = self.path_authority.activation_root(
                row["goal_id"], activation
            ).resolve(strict=False)
            if (
                not _within(expected_root, profile_path)
                or not profile_path.is_file()
                or profile_path.is_symlink()
                or hashlib.sha256(profile_path.read_bytes()).hexdigest() != digest
            ):
                raise ActivationStateError(
                    "compiled profile artifact is missing, escaped, or changed"
                )
            if row["activation_state"] == "CREATED":
                connection.execute(
                    """
                    UPDATE activations SET state = 'PROFILE_BOUND', updated_at = ?
                    WHERE id = ? AND state = 'CREATED'
                    """,
                    (utc_now(), activation),
                )
            elif row["activation_state"] != "PROFILE_BOUND":
                raise ActivationStateError(
                    f"profile cannot bind from {row['activation_state']}"
                )
        return self._load_record(activation)

    def bind_workspace(
        self,
        activation_id: str,
        workspace_or_sandbox_id: str,
    ) -> ActivationRecord:
        activation = require_identifier(activation_id, "activation_id")
        workspace = require_identifier(
            workspace_or_sandbox_id, "workspace_or_sandbox_id"
        )
        spec = self._creation_spec(activation)
        if spec.workspace_or_sandbox_id != workspace:
            raise ActivationStateError(
                "workspace differs from immutable activation spec"
            )
        with self.state_store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM activations WHERE id = ?",
                (activation,),
            ).fetchone()
            resource = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?",
                (workspace,),
            ).fetchone()
            if row is None or resource is None:
                raise ActivationStateError("activation workspace is missing")
            if row["state"] == "WORKSPACE_BOUND" and row["workspace_id"] == workspace:
                return self._load_record(activation)
            if row["state"] != "PROFILE_BOUND":
                raise ActivationStateError(
                    f"workspace cannot bind from {row['state']}"
                )
            if resource["state"] != "ACTIVE":
                raise ActivationStateError(
                    f"workspace is not active: {resource['state']}"
                )
            if (
                resource["target_id"] != row["target_id"]
                or resource["goal_id"] != row["goal_id"]
                or resource["subject_oid"] != row["subject_oid"]
            ):
                raise ActivationStateError(
                    "workspace target, goal, or exact subject OID conflicts"
                )
            sandbox_path = resource["path"] if resource["kind"] == "REVIEW" else None
            connection.execute(
                """
                UPDATE activations
                SET workspace_id = ?, sandbox_path = ?,
                    state = 'WORKSPACE_BOUND', updated_at = ?
                WHERE id = ? AND state = 'PROFILE_BOUND'
                """,
                (workspace, sandbox_path, utc_now(), activation),
            )
        return self._load_record(activation)

    def mark_running(
        self,
        activation_id: str,
        process_id: int | None,
    ) -> ActivationRecord:
        activation = require_identifier(activation_id, "activation_id")
        record = self._load_record(activation)
        if record.state in {
            ActivationState.REVOKE_FAILED,
            ActivationState.QUARANTINED,
            ActivationState.RECOVERY_CLEANED,
            ActivationState.TERMINATED,
        }:
            raise ActivationQuarantinedError(
                f"activation cannot be reused from {record.state.value}"
            )
        key = f"activation-running:{activation}"
        intent = self.state_store.begin_intent(
            operation="mark-activation-running",
            idempotency_key=key,
            expected_state="WORKSPACE_BOUND",
            expected_oid=record.subject_oid,
            payload={"activation_id": activation, "process_id": process_id},
        )
        if intent.status.value == "COMPLETED":
            return self._load_record(activation)
        if record.state is not ActivationState.WORKSPACE_BOUND:
            raise ActivationStateError(
                f"activation cannot run from {record.state.value}"
            )
        identity = (
            self.process_controller.capture(process_id)
            if process_id is not None
            else None
        )
        with self.state_store.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE activations SET state = 'RUNNING', process_id = ?,
                       updated_at = ?
                WHERE id = ? AND state = 'WORKSPACE_BOUND'
                """,
                (process_id, utc_now(), activation),
            ).rowcount
            if updated != 1:
                raise ActivationStateError("activation running transition raced")
        self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="RUNNING",
            resulting_oid=record.subject_oid,
            evidence={
                "process_identity": asdict(identity) if identity is not None else None
            },
        )
        return self._load_record(activation)

    def persist_result(
        self,
        activation_id: str,
        result: Mapping[str, Any],
    ) -> ActivationRecord:
        activation = require_identifier(activation_id, "activation_id")
        if not isinstance(result, Mapping):
            raise ActivationStateError("result must be a mapping")
        result_json = compact_json(result)
        with self.state_store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT state, result_json FROM activations WHERE id = ?",
                (activation,),
            ).fetchone()
            if row is None:
                raise ActivationStateError(f"activation does not exist: {activation}")
            if row["state"] == "RESULT_PERSISTED":
                if row["result_json"] != result_json:
                    raise ActivationStateError(
                        "activation result replay differs from persisted result"
                    )
                return self._load_record(activation)
            if row["state"] != "RUNNING":
                raise ActivationStateError(
                    f"result cannot persist from {row['state']}"
                )
            connection.execute(
                """
                UPDATE activations SET result_json = ?,
                       state = 'RESULT_PERSISTED', updated_at = ?
                WHERE id = ? AND state = 'RUNNING'
                """,
                (result_json, utc_now(), activation),
            )
        return self._load_record(activation)

    def revoke_and_terminate(self, activation_id: str) -> ActivationRecord:
        activation = require_identifier(activation_id, "activation_id")
        record = self._load_record(activation)
        if record.state is ActivationState.TERMINATED:
            return record
        if record.state in {
            ActivationState.QUARANTINED,
            ActivationState.REVOKE_FAILED,
        }:
            return self._load_record(activation)
        if record.state not in {
            ActivationState.RESULT_PERSISTED,
            ActivationState.PROFILE_REVOKED,
            ActivationState.RESOURCES_RELEASED,
        }:
            raise ActivationStateError(
                "result must be persisted before profile revocation"
            )
        if record.state is ActivationState.RESULT_PERSISTED:
            try:
                binding = self._profile_binding(activation)
                self.profile_controller.revoke(
                    activation_id=activation,
                    goal_id=record.goal_id,
                    compiled_profile_ref=Path(
                        binding["compiled_profile_ref"]
                    ).resolve(strict=False),
                    compiled_profile_digest=binding["compiled_profile_digest"],
                )
                with self.state_store.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        UPDATE profile_bindings
                        SET state = 'REVOKED', revoked_at = ?
                        WHERE activation_id = ? AND state = 'BOUND'
                        """,
                        (utc_now(), activation),
                    )
                    updated = connection.execute(
                        """
                        UPDATE activations SET state = 'PROFILE_REVOKED',
                               updated_at = ?
                        WHERE id = ? AND state = 'RESULT_PERSISTED'
                        """,
                        (utc_now(), activation),
                    ).rowcount
                    if updated != 1:
                        raise ActivationStateError(
                            "profile revocation transition raced"
                        )
            except Exception as exc:
                return self._quarantine_failure(
                    activation,
                    f"profile revocation failed: {type(exc).__name__}: {exc}",
                    profile_failure=True,
                )
            record = self._load_record(activation)
        if record.state is ActivationState.PROFILE_REVOKED:
            try:
                identity = self._process_identity(activation)
                if identity is not None:
                    identity_state = self.process_controller.inspect(identity)
                    if identity_state is ProcessIdentityState.RUNNING_MISMATCH:
                        raise ActivationIdentityError(
                            "PID belongs to a different process; termination refused"
                        )
                    if identity_state is ProcessIdentityState.RUNNING_MATCH:
                        self.process_controller.terminate(identity)
                        if (
                            self.process_controller.inspect(identity)
                            is not ProcessIdentityState.EXITED
                        ):
                            raise ActivationIdentityError(
                                "bound process remains after termination"
                            )
                self.resource_controller.release(record)
                with self.state_store.transaction(immediate=True) as connection:
                    updated = connection.execute(
                        """
                        UPDATE activations SET state = 'RESOURCES_RELEASED',
                               updated_at = ?
                        WHERE id = ? AND state = 'PROFILE_REVOKED'
                        """,
                        (utc_now(), activation),
                    ).rowcount
                    if updated != 1:
                        raise ActivationStateError(
                            "resource release transition raced"
                        )
            except Exception as exc:
                return self._quarantine_failure(
                    activation,
                    f"resource cleanup failed: {type(exc).__name__}: {exc}",
                    profile_failure=False,
                )
            record = self._load_record(activation)
        if record.state is ActivationState.RESOURCES_RELEASED:
            terminated_at = utc_now()
            with self.state_store.transaction(immediate=True) as connection:
                updated = connection.execute(
                    """
                    UPDATE activations
                    SET state = 'TERMINATED', updated_at = ?, terminated_at = ?
                    WHERE id = ? AND state = 'RESOURCES_RELEASED'
                    """,
                    (terminated_at, terminated_at, activation),
                ).rowcount
                if updated != 1:
                    raise ActivationStateError("termination transition raced")
        return self._load_record(activation)

    def quarantine(
        self,
        activation_id: str,
        reason: str,
    ) -> ActivationRecord:
        activation = require_identifier(activation_id, "activation_id")
        why = require_nonempty(reason, "reason")
        record = self._load_record(activation)
        if record.state is ActivationState.TERMINATED:
            raise ActivationStateError("terminated activation cannot be quarantined")
        try:
            self.resource_controller.quarantine(record, why)
        finally:
            with self.state_store.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE activations SET state = 'QUARANTINED', updated_at = ?
                    WHERE id = ? AND state <> 'TERMINATED'
                    """,
                    (utc_now(), activation),
                )
        self._record_activation_audit(
            activation,
            "ACTIVATION_QUARANTINED",
            {"reason": why},
            f"activation-quarantined:{activation}:{hashlib.sha256(why.encode()).hexdigest()}",
        )
        return self._load_record(activation)

    def recovery_cleaned(self, activation_id: str) -> ActivationRecord:
        activation = require_identifier(activation_id, "activation_id")
        record = self._load_record(activation)
        if record.state is not ActivationState.QUARANTINED:
            raise ActivationStateError(
                "only a quarantined activation can be recovery-cleaned"
            )
        self.resource_controller.recovery_cleanup(record)
        with self.state_store.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE activations SET state = 'RECOVERY_CLEANED', updated_at = ?
                WHERE id = ? AND state = 'QUARANTINED'
                """,
                (utc_now(), activation),
            ).rowcount
            if updated != 1:
                raise ActivationStateError("recovery cleanup transition raced")
        return self._load_record(activation)

    def teardown_receipt(self, activation_id: str) -> ActivationTeardownReceipt:
        record = self._load_record(require_identifier(activation_id, "activation_id"))
        reason = None
        if record.state is ActivationState.QUARANTINED:
            with self.state_store.transaction() as connection:
                row = connection.execute(
                    """
                    SELECT payload_json FROM audit_events
                    WHERE activation_id = ?
                      AND event_type = 'ACTIVATION_QUARANTINED'
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (record.activation_id,),
                ).fetchone()
            if row is not None:
                reason = json.loads(row["payload_json"]).get("reason")
        return ActivationTeardownReceipt(
            activation_id=record.activation_id,
            state=record.state,
            profile_revoked=record.state
            in {
                ActivationState.PROFILE_REVOKED,
                ActivationState.RESOURCES_RELEASED,
                ActivationState.TERMINATED,
                ActivationState.RECOVERY_CLEANED,
            },
            resources_released=record.state
            in {
                ActivationState.RESOURCES_RELEASED,
                ActivationState.TERMINATED,
                ActivationState.RECOVERY_CLEANED,
            },
            process_terminated_or_absent=record.state
            in {
                ActivationState.RESOURCES_RELEASED,
                ActivationState.TERMINATED,
                ActivationState.RECOVERY_CLEANED,
            },
            quarantined_reason=reason,
        )

    def _validate_run_graph(
        self,
        target_id: str,
        goal_id: str,
        run_id: str,
        subject_oid: str,
    ) -> None:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT r.state AS run_state, r.base_oid, g.state AS goal_state
                FROM runs AS r
                JOIN goals AS g ON g.id = r.goal_id
                WHERE r.id = ? AND r.goal_id = ? AND r.target_id = ?
                """,
                (run_id, goal_id, target_id),
            ).fetchone()
        if row is None:
            raise ActivationStateError("activation run/goal/target graph is missing")
        if row["run_state"] != "RUNNING" or row["goal_state"] != "ACTIVE":
            raise ActivationStateError("activation requires active goal and run")
        require_oid(subject_oid, "subject_oid")

    def _creation_spec(self, activation_id: str) -> ActivationSpec:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT i.payload_json
                FROM activations AS a
                JOIN operation_intents AS i ON i.idempotency_key = a.idempotency_key
                WHERE a.id = ? AND i.operation = 'create-activation'
                  AND i.status = 'COMPLETED'
                """,
                (activation_id,),
            ).fetchone()
        if row is None:
            raise ActivationStateError("activation has no immutable creation receipt")
        raw = json.loads(row["payload_json"])["spec"]
        raw["allowed_tools"] = tuple(raw["allowed_tools"])
        raw["commands"] = tuple(raw["commands"])
        return ActivationSpec(**raw)

    def _load_record(self, activation_id: str) -> ActivationRecord:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT a.*, p.compiled_profile_digest
                FROM activations AS a
                LEFT JOIN profile_bindings AS p ON p.activation_id = a.id
                WHERE a.id = ?
                """,
                (activation_id,),
            ).fetchone()
        if row is None:
            raise ActivationStateError(f"activation does not exist: {activation_id}")
        workspace_id = row["workspace_id"]
        if workspace_id is None:
            workspace_id = self._creation_spec(activation_id).workspace_or_sandbox_id
        return ActivationRecord(
            activation_id=row["id"],
            target_id=row["target_id"],
            goal_id=row["goal_id"],
            run_id=row["run_id"],
            workspace_or_sandbox_id=workspace_id,
            subject_oid=row["subject_oid"],
            role=row["role"],
            gate_or_task=row["gate_or_task"],
            state=ActivationState(row["state"]),
            idempotency_key=row["idempotency_key"],
            compiled_profile_digest=row["compiled_profile_digest"],
            process_id=row["process_id"],
        )

    def _profile_binding(self, activation_id: str) -> Any:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM profile_bindings WHERE activation_id = ?",
                (activation_id,),
            ).fetchone()
        if row is None or row["state"] not in {"BOUND", "REVOKED"}:
            raise ActivationStateError("activation profile binding is missing")
        return row

    def _process_identity(self, activation_id: str) -> ProcessIdentity | None:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT evidence_json FROM operation_intents
                WHERE operation = 'mark-activation-running'
                  AND idempotency_key = ?
                  AND status = 'COMPLETED'
                """,
                (f"activation-running:{activation_id}",),
            ).fetchone()
        if row is None:
            raise ActivationIdentityError("running activation lacks process receipt")
        raw = json.loads(row["evidence_json"]).get("process_identity")
        return ProcessIdentity(**raw) if raw is not None else None

    def _quarantine_failure(
        self,
        activation_id: str,
        reason: str,
        *,
        profile_failure: bool,
    ) -> ActivationRecord:
        with self.state_store.transaction(immediate=True) as connection:
            if profile_failure:
                connection.execute(
                    """
                    UPDATE profile_bindings
                    SET state = 'REVOKE_FAILED'
                    WHERE activation_id = ? AND state = 'BOUND'
                    """,
                    (activation_id,),
                )
            connection.execute(
                """
                UPDATE activations SET state = 'REVOKE_FAILED', updated_at = ?
                WHERE id = ? AND state NOT IN ('TERMINATED', 'QUARANTINED')
                """,
                (utc_now(), activation_id),
            )
            connection.execute(
                """
                UPDATE activations SET state = 'QUARANTINED', updated_at = ?
                WHERE id = ? AND state = 'REVOKE_FAILED'
                """,
                (utc_now(), activation_id),
            )
        record = self._load_record(activation_id)
        try:
            self.resource_controller.quarantine(record, reason)
        except Exception:
            pass
        self._record_activation_audit(
            activation_id,
            "ACTIVATION_QUARANTINED",
            {"reason": reason, "profile_failure": profile_failure},
            f"activation-cleanup-failed:{activation_id}:"
            f"{hashlib.sha256(reason.encode()).hexdigest()}",
        )
        return record

    def _record_activation_audit(
        self,
        activation_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> None:
        record = self._load_record(activation_id)
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=_stable_id("audit", idempotency_key),
                event_type=event_type,
                actor="service:activation-manager",
                subject_type="activation",
                subject_id=activation_id,
                payload=payload,
                occurred_at=utc_now(),
                idempotency_key=idempotency_key,
                goal_id=record.goal_id,
                run_id=record.run_id,
                activation_id=activation_id,
                subject_oid=record.subject_oid,
            )
        )
