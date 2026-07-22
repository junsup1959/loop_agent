from __future__ import annotations

"""Crash reconciliation across SQLite journals and actual Git state.

Reconciliation is evidence driven.  A pending journal is never treated as
success by itself: the reconciler inspects refs, objects, and registered
worktrees, then classifies the boundary as retry-safe, completed but
unrecorded, quarantined, or requiring human/PL action.
"""

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

try:
    from .agent_team_domain import (
        AuditEvent,
        FindingSeverity,
        FindingState,
        IntegrationAttemptState,
        ReconciliationFinding,
        ServiceIdentity,
        require_identifier,
        require_nonempty,
        require_oid,
    )
    from .agent_team_git import (
        GitRefUpdateReceipt,
        GitWorktreeReceipt,
        ManagedRepositoryService,
        TargetRefAdapter,
    )
    from .agent_team_integration import IntegrationController
    from .agent_team_paths import AxPathAuthority
    from .agent_team_state import (
        AxStateStore,
        IdempotencyConflictError,
        compact_json,
        utc_now,
    )
except ImportError:  # Direct script/module execution from the repository root.
    from agent_team_domain import (
        AuditEvent,
        FindingSeverity,
        FindingState,
        IntegrationAttemptState,
        ReconciliationFinding,
        ServiceIdentity,
        require_identifier,
        require_nonempty,
        require_oid,
    )
    from agent_team_git import (
        GitRefUpdateReceipt,
        GitWorktreeReceipt,
        ManagedRepositoryService,
        TargetRefAdapter,
    )
    from agent_team_integration import IntegrationController
    from agent_team_paths import AxPathAuthority
    from agent_team_state import (
        AxStateStore,
        IdempotencyConflictError,
        compact_json,
        utc_now,
    )


class ReconciliationClassification(str, Enum):
    RETRY_SAFE = "RETRY_SAFE"
    COMPLETED_BUT_UNRECORDED = "COMPLETED_BUT_UNRECORDED"
    QUARANTINED = "QUARANTINED"
    HUMAN_OR_PL_ACTION_REQUIRED = "HUMAN_OR_PL_ACTION_REQUIRED"


class RecoveryReconcilerError(RuntimeError):
    """Base error for startup reconciliation."""


class ReconciliationEvidenceError(RecoveryReconcilerError):
    """Stored journal/finding evidence is malformed or internally inconsistent."""


class ReconciliationConflictError(RecoveryReconcilerError):
    """Observed state changed while a reconciliation action was applied."""


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    finding_id: str
    resource_type: str
    resource_id: str
    classification: ReconciliationClassification
    action: str
    state: FindingState
    resulting_oid: str | None
    audit_event_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "finding_id", require_identifier(self.finding_id, "finding_id")
        )
        for field in ("resource_type", "resource_id", "action"):
            object.__setattr__(
                self, field, require_nonempty(getattr(self, field), field)
            )
        if not isinstance(self.classification, ReconciliationClassification):
            object.__setattr__(
                self,
                "classification",
                ReconciliationClassification(self.classification),
            )
        if not isinstance(self.state, FindingState):
            object.__setattr__(self, "state", FindingState(self.state))
        object.__setattr__(
            self,
            "resulting_oid",
            require_oid(self.resulting_oid, "resulting_oid", optional=True),
        )
        object.__setattr__(
            self,
            "audit_event_id",
            require_identifier(self.audit_event_id, "audit_event_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            require_nonempty(self.idempotency_key, "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class _Inspection:
    resource_type: str
    resource_id: str
    classification: ReconciliationClassification
    severity: FindingSeverity
    expected: Mapping[str, Any]
    observed: Mapping[str, Any]


def _stable_identifier(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _json_object(raw: str | None, field: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReconciliationEvidenceError(f"{field} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ReconciliationEvidenceError(f"{field} must be a JSON object")
    return parsed


class RecoveryReconciler:
    """Scan and reconcile incomplete controller boundaries idempotently."""

    def __init__(
        self,
        *,
        state_store: AxStateStore,
        repository_service: ManagedRepositoryService,
        path_authority: AxPathAuthority,
        integration_controller: IntegrationController | None = None,
        target_ref_adapter: TargetRefAdapter | None = None,
    ) -> None:
        if not isinstance(state_store, AxStateStore):
            raise TypeError("state_store must be an AxStateStore")
        if not isinstance(repository_service, ManagedRepositoryService):
            raise TypeError(
                "repository_service must be a ManagedRepositoryService"
            )
        if not isinstance(path_authority, AxPathAuthority):
            raise TypeError("path_authority must be an AxPathAuthority")
        if repository_service.state_store is not state_store:
            raise ValueError("repository service and reconciler must share state_store")
        if repository_service.path_authority != path_authority:
            raise ValueError(
                "repository service and reconciler must share path authority"
            )
        if (
            integration_controller is not None
            and integration_controller.state_store is not state_store
        ):
            raise ValueError(
                "integration controller and reconciler must share state_store"
            )
        if (
            target_ref_adapter is not None
            and target_ref_adapter.state_store is not state_store
        ):
            raise ValueError(
                "target ref adapter and reconciler must share state_store"
            )
        self.state_store = state_store
        self.repository_service = repository_service
        self.path_authority = path_authority
        self.integration_controller = integration_controller
        self.target_ref_adapter = target_ref_adapter
        self.state_store.initialize()

    def scan(self) -> tuple[ReconciliationFinding, ...]:
        """Inspect actual state and persist open findings without applying effects."""

        inspections: list[_Inspection] = []
        with self.state_store.transaction() as connection:
            pending = connection.execute(
                """
                SELECT * FROM operation_intents
                WHERE status = 'PENDING'
                ORDER BY created_at, id
                """
            ).fetchall()
            transitional_attempts = connection.execute(
                """
                SELECT * FROM integration_attempts
                WHERE state IN (
                    'PLANNED', 'PREFLIGHTING', 'MERGING', 'INTERRUPTED',
                    'EVIDENCE_PERSISTED', 'RECREATED'
                )
                ORDER BY created_at, id
                """
            ).fetchall()
            transitional_promotions = connection.execute(
                """
                SELECT * FROM promotions
                WHERE state IN ('REQUESTED', 'VALIDATING')
                ORDER BY created_at, id
                """
            ).fetchall()

        for intent in pending:
            inspection = self._inspect_intent(intent)
            if inspection is not None:
                inspections.append(inspection)

        pending_attempt_ids = set()
        for intent in pending:
            payload = _json_object(intent["payload_json"], "intent payload")
            attempt_id = payload.get("attempt_id")
            if isinstance(attempt_id, str):
                pending_attempt_ids.add(attempt_id)
        for attempt in transitional_attempts:
            if attempt["id"] not in pending_attempt_ids:
                inspections.append(self._inspect_attempt(attempt))

        pending_promotion_ids = set()
        for intent in pending:
            payload = _json_object(intent["payload_json"], "intent payload")
            promotion_id = payload.get("promotion_id")
            if isinstance(promotion_id, str):
                pending_promotion_ids.add(promotion_id)
        for promotion in transitional_promotions:
            if promotion["id"] not in pending_promotion_ids:
                inspections.append(self._inspect_promotion(promotion))

        findings = []
        for inspection in inspections:
            finding = self._persist_inspection(inspection)
            if finding.state in {FindingState.OPEN, FindingState.RECONCILING}:
                findings.append(finding)
        return tuple(findings)

    def reconcile(self, finding_id: str) -> ReconciliationResult:
        """Apply one bounded, classification-specific reconciliation action."""

        finding_id = require_identifier(finding_id, "finding_id")
        with self.state_store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_findings WHERE id = ?",
                (finding_id,),
            ).fetchone()
            if row is None:
                raise KeyError(finding_id)
            if row["state"] in {"RESOLVED", "QUARANTINED"}:
                return self._result_from_resolution(row)
            updated = connection.execute(
                """
                UPDATE reconciliation_findings
                SET state = 'RECONCILING'
                WHERE id = ? AND state = 'OPEN'
                """,
                (finding_id,),
            ).rowcount
            if updated != 1 and row["state"] != "RECONCILING":
                raise ReconciliationConflictError(
                    f"finding cannot reconcile from {row['state']}"
                )

        expected = _json_object(row["expected_json"], "finding expected")
        observed = _json_object(row["observed_json"], "finding observed")
        try:
            classification = ReconciliationClassification(
                observed["classification"]
            )
        except (KeyError, ValueError) as exc:
            raise ReconciliationEvidenceError(
                "finding has no supported classification"
            ) from exc

        if classification in {
            ReconciliationClassification.QUARANTINED,
            ReconciliationClassification.HUMAN_OR_PL_ACTION_REQUIRED,
        }:
            action = "QUARANTINED_FOR_HUMAN_OR_PL"
            resulting_oid = observed.get("resulting_oid")
            final_state = FindingState.QUARANTINED
        elif row["resource_type"] == "operation-intent":
            action, resulting_oid = self._reconcile_intent(
                row["resource_id"], classification, expected, observed
            )
            final_state = FindingState.RESOLVED
        elif row["resource_type"] == "integration-attempt":
            action, resulting_oid, quarantined = self._reconcile_attempt(
                row["resource_id"], classification
            )
            final_state = (
                FindingState.QUARANTINED
                if quarantined
                else FindingState.RESOLVED
            )
        elif row["resource_type"] == "promotion":
            action, resulting_oid = self._reconcile_promotion(
                row["resource_id"], classification, observed
            )
            final_state = FindingState.RESOLVED
        else:
            action = "RETRY_COMMAND_ALLOWED"
            resulting_oid = observed.get("resulting_oid")
            final_state = FindingState.RESOLVED

        resulting_oid = require_oid(
            resulting_oid, "resulting_oid", optional=True
        )
        key = f"reconcile:{finding_id}"
        audit_id = _stable_identifier("audit", key)
        resolution = {
            "finding_id": finding_id,
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "classification": classification.value,
            "action": action,
            "state": final_state.value,
            "resulting_oid": resulting_oid,
            "audit_event_id": audit_id,
            "idempotency_key": key,
        }
        with self.state_store.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT * FROM reconciliation_findings WHERE id = ?",
                (finding_id,),
            ).fetchone()
            assert current is not None
            if current["state"] in {"RESOLVED", "QUARANTINED"}:
                return self._result_from_resolution(current)
            connection.execute(
                """
                UPDATE reconciliation_findings
                SET state = ?, resolution_json = ?, resolved_at = ?
                WHERE id = ? AND state = 'RECONCILING'
                """,
                (
                    final_state.value,
                    compact_json(resolution),
                    utc_now(),
                    finding_id,
                ),
            )
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=audit_id,
                event_type="RECONCILIATION_APPLIED",
                actor=ServiceIdentity.RECOVERY_RECONCILER.value,
                subject_type=row["resource_type"],
                subject_id=row["resource_id"],
                subject_oid=resulting_oid,
                payload={
                    "finding_id": finding_id,
                    "classification": classification.value,
                    "action": action,
                    "final_state": final_state.value,
                    "expected": expected,
                    "observed": observed,
                },
                occurred_at=utc_now(),
                idempotency_key=key,
            )
        )
        return ReconciliationResult(**resolution)

    def _inspect_intent(self, intent: sqlite3.Row) -> _Inspection | None:
        operation = intent["operation"]
        payload = _json_object(intent["payload_json"], "intent payload")
        if operation == "execute-integration":
            attempt_id = self._payload_identifier(payload, "attempt_id")
            with self.state_store.transaction() as connection:
                attempt = connection.execute(
                    "SELECT * FROM integration_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
            if attempt is None:
                return self._inspection(
                    "operation-intent",
                    intent["id"],
                    ReconciliationClassification.HUMAN_OR_PL_ACTION_REQUIRED,
                    FindingSeverity.CRITICAL,
                    expected={"attempt_id": attempt_id},
                    observed={"reason": "integration attempt row is missing"},
                )
            return self._inspect_integration_effect(
                resource_type="operation-intent",
                resource_id=intent["id"],
                attempt=attempt,
            )

        if operation == "integration-merge-step":
            attempt_id = self._payload_identifier(payload, "attempt_id")
            with self.state_store.transaction() as connection:
                attempt = connection.execute(
                    "SELECT * FROM integration_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
            if attempt is None:
                return None
            inspected = self._inspect_integration_effect(
                resource_type="operation-intent",
                resource_id=intent["id"],
                attempt=attempt,
            )
            if (
                inspected.classification
                is ReconciliationClassification.RETRY_SAFE
            ):
                return inspected
            return _Inspection(
                resource_type=inspected.resource_type,
                resource_id=inspected.resource_id,
                classification=ReconciliationClassification.HUMAN_OR_PL_ACTION_REQUIRED,
                severity=FindingSeverity.ERROR,
                expected=inspected.expected,
                observed={
                    **dict(inspected.observed),
                    "reason": (
                        "merge-step effect lacks complete immutable attempt evidence"
                    ),
                },
            )

        if operation == "integration-state-transition":
            attempt_id = self._payload_identifier(payload, "attempt_id")
            resulting_state = self._payload_string(payload, "resulting_state")
            with self.state_store.transaction() as connection:
                attempt = connection.execute(
                    "SELECT state, result_oid FROM integration_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
            if attempt is None:
                classification = (
                    ReconciliationClassification.HUMAN_OR_PL_ACTION_REQUIRED
                )
                severity = FindingSeverity.ERROR
                observed_state = None
            elif attempt["state"] == resulting_state:
                classification = (
                    ReconciliationClassification.COMPLETED_BUT_UNRECORDED
                )
                severity = FindingSeverity.WARNING
                observed_state = attempt["state"]
            else:
                classification = ReconciliationClassification.RETRY_SAFE
                severity = FindingSeverity.WARNING
                observed_state = attempt["state"]
            return self._inspection(
                "operation-intent",
                intent["id"],
                classification,
                severity,
                expected={
                    "operation": operation,
                    "attempt_id": attempt_id,
                    "resulting_state": resulting_state,
                },
                observed={
                    "attempt_state": observed_state,
                    "resulting_oid": attempt["result_oid"] if attempt else None,
                },
            )

        if operation == "create-disposable-worktree":
            return self._inspect_create_worktree(intent, payload)
        if operation == "remove-disposable-worktree":
            return self._inspect_remove_worktree(intent, payload)
        if operation == "update-approved-target-ref":
            return self._inspect_target_ref_update(intent, payload)
        if operation in {"promote-approved-oid", "rollback-approved-oid"}:
            promotion_id = self._payload_identifier(payload, "promotion_id")
            with self.state_store.transaction() as connection:
                promotion = connection.execute(
                    "SELECT * FROM promotions WHERE id = ?", (promotion_id,)
                ).fetchone()
            if promotion is None:
                return self._inspection(
                    "operation-intent",
                    intent["id"],
                    ReconciliationClassification.HUMAN_OR_PL_ACTION_REQUIRED,
                    FindingSeverity.ERROR,
                    expected={"promotion_id": promotion_id},
                    observed={"reason": "promotion row is missing"},
                )
            inspected = self._inspect_promotion(promotion)
            return _Inspection(
                resource_type="operation-intent",
                resource_id=intent["id"],
                classification=inspected.classification,
                severity=inspected.severity,
                expected={
                    **dict(inspected.expected),
                    "promotion_id": promotion_id,
                    "controller_operation": operation,
                },
                observed=inspected.observed,
            )
        return None

    def _inspect_attempt(self, attempt: sqlite3.Row) -> _Inspection:
        return self._inspect_integration_effect(
            resource_type="integration-attempt",
            resource_id=attempt["id"],
            attempt=attempt,
        )

    def _inspect_integration_effect(
        self,
        *,
        resource_type: str,
        resource_id: str,
        attempt: sqlite3.Row,
    ) -> _Inspection:
        target, repository = self._goal_repository(attempt["goal_id"])
        final_ref = self._integration_ref(attempt["goal_id"], attempt["id"])
        final_oid = self.repository_service._read_direct_ref(
            repository,
            final_ref,
            require_commit=True,
            allow_missing=True,
        )
        path = self._integration_worktree_path(
            attempt["goal_id"], attempt["id"]
        )
        entries = self.repository_service._worktree_entries(repository)
        entry = entries.get(self._path_key(path))
        branch_ref = self._integration_branch(attempt["goal_id"], attempt["id"])
        branch_oid = self.repository_service._read_direct_ref(
            repository,
            branch_ref,
            require_commit=True,
            allow_missing=True,
        )
        if final_oid is not None:
            classification = (
                ReconciliationClassification.COMPLETED_BUT_UNRECORDED
            )
            severity = FindingSeverity.WARNING
        elif entry is None and (branch_oid is None or branch_oid == attempt["base_oid"]):
            classification = ReconciliationClassification.RETRY_SAFE
            severity = FindingSeverity.WARNING
        else:
            classification = (
                ReconciliationClassification.HUMAN_OR_PL_ACTION_REQUIRED
            )
            severity = FindingSeverity.ERROR
        return self._inspection(
            resource_type,
            resource_id,
            classification,
            severity,
            expected={
                "attempt_id": attempt["id"],
                "goal_id": attempt["goal_id"],
                "base_oid": attempt["base_oid"],
                "integration_ref": final_ref,
                "target_id": target,
            },
            observed={
                "attempt_state": attempt["state"],
                "result_oid": attempt["result_oid"],
                "final_ref_oid": final_oid,
                "worktree_path": str(path),
                "worktree_present": entry is not None,
                "worktree_path_exists": path.exists(),
                "worktree_entry": dict(entry) if entry is not None else None,
                "branch_ref": branch_ref,
                "branch_oid": branch_oid,
            },
        )

    def _inspect_create_worktree(
        self, intent: sqlite3.Row, payload: Mapping[str, Any]
    ) -> _Inspection:
        target_id = self._payload_identifier(payload, "target_id")
        expected_oid = self._payload_oid(payload, "oid")
        worktree_path = Path(self._payload_string(payload, "worktree_path")).resolve()
        target = self.repository_service._load_target(target_id)
        repository = Path(target.managed_repository_path).resolve()
        entry = self.repository_service._worktree_entries(repository).get(
            self._path_key(worktree_path)
        )
        if (
            entry is not None
            and entry.get("HEAD") == expected_oid
            and worktree_path.is_dir()
        ):
            classification = (
                ReconciliationClassification.COMPLETED_BUT_UNRECORDED
            )
            severity = FindingSeverity.WARNING
        elif entry is None and not worktree_path.exists():
            classification = ReconciliationClassification.RETRY_SAFE
            severity = FindingSeverity.WARNING
        else:
            classification = (
                ReconciliationClassification.HUMAN_OR_PL_ACTION_REQUIRED
            )
            severity = FindingSeverity.ERROR
        return self._inspection(
            "operation-intent",
            intent["id"],
            classification,
            severity,
            expected={
                "operation": intent["operation"],
                "target_id": target_id,
                "worktree_path": str(worktree_path),
                "oid": expected_oid,
                "branch_ref": payload.get("branch_ref"),
            },
            observed={
                "worktree_present": entry is not None,
                "path_exists": worktree_path.exists(),
                "entry": dict(entry) if entry is not None else None,
            },
        )

    def _inspect_remove_worktree(
        self, intent: sqlite3.Row, payload: Mapping[str, Any]
    ) -> _Inspection:
        target_id = self._payload_identifier(payload, "target_id")
        worktree_path = Path(self._payload_string(payload, "worktree_path")).resolve()
        target = self.repository_service._load_target(target_id)
        repository = Path(target.managed_repository_path).resolve()
        entry = self.repository_service._worktree_entries(repository).get(
            self._path_key(worktree_path)
        )
        if entry is None and not worktree_path.exists():
            classification = (
                ReconciliationClassification.COMPLETED_BUT_UNRECORDED
            )
            severity = FindingSeverity.WARNING
        else:
            classification = (
                ReconciliationClassification.HUMAN_OR_PL_ACTION_REQUIRED
            )
            severity = FindingSeverity.ERROR
        return self._inspection(
            "operation-intent",
            intent["id"],
            classification,
            severity,
            expected={
                "operation": intent["operation"],
                "target_id": target_id,
                "worktree_path": str(worktree_path),
                "expected_oid": payload.get("expected_oid"),
            },
            observed={
                "worktree_present": entry is not None,
                "path_exists": worktree_path.exists(),
                "entry": dict(entry) if entry is not None else None,
            },
        )

    def _inspect_target_ref_update(
        self, intent: sqlite3.Row, payload: Mapping[str, Any]
    ) -> _Inspection:
        target_id = self._payload_identifier(payload, "target_id")
        target = self.repository_service._load_target(target_id)
        repository = Path(target.git_common_dir).resolve()
        destination_ref = self._payload_string(payload, "destination_ref")
        approved_oid = self._payload_oid(payload, "approved_oid")
        expected_destination = payload.get("expected_destination_oid")
        if expected_destination is not None:
            expected_destination = str(
                require_oid(expected_destination, "expected_destination_oid")
            )
        actual = self.repository_service._read_direct_ref(
            repository,
            destination_ref,
            require_commit=True,
            allow_missing=True,
        )
        source_now = self.repository_service._read_direct_ref(
            repository,
            target.source_ref,
            require_commit=True,
        )
        if actual == approved_oid:
            classification = (
                ReconciliationClassification.COMPLETED_BUT_UNRECORDED
            )
            severity = FindingSeverity.WARNING
        elif actual == expected_destination and source_now == payload.get(
            "expected_source_oid"
        ):
            classification = ReconciliationClassification.RETRY_SAFE
            severity = FindingSeverity.WARNING
        else:
            classification = (
                ReconciliationClassification.HUMAN_OR_PL_ACTION_REQUIRED
            )
            severity = FindingSeverity.CRITICAL
        return self._inspection(
            "operation-intent",
            intent["id"],
            classification,
            severity,
            expected={
                "operation": intent["operation"],
                "target_id": target_id,
                "destination_ref": destination_ref,
                "approved_oid": approved_oid,
                "expected_destination_oid": expected_destination,
                "expected_source_oid": payload.get("expected_source_oid"),
            },
            observed={
                "destination_oid": actual,
                "source_oid": source_now,
                "resulting_oid": actual if actual == approved_oid else None,
            },
        )

    def _inspect_promotion(self, promotion: sqlite3.Row) -> _Inspection:
        target = self.repository_service._load_target(promotion["target_id"])
        repository = Path(target.git_common_dir).resolve()
        actual = self.repository_service._read_direct_ref(
            repository,
            promotion["destination_ref"],
            require_commit=True,
            allow_missing=True,
        )
        if actual == promotion["approved_oid"]:
            classification = (
                ReconciliationClassification.COMPLETED_BUT_UNRECORDED
            )
            severity = FindingSeverity.WARNING
        elif actual == promotion["expected_destination_oid"]:
            classification = ReconciliationClassification.RETRY_SAFE
            severity = FindingSeverity.WARNING
        else:
            classification = (
                ReconciliationClassification.HUMAN_OR_PL_ACTION_REQUIRED
            )
            severity = FindingSeverity.CRITICAL
        return self._inspection(
            "promotion",
            promotion["id"],
            classification,
            severity,
            expected={
                "goal_id": promotion["goal_id"],
                "target_id": promotion["target_id"],
                "destination_ref": promotion["destination_ref"],
                "approved_oid": promotion["approved_oid"],
                "expected_destination_oid": promotion["expected_destination_oid"],
            },
            observed={
                "promotion_state": promotion["state"],
                "destination_oid": actual,
                "resulting_oid": (
                    actual if actual == promotion["approved_oid"] else None
                ),
            },
        )

    def _reconcile_intent(
        self,
        intent_id: str,
        classification: ReconciliationClassification,
        expected: Mapping[str, Any],
        observed: Mapping[str, Any],
    ) -> tuple[str, str | None]:
        with self.state_store.transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM operation_intents WHERE id = ?", (intent_id,)
            ).fetchone()
        if intent is None:
            raise ReconciliationEvidenceError(
                f"operation intent disappeared: {intent_id}"
            )
        if intent["status"] == "COMPLETED":
            return "ALREADY_COMPLETED", intent["resulting_oid"]
        operation = intent["operation"]
        payload = _json_object(intent["payload_json"], "intent payload")

        if classification is ReconciliationClassification.RETRY_SAFE:
            return "RETRY_COMMAND_ALLOWED", None
        if classification is not ReconciliationClassification.COMPLETED_BUT_UNRECORDED:
            return "QUARANTINED_FOR_HUMAN_OR_PL", observed.get("resulting_oid")

        if operation == "execute-integration":
            attempt_id = self._payload_identifier(payload, "attempt_id")
            return self._recover_integration(attempt_id)
        if operation == "integration-state-transition":
            resulting_oid = observed.get("resulting_oid")
            completed = self.state_store.complete_intent(
                intent_id,
                resulting_state=self._payload_string(payload, "resulting_state"),
                resulting_oid=resulting_oid,
                evidence={
                    "classification": classification.value,
                    "attempt_id": payload.get("attempt_id"),
                },
            )
            return "SQLITE_TRANSITION_RECEIPT_COMPLETED", completed.resulting_oid
        if operation == "create-disposable-worktree":
            receipt = GitWorktreeReceipt(
                target_id=self._payload_identifier(payload, "target_id"),
                managed_repository_path=self._payload_string(
                    payload, "managed_repository_path"
                ),
                worktree_path=self._payload_string(payload, "worktree_path"),
                oid=self._payload_oid(payload, "oid"),
                branch_ref=payload.get("branch_ref"),
                intent_id=intent_id,
                idempotency_key=intent["idempotency_key"],
            )
            self.state_store.complete_intent(
                intent_id,
                resulting_state="WORKTREE_READY",
                resulting_oid=receipt.oid,
                evidence={"receipt": asdict(receipt)},
            )
            return "WORKTREE_RECEIPT_COMPLETED", receipt.oid
        if operation == "remove-disposable-worktree":
            expected_oid = self._payload_oid(payload, "expected_oid")
            self.state_store.complete_intent(
                intent_id,
                resulting_state="WORKTREE_REMOVED",
                resulting_oid=expected_oid,
                evidence={
                    "create_intent_id": payload.get("create_intent_id"),
                    "worktree_path": payload.get("worktree_path"),
                    "removed": True,
                    "classification": classification.value,
                },
            )
            return "WORKTREE_REMOVAL_RECEIPT_COMPLETED", expected_oid
        if operation == "update-approved-target-ref":
            approved_oid = self._payload_oid(payload, "approved_oid")
            receipt = GitRefUpdateReceipt(
                target_id=self._payload_identifier(payload, "target_id"),
                source_ref=self._payload_string(payload, "source_ref"),
                destination_ref=self._payload_string(payload, "destination_ref"),
                approved_oid=approved_oid,
                expected_source_oid=self._payload_oid(
                    payload, "expected_source_oid"
                ),
                previous_destination_oid=payload.get("expected_destination_oid"),
                resulting_destination_oid=approved_oid,
                intent_id=intent_id,
                idempotency_key=intent["idempotency_key"],
            )
            self.state_store.complete_intent(
                intent_id,
                resulting_state="TARGET_REF_UPDATED",
                resulting_oid=approved_oid,
                evidence={"receipt": asdict(receipt)},
            )
            return "TARGET_REF_RECEIPT_COMPLETED", approved_oid
        if operation in {"promote-approved-oid", "rollback-approved-oid"}:
            promotion_id = self._payload_identifier(payload, "promotion_id")
            action, oid = self._reconcile_promotion(
                promotion_id, classification, observed
            )
            self.state_store.complete_intent(
                intent_id,
                resulting_state=(
                    "PROMOTED"
                    if operation == "promote-approved-oid"
                    else "ROLLED_BACK"
                ),
                resulting_oid=oid,
                evidence={
                    "promotion_id": promotion_id,
                    "classification": classification.value,
                },
            )
            return action, oid
        raise ReconciliationEvidenceError(
            f"completed-but-unrecorded action is unsupported: {operation}"
        )

    def _reconcile_attempt(
        self,
        attempt_id: str,
        classification: ReconciliationClassification,
    ) -> tuple[str, str | None, bool]:
        if classification is ReconciliationClassification.RETRY_SAFE:
            if self.integration_controller is None:
                return "RETRY_COMMAND_ALLOWED", None, False
            recovered = self.integration_controller.recover_interrupted(attempt_id)
            return (
                "RECREATED_NEW_ATTEMPT"
                if recovered.state is IntegrationAttemptState.RECREATED
                else "RECOVERY_CLASSIFIED",
                recovered.result_oid,
                recovered.state is IntegrationAttemptState.QUARANTINED,
            )
        if classification is ReconciliationClassification.COMPLETED_BUT_UNRECORDED:
            action, oid = self._recover_integration(attempt_id)
            return action, oid, False
        return "QUARANTINED_FOR_HUMAN_OR_PL", None, True

    def _recover_integration(self, attempt_id: str) -> tuple[str, str | None]:
        if self.integration_controller is None:
            raise ReconciliationEvidenceError(
                "integration_controller is required to reconcile integration effects"
            )
        recovered = self.integration_controller.recover_interrupted(attempt_id)
        if recovered.state is IntegrationAttemptState.QUARANTINED:
            return "QUARANTINED_FOR_HUMAN_OR_PL", recovered.result_oid
        if recovered.state is IntegrationAttemptState.RECREATED:
            return "RECREATED_NEW_ATTEMPT", recovered.result_oid
        return "INTEGRATION_RESULT_RECEIPT_COMPLETED", recovered.result_oid

    def _reconcile_promotion(
        self,
        promotion_id: str,
        classification: ReconciliationClassification,
        observed: Mapping[str, Any],
    ) -> tuple[str, str | None]:
        with self.state_store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM promotions WHERE id = ?", (promotion_id,)
            ).fetchone()
            if row is None:
                raise ReconciliationEvidenceError(
                    f"promotion disappeared: {promotion_id}"
                )
            if classification is ReconciliationClassification.RETRY_SAFE:
                return "RETRY_COMMAND_ALLOWED", None
            if classification is not ReconciliationClassification.COMPLETED_BUT_UNRECORDED:
                return "QUARANTINED_FOR_HUMAN_OR_PL", observed.get("resulting_oid")
            actual = observed.get("resulting_oid") or observed.get(
                "destination_oid"
            )
            if actual != row["approved_oid"]:
                raise ReconciliationConflictError(
                    "observed promotion OID differs from immutable request"
                )
            resulting_state = (
                "ROLLED_BACK"
                if row["id"].startswith("rollback-")
                else "PROMOTED"
            )
            connection.execute(
                """
                UPDATE promotions
                SET state = ?, promoted_oid = ?, completed_at = COALESCE(completed_at, ?)
                WHERE id = ? AND state IN ('REQUESTED', 'VALIDATING', ?, ?)
                """,
                (
                    resulting_state,
                    actual,
                    utc_now(),
                    promotion_id,
                    resulting_state,
                    "BLOCKED",
                ),
            )
        return "PROMOTION_ROW_COMPLETED_FROM_REF", actual

    def _persist_inspection(self, inspection: _Inspection) -> ReconciliationFinding:
        observed = {
            **dict(inspection.observed),
            "classification": inspection.classification.value,
        }
        expected = dict(inspection.expected)
        digest = hashlib.sha256(
            compact_json(
                {
                    "resource_type": inspection.resource_type,
                    "resource_id": inspection.resource_id,
                    "classification": inspection.classification.value,
                    "expected": expected,
                    "observed": observed,
                }
            ).encode("utf-8")
        ).hexdigest()
        finding_id = f"reconcile-{digest[:24]}"
        key = f"reconciliation-scan:{digest}"
        with self.state_store.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM reconciliation_findings
                WHERE id = ? OR idempotency_key = ?
                """,
                (finding_id, key),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO reconciliation_findings (
                        id, resource_type, resource_id, severity, state,
                        expected_json, observed_json, idempotency_key, detected_at
                    ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
                    """,
                    (
                        finding_id,
                        inspection.resource_type,
                        inspection.resource_id,
                        inspection.severity.value,
                        compact_json(expected),
                        compact_json(observed),
                        key,
                        utc_now(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM reconciliation_findings WHERE id = ?",
                    (finding_id,),
                ).fetchone()
            elif (
                row["resource_type"] != inspection.resource_type
                or row["resource_id"] != inspection.resource_id
                or row["severity"] != inspection.severity.value
                or row["expected_json"] != compact_json(expected)
                or row["observed_json"] != compact_json(observed)
            ):
                raise IdempotencyConflictError(
                    "reconciliation finding identity has conflicting evidence"
                )
        assert row is not None
        return self._finding_from_row(row)

    @staticmethod
    def _inspection(
        resource_type: str,
        resource_id: str,
        classification: ReconciliationClassification,
        severity: FindingSeverity,
        *,
        expected: Mapping[str, Any],
        observed: Mapping[str, Any],
    ) -> _Inspection:
        return _Inspection(
            resource_type=resource_type,
            resource_id=resource_id,
            classification=classification,
            severity=severity,
            expected=expected,
            observed={
                **dict(observed),
                "classification": classification.value,
            },
        )

    @staticmethod
    def _finding_from_row(row: sqlite3.Row) -> ReconciliationFinding:
        return ReconciliationFinding(
            finding_id=row["id"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            severity=FindingSeverity(row["severity"]),
            state=FindingState(row["state"]),
            expected=_json_object(row["expected_json"], "finding expected"),
            observed=_json_object(row["observed_json"], "finding observed"),
            detected_at=row["detected_at"],
            idempotency_key=row["idempotency_key"],
        )

    @staticmethod
    def _result_from_resolution(row: sqlite3.Row) -> ReconciliationResult:
        resolution = _json_object(row["resolution_json"], "finding resolution")
        if not resolution:
            raise ReconciliationEvidenceError(
                "terminal reconciliation finding has no resolution"
            )
        return ReconciliationResult(**resolution)

    def _goal_repository(self, goal_id: str) -> tuple[str, Path]:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT g.target_id, mr.repository_path
                FROM goals AS g
                JOIN managed_repositories AS mr ON mr.target_id = g.target_id
                WHERE g.id = ?
                """,
                (goal_id,),
            ).fetchone()
        if row is None:
            raise ReconciliationEvidenceError(
                f"goal has no managed repository: {goal_id}"
            )
        return row["target_id"], Path(row["repository_path"]).resolve()

    @staticmethod
    def _payload_string(payload: Mapping[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise ReconciliationEvidenceError(
                f"intent payload field {field} must be a non-empty string"
            )
        return value

    @classmethod
    def _payload_identifier(cls, payload: Mapping[str, Any], field: str) -> str:
        return require_identifier(cls._payload_string(payload, field), field)

    @classmethod
    def _payload_oid(cls, payload: Mapping[str, Any], field: str) -> str:
        return str(require_oid(cls._payload_string(payload, field), field))

    @staticmethod
    def _path_key(path: Path) -> str:
        value = os.path.normcase(str(path.expanduser().resolve()))
        if os.name == "nt":
            value = value.casefold()
        return value.replace("\\", "/").rstrip("/")

    @staticmethod
    def _safe_ref_component(value: str) -> str:
        if (
            value
            and len(value) <= 64
            and all(character.isalnum() or character in "._-" for character in value)
            and value[0].isalnum()
        ):
            return value
        return f"id-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"

    def _integration_ref(self, goal_id: str, attempt_id: str) -> str:
        return (
            "refs/agentic-ax/integration/"
            f"{self._safe_ref_component(goal_id)}/"
            f"{self._safe_ref_component(attempt_id)}"
        )

    def _integration_worktree_path(
        self, goal_id: str, attempt_id: str
    ) -> Path:
        with self.state_store.transaction() as connection:
            lease = connection.execute(
                """
                SELECT worktree_path FROM runtime_leases
                WHERE idempotency_key = ? AND lease_kind = 'INTEGRATION'
                """,
                (f"integration-lease:{attempt_id}",),
            ).fetchone()
        if lease is not None:
            return Path(lease["worktree_path"]).resolve(strict=False)
        return self.path_authority.integration_worktree(goal_id, attempt_id)

    def _integration_branch(self, goal_id: str, attempt_id: str) -> str:
        return (
            "refs/heads/ax/integration/"
            f"{self._safe_ref_component(goal_id)}/"
            f"{self._safe_ref_component(attempt_id)}"
        )
