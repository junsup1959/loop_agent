from __future__ import annotations

"""Deterministic immutable integration attempts for Agent-Team worktrees.

PL supplies the already-approved candidate set and ordering.  This controller
performs only mechanical Git operations inside an AX-owned disposable worktree;
it never edits source to resolve conflicts and owns no approval authority.
"""

import hashlib
import json
import os
import platform
import re
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from .agent_team_domain import (
        AuditEvent,
        GateDecisionValue,
        GateType,
        IntegrationAttempt,
        IntegrationAttemptState,
        IntegrationPlan,
        IntegrationPlanState,
        IntentStatus,
        ServiceIdentity,
        require_identifier,
        require_nonempty,
        require_oid,
    )
    from .agent_team_gates import GateCoordinator
    from .agent_team_git import (
        EVIDENCE_REF_PREFIX,
        GitCommandError,
        GitCommandResult,
        GitCommandRunner,
        GitExecutor,
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
    from .agent_team_workflow import WorkflowDefinitionError, WorkflowDefinitions
except ImportError:  # Direct script/module execution from the repository root.
    from agent_team_domain import (
        AuditEvent,
        GateDecisionValue,
        GateType,
        IntegrationAttempt,
        IntegrationAttemptState,
        IntegrationPlan,
        IntegrationPlanState,
        IntentStatus,
        ServiceIdentity,
        require_identifier,
        require_nonempty,
        require_oid,
    )
    from agent_team_gates import GateCoordinator
    from agent_team_git import (
        EVIDENCE_REF_PREFIX,
        GitCommandError,
        GitCommandResult,
        GitCommandRunner,
        GitExecutor,
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
    from agent_team_workflow import WorkflowDefinitionError, WorkflowDefinitions


SUPPORTED_MERGE_STRATEGIES = frozenset({"no-ff", "ort", "merge-ort"})
MAX_NARROWING_REPLAYS = 32
CONTROLLER_NAME = "Agentic AX Integration Controller"
CONTROLLER_EMAIL = "integration-controller@agentic-ax.invalid"
DETERMINISTIC_COMMIT_TIME = 946684800  # 2000-01-01T00:00:00Z

BOUNDARY_INTENT_RECORDED = "intent-recorded"
BOUNDARY_WORKTREE_READY = "worktree-ready"
BOUNDARY_STEP_EFFECT_APPLIED = "step-effect-applied"
BOUNDARY_STEP_RECORDED = "step-recorded"
BOUNDARY_FINAL_REF_CREATED = "final-ref-created"
BOUNDARY_RESULT_RECORDED = "result-recorded"
BOUNDARY_WORKTREE_REMOVED = "worktree-removed"

BoundaryHook = Callable[[str, str], None]
ReplayProbe = Callable[[str, tuple[str, ...]], bool]

_URI_CREDENTIAL = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:access_token|auth|key|password|signature|token)=)[^&\s]+"
)
_REF_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

DELIVERY_WORKFLOW_ID = "delivery-v4"
DELIVERY_WORKFLOW_VERSION = "4.0.0"
TA_REVIEW_TRANSITION = "ta_review_exact_oid"


class IntegrationControllerError(RuntimeError):
    """Base error for immutable integration attempts."""


class IntegrationAuthorizationError(IntegrationControllerError):
    """The plan is not the candidate order authorized by PL and TA."""


class IntegrationAttemptConflictError(IntegrationControllerError):
    """An attempt or plan identity was reused with different immutable input."""


class IntegrationExecutionError(IntegrationControllerError):
    """A mechanical Git step failed without a source-level merge conflict."""


class IntegrationRecoveryError(IntegrationControllerError):
    """Interrupted state cannot be reconciled without quarantine or PL action."""


class IntegrationReplayLimitError(IntegrationControllerError, ValueError):
    """A narrowing request is invalid or exceeds its explicit replay bound."""


class IntegrationControllerInterrupted(BaseException):
    """Failure-injection exception that simulates process termination.

    It derives from :class:`BaseException` so generic application exception
    handlers do not accidentally convert a simulated process death into a
    completed failure decision.
    """


class DeliveryV4EvidenceError(ValueError):
    """Persisted evidence is not one exact admitted delivery-v4 result."""


@dataclass(frozen=True, slots=True)
class DeliveryV4ResultEvidence:
    contract_id: str
    result_id: str
    attempt_id: str
    transition_id: str
    capability_id: str
    goal_id: str
    run_id: str
    base_oid: str
    subject_oid: str
    result_oid: str
    profile_binding_id: str
    compiled_profile_digest: str
    mcp_binding_ids: tuple[str, ...]
    mcp_receipt_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IntegrationLeaseBinding:
    lease_id: str
    sandbox_binding_id: str
    repository_id: str
    goal_id: str
    run_id: str
    slot_id: str
    worker_assignment_id: str
    branch_ref: str
    worktree_path: str
    base_authority_id: str
    approved_authority_ids: tuple[str, ...]


def require_delivery_v4_result_evidence(
    state_store: AxStateStore,
    *,
    goal_id: str,
    subject_oid: str,
    transition_id: str,
    capability_id: str,
    result_kinds: Sequence[str],
    expected_result_oid: str | None = None,
) -> DeliveryV4ResultEvidence:
    """Return the unique exact-OID result committed through the v4 ledger.

    This is a read-only proof seam.  It deliberately does not synthesize an
    admission, result, profile binding, or MCP receipt on behalf of a caller.
    """

    if not isinstance(state_store, AxStateStore):
        raise TypeError("state_store must be an AxStateStore")
    goal = require_identifier(goal_id, "goal_id")
    subject = str(require_oid(subject_oid, "subject_oid"))
    transition_key = require_identifier(transition_id, "transition_id")
    capability_key = require_identifier(capability_id, "capability_id")
    allowed_results = tuple(
        require_identifier(value, "result_kind") for value in result_kinds
    )
    if not allowed_results or len(set(allowed_results)) != len(allowed_results):
        raise DeliveryV4EvidenceError("result_kinds must be unique and non-empty")
    expected_result = str(
        require_oid(
            expected_result_oid if expected_result_oid is not None else subject,
            "expected_result_oid",
        )
    )
    try:
        definitions = WorkflowDefinitions.load()
        transition = definitions.transition(transition_key)
    except (OSError, WorkflowDefinitionError) as exc:
        raise DeliveryV4EvidenceError(
            "the authoritative delivery-v4 definition cannot be loaded"
        ) from exc
    if (
        definitions.workflow.get("id") != DELIVERY_WORKFLOW_ID
        or definitions.workflow.get("version") != DELIVERY_WORKFLOW_VERSION
        or capability_key not in transition.capabilities
        or not set(allowed_results) <= set(transition.result_kinds)
    ):
        raise DeliveryV4EvidenceError(
            "requested proof does not match the active delivery-v4 transition"
        )

    with state_store.transaction() as connection:
        rows = connection.execute(
            """
            SELECT ac.id AS contract_id, ac.goal_id, ac.run_id,
                   ac.base_oid, ac.subject_oid, ac.contract_digest,
                   ac.state AS contract_state,
                   wt.transition_key, wt.failure_route,
                   lc.capability_key,
                   wd.workflow_key, wd.version AS workflow_version,
                   rd.sha256 AS workflow_sha256,
                   ca.decision AS admission_decision,
                   ca.contract_digest AS admission_contract_digest,
                   cp.id AS profile_binding_id,
                   cp.compiled_profile_digest, cp.state AS profile_state,
                   attempt.id AS attempt_id,
                   result.id AS result_id, result.disposition,
                   result.result_kind, result.payload_json,
                   rl.lease_kind, rl.state AS lease_state,
                   sb.source_read_only, sb.subject_oid AS sandbox_subject_oid,
                   oa.authority_kind, oa.oid AS authority_oid,
                   oa.state AS authority_state
            FROM activation_contracts AS ac
            JOIN workflow_transitions AS wt
              ON wt.id = ac.workflow_transition_id AND wt.state = 'ACTIVE'
            JOIN workflow_definitions AS wd
              ON wd.id = wt.workflow_definition_id AND wd.state = 'ACTIVE'
            JOIN registered_definitions AS rd
              ON rd.id = wd.definition_id AND rd.kind = 'WORKFLOW'
            JOIN logical_capabilities AS lc
              ON lc.id = ac.capability_id AND lc.state = 'ACTIVE'
            JOIN contract_admissions AS ca
              ON ca.contract_id = ac.id
            JOIN contract_profile_bindings AS cp
              ON cp.contract_id = ac.id
            JOIN contract_attempts AS attempt
              ON attempt.contract_id = ac.id
            JOIN activation_results AS result
              ON result.attempt_id = attempt.id
            JOIN runtime_leases AS rl ON rl.id = ac.lease_id
            JOIN sandbox_bindings AS sb ON sb.id = ac.sandbox_binding_id
            JOIN oid_authorities AS oa ON oa.id = ac.oid_authority_id
            WHERE ac.goal_id = ? AND ac.subject_oid = ?
              AND wt.transition_key = ?
              AND lc.capability_key = ?
              AND result.disposition = 'ACCEPTED'
            ORDER BY result.recorded_at, result.id
            """,
            (goal, subject, transition_key, capability_key),
        ).fetchall()
        if len(rows) != 1:
            raise DeliveryV4EvidenceError(
                "exactly one accepted delivery-v4 result is required"
            )
        row = rows[0]
        if (
            row["workflow_key"] != DELIVERY_WORKFLOW_ID
            or row["workflow_version"] != DELIVERY_WORKFLOW_VERSION
            or row["workflow_sha256"] != definitions.workflow_sha256
            or row["failure_route"] != transition.failure_state
            or row["admission_decision"] != "ACCEPTED"
            or row["admission_contract_digest"] != row["contract_digest"]
            or row["contract_state"] not in {"RESULT_RECORDED", "COMPLETED"}
            or row["profile_state"] != "BOUND"
            or row["result_kind"] not in allowed_results
            or row["lease_kind"] != "REVIEW"
            or row["lease_state"] not in {"ACTIVE", "RELEASED"}
            or row["source_read_only"] != 1
            or row["sandbox_subject_oid"] != subject
            or row["authority_kind"] != "SUBJECT"
            or row["authority_oid"] != subject
            or row["authority_state"] not in {"ACTIVE", "SUPERSEDED"}
        ):
            raise DeliveryV4EvidenceError(
                "delivery-v4 contract, profile, or exact-OID authority changed"
            )
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise DeliveryV4EvidenceError(
                "accepted activation result payload is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise DeliveryV4EvidenceError(
                "accepted activation result payload must be an object"
            )
        result_oid = payload.get("result_oid")
        evidence_refs = payload.get("evidence_refs")
        if (
            payload.get("contract_id") != row["contract_id"]
            or payload.get("transition_id") != transition_key
            or payload.get("capability_id") != capability_key
            or payload.get("subject_oid") != subject
            or payload.get("result_kind") != row["result_kind"]
            or result_oid != expected_result
            or not isinstance(evidence_refs, list)
            or not evidence_refs
            or any(not isinstance(value, str) or not value for value in evidence_refs)
        ):
            raise DeliveryV4EvidenceError(
                "accepted result identity, exact OID, or evidence refs differ"
            )
        blocking_violation = connection.execute(
            """
            SELECT 1 FROM contract_violations
            WHERE contract_id = ? AND violation_code <> 'FORMAT'
            LIMIT 1
            """,
            (row["contract_id"],),
        ).fetchone()
        if blocking_violation is not None:
            raise DeliveryV4EvidenceError(
                "accepted result has a blocking contract violation"
            )
        mcp_rows = connection.execute(
            """
            SELECT cmb.id, cmb.required_availability,
                   cmb.invocation_required, cmb.trigger_rule,
                   md.server_name, md.tool_name,
                   EXISTS (
                       SELECT 1 FROM mcp_health_observations AS health
                       WHERE health.mcp_definition_id = md.id
                         AND health.status = 'HEALTHY'
                         AND (health.contract_id IS NULL
                              OR health.contract_id = cmb.contract_id)
                   ) AS healthy,
                   receipt.id AS receipt_id
            FROM contract_mcp_bindings AS cmb
            JOIN mcp_definitions AS md ON md.id = cmb.mcp_definition_id
            LEFT JOIN mcp_usage_receipts AS receipt
              ON receipt.contract_id = cmb.contract_id
             AND receipt.attempt_id = ?
             AND receipt.mcp_binding_id = cmb.id
             AND receipt.tool_name = md.tool_name
            WHERE cmb.contract_id = ?
            ORDER BY cmb.id
            """,
            (row["attempt_id"], row["contract_id"]),
        ).fetchall()
        required_servers = set(
            definitions.mcp_policy.get("policy", {}).get("required_servers", [])
        )
        available_servers = {
            item["server_name"]
            for item in mcp_rows
            if item["required_availability"] == 1
        }
        required_use = {
            item["trigger_rule"]
            for item in mcp_rows
            if item["invocation_required"] == 1
        }
        if (
            not required_servers
            or not required_servers <= available_servers
            or any(
                item["required_availability"] == 1 and item["healthy"] != 1
                for item in mcp_rows
            )
            or required_use != set(transition.mcp_required_use_binding_ids)
            or any(
                item["invocation_required"] == 1
                and item["receipt_id"] is None
                for item in mcp_rows
            )
        ):
            raise DeliveryV4EvidenceError(
                "delivery-v4 MCP health, binding, or usage receipt is incomplete"
            )
        return DeliveryV4ResultEvidence(
            contract_id=row["contract_id"],
            result_id=row["result_id"],
            attempt_id=row["attempt_id"],
            transition_id=transition_key,
            capability_id=capability_key,
            goal_id=row["goal_id"],
            run_id=row["run_id"],
            base_oid=row["base_oid"],
            subject_oid=subject,
            result_oid=expected_result,
            profile_binding_id=row["profile_binding_id"],
            compiled_profile_digest=row["compiled_profile_digest"],
            mcp_binding_ids=tuple(item["id"] for item in mcp_rows),
            mcp_receipt_ids=tuple(
                item["receipt_id"]
                for item in mcp_rows
                if item["receipt_id"] is not None
            ),
            evidence_refs=tuple(evidence_refs),
        )


@dataclass(frozen=True, slots=True)
class UnmergedIndexEntry:
    mode: str
    oid: str
    stage: int
    path: str


@dataclass(frozen=True, slots=True)
class ConflictEvidence:
    attempt_id: str
    base_oid: str
    candidate_oid: str
    candidate_ordinal: int
    ordered_candidate_oids: tuple[str, ...]
    merge_bases: tuple[str, ...]
    partial_head_oid: str
    conflict_paths: tuple[str, ...]
    unmerged_index: tuple[UnmergedIndexEntry, ...]
    commands: tuple[Mapping[str, Any], ...]
    git_version: str
    environment_fingerprint: str
    failed_ref: str


@dataclass(frozen=True, slots=True)
class ReplayObservation:
    attempt_id: str
    candidate_oids: tuple[str, ...]
    result_oid: str | None
    state: IntegrationAttemptState
    probe_failed: bool | None


@dataclass(frozen=True, slots=True)
class CandidateFailureSet:
    """Bounded replay observations; never an owner assignment."""

    source_attempt_id: str
    candidate_oids: tuple[str, ...]
    replay_attempt_ids: tuple[str, ...]
    observations: tuple[ReplayObservation, ...]
    max_replays: int
    exhausted: bool
    assignment_owner: None = None


def _stable_identifier(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _sanitize(value: str) -> str:
    redacted = _URI_CREDENTIAL.sub(r"\1<redacted>@", value)
    redacted = _SENSITIVE_QUERY.sub(r"\1<redacted>", redacted)
    return redacted[:65536]


def _safe_ref_component(value: str) -> str:
    if _REF_COMPONENT.fullmatch(value) and value not in {".", "..", "@"}:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"id-{digest}"


def _command_evidence(result: GitCommandResult) -> dict[str, Any]:
    return {
        "arguments": [_sanitize(argument) for argument in result.arguments],
        "cwd": _sanitize(result.cwd),
        "returncode": result.returncode,
        "stdout": _sanitize(result.stdout),
        "stderr": _sanitize(result.stderr),
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
    }


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntegrationAttemptConflictError(
            "stored integration evidence is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise IntegrationAttemptConflictError(
            "stored integration evidence must be a JSON object"
        )
    return parsed


def _json_array(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntegrationAttemptConflictError(
            "stored candidate order is not valid JSON"
        ) from exc
    if not isinstance(parsed, list):
        raise IntegrationAttemptConflictError(
            "stored candidate order must be a JSON array"
        )
    return parsed


class IntegrationController:
    """Execute PL-authorized merges and retain every attempt as evidence."""

    def __init__(
        self,
        *,
        state_store: AxStateStore,
        repository_service: ManagedRepositoryService,
        path_authority: AxPathAuthority,
        command_runner: GitExecutor | None = None,
        gate_coordinator: GateCoordinator | None = None,
        boundary_hook: BoundaryHook | None = None,
        replay_probe: ReplayProbe | None = None,
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
            raise ValueError("repository service and controller must share state_store")
        if repository_service.path_authority != path_authority:
            raise ValueError(
                "repository service and controller must share path authority"
            )
        self.state_store = state_store
        self.repository_service = repository_service
        self.path_authority = path_authority
        self.command_runner = command_runner or GitCommandRunner()
        self.gate_coordinator = gate_coordinator or GateCoordinator(state_store)
        self.boundary_hook = boundary_hook
        self.replay_probe = replay_probe
        self.state_store.initialize()

    def execute(self, plan: IntegrationPlan) -> IntegrationAttempt:
        """Execute one immutable plan or return its idempotent outcome."""

        return self._execute(plan, replay_of=None, allow_approved_subset=False)

    def recover_interrupted(self, attempt_id: str) -> IntegrationAttempt:
        """Reconcile an interrupted attempt without trusting a pending journal.

        A reachable immutable final ref is completed-but-unrecorded evidence.
        An absent effect is retry-safe and receives a new ``RECREATED`` attempt.
        Ambiguous worktree or ref state is quarantined for human/PL action.
        """

        attempt_id = require_identifier(attempt_id, "attempt_id")
        with self.state_store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM integration_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            target = connection.execute(
                """
                SELECT g.target_id, mr.repository_path
                FROM goals AS g
                JOIN managed_repositories AS mr ON mr.target_id = g.target_id
                WHERE g.id = ?
                """,
                (row["goal_id"],),
            ).fetchone()
            candidates = connection.execute(
                """
                SELECT ordinal, candidate_id, candidate_oid
                FROM attempt_candidates
                WHERE attempt_id = ? ORDER BY ordinal
                """,
                (attempt_id,),
            ).fetchall()
            plan = connection.execute(
                "SELECT * FROM integration_plans WHERE id = ?", (row["plan_id"],)
            ).fetchone()
        if target is None or plan is None or not candidates:
            return self._quarantine_attempt(
                row,
                classification="HUMAN_OR_PL_ACTION_REQUIRED",
                reason="attempt ownership graph is incomplete",
                observed={},
            )

        repository = Path(target["repository_path"]).resolve()
        final_ref = self._integration_ref(row["goal_id"], attempt_id)
        final_oid = self.repository_service._read_direct_ref(  # Phase 4 evidence seam.
            repository,
            final_ref,
            require_commit=True,
            allow_missing=True,
        )
        if final_oid is not None:
            worktree_observed = self._worktree_observation(
                repository, row["goal_id"], attempt_id
            )
            evidence = _json_object(row["evidence_json"])
            binding = self._load_integration_lease_binding(attempt_id)
            integration_authority_id = None
            if binding is not None:
                integration_authority_id = self._record_integration_result_authority(
                    binding,
                    authority_kind="INTEGRATION",
                    oid=final_oid,
                    evidence={
                        "attempt_id": attempt_id,
                        "goal_id": row["goal_id"],
                        "integration_ref": final_ref,
                        "approved_oids": [
                            candidate["candidate_oid"]
                            for candidate in candidates
                        ],
                    },
                )
            evidence.update(
                {
                    "classification": "COMPLETED_BUT_UNRECORDED",
                    "base_oid": row["base_oid"],
                    "subject_oids": [
                        candidate["candidate_oid"] for candidate in candidates
                    ],
                    "approved_oids": [
                        candidate["candidate_oid"] for candidate in candidates
                    ],
                    "integration_candidate_oid": final_oid,
                    "failure_oid": None,
                    "integration_ref": final_ref,
                    "integration_authority_id": integration_authority_id,
                    "recovered_at": utc_now(),
                    "worktree_observed": worktree_observed,
                }
            )
            with self.state_store.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE integration_attempts
                    SET state = 'QA_PENDING', result_oid = ?,
                        evidence_json = ?, completed_at = COALESCE(completed_at, ?)
                    WHERE id = ?
                    """,
                    (final_oid, compact_json(evidence), utc_now(), attempt_id),
                )
                connection.execute(
                    """
                    UPDATE integration_plans
                    SET state = 'COMPLETED'
                    WHERE id = ? AND state IN ('APPROVED', 'EXECUTING', 'COMPLETED')
                    """,
                    (row["plan_id"],),
                )
            self._complete_pending_execute_intent(
                attempt_id=attempt_id,
                result_oid=final_oid,
                resulting_state="QA_PENDING",
                evidence={
                    "classification": "COMPLETED_BUT_UNRECORDED",
                    "integration_ref": final_ref,
                },
            )
            self._cleanup_recovered_worktree(
                target_id=target["target_id"],
                repository=repository,
                goal_id=row["goal_id"],
                attempt_id=attempt_id,
                expected_oid=final_oid,
            )
            self._finish_integration_lease(attempt_id, state="RELEASED")
            self._record_recovery_audit(
                attempt_id,
                row["goal_id"],
                final_oid,
                "COMPLETED_BUT_UNRECORDED",
            )
            return self._load_attempt(attempt_id)

        worktree_observed = self._worktree_observation(
            repository, row["goal_id"], attempt_id
        )
        branch_ref = self._integration_branch(row["goal_id"], attempt_id)
        branch_oid = self.repository_service._read_direct_ref(
            repository,
            branch_ref,
            require_commit=True,
            allow_missing=True,
        )
        if worktree_observed.get("present") or (
            branch_oid is not None and branch_oid != row["base_oid"]
        ):
            return self._quarantine_attempt(
                row,
                classification="HUMAN_OR_PL_ACTION_REQUIRED",
                reason="ambiguous partial Git effect has no immutable final ref",
                observed={
                    "worktree": worktree_observed,
                    "branch_ref": branch_ref,
                    "branch_oid": branch_oid,
                },
            )

        with self.state_store.transaction() as connection:
            step_effects = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM operation_intents
                WHERE operation = 'integration-merge-step'
                  AND status = 'COMPLETED'
                  AND json_extract(payload_json, '$.attempt_id') = ?
                """,
                (attempt_id,),
            ).fetchone()["count"]
        classification = "RETRY_SAFE" if step_effects == 0 else "QUARANTINED"
        if step_effects:
            return self._quarantine_attempt(
                row,
                classification=classification,
                reason="completed step journal has no reachable immutable final ref",
                observed={"completed_merge_steps": step_effects},
            )

        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE integration_attempts
                SET state = 'QUARANTINED', completed_at = COALESCE(completed_at, ?)
                WHERE id = ?
                """,
                (utc_now(), attempt_id),
            )
        self._finish_integration_lease(attempt_id, state="RELEASED")
        finding_id = self._record_recovery_finding(
            attempt_id=attempt_id,
            goal_id=row["goal_id"],
            subject_oid=row["result_oid"],
            classification="RETRY_SAFE",
            severity="WARNING",
            expected={
                "base_oid": row["base_oid"],
                "candidate_oids": [candidate["candidate_oid"] for candidate in candidates],
                "new_attempt_required": True,
            },
            observed={
                "integration_ref": None,
                "worktree": worktree_observed,
                "branch_oid": branch_oid,
                "completed_merge_steps": 0,
            },
        )
        recovery_attempt_id = _stable_identifier("attempt-recovery", attempt_id)
        recovery_key = f"recover-integration:{attempt_id}"
        environment = self._environment_record()
        evidence = {
            "classification": "RETRY_SAFE",
            "recovery_of": attempt_id,
            "recovery_finding_id": finding_id,
            "evidence_ids": [finding_id],
        }
        with self.state_store.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM integration_attempts WHERE id = ?",
                (recovery_attempt_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO integration_attempts (
                        id, plan_id, goal_id, base_oid, merge_strategy, state,
                        evidence_json, environment_json, idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'RECREATED', ?, ?, ?, ?)
                    """,
                    (
                        recovery_attempt_id,
                        row["plan_id"],
                        row["goal_id"],
                        row["base_oid"],
                        row["merge_strategy"],
                        compact_json(evidence),
                        compact_json(environment),
                        recovery_key,
                        utc_now(),
                    ),
                )
                for candidate in candidates:
                    connection.execute(
                        """
                        INSERT INTO attempt_candidates (
                            attempt_id, ordinal, candidate_id, candidate_oid
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            recovery_attempt_id,
                            candidate["ordinal"],
                            candidate["candidate_id"],
                            candidate["candidate_oid"],
                        ),
                    )
            else:
                self._assert_recovery_attempt(
                    existing,
                    source=row,
                    candidates=candidates,
                    recovery_key=recovery_key,
                )
        self._record_recovery_audit(
            recovery_attempt_id,
            row["goal_id"],
            None,
            "RETRY_SAFE_RECREATED",
        )
        return self._load_attempt(recovery_attempt_id)

    def _load_integration_lease_binding(
        self, attempt_id: str
    ) -> IntegrationLeaseBinding | None:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT rl.*, sb.id AS sandbox_binding_id
                FROM runtime_leases AS rl
                JOIN sandbox_bindings AS sb ON sb.lease_id = rl.id
                WHERE rl.idempotency_key = ?
                  AND rl.lease_kind = 'INTEGRATION'
                """,
                (f"integration-lease:{attempt_id}",),
            ).fetchone()
            if row is None:
                return None
            authorities = connection.execute(
                """
                SELECT id, authority_kind FROM oid_authorities
                WHERE lease_id = ? ORDER BY created_at, id
                """,
                (row["id"],),
            ).fetchall()
        base_ids = [
            item["id"] for item in authorities
            if item["authority_kind"] == "BASE"
        ]
        if len(base_ids) != 1:
            raise IntegrationRecoveryError(
                "integration lease has no unique base OID authority"
            )
        return IntegrationLeaseBinding(
            lease_id=row["id"],
            sandbox_binding_id=row["sandbox_binding_id"],
            repository_id=row["repository_id"],
            goal_id=row["goal_id"],
            run_id=row["run_id"],
            slot_id=row["slot_id"],
            worker_assignment_id=row["worker_assignment_id"],
            branch_ref=row["branch_ref"],
            worktree_path=row["worktree_path"],
            base_authority_id=base_ids[0],
            approved_authority_ids=tuple(
                item["id"] for item in authorities
                if item["authority_kind"] == "APPROVED"
            ),
        )

    def narrow_failure_candidates(
        self,
        attempt_id: str,
        *,
        max_replays: int,
    ) -> CandidateFailureSet:
        """Perform at most ``max_replays`` deterministic, non-assigning replays."""

        attempt_id = require_identifier(attempt_id, "attempt_id")
        if (
            not isinstance(max_replays, int)
            or isinstance(max_replays, bool)
            or max_replays < 1
            or max_replays > MAX_NARROWING_REPLAYS
        ):
            raise IntegrationReplayLimitError(
                f"max_replays must be 1..{MAX_NARROWING_REPLAYS}"
            )
        with self.state_store.transaction() as connection:
            source = connection.execute(
                "SELECT * FROM integration_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if source is None:
                raise KeyError(attempt_id)
            if source["state"] not in {
                "QA_FAILED",
                "BUILD_FAILED",
                "REWORK_REQUIRED",
            }:
                raise IntegrationReplayLimitError(
                    "candidate narrowing is allowed only for a retained failed attempt"
                )
            plan_row = connection.execute(
                "SELECT * FROM integration_plans WHERE id = ?",
                (source["plan_id"],),
            ).fetchone()
            candidates = connection.execute(
                """
                SELECT ordinal, candidate_id, candidate_oid
                FROM attempt_candidates
                WHERE attempt_id = ? ORDER BY ordinal
                """,
                (attempt_id,),
            ).fetchall()
        if plan_row is None or not candidates:
            raise IntegrationRecoveryError("failed attempt has no immutable plan input")

        observations: list[ReplayObservation] = []
        suspected: list[str] = []
        for replay_ordinal, candidate in enumerate(candidates[:max_replays]):
            replay_attempt_id = _stable_identifier(
                "attempt-replay", attempt_id, str(replay_ordinal)
            )
            replay_plan = IntegrationPlan(
                plan_id=source["plan_id"],
                attempt_id=replay_attempt_id,
                goal_id=source["goal_id"],
                base_oid=source["base_oid"],
                ordered_candidate_oids=(candidate["candidate_oid"],),
                merge_strategy=source["merge_strategy"],
                pl_decision_id=plan_row["pl_decision_id"],
                idempotency_key=f"narrow:{attempt_id}:{replay_ordinal}",
                state=IntegrationPlanState.APPROVED,
            )
            replay = self._execute(
                replay_plan,
                replay_of=attempt_id,
                allow_approved_subset=True,
            )
            probe_failed: bool | None = None
            if replay.result_oid is not None and self.replay_probe is not None:
                probe_failed = bool(
                    self.replay_probe(
                        replay.result_oid, replay.ordered_candidate_oids
                    )
                )
            if probe_failed is not False:
                suspected.extend(replay.ordered_candidate_oids)
            observation = ReplayObservation(
                attempt_id=replay.attempt_id,
                candidate_oids=replay.ordered_candidate_oids,
                result_oid=replay.result_oid,
                state=replay.state,
                probe_failed=probe_failed,
            )
            observations.append(observation)
            self._record_narrowing_audit(attempt_id, observation)

        result = CandidateFailureSet(
            source_attempt_id=attempt_id,
            candidate_oids=tuple(suspected),
            replay_attempt_ids=tuple(item.attempt_id for item in observations),
            observations=tuple(observations),
            max_replays=max_replays,
            exhausted=len(observations) < len(candidates),
        )
        self._record_recovery_finding(
            attempt_id=attempt_id,
            goal_id=source["goal_id"],
            subject_oid=source["result_oid"],
            classification="BOUNDED_CANDIDATE_NARROWING",
            severity="INFO",
            expected={
                "max_replays": max_replays,
                "automatic_assignment": False,
            },
            observed={
                "replay_attempt_ids": list(result.replay_attempt_ids),
                "candidate_oids": list(result.candidate_oids),
                "exhausted": result.exhausted,
                "probe_available": self.replay_probe is not None,
            },
        )
        return result

    def _acquire_integration_lease(
        self,
        plan: IntegrationPlan,
        *,
        target_id: str,
        run_id: str,
    ) -> IntegrationLeaseBinding:
        """Reserve the one active PL writer and its canonical AX workspace."""

        lease_id = _stable_identifier(
            "integration-lease", plan.goal_id, plan.attempt_id
        )
        branch_ref = self._integration_branch(plan.goal_id, plan.attempt_id)
        worktree_path = self.path_authority.workspace(
            plan.goal_id, run_id, lease_id
        ).resolve(strict=False)
        now = utc_now()
        expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat(
            timespec="microseconds"
        )
        with self.state_store.transaction(immediate=True) as connection:
            actors = connection.execute(
                """
                SELECT rr.id AS repository_id, rr.canonical_path,
                       rr.git_common_dir, mr.repository_path,
                       sca.slot_id, sca.worker_assignment_id
                FROM repository_registrations AS rr
                JOIN managed_repositories AS mr
                  ON mr.id = rr.managed_repository_id
                JOIN runs AS run
                  ON run.id = ? AND run.goal_id = ?
                 AND run.target_id = rr.target_id AND run.state = 'RUNNING'
                JOIN seat_capability_activations AS sca
                  ON sca.goal_id = run.goal_id AND sca.run_id = run.id
                 AND sca.state = 'ACTIVE'
                JOIN logical_capabilities AS lc
                  ON lc.id = sca.capability_id AND lc.state = 'ACTIVE'
                 AND lc.capability_key = 'pl' AND lc.merge_authority = 1
                JOIN worker_slot_assignments AS wsa
                  ON wsa.id = sca.worker_assignment_id
                 AND wsa.slot_id = sca.slot_id AND wsa.run_id = run.id
                 AND wsa.state = 'ACTIVE'
                JOIN runtime_slots AS slot
                  ON slot.id = sca.slot_id AND slot.kind = 'FIXED'
                 AND slot.state <> 'RETIRED'
                WHERE rr.target_id = ? AND rr.state = 'ACTIVE'
                ORDER BY sca.id
                """,
                (run_id, plan.goal_id, target_id),
            ).fetchall()
            if len(actors) != 1:
                raise IntegrationAuthorizationError(
                    "integration requires exactly one active PL merge slot"
                )
            actor = actors[0]
            protected = {
                str(Path(actor["canonical_path"]).resolve(strict=False)),
                str(Path(actor["git_common_dir"]).resolve(strict=False)),
                str(Path(actor["repository_path"]).resolve(strict=False)),
            }
            protected.update(
                str(Path(row["worktree_path"]).resolve(strict=False))
                for row in connection.execute(
                    """
                    SELECT worktree_path FROM runtime_leases
                    WHERE state = 'ACTIVE' AND id <> ?
                    """,
                    (lease_id,),
                ).fetchall()
            )
            lease_signature = (
                actor["repository_id"],
                plan.goal_id,
                run_id,
                actor["slot_id"],
                actor["worker_assignment_id"],
                "INTEGRATION",
                branch_ref,
                str(worktree_path),
                plan.base_oid,
                plan.base_oid,
                compact_json((str(worktree_path),)),
                compact_json(tuple(sorted(protected, key=str.casefold))),
                f"integration-lease:{plan.attempt_id}",
            )
            existing = connection.execute(
                "SELECT * FROM runtime_leases WHERE id = ? OR idempotency_key = ?",
                (lease_id, lease_signature[-1]),
            ).fetchall()
            if not existing:
                try:
                    connection.execute(
                        """
                        INSERT INTO runtime_leases (
                            id, repository_id, goal_id, run_id, slot_id,
                            worker_assignment_id, lease_kind, branch_ref,
                            worktree_path, base_oid, expected_head_oid,
                            write_roots_json, protected_roots_json, state,
                            expires_at, idempotency_key, created_at, released_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'ACTIVE', ?, ?, ?, NULL)
                        """,
                        (lease_id, *lease_signature[:-1], expires_at,
                         lease_signature[-1], now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise IntegrationAuthorizationError(
                        "the exclusive PL integration lease is unavailable"
                    ) from exc
            elif len(existing) != 1:
                raise IntegrationAttemptConflictError(
                    "integration lease ID and idempotency key diverged"
                )
            else:
                row = existing[0]
                actual = tuple(
                    row[column]
                    for column in (
                        "repository_id", "goal_id", "run_id", "slot_id",
                        "worker_assignment_id", "lease_kind", "branch_ref",
                        "worktree_path", "base_oid", "expected_head_oid",
                        "write_roots_json", "protected_roots_json",
                        "idempotency_key",
                    )
                )
                if actual != lease_signature or row["state"] != "ACTIVE":
                    raise IntegrationAttemptConflictError(
                        "integration lease was reused with different authority"
                    )

            sandbox_binding_id = _stable_identifier(
                "integration-sandbox", lease_id
            )
            attestation_digest = hashlib.sha256(
                compact_json(
                    {
                        "lease_id": lease_id,
                        "cwd": str(worktree_path),
                        "base_oid": plan.base_oid,
                        "write_roots": [str(worktree_path)],
                        "protected_roots": sorted(protected, key=str.casefold),
                    }
                ).encode("utf-8")
            ).hexdigest()
            sandbox_signature = (
                lease_id,
                actor["repository_id"],
                run_id,
                actor["slot_id"],
                plan.base_oid,
                str(worktree_path),
                str(worktree_path),
                0,
                compact_json((str(worktree_path),)),
                "deterministic-git-integration",
                attestation_digest,
                f"integration-sandbox:{plan.attempt_id}",
            )
            sandbox = connection.execute(
                "SELECT * FROM sandbox_bindings WHERE id = ?",
                (sandbox_binding_id,),
            ).fetchone()
            if sandbox is None:
                connection.execute(
                    """
                    INSERT INTO sandbox_bindings (
                        id, lease_id, repository_id, run_id, slot_id,
                        subject_oid, cwd, source_root, source_read_only,
                        writable_roots_json, backend, attestation_digest,
                        state, idempotency_key, bound_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE',
                              ?, ?, NULL)
                    """,
                    (sandbox_binding_id, *sandbox_signature, now),
                )
            else:
                actual = tuple(
                    sandbox[column]
                    for column in (
                        "lease_id", "repository_id", "run_id", "slot_id",
                        "subject_oid", "cwd", "source_root",
                        "source_read_only", "writable_roots_json", "backend",
                        "attestation_digest", "idempotency_key",
                    )
                )
                if actual != sandbox_signature or sandbox["state"] != "ACTIVE":
                    raise IntegrationAttemptConflictError(
                        "integration sandbox was reused with different authority"
                    )

            base_authority = self._ensure_integration_oid_authority(
                connection,
                lease_id=lease_id,
                sandbox_binding_id=sandbox_binding_id,
                repository_id=actor["repository_id"],
                goal_id=plan.goal_id,
                run_id=run_id,
                authority_kind="BASE",
                oid=plan.base_oid,
                evidence={"attempt_id": plan.attempt_id, "role": "integration-base"},
            )
            approved_authorities = tuple(
                self._ensure_integration_oid_authority(
                    connection,
                    lease_id=lease_id,
                    sandbox_binding_id=sandbox_binding_id,
                    repository_id=actor["repository_id"],
                    goal_id=plan.goal_id,
                    run_id=run_id,
                    authority_kind="APPROVED",
                    oid=oid,
                    evidence={
                        "attempt_id": plan.attempt_id,
                        "ordinal": ordinal,
                        "role": "ta-approved-revision",
                    },
                )
                for ordinal, oid in enumerate(plan.ordered_candidate_oids)
            )
        return IntegrationLeaseBinding(
            lease_id=lease_id,
            sandbox_binding_id=sandbox_binding_id,
            repository_id=actor["repository_id"],
            goal_id=plan.goal_id,
            run_id=run_id,
            slot_id=actor["slot_id"],
            worker_assignment_id=actor["worker_assignment_id"],
            branch_ref=branch_ref,
            worktree_path=str(worktree_path),
            base_authority_id=base_authority,
            approved_authority_ids=approved_authorities,
        )

    @staticmethod
    def _ensure_integration_oid_authority(
        connection: sqlite3.Connection,
        *,
        lease_id: str,
        sandbox_binding_id: str,
        repository_id: str,
        goal_id: str,
        run_id: str,
        authority_kind: str,
        oid: str,
        evidence: Mapping[str, Any],
    ) -> str:
        evidence_digest = hashlib.sha256(
            compact_json(dict(evidence)).encode("utf-8")
        ).hexdigest()
        authority_id = _stable_identifier(
            "integration-oid", lease_id, authority_kind, oid
        )
        idempotency_key = (
            f"integration-oid:{lease_id}:{authority_kind}:{oid}"
        )
        row = connection.execute(
            "SELECT * FROM oid_authorities WHERE id = ? OR idempotency_key = ?",
            (authority_id, idempotency_key),
        ).fetchone()
        signature = (
            repository_id, goal_id, run_id, lease_id, sandbox_binding_id,
            authority_kind, oid, evidence_digest, idempotency_key,
        )
        if row is None:
            connection.execute(
                """
                INSERT INTO oid_authorities (
                    id, repository_id, goal_id, run_id, lease_id,
                    sandbox_binding_id, authority_kind, oid, evidence_digest,
                    state, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (authority_id, *signature, utc_now()),
            )
        else:
            actual = tuple(
                row[column]
                for column in (
                    "repository_id", "goal_id", "run_id", "lease_id",
                    "sandbox_binding_id", "authority_kind", "oid",
                    "evidence_digest", "idempotency_key",
                )
            )
            if actual != signature or row["state"] != "ACTIVE":
                raise IntegrationAttemptConflictError(
                    "integration OID authority was reused with different evidence"
                )
        return authority_id

    def _record_integration_result_authority(
        self,
        binding: IntegrationLeaseBinding,
        *,
        authority_kind: str,
        oid: str,
        evidence: Mapping[str, Any],
    ) -> str:
        with self.state_store.transaction(immediate=True) as connection:
            return self._ensure_integration_oid_authority(
                connection,
                lease_id=binding.lease_id,
                sandbox_binding_id=binding.sandbox_binding_id,
                repository_id=binding.repository_id,
                goal_id=binding.goal_id,
                run_id=binding.run_id,
                authority_kind=authority_kind,
                oid=oid,
                evidence=evidence,
            )

    def _finish_integration_lease(
        self,
        attempt_id: str,
        *,
        state: str,
    ) -> None:
        if state not in {"RELEASED", "QUARANTINED"}:
            raise ValueError("integration lease final state is invalid")
        now = utc_now()
        with self.state_store.transaction(immediate=True) as connection:
            lease = connection.execute(
                """
                SELECT * FROM runtime_leases
                WHERE idempotency_key = ? AND lease_kind = 'INTEGRATION'
                """,
                (f"integration-lease:{attempt_id}",),
            ).fetchone()
            if lease is None:
                return
            if lease["state"] == state:
                return
            if lease["state"] != "ACTIVE":
                raise IntegrationRecoveryError(
                    "integration lease already has a different final state"
                )
            connection.execute(
                """
                UPDATE sandbox_bindings
                SET state = ?, released_at = ?
                WHERE lease_id = ? AND state = 'ACTIVE'
                """,
                (state, now, lease["id"]),
            )
            if state == "QUARANTINED":
                connection.execute(
                    """
                    UPDATE oid_authorities SET state = 'QUARANTINED'
                    WHERE lease_id = ? AND state = 'ACTIVE'
                    """,
                    (lease["id"],),
                )
            connection.execute(
                """
                UPDATE runtime_leases
                SET state = ?, released_at = ?
                WHERE id = ? AND state = 'ACTIVE'
                """,
                (state, now, lease["id"]),
            )

    def _integration_worktree_path(
        self,
        goal_id: str,
        attempt_id: str,
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

    def _execute(
        self,
        plan: IntegrationPlan,
        *,
        replay_of: str | None,
        allow_approved_subset: bool,
    ) -> IntegrationAttempt:
        if not isinstance(plan, IntegrationPlan):
            raise TypeError("plan must be an IntegrationPlan")
        if plan.state is IntegrationPlanState.SUPERSEDED:
            raise IntegrationAuthorizationError("superseded plan cannot execute")
        if plan.merge_strategy not in SUPPORTED_MERGE_STRATEGIES:
            raise IntegrationAuthorizationError(
                f"unsupported deterministic merge strategy: {plan.merge_strategy}"
            )

        authorization = self._authorize_plan(
            plan,
            replay_of=replay_of,
            allow_approved_subset=allow_approved_subset,
        )
        target_id = authorization["target_id"]
        candidate_ids = tuple(authorization["candidate_ids"])
        delivery_by_oid = {
            item.subject_oid: item
            for item in authorization["delivery_evidence"]
        }
        selected_delivery_evidence = tuple(
            delivery_by_oid[oid] for oid in plan.ordered_candidate_oids
        )
        for oid in (plan.base_oid, *plan.ordered_candidate_oids):
            self.repository_service.resolve_commit(target_id, oid)

        environment = self._environment_record()
        current = self._ensure_attempt_rows(
            plan,
            candidate_ids=candidate_ids,
            environment=environment,
            replay_of=replay_of,
        )
        if current.state in {
            IntegrationAttemptState.REWORK_REQUIRED,
            IntegrationAttemptState.QA_PENDING,
            IntegrationAttemptState.QA_FAILED,
            IntegrationAttemptState.BUILD_PENDING,
            IntegrationAttemptState.BUILD_FAILED,
            IntegrationAttemptState.GATE_PENDING,
            IntegrationAttemptState.APPROVED,
            IntegrationAttemptState.QUARANTINED,
        }:
            return current

        execute_key = f"integration-execute:{plan.idempotency_key}"
        intent = self.state_store.begin_intent(
            operation="execute-integration",
            idempotency_key=execute_key,
            expected_state="PLANNED_OR_RECREATED",
            expected_oid=plan.base_oid,
            payload={
                "attempt_id": plan.attempt_id,
                "plan_id": plan.plan_id,
                "goal_id": plan.goal_id,
                "base_oid": plan.base_oid,
                "ordered_candidate_oids": list(plan.ordered_candidate_oids),
                "merge_strategy": plan.merge_strategy,
                "pl_decision_id": plan.pl_decision_id,
                "delivery_v4_contract_ids": [
                    item.contract_id for item in selected_delivery_evidence
                ],
                "delivery_v4_result_ids": [
                    item.result_id for item in selected_delivery_evidence
                ],
                "replay_of": replay_of,
            },
        )
        if intent.status is IntentStatus.COMPLETED:
            return self._load_attempt(plan.attempt_id)
        self._hit_boundary(BOUNDARY_INTENT_RECORDED, plan.attempt_id)

        lease_binding = self._acquire_integration_lease(
            plan,
            target_id=target_id,
            run_id=authorization["run_id"],
        )
        worktree_path = Path(lease_binding.worktree_path)
        branch_ref = lease_binding.branch_ref
        self._journal_transition(
            plan.attempt_id,
            transition="preflight",
            expected_states={"PLANNED", "RECREATED", "PREFLIGHTING"},
            resulting_state="PREFLIGHTING",
            expected_oid=plan.base_oid,
            evidence={
                "worktree_path": str(worktree_path),
                "branch_ref": branch_ref,
                "runtime_lease_id": lease_binding.lease_id,
                "sandbox_binding_id": lease_binding.sandbox_binding_id,
            },
        )
        receipt = self.repository_service.create_disposable_worktree(
            target_id,
            oid=plan.base_oid,
            path=worktree_path,
            branch_ref=branch_ref,
        )
        self._hit_boundary(BOUNDARY_WORKTREE_READY, plan.attempt_id)
        self._journal_transition(
            plan.attempt_id,
            transition="merge-started",
            expected_states={"PREFLIGHTING", "MERGING"},
            resulting_state="MERGING",
            expected_oid=plan.base_oid,
            evidence={
                "worktree_receipt": asdict(receipt),
                "runtime_lease_id": lease_binding.lease_id,
            },
        )

        all_step_evidence: list[dict[str, Any]] = []
        current_head = plan.base_oid
        for ordinal, candidate_oid in enumerate(plan.ordered_candidate_oids):
            result = self._merge_candidate(
                plan=plan,
                receipt=receipt,
                worktree_path=worktree_path,
                current_head=current_head,
                candidate_oid=candidate_oid,
                ordinal=ordinal,
                environment=environment,
            )
            if isinstance(result, ConflictEvidence):
                failure_authority_id = self._record_integration_result_authority(
                    lease_binding,
                    authority_kind="FAILURE",
                    oid=result.partial_head_oid,
                    evidence={
                        "attempt_id": plan.attempt_id,
                        "goal_id": plan.goal_id,
                        "candidate_oid": result.candidate_oid,
                        "failed_ref": result.failed_ref,
                    },
                )
                evidence = {
                    "classification": "MERGE_CONFLICT",
                    "base_oid": plan.base_oid,
                    "subject_oids": list(plan.ordered_candidate_oids),
                    "approved_oids": list(plan.ordered_candidate_oids),
                    "integration_candidate_oid": result.partial_head_oid,
                    "failure_oid": result.partial_head_oid,
                    "conflict": self._conflict_to_json(result),
                    "environment": environment,
                    "worktree_receipt": asdict(receipt),
                    "evidence_ids": [],
                    "runtime_lease_id": lease_binding.lease_id,
                    "sandbox_binding_id": lease_binding.sandbox_binding_id,
                    "base_authority_id": lease_binding.base_authority_id,
                    "approved_authority_ids": list(
                        lease_binding.approved_authority_ids
                    ),
                    "failure_authority_id": failure_authority_id,
                    "delivery_v4_contract_ids": [
                        item.contract_id for item in selected_delivery_evidence
                    ],
                    "delivery_v4_result_ids": [
                        item.result_id for item in selected_delivery_evidence
                    ],
                    "rework_route": {
                        "transition_id": "pl_issue_rework",
                        "owner_capability": "pl",
                        "direct_source_repair_allowed": False,
                        "required_action": (
                            "CREATE_AND_ASSIGN_NEW_WORK_ITEM_REVISION"
                        ),
                        "restart_from_base_oid": plan.base_oid,
                    },
                    "replay_of": replay_of,
                }
                finding_id = self._record_conflict_finding(plan, result)
                evidence["evidence_ids"] = [finding_id]
                evidence["pl_rework_request_id"] = self.gate_coordinator.request_rework(
                    finding_id=finding_id,
                    requested_by_role=ServiceIdentity.INTEGRATION_CONTROLLER.value,
                ).request_id
                with self.state_store.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        UPDATE integration_attempts
                        SET state = 'REWORK_REQUIRED', result_oid = NULL,
                            evidence_json = ?, completed_at = ?
                        WHERE id = ?
                        """,
                        (compact_json(evidence), utc_now(), plan.attempt_id),
                    )
                completed = self.state_store.complete_intent(
                    intent.intent_id,
                    resulting_state="REWORK_REQUIRED",
                    resulting_oid=None,
                    evidence={
                        "attempt_id": plan.attempt_id,
                        "finding_id": finding_id,
                        "failed_ref": result.failed_ref,
                    },
                )
                self._cleanup_worktree(
                    receipt,
                    expected_oid=result.partial_head_oid,
                    attempt_id=plan.attempt_id,
                )
                self._finish_integration_lease(
                    plan.attempt_id, state="RELEASED"
                )
                self._record_attempt_audit(
                    completed.intent_id,
                    plan,
                    "INTEGRATION_CONFLICT_RECORDED",
                    None,
                    evidence,
                )
                return self._load_attempt(plan.attempt_id)
            current_head, step_evidence = result
            all_step_evidence.append(step_evidence)

        final_ref = self._integration_ref(plan.goal_id, plan.attempt_id)
        repository = Path(receipt.managed_repository_path).resolve()
        self.repository_service._ensure_immutable_ref(  # Phase 4 evidence seam.
            repository, final_ref, current_head
        )
        self._hit_boundary(BOUNDARY_FINAL_REF_CREATED, plan.attempt_id)
        integration_authority_id = self._record_integration_result_authority(
            lease_binding,
            authority_kind="INTEGRATION",
            oid=current_head,
            evidence={
                "attempt_id": plan.attempt_id,
                "goal_id": plan.goal_id,
                "integration_ref": final_ref,
                "approved_oids": list(plan.ordered_candidate_oids),
            },
        )
        evidence = {
            "classification": "CLEAN_MERGE",
            "base_oid": plan.base_oid,
            "subject_oids": list(plan.ordered_candidate_oids),
            "approved_oids": list(plan.ordered_candidate_oids),
            "integration_candidate_oid": current_head,
            "failure_oid": None,
            "environment": environment,
            "worktree_receipt": asdict(receipt),
            "steps": all_step_evidence,
            "integration_ref": final_ref,
            "evidence_ids": [final_ref],
            "runtime_lease_id": lease_binding.lease_id,
            "sandbox_binding_id": lease_binding.sandbox_binding_id,
            "base_authority_id": lease_binding.base_authority_id,
            "approved_authority_ids": list(
                lease_binding.approved_authority_ids
            ),
            "integration_authority_id": integration_authority_id,
            "delivery_v4_contract_ids": [
                item.contract_id for item in selected_delivery_evidence
            ],
            "delivery_v4_result_ids": [
                item.result_id for item in selected_delivery_evidence
            ],
            "delivery_v4_profile_binding_ids": [
                item.profile_binding_id for item in selected_delivery_evidence
            ],
            "delivery_v4_mcp_receipt_ids": [
                receipt_id
                for item in selected_delivery_evidence
                for receipt_id in item.mcp_receipt_ids
            ],
            "replay_of": replay_of,
        }
        with self.state_store.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE integration_attempts
                SET state = 'QA_PENDING', result_oid = ?,
                    evidence_json = ?, completed_at = ?
                WHERE id = ? AND state IN (
                    'PLANNED', 'RECREATED', 'PREFLIGHTING', 'MERGING', 'MERGED'
                )
                """,
                (current_head, compact_json(evidence), utc_now(), plan.attempt_id),
            ).rowcount
            if updated != 1:
                stored = connection.execute(
                    "SELECT result_oid, state FROM integration_attempts WHERE id = ?",
                    (plan.attempt_id,),
                ).fetchone()
                if stored is None or (
                    stored["result_oid"] != current_head
                    or stored["state"] != "QA_PENDING"
                ):
                    raise IntegrationAttemptConflictError(
                        "attempt state changed before result persistence"
                    )
            connection.execute(
                """
                UPDATE integration_plans
                SET state = 'COMPLETED'
                WHERE id = ? AND state IN ('APPROVED', 'EXECUTING', 'COMPLETED')
                """,
                (plan.plan_id,),
            )
        self._hit_boundary(BOUNDARY_RESULT_RECORDED, plan.attempt_id)
        completed = self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="QA_PENDING",
            resulting_oid=current_head,
            evidence={
                "attempt_id": plan.attempt_id,
                "integration_ref": final_ref,
                "step_count": len(all_step_evidence),
            },
        )
        self._cleanup_worktree(
            receipt,
            expected_oid=current_head,
            attempt_id=plan.attempt_id,
        )
        self._finish_integration_lease(plan.attempt_id, state="RELEASED")
        self._record_attempt_audit(
            completed.intent_id,
            plan,
            "INTEGRATION_MERGED",
            current_head,
            evidence,
        )
        return self._load_attempt(plan.attempt_id)

    def _authorize_plan(
        self,
        plan: IntegrationPlan,
        *,
        replay_of: str | None,
        allow_approved_subset: bool,
    ) -> dict[str, Any]:
        with self.state_store.transaction() as connection:
            goal = connection.execute(
                "SELECT * FROM goals WHERE id = ?", (plan.goal_id,)
            ).fetchone()
            if goal is None:
                raise IntegrationAuthorizationError(
                    f"goal does not exist: {plan.goal_id}"
                )
            if goal["base_oid"] != plan.base_oid:
                raise IntegrationAuthorizationError(
                    "integration base OID differs from the pinned goal base"
                )
            selection = connection.execute(
                "SELECT * FROM gate_decisions WHERE id = ?",
                (plan.pl_decision_id,),
            ).fetchone()
            if selection is None:
                raise IntegrationAuthorizationError(
                    "PL candidate-selection decision does not exist"
                )
            if (
                selection["goal_id"] != plan.goal_id
                or selection["gate_type"] != GateType.PL_CANDIDATE_SELECTION.value
                or selection["actor_role"] != "pl"
                or selection["subject_oid"] != plan.base_oid
                or selection["decision"] != GateDecisionValue.APPROVED.value
            ):
                raise IntegrationAuthorizationError(
                    "plan is not bound to an approved PL candidate-selection gate"
                )
            approved_candidate_ids = tuple(_json_array(selection["evidence_json"]))
            approved_rows = []
            delivery_evidence: list[DeliveryV4ResultEvidence] = []
            for candidate_id in approved_candidate_ids:
                row = connection.execute(
                    """
                    SELECT candidate_submissions.*,
                           work_revisions.base_oid AS revision_base_oid,
                           work_revisions.head_oid AS revision_head_oid
                    FROM candidate_submissions
                    JOIN work_revisions
                      ON work_revisions.id = candidate_submissions.revision_id
                    WHERE candidate_submissions.id = ?
                      AND candidate_submissions.goal_id = ?
                    """,
                    (candidate_id, plan.goal_id),
                ).fetchone()
                if (
                    row is None
                    or row["state"] != "APPROVED"
                    or row["revision_head_oid"] != row["candidate_oid"]
                ):
                    raise IntegrationAuthorizationError(
                        f"PL selected candidate is not TA-approved: {candidate_id}"
                    )
                try:
                    proof = require_delivery_v4_result_evidence(
                        self.state_store,
                        goal_id=plan.goal_id,
                        subject_oid=row["candidate_oid"],
                        transition_id=TA_REVIEW_TRANSITION,
                        capability_id="ta",
                        result_kinds=("approved",),
                        expected_result_oid=row["candidate_oid"],
                    )
                except DeliveryV4EvidenceError as exc:
                    raise IntegrationAuthorizationError(
                        f"candidate {candidate_id} lacks admitted delivery-v4 "
                        "exact-OID review evidence"
                    ) from exc
                if proof.base_oid != row["revision_base_oid"]:
                    raise IntegrationAuthorizationError(
                        f"candidate {candidate_id} review base differs from its revision"
                    )
                for gate_type in (
                    GateType.TA_CODE_QUALITY.value,
                    GateType.TA_ARCHITECTURE.value,
                ):
                    decisions = connection.execute(
                        """
                        SELECT * FROM gate_decisions
                        WHERE goal_id = ? AND subject_oid = ? AND gate_type = ?
                          AND actor_role = 'ta' AND decision = 'APPROVED'
                        """,
                        (plan.goal_id, row["candidate_oid"], gate_type),
                    ).fetchall()
                    if (
                        len(decisions) != 1
                        or decisions[0]["profile_digest"]
                        != proof.compiled_profile_digest
                    ):
                        raise IntegrationAuthorizationError(
                            f"candidate {candidate_id} lacks exact-OID {gate_type} "
                            "bound to its delivery-v4 profile"
                        )
                approved_rows.append(row)
                delivery_evidence.append(proof)

            approved_oids = tuple(row["candidate_oid"] for row in approved_rows)
            if allow_approved_subset:
                if replay_of is None:
                    raise IntegrationAuthorizationError(
                        "candidate subset execution requires a source failed attempt"
                    )
                requested = tuple(plan.ordered_candidate_oids)
                iterator = iter(approved_oids)
                if not all(any(value == item for value in iterator) for item in requested):
                    raise IntegrationAuthorizationError(
                        "bounded replay candidates are not an ordered PL-approved subset"
                    )
                requested_ids = tuple(
                    next(
                        row["id"]
                        for row in approved_rows
                        if row["candidate_oid"] == oid
                    )
                    for oid in requested
                )
                source = connection.execute(
                    "SELECT * FROM integration_attempts WHERE id = ?",
                    (replay_of,),
                ).fetchone()
                if (
                    source is None
                    or source["plan_id"] != plan.plan_id
                    or source["goal_id"] != plan.goal_id
                    or source["state"]
                    not in {"QA_FAILED", "BUILD_FAILED", "REWORK_REQUIRED"}
                ):
                    raise IntegrationAuthorizationError(
                        "bounded replay source is not a retained failed attempt"
                    )
            else:
                if approved_oids != tuple(plan.ordered_candidate_oids):
                    raise IntegrationAuthorizationError(
                        "plan candidate OIDs/order differ from PL selection evidence"
                    )
                requested_ids = approved_candidate_ids
            run_ids = {item.run_id for item in delivery_evidence}
            if len(run_ids) != 1:
                raise IntegrationAuthorizationError(
                    "selected exact-OID approvals span more than one run"
                )
            run_id = next(iter(run_ids))
            run = connection.execute(
                """
                SELECT * FROM runs
                WHERE id = ? AND goal_id = ? AND target_id = ?
                  AND base_oid = ? AND state = 'RUNNING'
                """,
                (run_id, plan.goal_id, goal["target_id"], plan.base_oid),
            ).fetchone()
            if run is None:
                raise IntegrationAuthorizationError(
                    "delivery-v4 review run is not the active pinned goal run"
                )
        return {
            "target_id": goal["target_id"],
            "run_id": run_id,
            "candidate_ids": requested_ids,
            "approved_candidate_ids": approved_candidate_ids,
            "delivery_evidence": tuple(delivery_evidence),
        }

    def _ensure_attempt_rows(
        self,
        plan: IntegrationPlan,
        *,
        candidate_ids: Sequence[str],
        environment: Mapping[str, Any],
        replay_of: str | None,
    ) -> IntegrationAttempt:
        now = utc_now()
        base_evidence = {
            "classification": "PLANNED",
            "replay_of": replay_of,
            "evidence_ids": [],
        }
        with self.state_store.transaction(immediate=True) as connection:
            plan_row = connection.execute(
                "SELECT * FROM integration_plans WHERE id = ?", (plan.plan_id,)
            ).fetchone()
            expected_plan = (
                plan.goal_id,
                plan.base_oid,
                plan.merge_strategy,
                plan.pl_decision_id,
            )
            if plan_row is None:
                try:
                    connection.execute(
                        """
                        INSERT INTO integration_plans (
                            id, goal_id, base_oid, merge_strategy, pl_decision_id,
                            state, idempotency_key, created_at, approved_at
                        ) VALUES (?, ?, ?, ?, ?, 'APPROVED', ?, ?, ?)
                        """,
                        (
                            plan.plan_id,
                            *expected_plan,
                            f"plan:{plan.plan_id}",
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise IntegrationAttemptConflictError(
                        "integration plan identity/idempotency is already used"
                    ) from exc
            else:
                actual_plan = (
                    plan_row["goal_id"],
                    plan_row["base_oid"],
                    plan_row["merge_strategy"],
                    plan_row["pl_decision_id"],
                )
                if actual_plan != expected_plan or plan_row["state"] == "SUPERSEDED":
                    raise IntegrationAttemptConflictError(
                        "integration plan immutable input differs"
                    )

            attempt = connection.execute(
                "SELECT * FROM integration_attempts WHERE id = ?", (plan.attempt_id,)
            ).fetchone()
            if attempt is None:
                try:
                    connection.execute(
                        """
                        INSERT INTO integration_attempts (
                            id, plan_id, goal_id, base_oid, merge_strategy, state,
                            evidence_json, environment_json, idempotency_key, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'PLANNED', ?, ?, ?, ?)
                        """,
                        (
                            plan.attempt_id,
                            plan.plan_id,
                            plan.goal_id,
                            plan.base_oid,
                            plan.merge_strategy,
                            compact_json(base_evidence),
                            compact_json(environment),
                            plan.idempotency_key,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise IntegrationAttemptConflictError(
                        "integration attempt identity/idempotency is already used"
                    ) from exc
                for ordinal, (candidate_id, candidate_oid) in enumerate(
                    zip(candidate_ids, plan.ordered_candidate_oids, strict=True)
                ):
                    connection.execute(
                        """
                        INSERT INTO attempt_candidates (
                            attempt_id, ordinal, candidate_id, candidate_oid
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            plan.attempt_id,
                            ordinal,
                            candidate_id,
                            candidate_oid,
                        ),
                    )
                attempt = connection.execute(
                    "SELECT * FROM integration_attempts WHERE id = ?",
                    (plan.attempt_id,),
                ).fetchone()
            else:
                expected_attempt = (
                    plan.plan_id,
                    plan.goal_id,
                    plan.base_oid,
                    plan.merge_strategy,
                    plan.idempotency_key,
                    compact_json(environment),
                )
                actual_attempt = (
                    attempt["plan_id"],
                    attempt["goal_id"],
                    attempt["base_oid"],
                    attempt["merge_strategy"],
                    attempt["idempotency_key"],
                    attempt["environment_json"],
                )
                if actual_attempt != expected_attempt:
                    raise IntegrationAttemptConflictError(
                        "integration attempt immutable input differs"
                    )
                stored_candidates = connection.execute(
                    """
                    SELECT candidate_id, candidate_oid
                    FROM attempt_candidates
                    WHERE attempt_id = ? ORDER BY ordinal
                    """,
                    (plan.attempt_id,),
                ).fetchall()
                expected_candidates = tuple(
                    zip(candidate_ids, plan.ordered_candidate_oids, strict=True)
                )
                actual_candidates = tuple(
                    (row["candidate_id"], row["candidate_oid"])
                    for row in stored_candidates
                )
                if actual_candidates != expected_candidates:
                    raise IntegrationAttemptConflictError(
                        "integration attempt candidate order differs"
                    )
        assert attempt is not None
        return self._attempt_from_row(attempt)

    def _merge_candidate(
        self,
        *,
        plan: IntegrationPlan,
        receipt: GitWorktreeReceipt,
        worktree_path: Path,
        current_head: str,
        candidate_oid: str,
        ordinal: int,
        environment: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]] | ConflictEvidence:
        payload = {
            "attempt_id": plan.attempt_id,
            "ordinal": ordinal,
            "current_head_oid": current_head,
            "candidate_oid": candidate_oid,
            "merge_strategy": plan.merge_strategy,
        }
        intent = self.state_store.begin_intent(
            operation="integration-merge-step",
            idempotency_key=f"integration:{plan.attempt_id}:step:{ordinal}",
            expected_state="MERGING",
            expected_oid=current_head,
            payload=payload,
        )
        if intent.status is IntentStatus.COMPLETED:
            if intent.resulting_state == "STEP_MERGED":
                assert intent.resulting_oid is not None
                reset = self._git(
                    ["reset", "--hard", intent.resulting_oid], cwd=worktree_path
                )
                return intent.resulting_oid, {
                    **dict(intent.evidence),
                    "resume_command": _command_evidence(reset),
                }
            if intent.resulting_state == "CONFLICTED":
                return self._conflict_from_json(dict(intent.evidence["conflict"]))
            raise IntegrationAttemptConflictError(
                "completed merge-step intent has an unknown outcome"
            )

        commands: list[Mapping[str, Any]] = []
        head = self._git(["rev-parse", "--verify", "HEAD"], cwd=worktree_path)
        commands.append(_command_evidence(head))
        actual_head = str(require_oid(head.stdout.strip(), "worktree_head"))
        if actual_head != current_head:
            raise IntegrationAttemptConflictError(
                f"integration worktree is {actual_head}, expected {current_head}"
            )
        merge_base = self._git(
            ["merge-base", "--all", current_head, candidate_oid],
            cwd=worktree_path,
            check=False,
        )
        commands.append(_command_evidence(merge_base))
        merge = self._git(
            [
                "-c",
                f"user.name={CONTROLLER_NAME}",
                "-c",
                f"user.email={CONTROLLER_EMAIL}",
                "merge",
                "--no-commit",
                "--no-ff",
                "--no-edit",
                "--no-autostash",
                "-s",
                "ort",
                candidate_oid,
            ],
            cwd=worktree_path,
            check=False,
        )
        commands.append(_command_evidence(merge))
        unmerged = self._unmerged_index(worktree_path)
        if unmerged:
            conflict_paths = tuple(sorted({entry.path for entry in unmerged}))
            failed_ref = self._failed_ref(plan.goal_id, plan.attempt_id)
            repository = Path(receipt.managed_repository_path).resolve()
            self.repository_service._ensure_immutable_ref(
                repository, failed_ref, current_head
            )
            evidence = ConflictEvidence(
                attempt_id=plan.attempt_id,
                base_oid=plan.base_oid,
                candidate_oid=candidate_oid,
                candidate_ordinal=ordinal,
                ordered_candidate_oids=plan.ordered_candidate_oids,
                merge_bases=tuple(
                    line.strip()
                    for line in merge_base.stdout.splitlines()
                    if line.strip()
                ),
                partial_head_oid=current_head,
                conflict_paths=conflict_paths,
                unmerged_index=unmerged,
                commands=tuple(commands),
                git_version=str(environment["git_version"]),
                environment_fingerprint=str(environment["fingerprint"]),
                failed_ref=failed_ref,
            )
            completed = self.state_store.complete_intent(
                intent.intent_id,
                resulting_state="CONFLICTED",
                resulting_oid=current_head,
                evidence={"conflict": self._conflict_to_json(evidence)},
            )
            abort = self._git(["merge", "--abort"], cwd=worktree_path, check=False)
            if abort.returncode != 0:
                reset = self._git(
                    ["reset", "--hard", current_head],
                    cwd=worktree_path,
                    check=False,
                )
                if reset.returncode != 0:
                    self._record_recovery_finding(
                        attempt_id=plan.attempt_id,
                        goal_id=plan.goal_id,
                        subject_oid=None,
                        classification="CONFLICT_ABORT_FAILED",
                        severity="CRITICAL",
                        expected={"head_oid": current_head, "merge_state": "ABORTED"},
                        observed={
                            "abort": _command_evidence(abort),
                            "reset": _command_evidence(reset),
                        },
                    )
            with self.state_store.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE integration_attempts
                    SET state = 'EVIDENCE_PERSISTED'
                    WHERE id = ? AND state IN ('MERGING', 'CONFLICTED')
                    """,
                    (plan.attempt_id,),
                )
            self._record_attempt_audit(
                completed.intent_id,
                plan,
                "INTEGRATION_CONFLICT_CAPTURED",
                current_head,
                {"conflict": self._conflict_to_json(evidence)},
            )
            return evidence
        if merge.returncode != 0:
            raise IntegrationExecutionError(
                "Git merge failed without unmerged index evidence: "
                f"{_sanitize(merge.stderr.strip())}"
            )

        tree = self._git(["write-tree"], cwd=worktree_path)
        commands.append(_command_evidence(tree))
        tree_oid = str(require_oid(tree.stdout.strip(), "merge_tree_oid"))
        commit_oid, hash_command = self._write_deterministic_merge_commit(
            worktree_path=worktree_path,
            plan=plan,
            ordinal=ordinal,
            tree_oid=tree_oid,
            parent_oid=current_head,
            candidate_oid=candidate_oid,
        )
        commands.append(_command_evidence(hash_command))
        reset = self._git(["reset", "--hard", commit_oid], cwd=worktree_path)
        commands.append(_command_evidence(reset))
        step_ref = self._integration_step_ref(
            plan.goal_id, plan.attempt_id, ordinal
        )
        repository = Path(receipt.managed_repository_path).resolve()
        self.repository_service._ensure_immutable_ref(repository, step_ref, commit_oid)
        step_evidence = {
            "ordinal": ordinal,
            "input_head_oid": current_head,
            "candidate_oid": candidate_oid,
            "merge_bases": [
                line.strip()
                for line in merge_base.stdout.splitlines()
                if line.strip()
            ],
            "tree_oid": tree_oid,
            "result_oid": commit_oid,
            "step_ref": step_ref,
            "commands": commands,
        }
        self._hit_boundary(BOUNDARY_STEP_EFFECT_APPLIED, plan.attempt_id)
        self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="STEP_MERGED",
            resulting_oid=commit_oid,
            evidence=step_evidence,
        )
        self._hit_boundary(BOUNDARY_STEP_RECORDED, plan.attempt_id)
        return commit_oid, step_evidence

    def _write_deterministic_merge_commit(
        self,
        *,
        worktree_path: Path,
        plan: IntegrationPlan,
        ordinal: int,
        tree_oid: str,
        parent_oid: str,
        candidate_oid: str,
    ) -> tuple[str, GitCommandResult]:
        plan_digest = hashlib.sha256(
            compact_json(
                {
                    "base_oid": plan.base_oid,
                    "ordered_candidate_oids": list(plan.ordered_candidate_oids),
                    "merge_strategy": plan.merge_strategy,
                    "pl_decision_id": plan.pl_decision_id,
                }
            ).encode("utf-8")
        ).hexdigest()
        message = (
            "Agentic AX deterministic integration\n\n"
            f"Plan-Digest: {plan_digest}\n"
            f"Candidate-Ordinal: {ordinal}\n"
            f"Candidate-OID: {candidate_oid}\n"
        )
        identity = f"{CONTROLLER_NAME} <{CONTROLLER_EMAIL}>"
        content = (
            f"tree {tree_oid}\n"
            f"parent {parent_oid}\n"
            f"parent {candidate_oid}\n"
            f"author {identity} {DETERMINISTIC_COMMIT_TIME} +0000\n"
            f"committer {identity} {DETERMINISTIC_COMMIT_TIME} +0000\n"
            f"\n{message}"
        ).encode("utf-8")
        scratch = worktree_path.parent / (
            f".{_safe_ref_component(plan.attempt_id)}-step-{ordinal}.commit"
        )
        if scratch.exists():
            if scratch.read_bytes() != content:
                raise IntegrationAttemptConflictError(
                    "deterministic commit scratch path contains different input"
                )
        else:
            scratch.write_bytes(content)
        try:
            result = self._git(
                ["hash-object", "-t", "commit", "-w", str(scratch)],
                cwd=worktree_path,
            )
        finally:
            scratch.unlink(missing_ok=True)
        commit_oid = str(require_oid(result.stdout.strip(), "merge_commit_oid"))
        return commit_oid, result

    def _unmerged_index(self, worktree_path: Path) -> tuple[UnmergedIndexEntry, ...]:
        result = self._git(
            ["ls-files", "-u", "-z"], cwd=worktree_path, check=False
        )
        entries = []
        for record in result.stdout.split("\x00"):
            if not record:
                continue
            metadata, separator, path = record.partition("\t")
            fields = metadata.split()
            if not separator or len(fields) != 3:
                raise IntegrationExecutionError(
                    "Git returned malformed unmerged index evidence"
                )
            mode, oid, stage_raw = fields
            entries.append(
                UnmergedIndexEntry(
                    mode=mode,
                    oid=str(require_oid(oid, "unmerged_index_oid")),
                    stage=int(stage_raw),
                    path=path,
                )
            )
        return tuple(entries)

    def _journal_transition(
        self,
        attempt_id: str,
        *,
        transition: str,
        expected_states: set[str],
        resulting_state: str,
        expected_oid: str | None,
        evidence: Mapping[str, Any],
    ) -> None:
        key = f"integration:{attempt_id}:transition:{transition}"
        intent = self.state_store.begin_intent(
            operation="integration-state-transition",
            idempotency_key=key,
            expected_state="|".join(sorted(expected_states)),
            expected_oid=expected_oid,
            payload={
                "attempt_id": attempt_id,
                "transition": transition,
                "resulting_state": resulting_state,
                "evidence": evidence,
            },
        )
        if intent.status is IntentStatus.COMPLETED:
            return
        with self.state_store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT state FROM integration_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            if row["state"] != resulting_state:
                if row["state"] not in expected_states:
                    raise IntegrationAttemptConflictError(
                        f"attempt cannot transition {row['state']} -> {resulting_state}"
                    )
                connection.execute(
                    "UPDATE integration_attempts SET state = ? WHERE id = ?",
                    (resulting_state, attempt_id),
                )
        self.state_store.complete_intent(
            intent.intent_id,
            resulting_state=resulting_state,
            resulting_oid=expected_oid,
            evidence=evidence,
        )

    def _cleanup_worktree(
        self,
        receipt: GitWorktreeReceipt,
        *,
        expected_oid: str,
        attempt_id: str,
    ) -> None:
        try:
            self.repository_service.remove_disposable_worktree(
                receipt, expected_oid=expected_oid
            )
        except Exception as exc:
            self._record_recovery_finding(
                attempt_id=attempt_id,
                goal_id=None,
                subject_oid=expected_oid,
                classification="WORKTREE_CLEANUP_REQUIRED",
                severity="ERROR",
                expected={
                    "worktree_path": receipt.worktree_path,
                    "expected_oid": expected_oid,
                    "state": "REMOVED",
                },
                observed={"error": _sanitize(str(exc))},
            )
            raise IntegrationRecoveryError(
                "integration result is retained but disposable worktree cleanup failed"
            ) from exc
        self._hit_boundary(BOUNDARY_WORKTREE_REMOVED, attempt_id)

    def _cleanup_recovered_worktree(
        self,
        *,
        target_id: str,
        repository: Path,
        goal_id: str,
        attempt_id: str,
        expected_oid: str,
    ) -> None:
        path = self._integration_worktree_path(goal_id, attempt_id)
        entry = self.repository_service._worktree_entries(repository).get(
            self._path_key(path)
        )
        if entry is None:
            return
        create_intent = self._find_worktree_create_intent(path)
        if create_intent is None:
            self._record_recovery_finding(
                attempt_id=attempt_id,
                goal_id=goal_id,
                subject_oid=expected_oid,
                classification="WORKTREE_RECEIPT_MISSING",
                severity="ERROR",
                expected={"worktree_path": str(path), "state": "REMOVED"},
                observed={"worktree_entry": dict(entry)},
            )
            return
        evidence = _json_object(create_intent["evidence_json"])
        receipt_raw = evidence.get("receipt")
        if not isinstance(receipt_raw, dict):
            return
        receipt = GitWorktreeReceipt(**receipt_raw)
        self._cleanup_worktree(
            receipt, expected_oid=expected_oid, attempt_id=attempt_id
        )

    def _find_worktree_create_intent(self, path: Path) -> sqlite3.Row | None:
        with self.state_store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM operation_intents
                WHERE operation = 'create-disposable-worktree'
                  AND status = 'COMPLETED'
                ORDER BY created_at DESC
                """
            ).fetchall()
        expected = self._path_key(path)
        for row in rows:
            payload = _json_object(row["payload_json"])
            value = payload.get("worktree_path")
            if isinstance(value, str) and self._path_key(Path(value)) == expected:
                return row
        return None

    def _complete_pending_execute_intent(
        self,
        *,
        attempt_id: str,
        result_oid: str,
        resulting_state: str,
        evidence: Mapping[str, Any],
    ) -> None:
        with self.state_store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM operation_intents
                WHERE operation = 'execute-integration' AND status = 'PENDING'
                ORDER BY created_at
                """
            ).fetchall()
        for row in rows:
            payload = _json_object(row["payload_json"])
            if payload.get("attempt_id") == attempt_id:
                self.state_store.complete_intent(
                    row["id"],
                    resulting_state=resulting_state,
                    resulting_oid=result_oid,
                    evidence=evidence,
                )

    def _quarantine_attempt(
        self,
        row: sqlite3.Row,
        *,
        classification: str,
        reason: str,
        observed: Mapping[str, Any],
    ) -> IntegrationAttempt:
        finding_id = self._record_recovery_finding(
            attempt_id=row["id"],
            goal_id=row["goal_id"],
            subject_oid=row["result_oid"],
            classification=classification,
            severity="ERROR",
            expected={
                "base_oid": row["base_oid"],
                "state": "UNAMBIGUOUS_RETRY_OR_COMPLETION",
            },
            observed={"reason": reason, **dict(observed)},
        )
        evidence = _json_object(row["evidence_json"])
        evidence.update(
            {
                "classification": classification,
                "quarantine_reason": reason,
                "recovery_finding_id": finding_id,
                "evidence_ids": sorted(
                    set(evidence.get("evidence_ids", [])) | {finding_id}
                ),
            }
        )
        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE integration_attempts
                SET state = 'QUARANTINED', evidence_json = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE id = ?
                """,
                (compact_json(evidence), utc_now(), row["id"]),
            )
        self._finish_integration_lease(row["id"], state="QUARANTINED")
        self._record_recovery_audit(
            row["id"], row["goal_id"], row["result_oid"], classification
        )
        return self._load_attempt(row["id"])

    def _record_conflict_finding(
        self, plan: IntegrationPlan, evidence: ConflictEvidence
    ) -> str:
        return self._record_recovery_finding(
            attempt_id=plan.attempt_id,
            goal_id=plan.goal_id,
            subject_oid=None,
            classification="MERGE_CONFLICT",
            severity="ERROR",
            expected={
                "goal_id": plan.goal_id,
                "attempt_id": plan.attempt_id,
                "subject_oid": None,
                "base_oid": plan.base_oid,
                "ordered_candidate_oids": list(plan.ordered_candidate_oids),
                "next_owner_role": "pl",
                "required_action": "CREATE_NEW_REVISION_AND_NEW_ATTEMPT",
                "repair_integration_worktree": False,
            },
            observed=self._conflict_to_json(evidence),
        )

    def _record_recovery_finding(
        self,
        *,
        attempt_id: str,
        goal_id: str | None,
        subject_oid: str | None,
        classification: str,
        severity: str,
        expected: Mapping[str, Any],
        observed: Mapping[str, Any],
    ) -> str:
        attempt_id = require_identifier(attempt_id, "attempt_id")
        classification = require_identifier(classification, "classification")
        finding_id = _stable_identifier(
            "finding", attempt_id, classification, compact_json(observed)
        )
        key = f"integration-finding:{finding_id}"
        expected_payload = {
            "goal_id": goal_id,
            "attempt_id": attempt_id,
            "subject_oid": subject_oid,
            **dict(expected),
        }
        observed_payload = {
            "goal_id": goal_id,
            "attempt_id": attempt_id,
            "subject_oid": subject_oid,
            "classification": classification,
            **dict(observed),
        }
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
                    ) VALUES (
                        ?, 'integration-attempt', ?, ?, 'OPEN', ?, ?, ?, ?
                    )
                    """,
                    (
                        finding_id,
                        attempt_id,
                        severity,
                        compact_json(expected_payload),
                        compact_json(observed_payload),
                        key,
                        utc_now(),
                    ),
                )
            elif (
                row["expected_json"] != compact_json(expected_payload)
                or row["observed_json"] != compact_json(observed_payload)
            ):
                raise IdempotencyConflictError(
                    "reconciliation finding identity was reused with different evidence"
                )
        return finding_id

    def _environment_record(self) -> dict[str, Any]:
        git_version_result = self._git(
            ["--version"], cwd=self.path_authority.ax_source_root
        )
        raw = {
            "git_version": git_version_result.stdout.strip(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "os_name": os.name,
            "byteorder": sys.byteorder,
            "merge_strategy": "ort",
            "controller_name": CONTROLLER_NAME,
            "controller_email": CONTROLLER_EMAIL,
            "deterministic_commit_time": DETERMINISTIC_COMMIT_TIME,
        }
        raw["fingerprint"] = hashlib.sha256(
            compact_json(raw).encode("utf-8")
        ).hexdigest()
        return raw

    def _git(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
    ) -> GitCommandResult:
        try:
            return self.command_runner.run(arguments, cwd=cwd, check=check)
        except GitCommandError:
            raise
        except Exception as exc:
            raise IntegrationExecutionError(
                f"Git executor failed: {_sanitize(str(exc))}"
            ) from exc

    def _integration_branch(self, goal_id: str, attempt_id: str) -> str:
        return (
            "refs/heads/ax/integration/"
            f"{_safe_ref_component(goal_id)}/{_safe_ref_component(attempt_id)}"
        )

    def _integration_ref(self, goal_id: str, attempt_id: str) -> str:
        return (
            f"{EVIDENCE_REF_PREFIX}integration/"
            f"{_safe_ref_component(goal_id)}/{_safe_ref_component(attempt_id)}"
        )

    def _integration_step_ref(
        self, goal_id: str, attempt_id: str, ordinal: int
    ) -> str:
        return (
            f"{EVIDENCE_REF_PREFIX}integration-steps/"
            f"{_safe_ref_component(goal_id)}/"
            f"{_safe_ref_component(attempt_id)}/{ordinal}"
        )

    def _failed_ref(self, goal_id: str, attempt_id: str) -> str:
        return (
            f"{EVIDENCE_REF_PREFIX}failed/integration/"
            f"{_safe_ref_component(goal_id)}/{_safe_ref_component(attempt_id)}"
        )

    def _worktree_observation(
        self, repository: Path, goal_id: str, attempt_id: str
    ) -> dict[str, Any]:
        path = self._integration_worktree_path(goal_id, attempt_id)
        entry = self.repository_service._worktree_entries(repository).get(
            self._path_key(path)
        )
        observation: dict[str, Any] = {
            "path": str(path),
            "present": entry is not None,
            "path_exists": path.exists(),
            "entry": dict(entry) if entry is not None else None,
        }
        if entry is not None and path.is_dir():
            status = self._git(
                ["status", "--porcelain=v1", "--untracked-files=all"],
                cwd=path,
                check=False,
            )
            unmerged = self._git(
                ["ls-files", "-u", "-z"], cwd=path, check=False
            )
            observation["status"] = _command_evidence(status)
            observation["has_unmerged_index"] = bool(unmerged.stdout)
        return observation

    @staticmethod
    def _path_key(path: Path) -> str:
        value = os.path.normcase(str(path.expanduser().resolve()))
        if os.name == "nt":
            value = value.casefold()
        return value.replace("\\", "/").rstrip("/")

    def _load_attempt(self, attempt_id: str) -> IntegrationAttempt:
        with self.state_store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM integration_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            candidates = connection.execute(
                """
                SELECT candidate_oid FROM attempt_candidates
                WHERE attempt_id = ? ORDER BY ordinal
                """,
                (attempt_id,),
            ).fetchall()
        if row is None:
            raise KeyError(attempt_id)
        return self._attempt_from_row(row, candidates)

    def _attempt_from_row(
        self,
        row: sqlite3.Row,
        candidates: Sequence[sqlite3.Row] | None = None,
    ) -> IntegrationAttempt:
        if candidates is None:
            with self.state_store.transaction() as connection:
                candidates = connection.execute(
                    """
                    SELECT candidate_oid FROM attempt_candidates
                    WHERE attempt_id = ? ORDER BY ordinal
                    """,
                    (row["id"],),
                ).fetchall()
        evidence = _json_object(row["evidence_json"])
        raw_ids = evidence.get("evidence_ids", [])
        if not isinstance(raw_ids, list) or any(
            not isinstance(item, str) for item in raw_ids
        ):
            raise IntegrationAttemptConflictError(
                "attempt evidence_ids must be a JSON string array"
            )
        return IntegrationAttempt(
            attempt_id=row["id"],
            plan_id=row["plan_id"],
            goal_id=row["goal_id"],
            base_oid=row["base_oid"],
            ordered_candidate_oids=tuple(
                candidate["candidate_oid"] for candidate in candidates
            ),
            merge_strategy=row["merge_strategy"],
            state=IntegrationAttemptState(row["state"]),
            idempotency_key=row["idempotency_key"],
            result_oid=row["result_oid"],
            evidence_ids=tuple(raw_ids),
        )

    @staticmethod
    def _conflict_to_json(evidence: ConflictEvidence) -> dict[str, Any]:
        return {
            "attempt_id": evidence.attempt_id,
            "base_oid": evidence.base_oid,
            "candidate_oid": evidence.candidate_oid,
            "candidate_ordinal": evidence.candidate_ordinal,
            "ordered_candidate_oids": list(evidence.ordered_candidate_oids),
            "merge_bases": list(evidence.merge_bases),
            "partial_head_oid": evidence.partial_head_oid,
            "conflict_paths": list(evidence.conflict_paths),
            "unmerged_index": [asdict(item) for item in evidence.unmerged_index],
            "commands": [dict(item) for item in evidence.commands],
            "git_version": evidence.git_version,
            "environment_fingerprint": evidence.environment_fingerprint,
            "failed_ref": evidence.failed_ref,
        }

    @staticmethod
    def _conflict_from_json(raw: Mapping[str, Any]) -> ConflictEvidence:
        return ConflictEvidence(
            attempt_id=str(raw["attempt_id"]),
            base_oid=str(raw["base_oid"]),
            candidate_oid=str(raw["candidate_oid"]),
            candidate_ordinal=int(raw["candidate_ordinal"]),
            ordered_candidate_oids=tuple(raw["ordered_candidate_oids"]),
            merge_bases=tuple(raw["merge_bases"]),
            partial_head_oid=str(raw["partial_head_oid"]),
            conflict_paths=tuple(raw["conflict_paths"]),
            unmerged_index=tuple(
                UnmergedIndexEntry(**item) for item in raw["unmerged_index"]
            ),
            commands=tuple(raw["commands"]),
            git_version=str(raw["git_version"]),
            environment_fingerprint=str(raw["environment_fingerprint"]),
            failed_ref=str(raw["failed_ref"]),
        )

    def _assert_recovery_attempt(
        self,
        existing: sqlite3.Row,
        *,
        source: sqlite3.Row,
        candidates: Sequence[sqlite3.Row],
        recovery_key: str,
    ) -> None:
        if (
            existing["plan_id"] != source["plan_id"]
            or existing["goal_id"] != source["goal_id"]
            or existing["base_oid"] != source["base_oid"]
            or existing["merge_strategy"] != source["merge_strategy"]
            or existing["idempotency_key"] != recovery_key
        ):
            raise IntegrationAttemptConflictError(
                "recovery attempt ID is already bound to different input"
            )
        with self.state_store.transaction() as connection:
            stored = connection.execute(
                """
                SELECT candidate_id, candidate_oid
                FROM attempt_candidates
                WHERE attempt_id = ? ORDER BY ordinal
                """,
                (existing["id"],),
            ).fetchall()
        if tuple(
            (row["candidate_id"], row["candidate_oid"]) for row in stored
        ) != tuple(
            (row["candidate_id"], row["candidate_oid"]) for row in candidates
        ):
            raise IntegrationAttemptConflictError(
                "recovery attempt candidate order differs"
            )

    def _hit_boundary(self, boundary: str, attempt_id: str) -> None:
        if self.boundary_hook is not None:
            self.boundary_hook(boundary, attempt_id)

    def _record_attempt_audit(
        self,
        intent_id: str,
        plan: IntegrationPlan,
        event_type: str,
        result_oid: str | None,
        evidence: Mapping[str, Any],
    ) -> None:
        idempotency_key = f"audit:{intent_id}:{event_type}"
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=_stable_identifier("audit", intent_id, event_type),
                event_type=event_type,
                actor=ServiceIdentity.INTEGRATION_CONTROLLER.value,
                subject_type="integration-attempt",
                subject_id=plan.attempt_id,
                goal_id=plan.goal_id,
                subject_oid=result_oid,
                payload={
                    "intent_id": intent_id,
                    "plan_id": plan.plan_id,
                    "base_oid": plan.base_oid,
                    "ordered_candidate_oids": list(plan.ordered_candidate_oids),
                    "merge_strategy": plan.merge_strategy,
                    "evidence": evidence,
                },
                occurred_at=self._audit_occurred_at(idempotency_key),
                idempotency_key=idempotency_key,
            )
        )

    def _record_recovery_audit(
        self,
        attempt_id: str,
        goal_id: str,
        subject_oid: str | None,
        classification: str,
    ) -> None:
        key = f"recovery:{attempt_id}:{classification}"
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=_stable_identifier("audit", key),
                event_type="INTEGRATION_RECOVERY_CLASSIFIED",
                actor=ServiceIdentity.INTEGRATION_CONTROLLER.value,
                subject_type="integration-attempt",
                subject_id=attempt_id,
                goal_id=goal_id,
                subject_oid=subject_oid,
                payload={"classification": classification},
                occurred_at=self._audit_occurred_at(key),
                idempotency_key=key,
            )
        )

    def _record_narrowing_audit(
        self, source_attempt_id: str, observation: ReplayObservation
    ) -> None:
        key = f"narrow:{source_attempt_id}:{observation.attempt_id}"
        with self.state_store.transaction() as connection:
            source = connection.execute(
                "SELECT goal_id FROM integration_attempts WHERE id = ?",
                (source_attempt_id,),
            ).fetchone()
        assert source is not None
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=_stable_identifier("audit", key),
                event_type="INTEGRATION_NARROWING_REPLAY_RECORDED",
                actor=ServiceIdentity.INTEGRATION_CONTROLLER.value,
                subject_type="integration-attempt",
                subject_id=observation.attempt_id,
                goal_id=source["goal_id"],
                subject_oid=observation.result_oid,
                payload={
                    "source_attempt_id": source_attempt_id,
                    "candidate_oids": list(observation.candidate_oids),
                    "state": observation.state.value,
                    "probe_failed": observation.probe_failed,
                    "automatic_assignment": False,
                },
                occurred_at=self._audit_occurred_at(key),
                idempotency_key=key,
            )
        )

    def _audit_occurred_at(self, idempotency_key: str) -> str:
        """Reuse the first audit timestamp so an exact replay stays identical."""

        with self.state_store.transaction() as connection:
            existing = connection.execute(
                "SELECT occurred_at FROM audit_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return existing["occurred_at"] if existing is not None else utc_now()
