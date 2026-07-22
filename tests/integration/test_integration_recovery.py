from __future__ import annotations

import unittest

from scripts.agent_team_domain import IntegrationAttemptState
from scripts.agent_team_integration import (
    BOUNDARY_FINAL_REF_CREATED,
    BOUNDARY_INTENT_RECORDED,
    BOUNDARY_RESULT_RECORDED,
    BOUNDARY_STEP_EFFECT_APPLIED,
    BOUNDARY_STEP_RECORDED,
    BOUNDARY_WORKTREE_READY,
    BOUNDARY_WORKTREE_REMOVED,
    IntegrationController,
    IntegrationControllerInterrupted,
    IntegrationReplayLimitError,
)
from scripts.agent_team_recovery import (
    ReconciliationClassification,
    RecoveryReconciler,
)
from tests.integration.test_integration_attempts import IntegrationHarness


class OneShotCrash:
    def __init__(self, boundary: str) -> None:
        self.boundary = boundary
        self.triggered = False

    def __call__(self, boundary: str, attempt_id: str) -> None:
        if boundary == self.boundary and not self.triggered:
            self.triggered = True
            raise IntegrationControllerInterrupted(
                f"injected crash at {boundary} for {attempt_id}"
            )


class IntegrationRecoveryTests(IntegrationHarness, unittest.TestCase):
    def _recover_after_boundary(
        self, boundary: str
    ) -> IntegrationAttemptState:
        candidate_id, candidate_oid = self.create_candidate(
            1, path="boundary.txt", content=f"{boundary}\n"
        )
        plan = self.approve_plan((candidate_id,), (candidate_oid,))
        crashing = IntegrationController(
            state_store=self.store,
            repository_service=self.service,
            path_authority=self.authority,
            gate_coordinator=self.gates,
            boundary_hook=OneShotCrash(boundary),
        )
        with self.assertRaises(IntegrationControllerInterrupted):
            crashing.execute(plan)
        normal = IntegrationController(
            state_store=self.store,
            repository_service=self.service,
            path_authority=self.authority,
            gate_coordinator=self.gates,
        )
        return normal.recover_interrupted("attempt-1").state

    def test_completed_git_effect_is_reconciled_only_after_ref_observation(self) -> None:
        first_id, first_oid = self.create_candidate(
            1, path="one.txt", content="one\n"
        )
        second_id, second_oid = self.create_candidate(
            2, path="two.txt", content="two\n"
        )
        plan = self.approve_plan(
            (first_id, second_id), (first_oid, second_oid)
        )
        crash = OneShotCrash(BOUNDARY_FINAL_REF_CREATED)
        crashing = IntegrationController(
            state_store=self.store,
            repository_service=self.service,
            path_authority=self.authority,
            gate_coordinator=self.gates,
            boundary_hook=crash,
        )

        with self.assertRaises(IntegrationControllerInterrupted):
            crashing.execute(plan)
        with self.store.transaction() as connection:
            before = connection.execute(
                "SELECT * FROM integration_attempts WHERE id = 'attempt-1'"
            ).fetchone()
        self.assertEqual("MERGING", before["state"])
        self.assertIsNone(before["result_oid"])

        normal = IntegrationController(
            state_store=self.store,
            repository_service=self.service,
            path_authority=self.authority,
            gate_coordinator=self.gates,
        )
        reconciler = RecoveryReconciler(
            state_store=self.store,
            repository_service=self.service,
            path_authority=self.authority,
            integration_controller=normal,
        )
        findings = reconciler.scan()
        execute_findings = [
            finding
            for finding in findings
            if finding.resource_type == "operation-intent"
            and finding.expected.get("attempt_id") == "attempt-1"
            and finding.observed.get("classification")
            == ReconciliationClassification.COMPLETED_BUT_UNRECORDED.value
        ]
        self.assertEqual(1, len(execute_findings))

        result = reconciler.reconcile(execute_findings[0].finding_id)
        replay = reconciler.reconcile(execute_findings[0].finding_id)
        self.assertEqual(result, replay)
        self.assertEqual(
            ReconciliationClassification.COMPLETED_BUT_UNRECORDED,
            result.classification,
        )
        self.assertEqual("INTEGRATION_RESULT_RECEIPT_COMPLETED", result.action)
        recovered = normal._load_attempt("attempt-1")
        self.assertEqual(IntegrationAttemptState.QA_PENDING, recovered.state)
        self.assertEqual(result.resulting_oid, recovered.result_oid)
        self.assertFalse(
            self.authority.integration_worktree("goal-1", "attempt-1").exists()
        )
        with self.store.transaction() as connection:
            intent = connection.execute(
                """
                SELECT * FROM operation_intents
                WHERE operation = 'execute-integration'
                """
            ).fetchone()
        self.assertEqual("COMPLETED", intent["status"])

    def test_no_effect_crash_creates_new_recovery_attempt_and_keeps_history(self) -> None:
        candidate_id, candidate_oid = self.create_candidate(
            1, path="one.txt", content="one\n"
        )
        plan = self.approve_plan((candidate_id,), (candidate_oid,))
        crash = OneShotCrash(BOUNDARY_INTENT_RECORDED)
        crashing = IntegrationController(
            state_store=self.store,
            repository_service=self.service,
            path_authority=self.authority,
            gate_coordinator=self.gates,
            boundary_hook=crash,
        )
        with self.assertRaises(IntegrationControllerInterrupted):
            crashing.execute(plan)

        normal = IntegrationController(
            state_store=self.store,
            repository_service=self.service,
            path_authority=self.authority,
            gate_coordinator=self.gates,
        )
        recovered = normal.recover_interrupted("attempt-1")
        replay = normal.recover_interrupted("attempt-1")
        self.assertEqual(recovered, replay)
        self.assertEqual(IntegrationAttemptState.RECREATED, recovered.state)
        self.assertNotEqual("attempt-1", recovered.attempt_id)
        self.assertEqual((candidate_oid,), recovered.ordered_candidate_oids)
        with self.store.transaction() as connection:
            original = connection.execute(
                "SELECT * FROM integration_attempts WHERE id = 'attempt-1'"
            ).fetchone()
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM integration_attempts"
            ).fetchone()["count"]
        self.assertEqual("QUARANTINED", original["state"])
        self.assertEqual(2, count)
        self.assertFalse(
            self.authority.integration_worktree("goal-1", "attempt-1").exists()
        )

    def test_partial_step_without_final_ref_is_quarantined_not_inferred_success(self) -> None:
        candidate_id, candidate_oid = self.create_candidate(
            1, path="one.txt", content="one\n"
        )
        plan = self.approve_plan((candidate_id,), (candidate_oid,))
        crash = OneShotCrash(BOUNDARY_STEP_EFFECT_APPLIED)
        crashing = IntegrationController(
            state_store=self.store,
            repository_service=self.service,
            path_authority=self.authority,
            gate_coordinator=self.gates,
            boundary_hook=crash,
        )
        with self.assertRaises(IntegrationControllerInterrupted):
            crashing.execute(plan)

        normal = IntegrationController(
            state_store=self.store,
            repository_service=self.service,
            path_authority=self.authority,
            gate_coordinator=self.gates,
        )
        recovered = normal.recover_interrupted("attempt-1")
        self.assertEqual(IntegrationAttemptState.QUARANTINED, recovered.state)
        self.assertIsNone(recovered.result_oid)
        with self.store.transaction() as connection:
            finding = connection.execute(
                """
                SELECT * FROM reconciliation_findings
                WHERE resource_id = 'attempt-1'
                  AND state = 'OPEN'
                ORDER BY detected_at DESC
                LIMIT 1
                """
            ).fetchone()
        self.assertIsNotNone(finding)

    def test_ready_worktree_without_merge_effect_is_conservatively_quarantined(
        self,
    ) -> None:
        state = self._recover_after_boundary(BOUNDARY_WORKTREE_READY)
        self.assertEqual(IntegrationAttemptState.QUARANTINED, state)

    def test_recorded_step_without_final_ref_is_quarantined(self) -> None:
        state = self._recover_after_boundary(BOUNDARY_STEP_RECORDED)
        self.assertEqual(IntegrationAttemptState.QUARANTINED, state)

    def test_recorded_result_is_reconciled_and_disposable_worktree_removed(
        self,
    ) -> None:
        state = self._recover_after_boundary(BOUNDARY_RESULT_RECORDED)
        self.assertEqual(IntegrationAttemptState.QA_PENDING, state)
        self.assertFalse(
            self.authority.integration_worktree("goal-1", "attempt-1").exists()
        )

    def test_removed_worktree_boundary_reconciles_completed_attempt(self) -> None:
        state = self._recover_after_boundary(BOUNDARY_WORKTREE_REMOVED)
        self.assertEqual(IntegrationAttemptState.QA_PENDING, state)
        self.assertFalse(
            self.authority.integration_worktree("goal-1", "attempt-1").exists()
        )

    def test_bounded_narrowing_records_each_replay_and_never_assigns(self) -> None:
        first_id, first_oid = self.create_candidate(
            1, path="one.txt", content="one\n"
        )
        second_id, second_oid = self.create_candidate(
            2, path="two.txt", content="two\n"
        )
        plan = self.approve_plan(
            (first_id, second_id), (first_oid, second_oid)
        )
        merged = self.controller.execute(plan)
        self.assertEqual(IntegrationAttemptState.QA_PENDING, merged.state)
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE integration_attempts SET state = 'QA_FAILED'
                WHERE id = 'attempt-1'
                """
            )

        narrowing = IntegrationController(
            state_store=self.store,
            repository_service=self.service,
            path_authority=self.authority,
            gate_coordinator=self.gates,
            replay_probe=lambda _oid, candidates: candidates == (second_oid,),
        )
        result = narrowing.narrow_failure_candidates(
            "attempt-1", max_replays=2
        )
        self.assertEqual((second_oid,), result.candidate_oids)
        self.assertEqual(2, len(result.replay_attempt_ids))
        self.assertEqual(2, len(result.observations))
        self.assertFalse(result.exhausted)
        self.assertIsNone(result.assignment_owner)
        self.assertEqual(
            [False, True],
            [observation.probe_failed for observation in result.observations],
        )
        with self.store.transaction() as connection:
            replay_rows = connection.execute(
                """
                SELECT COUNT(*) AS count FROM integration_attempts
                WHERE id IN (?, ?)
                """,
                result.replay_attempt_ids,
            ).fetchone()["count"]
            audit_rows = connection.execute(
                """
                SELECT COUNT(*) AS count FROM audit_events
                WHERE event_type = 'INTEGRATION_NARROWING_REPLAY_RECORDED'
                """
            ).fetchone()["count"]
        self.assertEqual(2, replay_rows)
        self.assertEqual(2, audit_rows)

        with self.assertRaises(IntegrationReplayLimitError):
            narrowing.narrow_failure_candidates("attempt-1", max_replays=0)
        with self.assertRaises(IntegrationReplayLimitError):
            narrowing.narrow_failure_candidates("attempt-1", max_replays=33)


if __name__ == "__main__":
    unittest.main()
