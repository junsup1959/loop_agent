from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.agent_team_domain import (
    GateDecision,
    GateDecisionValue,
    GateType,
)
from scripts.agent_team_gates import (
    GateAuthorityError,
    GateCoordinator,
    GateDecisionConflictError,
    GateEvidenceError,
    GateSelfApprovalError,
    GateSequenceError,
    PromotionInvariantError,
)
from scripts.agent_team_state import AxStateStore


OID_BASE = "1" * 40
OID_CANDIDATE = "2" * 40
OID_INTEGRATION = "3" * 40
OID_WRONG = "4" * 40
NOW = "2026-07-21T00:00:00.000000+00:00"


class GateInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ax-gates-")
        self.addCleanup(self.temporary.cleanup)
        self.store = AxStateStore(Path(self.temporary.name) / "ax.db")
        self.store.initialize()
        self.coordinator = GateCoordinator(self.store)
        self._insert_base_graph()

    def test_wrong_role_self_approval_missing_evidence_and_wrong_subject_fail(self) -> None:
        self._insert_candidate(owner="ta")
        self._insert_activation(
            "activation-ta-self",
            role="ta",
            subject_oid=OID_CANDIDATE,
            gate_or_task="code-review",
            seat_id="ta",
        )
        self._insert_review(
            "review-ta-self",
            activation_id="activation-ta-self",
            review_type="CODE_QUALITY",
        )
        with self.assertRaises(GateSelfApprovalError):
            self.coordinator.record_decision(
                self._decision(
                    "gate-ta-self",
                    "activation-ta-self",
                    GateType.TA_CODE_QUALITY,
                    "ta",
                    OID_CANDIDATE,
                    ("review-ta-self",),
                )
            )

        with self.assertRaises(GateAuthorityError):
            self.coordinator.record_decision(
                self._decision(
                    "gate-wrong-role",
                    "activation-ta-self",
                    GateType.TA_CODE_QUALITY,
                    "pl",
                    OID_CANDIDATE,
                    ("review-ta-self",),
                )
            )

        self._set_candidate_owner("dev_1")
        with self.assertRaises(GateEvidenceError):
            self.coordinator.record_decision(
                self._decision(
                    "gate-missing-evidence",
                    "activation-ta-self",
                    GateType.TA_CODE_QUALITY,
                    "ta",
                    OID_CANDIDATE,
                    ("review-does-not-exist",),
                )
            )

        self._insert_activation(
            "activation-ta-wrong-subject",
            role="ta",
            subject_oid=OID_WRONG,
            gate_or_task="code-review",
        )
        with self.assertRaises(GateAuthorityError):
            self.coordinator.record_decision(
                self._decision(
                    "gate-wrong-subject",
                    "activation-ta-wrong-subject",
                    GateType.TA_CODE_QUALITY,
                    "ta",
                    OID_CANDIDATE,
                    ("review-ta-self",),
                )
            )

    def test_gate_sequence_duplicate_guard_and_exact_promotion_invariant(self) -> None:
        self._approve_candidate()
        selection = self._record_selection()
        self._insert_integration_attempt(selection.decision_id)

        self._insert_activation(
            "activation-build-early",
            role="build_release",
            subject_oid=OID_INTEGRATION,
            gate_or_task="build-validation",
        )
        self._insert_build_run(
            "build-early", "activation-build-early", state="PASSED"
        )
        with self.assertRaises(GateSequenceError):
            self.coordinator.record_decision(
                self._decision(
                    "gate-build-early",
                    "activation-build-early",
                    GateType.BUILD,
                    "build_release",
                    OID_INTEGRATION,
                    ("build-early",),
                )
            )

        qa = self._record_qa()
        build = self._record_build()
        pl = self._record_pl_integration(qa.decision_id, build.decision_id)
        pm = self._record_pm(qa.decision_id, build.decision_id, pl.decision_id)

        self.assertEqual(
            OID_INTEGRATION,
            self.coordinator.assert_promotion_invariant("goal-1"),
        )
        required = self.coordinator.required_decisions(
            "goal-1", OID_INTEGRATION
        )
        self.assertEqual(
            [
                GateType.TA_CODE_QUALITY,
                GateType.TA_ARCHITECTURE,
                GateType.PL_CANDIDATE_SELECTION,
                GateType.QA_QUALITY,
                GateType.BUILD,
                GateType.PL_INTEGRATION,
                GateType.PM_REQUIREMENTS,
            ],
            [decision.gate_type for decision in required],
        )
        self.assertEqual(pm.decision_id, required[-1].decision_id)

        duplicate = self._decision(
            "gate-pm-duplicate",
            "activation-pm",
            GateType.PM_REQUIREMENTS,
            "pm",
            OID_INTEGRATION,
            (qa.decision_id, build.decision_id, pl.decision_id),
        )
        with self.assertRaises(GateDecisionConflictError):
            self.coordinator.record_decision(duplicate)

        with self.assertRaises(PromotionInvariantError):
            self.coordinator.assert_promotion_invariant("goal-1", OID_WRONG)

    def test_qa_failure_retains_oid_and_routes_request_to_pl_without_assignment(self) -> None:
        self._approve_candidate()
        selection = self._record_selection()
        self._insert_integration_attempt(selection.decision_id)
        before_revisions = self._count("work_revisions")

        self._insert_activation(
            "activation-qa-failed",
            role="qa_sdet",
            subject_oid=OID_INTEGRATION,
            gate_or_task="integration-validation",
        )
        self._insert_quality_run(
            "quality-failed",
            "activation-qa-failed",
            state="FAILED",
        )
        rejected = self.coordinator.record_decision(
            self._decision(
                "gate-qa-failed",
                "activation-qa-failed",
                GateType.QA_QUALITY,
                "qa_sdet",
                OID_INTEGRATION,
                ("quality-failed",),
                value=GateDecisionValue.NEEDS_REWORK,
            )
        )
        self.assertEqual(GateDecisionValue.NEEDS_REWORK, rejected.decision)

        with self.store.transaction() as connection:
            attempt = connection.execute(
                "SELECT * FROM integration_attempts WHERE id = 'attempt-1'"
            ).fetchone()
            rework = connection.execute(
                """
                SELECT * FROM reconciliation_findings
                WHERE resource_type = 'pl-rework-request'
                """
            ).fetchone()
        self.assertEqual("QA_FAILED", attempt["state"])
        self.assertEqual(OID_INTEGRATION, attempt["result_oid"])
        self.assertIsNotNone(rework)
        expected = json.loads(rework["expected_json"])
        observed = json.loads(rework["observed_json"])
        self.assertEqual("pl", expected["owner_role"])
        self.assertEqual("dev_1", expected["preferred_owner"])
        self.assertFalse(expected["repair_integration_worktree"])
        self.assertEqual("qa_sdet", observed["requested_by_role"])
        self.assertEqual(before_revisions, self._count("work_revisions"))

    def _insert_base_graph(self) -> None:
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO targets (
                    id, canonical_checkout_path, git_common_dir, source_ref,
                    observed_source_oid, state, created_at, updated_at
                ) VALUES (
                    'target-1', 'C:/fixture/target', 'C:/fixture/target/.git',
                    'refs/heads/main', ?, 'ACTIVE', ?, ?
                )
                """,
                (OID_BASE, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO managed_repositories (
                    id, target_id, repository_path, state, created_at, updated_at
                ) VALUES (
                    'managed-1', 'target-1', 'C:/fixture/runtime/repository.git',
                    'READY', ?, ?
                )
                """,
                (NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO goals (
                    id, target_id, base_oid, state, created_at, updated_at
                ) VALUES ('goal-1', 'target-1', ?, 'ACTIVE', ?, ?)
                """,
                (OID_BASE, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO runs (
                    id, goal_id, target_id, base_oid, state,
                    idempotency_key, created_at
                ) VALUES (
                    'run-1', 'goal-1', 'target-1', ?, 'RUNNING',
                    'run-key-1', ?
                )
                """,
                (OID_BASE, NOW),
            )

    def _insert_candidate(self, *, owner: str = "dev_1") -> None:
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO work_items (
                    id, goal_id, title, assigned_owner,
                    source_write_scope_json, state, created_at, updated_at
                ) VALUES (
                    'work-1', 'goal-1', 'Candidate work', ?,
                    '["src"]', 'REVIEW_PENDING', ?, ?
                )
                """,
                (owner, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO work_revisions (
                    id, work_item_id, revision, owner, base_oid, head_oid,
                    state, idempotency_key, created_at, updated_at
                ) VALUES (
                    'revision-1', 'work-1', 1, ?, ?, ?,
                    'SUBMITTED', 'revision-key-1', ?, ?
                )
                """,
                (owner, OID_BASE, OID_CANDIDATE, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, target_id, goal_id, kind, path, branch_ref, subject_oid,
                    state, created_at, updated_at
                ) VALUES (
                    'workspace-1', 'target-1', 'goal-1', 'DEVELOPMENT',
                    'C:/fixture/runtime/workspace-1',
                    'refs/heads/ax/work/goal-1/work-1/1', ?,
                    'ACTIVE', ?, ?
                )
                """,
                (OID_CANDIDATE, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO workspace_leases (
                    id, workspace_id, target_id, goal_id, work_item_id,
                    revision_id, owner, branch_ref, worktree_path, base_oid,
                    expected_head_oid, source_write_scope_json,
                    generated_write_scope_json, state, expires_at,
                    idempotency_key, created_at
                ) VALUES (
                    'lease-1', 'workspace-1', 'target-1', 'goal-1', 'work-1',
                    'revision-1', ?, 'refs/heads/ax/work/goal-1/work-1/1',
                    'C:/fixture/runtime/workspace-1', ?, ?,
                    '["src"]', '[]', 'ACTIVE', ?, 'lease-key-1', ?
                )
                """,
                (owner, OID_BASE, OID_CANDIDATE, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO candidate_submissions (
                    id, goal_id, work_item_id, revision_id, lease_id, branch_ref,
                    expected_previous_oid, candidate_oid,
                    self_test_evidence_json, state, idempotency_key, created_at
                ) VALUES (
                    'candidate-1', 'goal-1', 'work-1', 'revision-1', 'lease-1',
                    'refs/heads/ax/work/goal-1/work-1/1', ?, ?,
                    '["self-test-1"]', 'SUBMITTED', 'candidate-key-1', ?
                )
                """,
                (OID_BASE, OID_CANDIDATE, NOW),
            )

    def _set_candidate_owner(self, owner: str) -> None:
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE work_items SET assigned_owner = ? WHERE id = 'work-1'",
                (owner,),
            )
            connection.execute(
                "UPDATE work_revisions SET owner = ? WHERE id = 'revision-1'",
                (owner,),
            )
            connection.execute(
                "UPDATE workspace_leases SET owner = ? WHERE id = 'lease-1'",
                (owner,),
            )

    def _approve_candidate(self) -> None:
        self._insert_candidate()
        for suffix, gate_type, review_type in (
            ("code", GateType.TA_CODE_QUALITY, "CODE_QUALITY"),
            ("architecture", GateType.TA_ARCHITECTURE, "ARCHITECTURE"),
        ):
            activation_id = f"activation-ta-{suffix}"
            review_id = f"review-ta-{suffix}"
            self._insert_activation(
                activation_id,
                role="ta",
                subject_oid=OID_CANDIDATE,
                gate_or_task=f"{suffix}-review",
                seat_id="ta_1",
            )
            self._insert_review(
                review_id,
                activation_id=activation_id,
                review_type=review_type,
            )
            self.coordinator.record_decision(
                self._decision(
                    f"gate-ta-{suffix}",
                    activation_id,
                    gate_type,
                    "ta",
                    OID_CANDIDATE,
                    (review_id,),
                )
            )

    def _record_selection(self) -> GateDecision:
        self._insert_activation(
            "activation-pl-selection",
            role="pl",
            subject_oid=OID_BASE,
            gate_or_task="candidate-selection",
        )
        return self.coordinator.record_decision(
            self._decision(
                "gate-pl-selection",
                "activation-pl-selection",
                GateType.PL_CANDIDATE_SELECTION,
                "pl",
                OID_BASE,
                ("candidate-1",),
            )
        )

    def _insert_integration_attempt(self, selection_id: str) -> None:
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO integration_plans (
                    id, goal_id, base_oid, merge_strategy, pl_decision_id,
                    state, idempotency_key, created_at, approved_at
                ) VALUES (
                    'plan-1', 'goal-1', ?, 'no-ff', ?,
                    'COMPLETED', 'plan-key-1', ?, ?
                )
                """,
                (OID_BASE, selection_id, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO integration_attempts (
                    id, plan_id, goal_id, base_oid, merge_strategy, state,
                    result_oid, evidence_json, environment_json,
                    idempotency_key, created_at, completed_at
                ) VALUES (
                    'attempt-1', 'plan-1', 'goal-1', ?, 'no-ff', 'QA_PENDING',
                    ?, '{"evidence_ids":["integration-ref-1"]}', '{}',
                    'attempt-key-1', ?, ?
                )
                """,
                (OID_BASE, OID_INTEGRATION, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO attempt_candidates (
                    attempt_id, ordinal, candidate_id, candidate_oid
                ) VALUES ('attempt-1', 0, 'candidate-1', ?)
                """,
                (OID_CANDIDATE,),
            )

    def _record_qa(self) -> GateDecision:
        self._insert_activation(
            "activation-qa",
            role="qa_sdet",
            subject_oid=OID_INTEGRATION,
            gate_or_task="integration-validation",
        )
        self._insert_quality_run("quality-1", "activation-qa", state="PASSED")
        return self.coordinator.record_decision(
            self._decision(
                "gate-qa",
                "activation-qa",
                GateType.QA_QUALITY,
                "qa_sdet",
                OID_INTEGRATION,
                ("quality-1",),
            )
        )

    def _record_build(self) -> GateDecision:
        self._insert_activation(
            "activation-build",
            role="build_release",
            subject_oid=OID_INTEGRATION,
            gate_or_task="build-validation",
        )
        self._insert_build_run("build-1", "activation-build", state="PASSED")
        return self.coordinator.record_decision(
            self._decision(
                "gate-build",
                "activation-build",
                GateType.BUILD,
                "build_release",
                OID_INTEGRATION,
                ("build-1",),
            )
        )

    def _record_pl_integration(self, qa_id: str, build_id: str) -> GateDecision:
        self._insert_activation(
            "activation-pl-integration",
            role="pl",
            subject_oid=OID_INTEGRATION,
            gate_or_task="integration-gate",
        )
        return self.coordinator.record_decision(
            self._decision(
                "gate-pl-integration",
                "activation-pl-integration",
                GateType.PL_INTEGRATION,
                "pl",
                OID_INTEGRATION,
                (qa_id, build_id),
            )
        )

    def _record_pm(
        self, qa_id: str, build_id: str, pl_id: str
    ) -> GateDecision:
        self._insert_activation(
            "activation-pm",
            role="pm",
            subject_oid=OID_INTEGRATION,
            gate_or_task="requirements",
        )
        return self.coordinator.record_decision(
            self._decision(
                "gate-pm",
                "activation-pm",
                GateType.PM_REQUIREMENTS,
                "pm",
                OID_INTEGRATION,
                (qa_id, build_id, pl_id),
            )
        )

    def _insert_activation(
        self,
        activation_id: str,
        *,
        role: str,
        subject_oid: str,
        gate_or_task: str,
        seat_id: str | None = None,
    ) -> None:
        result = json.dumps({"seat_id": seat_id} if seat_id else {})
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO activations (
                    id, target_id, goal_id, run_id, sandbox_path, subject_oid,
                    role, gate_or_task, state, result_json, idempotency_key,
                    created_at, updated_at
                ) VALUES (
                    ?, 'target-1', 'goal-1', 'run-1', ?, ?, ?, ?,
                    'RESULT_PERSISTED', ?, ?, ?, ?
                )
                """,
                (
                    activation_id,
                    f"C:/fixture/runtime/sandboxes/{activation_id}",
                    subject_oid,
                    role,
                    gate_or_task,
                    result,
                    f"activation-key:{activation_id}",
                    NOW,
                    NOW,
                ),
            )

    def _insert_review(
        self, review_id: str, *, activation_id: str, review_type: str
    ) -> None:
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO reviews (
                    id, goal_id, candidate_id, activation_id, reviewer_role,
                    review_type, subject_oid, decision, source_integrity,
                    profile_digest, evidence_json, idempotency_key, created_at
                ) VALUES (
                    ?, 'goal-1', 'candidate-1', ?, 'ta', ?, ?,
                    'APPROVED', 'CLEAN', 'profile-digest',
                    '["review-artifact"]', ?, ?
                )
                """,
                (
                    review_id,
                    activation_id,
                    review_type,
                    OID_CANDIDATE,
                    f"review-key:{review_id}",
                    NOW,
                ),
            )

    def _insert_quality_run(
        self, run_id: str, activation_id: str, *, state: str
    ) -> None:
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO quality_runs (
                    id, goal_id, attempt_id, activation_id, subject_oid, state,
                    source_integrity, evidence_json, idempotency_key,
                    created_at, completed_at
                ) VALUES (
                    ?, 'goal-1', 'attempt-1', ?, ?, ?, 'CLEAN',
                    '["quality-artifact"]', ?, ?, ?
                )
                """,
                (
                    run_id,
                    activation_id,
                    OID_INTEGRATION,
                    state,
                    f"quality-key:{run_id}",
                    NOW,
                    NOW,
                ),
            )

    def _insert_build_run(
        self, run_id: str, activation_id: str, *, state: str
    ) -> None:
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO build_runs (
                    id, goal_id, attempt_id, activation_id, subject_oid, state,
                    source_integrity, evidence_json, idempotency_key,
                    created_at, completed_at
                ) VALUES (
                    ?, 'goal-1', 'attempt-1', ?, ?, ?, 'CLEAN',
                    '["build-artifact"]', ?, ?, ?
                )
                """,
                (
                    run_id,
                    activation_id,
                    OID_INTEGRATION,
                    state,
                    f"build-key:{run_id}",
                    NOW,
                    NOW,
                ),
            )

    @staticmethod
    def _decision(
        decision_id: str,
        activation_id: str,
        gate_type: GateType,
        role: str,
        subject_oid: str,
        evidence_ids: tuple[str, ...],
        *,
        value: GateDecisionValue = GateDecisionValue.APPROVED,
    ) -> GateDecision:
        return GateDecision(
            decision_id=decision_id,
            goal_id="goal-1",
            activation_id=activation_id,
            gate_type=gate_type,
            actor_role=role,
            subject_oid=subject_oid,
            decision=value,
            profile_digest="profile-digest",
            evidence_ids=evidence_ids,
            idempotency_key=f"decision-key:{decision_id}",
        )

    def _count(self, table: str) -> int:
        with self.store.transaction() as connection:
            return connection.execute(
                f"SELECT COUNT(*) AS count FROM {table}"
            ).fetchone()["count"]


if __name__ == "__main__":
    unittest.main()
