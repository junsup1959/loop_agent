from __future__ import annotations

"""Leased developer worktrees and the scoped consumer-facing Git facade.

This module is a detailed workspace seam inside the existing Agent-Team
architecture.  It deliberately does not launch agent processes or claim OS
write-root enforcement; :class:`WorkspaceExecutionContract` is the immutable
contract that Phase 7 must enforce when it starts a runner.
"""

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from .agent_team_domain import (
        AuditEvent,
        CandidateState,
        CandidateSubmission,
        FindingSeverity,
        FindingState,
        IntentStatus,
        LeaseState,
        ReconciliationFinding,
        WorkspaceLease,
        require_git_ref,
        require_identifier,
        require_nonempty,
        require_oid,
        thaw_json,
    )
    from .agent_team_git import (
        GitCASMismatchError,
        GitCommandError,
        GitValidationError,
        GitWorktreeReceipt,
        ManagedRepositoryService,
    )
    from .agent_team_paths import AxPathAuthority
    from .agent_team_state import (
        AxStateStore,
        IdempotencyConflictError,
        compact_json,
        utc_now,
    )
except ImportError:
    from agent_team_domain import (
        AuditEvent,
        CandidateState,
        CandidateSubmission,
        FindingSeverity,
        FindingState,
        IntentStatus,
        LeaseState,
        ReconciliationFinding,
        WorkspaceLease,
        require_git_ref,
        require_identifier,
        require_nonempty,
        require_oid,
        thaw_json,
    )
    from agent_team_git import (
        GitCASMismatchError,
        GitCommandError,
        GitValidationError,
        GitWorktreeReceipt,
        ManagedRepositoryService,
    )
    from agent_team_paths import AxPathAuthority
    from agent_team_state import (
        AxStateStore,
        IdempotencyConflictError,
        compact_json,
        utc_now,
    )


MANAGED_WORK_BRANCH_PREFIX = "refs/heads/ax/work/"
_SAFE_REF_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ZERO_OID_LENGTHS = {40, 64}
_COMMITTER_NAME = "Agentic AX Workspace"
_COMMITTER_EMAIL = "agentic-ax@localhost.invalid"


class WorkspaceError(RuntimeError):
    """Base error for leased workspaces."""


class WorkspaceValidationError(WorkspaceError, ValueError):
    """Raised when a path, scope, identity, or message is unsafe."""


class WorkspaceConflictError(WorkspaceError):
    """Raised when another lease or Git worktree owns the requested resource."""


class WorkspaceNotFoundError(WorkspaceError, KeyError):
    """Raised when a lease or workspace does not exist."""


class WorkspaceLeaseStateError(WorkspaceError):
    """Raised when a lease is expired, stale, released, or quarantined."""


class WorkspaceScopeError(WorkspaceError):
    """Raised before staging when any changed path is outside declared scope."""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = tuple(paths)
        super().__init__(
            "changed paths are outside the lease write scope: "
            + ", ".join(self.paths)
        )


class WorkspaceDirtyError(WorkspaceError):
    """Raised when an operation requires a clean worktree."""


class WorkspaceCheckpointError(WorkspaceError):
    """Raised when a scoped checkpoint cannot be created safely."""


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    lease_id: str
    branch_ref: str
    head_oid: str
    changed_paths: tuple[str, ...]
    staged_paths: tuple[str, ...]
    unstaged_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    out_of_scope_paths: tuple[str, ...]
    clean: bool


@dataclass(frozen=True, slots=True)
class WorkspaceExecutionContract:
    """Exact runner boundary; Phase 7 supplies the OS enforcement."""

    lease_id: str
    target_id: str
    goal_id: str
    work_item_id: str
    revision: int
    owner: str
    branch_ref: str
    expected_head_oid: str
    cwd: Path
    source_write_paths: tuple[Path, ...]
    generated_write_paths: tuple[Path, ...]
    prohibited_roots: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceReleaseReceipt:
    lease_id: str
    workspace_id: str
    owner: str
    branch_ref: str
    final_oid: str
    worktree_path: Path
    released_at: str


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _path_key(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    value = os.path.normcase(str(resolved))
    if os.name == "nt":
        value = value.casefold()
    return value.replace("\\", "/").rstrip("/")


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _normalize_repo_path(value: str, field: str = "path") -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceValidationError(f"{field} must be a non-empty path")
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
        or raw.startswith(":")
        or "\x00" in raw
    ):
        raise WorkspaceValidationError(
            f"{field} must be a normalized repository-relative path: {value!r}"
        )
    if path.parts and path.parts[0].casefold() == ".git":
        raise WorkspaceValidationError(f"{field} may not grant Git metadata access")
    return path.as_posix()


def _normalize_scope(values: Sequence[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise WorkspaceValidationError(f"{field} must be a sequence of paths")
    result = tuple(_normalize_repo_path(value, field) for value in values)
    if field == "source_write_scope" and not result:
        raise WorkspaceValidationError("source_write_scope must not be empty")
    if len(result) != len(set(result)):
        raise WorkspaceValidationError(f"{field} contains duplicate paths")
    return result


def _scope_contains(scope: str, path: str) -> bool:
    scope_path = PurePosixPath(scope)
    path_value = PurePosixPath(path)
    return path_value == scope_path or scope_path in path_value.parents


def _safe_ref_component(value: str, field: str) -> str:
    result = require_identifier(value, field)
    if (
        not _SAFE_REF_COMPONENT.fullmatch(result)
        or result.startswith(".")
        or result.endswith(".lock")
        or result in {".", "..", "@"}
        or ".." in result
    ):
        raise WorkspaceValidationError(
            f"{field} cannot be represented safely in an AX branch ref"
        )
    return result


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise WorkspaceValidationError("persisted lease expiry must include a timezone")
    return parsed.astimezone(UTC)


class WorkspaceManager:
    """Allocate, release, and reconcile one active writer per lease."""

    def __init__(
        self,
        *,
        state_store: AxStateStore,
        path_authority: AxPathAuthority,
        repository_service: ManagedRepositoryService,
        clock: Callable[[], datetime] | None = None,
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
        self.clock = clock or (lambda: datetime.now(UTC))
        self.state_store.initialize()

    def allocate_development(
        self,
        *,
        goal_id: str,
        work_item_id: str,
        revision: int,
        owner_seat_id: str,
        target_id: str,
        base_oid: str,
        source_write_scope: Sequence[str],
        generated_write_scope: Sequence[str],
        lease_seconds: int,
        idempotency_key: str,
        run_id: str | None = None,
        slot_id: str | None = None,
        repository_id: str | None = None,
    ) -> WorkspaceLease:
        goal = _safe_ref_component(goal_id, "goal_id")
        work_item = _safe_ref_component(work_item_id, "work_item_id")
        target = require_identifier(target_id, "target_id")
        owner = require_identifier(owner_seat_id, "owner_seat_id")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise WorkspaceValidationError("revision must be a positive integer")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds < 1
        ):
            raise WorkspaceValidationError("lease_seconds must be positive")
        base = str(require_oid(base_oid, "base_oid"))
        source_scope = _normalize_scope(source_write_scope, "source_write_scope")
        generated_scope = _normalize_scope(
            generated_write_scope, "generated_write_scope"
        )
        key = require_nonempty(idempotency_key, "idempotency_key")
        runtime_run = (
            require_identifier(run_id, "run_id")
            if run_id is not None
            else "legacy"
        )
        lease_id = _stable_id("lease", key)
        branch_ref = require_git_ref(
            f"{MANAGED_WORK_BRANCH_PREFIX}{goal}/{work_item}/{revision}",
            "branch_ref",
        )
        worktree_path = self.path_authority.workspace(
            goal, runtime_run, lease_id
        )
        workspace_id = _stable_id(
            "workspace", target, goal, work_item, str(revision)
        )
        revision_id = self._validate_allocation_graph(
            target_id=target,
            goal_id=goal,
            work_item_id=work_item,
            revision=revision,
            owner=owner,
            base_oid=base,
            source_scope=source_scope,
        )
        self.repository_service.resolve_commit(target, base)
        payload = {
            "lease_id": lease_id,
            "workspace_id": workspace_id,
            "target_id": target,
            "goal_id": goal,
            "work_item_id": work_item,
            "revision_id": revision_id,
            "revision": revision,
            "owner": owner,
            "branch_ref": branch_ref,
            "worktree_path": str(worktree_path),
            "base_oid": base,
            "source_write_scope": list(source_scope),
            "generated_write_scope": list(generated_scope),
            "lease_seconds": lease_seconds,
            "run_id": runtime_run,
            "slot_id": slot_id,
            "repository_id": repository_id,
        }
        intent = self.state_store.begin_intent(
            operation="allocate-development-workspace",
            idempotency_key=key,
            expected_state="WORKSPACE_ABSENT",
            expected_oid=base,
            payload=payload,
        )
        if intent.status is IntentStatus.COMPLETED:
            lease = self._lease_from_completed_intent(intent)
            self._assert_lease_materialized(lease)
            if run_id is not None and slot_id is not None and repository_id is not None:
                self._persist_runtime_lease(
                    lease,
                    repository_id=repository_id,
                    run_id=run_id,
                    slot_id=slot_id,
                )
            return lease

        existing = self._load_lease(lease_id, allow_missing=True)
        if existing is not None and existing.state is not LeaseState.ACTIVE:
            raise WorkspaceLeaseStateError(
                f"idempotent allocation cannot reuse {existing.state.value} lease "
                f"{lease_id}"
            )

        self._assert_no_unleased_git_conflict(
            target_id=target,
            branch_ref=branch_ref,
            worktree_path=worktree_path,
            expected_oid=base,
            lease_id=lease_id,
        )
        expires_at = (self.clock().astimezone(UTC) + timedelta(seconds=lease_seconds)).isoformat(
            timespec="microseconds"
        )
        self._reserve_lease(
            lease_id=lease_id,
            workspace_id=workspace_id,
            target_id=target,
            goal_id=goal,
            work_item_id=work_item,
            revision_id=revision_id,
            owner=owner,
            branch_ref=branch_ref,
            worktree_path=worktree_path,
            base_oid=base,
            source_scope=source_scope,
            generated_scope=generated_scope,
            expires_at=expires_at,
            idempotency_key=key,
        )
        try:
            git_receipt = self.repository_service.create_disposable_worktree(
                target,
                oid=base,
                path=worktree_path,
                branch_ref=branch_ref,
            )
            self._activate_reservation(
                lease_id=lease_id,
                workspace_id=workspace_id,
                revision_id=revision_id,
                work_item_id=work_item,
            )
        except Exception:
            self._quarantine_reservation(
                lease_id,
                workspace_id,
                reason="Git worktree materialization did not complete",
            )
            raise

        lease = self._load_lease(lease_id)
        if run_id is not None and slot_id is not None and repository_id is not None:
            self._persist_runtime_lease(
                lease,
                repository_id=repository_id,
                run_id=run_id,
                slot_id=slot_id,
            )
        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="ACTIVE",
            resulting_oid=base,
            evidence={
                "lease": asdict(lease),
                "git_worktree_receipt": asdict(git_receipt),
            },
        )
        self._record_audit(
            event_type="DEVELOPMENT_WORKSPACE_ALLOCATED",
            subject_id=lease.lease_id,
            subject_oid=lease.expected_head_oid,
            idempotency_key=f"audit:{completed.intent_id}",
            goal_id=lease.goal_id,
            payload={
                "workspace_id": lease.workspace_id,
                "branch_ref": lease.branch_ref,
                "worktree_path": lease.worktree_path,
                "owner": lease.owner,
            },
        )
        return lease

    def allocate_developer_workspace(
        self,
        repo_id: str,
        run_id: str,
        work_item_id: str,
        base_oid: str,
        slot_id: str,
    ) -> WorkspaceLease:
        """Allocate a developer worktree from the authoritative v4 graph."""

        repository = require_identifier(repo_id, "repo_id")
        run = require_identifier(run_id, "run_id")
        work_item = require_identifier(work_item_id, "work_item_id")
        slot = require_identifier(slot_id, "slot_id")
        base = str(require_oid(base_oid, "base_oid"))
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT rr.target_id, g.id AS goal_id, w.assigned_owner,
                       w.source_write_scope_json, r.revision,
                       r.base_oid AS revision_base_oid, rs.slot_key,
                       rs.physical_seat_id, wsa.id AS worker_assignment_id,
                       lc.capability_key
                FROM repository_registrations AS rr
                JOIN runs AS run
                  ON run.id = ? AND run.target_id = rr.target_id
                JOIN goals AS g
                  ON g.id = run.goal_id AND g.target_id = rr.target_id
                JOIN work_items AS w
                  ON w.id = ? AND w.goal_id = g.id
                JOIN work_revisions AS r
                  ON r.work_item_id = w.id
                JOIN runtime_slots AS rs ON rs.id = ?
                JOIN worker_slot_assignments AS wsa
                  ON wsa.slot_id = rs.id AND wsa.run_id = run.id
                 AND wsa.state = 'ACTIVE'
                JOIN seat_capability_activations AS sca
                  ON sca.slot_id = rs.id
                 AND sca.worker_assignment_id = wsa.id
                 AND sca.goal_id = g.id AND sca.run_id = run.id
                 AND sca.state = 'ACTIVE'
                JOIN logical_capabilities AS lc
                  ON lc.id = sca.capability_id AND lc.state = 'ACTIVE'
                WHERE rr.id = ? AND rr.state = 'ACTIVE'
                  AND run.state = 'RUNNING' AND g.state = 'ACTIVE'
                  AND rs.kind = 'FIXED' AND rs.state <> 'RETIRED'
                  AND lc.capability_key = 'developer'
                  AND r.revision = (
                      SELECT MAX(r2.revision) FROM work_revisions AS r2
                      WHERE r2.work_item_id = w.id
                  )
                """,
                (run, work_item, slot, repository),
            ).fetchone()
        if row is None:
            raise WorkspaceConflictError(
                "v4 repository/run/developer-slot allocation graph is not active"
            )
        if row["revision_base_oid"] != base:
            raise GitCASMismatchError("revision base OID does not match allocation")
        if row["assigned_owner"] not in {row["slot_key"], row["physical_seat_id"]}:
            raise WorkspaceConflictError(
                "work item owner does not match the active developer slot"
            )
        scopes = tuple(json.loads(row["source_write_scope_json"]))
        return self.allocate_development(
            goal_id=row["goal_id"],
            work_item_id=work_item,
            revision=int(row["revision"]),
            owner_seat_id=row["assigned_owner"],
            target_id=row["target_id"],
            base_oid=base,
            source_write_scope=scopes,
            generated_write_scope=(),
            lease_seconds=3600,
            idempotency_key=(
                f"developer-workspace:{repository}:{run}:{work_item}:{slot}:{base}"
            ),
            run_id=run,
            slot_id=slot,
            repository_id=repository,
        )

    def release(
        self,
        lease_id: str,
        *,
        expected_owner: str,
    ) -> WorkspaceReleaseReceipt:
        lease_key = require_identifier(lease_id, "lease_id")
        owner = require_identifier(expected_owner, "expected_owner")
        lease = self._load_lease(lease_key)
        if lease.owner != owner:
            raise WorkspaceConflictError(
                f"lease {lease_key} belongs to {lease.owner}, not {owner}"
            )
        if lease.state is LeaseState.RELEASED:
            return self._release_receipt(lease)
        if lease.state is not LeaseState.ACTIVE:
            raise WorkspaceLeaseStateError(
                f"lease {lease_key} cannot release from {lease.state.value}"
            )
        status = WorkspaceGitFacade(self).status(lease_key)
        if not status.clean:
            raise WorkspaceDirtyError(
                f"lease {lease_key} has uncommitted paths and cannot be released"
            )
        git_receipt = self._allocation_git_receipt(lease)
        key = f"release-workspace:{lease.lease_id}:{lease.owner}"
        intent = self.state_store.begin_intent(
            operation="release-development-workspace",
            idempotency_key=key,
            expected_state="ACTIVE",
            expected_oid=lease.expected_head_oid,
            payload={
                "lease_id": lease.lease_id,
                "workspace_id": lease.workspace_id,
                "owner": lease.owner,
                "branch_ref": lease.branch_ref,
                "worktree_path": lease.worktree_path,
            },
        )
        if intent.status is IntentStatus.COMPLETED:
            current = self._load_lease(lease.lease_id)
            return self._release_receipt(current)
        self.repository_service.remove_disposable_worktree(
            git_receipt,
            expected_oid=lease.expected_head_oid,
        )
        released_at = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE workspace_leases
                SET state = 'RELEASED', released_at = ?
                WHERE id = ? AND state = 'ACTIVE' AND owner = ?
                """,
                (released_at, lease.lease_id, owner),
            ).rowcount
            if updated != 1:
                raise WorkspaceLeaseStateError(
                    f"lease {lease.lease_id} changed during release"
                )
            connection.execute(
                """
                UPDATE workspaces
                SET state = 'RELEASED', updated_at = ?
                WHERE id = ? AND state = 'ACTIVE'
                """,
                (released_at, lease.workspace_id),
            )
            connection.execute(
                """
                UPDATE runtime_leases
                SET state = 'RELEASED', released_at = ?
                WHERE id = ? AND state = 'ACTIVE'
                """,
                (released_at, lease.lease_id),
            )
        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="RELEASED",
            resulting_oid=lease.expected_head_oid,
            evidence={"released_at": released_at},
        )
        self._record_audit(
            event_type="DEVELOPMENT_WORKSPACE_RELEASED",
            subject_id=lease.lease_id,
            subject_oid=lease.expected_head_oid,
            idempotency_key=f"audit:{completed.intent_id}",
            goal_id=lease.goal_id,
            payload={"workspace_id": lease.workspace_id, "owner": owner},
        )
        return self._release_receipt(self._load_lease(lease.lease_id))

    def execution_contract(self, lease_id: str) -> WorkspaceExecutionContract:
        lease = self._require_active_lease(lease_id)
        root = Path(lease.worktree_path).resolve(strict=True)
        source_paths = tuple(
            self._scope_path(root, path) for path in lease.source_write_scope
        )
        generated_paths = tuple(
            self._scope_path(root, path) for path in lease.generated_write_scope
        )
        with self.state_store.transaction() as connection:
            target = connection.execute(
                """
                SELECT t.canonical_checkout_path, t.git_common_dir,
                       m.repository_path
                FROM targets AS t
                JOIN managed_repositories AS m ON m.target_id = t.id
                WHERE t.id = ?
                """,
                (lease.target_id,),
            ).fetchone()
            others = connection.execute(
                """
                SELECT worktree_path FROM workspace_leases
                WHERE state = 'ACTIVE' AND id <> ?
                ORDER BY worktree_path
                """,
                (lease.lease_id,),
            ).fetchall()
        if target is None:
            raise WorkspaceConflictError("lease target registry disappeared")
        prohibited = {
            Path(target["canonical_checkout_path"]).resolve(strict=False),
            Path(target["git_common_dir"]).resolve(strict=False),
            Path(target["repository_path"]).resolve(strict=False),
            *(Path(row["worktree_path"]).resolve(strict=False) for row in others),
        }
        return WorkspaceExecutionContract(
            lease_id=lease.lease_id,
            target_id=lease.target_id,
            goal_id=lease.goal_id,
            work_item_id=lease.work_item_id,
            revision=lease.revision,
            owner=lease.owner,
            branch_ref=lease.branch_ref,
            expected_head_oid=lease.expected_head_oid,
            cwd=root,
            source_write_paths=source_paths,
            generated_write_paths=generated_paths,
            prohibited_roots=tuple(sorted(prohibited, key=_path_key)),
        )

    def reconcile(self) -> tuple[ReconciliationFinding, ...]:
        findings: list[ReconciliationFinding] = []
        with self.state_store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT l.*, r.revision, m.repository_path
                FROM workspace_leases AS l
                JOIN work_revisions AS r ON r.id = l.revision_id
                JOIN managed_repositories AS m ON m.target_id = l.target_id
                WHERE l.state = 'ACTIVE'
                ORDER BY l.target_id, l.id
                """
            ).fetchall()
            repositories = connection.execute(
                """
                SELECT target_id, repository_path
                FROM managed_repositories
                WHERE state IN ('READY', 'RESYNC_REQUIRED', 'QUARANTINED')
                ORDER BY target_id
                """
            ).fetchall()
        by_target: dict[str, list[sqlite3.Row]] = {
            row["target_id"]: [] for row in repositories
        }
        for row in rows:
            by_target.setdefault(row["target_id"], []).append(row)
        repository_paths = {
            row["target_id"]: Path(row["repository_path"]).resolve()
            for row in repositories
        }
        now = self.clock().astimezone(UTC)
        for target_id, leases in by_target.items():
            repository = repository_paths.get(target_id)
            if repository is None or not repository.is_dir():
                continue
            entries = self.repository_service._worktree_entries(repository)
            leased_paths = {_path_key(row["worktree_path"]) for row in leases}
            for row in leases:
                observed: dict[str, Any] = {}
                expected = {
                    "branch_ref": row["branch_ref"],
                    "head_oid": row["expected_head_oid"],
                    "worktree_path": row["worktree_path"],
                }
                state = "QUARANTINED"
                reason: str | None = None
                if _parse_utc(row["expires_at"]) <= now:
                    reason = "active lease expired"
                    state = "EXPIRED"
                else:
                    entry = entries.get(_path_key(row["worktree_path"]))
                    if entry is None:
                        reason = "registered active worktree is missing"
                    else:
                        observed = {
                            "branch_ref": entry.get("branch"),
                            "head_oid": entry.get("HEAD"),
                            "worktree_path": entry.get("worktree"),
                        }
                        if (
                            entry.get("branch") != row["branch_ref"]
                            or entry.get("HEAD") != row["expected_head_oid"]
                        ):
                            reason = "Git worktree metadata differs from active lease"
                if reason is not None:
                    findings.append(
                        self._persist_finding(
                            resource_type="workspace-lease",
                            resource_id=row["id"],
                            severity=FindingSeverity.CRITICAL,
                            expected=expected,
                            observed={"reason": reason, **observed},
                        )
                    )
                    self._mark_lease_unusable(
                        row["id"], row["workspace_id"], state=state
                    )
            for key, entry in entries.items():
                entry_path = Path(str(entry.get("worktree", ""))).resolve(
                    strict=False
                )
                try:
                    relative = entry_path.relative_to(
                        self.path_authority.goals_root.resolve(strict=False)
                    )
                except ValueError:
                    continue
                parts = relative.parts
                is_development = (
                    len(parts) >= 4
                    and parts[1:3] == ("worktrees", "development")
                )
                if (
                    is_development
                    and key not in leased_paths
                ):
                    findings.append(
                        self._persist_finding(
                            resource_type="git-worktree",
                            resource_id=_stable_id("orphan", target_id, key),
                            severity=FindingSeverity.CRITICAL,
                            expected={"active_lease": True},
                            observed={
                                "active_lease": False,
                                "path": str(entry_path),
                                "branch_ref": entry.get("branch"),
                                "head_oid": entry.get("HEAD"),
                            },
                        )
                    )
        return tuple(findings)

    def _validate_allocation_graph(
        self,
        *,
        target_id: str,
        goal_id: str,
        work_item_id: str,
        revision: int,
        owner: str,
        base_oid: str,
        source_scope: tuple[str, ...],
    ) -> str:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT t.state AS target_state, g.state AS goal_state,
                       g.base_oid AS goal_base_oid,
                       w.assigned_owner, w.source_write_scope_json,
                       w.state AS work_item_state,
                       r.id AS revision_id, r.owner AS revision_owner,
                       r.base_oid AS revision_base_oid,
                       r.state AS revision_state
                FROM targets AS t
                JOIN goals AS g ON g.target_id = t.id
                JOIN work_items AS w ON w.goal_id = g.id
                JOIN work_revisions AS r ON r.work_item_id = w.id
                WHERE t.id = ? AND g.id = ? AND w.id = ? AND r.revision = ?
                """,
                (target_id, goal_id, work_item_id, revision),
            ).fetchone()
        if row is None:
            raise WorkspaceConflictError(
                "target/goal/work-item/revision allocation graph is missing"
            )
        if row["target_state"] != "ACTIVE" or row["goal_state"] != "ACTIVE":
            raise WorkspaceConflictError("target and goal must both be ACTIVE")
        if row["work_item_state"] not in {
            "ASSIGNED",
            "IN_PROGRESS",
            "REWORK_REQUIRED",
        }:
            raise WorkspaceConflictError(
                f"work item cannot allocate from {row['work_item_state']}"
            )
        if row["revision_state"] not in {"CREATED", "ACTIVE"}:
            raise WorkspaceConflictError(
                f"revision cannot allocate from {row['revision_state']}"
            )
        if row["assigned_owner"] != owner or row["revision_owner"] != owner:
            raise WorkspaceConflictError("PL assignment and revision owner must match")
        if row["revision_base_oid"] != base_oid:
            raise GitCASMismatchError("revision base OID does not match allocation")
        declared = tuple(
            _normalize_repo_path(item, "declared source scope")
            for item in json.loads(row["source_write_scope_json"])
        )
        if declared != source_scope:
            raise WorkspaceConflictError(
                "requested source scope differs from the PL-owned work item scope"
            )
        return str(row["revision_id"])

    def _persist_runtime_lease(
        self,
        lease: WorkspaceLease,
        *,
        repository_id: str,
        run_id: str,
        slot_id: str,
    ) -> None:
        repository = require_identifier(repository_id, "repository_id")
        run = require_identifier(run_id, "run_id")
        slot = require_identifier(slot_id, "slot_id")
        root = Path(lease.worktree_path).resolve(strict=False)
        with self.state_store.transaction(immediate=True) as connection:
            graph = connection.execute(
                """
                SELECT rr.canonical_path, rr.git_common_dir,
                       m.repository_path,
                       wsa.id AS worker_assignment_id
                FROM repository_registrations AS rr
                JOIN managed_repositories AS m
                  ON m.id = rr.managed_repository_id
                JOIN runs AS r
                  ON r.id = ? AND r.goal_id = ?
                 AND r.target_id = rr.target_id
                JOIN worker_slot_assignments AS wsa
                  ON wsa.run_id = r.id AND wsa.slot_id = ?
                 AND wsa.state = 'ACTIVE'
                WHERE rr.id = ? AND rr.state = 'ACTIVE'
                """,
                (run, lease.goal_id, slot, repository),
            ).fetchone()
            if graph is None:
                raise WorkspaceConflictError(
                    "v4 runtime lease ownership graph changed before persistence"
                )
            protected = tuple(
                sorted(
                    {
                        str(Path(graph["canonical_path"]).resolve(strict=False)),
                        str(Path(graph["git_common_dir"]).resolve(strict=False)),
                        str(Path(graph["repository_path"]).resolve(strict=False)),
                    },
                    key=lambda value: _path_key(value),
                )
            )
            signature = (
                repository,
                lease.goal_id,
                run,
                slot,
                graph["worker_assignment_id"],
                lease.branch_ref,
                _path_key(root),
                lease.base_oid,
                lease.expected_head_oid,
                compact_json((str(root),)),
                compact_json(protected),
            )
            existing = connection.execute(
                "SELECT * FROM runtime_leases WHERE id = ?",
                (lease.lease_id,),
            ).fetchone()
            if existing is None:
                try:
                    connection.execute(
                        """
                        INSERT INTO runtime_leases (
                            id, repository_id, goal_id, run_id, slot_id,
                            worker_assignment_id, lease_kind, branch_ref,
                            worktree_path, base_oid, expected_head_oid,
                            write_roots_json, protected_roots_json, state,
                            expires_at, idempotency_key, created_at, released_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'DEVELOPMENT', ?, ?, ?, ?,
                                  ?, ?, 'ACTIVE', ?, ?, ?, NULL)
                        """,
                        (
                            lease.lease_id,
                            *signature[:5],
                            *signature[5:],
                            lease.expires_at,
                            f"runtime-lease:{lease.lease_id}",
                            utc_now(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise WorkspaceConflictError(
                        "v4 runtime lease uniqueness rejected the allocation"
                    ) from exc
                return
            actual = (
                existing["repository_id"],
                existing["goal_id"],
                existing["run_id"],
                existing["slot_id"],
                existing["worker_assignment_id"],
                existing["branch_ref"],
                _path_key(existing["worktree_path"]),
                existing["base_oid"],
                existing["expected_head_oid"],
                existing["write_roots_json"],
                existing["protected_roots_json"],
            )
            if actual != signature or existing["state"] != "ACTIVE":
                raise WorkspaceConflictError(
                    f"runtime lease identity conflicts with {lease.lease_id}"
                )

    def _reserve_lease(
        self,
        *,
        lease_id: str,
        workspace_id: str,
        target_id: str,
        goal_id: str,
        work_item_id: str,
        revision_id: str,
        owner: str,
        branch_ref: str,
        worktree_path: Path,
        base_oid: str,
        source_scope: tuple[str, ...],
        generated_scope: tuple[str, ...],
        expires_at: str,
        idempotency_key: str,
    ) -> None:
        now = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM workspace_leases WHERE id = ?",
                (lease_id,),
            ).fetchone()
            if existing is not None:
                signature = (
                    existing["workspace_id"],
                    existing["target_id"],
                    existing["goal_id"],
                    existing["work_item_id"],
                    existing["revision_id"],
                    existing["owner"],
                    existing["branch_ref"],
                    _path_key(existing["worktree_path"]),
                    existing["base_oid"],
                    existing["idempotency_key"],
                )
                expected = (
                    workspace_id,
                    target_id,
                    goal_id,
                    work_item_id,
                    revision_id,
                    owner,
                    branch_ref,
                    _path_key(worktree_path),
                    base_oid,
                    idempotency_key,
                )
                if signature != expected or existing["state"] != "ACTIVE":
                    raise WorkspaceConflictError(
                        f"lease reservation conflicts with {lease_id}"
                    )
                return
            competing = connection.execute(
                """
                SELECT id FROM workspace_leases
                WHERE state = 'ACTIVE'
                  AND (
                    (target_id = ? AND branch_ref = ?)
                    OR worktree_path = ?
                    OR workspace_id = ?
                  )
                """,
                (target_id, branch_ref, str(worktree_path), workspace_id),
            ).fetchone()
            if competing is not None:
                raise WorkspaceConflictError(
                    f"active writer already owns requested branch/worktree: "
                    f"{competing['id']}"
                )
            workspace = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                connection.execute(
                    """
                    INSERT INTO workspaces (
                        id, target_id, goal_id, kind, path, branch_ref,
                        subject_oid, state, created_at, updated_at
                    ) VALUES (?, ?, ?, 'DEVELOPMENT', ?, ?, ?,
                              'PROVISIONING', ?, ?)
                    """,
                    (
                        workspace_id,
                        target_id,
                        goal_id,
                        str(worktree_path),
                        branch_ref,
                        base_oid,
                        now,
                        now,
                    ),
                )
            elif (
                workspace["state"] != "PROVISIONING"
                or workspace["target_id"] != target_id
                or workspace["goal_id"] != goal_id
                or _path_key(workspace["path"]) != _path_key(worktree_path)
            ):
                raise WorkspaceConflictError(
                    f"workspace reservation conflicts with {workspace_id}"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO workspace_leases (
                        id, workspace_id, target_id, goal_id, work_item_id,
                        revision_id, owner, branch_ref, worktree_path,
                        base_oid, expected_head_oid, source_write_scope_json,
                        generated_write_scope_json, state, expires_at,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'ACTIVE', ?, ?, ?)
                    """,
                    (
                        lease_id,
                        workspace_id,
                        target_id,
                        goal_id,
                        work_item_id,
                        revision_id,
                        owner,
                        branch_ref,
                        str(worktree_path),
                        base_oid,
                        base_oid,
                        compact_json(source_scope),
                        compact_json(generated_scope),
                        expires_at,
                        idempotency_key,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkspaceConflictError(
                    "Phase 2 active-writer uniqueness rejected the lease"
                ) from exc

    def _activate_reservation(
        self,
        *,
        lease_id: str,
        workspace_id: str,
        revision_id: str,
        work_item_id: str,
    ) -> None:
        now = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE workspaces SET state = 'ACTIVE', updated_at = ?
                WHERE id = ? AND state = 'PROVISIONING'
                """,
                (now, workspace_id),
            ).rowcount
            if updated != 1:
                raise WorkspaceConflictError(
                    f"workspace reservation changed before activation: {workspace_id}"
                )
            connection.execute(
                """
                UPDATE work_revisions SET state = 'ACTIVE', updated_at = ?
                WHERE id = ? AND state = 'CREATED'
                """,
                (now, revision_id),
            )
            connection.execute(
                """
                UPDATE work_items SET state = 'IN_PROGRESS', updated_at = ?
                WHERE id = ? AND state IN ('ASSIGNED', 'REWORK_REQUIRED')
                """,
                (now, work_item_id),
            )
            active = connection.execute(
                "SELECT 1 FROM workspace_leases WHERE id = ? AND state = 'ACTIVE'",
                (lease_id,),
            ).fetchone()
            if active is None:
                raise WorkspaceConflictError("active lease reservation disappeared")

    def _quarantine_reservation(
        self,
        lease_id: str,
        workspace_id: str,
        *,
        reason: str,
    ) -> None:
        now = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE workspace_leases SET state = 'QUARANTINED'
                WHERE id = ? AND state = 'ACTIVE'
                """,
                (lease_id,),
            )
            connection.execute(
                """
                UPDATE workspaces SET state = 'QUARANTINED', updated_at = ?
                WHERE id = ? AND state IN ('PROVISIONING', 'ACTIVE')
                """,
                (now, workspace_id),
            )
        self._persist_finding(
            resource_type="workspace-lease",
            resource_id=lease_id,
            severity=FindingSeverity.CRITICAL,
            expected={"state": "ACTIVE"},
            observed={"state": "QUARANTINED", "reason": reason},
        )

    def _assert_no_unleased_git_conflict(
        self,
        *,
        target_id: str,
        branch_ref: str,
        worktree_path: Path,
        expected_oid: str,
        lease_id: str,
    ) -> None:
        registration = self.repository_service._load_target(target_id)
        repository = Path(registration.managed_repository_path).resolve()
        entries = self.repository_service._worktree_entries(repository)
        by_path = entries.get(_path_key(worktree_path))
        by_branch = [
            entry for entry in entries.values() if entry.get("branch") == branch_ref
        ]
        existing = self._load_lease(lease_id, allow_missing=True)
        if by_path is None and not by_branch:
            return
        if (
            existing is not None
            and existing.state is LeaseState.ACTIVE
            and by_path is not None
            and len(by_branch) == 1
            and by_path.get("branch") == branch_ref
            and by_path.get("HEAD") == expected_oid
        ):
            return
        raise WorkspaceConflictError(
            "Git worktree registry already owns the requested branch or path"
        )

    def _assert_lease_materialized(self, lease: WorkspaceLease) -> None:
        if lease.state is not LeaseState.ACTIVE:
            raise WorkspaceLeaseStateError(
                f"replayed lease is not active: {lease.state.value}"
            )
        registration = self.repository_service._load_target(lease.target_id)
        entries = self.repository_service._worktree_entries(
            Path(registration.managed_repository_path).resolve()
        )
        entry = entries.get(_path_key(lease.worktree_path))
        if (
            entry is None
            or entry.get("branch") != lease.branch_ref
            or entry.get("HEAD") != lease.expected_head_oid
        ):
            raise GitCASMismatchError(
                "active lease does not match the actual Git worktree registry"
            )

    def _require_active_lease(self, lease_id: str) -> WorkspaceLease:
        lease = self._load_lease(require_identifier(lease_id, "lease_id"))
        if lease.state is not LeaseState.ACTIVE:
            raise WorkspaceLeaseStateError(
                f"lease {lease.lease_id} is {lease.state.value}"
            )
        if _parse_utc(lease.expires_at) <= self.clock().astimezone(UTC):
            raise WorkspaceLeaseStateError(f"lease {lease.lease_id} is expired")
        self._assert_lease_materialized(lease)
        return lease

    def _load_lease(
        self,
        lease_id: str,
        *,
        allow_missing: bool = False,
    ) -> WorkspaceLease | None:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT l.*, r.revision
                FROM workspace_leases AS l
                JOIN work_revisions AS r ON r.id = l.revision_id
                WHERE l.id = ?
                """,
                (lease_id,),
            ).fetchone()
        if row is None:
            if allow_missing:
                return None
            raise WorkspaceNotFoundError(lease_id)
        return WorkspaceLease(
            lease_id=row["id"],
            workspace_id=row["workspace_id"],
            target_id=row["target_id"],
            goal_id=row["goal_id"],
            work_item_id=row["work_item_id"],
            revision_id=row["revision_id"],
            revision=row["revision"],
            owner=row["owner"],
            branch_ref=row["branch_ref"],
            worktree_path=row["worktree_path"],
            base_oid=row["base_oid"],
            expected_head_oid=row["expected_head_oid"],
            source_write_scope=tuple(json.loads(row["source_write_scope_json"])),
            generated_write_scope=tuple(
                json.loads(row["generated_write_scope_json"])
            ),
            expires_at=row["expires_at"],
            state=LeaseState(row["state"]),
        )

    def _lease_from_completed_intent(self, intent: Any) -> WorkspaceLease:
        evidence = intent.evidence.get("lease")
        if not isinstance(evidence, Mapping):
            raise WorkspaceConflictError(
                f"completed allocation intent lacks lease receipt: {intent.intent_id}"
            )
        data = thaw_json(evidence)
        return WorkspaceLease(**data)

    def _allocation_git_receipt(self, lease: WorkspaceLease) -> GitWorktreeReceipt:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT i.status, i.evidence_json
                FROM workspace_leases AS l
                JOIN operation_intents AS i
                  ON i.idempotency_key = l.idempotency_key
                WHERE l.id = ?
                  AND i.operation = 'allocate-development-workspace'
                """,
                (lease.lease_id,),
            ).fetchone()
        if row is None or row["status"] != "COMPLETED":
            raise WorkspaceLeaseStateError(
                f"lease has no completed allocation receipt: {lease.lease_id}"
            )
        receipt = json.loads(row["evidence_json"]).get("git_worktree_receipt")
        if not isinstance(receipt, Mapping):
            raise WorkspaceLeaseStateError("allocation Git receipt is missing")
        return GitWorktreeReceipt(**receipt)

    @staticmethod
    def _scope_path(root: Path, relative: str) -> Path:
        parts = PurePosixPath(relative).parts
        candidate = root.joinpath(*parts)
        resolved = candidate.resolve(strict=False)
        if not _within(root, resolved):
            raise WorkspaceValidationError(
                f"write scope escapes its assigned worktree: {relative}"
            )
        return resolved

    def _release_receipt(self, lease: WorkspaceLease) -> WorkspaceReleaseReceipt:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                "SELECT released_at FROM workspace_leases WHERE id = ?",
                (lease.lease_id,),
            ).fetchone()
        released_at = (
            row["released_at"] if row is not None and row["released_at"] else utc_now()
        )
        return WorkspaceReleaseReceipt(
            lease_id=lease.lease_id,
            workspace_id=lease.workspace_id,
            owner=lease.owner,
            branch_ref=lease.branch_ref,
            final_oid=lease.expected_head_oid,
            worktree_path=Path(lease.worktree_path).resolve(strict=False),
            released_at=released_at,
        )

    def _persist_finding(
        self,
        *,
        resource_type: str,
        resource_id: str,
        severity: FindingSeverity,
        expected: Mapping[str, Any],
        observed: Mapping[str, Any],
    ) -> ReconciliationFinding:
        signature = compact_json(
            {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "expected": expected,
                "observed": observed,
            }
        )
        finding_id = _stable_id("finding", signature)
        idempotency_key = f"reconcile:{finding_id}"
        detected_at = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO reconciliation_findings (
                    id, resource_type, resource_id, severity, state,
                    expected_json, observed_json, idempotency_key, detected_at
                ) VALUES (?, ?, ?, ?, 'QUARANTINED', ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    finding_id,
                    resource_type,
                    resource_id,
                    severity.value,
                    compact_json(expected),
                    compact_json(observed),
                    idempotency_key,
                    detected_at,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM reconciliation_findings
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        return ReconciliationFinding(
            finding_id=row["id"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            severity=FindingSeverity(row["severity"]),
            state=FindingState(row["state"]),
            expected=json.loads(row["expected_json"]),
            observed=json.loads(row["observed_json"]),
            detected_at=row["detected_at"],
            idempotency_key=row["idempotency_key"],
        )

    def _mark_lease_unusable(
        self,
        lease_id: str,
        workspace_id: str,
        *,
        state: str,
    ) -> None:
        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE workspace_leases SET state = ? WHERE id = ? AND state = 'ACTIVE'",
                (state, lease_id),
            )
            connection.execute(
                """
                UPDATE workspaces SET state = 'QUARANTINED', updated_at = ?
                WHERE id = ? AND state IN ('PROVISIONING', 'ACTIVE')
                """,
                (utc_now(), workspace_id),
            )

    def _record_audit(
        self,
        *,
        event_type: str,
        subject_id: str,
        subject_oid: str,
        idempotency_key: str,
        goal_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=_stable_id("audit", idempotency_key),
                event_type=event_type,
                actor="service:workspace-manager",
                subject_type="workspace-lease",
                subject_id=subject_id,
                payload=payload,
                occurred_at=utc_now(),
                idempotency_key=idempotency_key,
                goal_id=goal_id,
                subject_oid=subject_oid,
            )
        )


class WorkspaceGitFacade:
    """The only consumer-facing mutation seam for leased developer Git."""

    def __init__(self, manager: WorkspaceManager) -> None:
        if not isinstance(manager, WorkspaceManager):
            raise TypeError("manager must be a WorkspaceManager")
        self.manager = manager
        self.state_store = manager.state_store
        self.repository_service = manager.repository_service

    def status(self, lease_id: str) -> WorkspaceStatus:
        lease = self.manager._require_active_lease(lease_id)
        root = Path(lease.worktree_path).resolve(strict=True)
        branch, head = self._branch_and_head(root)
        if branch != lease.branch_ref:
            raise WorkspaceLeaseStateError(
                f"worktree branch is {branch}, expected {lease.branch_ref}"
            )
        if head != lease.expected_head_oid:
            raise GitCASMismatchError(
                f"worktree HEAD is {head}, expected {lease.expected_head_oid}"
            )
        staged = self._path_output(
            root, ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACDMRTUXB"]
        )
        unstaged = self._path_output(
            root, ["diff", "--name-only", "-z", "--diff-filter=ACDMRTUXB"]
        )
        untracked = self._path_output(
            root, ["ls-files", "--others", "--exclude-standard", "-z"]
        )
        changed = tuple(sorted(set((*staged, *unstaged, *untracked))))
        out_of_scope = tuple(
            path for path in changed if not self._path_is_authorized(lease, root, path)
        )
        return WorkspaceStatus(
            lease_id=lease.lease_id,
            branch_ref=branch,
            head_oid=head,
            changed_paths=changed,
            staged_paths=staged,
            unstaged_paths=unstaged,
            untracked_paths=untracked,
            out_of_scope_paths=out_of_scope,
            clean=not changed,
        )

    def checkpoint(
        self,
        lease_id: str,
        *,
        expected_head_oid: str,
        message: str,
        idempotency_key: str,
    ) -> CandidateSubmission:
        lease = self.manager._require_active_lease(lease_id)
        expected = str(require_oid(expected_head_oid, "expected_head_oid"))
        commit_message = self._safe_message(message)
        key = require_nonempty(idempotency_key, "idempotency_key")
        payload = {
            "lease_id": lease.lease_id,
            "branch_ref": lease.branch_ref,
            "expected_head_oid": expected,
            "message": commit_message,
        }
        intent = self.state_store.begin_intent(
            operation="workspace-checkpoint",
            idempotency_key=key,
            expected_state="ACTIVE",
            expected_oid=expected,
            payload=payload,
        )
        if intent.status is IntentStatus.COMPLETED:
            return self._submission_from_intent(intent)
        if lease.expected_head_oid != expected:
            raise GitCASMismatchError(
                f"lease expects {lease.expected_head_oid}, not {expected}"
            )
        root = Path(lease.worktree_path).resolve(strict=True)
        branch, actual = self._branch_and_head(root)
        if branch != lease.branch_ref or actual != expected:
            raise GitCASMismatchError(
                "branch or HEAD changed before scoped checkpoint"
            )
        status = self.status(lease.lease_id)
        if status.out_of_scope_paths:
            raise WorkspaceScopeError(status.out_of_scope_paths)
        if not status.changed_paths:
            raise WorkspaceCheckpointError("checkpoint requires at least one changed path")
        self.repository_service.command_runner.run(
            ["--literal-pathspecs", "add", "-A", "--", *status.changed_paths],
            cwd=root,
        )
        branch_immediate, head_immediate = self._branch_and_head(root)
        if branch_immediate != lease.branch_ref or head_immediate != expected:
            raise GitCASMismatchError(
                "branch or HEAD changed immediately before commit"
            )
        self.repository_service.command_runner.run(
            [
                "-c",
                f"user.name={_COMMITTER_NAME}",
                "-c",
                f"user.email={_COMMITTER_EMAIL}",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--no-verify",
                "-m",
                commit_message,
            ],
            cwd=root,
        )
        branch_after, candidate_oid = self._branch_and_head(root)
        if branch_after != lease.branch_ref or candidate_oid == expected:
            raise WorkspaceCheckpointError(
                "Git did not create the expected scoped checkpoint"
            )
        parent = self._first_parent(root, candidate_oid)
        if parent != expected:
            raise GitCASMismatchError(
                f"checkpoint parent is {parent}, expected {expected}"
            )
        changed_paths = self._diff_paths(root, expected, candidate_oid)
        self._require_authorized_paths(lease, root, changed_paths)
        with self.state_store.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE workspace_leases SET expected_head_oid = ?
                WHERE id = ? AND state = 'ACTIVE' AND expected_head_oid = ?
                """,
                (candidate_oid, lease.lease_id, expected),
            ).rowcount
            if updated != 1:
                raise GitCASMismatchError(
                    "lease head CAS failed after Git checkpoint; reconciliation required"
                )
            connection.execute(
                """
                UPDATE work_revisions SET head_oid = ?, updated_at = ?
                WHERE id = ? AND state = 'ACTIVE'
                """,
                (candidate_oid, utc_now(), lease.revision_id),
            )
        receipt = CandidateSubmission(
            candidate_id=_stable_id("checkpoint", key),
            goal_id=lease.goal_id,
            work_item_id=lease.work_item_id,
            revision_id=lease.revision_id,
            revision=lease.revision,
            lease_id=lease.lease_id,
            branch_ref=lease.branch_ref,
            expected_previous_oid=expected,
            candidate_oid=candidate_oid,
            self_test_evidence=(),
            idempotency_key=key,
            state=CandidateState.SUBMITTED,
        )
        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="CHECKPOINTED",
            resulting_oid=candidate_oid,
            evidence={
                "submission": asdict(receipt),
                "changed_paths": list(changed_paths),
                "owner": lease.owner,
            },
        )
        self.manager._record_audit(
            event_type="WORKSPACE_CHECKPOINT_CREATED",
            subject_id=lease.lease_id,
            subject_oid=candidate_oid,
            idempotency_key=f"audit:{completed.intent_id}",
            goal_id=lease.goal_id,
            payload={
                "branch_ref": lease.branch_ref,
                "parent_oid": expected,
                "changed_paths": list(changed_paths),
            },
        )
        return receipt

    def submit_candidate(
        self,
        lease_id: str,
        *,
        expected_head_oid: str,
        evidence_ids: Sequence[str],
        idempotency_key: str,
    ) -> CandidateSubmission:
        lease = self.manager._require_active_lease(lease_id)
        expected = str(require_oid(expected_head_oid, "expected_head_oid"))
        evidence = tuple(
            require_identifier(item, "evidence_id") for item in evidence_ids
        )
        if len(evidence) != len(set(evidence)):
            raise WorkspaceValidationError("evidence_ids must not contain duplicates")
        key = require_nonempty(idempotency_key, "idempotency_key")
        payload = {
            "lease_id": lease.lease_id,
            "expected_head_oid": expected,
            "evidence_ids": list(evidence),
        }
        intent = self.state_store.begin_intent(
            operation="submit-workspace-candidate",
            idempotency_key=key,
            expected_state="ACTIVE",
            expected_oid=expected,
            payload=payload,
        )
        if intent.status is IntentStatus.COMPLETED:
            return self._submission_from_intent(intent)
        if lease.expected_head_oid != expected:
            raise GitCASMismatchError(
                f"lease expects {lease.expected_head_oid}, not {expected}"
            )
        root = Path(lease.worktree_path).resolve(strict=True)
        status = self.status(lease.lease_id)
        if not status.clean:
            if status.out_of_scope_paths:
                raise WorkspaceScopeError(status.out_of_scope_paths)
            raise WorkspaceDirtyError(
                "candidate submission requires a clean checkpointed worktree"
            )
        branch, candidate_oid = self._branch_and_head(root)
        if branch != lease.branch_ref or candidate_oid != expected:
            raise GitCASMismatchError(
                "branch or HEAD changed before candidate submission"
            )
        if candidate_oid == lease.base_oid:
            raise WorkspaceCheckpointError(
                "candidate must differ from the allocated base OID"
            )
        parent_oid = self._first_parent(root, candidate_oid)
        changed_paths = self._diff_paths(root, lease.base_oid, candidate_oid)
        if not changed_paths:
            raise WorkspaceCheckpointError("candidate contains no changed paths")
        self._require_authorized_paths(lease, root, changed_paths)
        registration = self.repository_service._load_target(lease.target_id)
        managed = Path(registration.managed_repository_path).resolve()
        branch_immediately = self.repository_service._read_direct_ref(
            managed,
            lease.branch_ref,
            require_commit=True,
        )
        if branch_immediately != candidate_oid:
            raise GitCASMismatchError(
                "managed branch changed immediately before candidate pinning"
            )
        evidence_ref = (
            f"refs/agentic-ax/candidates/{_safe_ref_component(lease.goal_id, 'goal_id')}/"
            f"{_safe_ref_component(lease.work_item_id, 'work_item_id')}/"
            f"{lease.revision}"
        )
        self.repository_service._ensure_immutable_ref(
            managed, evidence_ref, candidate_oid
        )
        candidate_id = _stable_id(
            "candidate",
            lease.goal_id,
            lease.work_item_id,
            str(lease.revision),
            candidate_oid,
        )
        submission = CandidateSubmission(
            candidate_id=candidate_id,
            goal_id=lease.goal_id,
            work_item_id=lease.work_item_id,
            revision_id=lease.revision_id,
            revision=lease.revision,
            lease_id=lease.lease_id,
            branch_ref=lease.branch_ref,
            expected_previous_oid=parent_oid,
            candidate_oid=candidate_oid,
            self_test_evidence=evidence,
            idempotency_key=key,
            state=CandidateState.SUBMITTED,
        )
        created_at = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM candidate_submissions
                WHERE id = ? OR idempotency_key = ?
                """,
                (candidate_id, key),
            ).fetchone()
            expected_signature = (
                submission.candidate_id,
                submission.goal_id,
                submission.work_item_id,
                submission.revision_id,
                submission.lease_id,
                submission.branch_ref,
                submission.expected_previous_oid,
                submission.candidate_oid,
                compact_json(submission.self_test_evidence),
                submission.idempotency_key,
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO candidate_submissions (
                        id, goal_id, work_item_id, revision_id, lease_id,
                        branch_ref, expected_previous_oid, candidate_oid,
                        self_test_evidence_json, state, idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUBMITTED', ?, ?)
                    """,
                    (*expected_signature, created_at),
                )
            else:
                actual_signature = (
                    existing["id"],
                    existing["goal_id"],
                    existing["work_item_id"],
                    existing["revision_id"],
                    existing["lease_id"],
                    existing["branch_ref"],
                    existing["expected_previous_oid"],
                    existing["candidate_oid"],
                    existing["self_test_evidence_json"],
                    existing["idempotency_key"],
                )
                if actual_signature != expected_signature:
                    raise IdempotencyConflictError(
                        "candidate identity or idempotency key was reused"
                    )
            connection.execute(
                """
                UPDATE work_revisions
                SET head_oid = ?, state = 'SUBMITTED', updated_at = ?
                WHERE id = ? AND state IN ('ACTIVE', 'SUBMITTED')
                """,
                (candidate_oid, created_at, lease.revision_id),
            )
            connection.execute(
                """
                UPDATE work_items SET state = 'REVIEW_PENDING', updated_at = ?
                WHERE id = ? AND state IN ('IN_PROGRESS', 'REVIEW_PENDING')
                """,
                (created_at, lease.work_item_id),
            )
        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="SUBMITTED",
            resulting_oid=candidate_oid,
            evidence={
                "submission": asdict(submission),
                "base_oid": lease.base_oid,
                "parent_oid": parent_oid,
                "owner": lease.owner,
                "changed_paths": list(changed_paths),
                "evidence_ref": evidence_ref,
            },
        )
        self.manager._record_audit(
            event_type="CANDIDATE_SUBMITTED",
            subject_id=submission.candidate_id,
            subject_oid=candidate_oid,
            idempotency_key=f"audit:{completed.intent_id}",
            goal_id=lease.goal_id,
            payload={
                "lease_id": lease.lease_id,
                "revision_id": lease.revision_id,
                "owner": lease.owner,
                "base_oid": lease.base_oid,
                "parent_oid": parent_oid,
                "changed_paths": list(changed_paths),
                "evidence_ids": list(evidence),
                "evidence_ref": evidence_ref,
            },
        )
        return submission

    def _branch_and_head(self, root: Path) -> tuple[str, str]:
        branch_result = self.repository_service.command_runner.run(
            ["symbolic-ref", "--quiet", "HEAD"], cwd=root, check=False
        )
        if branch_result.returncode != 0:
            raise WorkspaceLeaseStateError("development worktree HEAD is detached")
        branch = require_git_ref(branch_result.stdout.strip(), "branch_ref")
        head_result = self.repository_service.command_runner.run(
            ["rev-parse", "--verify", "HEAD"], cwd=root
        )
        head = str(require_oid(head_result.stdout.strip(), "head_oid"))
        return branch, head

    def _path_output(
        self,
        root: Path,
        arguments: Sequence[str],
    ) -> tuple[str, ...]:
        result = self.repository_service.command_runner.run(arguments, cwd=root)
        values = []
        for raw in result.stdout.split("\x00"):
            if not raw:
                continue
            values.append(_normalize_repo_path(raw, "Git changed path"))
        return tuple(sorted(set(values)))

    def _diff_paths(
        self,
        root: Path,
        old_oid: str,
        new_oid: str,
    ) -> tuple[str, ...]:
        return self._path_output(
            root,
            [
                "diff",
                "--name-only",
                "-z",
                "--diff-filter=ACDMRTUXB",
                old_oid,
                new_oid,
                "--",
            ],
        )

    def _path_is_authorized(
        self,
        lease: WorkspaceLease,
        root: Path,
        relative: str,
    ) -> bool:
        path = _normalize_repo_path(relative, "changed path")
        if not any(
            _scope_contains(scope, path)
            for scope in (*lease.source_write_scope, *lease.generated_write_scope)
        ):
            return False
        candidate = root.joinpath(*PurePosixPath(path).parts)
        resolved = candidate.resolve(strict=False)
        if not _within(root, resolved):
            return False
        return True

    def _require_authorized_paths(
        self,
        lease: WorkspaceLease,
        root: Path,
        paths: Sequence[str],
    ) -> None:
        rejected = tuple(
            path for path in paths if not self._path_is_authorized(lease, root, path)
        )
        if rejected:
            raise WorkspaceScopeError(rejected)

    def _first_parent(self, root: Path, oid: str) -> str:
        result = self.repository_service.command_runner.run(
            ["rev-list", "--parents", "-n", "1", oid], cwd=root
        )
        parts = result.stdout.strip().split()
        if len(parts) != 2:
            raise WorkspaceCheckpointError(
                "workspace checkpoints/candidates must be single-parent commits"
            )
        return str(require_oid(parts[1], "parent_oid"))

    @staticmethod
    def _safe_message(value: str) -> str:
        message = require_nonempty(value, "message")
        if (
            any(character in message for character in ("\x00", "\r", "\n"))
            or len(message) > 512
        ):
            raise WorkspaceValidationError(
                "checkpoint message must be one safe line of at most 512 characters"
            )
        return message

    @staticmethod
    def _submission_from_intent(intent: Any) -> CandidateSubmission:
        receipt = intent.evidence.get("submission")
        if not isinstance(receipt, Mapping):
            raise WorkspaceCheckpointError(
                f"completed operation lacks immutable submission receipt: "
                f"{intent.intent_id}"
            )
        data = thaw_json(receipt)
        return CandidateSubmission(**data)
