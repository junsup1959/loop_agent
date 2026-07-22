from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from types import MappingProxyType

from scripts.agent_team_contracts import (
    ActivationResult,
    AdmissionReceipt,
    ContractViolation,
    ReworkRoute,
    TransitionReceipt,
    admit,
    begin_attempt,
    commit_result,
)
from scripts.project_skills import load_mcp_policy
from scripts.serena_project_knowledge import (
    ProjectKnowledgeError,
    ensure_serena_onboarding,
)
from tests.integration.test_six_seat_contract_flow import (
    OID_BASE,
    SixSeatContractFixture,
    oid,
)
import tests.unit.test_state_v4_constraints as state_v4


class McpEnforcementIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow = SixSeatContractFixture(self)

    def _durable_contract_with_trusted_receipt(self):
        durable = state_v4.AxStateV4ConstraintTests(methodName="runTest")
        durable.setUp()
        self.addCleanup(durable.doCleanups)
        attempt_id, receipt_id, _ = durable._prepare_mcp_attempt()
        contract = self.flow.contract(
            "dev_implementation",
            "dev_submit_revision",
            "developer",
            oid("durable-mcp-revision"),
            serial="durable-mcp",
        )
        document = contract.as_dict()
        document["contract_id"] = durable.ids["contract_id"]
        document["activation_id"] = "activation-v4"
        source_binding = next(
            binding
            for binding in document["mcp_bindings"]
            if binding["server_id"] == "serena"
            and "initial_instructions" in binding["tool_ids"]
        )
        document["mcp_bindings"] = [
            {
                **source_binding,
                "tool_ids": ["initial_instructions"],
                "required_use": True,
                "usage_receipt_required": True,
            }
        ]
        document["serena_onboarding"] = None
        stateful = replace(
            contract,
            document=MappingProxyType(document),
            transition=replace(
                contract.transition,
                from_states=("START",),
                to_state="NEXT",
                failure_state="FAILED",
                serena_consumption_receipt_required=False,
            ),
            workflow_instance=replace(
                contract.workflow_instance,
                instance_id="workflow-instance-v4",
                current_state="START",
            ),
            state_store=durable.store,
            database_bindings={"transition_database_id": "transition-v4"},
        )
        payload = self.flow.result_payload(stateful, "revision_submitted")
        payload["mcp_usage_receipts"] = [{
            "receipt_id": receipt_id,
            "server_id": "serena",
            "tool_id": "initial_instructions",
            "activation_id": stateful.activation_id,
            "evidence_sha256": durable._mcp_receipt_digests[receipt_id],
        }]
        payload["serena_consumption_receipts"] = []
        return durable, stateful, attempt_id, payload

    def test_health_tool_and_usage_receipts_are_all_required(self) -> None:
        revision_oid = oid("mcp-revision")
        unhealthy = self.flow.contract(
            "dev_implementation",
            "dev_submit_revision",
            "developer",
            revision_oid,
            serial="mcp-unhealthy",
            status_by_server={"serena": "UNHEALTHY"},
        )
        missing_tool = self.flow.contract(
            "dev_implementation",
            "dev_submit_revision",
            "developer",
            revision_oid,
            serial="mcp-missing-tool",
            tools_by_server={"serena": (), "sequentialthinking": ()},
        )
        unhealthy_decision = admit(unhealthy)
        tool_decision = admit(missing_tool)
        self.assertIsInstance(unhealthy_decision, ContractViolation)
        self.assertEqual("mcp_health", unhealthy_decision.category)
        self.assertFalse(unhealthy_decision.backend_call_recorded)
        self.assertIsInstance(tool_decision, ContractViolation)
        self.assertEqual("mcp_tool", tool_decision.category)
        self.assertFalse(tool_decision.backend_call_recorded)

        healthy = self.flow.contract(
            "dev_implementation",
            "dev_submit_revision",
            "developer",
            revision_oid,
            serial="mcp-healthy",
        )
        admission = admit(healthy)
        self.assertIsInstance(admission, AdmissionReceipt)
        missing_attempt_id = begin_attempt(
            healthy,
            admission,
            backend="codex-test",
            model="test-model",
            input_digest=state_v4.digest(160),
        )
        missing_receipts = self.flow.result_payload(
            healthy,
            "revision_submitted",
        )
        missing_receipts["mcp_usage_receipts"] = []
        routed = commit_result(
            ActivationResult(healthy, missing_receipts, missing_attempt_id)
        )
        self.assertIsInstance(routed, ReworkRoute)
        self.assertEqual("required-mcp-receipt-missing", routed.reason_code)

        healthy_accepted = self.flow.contract(
            "dev_implementation",
            "dev_submit_revision",
            "developer",
            revision_oid,
            serial="mcp-healthy-accepted",
        )
        accepted_admission = admit(healthy_accepted)
        accepted_attempt_id = begin_attempt(
            healthy_accepted,
            accepted_admission,
            backend="codex-test",
            model="test-model",
            input_digest=state_v4.digest(161),
        )
        accepted = commit_result(
            ActivationResult(
                healthy_accepted,
                self.flow.result_payload(
                    healthy_accepted,
                    "revision_submitted",
                    attempt_id=accepted_attempt_id,
                ),
                accepted_attempt_id,
            )
        )
        self.assertIsInstance(accepted, TransitionReceipt)
        self.assertEqual("ta_review", accepted.to_state)

    def test_injected_initial_instructions_and_sequential_receipts_materialize(self) -> None:
        # The fixture supplies tool observations explicitly; this never depends on a
        # live Serena installation and therefore cannot silently skip enforcement.
        from tests.unit.test_serena_onboarding import SerenaOnboardingTests

        onboarding = SerenaOnboardingTests(methodName="runTest")
        onboarding.setUp()
        self.addCleanup(onboarding.doCleanups)
        missing = copy.deepcopy(onboarding.evidence())
        missing["initial_instructions"]["invoked"] = False
        with self.assertRaisesRegex(ProjectKnowledgeError, "initial_instructions"):
            ensure_serena_onboarding(
                onboarding.repo,
                missing,
                onboarding.required(),
            )

        snapshot = ensure_serena_onboarding(
            onboarding.repo,
            onboarding.evidence(),
            onboarding.required(),
        )
        contract = self.flow.contract(
            "pl_assignment",
            "pl_assign_implementation",
            "pl",
            OID_BASE,
            serial="pl-mcp-materialized",
            snapshot_override=snapshot,
        )
        admission = admit(contract)
        self.assertIsInstance(admission, AdmissionReceipt)
        attempt_id = begin_attempt(
            contract,
            admission,
            backend="codex-test",
            model="test-model",
            input_digest=state_v4.digest(162),
        )
        payload = self.flow.result_payload(
            contract, "work_assigned", attempt_id=attempt_id
        )
        receipt_pairs = {
            (item["server_id"], item["tool_id"])
            for item in payload["mcp_usage_receipts"]
        }
        self.assertIn(("serena", "initial_instructions"), receipt_pairs)
        self.assertIn(("sequentialthinking", "sequentialthinking"), receipt_pairs)
        self.assertIsInstance(
            commit_result(ActivationResult(contract, payload, attempt_id)),
            TransitionReceipt,
        )
        policy = load_mcp_policy()
        self.assertFalse(policy["policy"]["fallback_allowed"])
        self.assertTrue(policy["policy"]["health_preflight_required"])
        self.assertTrue(policy["policy"]["tool_preflight_required"])

    def test_result_can_only_reference_a_preexisting_trusted_receipt(self) -> None:
        _, contract, attempt_id, payload = (
            self._durable_contract_with_trusted_receipt()
        )
        fabricated = copy.deepcopy(payload)
        fabricated["mcp_usage_receipts"][0]["receipt_id"] = "fabricated-mcp-receipt"
        rejected = commit_result(
            ActivationResult(contract, fabricated, attempt_id=attempt_id)
        )
        self.assertIsInstance(rejected, ReworkRoute)
        self.assertEqual("trusted-mcp-receipt-invalid", rejected.reason_code)

        _, changed_contract, changed_attempt_id, changed_payload = (
            self._durable_contract_with_trusted_receipt()
        )
        changed_digest = copy.deepcopy(changed_payload)
        changed_digest["mcp_usage_receipts"][0]["evidence_sha256"] = "f" * 64
        rejected = commit_result(
            ActivationResult(
                changed_contract,
                changed_digest,
                attempt_id=changed_attempt_id,
            )
        )
        self.assertIsInstance(rejected, ReworkRoute)
        self.assertEqual("trusted-mcp-receipt-invalid", rejected.reason_code)

        durable, accepted_contract, accepted_attempt_id, accepted_payload = (
            self._durable_contract_with_trusted_receipt()
        )
        accepted = commit_result(
            ActivationResult(
                accepted_contract,
                accepted_payload,
                attempt_id=accepted_attempt_id,
            )
        )
        self.assertIsInstance(accepted, TransitionReceipt)
        with durable.store.transaction() as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) AS count FROM mcp_usage_receipts"
                ).fetchone()["count"],
            )
            self.assertEqual(
                "COMPLETED",
                connection.execute(
                    """
                    SELECT state FROM activation_contracts
                    WHERE id = 'contract-v4'
                    """
                ).fetchone()["state"],
            )


if __name__ == "__main__":
    unittest.main()
