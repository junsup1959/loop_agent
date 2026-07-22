from __future__ import annotations

import unittest

from scripts.agent_team_contracts import (
    ActivationResult,
    AdmissionReceipt,
    ContractViolation,
    ReworkRoute,
    admit,
    begin_attempt,
    commit_result,
)
from scripts.agent_team_taskflow import (
    TaskFlowContractError,
    _materialize_pre_mutation_serena_consumption,
)
from scripts.agent_team_workflow import sha256_json
from scripts.serena_project_knowledge import (
    SerenaMemoryReference,
    SerenaOnboardingSnapshot,
)
from tests.unit.test_workflow_contracts import ContractFixture


OID = "b" * 40
CONSUMED_AT = "2026-07-21T00:00:00+00:00"


class SerenaOnboardingHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ContractFixture(self)
        self.memories = (
            SerenaMemoryReference("core", "serena://core", "1" * 64),
            SerenaMemoryReference(
                "conventions", "serena://conventions", "2" * 64
            ),
            SerenaMemoryReference(
                "suggested_commands", "serena://suggested_commands", "3" * 64
            ),
        )
        self.snapshot = SerenaOnboardingSnapshot(
            snapshot_id="snapshot-1",
            repository_id="repository-1",
            source_oid=OID,
            policy_sha256="4" * 64,
            memory_bindings=self.memories,
            evidence_refs=("artifact://serena/onboarding",),
            trigger_ids=(),
            initial_instructions_receipt_sha256="5" * 64,
            refreshed=False,
        )

    def contract(self, *, tools=("initial_instructions",)):
        return self.fixture.flow.contract(
            "dev_implementation",
            "dev_submit_revision",
            "developer",
            OID,
            serial=f"serena-handoff-{self.fixture.flow._scope_counter + 1}",
            snapshot_override=self.snapshot,
            tools_by_server={"serena": tuple(tools)},
        )

    def refs(self) -> list[dict[str, object]]:
        result = []
        for memory in self.memories:
            receipt = {
                "snapshot_id": self.snapshot.snapshot_id,
                "memory_name": memory.name,
                "memory_sha256": memory.sha256,
                "consumed_at": CONSUMED_AT,
            }
            result.append(
                {
                    "validated": True,
                    "name": memory.name,
                    "ref": memory.memory_ref,
                    "sha256": memory.sha256,
                    "consumed_before_mutation": True,
                    "consumed_at": CONSUMED_AT,
                    "receipt_sha256": sha256_json(receipt),
                }
            )
        return result

    @staticmethod
    def conf(refs: list[dict[str, object]]) -> dict[str, object]:
        return {"subject_oid": OID, "validated_memory_refs": refs}

    def result_payload(self, contract, attempt_id: str) -> dict[str, object]:
        return self.fixture.flow.result_payload(
            contract,
            "blocked",
            attempt_id=attempt_id,
        )

    def admitted_attempt(self, contract) -> str:
        admission = admit(contract)
        self.assertIsInstance(admission, AdmissionReceipt)
        return begin_attempt(
            contract,
            admission,
            backend="codex-test",
            model="test-model",
            input_digest=sha256_json({"contract_id": contract.contract_id}),
        )

    def test_initial_instructions_availability_and_usage_are_required(self) -> None:
        routed = admit(self.contract(tools=()))
        self.assertIsInstance(routed, ContractViolation)
        self.assertEqual("mcp_tool", routed.category)

        contract = self.contract()
        attempt_id = self.admitted_attempt(contract)
        payload = self.result_payload(contract, attempt_id)
        payload["mcp_usage_receipts"] = []
        routed = commit_result(ActivationResult(contract, payload, attempt_id))
        self.assertIsInstance(routed, ReworkRoute)
        self.assertEqual("required-mcp-receipt-missing", routed.reason_code)

    def test_minimum_refs_are_injected_and_consumed_before_mutation(self) -> None:
        contract = self.contract()
        self.assertIsInstance(admit(contract), AdmissionReceipt)
        receipts = _materialize_pre_mutation_serena_consumption(
            self.conf(self.refs()),
            {"accepted": True, "contract": contract},
        )
        self.assertEqual(
            [memory.name for memory in self.memories],
            [receipt["memory_name"] for receipt in receipts],
        )
        with self.assertRaisesRegex(TaskFlowContractError, "minimum"):
            _materialize_pre_mutation_serena_consumption(
                self.conf(self.refs()[:-1]),
                {"accepted": True, "contract": contract},
            )

    def test_pre_mutation_and_result_receipts_materialize_to_contract_bindings(self) -> None:
        contract = self.contract()
        attempt_id = self.admitted_attempt(contract)
        _materialize_pre_mutation_serena_consumption(
            self.conf(self.refs()),
            {"accepted": True, "contract": contract},
        )
        routed = commit_result(
            ActivationResult(
                contract,
                self.result_payload(contract, attempt_id),
                attempt_id=attempt_id,
            )
        )
        self.assertIsInstance(routed, ReworkRoute)
        with contract.state_store.transaction() as connection:
            mcp_count = connection.execute(
                "SELECT COUNT(*) AS count FROM mcp_usage_receipts"
            ).fetchone()["count"]
            serena_count = connection.execute(
                "SELECT COUNT(*) AS count FROM serena_consumption_receipts"
            ).fetchone()["count"]
            result_count = connection.execute(
                "SELECT COUNT(*) AS count FROM activation_results"
            ).fetchone()["count"]
        self.assertGreaterEqual(mcp_count, 1)
        self.assertEqual(3, serena_count)
        self.assertEqual(1, result_count)

    def test_changed_duplicate_or_wildcard_receipts_fail_closed(self) -> None:
        for mutation in ("changed", "duplicate", "wildcard", "changed_ref"):
            with self.subTest(mutation=mutation):
                contract = self.contract()
                if mutation == "changed":
                    attempt_id = self.admitted_attempt(contract)
                    payload = self.result_payload(contract, attempt_id)
                    receipts = payload["serena_consumption_receipts"]
                    receipts[0]["memory_sha256"] = "f" * 64
                elif mutation == "duplicate":
                    attempt_id = self.admitted_attempt(contract)
                    payload = self.result_payload(contract, attempt_id)
                    receipts = payload["serena_consumption_receipts"]
                    receipts[1] = dict(receipts[0])
                elif mutation in {"wildcard", "changed_ref"}:
                    refs = self.refs()
                    refs[0]["ref"] = (
                        "*" if mutation == "wildcard" else "serena://other-core"
                    )
                    with self.assertRaisesRegex(TaskFlowContractError, "contract binding"):
                        _materialize_pre_mutation_serena_consumption(
                            self.conf(refs),
                            {"accepted": True, "contract": contract},
                        )
                    continue
                routed = commit_result(
                    ActivationResult(contract, payload, attempt_id)
                )
                self.assertIsInstance(routed, ReworkRoute)
                self.assertIn("serena-receipt", routed.reason_code)


if __name__ == "__main__":
    unittest.main()
