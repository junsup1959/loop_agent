from __future__ import annotations

"""Role-authorized exact-OID gates for the Agent-Team worktree detail.

This module extends the existing role and SQLite contracts.  It does not create
another approval hierarchy: TA, QA/SDET, Build/Release, PL, and PM retain the
authorities declared in ``agents/roles/*.toml``.  Deterministic controllers are
service identities and are deliberately absent from :data:`GATE_AUTHORITIES`.
"""

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

try:
    from .agent_team_domain import (
        AuditEvent,
        FindingSeverity,
        FindingState,
        GateDecision,
        GateDecisionValue,
        GateType,
        IntegrationAttemptState,
        SourceIntegrity,
        require_identifier,
        require_nonempty,
        require_oid,
    )
    from .agent_team_state import AxStateStore, IdempotencyConflictError, compact_json, utc_now
except ImportError:  # Direct script/module execution from the repository root.
    from agent_team_domain import (
        AuditEvent,
        FindingSeverity,
        FindingState,
        GateDecision,
        GateDecisionValue,
        GateType,
        IntegrationAttemptState,
        SourceIntegrity,
        require_identifier,
        require_nonempty,
        require_oid,
    )
    from agent_team_state import AxStateStore, IdempotencyConflictError, compact_json, utc_now


GATE_AUTHORITIES: Mapping[GateType, str] = {
    GateType.TA_CODE_QUALITY: "ta",
    GateType.TA_ARCHITECTURE: "ta",
    GateType.QA_QUALITY: "qa_sdet",
    GateType.BUILD: "build_release",
    GateType.PL_CANDIDATE_SELECTION: "pl",
    GateType.PL_INTEGRATION: "pl",
    GateType.PM_REQUIREMENTS: "pm",
}

POST_MERGE_GATE_SEQUENCE = (
    GateType.QA_QUALITY,
    GateType.BUILD,
    GateType.PL_INTEGRATION,
    GateType.PM_REQUIREMENTS,
)

REWORK_REPORTER_ROLES = frozenset(
    {
        "ta",
        "qa_sdet",
        "build_release",
        "pl",
        "service:integration-controller",
        "service:recovery-reconciler",
    }
)

_DECISION_TERMINAL_ACTIVATION_STATES = frozenset(
    {
        "RESULT_PERSISTED",
        "PROFILE_REVOKED",
        "RESOURCES_RELEASED",
        "TERMINATED",
        "RECOVERY_CLEANED",
    }
)


class GateCoordinatorError(RuntimeError):
    """Base error for gate authorization and sequencing."""


class GateAuthorityError(GateCoordinatorError):
    """The activation, role, or seat identity does not own the requested gate."""


class GateSelfApprovalError(GateAuthorityError):
    """A candidate owner attempted to review its own revision."""


class GateEvidenceError(GateCoordinatorError):
    """Gate evidence is missing, dirty, stale, or tied to another OID."""


class GateSequenceError(GateCoordinatorError):
    """A gate was attempted before its required predecessor."""


class GateDecisionConflictError(GateCoordinatorError):
    """An immutable gate already has a duplicate or conflicting decision."""


class PromotionInvariantError(GateCoordinatorError):
    """The exact-OID promotion invariant cannot be proven."""


class ReworkAuthorityError(GateCoordinatorError):
    """A role that owns neither reporting nor allocation requested rework."""


@dataclass(frozen=True, slots=True)
class PLReworkRequest:
    """A finding routed to PL without creating or assigning a revision.

    Only PL may turn this request into a new work-item revision and choose its
    developer owner.  ``preferred_owner`` is evidence, not an assignment.
    """

    request_id: str
    finding_id: str
    requested_by_role: str
    goal_id: str | None
    attempt_id: str | None
    subject_oid: str | None
    preferred_owner: str | None
    state: str
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", require_identifier(self.request_id, "request_id")
        )
        object.__setattr__(
            self, "finding_id", require_identifier(self.finding_id, "finding_id")
        )
        object.__setattr__(
            self,
            "requested_by_role",
            require_nonempty(self.requested_by_role, "requested_by_role"),
        )
        for field in ("goal_id", "attempt_id"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, require_identifier(value, field))
        object.__setattr__(
            self,
            "subject_oid",
            require_oid(self.subject_oid, "subject_oid", optional=True),
        )
        if self.preferred_owner is not None:
            object.__setattr__(
                self,
                "preferred_owner",
                require_identifier(self.preferred_owner, "preferred_owner"),
            )
        object.__setattr__(self, "state", require_nonempty(self.state, "state"))
        object.__setattr__(
            self,
            "idempotency_key",
            require_nonempty(self.idempotency_key, "idempotency_key"),
        )


def _stable_identifier(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _json_object(value: str | None) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GateEvidenceError("stored gate evidence is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise GateEvidenceError("stored gate evidence must be a JSON object")
    return parsed


def _json_array(value: str | None) -> list[Any]:
    if value is None:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GateEvidenceError("stored gate evidence is not valid JSON") from exc
    if not isinstance(parsed, list):
        raise GateEvidenceError("stored gate evidence must be a JSON array")
    return parsed


class GateCoordinator:
    """Persist immutable role decisions after proving authority and sequence."""

    def __init__(self, state_store: AxStateStore) -> None:
        if not isinstance(state_store, AxStateStore):
            raise TypeError("state_store must be an AxStateStore")
        self.state_store = state_store
        self.state_store.initialize()

    def record_decision(self, decision: GateDecision) -> GateDecision:
        """Validate and append one exact-OID gate decision.

        A byte-for-byte idempotent replay is returned.  A second decision for
        the same ``goal + gate + subject_oid`` is rejected even if it agrees:
        callers must not disguise a duplicate activation as independent gate
        evidence.
        """

        if not isinstance(decision, GateDecision):
            raise TypeError("decision must be a GateDecision")

        with self.state_store.transaction(immediate=True) as connection:
            replay = self._existing_decision_or_reject(connection, decision)
            if replay is not None:
                return replay
            activation = self._require_authorized_activation(connection, decision)
            validation = self._validate_sequence_and_evidence(
                connection, decision, activation
            )

        intent = self.state_store.begin_intent(
            operation="record-gate-decision",
            idempotency_key=decision.idempotency_key,
            expected_state="GATE_UNDECIDED",
            expected_oid=decision.subject_oid,
            payload={
                "decision_id": decision.decision_id,
                "goal_id": decision.goal_id,
                "activation_id": decision.activation_id,
                "gate_type": decision.gate_type.value,
                "actor_role": decision.actor_role,
                "decision": decision.decision.value,
                "profile_digest": decision.profile_digest,
                "evidence_ids": list(decision.evidence_ids),
            },
        )
        if intent.status.value == "COMPLETED":
            stored_id = intent.evidence.get("decision_id")
            if stored_id != decision.decision_id:
                raise IdempotencyConflictError(
                    "completed gate intent points to another decision"
                )
            return self.get_decision(decision.decision_id)

        decided_at = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            replay = self._existing_decision_or_reject(connection, decision)
            if replay is None:
                try:
                    connection.execute(
                        """
                        INSERT INTO gate_decisions (
                            id, goal_id, activation_id, gate_type, actor_role,
                            subject_oid, decision, profile_digest, evidence_json,
                            idempotency_key, decided_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            decision.decision_id,
                            decision.goal_id,
                            decision.activation_id,
                            decision.gate_type.value,
                            decision.actor_role,
                            decision.subject_oid,
                            decision.decision.value,
                            decision.profile_digest,
                            compact_json(list(decision.evidence_ids)),
                            decision.idempotency_key,
                            decided_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise GateDecisionConflictError(
                        "gate decision identity or idempotency key is already used"
                    ) from exc
                self._apply_gate_state_transition(connection, decision, validation)
            else:
                decision = replay

        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state=f"GATE_{decision.decision.value}",
            resulting_oid=decision.subject_oid,
            evidence={
                "decision_id": decision.decision_id,
                "gate_type": decision.gate_type.value,
                "validation": validation,
            },
        )
        self._record_audit(
            event_type="GATE_DECISION_RECORDED",
            actor=decision.actor_role,
            subject_type="gate-decision",
            subject_id=decision.decision_id,
            goal_id=decision.goal_id,
            subject_oid=decision.subject_oid,
            idempotency_key=f"audit:{completed.intent_id}",
            payload={
                "intent_id": completed.intent_id,
                "gate_type": decision.gate_type.value,
                "decision": decision.decision.value,
                "activation_id": decision.activation_id,
                "profile_digest": decision.profile_digest,
                "evidence_ids": list(decision.evidence_ids),
            },
        )

        if (
            decision.gate_type in {GateType.QA_QUALITY, GateType.BUILD}
            and decision.decision is not GateDecisionValue.APPROVED
        ):
            finding_id = self._record_post_merge_failure(decision)
            self.request_rework(
                finding_id=finding_id,
                requested_by_role=decision.actor_role,
            )
        return decision

    def get_decision(self, decision_id: str) -> GateDecision:
        decision_id = require_identifier(decision_id, "decision_id")
        with self.state_store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM gate_decisions WHERE id = ?", (decision_id,)
            ).fetchone()
        if row is None:
            raise KeyError(decision_id)
        return self._decision_from_row(row)

    def required_decisions(
        self, goal_id: str, subject_oid: str
    ) -> tuple[GateDecision, ...]:
        """Return the complete candidate-to-requirement approval chain.

        Candidate TA gates naturally reference candidate OIDs, while selection
        references the goal base and post-merge gates reference
        ``subject_oid``.  The method traces the immutable integration attempt so
        callers do not accidentally aggregate unrelated decisions.
        """

        goal_id = require_identifier(goal_id, "goal_id")
        subject_oid = str(require_oid(subject_oid, "subject_oid"))
        with self.state_store.transaction() as connection:
            attempt = self._require_integration_attempt(
                connection, goal_id, subject_oid
            )
            plan = connection.execute(
                "SELECT * FROM integration_plans WHERE id = ?",
                (attempt["plan_id"],),
            ).fetchone()
            if plan is None:
                raise PromotionInvariantError(
                    f"integration attempt {attempt['id']} has no plan"
                )
            candidates = connection.execute(
                """
                SELECT ac.ordinal, ac.candidate_id, ac.candidate_oid
                FROM attempt_candidates AS ac
                WHERE ac.attempt_id = ?
                ORDER BY ac.ordinal
                """,
                (attempt["id"],),
            ).fetchall()
            if not candidates:
                raise PromotionInvariantError("integration attempt has no candidates")

            decisions: list[GateDecision] = []
            for candidate in candidates:
                for gate_type in (
                    GateType.TA_CODE_QUALITY,
                    GateType.TA_ARCHITECTURE,
                ):
                    row = self._one_approved_gate(
                        connection,
                        goal_id=goal_id,
                        gate_type=gate_type,
                        subject_oid=candidate["candidate_oid"],
                    )
                    decisions.append(self._decision_from_row(row))

            selection = connection.execute(
                "SELECT * FROM gate_decisions WHERE id = ?",
                (plan["pl_decision_id"],),
            ).fetchone()
            if selection is None:
                raise PromotionInvariantError("integration plan has no PL decision")
            selection_decision = self._decision_from_row(selection)
            if (
                selection_decision.gate_type
                is not GateType.PL_CANDIDATE_SELECTION
                or selection_decision.decision is not GateDecisionValue.APPROVED
                or selection_decision.goal_id != goal_id
                or selection_decision.subject_oid != plan["base_oid"]
            ):
                raise PromotionInvariantError(
                    "integration plan does not reference an approved PL selection "
                    "for its exact base OID"
                )
            selected_ids = tuple(selection_decision.evidence_ids)
            actual_ids = tuple(row["candidate_id"] for row in candidates)
            if selected_ids != actual_ids:
                raise PromotionInvariantError(
                    "PL selection evidence does not match candidate order"
                )
            decisions.append(selection_decision)

            for gate_type in POST_MERGE_GATE_SEQUENCE:
                row = self._one_approved_gate(
                    connection,
                    goal_id=goal_id,
                    gate_type=gate_type,
                    subject_oid=subject_oid,
                )
                decisions.append(self._decision_from_row(row))
        return tuple(decisions)

    def assert_promotion_invariant(
        self, goal_id: str, subject_oid: str | None = None
    ) -> str:
        """Prove all mandatory gates and return the single full integration OID."""

        goal_id = require_identifier(goal_id, "goal_id")
        with self.state_store.transaction() as connection:
            if subject_oid is None:
                rows = connection.execute(
                    """
                    SELECT DISTINCT subject_oid
                    FROM gate_decisions
                    WHERE goal_id = ?
                      AND gate_type = 'PM_REQUIREMENTS'
                      AND decision = 'APPROVED'
                    """,
                    (goal_id,),
                ).fetchall()
                if len(rows) != 1:
                    raise PromotionInvariantError(
                        "goal must have exactly one PM-approved promotion OID "
                        "when subject_oid is omitted"
                    )
                proven_oid = rows[0]["subject_oid"]
            else:
                proven_oid = str(require_oid(subject_oid, "subject_oid"))

            try:
                attempt = self._require_integration_attempt(
                    connection, goal_id, proven_oid
                )
            except GateEvidenceError as exc:
                raise PromotionInvariantError(str(exc)) from exc
            if attempt["state"] not in {
                IntegrationAttemptState.GATE_PENDING.value,
                IntegrationAttemptState.APPROVED.value,
            }:
                raise PromotionInvariantError(
                    "integration attempt has not reached the approval gate"
                )

            quality = connection.execute(
                """
                SELECT id FROM quality_runs
                WHERE goal_id = ? AND attempt_id = ? AND subject_oid = ?
                  AND state = 'PASSED' AND source_integrity = 'CLEAN'
                """,
                (goal_id, attempt["id"], proven_oid),
            ).fetchall()
            build = connection.execute(
                """
                SELECT id FROM build_runs
                WHERE goal_id = ? AND attempt_id = ? AND subject_oid = ?
                  AND state = 'PASSED' AND source_integrity = 'CLEAN'
                """,
                (goal_id, attempt["id"], proven_oid),
            ).fetchall()
            if not quality or not build:
                raise PromotionInvariantError(
                    "at least one clean passed QA run and Build run are required"
                )

            blocking = connection.execute(
                """
                SELECT id FROM reconciliation_findings
                WHERE resource_id = ?
                  AND state IN ('OPEN', 'RECONCILING', 'QUARANTINED')
                  AND severity IN ('ERROR', 'CRITICAL')
                """,
                (attempt["id"],),
            ).fetchall()
            if blocking:
                raise PromotionInvariantError(
                    "integration attempt has unresolved reconciliation findings"
                )

        decisions = self.required_decisions(goal_id, proven_oid)
        post_merge = {
            decision.gate_type: decision.subject_oid
            for decision in decisions
            if decision.gate_type in POST_MERGE_GATE_SEQUENCE
        }
        if tuple(post_merge) != POST_MERGE_GATE_SEQUENCE:
            raise PromotionInvariantError("mandatory post-merge gate order is incomplete")
        equality = {
            post_merge[GateType.QA_QUALITY],
            post_merge[GateType.BUILD],
            post_merge[GateType.PL_INTEGRATION],
            post_merge[GateType.PM_REQUIREMENTS],
            proven_oid,
        }
        if len(equality) != 1:
            raise PromotionInvariantError(
                "QA, Build, PL integration, PM requirements, and promotion "
                "must reference one exact OID"
            )
        return str(require_oid(proven_oid, "proven_oid"))

    def request_rework(
        self,
        *,
        finding_id: str,
        requested_by_role: str,
    ) -> PLReworkRequest:
        """Route a finding to PL without assigning or editing a revision."""

        finding_id = require_identifier(finding_id, "finding_id")
        requester = require_nonempty(requested_by_role, "requested_by_role")
        if requester not in REWORK_REPORTER_ROLES:
            raise ReworkAuthorityError(
                f"{requester!r} may not create a PL rework request"
            )

        with self.state_store.transaction() as connection:
            source = connection.execute(
                "SELECT * FROM reconciliation_findings WHERE id = ?",
                (finding_id,),
            ).fetchone()
        if source is None:
            raise KeyError(finding_id)

        expected = _json_object(source["expected_json"])
        observed = _json_object(source["observed_json"])
        goal_id = self._optional_identifier(
            observed.get("goal_id") or expected.get("goal_id"), "goal_id"
        )
        attempt_id = self._optional_identifier(
            observed.get("attempt_id") or expected.get("attempt_id"), "attempt_id"
        )
        subject_oid = require_oid(
            observed.get("subject_oid") or expected.get("subject_oid"),
            "subject_oid",
            optional=True,
        )
        preferred_owner = self._preferred_owner(goal_id, attempt_id)
        request_id = _stable_identifier("rework", finding_id)
        idempotency_key = f"pl-rework:{finding_id}"
        request_expected = {
            "owner_role": "pl",
            "required_action": "CREATE_AND_ASSIGN_NEW_WORK_ITEM_REVISION",
            "repair_integration_worktree": False,
            "preferred_owner": preferred_owner,
            "goal_id": goal_id,
            "attempt_id": attempt_id,
            "subject_oid": subject_oid,
        }
        request_observed = {
            "source_finding_id": finding_id,
            "requested_by_role": requester,
            "source_resource_type": source["resource_type"],
            "source_resource_id": source["resource_id"],
            "source_state": source["state"],
            "goal_id": goal_id,
            "attempt_id": attempt_id,
            "subject_oid": subject_oid,
        }

        with self.state_store.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM reconciliation_findings
                WHERE id = ? OR idempotency_key = ?
                """,
                (request_id, idempotency_key),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO reconciliation_findings (
                        id, resource_type, resource_id, severity, state,
                        expected_json, observed_json, idempotency_key, detected_at
                    ) VALUES (?, 'pl-rework-request', ?, ?, 'OPEN', ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        attempt_id or source["resource_id"],
                        source["severity"],
                        compact_json(request_expected),
                        compact_json(request_observed),
                        idempotency_key,
                        utc_now(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM reconciliation_findings WHERE id = ?",
                    (request_id,),
                ).fetchone()
            else:
                if (
                    existing["resource_type"] != "pl-rework-request"
                    or existing["expected_json"] != compact_json(request_expected)
                    or _json_object(existing["observed_json"]).get(
                        "source_finding_id"
                    )
                    != finding_id
                ):
                    raise IdempotencyConflictError(
                        "PL rework request identity was reused with different data"
                    )
                row = existing
        assert row is not None
        request = self._rework_from_row(row)
        self._record_audit(
            event_type="PL_REWORK_REQUESTED",
            actor=requester,
            subject_type="pl-rework-request",
            subject_id=request.request_id,
            goal_id=request.goal_id,
            subject_oid=request.subject_oid,
            idempotency_key=f"audit:{request.idempotency_key}",
            payload={
                "source_finding_id": finding_id,
                "attempt_id": request.attempt_id,
                "preferred_owner": request.preferred_owner,
                "assignment_owner": None,
                "repair_integration_worktree": False,
            },
        )
        return request

    def _existing_decision_or_reject(
        self,
        connection: sqlite3.Connection,
        decision: GateDecision,
    ) -> GateDecision | None:
        rows = connection.execute(
            """
            SELECT * FROM gate_decisions
            WHERE id = ? OR idempotency_key = ?
               OR (
                    goal_id = ? AND gate_type = ? AND subject_oid = ?
               )
            """,
            (
                decision.decision_id,
                decision.idempotency_key,
                decision.goal_id,
                decision.gate_type.value,
                decision.subject_oid,
            ),
        ).fetchall()
        if not rows:
            return None
        if len(rows) == 1:
            stored = self._decision_from_row(rows[0])
            if stored == decision:
                return stored
        raise GateDecisionConflictError(
            "gate decisions are immutable and this gate/OID already has a "
            "duplicate or conflicting decision"
        )

    def _require_authorized_activation(
        self,
        connection: sqlite3.Connection,
        decision: GateDecision,
    ) -> sqlite3.Row:
        expected_role = GATE_AUTHORITIES.get(decision.gate_type)
        if expected_role is None:
            raise GateAuthorityError(
                f"no role authority is registered for {decision.gate_type.value}"
            )
        if decision.actor_role != expected_role:
            raise GateAuthorityError(
                f"{decision.gate_type.value} belongs to {expected_role}, "
                f"not {decision.actor_role}"
            )
        activation = connection.execute(
            "SELECT * FROM activations WHERE id = ?",
            (decision.activation_id,),
        ).fetchone()
        if activation is None:
            raise GateAuthorityError(
                f"activation does not exist: {decision.activation_id}"
            )
        if (
            activation["goal_id"] != decision.goal_id
            or activation["subject_oid"] != decision.subject_oid
        ):
            raise GateAuthorityError(
                "activation goal/subject OID does not match the gate decision"
            )
        if activation["role"] != expected_role:
            raise GateAuthorityError(
                "activation role/seat does not own this gate authority"
            )
        if activation["state"] not in _DECISION_TERMINAL_ACTIVATION_STATES:
            raise GateSequenceError(
                "gate decision requires persisted activation results"
            )
        profile = connection.execute(
            "SELECT compiled_profile_digest FROM profile_bindings WHERE activation_id = ?",
            (decision.activation_id,),
        ).fetchone()
        if (
            profile is not None
            and profile["compiled_profile_digest"] != decision.profile_digest
        ):
            raise GateEvidenceError(
                "decision profile digest differs from the activation binding"
            )
        return activation

    def _validate_sequence_and_evidence(
        self,
        connection: sqlite3.Connection,
        decision: GateDecision,
        activation: sqlite3.Row,
    ) -> dict[str, Any]:
        if decision.gate_type in {
            GateType.TA_CODE_QUALITY,
            GateType.TA_ARCHITECTURE,
        }:
            return self._validate_ta_gate(connection, decision, activation)
        if decision.gate_type is GateType.PL_CANDIDATE_SELECTION:
            return self._validate_candidate_selection(connection, decision)
        if decision.gate_type is GateType.QA_QUALITY:
            return self._validate_quality_gate(connection, decision)
        if decision.gate_type is GateType.BUILD:
            return self._validate_build_gate(connection, decision)
        if decision.gate_type is GateType.PL_INTEGRATION:
            return self._validate_pl_integration_gate(connection, decision)
        if decision.gate_type is GateType.PM_REQUIREMENTS:
            return self._validate_pm_requirements_gate(connection, decision)
        raise GateAuthorityError(f"unsupported gate: {decision.gate_type.value}")

    def _validate_ta_gate(
        self,
        connection: sqlite3.Connection,
        decision: GateDecision,
        activation: sqlite3.Row,
    ) -> dict[str, Any]:
        candidates = connection.execute(
            """
            SELECT
                c.id, c.state, c.candidate_oid,
                wr.owner AS revision_owner,
                wl.owner AS lease_owner
            FROM candidate_submissions AS c
            JOIN work_revisions AS wr ON wr.id = c.revision_id
            JOIN workspace_leases AS wl ON wl.id = c.lease_id
            WHERE c.goal_id = ? AND c.candidate_oid = ?
            """,
            (decision.goal_id, decision.subject_oid),
        ).fetchall()
        if len(candidates) != 1:
            raise GateEvidenceError(
                "TA gate subject must resolve to exactly one submitted candidate"
            )
        candidate = candidates[0]
        actor_seat = activation["role"]
        result = _json_object(activation["result_json"])
        if isinstance(result.get("seat_id"), str):
            actor_seat = result["seat_id"]
        if actor_seat in {
            candidate["revision_owner"],
            candidate["lease_owner"],
        } or decision.activation_id in {
            candidate["revision_owner"],
            candidate["lease_owner"],
        }:
            raise GateSelfApprovalError(
                "candidate owner may not approve its own TA gate"
            )

        review_type = (
            "CODE_QUALITY"
            if decision.gate_type is GateType.TA_CODE_QUALITY
            else "ARCHITECTURE"
        )
        placeholders = ",".join("?" for _ in decision.evidence_ids)
        reviews = connection.execute(
            f"""
            SELECT * FROM reviews
            WHERE id IN ({placeholders})
              AND goal_id = ?
              AND candidate_id = ?
              AND activation_id = ?
              AND reviewer_role = 'ta'
              AND review_type = ?
              AND subject_oid = ?
            """,
            (
                *decision.evidence_ids,
                decision.goal_id,
                candidate["id"],
                decision.activation_id,
                review_type,
                decision.subject_oid,
            ),
        ).fetchall()
        if len(reviews) != 1:
            raise GateEvidenceError(
                "TA decision must cite exactly one matching review record"
            )
        review = reviews[0]
        if decision.decision is GateDecisionValue.APPROVED:
            if (
                review["decision"] != "APPROVED"
                or review["source_integrity"] != SourceIntegrity.CLEAN.value
            ):
                raise GateEvidenceError(
                    "TA approval requires an approved CLEAN exact-OID review"
                )
        elif review["decision"] == "APPROVED":
            raise GateEvidenceError(
                "negative TA gate cannot cite an approved review"
            )
        self._require_extra_evidence_exists(
            connection,
            decision.evidence_ids,
            essential_ids={review["id"]},
        )
        return {
            "candidate_id": candidate["id"],
            "review_id": review["id"],
            "review_type": review_type,
            "source_integrity": review["source_integrity"],
        }

    def _validate_candidate_selection(
        self,
        connection: sqlite3.Connection,
        decision: GateDecision,
    ) -> dict[str, Any]:
        goal = connection.execute(
            "SELECT base_oid FROM goals WHERE id = ?", (decision.goal_id,)
        ).fetchone()
        if goal is None or goal["base_oid"] != decision.subject_oid:
            raise GateEvidenceError(
                "PL candidate selection must reference the exact goal base OID"
            )
        if decision.decision is not GateDecisionValue.APPROVED:
            self._require_extra_evidence_exists(
                connection, decision.evidence_ids, essential_ids=set()
            )
            return {"candidate_ids": [], "candidate_oids": []}

        candidate_rows = []
        for candidate_id in decision.evidence_ids:
            row = connection.execute(
                """
                SELECT * FROM candidate_submissions
                WHERE id = ? AND goal_id = ?
                """,
                (candidate_id, decision.goal_id),
            ).fetchone()
            if row is None:
                raise GateEvidenceError(
                    "PL selection evidence must be ordered candidate IDs only"
                )
            if row["state"] != "APPROVED":
                raise GateSequenceError(
                    f"candidate {candidate_id} has not passed both TA gates"
                )
            for gate_type in (
                GateType.TA_CODE_QUALITY,
                GateType.TA_ARCHITECTURE,
            ):
                self._one_approved_gate(
                    connection,
                    goal_id=decision.goal_id,
                    gate_type=gate_type,
                    subject_oid=row["candidate_oid"],
                    error_type=GateSequenceError,
                )
            candidate_rows.append(row)
        if not candidate_rows:
            raise GateEvidenceError("PL selection requires at least one candidate")
        return {
            "candidate_ids": [row["id"] for row in candidate_rows],
            "candidate_oids": [row["candidate_oid"] for row in candidate_rows],
        }

    def _validate_quality_gate(
        self,
        connection: sqlite3.Connection,
        decision: GateDecision,
    ) -> dict[str, Any]:
        attempt = self._require_integration_attempt(
            connection, decision.goal_id, decision.subject_oid
        )
        if attempt["state"] not in {
            IntegrationAttemptState.MERGED.value,
            IntegrationAttemptState.QA_PENDING.value,
            IntegrationAttemptState.QA_FAILED.value,
        }:
            raise GateSequenceError("QA gate requires a completed clean integration")
        run = self._require_run_evidence(
            connection,
            table="quality_runs",
            ids=decision.evidence_ids,
            goal_id=decision.goal_id,
            attempt_id=attempt["id"],
            subject_oid=decision.subject_oid,
            approved=decision.decision is GateDecisionValue.APPROVED,
        )
        self._require_extra_evidence_exists(
            connection, decision.evidence_ids, essential_ids={run["id"]}
        )
        return {"attempt_id": attempt["id"], "quality_run_id": run["id"]}

    def _validate_build_gate(
        self,
        connection: sqlite3.Connection,
        decision: GateDecision,
    ) -> dict[str, Any]:
        attempt = self._require_integration_attempt(
            connection, decision.goal_id, decision.subject_oid
        )
        qa = self._one_approved_gate(
            connection,
            goal_id=decision.goal_id,
            gate_type=GateType.QA_QUALITY,
            subject_oid=decision.subject_oid,
            error_type=GateSequenceError,
        )
        if attempt["state"] not in {
            IntegrationAttemptState.QA_PASSED.value,
            IntegrationAttemptState.BUILD_PENDING.value,
            IntegrationAttemptState.BUILD_FAILED.value,
        }:
            raise GateSequenceError("Build gate requires the exact-OID QA approval")
        run = self._require_run_evidence(
            connection,
            table="build_runs",
            ids=decision.evidence_ids,
            goal_id=decision.goal_id,
            attempt_id=attempt["id"],
            subject_oid=decision.subject_oid,
            approved=decision.decision is GateDecisionValue.APPROVED,
        )
        self._require_extra_evidence_exists(
            connection, decision.evidence_ids, essential_ids={run["id"]}
        )
        return {
            "attempt_id": attempt["id"],
            "qa_decision_id": qa["id"],
            "build_run_id": run["id"],
        }

    def _validate_pl_integration_gate(
        self,
        connection: sqlite3.Connection,
        decision: GateDecision,
    ) -> dict[str, Any]:
        attempt = self._require_integration_attempt(
            connection, decision.goal_id, decision.subject_oid
        )
        if decision.decision is GateDecisionValue.APPROVED:
            qa = self._one_approved_gate(
                connection,
                goal_id=decision.goal_id,
                gate_type=GateType.QA_QUALITY,
                subject_oid=decision.subject_oid,
                error_type=GateSequenceError,
            )
            build = self._one_approved_gate(
                connection,
                goal_id=decision.goal_id,
                gate_type=GateType.BUILD,
                subject_oid=decision.subject_oid,
                error_type=GateSequenceError,
            )
            required = {qa["id"], build["id"]}
            if not required.issubset(decision.evidence_ids):
                raise GateEvidenceError(
                    "PL integration approval must cite QA and Build decisions"
                )
        else:
            required = set()
        self._require_extra_evidence_exists(
            connection, decision.evidence_ids, essential_ids=required
        )
        return {
            "attempt_id": attempt["id"],
            "prerequisite_decision_ids": sorted(required),
        }

    def _validate_pm_requirements_gate(
        self,
        connection: sqlite3.Connection,
        decision: GateDecision,
    ) -> dict[str, Any]:
        attempt = self._require_integration_attempt(
            connection, decision.goal_id, decision.subject_oid
        )
        if decision.decision is GateDecisionValue.APPROVED:
            required = set()
            for gate_type in (
                GateType.QA_QUALITY,
                GateType.BUILD,
                GateType.PL_INTEGRATION,
            ):
                row = self._one_approved_gate(
                    connection,
                    goal_id=decision.goal_id,
                    gate_type=gate_type,
                    subject_oid=decision.subject_oid,
                    error_type=GateSequenceError,
                )
                required.add(row["id"])
            if not required.issubset(decision.evidence_ids):
                raise GateEvidenceError(
                    "PM requirement approval must cite QA, Build, and PL "
                    "integration decisions"
                )
        else:
            required = set()
        self._require_extra_evidence_exists(
            connection, decision.evidence_ids, essential_ids=required
        )
        return {
            "attempt_id": attempt["id"],
            "prerequisite_decision_ids": sorted(required),
        }

    def _require_run_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        table: str,
        ids: Sequence[str],
        goal_id: str,
        attempt_id: str,
        subject_oid: str,
        approved: bool,
    ) -> sqlite3.Row:
        if table not in {"quality_runs", "build_runs"}:
            raise ValueError(f"unsupported run table: {table}")
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"""
            SELECT * FROM {table}
            WHERE id IN ({placeholders})
              AND goal_id = ?
              AND attempt_id = ?
              AND subject_oid = ?
            """,
            (*ids, goal_id, attempt_id, subject_oid),
        ).fetchall()
        if len(rows) != 1:
            raise GateEvidenceError(
                f"gate must cite exactly one matching {table} record"
            )
        row = rows[0]
        if approved:
            if row["state"] != "PASSED" or row["source_integrity"] != "CLEAN":
                raise GateEvidenceError(
                    "approval requires a passed CLEAN exact-OID execution"
                )
        elif row["state"] not in {"FAILED", "INVALIDATED"}:
            raise GateEvidenceError(
                "negative decision requires failed or invalidated execution evidence"
            )
        return row

    def _require_extra_evidence_exists(
        self,
        connection: sqlite3.Connection,
        evidence_ids: Sequence[str],
        *,
        essential_ids: set[str],
    ) -> None:
        for evidence_id in evidence_ids:
            if evidence_id in essential_ids:
                continue
            found = False
            for table in (
                "artifacts",
                "reviews",
                "quality_runs",
                "build_runs",
                "gate_decisions",
                "candidate_submissions",
                "integration_attempts",
                "reconciliation_findings",
                "audit_events",
            ):
                if (
                    connection.execute(
                        f"SELECT 1 FROM {table} WHERE id = ?", (evidence_id,)
                    ).fetchone()
                    is not None
                ):
                    found = True
                    break
            if not found:
                raise GateEvidenceError(
                    f"evidence does not exist in the AX state store: {evidence_id}"
                )

    def _apply_gate_state_transition(
        self,
        connection: sqlite3.Connection,
        decision: GateDecision,
        validation: Mapping[str, Any],
    ) -> None:
        approved = decision.decision is GateDecisionValue.APPROVED
        if decision.gate_type in {
            GateType.TA_CODE_QUALITY,
            GateType.TA_ARCHITECTURE,
        }:
            candidate_id = str(validation["candidate_id"])
            if not approved:
                connection.execute(
                    """
                    UPDATE candidate_submissions SET state = 'REJECTED'
                    WHERE id = ? AND state NOT IN ('SUPERSEDED')
                    """,
                    (candidate_id,),
                )
                return
            candidate_oid = decision.subject_oid
            approvals = connection.execute(
                """
                SELECT gate_type FROM gate_decisions
                WHERE goal_id = ? AND subject_oid = ? AND decision = 'APPROVED'
                  AND gate_type IN ('TA_CODE_QUALITY', 'TA_ARCHITECTURE')
                """,
                (decision.goal_id, candidate_oid),
            ).fetchall()
            if {row["gate_type"] for row in approvals} == {
                GateType.TA_CODE_QUALITY.value,
                GateType.TA_ARCHITECTURE.value,
            }:
                connection.execute(
                    """
                    UPDATE candidate_submissions SET state = 'APPROVED'
                    WHERE id = ? AND state IN ('SUBMITTED', 'REVIEW_PENDING', 'APPROVED')
                    """,
                    (candidate_id,),
                )
            else:
                connection.execute(
                    """
                    UPDATE candidate_submissions SET state = 'REVIEW_PENDING'
                    WHERE id = ? AND state = 'SUBMITTED'
                    """,
                    (candidate_id,),
                )
            return

        attempt_id = validation.get("attempt_id")
        if not isinstance(attempt_id, str):
            return
        if decision.gate_type is GateType.QA_QUALITY:
            state = "BUILD_PENDING" if approved else "QA_FAILED"
        elif decision.gate_type is GateType.BUILD:
            state = "GATE_PENDING" if approved else "BUILD_FAILED"
        elif decision.gate_type is GateType.PL_INTEGRATION and approved:
            state = "APPROVED"
        else:
            return
        connection.execute(
            "UPDATE integration_attempts SET state = ? WHERE id = ?",
            (state, attempt_id),
        )

    def _record_post_merge_failure(self, decision: GateDecision) -> str:
        with self.state_store.transaction() as connection:
            attempt = self._require_integration_attempt(
                connection, decision.goal_id, decision.subject_oid
            )
        finding_id = _stable_identifier("finding", "gate-failure", decision.decision_id)
        idempotency_key = f"gate-failure:{decision.decision_id}"
        expected = {
            "goal_id": decision.goal_id,
            "attempt_id": attempt["id"],
            "subject_oid": decision.subject_oid,
            "next_owner_role": "pl",
            "required_action": "CREATE_NEW_REVISION_AND_NEW_INTEGRATION_ATTEMPT",
            "repair_integration_worktree": False,
        }
        observed = {
            "goal_id": decision.goal_id,
            "attempt_id": attempt["id"],
            "subject_oid": decision.subject_oid,
            "failed_gate": decision.gate_type.value,
            "decision_id": decision.decision_id,
            "decision": decision.decision.value,
            "evidence_ids": list(decision.evidence_ids),
        }
        with self.state_store.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM reconciliation_findings
                WHERE id = ? OR idempotency_key = ?
                """,
                (finding_id, idempotency_key),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO reconciliation_findings (
                        id, resource_type, resource_id, severity, state,
                        expected_json, observed_json, idempotency_key, detected_at
                    ) VALUES (
                        ?, 'post-merge-gate-failure', ?, 'ERROR', 'OPEN',
                        ?, ?, ?, ?
                    )
                    """,
                    (
                        finding_id,
                        attempt["id"],
                        compact_json(expected),
                        compact_json(observed),
                        idempotency_key,
                        utc_now(),
                    ),
                )
            elif (
                existing["expected_json"] != compact_json(expected)
                or existing["observed_json"] != compact_json(observed)
            ):
                raise IdempotencyConflictError(
                    "post-merge failure finding was reused with different evidence"
                )
        return finding_id

    def _preferred_owner(
        self, goal_id: str | None, attempt_id: str | None
    ) -> str | None:
        if goal_id is None or attempt_id is None:
            return None
        with self.state_store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT wi.assigned_owner
                FROM attempt_candidates AS ac
                JOIN candidate_submissions AS c ON c.id = ac.candidate_id
                JOIN work_items AS wi ON wi.id = c.work_item_id
                WHERE ac.attempt_id = ? AND wi.goal_id = ?
                ORDER BY wi.assigned_owner
                """,
                (attempt_id, goal_id),
            ).fetchall()
        return rows[0]["assigned_owner"] if len(rows) == 1 else None

    @staticmethod
    def _optional_identifier(value: Any, field: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise GateEvidenceError(f"{field} evidence must be a string")
        return require_identifier(value, field)

    def _rework_from_row(self, row: sqlite3.Row) -> PLReworkRequest:
        expected = _json_object(row["expected_json"])
        observed = _json_object(row["observed_json"])
        return PLReworkRequest(
            request_id=row["id"],
            finding_id=str(observed["source_finding_id"]),
            requested_by_role=str(observed["requested_by_role"]),
            goal_id=self._optional_identifier(observed.get("goal_id"), "goal_id"),
            attempt_id=self._optional_identifier(
                observed.get("attempt_id"), "attempt_id"
            ),
            subject_oid=observed.get("subject_oid"),
            preferred_owner=expected.get("preferred_owner"),
            state=row["state"],
            idempotency_key=row["idempotency_key"],
        )

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> GateDecision:
        return GateDecision(
            decision_id=row["id"],
            goal_id=row["goal_id"],
            activation_id=row["activation_id"],
            gate_type=GateType(row["gate_type"]),
            actor_role=row["actor_role"],
            subject_oid=row["subject_oid"],
            decision=GateDecisionValue(row["decision"]),
            profile_digest=row["profile_digest"],
            evidence_ids=tuple(_json_array(row["evidence_json"])),
            idempotency_key=row["idempotency_key"],
        )

    @staticmethod
    def _require_integration_attempt(
        connection: sqlite3.Connection,
        goal_id: str,
        subject_oid: str,
    ) -> sqlite3.Row:
        rows = connection.execute(
            """
            SELECT * FROM integration_attempts
            WHERE goal_id = ? AND result_oid = ?
            ORDER BY created_at DESC, id DESC
            """,
            (goal_id, subject_oid),
        ).fetchall()
        if not rows:
            raise GateEvidenceError(
                "subject OID is not a retained integration result for this goal"
            )
        return rows[0]

    @staticmethod
    def _one_approved_gate(
        connection: sqlite3.Connection,
        *,
        goal_id: str,
        gate_type: GateType,
        subject_oid: str,
        error_type: type[GateCoordinatorError] = PromotionInvariantError,
    ) -> sqlite3.Row:
        rows = connection.execute(
            """
            SELECT * FROM gate_decisions
            WHERE goal_id = ? AND gate_type = ? AND subject_oid = ?
              AND decision = 'APPROVED'
            """,
            (goal_id, gate_type.value, subject_oid),
        ).fetchall()
        if len(rows) != 1:
            raise error_type(
                f"exactly one approved {gate_type.value} decision is required "
                f"for {subject_oid}"
            )
        return rows[0]

    def _record_audit(
        self,
        *,
        event_type: str,
        actor: str,
        subject_type: str,
        subject_id: str,
        goal_id: str | None,
        subject_oid: str | None,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> None:
        event_id = _stable_identifier("audit", idempotency_key)
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=event_id,
                event_type=event_type,
                actor=actor,
                subject_type=subject_type,
                subject_id=subject_id,
                goal_id=goal_id,
                subject_oid=subject_oid,
                payload=payload,
                occurred_at=utc_now(),
                idempotency_key=idempotency_key,
            )
        )
