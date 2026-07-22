from __future__ import annotations

"""Managed Git authority for the existing Agent-Team worktree subsystem.

This module is deliberately a subordinate platform seam.  It provisions and
operates the target-specific bare repository, but it does not decide workspace
ownership, gate approval, merge policy, or promotion eligibility.

SQLite and Git are not presented as one atomic store.  Every mutating Git
operation first records a Phase 2 operation intent, performs an expected-state
or expected-OID guarded mutation, and then records the actual result.  A later
reconciler can therefore distinguish pending, applied, and completed work.
"""

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

try:
    from .agent_team_domain import (
        AuditEvent,
        IntentRecord,
        IntentStatus,
        ManagedRepositoryRecord,
        ManagedRepositoryState,
        RepositorySnapshot,
        TargetRegistration,
        TargetState,
        require_git_ref,
        require_identifier,
        require_nonempty,
        require_oid,
        thaw_json,
    )
    from .agent_team_paths import AxPathAuthority, AxPathError
    from .agent_team_state import (
        AxStateStore,
        IdempotencyConflictError,
        compact_json,
        utc_now,
    )
except ImportError:  # Direct script/module execution from the repository root.
    from agent_team_domain import (
        AuditEvent,
        IntentRecord,
        IntentStatus,
        ManagedRepositoryRecord,
        ManagedRepositoryState,
        RepositorySnapshot,
        TargetRegistration,
        TargetState,
        require_git_ref,
        require_identifier,
        require_nonempty,
        require_oid,
        thaw_json,
    )
    from agent_team_paths import AxPathAuthority, AxPathError
    from agent_team_state import (
        AxStateStore,
        IdempotencyConflictError,
        compact_json,
        utc_now,
    )


APPROVED_REF_PREFIX = "refs/agentic-ax/approved/"
EVIDENCE_REF_PREFIX = "refs/agentic-ax/"
MANAGED_BRANCH_PREFIX = "refs/heads/ax/"
_SAFE_REF_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_URI_CREDENTIAL = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:access_token|auth|key|password|signature|token)=)[^&\s]+"
)


class AgentTeamGitError(RuntimeError):
    """Base error for the managed Git boundary."""


class GitValidationError(AgentTeamGitError, ValueError):
    """Raised before Git mutation when an input is not a safe exact value."""


class GitCommandError(AgentTeamGitError):
    """Raised when a sanitized, bounded Git command fails."""

    def __init__(self, message: str, *, result: GitCommandResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class GitObjectError(AgentTeamGitError):
    """Raised when an exact object is missing or is not a commit."""


class GitRefError(AgentTeamGitError):
    """Raised when a ref is unsafe, symbolic, missing, or immutable."""


class GitCASMismatchError(GitRefError):
    """Raised when an expected source, destination, branch, or worktree OID drifts."""


class GitImmutableRefError(GitRefError):
    """Raised when immutable evidence would be rewritten."""


class TargetNotFoundError(AgentTeamGitError, KeyError):
    """Raised when the Phase 2 target registry has no requested target."""


class TargetRegistrationConflictError(AgentTeamGitError):
    """Raised when target ID, common-dir, source-ref, or managed path conflicts."""


class ManagedRepositoryError(AgentTeamGitError):
    """Raised when a managed repository is absent, unsafe, or malformed."""


class GitWorktreeError(AgentTeamGitError):
    """Raised when a disposable worktree cannot be safely materialized or removed."""


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    """Bounded result from one shell-free Git invocation."""

    arguments: tuple[str, ...]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(frozen=True, slots=True)
class GitWorktreeReceipt:
    """Exact-OID receipt consumed by Phase 5 workspace/sandbox services."""

    target_id: str
    managed_repository_path: str
    worktree_path: str
    oid: str
    branch_ref: str | None
    intent_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "target_id", require_identifier(self.target_id, "target_id")
        )
        for field in ("managed_repository_path", "worktree_path"):
            object.__setattr__(
                self, field, require_nonempty(getattr(self, field), field)
            )
        object.__setattr__(self, "oid", require_oid(self.oid, "oid"))
        if self.branch_ref is not None:
            branch = require_git_ref(self.branch_ref, "branch_ref")
            if not branch.startswith(MANAGED_BRANCH_PREFIX):
                raise GitValidationError(
                    f"branch_ref must be under {MANAGED_BRANCH_PREFIX}"
                )
            object.__setattr__(self, "branch_ref", branch)
        object.__setattr__(
            self, "intent_id", require_identifier(self.intent_id, "intent_id")
        )
        object.__setattr__(
            self,
            "idempotency_key",
            require_nonempty(self.idempotency_key, "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class GitRefUpdateReceipt:
    """Receipt for a low-level approved-namespace target ref update."""

    target_id: str
    source_ref: str
    destination_ref: str
    approved_oid: str
    expected_source_oid: str
    previous_destination_oid: str | None
    resulting_destination_oid: str
    intent_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "target_id", require_identifier(self.target_id, "target_id")
        )
        object.__setattr__(
            self, "source_ref", require_git_ref(self.source_ref, "source_ref")
        )
        destination = require_git_ref(self.destination_ref, "destination_ref")
        if not destination.startswith(APPROVED_REF_PREFIX):
            raise GitValidationError(
                f"destination_ref must be under {APPROVED_REF_PREFIX}"
            )
        object.__setattr__(self, "destination_ref", destination)
        for field in (
            "approved_oid",
            "expected_source_oid",
            "resulting_destination_oid",
        ):
            object.__setattr__(
                self, field, require_oid(getattr(self, field), field)
            )
        object.__setattr__(
            self,
            "previous_destination_oid",
            require_oid(
                self.previous_destination_oid,
                "previous_destination_oid",
                optional=True,
            ),
        )
        object.__setattr__(
            self, "intent_id", require_identifier(self.intent_id, "intent_id")
        )
        object.__setattr__(
            self,
            "idempotency_key",
            require_nonempty(self.idempotency_key, "idempotency_key"),
        )


class GitExecutor(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: str | Path,
        check: bool = True,
    ) -> GitCommandResult: ...


class GitCommandRunner:
    """Run Git with argument arrays, explicit cwd, hardened env, and bounded text."""

    def __init__(
        self,
        *,
        git_executable: str = "git",
        timeout_seconds: int = 120,
        max_output_bytes: int = 128 * 1024,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.git_executable = require_nonempty(git_executable, "git_executable")
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or timeout_seconds < 1
        ):
            raise ValueError("timeout_seconds must be a positive integer")
        if (
            not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or max_output_bytes < 1024
        ):
            raise ValueError("max_output_bytes must be at least 1024")
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.base_environment = dict(base_environment or os.environ)

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: str | Path,
        check: bool = True,
    ) -> GitCommandResult:
        if isinstance(arguments, (str, bytes)) or not isinstance(
            arguments, Sequence
        ):
            raise GitValidationError("Git arguments must be a sequence of strings")
        normalized = tuple(arguments)
        if not normalized:
            raise GitValidationError("Git arguments must not be empty")
        for argument in normalized:
            if not isinstance(argument, str):
                raise GitValidationError("every Git argument must be a string")
            if "\x00" in argument or "\r" in argument or "\n" in argument:
                raise GitValidationError("Git arguments must not contain control lines")

        resolved_cwd = Path(cwd).expanduser().resolve()
        if not resolved_cwd.is_dir():
            raise GitValidationError(f"Git cwd is not a directory: {resolved_cwd}")

        command = [self.git_executable, *normalized]
        try:
            completed = subprocess.run(
                command,
                cwd=resolved_cwd,
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError(
                f"Git command timed out after {self.timeout_seconds} seconds: "
                f"{_safe_command_name(normalized)}"
            ) from exc
        except OSError as exc:
            raise GitCommandError(
                f"Git command could not start: {_sanitize_diagnostic(str(exc))}"
            ) from exc

        stdout, stdout_truncated = self._decode_bounded(completed.stdout)
        stderr, stderr_truncated = self._decode_bounded(completed.stderr)
        result = GitCommandResult(
            arguments=normalized,
            cwd=str(resolved_cwd),
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
        if check and completed.returncode != 0:
            detail = _sanitize_diagnostic(stderr.strip()) or "no diagnostic"
            raise GitCommandError(
                f"Git command failed ({completed.returncode}) "
                f"{_safe_command_name(normalized)}: {detail}",
                result=result,
            )
        return result

    def _decode_bounded(self, value: bytes) -> tuple[str, bool]:
        truncated = len(value) > self.max_output_bytes
        bounded = value[: self.max_output_bytes]
        try:
            return bounded.decode("utf-8"), truncated
        except UnicodeDecodeError:
            return bounded.decode("utf-8", errors="replace"), truncated

    def _environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in self.base_environment.items()
            if not key.upper().startswith("GIT_")
        }
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": os.devnull,
                "GIT_CONFIG_KEY_1": "credential.helper",
                "GIT_CONFIG_VALUE_1": "",
                "GIT_CONFIG_KEY_2": "protocol.file.allow",
                "GIT_CONFIG_VALUE_2": "always",
                "LANG": "C.UTF-8",
            }
        )
        return environment


def _safe_command_name(arguments: Sequence[str]) -> str:
    command = next(
        (
            argument
            for argument in arguments
            if not argument.startswith("-") and "=" not in argument
        ),
        "git",
    )
    return _sanitize_diagnostic(command)


def _sanitize_diagnostic(value: str) -> str:
    redacted = _URI_CREDENTIAL.sub(r"\1<redacted>@", value)
    redacted = _SENSITIVE_QUERY.sub(r"\1<redacted>", redacted)
    return redacted[:4096]


def _path_key(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve()
    value = os.path.normcase(str(resolved))
    if os.name == "nt":
        value = value.casefold()
    return value.replace("\\", "/").rstrip("/")


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _stable_identifier(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _derived_idempotency_key(operation: str, payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(compact_json(payload).encode("utf-8")).hexdigest()
    return f"derived:{operation}:{digest}"


def _validate_ref_component(value: str, field: str) -> str:
    result = require_identifier(value, field)
    if (
        not _SAFE_REF_COMPONENT.fullmatch(result)
        or result in {".", "..", "@"}
        or result.endswith(".lock")
        or result.startswith(".")
        or ".." in result
    ):
        raise GitValidationError(
            f"{field} must be a safe Git-ref component using letters, digits, "
            "dot, underscore, or hyphen"
        )
    return result


class _GitBoundary:
    def __init__(
        self,
        *,
        state_store: AxStateStore,
        path_authority: AxPathAuthority,
        command_runner: GitExecutor | None = None,
    ) -> None:
        if not isinstance(state_store, AxStateStore):
            raise TypeError("state_store must be an AxStateStore")
        if not isinstance(path_authority, AxPathAuthority):
            raise TypeError("path_authority must be an AxPathAuthority")
        self.state_store = state_store
        self.path_authority = path_authority
        self.command_runner = command_runner or GitCommandRunner()
        self.state_store.initialize()

    def _git(
        self,
        arguments: Sequence[str],
        *,
        cwd: str | Path,
        check: bool = True,
    ) -> GitCommandResult:
        return self.command_runner.run(arguments, cwd=cwd, check=check)

    def _bare_git(
        self,
        repository: str | Path,
        arguments: Sequence[str],
        *,
        check: bool = True,
    ) -> GitCommandResult:
        repository_path = Path(repository).expanduser().resolve()
        return self._git(
            [f"--git-dir={repository_path}", *arguments],
            cwd=repository_path.parent,
            check=check,
        )

    def _validate_ref(
        self,
        value: str,
        field: str,
        *,
        required_prefix: str | None = None,
    ) -> str:
        try:
            result = require_git_ref(value, field)
        except ValueError as exc:
            raise GitValidationError(str(exc)) from exc
        if required_prefix is not None and not result.startswith(required_prefix):
            raise GitValidationError(
                f"{field} must be under {required_prefix}"
            )
        checked = self._git(
            ["check-ref-format", result],
            cwd=self.path_authority.ax_source_root,
            check=False,
        )
        if checked.returncode != 0:
            raise GitValidationError(f"{field} is not accepted by Git")
        return result

    def _require_exact_oid(self, value: str, field: str) -> str:
        try:
            return str(require_oid(value, field))
        except ValueError as exc:
            raise GitValidationError(str(exc)) from exc

    def _ensure_commit(self, repository: Path, oid: str) -> str:
        exact = self._require_exact_oid(oid, "oid")
        result = self._bare_git(
            repository,
            ["cat-file", "-t", exact],
            check=False,
        )
        if result.returncode != 0:
            raise GitObjectError(f"commit object is not present: {exact}")
        if result.stdout.strip() != "commit":
            raise GitObjectError(f"object is not a commit: {exact}")
        return exact

    def _read_direct_ref(
        self,
        repository: Path,
        ref: str,
        *,
        require_commit: bool,
        allow_missing: bool = False,
    ) -> str | None:
        validated = self._validate_ref(ref, "ref")
        symbolic = self._bare_git(
            repository,
            ["symbolic-ref", "--quiet", validated],
            check=False,
        )
        if symbolic.returncode == 0:
            raise GitRefError(f"symbolic refs are not accepted here: {validated}")
        if symbolic.returncode not in {1, 128}:
            raise GitRefError(f"could not inspect ref identity: {validated}")

        resolved = self._bare_git(
            repository,
            [
                "for-each-ref",
                "--format=%(refname)%09%(objectname)",
                validated,
            ],
        )
        exact_matches = []
        for line in resolved.stdout.splitlines():
            fields = line.split("\t", 1)
            if len(fields) == 2 and fields[0] == validated:
                exact_matches.append(fields[1])
        if not exact_matches:
            if allow_missing:
                return None
            raise GitRefError(f"ref does not exist: {validated}")
        if len(exact_matches) != 1:
            raise GitRefError(f"ref resolved ambiguously: {validated}")
        oid = self._require_exact_oid(exact_matches[0], "ref_oid")
        if require_commit:
            return self._ensure_commit(repository, oid)
        return oid

    def _load_target(self, target_id: str) -> TargetRegistration:
        target_id = require_identifier(target_id, "target_id")
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT
                    t.*,
                    m.id AS managed_repository_id,
                    m.repository_path AS managed_repository_path,
                    m.state AS managed_repository_state,
                    m.created_at AS managed_repository_created_at
                FROM targets AS t
                LEFT JOIN managed_repositories AS m ON m.target_id = t.id
                WHERE t.id = ?
                """,
                (target_id,),
            ).fetchone()
        if row is None:
            raise TargetNotFoundError(target_id)
        if row["managed_repository_id"] is None:
            raise ManagedRepositoryError(
                f"target has no managed repository record: {target_id}"
            )
        expected_path = self.path_authority.managed_repository(target_id)
        actual_path = Path(row["managed_repository_path"]).expanduser().resolve()
        if _path_key(expected_path) != _path_key(actual_path):
            raise ManagedRepositoryError(
                f"managed repository path disagrees with path authority: {target_id}"
            )
        try:
            self.path_authority.assert_runtime_outside_target(
                row["canonical_checkout_path"],
                git_common_dir=row["git_common_dir"],
            )
        except AxPathError as exc:
            raise TargetRegistrationConflictError(
                f"registered target overlaps AX authority: {target_id}"
            ) from exc
        return TargetRegistration(
            target_id=row["id"],
            canonical_worktree_path=row["canonical_checkout_path"],
            git_common_dir=row["git_common_dir"],
            source_ref=row["source_ref"],
            observed_source_oid=row["observed_source_oid"],
            managed_repository_path=str(actual_path),
            state=TargetState(row["state"]),
        )

    def _assert_target_ready(self, target: TargetRegistration) -> None:
        if target.state is not TargetState.ACTIVE:
            raise ManagedRepositoryError(
                f"target is not active: {target.target_id} ({target.state.value})"
            )
        managed = self._managed_record(target.target_id)
        if managed.state is not ManagedRepositoryState.READY:
            raise ManagedRepositoryError(
                "managed repository is not ready: "
                f"{target.target_id} ({managed.state.value})"
            )

    def _managed_record(self, target_id: str) -> ManagedRepositoryRecord:
        target_id = require_identifier(target_id, "target_id")
        with self.state_store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM managed_repositories WHERE target_id = ?",
                (target_id,),
            ).fetchone()
        if row is None:
            raise ManagedRepositoryError(
                f"managed repository is not registered for target: {target_id}"
            )
        return ManagedRepositoryRecord(
            managed_repository_id=row["id"],
            target_id=row["target_id"],
            repository_path=row["repository_path"],
            state=ManagedRepositoryState(row["state"]),
            created_at=row["created_at"],
        )

    def _ensure_repository_registration(
        self,
        target_id: str,
        source_oid: str,
    ) -> str:
        """Mirror the managed repository into the authoritative v4 relation.

        Repository IDs intentionally equal the stable target IDs.  This keeps
        the public ``repo_id`` boundary stable while the v2/v3 target records
        remain readable during migration.
        """

        target = require_identifier(target_id, "target_id")
        oid = self._require_exact_oid(source_oid, "source_oid")
        now = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            graph = connection.execute(
                """
                SELECT t.canonical_checkout_path, t.git_common_dir, t.state,
                       m.id AS managed_repository_id,
                       m.state AS managed_repository_state
                FROM targets AS t
                JOIN managed_repositories AS m ON m.target_id = t.id
                WHERE t.id = ?
                """,
                (target,),
            ).fetchone()
            if graph is None:
                raise ManagedRepositoryError(
                    f"repository registration graph is missing: {target}"
                )
            if graph["state"] != "ACTIVE" or graph["managed_repository_state"] != "READY":
                raise ManagedRepositoryError(
                    f"repository registration graph is not active: {target}"
                )
            existing = connection.execute(
                "SELECT * FROM repository_registrations WHERE id = ?",
                (target,),
            ).fetchone()
            signature = (
                target,
                graph["managed_repository_id"],
                _path_key(graph["canonical_checkout_path"]),
                _path_key(graph["git_common_dir"]),
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO repository_registrations (
                        id, target_id, managed_repository_id, canonical_path,
                        git_common_dir, source_oid, state, idempotency_key,
                        registered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                    """,
                    (
                        target,
                        target,
                        graph["managed_repository_id"],
                        graph["canonical_checkout_path"],
                        graph["git_common_dir"],
                        oid,
                        f"repository-registration:{target}",
                        now,
                    ),
                )
            else:
                actual = (
                    existing["target_id"],
                    existing["managed_repository_id"],
                    _path_key(existing["canonical_path"]),
                    _path_key(existing["git_common_dir"]),
                )
                if actual != signature or existing["state"] != "ACTIVE":
                    raise ManagedRepositoryError(
                        f"v4 repository registration conflicts with {target}"
                    )
                if existing["source_oid"] != oid:
                    connection.execute(
                        "UPDATE repository_registrations SET source_oid = ? WHERE id = ?",
                        (oid, target),
                    )
        return target

    def _audit_completed_intent(
        self,
        intent: IntentRecord,
        *,
        actor: str,
        subject_type: str,
        subject_id: str,
        subject_oid: str | None,
    ) -> None:
        event_id = _stable_identifier("audit", intent.intent_id)
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=event_id,
                event_type=f"{intent.operation.upper()}_COMPLETED",
                actor=actor,
                subject_type=subject_type,
                subject_id=subject_id,
                payload={
                    "intent_id": intent.intent_id,
                    "operation": intent.operation,
                    "resulting_state": intent.resulting_state,
                    "resulting_oid": intent.resulting_oid,
                    "evidence": thaw_json(intent.evidence),
                },
                occurred_at=intent.completed_at or datetime.now(UTC).isoformat(),
                idempotency_key=f"audit:{intent.intent_id}",
                subject_oid=subject_oid,
            )
        )


class ManagedRepositoryService(_GitBoundary):
    """Paved-path API for target identity and managed Git primitives."""

    def register_target(
        self,
        checkout: Path,
        *,
        source_ref: str,
        requested_target_id: str | None = None,
        idempotency_key: str,
    ) -> TargetRegistration:
        checkout_path = Path(checkout).expanduser().resolve()
        if not checkout_path.is_dir():
            raise GitValidationError(
                f"target checkout is not a directory: {checkout_path}"
            )
        source = self._validate_ref(source_ref, "source_ref")
        requested = None
        if requested_target_id is not None:
            requested = _validate_ref_component(
                requested_target_id, "requested_target_id"
            )
        key = require_nonempty(idempotency_key, "idempotency_key")

        common_result = self._git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=checkout_path,
            check=False,
        )
        top_result = self._git(
            ["rev-parse", "--path-format=absolute", "--show-toplevel"],
            cwd=checkout_path,
            check=False,
        )
        object_format_result = self._git(
            ["rev-parse", "--show-object-format"],
            cwd=checkout_path,
            check=False,
        )
        if (
            common_result.returncode != 0
            or top_result.returncode != 0
            or object_format_result.returncode != 0
        ):
            raise GitValidationError(
                f"target must be a non-bare Git worktree: {checkout_path}"
            )
        object_format = object_format_result.stdout.strip()
        if object_format not in {"sha1", "sha256"}:
            raise GitValidationError(
                f"unsupported Git object format: {object_format!r}"
            )
        git_common_dir = Path(common_result.stdout.strip())
        if not git_common_dir.is_absolute():
            git_common_dir = checkout_path / git_common_dir
        git_common_dir = git_common_dir.resolve()
        canonical_worktree = Path(top_result.stdout.strip()).resolve()
        filesystem_common = self.path_authority.canonical_git_common_dir(
            checkout_path
        )
        if (
            filesystem_common is None
            or _path_key(filesystem_common) != _path_key(git_common_dir)
        ):
            raise GitValidationError(
                "Git common directory disagrees with the local worktree metadata"
            )
        self.path_authority.assert_runtime_outside_target(
            canonical_worktree,
            git_common_dir=git_common_dir,
        )
        canonical_target_id = self.path_authority.canonical_target_identity(
            checkout_path
        )

        intent = self.state_store.begin_intent(
            operation="register-target",
            idempotency_key=key,
            expected_state="UNREGISTERED_OR_MATCHING",
            expected_oid=None,
            payload={
                "checkout": str(canonical_worktree),
                "git_common_dir": str(git_common_dir),
                "source_ref": source,
                "requested_target_id": requested,
                "canonical_target_id": canonical_target_id,
                "object_format": object_format,
            },
        )
        if intent.status is IntentStatus.COMPLETED:
            target_id = str(intent.evidence["target_id"])
            registration = self._load_target(target_id)
            self._ensure_repository_registration(
                registration.target_id,
                registration.observed_source_oid,
            )
            self._audit_completed_intent(
                intent,
                actor="service:managed-repository",
                subject_type="target",
                subject_id=registration.target_id,
                subject_oid=registration.observed_source_oid,
            )
            return registration

        observed_oid = self._read_direct_ref(
            git_common_dir,
            source,
            require_commit=True,
        )
        assert observed_oid is not None

        created_at = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            by_common = connection.execute(
                "SELECT * FROM targets WHERE git_common_dir = ?",
                (str(git_common_dir),),
            ).fetchone()
            target_id = (
                by_common["id"]
                if by_common is not None
                else (requested or canonical_target_id)
            )
            _validate_ref_component(target_id, "target_id")
            by_id = connection.execute(
                "SELECT * FROM targets WHERE id = ?",
                (target_id,),
            ).fetchone()

            if by_common is not None:
                if requested is not None and requested != by_common["id"]:
                    raise TargetRegistrationConflictError(
                        "requested target ID conflicts with the target already "
                        "registered for this Git common directory"
                    )
                if by_common["source_ref"] != source:
                    raise TargetRegistrationConflictError(
                        "the Git common directory is already registered with a "
                        "different source ref"
                    )
                if by_common["observed_source_oid"] != observed_oid:
                    raise GitCASMismatchError(
                        "registered source ref advanced; use resync_target instead "
                        "of duplicate registration"
                    )
                if by_common["state"] not in {"REGISTERED", "ACTIVE"}:
                    raise TargetRegistrationConflictError(
                        "the existing target registration is not reusable from "
                        f"state {by_common['state']}"
                    )
                if by_id is not None and by_id["git_common_dir"] != str(
                    git_common_dir
                ):
                    raise TargetRegistrationConflictError(
                        "target ID is already registered to another repository"
                    )
            else:
                if by_id is not None:
                    raise TargetRegistrationConflictError(
                        "target ID is already registered to another repository"
                    )
                managed_path = self.path_authority.managed_repository(target_id)
                managed_id = _stable_identifier("managed", target_id)
                connection.execute(
                    """
                    INSERT INTO targets (
                        id, canonical_checkout_path, git_common_dir, source_ref,
                        observed_source_oid, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'REGISTERED', ?, ?)
                    """,
                    (
                        target_id,
                        str(canonical_worktree),
                        str(git_common_dir),
                        source,
                        observed_oid,
                        created_at,
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO managed_repositories (
                        id, target_id, repository_path, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'PROVISIONING', ?, ?)
                    """,
                    (
                        managed_id,
                        target_id,
                        str(managed_path),
                        created_at,
                        created_at,
                    ),
                )

            repository_row = connection.execute(
                "SELECT * FROM managed_repositories WHERE target_id = ?",
                (target_id,),
            ).fetchone()
            if repository_row is None:
                raise ManagedRepositoryError(
                    "target registration has no managed repository reservation"
                )
            if repository_row["state"] not in {"PROVISIONING", "READY"}:
                raise ManagedRepositoryError(
                    "managed repository reservation is not reusable from state "
                    f"{repository_row['state']}"
                )

        managed_repository = self.path_authority.managed_repository(target_id)
        if _path_key(repository_row["repository_path"]) != _path_key(
            managed_repository
        ):
            raise TargetRegistrationConflictError(
                "reserved managed repository path conflicts with path authority"
            )
        self._ensure_bare_repository(
            managed_repository,
            object_format=object_format,
        )

        updated_at = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            repository_updated = connection.execute(
                """
                UPDATE managed_repositories
                SET state = 'READY', updated_at = ?
                WHERE target_id = ?
                  AND repository_path = ?
                  AND state IN ('PROVISIONING', 'READY')
                """,
                (updated_at, target_id, str(managed_repository)),
            ).rowcount
            target_updated = connection.execute(
                """
                UPDATE targets
                SET state = 'ACTIVE', updated_at = ?
                WHERE id = ? AND state IN ('REGISTERED', 'ACTIVE')
                """,
                (updated_at, target_id),
            ).rowcount
            if repository_updated != 1 or target_updated != 1:
                raise TargetRegistrationConflictError(
                    "target or managed repository state changed during provisioning"
                )

        self._ensure_repository_registration(target_id, observed_oid)

        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="ACTIVE",
            resulting_oid=observed_oid,
            evidence={
                "target_id": target_id,
                "git_common_dir": str(git_common_dir),
                "managed_repository_path": str(managed_repository),
            },
        )
        self._audit_completed_intent(
            completed,
            actor="service:managed-repository",
            subject_type="target",
            subject_id=target_id,
            subject_oid=observed_oid,
        )
        return self._load_target(target_id)

    def import_snapshot(
        self,
        target_id: str,
        *,
        expected_source_oid: str | None,
        idempotency_key: str,
    ) -> RepositorySnapshot:
        target = self._load_target(target_id)
        expected = (
            self._require_exact_oid(expected_source_oid, "expected_source_oid")
            if expected_source_oid is not None
            else target.observed_source_oid
        )
        return self._import_snapshot(
            target,
            operation="import-snapshot",
            expected_oid=expected,
            expected_previous_snapshot_oid=None,
            idempotency_key=idempotency_key,
        )

    def resync_target(
        self,
        target_id: str,
        *,
        expected_previous_snapshot_oid: str,
        idempotency_key: str,
    ) -> RepositorySnapshot:
        target = self._load_target(target_id)
        previous = self._require_exact_oid(
            expected_previous_snapshot_oid,
            "expected_previous_snapshot_oid",
        )
        return self._import_snapshot(
            target,
            operation="resync-target",
            expected_oid=previous,
            expected_previous_snapshot_oid=previous,
            idempotency_key=idempotency_key,
        )

    def resolve_commit(self, target_id: str, oid: str) -> str:
        target = self._load_target(target_id)
        self._assert_target_ready(target)
        repository = Path(target.managed_repository_path).resolve()
        return self._ensure_commit(
            repository,
            self._require_exact_oid(oid, "oid"),
        )

    def create_disposable_worktree(
        self,
        target_id: str,
        *,
        oid: str,
        path: Path,
        branch_ref: str | None,
    ) -> GitWorktreeReceipt:
        target = self._load_target(target_id)
        self._assert_target_ready(target)
        repository = Path(target.managed_repository_path).resolve()
        exact_oid = self._ensure_commit(
            repository, self._require_exact_oid(oid, "oid")
        )
        worktree_path = self._validate_disposable_path(path, repository)
        branch = None
        if branch_ref is not None:
            branch = self._validate_ref(
                branch_ref,
                "branch_ref",
                required_prefix=MANAGED_BRANCH_PREFIX,
            )
        payload = {
            "target_id": target.target_id,
            "managed_repository_path": str(repository),
            "worktree_path": str(worktree_path),
            "oid": exact_oid,
            "branch_ref": branch,
        }
        key = _derived_idempotency_key("create-disposable-worktree", payload)
        intent = self.state_store.begin_intent(
            operation="create-disposable-worktree",
            idempotency_key=key,
            expected_state="WORKTREE_ABSENT",
            expected_oid=exact_oid,
            payload=payload,
        )
        if intent.status is IntentStatus.COMPLETED:
            receipt = self._worktree_receipt_from_intent(intent)
            self._audit_completed_intent(
                intent,
                actor="service:managed-repository",
                subject_type="git-worktree",
                subject_id=receipt.worktree_path,
                subject_oid=receipt.oid,
            )
            return receipt

        existing = self._worktree_entries(repository).get(_path_key(worktree_path))
        if existing is not None:
            self._assert_worktree_entry(
                existing,
                expected_oid=exact_oid,
                expected_branch=branch,
            )
        else:
            if worktree_path.exists():
                raise GitWorktreeError(
                    "worktree destination already exists but is not registered: "
                    f"{worktree_path}"
                )
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            if branch is None:
                arguments = [
                    f"--git-dir={repository}",
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree_path),
                    exact_oid,
                ]
            else:
                branch_oid = self._read_direct_ref(
                    repository,
                    branch,
                    require_commit=True,
                    allow_missing=True,
                )
                if branch_oid is None:
                    self._bare_git(
                        repository,
                        [
                            "update-ref",
                            branch,
                            exact_oid,
                            "0" * len(exact_oid),
                        ],
                    )
                elif branch_oid != exact_oid:
                    raise GitCASMismatchError(
                        f"managed branch already points to {branch_oid}, "
                        f"expected {exact_oid}"
                    )
                arguments = [
                    f"--git-dir={repository}",
                    "worktree",
                    "add",
                    str(worktree_path),
                    branch.removeprefix("refs/heads/"),
                ]
            self._git(arguments, cwd=repository.parent)

        entry = self._worktree_entries(repository).get(_path_key(worktree_path))
        if entry is None:
            raise GitWorktreeError("Git did not register the requested worktree")
        self._assert_worktree_entry(
            entry,
            expected_oid=exact_oid,
            expected_branch=branch,
        )
        checkout_oid = self._git(
            ["rev-parse", "--verify", "HEAD"],
            cwd=worktree_path,
        ).stdout.strip()
        checkout_oid = self._require_exact_oid(checkout_oid, "worktree_head_oid")
        if checkout_oid != exact_oid:
            raise GitCASMismatchError(
                f"worktree materialized {checkout_oid}, expected {exact_oid}"
            )

        provisional = GitWorktreeReceipt(
            target_id=target.target_id,
            managed_repository_path=str(repository),
            worktree_path=str(worktree_path),
            oid=exact_oid,
            branch_ref=branch,
            intent_id=intent.intent_id,
            idempotency_key=key,
        )
        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="WORKTREE_READY",
            resulting_oid=exact_oid,
            evidence={"receipt": asdict(provisional)},
        )
        receipt = self._worktree_receipt_from_intent(completed)
        self._audit_completed_intent(
            completed,
            actor="service:managed-repository",
            subject_type="git-worktree",
            subject_id=receipt.worktree_path,
            subject_oid=receipt.oid,
        )
        return receipt

    def remove_disposable_worktree(
        self,
        receipt: GitWorktreeReceipt,
        *,
        expected_oid: str,
    ) -> None:
        if not isinstance(receipt, GitWorktreeReceipt):
            raise TypeError("receipt must be a GitWorktreeReceipt")
        exact_oid = self._require_exact_oid(expected_oid, "expected_oid")
        target = self._load_target(receipt.target_id)
        repository = Path(target.managed_repository_path).resolve()
        if _path_key(repository) != _path_key(receipt.managed_repository_path):
            raise GitWorktreeError("receipt managed repository does not match target")
        worktree_path = self._validate_disposable_path(
            Path(receipt.worktree_path),
            repository,
        )
        self._verify_worktree_receipt(receipt)

        payload = {
            "create_intent_id": receipt.intent_id,
            "target_id": receipt.target_id,
            "managed_repository_path": str(repository),
            "worktree_path": str(worktree_path),
            "expected_oid": exact_oid,
            "branch_ref": receipt.branch_ref,
        }
        key = _derived_idempotency_key("remove-disposable-worktree", payload)
        intent = self.state_store.begin_intent(
            operation="remove-disposable-worktree",
            idempotency_key=key,
            expected_state="WORKTREE_READY",
            expected_oid=exact_oid,
            payload=payload,
        )
        if intent.status is IntentStatus.COMPLETED:
            self._audit_completed_intent(
                intent,
                actor="service:managed-repository",
                subject_type="git-worktree",
                subject_id=str(worktree_path),
                subject_oid=exact_oid,
            )
            return

        entry = self._worktree_entries(repository).get(_path_key(worktree_path))
        if entry is None:
            if worktree_path.exists():
                raise GitWorktreeError(
                    "receipt path exists but is not the registered Git worktree"
                )
            prior_removal_oid = self._completed_removal_oid(
                receipt.intent_id,
                worktree_path,
                exclude_intent_id=intent.intent_id,
            )
            if prior_removal_oid != exact_oid:
                raise GitCASMismatchError(
                    "worktree is absent without a matching completed removal "
                    f"receipt at {exact_oid}"
                )
        else:
            self._assert_worktree_entry(
                entry,
                expected_oid=exact_oid,
                expected_branch=receipt.branch_ref,
            )
            checkout_oid = self._git(
                ["rev-parse", "--verify", "HEAD"],
                cwd=worktree_path,
            ).stdout.strip()
            checkout_oid = self._require_exact_oid(
                checkout_oid, "worktree_head_oid"
            )
            if checkout_oid != exact_oid:
                raise GitCASMismatchError(
                    f"worktree HEAD is {checkout_oid}, expected {exact_oid}"
                )
            self._bare_git(
                repository,
                ["worktree", "remove", "--force", str(worktree_path)],
            )

        if _path_key(worktree_path) in self._worktree_entries(repository):
            raise GitWorktreeError("Git worktree metadata remains after removal")
        if worktree_path.exists():
            raise GitWorktreeError("worktree path remains after Git removal")

        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="WORKTREE_REMOVED",
            resulting_oid=exact_oid,
            evidence={
                "create_intent_id": receipt.intent_id,
                "worktree_path": str(worktree_path),
                "removed": True,
            },
        )
        self._audit_completed_intent(
            completed,
            actor="service:managed-repository",
            subject_type="git-worktree",
            subject_id=str(worktree_path),
            subject_oid=exact_oid,
        )

    def _ensure_bare_repository(
        self,
        repository: Path,
        *,
        object_format: str,
    ) -> None:
        if object_format not in {"sha1", "sha256"}:
            raise GitValidationError(
                f"unsupported Git object format: {object_format!r}"
            )
        repository = self.path_authority.assert_runtime_path(repository)
        if repository.exists():
            if not repository.is_dir():
                raise ManagedRepositoryError(
                    f"managed repository path is not a directory: {repository}"
                )
            probe = self._bare_git(
                repository,
                ["rev-parse", "--is-bare-repository"],
                check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "true":
                actual_format = self._bare_git(
                    repository,
                    ["rev-parse", "--show-object-format"],
                ).stdout.strip()
                if actual_format != object_format:
                    raise ManagedRepositoryError(
                        "managed repository object format does not match target: "
                        f"{actual_format} != {object_format}"
                    )
                return
            if any(repository.iterdir()):
                raise ManagedRepositoryError(
                    f"managed repository path contains non-bare content: {repository}"
                )
        repository.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            [
                "init",
                "--bare",
                f"--object-format={object_format}",
                "--initial-branch=ax/bootstrap",
                str(repository),
            ],
            cwd=repository.parent,
        )
        probe = self._bare_git(
            repository,
            ["rev-parse", "--is-bare-repository"],
        )
        if probe.stdout.strip() != "true":
            raise ManagedRepositoryError(
                f"Git did not create a bare repository: {repository}"
            )
        actual_format = self._bare_git(
            repository,
            ["rev-parse", "--show-object-format"],
        ).stdout.strip()
        if actual_format != object_format:
            raise ManagedRepositoryError(
                "Git created the managed repository with the wrong object format"
            )

    def _import_snapshot(
        self,
        target: TargetRegistration,
        *,
        operation: str,
        expected_oid: str,
        expected_previous_snapshot_oid: str | None,
        idempotency_key: str,
    ) -> RepositorySnapshot:
        key = require_nonempty(idempotency_key, "idempotency_key")
        repository = Path(target.managed_repository_path).resolve()
        managed = self._managed_record(target.target_id)
        if target.state not in {TargetState.ACTIVE, TargetState.RESYNC_REQUIRED}:
            raise ManagedRepositoryError(
                f"target cannot import/resync from state {target.state.value}"
            )
        if managed.state not in {
            ManagedRepositoryState.READY,
            ManagedRepositoryState.RESYNC_REQUIRED,
        }:
            raise ManagedRepositoryError(
                "managed repository cannot import/resync from state "
                f"{managed.state.value}"
            )
        snapshot_id = _stable_identifier(
            "snapshot", operation, target.target_id, key
        )
        evidence_ref = self._validate_ref(
            (
                f"refs/agentic-ax/imported/"
                f"{_validate_ref_component(target.target_id, 'target_id')}/"
                f"{snapshot_id}"
            ),
            "evidence_ref",
            required_prefix=EVIDENCE_REF_PREFIX,
        )
        intent = self.state_store.begin_intent(
            operation=operation,
            idempotency_key=key,
            expected_state=(
                "LATEST_SNAPSHOT_MATCHES"
                if expected_previous_snapshot_oid is not None
                else "TARGET_REGISTERED"
            ),
            expected_oid=expected_oid,
            payload={
                "target_id": target.target_id,
                "managed_repository_id": managed.managed_repository_id,
                "source_ref": target.source_ref,
                "evidence_ref": evidence_ref,
                "snapshot_id": snapshot_id,
            },
        )
        if intent.status is IntentStatus.COMPLETED:
            snapshot = self._load_snapshot(str(intent.evidence["snapshot_id"]))
            self._audit_completed_intent(
                intent,
                actor="service:managed-repository",
                subject_type="repository-snapshot",
                subject_id=snapshot.snapshot_id,
                subject_oid=snapshot.imported_oid,
            )
            return snapshot

        if expected_previous_snapshot_oid is not None:
            with self.state_store.transaction() as connection:
                latest = connection.execute(
                    """
                    SELECT imported_oid
                    FROM repository_snapshots
                    WHERE target_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (target.target_id,),
                ).fetchone()
            if latest is None:
                raise GitCASMismatchError(
                    "resync requires an existing imported snapshot"
                )
            if latest["imported_oid"] != expected_previous_snapshot_oid:
                raise GitCASMismatchError(
                    "latest managed snapshot does not match "
                    "expected_previous_snapshot_oid"
                )

        source_repository = Path(target.git_common_dir).expanduser().resolve()
        current_source_oid = self._read_direct_ref(
            source_repository,
            target.source_ref,
            require_commit=True,
        )
        assert current_source_oid is not None
        if (
            expected_previous_snapshot_oid is None
            and current_source_oid != expected_oid
        ):
            raise GitCASMismatchError(
                f"source ref is {current_source_oid}, expected {expected_oid}"
            )

        self._bare_git(
            repository,
            [
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-recurse-submodules",
                "--force",
                str(source_repository),
                target.source_ref,
            ],
        )
        self._ensure_commit(repository, current_source_oid)
        self._ensure_immutable_ref(repository, evidence_ref, current_source_oid)

        created_at = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM repository_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
            expected_signature = (
                target.target_id,
                managed.managed_repository_id,
                target.source_ref,
                current_source_oid,
                current_source_oid,
                evidence_ref,
                key,
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO repository_snapshots (
                        id, target_id, managed_repository_id, source_ref,
                        source_oid, imported_oid, evidence_ref,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        *expected_signature,
                        created_at,
                    ),
                )
            else:
                actual_signature = (
                    existing["target_id"],
                    existing["managed_repository_id"],
                    existing["source_ref"],
                    existing["source_oid"],
                    existing["imported_oid"],
                    existing["evidence_ref"],
                    existing["idempotency_key"],
                )
                if actual_signature != expected_signature:
                    raise IdempotencyConflictError(
                        "snapshot identity was reused with different Git evidence"
                    )
            now = utc_now()
            target_updated = connection.execute(
                """
                UPDATE targets
                SET observed_source_oid = ?, state = 'ACTIVE', updated_at = ?
                WHERE id = ?
                  AND state IN ('ACTIVE', 'RESYNC_REQUIRED')
                """,
                (current_source_oid, now, target.target_id),
            ).rowcount
            repository_updated = connection.execute(
                """
                UPDATE managed_repositories
                SET state = 'READY', updated_at = ?
                WHERE id = ?
                  AND state IN ('READY', 'RESYNC_REQUIRED')
                """,
                (now, managed.managed_repository_id),
            ).rowcount
            if target_updated != 1 or repository_updated != 1:
                raise ManagedRepositoryError(
                    "target or managed repository state changed during snapshot import"
                )

        self._ensure_repository_registration(target.target_id, current_source_oid)

        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="SNAPSHOT_IMPORTED",
            resulting_oid=current_source_oid,
            evidence={
                "snapshot_id": snapshot_id,
                "evidence_ref": evidence_ref,
                "source_oid": current_source_oid,
                "imported_oid": current_source_oid,
            },
        )
        snapshot = self._load_snapshot(snapshot_id)
        self._audit_completed_intent(
            completed,
            actor="service:managed-repository",
            subject_type="repository-snapshot",
            subject_id=snapshot.snapshot_id,
            subject_oid=snapshot.imported_oid,
        )
        return snapshot

    def _load_snapshot(self, snapshot_id: str) -> RepositorySnapshot:
        snapshot_id = require_identifier(snapshot_id, "snapshot_id")
        with self.state_store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM repository_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise ManagedRepositoryError(f"snapshot does not exist: {snapshot_id}")
        return RepositorySnapshot(
            snapshot_id=row["id"],
            target_id=row["target_id"],
            managed_repository_id=row["managed_repository_id"],
            source_ref=row["source_ref"],
            source_oid=row["source_oid"],
            imported_oid=row["imported_oid"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
        )

    def _ensure_immutable_ref(
        self,
        repository: Path,
        ref: str,
        oid: str,
    ) -> None:
        validated = self._validate_ref(
            ref,
            "evidence_ref",
            required_prefix=EVIDENCE_REF_PREFIX,
        )
        current = self._read_direct_ref(
            repository,
            validated,
            require_commit=True,
            allow_missing=True,
        )
        if current is None:
            self._bare_git(
                repository,
                [
                    "update-ref",
                    validated,
                    oid,
                    "0" * len(oid),
                ],
            )
            return
        if current != oid:
            raise GitImmutableRefError(
                f"immutable evidence ref {validated} is {current}, expected {oid}"
            )

    def _validate_disposable_path(
        self,
        path: Path,
        repository: Path,
    ) -> Path:
        raw = str(path)
        if "\x00" in raw or "\r" in raw or "\n" in raw:
            raise GitValidationError("worktree path contains control characters")
        try:
            resolved = self.path_authority.assert_runtime_path(path)
        except AxPathError as exc:
            raise GitValidationError(str(exc)) from exc
        allowed_roots = (
            self.path_authority.workspaces_root,
            self.path_authority.activations_root,
        )
        if not any(
            resolved != root and _is_within(root, resolved)
            for root in allowed_roots
        ):
            raise GitValidationError(
                "disposable worktree path must be beneath workspaces/ or activations/"
            )
        if _is_within(repository, resolved) or _is_within(resolved, repository):
            raise GitValidationError(
                "disposable worktree path overlaps managed repository authority"
            )
        return resolved

    def _worktree_entries(
        self, repository: Path
    ) -> dict[str, Mapping[str, str | bool]]:
        result = self._bare_git(
            repository,
            ["worktree", "list", "--porcelain"],
        )
        entries: dict[str, Mapping[str, str | bool]] = {}
        for block in result.stdout.replace("\r\n", "\n").strip().split("\n\n"):
            if not block.strip():
                continue
            entry: dict[str, str | bool] = {}
            for line in block.splitlines():
                if " " in line:
                    key, value = line.split(" ", 1)
                    entry[key] = value
                else:
                    entry[line] = True
            path_value = entry.get("worktree")
            if isinstance(path_value, str):
                entries[_path_key(path_value)] = entry
        return entries

    @staticmethod
    def _assert_worktree_entry(
        entry: Mapping[str, str | bool],
        *,
        expected_oid: str,
        expected_branch: str | None,
    ) -> None:
        actual_oid = entry.get("HEAD")
        if actual_oid != expected_oid:
            raise GitCASMismatchError(
                f"worktree metadata is pinned to {actual_oid}, expected {expected_oid}"
            )
        actual_branch = entry.get("branch")
        detached = bool(entry.get("detached"))
        if expected_branch is None:
            if not detached or actual_branch is not None:
                raise GitWorktreeError("expected a detached disposable worktree")
        elif actual_branch != expected_branch or detached:
            raise GitWorktreeError(
                f"worktree branch is {actual_branch}, expected {expected_branch}"
            )

    @staticmethod
    def _worktree_receipt_from_intent(
        intent: IntentRecord,
    ) -> GitWorktreeReceipt:
        receipt = intent.evidence.get("receipt")
        if not isinstance(receipt, Mapping):
            raise GitWorktreeError(
                f"completed worktree intent has no receipt: {intent.intent_id}"
            )
        return GitWorktreeReceipt(**thaw_json(receipt))

    def _verify_worktree_receipt(self, receipt: GitWorktreeReceipt) -> None:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM operation_intents WHERE id = ?",
                (receipt.intent_id,),
            ).fetchone()
        if row is None or row["operation"] != "create-disposable-worktree":
            raise GitWorktreeError("worktree receipt has no matching create intent")
        if row["status"] != IntentStatus.COMPLETED.value:
            raise GitWorktreeError("worktree create intent is not completed")
        evidence = json.loads(row["evidence_json"])
        expected = evidence.get("receipt")
        if expected != asdict(receipt):
            raise GitWorktreeError("worktree receipt differs from journal evidence")

    def _completed_removal_oid(
        self,
        create_intent_id: str,
        worktree_path: Path,
        *,
        exclude_intent_id: str,
    ) -> str | None:
        with self.state_store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, payload_json, resulting_oid
                FROM operation_intents
                WHERE operation = 'remove-disposable-worktree'
                  AND status = 'COMPLETED'
                  AND id <> ?
                """,
                (exclude_intent_id,),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if (
                payload.get("create_intent_id") == create_intent_id
                and _path_key(payload.get("worktree_path", ""))
                == _path_key(worktree_path)
            ):
                return row["resulting_oid"]
        return None


class TargetRefAdapter(_GitBoundary):
    """Low-level object transfer and approved-namespace CAS primitive.

    The adapter does not inspect gates and does not decide whether an OID is
    eligible for promotion.  Phase 6 must make that decision before calling
    this primitive.
    """

    def transfer_object_and_update_namespaced_ref(
        self,
        *,
        target: TargetRegistration,
        approved_oid: str,
        destination_ref: str,
        expected_source_oid: str,
        expected_destination_oid: str | None,
        idempotency_key: str,
    ) -> GitRefUpdateReceipt:
        if not isinstance(target, TargetRegistration):
            raise TypeError("target must be a TargetRegistration")
        stored = self._load_target(target.target_id)
        self._assert_target_ready(stored)
        identity_fields = (
            "target_id",
            "canonical_worktree_path",
            "git_common_dir",
            "source_ref",
            "managed_repository_path",
        )
        if any(
            _path_key(getattr(stored, field))
            != _path_key(getattr(target, field))
            if field
            in {
                "canonical_worktree_path",
                "git_common_dir",
                "managed_repository_path",
            }
            else getattr(stored, field) != getattr(target, field)
            for field in identity_fields
        ):
            raise TargetRegistrationConflictError(
                "target receipt does not match the current Phase 2 registry"
            )
        approved = self._require_exact_oid(approved_oid, "approved_oid")
        expected_source = self._require_exact_oid(
            expected_source_oid, "expected_source_oid"
        )
        expected_destination = (
            self._require_exact_oid(
                expected_destination_oid, "expected_destination_oid"
            )
            if expected_destination_oid is not None
            else None
        )
        destination = self._validate_ref(
            destination_ref,
            "destination_ref",
            required_prefix=APPROVED_REF_PREFIX,
        )
        key = require_nonempty(idempotency_key, "idempotency_key")

        managed_repository = Path(stored.managed_repository_path).resolve()
        target_repository = Path(stored.git_common_dir).resolve()
        self._ensure_commit(managed_repository, approved)

        intent = self.state_store.begin_intent(
            operation="update-approved-target-ref",
            idempotency_key=key,
            expected_state=(
                "DESTINATION_ABSENT"
                if expected_destination is None
                else "DESTINATION_MATCHES"
            ),
            expected_oid=expected_destination,
            payload={
                "target_id": stored.target_id,
                "source_ref": stored.source_ref,
                "approved_oid": approved,
                "destination_ref": destination,
                "expected_source_oid": expected_source,
                "expected_destination_oid": expected_destination,
            },
        )
        if intent.status is IntentStatus.COMPLETED:
            receipt = self._ref_receipt_from_intent(intent)
            self._audit_completed_intent(
                intent,
                actor="service:target-ref-adapter",
                subject_type="target-ref",
                subject_id=receipt.destination_ref,
                subject_oid=receipt.resulting_destination_oid,
            )
            return receipt

        source_now = self._read_direct_ref(
            target_repository,
            stored.source_ref,
            require_commit=True,
        )
        if source_now != expected_source:
            raise GitCASMismatchError(
                f"source ref is {source_now}, expected {expected_source}"
            )
        destination_now = self._read_direct_ref(
            target_repository,
            destination,
            require_commit=True,
            allow_missing=True,
        )
        if destination_now != expected_destination:
            raise GitCASMismatchError(
                f"destination ref is {destination_now}, "
                f"expected {expected_destination}"
            )

        self._bare_git(
            target_repository,
            [
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-recurse-submodules",
                "--force",
                str(managed_repository),
                approved,
            ],
        )
        self._ensure_commit(target_repository, approved)

        source_immediately_before_update = self._read_direct_ref(
            target_repository,
            stored.source_ref,
            require_commit=True,
        )
        if source_immediately_before_update != expected_source:
            raise GitCASMismatchError(
                "source ref changed during object transfer; target ref was not updated"
            )
        destination_immediately_before_update = self._read_direct_ref(
            target_repository,
            destination,
            require_commit=True,
            allow_missing=True,
        )
        if destination_immediately_before_update != expected_destination:
            raise GitCASMismatchError(
                "destination ref changed during object transfer; target ref was not updated"
            )

        self._bare_git(
            target_repository,
            [
                "update-ref",
                destination,
                approved,
                (
                    expected_destination
                    if expected_destination is not None
                    else "0" * len(approved)
                ),
            ],
        )
        resulting = self._read_direct_ref(
            target_repository,
            destination,
            require_commit=True,
        )
        if resulting != approved:
            raise GitCASMismatchError(
                f"destination ref became {resulting}, expected {approved}"
            )

        provisional = GitRefUpdateReceipt(
            target_id=stored.target_id,
            source_ref=stored.source_ref,
            destination_ref=destination,
            approved_oid=approved,
            expected_source_oid=expected_source,
            previous_destination_oid=expected_destination,
            resulting_destination_oid=resulting,
            intent_id=intent.intent_id,
            idempotency_key=key,
        )
        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="TARGET_REF_UPDATED",
            resulting_oid=resulting,
            evidence={"receipt": asdict(provisional)},
        )
        receipt = self._ref_receipt_from_intent(completed)
        self._audit_completed_intent(
            completed,
            actor="service:target-ref-adapter",
            subject_type="target-ref",
            subject_id=receipt.destination_ref,
            subject_oid=receipt.resulting_destination_oid,
        )
        return receipt

    @staticmethod
    def _ref_receipt_from_intent(intent: IntentRecord) -> GitRefUpdateReceipt:
        receipt = intent.evidence.get("receipt")
        if not isinstance(receipt, Mapping):
            raise GitRefError(
                f"completed target-ref intent has no receipt: {intent.intent_id}"
            )
        return GitRefUpdateReceipt(**thaw_json(receipt))
