from __future__ import annotations

"""Same-OID promotion and authorized rollback policy.

The Phase 4 :class:`TargetRefAdapter` remains the only component that transfers
objects into the user's Git authority.  This module proves policy first and
then calls that narrow primitive; it never updates ``HEAD``, the index,
``refs/heads/*``, or a working tree.
"""

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

try:
    from .agent_team_domain import (
        AuditEvent,
        GateDecisionValue,
        GateType,
        PromotionRequest,
        PromotionState,
        ServiceIdentity,
        require_git_ref,
        require_identifier,
        require_nonempty,
        require_oid,
    )
    from .agent_team_gates import GateCoordinator, PromotionInvariantError
    from .agent_team_integration import (
        DeliveryV4EvidenceError,
        require_delivery_v4_result_evidence,
    )
    from .agent_team_git import (
        APPROVED_REF_PREFIX,
        GitCASMismatchError,
        GitRefError,
        GitRefUpdateReceipt,
        ManagedRepositoryService,
        TargetRefAdapter,
    )
    from .agent_team_state import (
        AxStateStore,
        IdempotencyConflictError,
        compact_json,
        utc_now,
    )
except ImportError:  # Direct script/module execution from the repository root.
    from agent_team_domain import (
        AuditEvent,
        GateDecisionValue,
        GateType,
        PromotionRequest,
        PromotionState,
        ServiceIdentity,
        require_git_ref,
        require_identifier,
        require_nonempty,
        require_oid,
    )
    from agent_team_gates import GateCoordinator, PromotionInvariantError
    from agent_team_integration import (
        DeliveryV4EvidenceError,
        require_delivery_v4_result_evidence,
    )
    from agent_team_git import (
        APPROVED_REF_PREFIX,
        GitCASMismatchError,
        GitRefError,
        GitRefUpdateReceipt,
        ManagedRepositoryService,
        TargetRefAdapter,
    )
    from agent_team_state import (
        AxStateStore,
        IdempotencyConflictError,
        compact_json,
        utc_now,
    )


class PromotionControllerError(RuntimeError):
    """Base error for promotion and rollback policy."""


class PromotionAuthorizationError(PromotionControllerError):
    """Required gates, target ownership, or authorization IDs are invalid."""


class PromotionRequestConflictError(PromotionControllerError):
    """A promotion identity/idempotency key was reused with different input."""


class PromotionBlockedError(PromotionControllerError):
    """Policy passed but a source/destination CAS or reconciliation guard blocked."""


class RollbackAuthorizationError(PromotionAuthorizationError):
    """Rollback lacks explicit PL/PM authorization or approved history."""


@dataclass(frozen=True, slots=True)
class PromotionReceipt:
    promotion_id: str
    goal_id: str
    target_id: str
    operation: str
    destination_ref: str
    previous_destination_oid: str | None
    resulting_oid: str
    invariant_oid: str
    gate_decision_ids: tuple[str, ...]
    authorization_ids: tuple[str, ...]
    state: PromotionState
    adapter_intent_id: str
    controller_intent_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for field in ("promotion_id", "goal_id", "target_id"):
            object.__setattr__(
                self, field, require_identifier(getattr(self, field), field)
            )
        operation = require_nonempty(self.operation, "operation")
        if operation not in {"PROMOTE", "ROLLBACK"}:
            raise ValueError("operation must be PROMOTE or ROLLBACK")
        object.__setattr__(self, "operation", operation)
        destination = require_git_ref(self.destination_ref, "destination_ref")
        if not destination.startswith(APPROVED_REF_PREFIX):
            raise ValueError(
                f"destination_ref must be under {APPROVED_REF_PREFIX}"
            )
        object.__setattr__(self, "destination_ref", destination)
        object.__setattr__(
            self,
            "previous_destination_oid",
            require_oid(
                self.previous_destination_oid,
                "previous_destination_oid",
                optional=True,
            ),
        )
        for field in ("resulting_oid", "invariant_oid"):
            object.__setattr__(
                self, field, require_oid(getattr(self, field), field)
            )
        for field in ("gate_decision_ids", "authorization_ids"):
            value = tuple(getattr(self, field))
            if any(not isinstance(item, str) or not item for item in value):
                raise ValueError(f"{field} must contain non-empty strings")
            if len(set(value)) != len(value):
                raise ValueError(f"{field} must not contain duplicates")
            object.__setattr__(self, field, value)
        if not isinstance(self.state, PromotionState):
            object.__setattr__(self, "state", PromotionState(self.state))
        for field in ("adapter_intent_id", "controller_intent_id"):
            object.__setattr__(
                self, field, require_identifier(getattr(self, field), field)
            )
        object.__setattr__(
            self,
            "idempotency_key",
            require_nonempty(self.idempotency_key, "idempotency_key"),
        )


def _stable_identifier(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _json_array(raw: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PromotionRequestConflictError(
            "stored promotion gate IDs are not valid JSON"
        ) from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise PromotionRequestConflictError(
            "stored promotion gate IDs must be a string array"
        )
    return tuple(parsed)


class PromotionController:
    """Guard policy and delegate one namespaced CAS effect to Phase 4."""

    def __init__(
        self,
        *,
        state_store: AxStateStore,
        repository_service: ManagedRepositoryService,
        target_ref_adapter: TargetRefAdapter,
        gate_coordinator: GateCoordinator,
    ) -> None:
        if not isinstance(state_store, AxStateStore):
            raise TypeError("state_store must be an AxStateStore")
        if not isinstance(repository_service, ManagedRepositoryService):
            raise TypeError(
                "repository_service must be a ManagedRepositoryService"
            )
        if not isinstance(target_ref_adapter, TargetRefAdapter):
            raise TypeError("target_ref_adapter must be a TargetRefAdapter")
        if not isinstance(gate_coordinator, GateCoordinator):
            raise TypeError("gate_coordinator must be a GateCoordinator")
        if (
            repository_service.state_store is not state_store
            or target_ref_adapter.state_store is not state_store
            or gate_coordinator.state_store is not state_store
        ):
            raise ValueError("promotion collaborators must share one state_store")
        self.state_store = state_store
        self.repository_service = repository_service
        self.target_ref_adapter = target_ref_adapter
        self.gate_coordinator = gate_coordinator
        self.state_store.initialize()

    def promote(self, request: PromotionRequest) -> PromotionReceipt:
        """Promote only the one OID proven by every mandatory gate."""

        if not isinstance(request, PromotionRequest):
            raise TypeError("request must be a PromotionRequest")
        proven_oid = self.gate_coordinator.assert_promotion_invariant(
            request.goal_id, request.approved_oid
        )
        if proven_oid != request.approved_oid:
            raise PromotionAuthorizationError(
                "requested approved OID differs from the gate-proven OID"
            )
        delivery_results = []
        for transition_id, capability_id in (
            ("qa_validate_integration", "qa_sdet"),
            ("build_validate_integration", "build_release"),
            ("pm_accept_integration", "pm"),
        ):
            try:
                delivery_results.append(
                    require_delivery_v4_result_evidence(
                        self.state_store,
                        goal_id=request.goal_id,
                        subject_oid=proven_oid,
                        transition_id=transition_id,
                        capability_id=capability_id,
                        result_kinds=("approved",),
                        expected_result_oid=proven_oid,
                    )
                )
            except DeliveryV4EvidenceError as exc:
                raise PromotionAuthorizationError(
                    f"promotion lacks admitted {transition_id} delivery-v4 evidence"
                ) from exc
        required = self.gate_coordinator.required_decisions(
            request.goal_id, proven_oid
        )
        required_ids = tuple(decision.decision_id for decision in required)
        if set(request.required_gate_decision_ids) != set(required_ids):
            missing = sorted(
                set(required_ids) - set(request.required_gate_decision_ids)
            )
            extra = sorted(
                set(request.required_gate_decision_ids) - set(required_ids)
            )
            raise PromotionAuthorizationError(
                f"promotion gate decision set is not exact; "
                f"missing={missing}, extra={extra}"
            )
        target = self.repository_service._load_target(request.target_id)
        self._assert_goal_target(request.goal_id, request.target_id)

        signature = {
            "promotion_id": request.promotion_id,
            "goal_id": request.goal_id,
            "target_id": request.target_id,
            "approved_oid": request.approved_oid,
            "expected_source_oid": request.expected_source_oid,
            "destination_ref": request.destination_ref,
            "expected_destination_oid": request.expected_destination_oid,
            "required_gate_decision_ids": list(
                request.required_gate_decision_ids
            ),
            "delivery_v4_contract_ids": [
                item.contract_id for item in delivery_results
            ],
            "delivery_v4_result_ids": [
                item.result_id for item in delivery_results
            ],
            "operation": "PROMOTE",
        }
        intent = self.state_store.begin_intent(
            operation="promote-approved-oid",
            idempotency_key=f"promotion-controller:{request.idempotency_key}",
            expected_state="GATES_APPROVED",
            expected_oid=proven_oid,
            payload=signature,
        )
        existing = self._ensure_promotion_row(
            promotion_id=request.promotion_id,
            goal_id=request.goal_id,
            target_id=request.target_id,
            approved_oid=request.approved_oid,
            expected_source_oid=request.expected_source_oid,
            destination_ref=request.destination_ref,
            expected_destination_oid=request.expected_destination_oid,
            gate_ids=request.required_gate_decision_ids,
            idempotency_key=request.idempotency_key,
        )
        if intent.status.value == "COMPLETED":
            if existing["state"] == PromotionState.BLOCKED.value:
                raise PromotionBlockedError(
                    f"promotion is already blocked: {request.promotion_id}"
                )
            return self._receipt_from_completed_row(
                existing,
                operation="PROMOTE",
                invariant_oid=proven_oid,
                authorization_ids=(),
                controller_intent_id=intent.intent_id,
            )

        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE promotions SET state = 'VALIDATING'
                WHERE id = ? AND state IN ('REQUESTED', 'VALIDATING')
                """,
                (request.promotion_id,),
            )
        try:
            adapter_receipt = (
                self.target_ref_adapter.transfer_object_and_update_namespaced_ref(
                    target=target,
                    approved_oid=proven_oid,
                    destination_ref=request.destination_ref,
                    expected_source_oid=request.expected_source_oid,
                    expected_destination_oid=request.expected_destination_oid,
                    idempotency_key=f"promotion-adapter:{request.idempotency_key}",
                )
            )
        except (GitCASMismatchError, GitRefError) as exc:
            self._block_promotion(
                promotion_id=request.promotion_id,
                controller_intent_id=intent.intent_id,
                goal_id=request.goal_id,
                subject_oid=proven_oid,
                reason=str(exc),
            )
            raise PromotionBlockedError(
                "promotion source/destination CAS or ref guard failed"
            ) from exc

        if adapter_receipt.resulting_destination_oid != proven_oid:
            self._block_promotion(
                promotion_id=request.promotion_id,
                controller_intent_id=intent.intent_id,
                goal_id=request.goal_id,
                subject_oid=proven_oid,
                reason="target adapter returned a different resulting OID",
            )
            raise PromotionBlockedError(
                "target adapter did not produce the gate-proven OID"
            )
        with self.state_store.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE promotions
                SET state = 'PROMOTED', promoted_oid = ?, completed_at = ?
                WHERE id = ? AND state = 'VALIDATING'
                """,
                (proven_oid, utc_now(), request.promotion_id),
            ).rowcount
            if updated != 1:
                row = connection.execute(
                    "SELECT * FROM promotions WHERE id = ?",
                    (request.promotion_id,),
                ).fetchone()
                if (
                    row is None
                    or row["state"] != "PROMOTED"
                    or row["promoted_oid"] != proven_oid
                ):
                    raise PromotionRequestConflictError(
                        "promotion state changed during guarded ref update"
                    )
        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="PROMOTED",
            resulting_oid=proven_oid,
            evidence={
                "promotion_id": request.promotion_id,
                "adapter_receipt": asdict(adapter_receipt),
                "gate_decision_ids": list(required_ids),
                "delivery_v4_contract_ids": [
                    item.contract_id for item in delivery_results
                ],
                "delivery_v4_result_ids": [
                    item.result_id for item in delivery_results
                ],
            },
        )
        receipt = self._receipt(
            promotion_id=request.promotion_id,
            goal_id=request.goal_id,
            target_id=request.target_id,
            operation="PROMOTE",
            destination_ref=request.destination_ref,
            invariant_oid=proven_oid,
            gate_ids=required_ids,
            authorization_ids=(),
            adapter_receipt=adapter_receipt,
            controller_intent_id=completed.intent_id,
            idempotency_key=request.idempotency_key,
            state=PromotionState.PROMOTED,
        )
        self._record_audit(receipt, adapter_receipt)
        return receipt

    def rollback_to_approved(
        self,
        *,
        target_id: str,
        destination_ref: str,
        current_expected_oid: str,
        prior_approved_oid: str,
        authorization_ids: Sequence[str],
    ) -> PromotionReceipt:
        """Create a new, explicitly authorized namespaced CAS rollback record."""

        target_id = require_identifier(target_id, "target_id")
        destination = require_git_ref(destination_ref, "destination_ref")
        if not destination.startswith(APPROVED_REF_PREFIX):
            raise RollbackAuthorizationError(
                f"rollback destination must be under {APPROVED_REF_PREFIX}"
            )
        current = str(require_oid(current_expected_oid, "current_expected_oid"))
        prior = str(require_oid(prior_approved_oid, "prior_approved_oid"))
        if current == prior:
            raise RollbackAuthorizationError(
                "rollback target must differ from the current expected OID"
            )
        if isinstance(authorization_ids, (str, bytes)):
            raise RollbackAuthorizationError(
                "authorization_ids must be a sequence"
            )
        authorizations = tuple(
            require_identifier(item, "authorization_id")
            for item in authorization_ids
        )
        if not authorizations or len(set(authorizations)) != len(authorizations):
            raise RollbackAuthorizationError(
                "rollback requires unique explicit authorization IDs"
            )

        history = self._require_rollback_history(
            target_id=target_id,
            destination_ref=destination,
            current_oid=current,
            prior_oid=prior,
        )
        goal_id = history["goal_id"]
        proven_oid = self.gate_coordinator.assert_promotion_invariant(
            goal_id, prior
        )
        if proven_oid != prior:
            raise RollbackAuthorizationError(
                "prior OID no longer has a complete exact-OID approval chain"
            )
        self._validate_rollback_authorizations(
            goal_id=goal_id,
            prior_oid=prior,
            authorization_ids=authorizations,
        )
        required_ids = tuple(
            decision.decision_id
            for decision in self.gate_coordinator.required_decisions(goal_id, prior)
        )
        target = self.repository_service._load_target(target_id)
        payload = {
            "goal_id": goal_id,
            "target_id": target_id,
            "destination_ref": destination,
            "current_expected_oid": current,
            "prior_approved_oid": prior,
            "authorization_ids": list(authorizations),
        }
        digest = hashlib.sha256(compact_json(payload).encode("utf-8")).hexdigest()
        promotion_id = f"rollback-{digest[:24]}"
        idempotency_key = f"rollback:{digest}"
        intent = self.state_store.begin_intent(
            operation="rollback-approved-oid",
            idempotency_key=f"promotion-controller:{idempotency_key}",
            expected_state="EXPLICITLY_AUTHORIZED",
            expected_oid=current,
            payload=payload,
        )
        row = self._ensure_promotion_row(
            promotion_id=promotion_id,
            goal_id=goal_id,
            target_id=target_id,
            approved_oid=prior,
            expected_source_oid=target.observed_source_oid,
            destination_ref=destination,
            expected_destination_oid=current,
            gate_ids=authorizations,
            idempotency_key=idempotency_key,
        )
        if intent.status.value == "COMPLETED":
            if row["state"] == PromotionState.BLOCKED.value:
                raise PromotionBlockedError(
                    f"rollback is already blocked: {promotion_id}"
                )
            return self._receipt_from_completed_row(
                row,
                operation="ROLLBACK",
                invariant_oid=prior,
                authorization_ids=authorizations,
                controller_intent_id=intent.intent_id,
                gate_ids=required_ids,
            )

        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE promotions SET state = 'VALIDATING'
                WHERE id = ? AND state IN ('REQUESTED', 'VALIDATING')
                """,
                (promotion_id,),
            )
        try:
            adapter_receipt = (
                self.target_ref_adapter.transfer_object_and_update_namespaced_ref(
                    target=target,
                    approved_oid=prior,
                    destination_ref=destination,
                    expected_source_oid=target.observed_source_oid,
                    expected_destination_oid=current,
                    idempotency_key=f"promotion-adapter:{idempotency_key}",
                )
            )
        except (GitCASMismatchError, GitRefError) as exc:
            self._block_promotion(
                promotion_id=promotion_id,
                controller_intent_id=intent.intent_id,
                goal_id=goal_id,
                subject_oid=prior,
                reason=str(exc),
            )
            raise PromotionBlockedError(
                "rollback source/destination CAS or ref guard failed"
            ) from exc
        if adapter_receipt.resulting_destination_oid != prior:
            raise PromotionBlockedError(
                "rollback adapter did not produce the prior approved OID"
            )
        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE promotions
                SET state = 'ROLLED_BACK', promoted_oid = ?, completed_at = ?
                WHERE id = ? AND state = 'VALIDATING'
                """,
                (prior, utc_now(), promotion_id),
            )
        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="ROLLED_BACK",
            resulting_oid=prior,
            evidence={
                "promotion_id": promotion_id,
                "adapter_receipt": asdict(adapter_receipt),
                "authorization_ids": list(authorizations),
                "prior_history_promotion_id": history["id"],
            },
        )
        receipt = self._receipt(
            promotion_id=promotion_id,
            goal_id=goal_id,
            target_id=target_id,
            operation="ROLLBACK",
            destination_ref=destination,
            invariant_oid=prior,
            gate_ids=required_ids,
            authorization_ids=authorizations,
            adapter_receipt=adapter_receipt,
            controller_intent_id=completed.intent_id,
            idempotency_key=idempotency_key,
            state=PromotionState.ROLLED_BACK,
        )
        self._record_audit(receipt, adapter_receipt)
        return receipt

    def _assert_goal_target(self, goal_id: str, target_id: str) -> None:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                "SELECT target_id FROM goals WHERE id = ?", (goal_id,)
            ).fetchone()
        if row is None or row["target_id"] != target_id:
            raise PromotionAuthorizationError(
                "promotion target does not own the requested goal"
            )

    def _ensure_promotion_row(
        self,
        *,
        promotion_id: str,
        goal_id: str,
        target_id: str,
        approved_oid: str,
        expected_source_oid: str,
        destination_ref: str,
        expected_destination_oid: str | None,
        gate_ids: Sequence[str],
        idempotency_key: str,
    ) -> sqlite3.Row:
        expected_signature = (
            goal_id,
            target_id,
            approved_oid,
            expected_source_oid,
            destination_ref,
            expected_destination_oid,
            compact_json(list(gate_ids)),
            idempotency_key,
        )
        with self.state_store.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM promotions
                WHERE id = ? OR idempotency_key = ?
                """,
                (promotion_id, idempotency_key),
            ).fetchall()
            if not rows:
                connection.execute(
                    """
                    INSERT INTO promotions (
                        id, goal_id, target_id, approved_oid,
                        expected_source_oid, destination_ref,
                        expected_destination_oid,
                        required_gate_decision_ids_json, state,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'REQUESTED', ?, ?)
                    """,
                    (
                        promotion_id,
                        *expected_signature[:-1],
                        idempotency_key,
                        utc_now(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM promotions WHERE id = ?", (promotion_id,)
                ).fetchone()
            elif len(rows) == 1:
                row = rows[0]
            else:
                raise PromotionRequestConflictError(
                    "promotion ID and idempotency key resolve to different records"
                )
            assert row is not None
            actual_signature = (
                row["goal_id"],
                row["target_id"],
                row["approved_oid"],
                row["expected_source_oid"],
                row["destination_ref"],
                row["expected_destination_oid"],
                row["required_gate_decision_ids_json"],
                row["idempotency_key"],
            )
            if actual_signature != expected_signature:
                raise PromotionRequestConflictError(
                    "promotion immutable request input differs"
                )
            return row

    def _require_rollback_history(
        self,
        *,
        target_id: str,
        destination_ref: str,
        current_oid: str,
        prior_oid: str,
    ) -> sqlite3.Row:
        with self.state_store.transaction() as connection:
            current_rows = connection.execute(
                """
                SELECT * FROM promotions
                WHERE target_id = ? AND destination_ref = ?
                  AND promoted_oid = ?
                  AND state IN ('PROMOTED', 'ROLLED_BACK')
                ORDER BY completed_at DESC, created_at DESC
                """,
                (target_id, destination_ref, current_oid),
            ).fetchall()
            prior_rows = connection.execute(
                """
                SELECT * FROM promotions
                WHERE target_id = ? AND destination_ref = ?
                  AND promoted_oid = ?
                  AND state IN ('PROMOTED', 'ROLLED_BACK')
                ORDER BY completed_at DESC, created_at DESC
                """,
                (target_id, destination_ref, prior_oid),
            ).fetchall()
        if not current_rows:
            raise RollbackAuthorizationError(
                "current expected OID has no retained promotion history"
            )
        if not prior_rows:
            raise RollbackAuthorizationError(
                "prior OID has no retained approved promotion history"
            )
        if current_rows[0]["goal_id"] != prior_rows[0]["goal_id"]:
            raise RollbackAuthorizationError(
                "rollback history belongs to different goals"
            )
        return prior_rows[0]

    def _validate_rollback_authorizations(
        self,
        *,
        goal_id: str,
        prior_oid: str,
        authorization_ids: Sequence[str],
    ) -> None:
        with self.state_store.transaction() as connection:
            placeholders = ",".join("?" for _ in authorization_ids)
            rows = connection.execute(
                f"""
                SELECT * FROM gate_decisions
                WHERE id IN ({placeholders})
                """,
                tuple(authorization_ids),
            ).fetchall()
        if len(rows) != len(authorization_ids):
            raise RollbackAuthorizationError(
                "one or more rollback authorization IDs do not exist"
            )
        by_type = {}
        for row in rows:
            if (
                row["goal_id"] != goal_id
                or row["subject_oid"] != prior_oid
                or row["decision"] != GateDecisionValue.APPROVED.value
            ):
                raise RollbackAuthorizationError(
                    "rollback authorization must approve the prior OID for this goal"
                )
            by_type[row["gate_type"]] = row
        if {
            GateType.PL_INTEGRATION.value,
            GateType.PM_REQUIREMENTS.value,
        } - set(by_type):
            raise RollbackAuthorizationError(
                "rollback requires explicit PL integration and PM requirement "
                "authorization IDs"
            )

    def _block_promotion(
        self,
        *,
        promotion_id: str,
        controller_intent_id: str,
        goal_id: str,
        subject_oid: str,
        reason: str,
    ) -> None:
        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE promotions
                SET state = 'BLOCKED', completed_at = COALESCE(completed_at, ?)
                WHERE id = ? AND state IN ('REQUESTED', 'VALIDATING', 'BLOCKED')
                """,
                (utc_now(), promotion_id),
            )
        self.state_store.complete_intent(
            controller_intent_id,
            resulting_state="BLOCKED",
            resulting_oid=None,
            evidence={"promotion_id": promotion_id, "reason": reason[:4096]},
        )
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=_stable_identifier("audit", controller_intent_id, "blocked"),
                event_type="PROMOTION_BLOCKED",
                actor=ServiceIdentity.PROMOTION_CONTROLLER.value,
                subject_type="promotion",
                subject_id=promotion_id,
                goal_id=goal_id,
                subject_oid=subject_oid,
                payload={"reason": reason[:4096]},
                occurred_at=utc_now(),
                idempotency_key=f"audit:{controller_intent_id}:blocked",
            )
        )

    def _receipt(
        self,
        *,
        promotion_id: str,
        goal_id: str,
        target_id: str,
        operation: str,
        destination_ref: str,
        invariant_oid: str,
        gate_ids: Sequence[str],
        authorization_ids: Sequence[str],
        adapter_receipt: GitRefUpdateReceipt,
        controller_intent_id: str,
        idempotency_key: str,
        state: PromotionState,
    ) -> PromotionReceipt:
        return PromotionReceipt(
            promotion_id=promotion_id,
            goal_id=goal_id,
            target_id=target_id,
            operation=operation,
            destination_ref=destination_ref,
            previous_destination_oid=adapter_receipt.previous_destination_oid,
            resulting_oid=adapter_receipt.resulting_destination_oid,
            invariant_oid=invariant_oid,
            gate_decision_ids=tuple(gate_ids),
            authorization_ids=tuple(authorization_ids),
            state=state,
            adapter_intent_id=adapter_receipt.intent_id,
            controller_intent_id=controller_intent_id,
            idempotency_key=idempotency_key,
        )

    def _receipt_from_completed_row(
        self,
        row: sqlite3.Row,
        *,
        operation: str,
        invariant_oid: str,
        authorization_ids: Sequence[str],
        controller_intent_id: str,
        gate_ids: Sequence[str] | None = None,
    ) -> PromotionReceipt:
        resulting = row["promoted_oid"]
        if resulting is None:
            raise PromotionRequestConflictError(
                "completed promotion record has no promoted OID"
            )
        controller_intent = self._load_intent(controller_intent_id)
        evidence = json.loads(controller_intent["evidence_json"])
        adapter_raw = evidence.get("adapter_receipt")
        if not isinstance(adapter_raw, dict):
            raise PromotionRequestConflictError(
                "completed promotion intent has no adapter receipt"
            )
        adapter = GitRefUpdateReceipt(**adapter_raw)
        return self._receipt(
            promotion_id=row["id"],
            goal_id=row["goal_id"],
            target_id=row["target_id"],
            operation=operation,
            destination_ref=row["destination_ref"],
            invariant_oid=invariant_oid,
            gate_ids=(
                tuple(gate_ids)
                if gate_ids is not None
                else _json_array(row["required_gate_decision_ids_json"])
            ),
            authorization_ids=authorization_ids,
            adapter_receipt=adapter,
            controller_intent_id=controller_intent_id,
            idempotency_key=row["idempotency_key"],
            state=PromotionState(row["state"]),
        )

    def _load_intent(self, intent_id: str) -> sqlite3.Row:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM operation_intents WHERE id = ?", (intent_id,)
            ).fetchone()
        if row is None:
            raise PromotionRequestConflictError(
                f"controller intent does not exist: {intent_id}"
            )
        return row

    def _record_audit(
        self,
        receipt: PromotionReceipt,
        adapter_receipt: GitRefUpdateReceipt,
    ) -> None:
        key = f"promotion:{receipt.controller_intent_id}:{receipt.operation}"
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=_stable_identifier("audit", key),
                event_type=(
                    "APPROVED_OID_PROMOTED"
                    if receipt.operation == "PROMOTE"
                    else "APPROVED_OID_ROLLED_BACK"
                ),
                actor=ServiceIdentity.PROMOTION_CONTROLLER.value,
                subject_type="promotion",
                subject_id=receipt.promotion_id,
                goal_id=receipt.goal_id,
                subject_oid=receipt.resulting_oid,
                payload={
                    "operation": receipt.operation,
                    "destination_ref": receipt.destination_ref,
                    "previous_destination_oid": receipt.previous_destination_oid,
                    "resulting_oid": receipt.resulting_oid,
                    "gate_decision_ids": list(receipt.gate_decision_ids),
                    "authorization_ids": list(receipt.authorization_ids),
                    "adapter_receipt": asdict(adapter_receipt),
                    "user_branch_or_checkout_updated": False,
                },
                occurred_at=utc_now(),
                idempotency_key=key,
            )
        )
