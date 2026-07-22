from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.agent_team_contracts import (
    ActivationResult,
    AdmissionReceipt,
    ContractCompilationError,
    ContractViolation,
    Quarantine,
    ReworkRoute,
    admit,
    begin_attempt,
    commit_result,
    compile_transition,
)
from scripts.agent_team_workflow import (
    ActorBinding,
    EvidenceSet,
    RepositoryBinding,
    WorkflowDefinitions,
    WorkflowInstance,
)
from scripts.agent_team_state import AxStateStore
from tests.integration.test_six_seat_contract_flow import SixSeatContractFixture


OID_A = "a" * 40
OID_B = "b" * 40
DIGESTS = tuple(f"{index:064x}" for index in range(1, 7))
ISSUED = "2026-07-21T00:00:00+00:00"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ContractFixture:
    def __init__(self, owner: unittest.TestCase) -> None:
        self.owner = owner
        self.temporary = tempfile.TemporaryDirectory(prefix="contract-core-")
        owner.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "profile.json"
        self.profile.write_text('{"professional_skill_id":"professional-profile-runtime"}', encoding="utf-8")
        self.definitions = WorkflowDefinitions.load()
        self.professional_skill = Path("skills/professional-profile-runtime/SKILL.md")
        self.flow = SixSeatContractFixture(owner)

    def instance(self, state: str = "pm_intake") -> WorkflowInstance:
        return WorkflowInstance(
            instance_id="workflow-instance-1",
            goal_id="goal-1",
            run_id="run-1",
            target_id="target-1",
            current_state=state,
        )

    def actor(self, capability: str = "pm", slot: str = "pm_ta") -> ActorBinding:
        return ActorBinding(
            activation_id="activation-1",
            capability_id=capability,
            slot_key=slot,
            slot_type="fixed",
            seat_id="PM_fixture" if slot == "pm_ta" else "DEV_fixture",
            physical_seat_id="seat-1",
            seat_capability_activation_id="seat-capability-1",
            worker_id="worker-1",
            worker_fingerprint=DIGESTS[0],
            worker_fingerprint_id="fingerprint-1",
            slot_id="slot-1",
            worker_assignment_id="assignment-1",
            compiled_profile_ref=str(self.profile),
            compiled_profile_sha256=_sha(self.profile),
            profile_reference_sha256s=DIGESTS[1:5],
            selected_skills=(
                {
                    "id": "professional-profile-runtime",
                    "version": "1.0.0",
                    "kind": "professional",
                    "path": "professional-profile-runtime/SKILL.md",
                    "sha256": _sha(self.professional_skill),
                    "content_budget_chars": 4000,
                    "mcp_prerequisites": [],
                    "eligible_capabilities": [
                        "pm", "ta", "pl", "developer", "qa_sdet",
                        "build_release", "worker", "advisory",
                    ],
                },
            ),
        )

    def evidence(self, *, healthy: bool = True, tools: tuple[str, ...] = ()) -> EvidenceSet:
        return EvidenceSet(
            repository=RepositoryBinding("repository-1", OID_A),
            lease_id="lease-1",
            sandbox_binding_id="sandbox-1",
            oid_authority_id="oid-authority-1",
            base_oid=OID_A,
            subject_oid=OID_B,
            head_oid=OID_B,
            workspace={
                "workspace_id": "workspace-1",
                "lease_id": "lease-1",
                "sandbox_id": None,
                "cwd": "C:/ax/worktree",
                "source_roots": ["C:/ax/worktree"],
                "writable_roots": ["C:/ax/worktree/src"],
                "protected_roots": ["C:/target"],
                "prohibited_roots": ["C:/target", "C:/ax/sibling"],
            },
            mcp_health={
                "serena": {
                    "status": "HEALTHY" if healthy else "UNHEALTHY",
                    "tools": list(tools),
                    "evidence_digest": DIGESTS[4],
                },
                "sequentialthinking": {
                    "status": "HEALTHY" if healthy else "UNHEALTHY",
                    "tools": list(tools),
                    "evidence_digest": DIGESTS[5],
                },
            },
            issued_at=ISSUED,
        )

    def contract(self, *, healthy: bool = True):
        return self.flow.contract(
            "pm_intake",
            "pm_intake_goal",
            "pm",
            OID_B,
            serial="unit-contract",
            status_by_server=(None if healthy else {"serena": "UNHEALTHY"}),
        )


class WorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ContractFixture(self)

    def test_compile_is_byte_stable_and_binds_one_capability(self) -> None:
        first = self.fixture.contract()
        second = self.fixture.contract()
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(
            first.rendered_packet.packet_sha256,
            second.rendered_packet.packet_sha256,
        )
        self.assertEqual("pm", first.document["identity"]["capability_id"])
        self.assertEqual(1, sum(
            skill["id"] == "professional-profile-runtime"
            for skill in first.document["skills"]
        ))

    def test_illegal_state_and_cross_capability_fail_closed(self) -> None:
        transition = self.fixture.definitions.transition("pm_intake_goal")
        with self.assertRaisesRegex(ContractCompilationError, "illegal workflow"):
            compile_transition(
                self.fixture.instance("ta_review"),
                transition,
                self.fixture.actor(),
                self.fixture.evidence(),
            )

    def test_compile_requires_one_identical_durable_store_on_both_bindings(self) -> None:
        transition = self.fixture.definitions.transition("pm_intake_goal")
        with self.assertRaisesRegex(ContractCompilationError, "durable AxStateStore"):
            compile_transition(
                self.fixture.instance(),
                transition,
                self.fixture.actor(),
                self.fixture.evidence(),
            )
        first = AxStateStore(self.fixture.root / "first.db")
        second = AxStateStore(self.fixture.root / "second.db")
        first.initialize()
        second.initialize()
        with self.assertRaisesRegex(ContractCompilationError, "same AxStateStore"):
            compile_transition(
                replace(self.fixture.instance(), state_store=first),
                transition,
                self.fixture.actor(),
                replace(
                    self.fixture.evidence(),
                    repository=replace(
                        self.fixture.evidence().repository,
                        state_store=second,
                    ),
                ),
            )
        with self.assertRaisesRegex(ContractCompilationError, "illegal workflow"):
            compile_transition(
                self.fixture.instance(),
                transition,
                self.fixture.actor("ta"),
                self.fixture.evidence(),
            )

    def test_admission_rejects_unhealthy_mcp_before_model_permission(self) -> None:
        rejected = admit(self.fixture.contract(healthy=False))
        self.assertIsInstance(rejected, ContractViolation)
        self.assertFalse(rejected.backend_call_recorded)
        self.assertEqual("mcp_health", rejected.category)

    def test_admission_accepts_complete_contract(self) -> None:
        accepted = admit(self.fixture.contract())
        self.assertIsInstance(accepted, AdmissionReceipt)
        self.assertTrue(accepted.model_call_permitted)
        self.assertTrue(all(accepted.checks.values()))

    def test_result_identity_and_oid_violations_quarantine(self) -> None:
        contract = self.fixture.contract()
        admission = admit(contract)
        attempt_id = begin_attempt(
            contract,
            admission,
            backend="codex-test",
            model="test-model",
            input_digest="a" * 64,
        )
        result = _result_payload(contract)
        result["subject_oid"] = OID_A
        routed = commit_result(ActivationResult(contract, result, attempt_id))
        self.assertIsInstance(routed, Quarantine)
        self.assertEqual("oid", routed.category)

    def test_failure_result_routes_to_pl_without_direct_repair(self) -> None:
        contract = self.fixture.contract()
        admission = admit(contract)
        attempt_id = begin_attempt(
            contract,
            admission,
            backend="codex-test",
            model="test-model",
            input_digest="b" * 64,
        )
        result = _result_payload(contract, result_kind="blocked")
        routed = commit_result(ActivationResult(contract, result, attempt_id))
        self.assertIsInstance(routed, ReworkRoute)
        self.assertEqual("pl", routed.owner_capability)
        self.assertFalse(routed.direct_source_repair_allowed)


def _result_payload(contract, *, result_kind: str = "goal_defined") -> dict[str, object]:
    return {
        "schema_version": 4,
        "result_id": "result-1",
        "contract_id": contract.contract_id,
        "activation_id": contract.activation_id,
        "transition_id": contract.transition.transition_id,
        "capability_id": contract.actor.capability_id,
        "result_kind": result_kind,
        "subject_oid": contract.evidence.subject_oid,
        "result_oid": contract.evidence.subject_oid,
        "evidence_refs": ["artifact-1"],
        "mcp_usage_receipts": [],
        "serena_consumption_receipts": [],
        "format_repair_used": False,
        "token_accounting": {
            "input_tokens": 1,
            "output_tokens": 1,
            "repair_tokens": 0,
        },
        "payload": {},
        "idempotency_key": contract.document["idempotency_key"],
        "completed_at": "2026-07-21T00:00:01+00:00",
    }


if __name__ == "__main__":
    unittest.main()
