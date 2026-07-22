from __future__ import annotations

import dataclasses
import tomllib
import unittest
from pathlib import Path

from scripts.agent_team_domain import (
    AuditEvent,
    DomainValidationError,
    GateDecision,
    GateDecisionValue,
    GateType,
    IntegrationPlan,
    PromotionRequest,
    ServiceIdentity,
    TargetRegistration,
    thaw_json,
)


OID_A = "a" * 40
OID_B = "b" * 40
OID_C = "c" * 40
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DomainContractTests(unittest.TestCase):
    def test_target_is_validated_normalized_and_immutable(self) -> None:
        target = TargetRegistration(
            target_id="target-one",
            canonical_worktree_path="C:/targets/one",
            git_common_dir="C:/targets/one/.git",
            source_ref="refs/heads/main",
            observed_source_oid=OID_A.upper(),
            managed_repository_path="C:/ax/targets/target-one/repository.git",
        )

        self.assertEqual(OID_A, target.observed_source_oid)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            target.target_id = "other"  # type: ignore[misc]

        with self.assertRaises(DomainValidationError):
            TargetRegistration(
                target_id="../escape",
                canonical_worktree_path="C:/targets/one",
                git_common_dir="C:/targets/one/.git",
                source_ref="refs/heads/main",
                observed_source_oid=OID_A,
                managed_repository_path="C:/ax/repository.git",
            )

    def test_exact_oid_and_ordered_candidate_contracts_fail_closed(self) -> None:
        plan = IntegrationPlan(
            plan_id="plan-1",
            attempt_id="attempt-1",
            goal_id="goal-1",
            base_oid=OID_A,
            ordered_candidate_oids=(OID_B, OID_C),
            merge_strategy="no-ff",
            pl_decision_id="decision-1",
            idempotency_key="plan-key",
        )
        self.assertEqual((OID_B, OID_C), plan.ordered_candidate_oids)

        with self.assertRaises(DomainValidationError):
            IntegrationPlan(
                plan_id="plan-2",
                attempt_id="attempt-2",
                goal_id="goal-1",
                base_oid="short",
                ordered_candidate_oids=(OID_B,),
                merge_strategy="no-ff",
                pl_decision_id="decision-1",
                idempotency_key="plan-key-2",
            )
        with self.assertRaises(DomainValidationError):
            IntegrationPlan(
                plan_id="plan-3",
                attempt_id="attempt-3",
                goal_id="goal-1",
                base_oid=OID_A,
                ordered_candidate_oids=(OID_B, OID_B),
                merge_strategy="no-ff",
                pl_decision_id="decision-1",
                idempotency_key="plan-key-3",
            )

    def test_gate_evidence_is_bound_to_an_exact_oid(self) -> None:
        decision = GateDecision(
            decision_id="gate-1",
            goal_id="goal-1",
            activation_id="activation-1",
            gate_type=GateType.QA_QUALITY,
            actor_role="qa_sdet",
            subject_oid=OID_C,
            decision=GateDecisionValue.APPROVED,
            profile_digest="profile-digest",
            evidence_ids=("artifact-1",),
            idempotency_key="gate-key",
        )
        self.assertEqual(OID_C, decision.subject_oid)

        with self.assertRaises(DomainValidationError):
            GateDecision(
                decision_id="gate-2",
                goal_id="goal-1",
                activation_id="activation-1",
                gate_type=GateType.QA_QUALITY,
                actor_role="qa_sdet",
                subject_oid=OID_C,
                decision=GateDecisionValue.APPROVED,
                profile_digest="profile-digest",
                evidence_ids=(),
                idempotency_key="gate-key-2",
            )

    def test_nested_audit_payload_is_immutable_and_json_round_trips(self) -> None:
        event = AuditEvent(
            event_id="audit-1",
            event_type="LEASE_CREATED",
            actor="service:workspace-manager",
            subject_type="workspace-lease",
            subject_id="lease-1",
            payload={"scopes": ["src", "tests"], "expected": {"state": "ACTIVE"}},
            occurred_at="2026-07-21T00:00:00+00:00",
        )

        with self.assertRaises(TypeError):
            event.payload["new"] = True  # type: ignore[index]
        with self.assertRaises(TypeError):
            event.payload["expected"]["state"] = "RELEASED"  # type: ignore[index]
        self.assertEqual(
            {
                "scopes": ["src", "tests"],
                "expected": {"state": "ACTIVE"},
            },
            thaw_json(event.payload),
        )

    def test_promotion_is_namespaced_and_controllers_are_not_llm_seats(self) -> None:
        request = PromotionRequest(
            promotion_id="promotion-1",
            goal_id="goal-1",
            target_id="target-1",
            approved_oid=OID_C,
            expected_source_oid=OID_A,
            destination_ref="refs/agentic-ax/approved/goal-1/revision-1",
            expected_destination_oid=None,
            required_gate_decision_ids=("gate-1", "gate-2"),
            idempotency_key="promotion-key",
        )
        self.assertTrue(
            request.destination_ref.startswith("refs/agentic-ax/approved/")
        )
        self.assertTrue(
            all(identity.value.startswith("service:") for identity in ServiceIdentity)
        )

        with self.assertRaises(DomainValidationError):
            PromotionRequest(
                promotion_id="promotion-2",
                goal_id="goal-1",
                target_id="target-1",
                approved_oid=OID_C,
                expected_source_oid=OID_A,
                destination_ref="refs/heads/main",
                expected_destination_oid=None,
                required_gate_decision_ids=("gate-1",),
                idempotency_key="promotion-key-2",
            )

    def test_runtime_policy_keeps_deterministic_controllers_outside_seats(self) -> None:
        with (PROJECT_ROOT / "agents" / "ax-runtime.toml").open("rb") as stream:
            config = tomllib.load(stream)

        self.assertEqual("existing-agent-team", config["runtime"]["architecture"])
        self.assertEqual(
            "worktree-execution-detail", config["runtime"]["scope"]
        )
        self.assertEqual(4, config["sqlite"]["schema_version"])
        self.assertEqual(
            0, config["service_identities"]["llm_seats_consumed"]
        )


if __name__ == "__main__":
    unittest.main()
