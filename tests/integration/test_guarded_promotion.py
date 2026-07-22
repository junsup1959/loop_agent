from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.agent_team_domain import (
    GateDecision,
    GateDecisionValue,
    GateType,
    PromotionRequest,
    PromotionState,
)
from scripts.agent_team_gates import PromotionInvariantError
from scripts.agent_team_git import TargetRefAdapter
from scripts.agent_team_promotion import (
    PromotionAuthorizationError,
    PromotionBlockedError,
    PromotionController,
    RollbackAuthorizationError,
)
from scripts.agent_team_state import utc_now
from tests.integration.test_integration_attempts import (
    IntegrationHarness,
    run_git,
)


class GuardedPromotionTests(IntegrationHarness, unittest.TestCase):
    destination_ref = "refs/agentic-ax/approved/goal-1/1"

    def setUp(self) -> None:
        super().setUp()
        self.adapter = TargetRefAdapter(
            state_store=self.store,
            path_authority=self.authority,
        )
        self.promotion = PromotionController(
            state_store=self.store,
            repository_service=self.service,
            target_ref_adapter=self.adapter,
            gate_coordinator=self.gates,
        )

    def all_target_refs(self) -> dict[str, str]:
        output = run_git(
            self.checkout,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
        ).stdout
        return {
            line.split(" ", 1)[0]: line.split(" ", 1)[1]
            for line in output.splitlines()
            if line
        }

    def integrate_candidate(self) -> str:
        candidate_id, candidate_oid = self.create_candidate(
            1,
            path="promoted-feature.txt",
            content="approved feature\n",
        )
        plan = self.approve_plan((candidate_id,), (candidate_oid,))
        attempt = self.controller.execute(plan)
        self.assertIsNotNone(attempt.result_oid)
        return str(attempt.result_oid)

    def record_quality_gate(self, subject_oid: str) -> GateDecision:
        self.record_delivery_v4_result(
            transition_id="qa_validate_integration",
            capability_id="qa_sdet",
            subject_oid=subject_oid,
            result_kind="approved",
            suffix=f"qa-{subject_oid[:12]}",
        )
        activation_id = "activation-qa-quality"
        run_id = "quality-run-1"
        self._insert_activation(
            activation_id,
            role="qa_sdet",
            subject_oid=subject_oid,
            gate_or_task="post-merge-quality",
            seat_id="qa_sdet_1",
        )
        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO quality_runs (
                    id, goal_id, attempt_id, activation_id, subject_oid,
                    state, source_integrity, evidence_json, idempotency_key,
                    created_at, completed_at
                ) VALUES (
                    ?, 'goal-1', 'attempt-1', ?, ?,
                    'PASSED', 'CLEAN', '["qa-test-log"]', ?, ?, ?
                )
                """,
                (
                    run_id,
                    activation_id,
                    subject_oid,
                    f"quality-key:{run_id}",
                    now,
                    now,
                ),
            )
        return self.gates.record_decision(
            GateDecision(
                decision_id="gate-qa-quality",
                goal_id="goal-1",
                activation_id=activation_id,
                gate_type=GateType.QA_QUALITY,
                actor_role="qa_sdet",
                subject_oid=subject_oid,
                decision=GateDecisionValue.APPROVED,
                profile_digest="qa-profile-digest",
                evidence_ids=(run_id,),
                idempotency_key="decision-key:gate-qa-quality",
            )
        )

    def record_build_gate(self, subject_oid: str) -> GateDecision:
        self.record_delivery_v4_result(
            transition_id="build_validate_integration",
            capability_id="build_release",
            subject_oid=subject_oid,
            result_kind="approved",
            suffix=f"build-{subject_oid[:12]}",
        )
        activation_id = "activation-build"
        run_id = "build-run-1"
        self._insert_activation(
            activation_id,
            role="build_release",
            subject_oid=subject_oid,
            gate_or_task="post-merge-build",
            seat_id="build_release_1",
        )
        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO build_runs (
                    id, goal_id, attempt_id, activation_id, subject_oid,
                    state, source_integrity, evidence_json, idempotency_key,
                    created_at, completed_at
                ) VALUES (
                    ?, 'goal-1', 'attempt-1', ?, ?,
                    'PASSED', 'CLEAN', '["build-log"]', ?, ?, ?
                )
                """,
                (
                    run_id,
                    activation_id,
                    subject_oid,
                    f"build-key:{run_id}",
                    now,
                    now,
                ),
            )
        return self.gates.record_decision(
            GateDecision(
                decision_id="gate-build",
                goal_id="goal-1",
                activation_id=activation_id,
                gate_type=GateType.BUILD,
                actor_role="build_release",
                subject_oid=subject_oid,
                decision=GateDecisionValue.APPROVED,
                profile_digest="build-profile-digest",
                evidence_ids=(run_id,),
                idempotency_key="decision-key:gate-build",
            )
        )

    def record_pl_gate(
        self,
        subject_oid: str,
        qa: GateDecision,
        build: GateDecision,
    ) -> GateDecision:
        activation_id = "activation-pl-integration"
        self._insert_activation(
            activation_id,
            role="pl",
            subject_oid=subject_oid,
            gate_or_task="integration-approval",
        )
        return self.gates.record_decision(
            GateDecision(
                decision_id="gate-pl-integration",
                goal_id="goal-1",
                activation_id=activation_id,
                gate_type=GateType.PL_INTEGRATION,
                actor_role="pl",
                subject_oid=subject_oid,
                decision=GateDecisionValue.APPROVED,
                profile_digest="pl-profile-digest",
                evidence_ids=(qa.decision_id, build.decision_id),
                idempotency_key="decision-key:gate-pl-integration",
            )
        )

    def record_pm_gate(
        self,
        subject_oid: str,
        qa: GateDecision,
        build: GateDecision,
        pl: GateDecision,
    ) -> GateDecision:
        self.record_delivery_v4_result(
            transition_id="pm_accept_integration",
            capability_id="pm",
            subject_oid=subject_oid,
            result_kind="approved",
            suffix=f"pm-{subject_oid[:12]}",
        )
        activation_id = "activation-pm-requirements"
        self._insert_activation(
            activation_id,
            role="pm",
            subject_oid=subject_oid,
            gate_or_task="requirements-approval",
        )
        return self.gates.record_decision(
            GateDecision(
                decision_id="gate-pm-requirements",
                goal_id="goal-1",
                activation_id=activation_id,
                gate_type=GateType.PM_REQUIREMENTS,
                actor_role="pm",
                subject_oid=subject_oid,
                decision=GateDecisionValue.APPROVED,
                profile_digest="pm-profile-digest",
                evidence_ids=(
                    qa.decision_id,
                    build.decision_id,
                    pl.decision_id,
                ),
                idempotency_key="decision-key:gate-pm-requirements",
            )
        )

    def record_all_post_merge_gates(
        self, subject_oid: str
    ) -> tuple[GateDecision, GateDecision, GateDecision, GateDecision]:
        qa = self.record_quality_gate(subject_oid)
        build = self.record_build_gate(subject_oid)
        pl = self.record_pl_gate(subject_oid, qa, build)
        pm = self.record_pm_gate(subject_oid, qa, build, pl)
        return qa, build, pl, pm

    def promotion_request(
        self,
        subject_oid: str,
        *,
        request_suffix: str,
        gate_ids: tuple[str, ...],
        destination_ref: str | None = None,
        expected_destination_oid: str | None = None,
    ) -> PromotionRequest:
        return PromotionRequest(
            promotion_id=f"promotion-{request_suffix}",
            goal_id="goal-1",
            target_id=self.target.target_id,
            approved_oid=subject_oid,
            expected_source_oid=self.base_oid,
            destination_ref=destination_ref or self.destination_ref,
            expected_destination_oid=expected_destination_oid,
            required_gate_decision_ids=gate_ids,
            idempotency_key=f"promotion-key:{request_suffix}",
        )

    def required_gate_ids(self, subject_oid: str) -> tuple[str, ...]:
        return tuple(
            decision.decision_id
            for decision in self.gates.required_decisions(
                "goal-1", subject_oid
            )
        )

    def create_managed_history_commit(self, base_oid: str) -> str:
        worktree = self.authority.development_worktree(
            "goal-1", "promotion-history", 1
        )
        branch_ref = "refs/heads/ax/work/goal-1/promotion-history/1"
        receipt = self.service.create_disposable_worktree(
            self.target.target_id,
            oid=base_oid,
            path=worktree,
            branch_ref=branch_ref,
        )
        (worktree / "current-approved.txt").write_text(
            "current approved\n", encoding="utf-8"
        )
        run_git(worktree, "config", "user.name", "AX History Fixture")
        run_git(
            worktree,
            "config",
            "user.email",
            "history@example.invalid",
        )
        run_git(worktree, "add", "current-approved.txt")
        run_git(worktree, "commit", "-m", "current approved")
        current_oid = run_git(worktree, "rev-parse", "HEAD").stdout.strip()
        self.service.remove_disposable_worktree(
            receipt, expected_oid=current_oid
        )
        return current_oid

    def test_promotion_is_same_oid_idempotent_and_checkout_safe(self) -> None:
        subject_oid = self.integrate_candidate()
        self.record_all_post_merge_gates(subject_oid)
        gate_ids = self.required_gate_ids(subject_oid)
        request = self.promotion_request(
            subject_oid,
            request_suffix="success",
            gate_ids=gate_ids,
        )
        before_checkout = self.checkout_state()
        before_refs = self.all_target_refs()

        first = self.promotion.promote(request)
        replay = self.promotion.promote(request)

        self.assertEqual(first, replay)
        self.assertEqual(PromotionState.PROMOTED, first.state)
        self.assertEqual(subject_oid, first.invariant_oid)
        self.assertEqual(subject_oid, first.resulting_oid)
        self.assertEqual(before_checkout, self.checkout_state())
        after_refs = self.all_target_refs()
        self.assertEqual(subject_oid, after_refs[self.destination_ref])
        self.assertEqual(
            before_refs,
            {
                ref: oid
                for ref, oid in after_refs.items()
                if ref != self.destination_ref
            },
        )

    def test_each_missing_gate_and_mismatched_gate_set_blocks_promotion(
        self,
    ) -> None:
        subject_oid = self.integrate_candidate()
        before_refs = self.all_target_refs()
        incomplete = ("gate-pl-selection",)

        with self.assertRaises(PromotionInvariantError):
            self.promotion.promote(
                self.promotion_request(
                    subject_oid,
                    request_suffix="missing-qa",
                    gate_ids=incomplete,
                )
            )
        qa = self.record_quality_gate(subject_oid)
        with self.assertRaises(PromotionInvariantError):
            self.promotion.promote(
                self.promotion_request(
                    subject_oid,
                    request_suffix="missing-build",
                    gate_ids=incomplete,
                )
            )
        build = self.record_build_gate(subject_oid)
        with self.assertRaises(PromotionInvariantError):
            self.promotion.promote(
                self.promotion_request(
                    subject_oid,
                    request_suffix="missing-pl",
                    gate_ids=incomplete,
                )
            )
        pl = self.record_pl_gate(subject_oid, qa, build)
        with self.assertRaises(PromotionInvariantError):
            self.promotion.promote(
                self.promotion_request(
                    subject_oid,
                    request_suffix="missing-pm",
                    gate_ids=incomplete,
                )
            )
        self.record_pm_gate(subject_oid, qa, build, pl)
        required = self.required_gate_ids(subject_oid)

        with self.assertRaises(PromotionAuthorizationError):
            self.promotion.promote(
                self.promotion_request(
                    subject_oid,
                    request_suffix="missing-required-id",
                    gate_ids=required[:-1],
                )
            )
        with self.assertRaises(PromotionAuthorizationError):
            self.promotion.promote(
                self.promotion_request(
                    subject_oid,
                    request_suffix="extra-required-id",
                    gate_ids=(*required, "gate-not-in-chain"),
                )
            )
        with self.assertRaises(PromotionInvariantError):
            self.promotion.promote(
                self.promotion_request(
                    self.base_oid,
                    request_suffix="wrong-oid",
                    gate_ids=required,
                )
            )
        self.assertEqual(before_refs, self.all_target_refs())

    def test_stale_destination_cas_is_persistently_blocked(self) -> None:
        subject_oid = self.integrate_candidate()
        self.record_all_post_merge_gates(subject_oid)
        gate_ids = self.required_gate_ids(subject_oid)
        self.promotion.promote(
            self.promotion_request(
                subject_oid,
                request_suffix="cas-first",
                gate_ids=gate_ids,
            )
        )
        refs_before_stale = self.all_target_refs()
        stale = self.promotion_request(
            subject_oid,
            request_suffix="cas-stale",
            gate_ids=gate_ids,
        )

        with self.assertRaises(PromotionBlockedError):
            self.promotion.promote(stale)
        with self.assertRaises(PromotionBlockedError):
            self.promotion.promote(stale)

        self.assertEqual(refs_before_stale, self.all_target_refs())
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM promotions WHERE id = ?",
                (stale.promotion_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(PromotionState.BLOCKED.value, row["state"])

    def test_rollback_requires_pl_pm_and_retains_append_only_history(
        self,
    ) -> None:
        prior_oid = self.integrate_candidate()
        _, _, pl, pm = self.record_all_post_merge_gates(prior_oid)
        gate_ids = self.required_gate_ids(prior_oid)
        self.promotion.promote(
            self.promotion_request(
                prior_oid,
                request_suffix="prior",
                gate_ids=gate_ids,
            )
        )
        current_oid = self.create_managed_history_commit(prior_oid)
        self.adapter.transfer_object_and_update_namespaced_ref(
            target=self.target,
            approved_oid=current_oid,
            destination_ref=self.destination_ref,
            expected_source_oid=self.base_oid,
            expected_destination_oid=prior_oid,
            idempotency_key="promotion-adapter:current-history",
        )
        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO promotions (
                    id, goal_id, target_id, approved_oid,
                    expected_source_oid, destination_ref,
                    expected_destination_oid, promoted_oid,
                    required_gate_decision_ids_json, state, idempotency_key,
                    created_at, completed_at
                ) VALUES (
                    'promotion-current-history', 'goal-1', ?, ?, ?, ?,
                    ?, ?, ?, 'PROMOTED', 'promotion-key:current-history',
                    ?, ?
                )
                """,
                (
                    self.target.target_id,
                    current_oid,
                    self.base_oid,
                    self.destination_ref,
                    prior_oid,
                    current_oid,
                    json.dumps(gate_ids),
                    now,
                    now,
                ),
            )

        with self.assertRaises(RollbackAuthorizationError):
            self.promotion.rollback_to_approved(
                target_id=self.target.target_id,
                destination_ref=self.destination_ref,
                current_expected_oid=current_oid,
                prior_approved_oid=prior_oid,
                authorization_ids=(pl.decision_id,),
            )
        self.assertEqual(
            current_oid, self.all_target_refs()[self.destination_ref]
        )

        authorization_ids = (pl.decision_id, pm.decision_id)
        first = self.promotion.rollback_to_approved(
            target_id=self.target.target_id,
            destination_ref=self.destination_ref,
            current_expected_oid=current_oid,
            prior_approved_oid=prior_oid,
            authorization_ids=authorization_ids,
        )
        replay = self.promotion.rollback_to_approved(
            target_id=self.target.target_id,
            destination_ref=self.destination_ref,
            current_expected_oid=current_oid,
            prior_approved_oid=prior_oid,
            authorization_ids=authorization_ids,
        )

        self.assertEqual(first, replay)
        self.assertEqual(PromotionState.ROLLED_BACK, first.state)
        self.assertEqual(prior_oid, first.resulting_oid)
        self.assertEqual(
            prior_oid, self.all_target_refs()[self.destination_ref]
        )
        with self.store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, state, promoted_oid
                FROM promotions
                WHERE goal_id = 'goal-1' AND destination_ref = ?
                ORDER BY created_at, id
                """,
                (self.destination_ref,),
            ).fetchall()
        self.assertEqual(3, len(rows))
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(
            PromotionState.PROMOTED.value,
            by_id["promotion-prior"]["state"],
        )
        self.assertEqual(
            PromotionState.PROMOTED.value,
            by_id["promotion-current-history"]["state"],
        )
        self.assertEqual(PromotionState.ROLLED_BACK.value, first.state.value)


if __name__ == "__main__":
    unittest.main()
