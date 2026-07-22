from __future__ import annotations

import unittest
from dataclasses import replace
from types import MappingProxyType

from scripts.agent_team_contracts import (
    ActivationResult,
    AdmissionReceipt,
    ContractViolation,
    Quarantine,
    ReworkRoute,
    admit,
    begin_attempt,
    commit_result,
)
from scripts.agent_team_workflow import RepositoryBinding
from scripts.agent_team_domain import ContractAttemptKind
from tests.integration.test_six_seat_contract_flow import (
    OID_BASE,
    SixSeatContractFixture,
    oid,
)
import tests.unit.test_state_v4_constraints as state_v4


class _CountingBackend:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, contract) -> None:
        del contract
        self.calls += 1


class ContractAdmissionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow = SixSeatContractFixture(self)
        self.backend = _CountingBackend()

    def _gate(self, factory):
        try:
            contract = factory()
        except (ValueError, RuntimeError):
            return None
        decision = admit(contract)
        if isinstance(decision, AdmissionReceipt):
            self.backend.execute(contract)
        return decision

    def test_invalid_contract_dimensions_make_zero_backend_calls(self) -> None:
        valid_digest = self.flow.contract(
            "pm_intake",
            "pm_intake_goal",
            "pm",
            OID_BASE,
            serial="admission-digest",
        )
        invalid_digest = replace(
            valid_digest,
            rendered_packet=replace(
                valid_digest.rendered_packet,
                contract_sha256="0" * 64,
            ),
        )
        valid_budget = self.flow.contract(
            "pm_intake",
            "pm_intake_goal",
            "pm",
            OID_BASE,
            serial="admission-budget",
        )
        over_budget = replace(
            valid_budget,
            rendered_packet=replace(
                valid_budget.rendered_packet,
                combined_character_count=valid_budget.effective_budget + 1,
            ),
        )
        unhealthy = self.flow.contract(
            "pm_intake",
            "pm_intake_goal",
            "pm",
            OID_BASE,
            serial="admission-mcp",
            status_by_server={"serena": "UNHEALTHY"},
        )
        no_onboarding = self.flow.contract(
            "pl_assignment",
            "pl_assign_implementation",
            "pl",
            OID_BASE,
            serial="admission-onboarding",
            include_snapshot=False,
        )
        root_evidence = self.flow.evidence("pm_intake_goal", OID_BASE)
        root_evidence = replace(
            root_evidence,
            workspace={
                **root_evidence.workspace,
                "binding_sha256": "0" * 64,
            },
        )

        cases = {
            "authority": lambda: self.flow.contract(
                "pm_intake",
                "pm_intake_goal",
                "pm",
                OID_BASE,
                actor_override=self.flow.actor("ta", "invalid-authority"),
            ),
            "oid": lambda: RepositoryBinding("repository-invalid", "b" * 12),
            "root": lambda: self.flow.contract(
                "pm_intake",
                "pm_intake_goal",
                "pm",
                OID_BASE,
                actor_override=self.flow.actor("pm", "invalid-root"),
                evidence_override=root_evidence,
            ),
            "digest": lambda: invalid_digest,
            "budget": lambda: over_budget,
            "mcp": lambda: unhealthy,
            "onboarding": lambda: no_onboarding,
        }
        decisions = {}
        for name, factory in cases.items():
            with self.subTest(case=name):
                decisions[name] = self._gate(factory)
                self.assertEqual(0, self.backend.calls)

        self.assertIsInstance(decisions["digest"], ContractViolation)
        self.assertIsInstance(decisions["budget"], ContractViolation)
        self.assertIsInstance(decisions["mcp"], ContractViolation)
        self.assertIsInstance(decisions["onboarding"], ContractViolation)
        self.assertEqual("budget", decisions["budget"].category)
        self.assertEqual("mcp_health", decisions["mcp"].category)
        self.assertEqual("serena_onboarding", decisions["onboarding"].category)

    def test_authority_quarantine_and_single_format_repair_circuit(self) -> None:
        contract = self.flow.contract(
            "pm_intake",
            "pm_intake_goal",
            "pm",
            OID_BASE,
            serial="result-authority",
        )
        payload = self.flow.result_payload(contract, "goal_defined")
        admission = admit(contract)
        self.assertIsInstance(admission, AdmissionReceipt)
        attempt_id = begin_attempt(
            contract,
            admission,
            backend="codex-test",
            model="test-model",
            input_digest=state_v4.digest(139),
        )
        payload["capability_id"] = "ta"
        quarantined = commit_result(
            ActivationResult(contract, payload, attempt_id=attempt_id)
        )
        self.assertIsInstance(quarantined, Quarantine)
        self.assertEqual("authority", quarantined.category)

        durable = state_v4.AxStateV4ConstraintTests(methodName="runTest")
        durable.setUp()
        self.addCleanup(durable.doCleanups)
        durable._prepare_accepted_contract()
        stateful_document = contract.as_dict()
        stateful_document["contract_id"] = durable.ids["contract_id"]
        stateful_document["activation_id"] = "activation-v4"
        stateful = replace(
            contract,
            document=MappingProxyType(stateful_document),
            state_store=durable.store,
            database_bindings={"transition_database_id": "transition-v4"},
        )
        primary_id = durable.store.record_contract_attempt(
            contract_id=durable.ids["contract_id"],
            backend="codex",
            model="gpt-5",
            input_digest=state_v4.digest(140),
        )
        first = commit_result(
            ActivationResult(
                stateful,
                {"not": "activation-result-v4"},
                attempt_id=primary_id,
                format_error_only=True,
            )
        )
        repair_id = durable.store.record_contract_attempt(
            contract_id=durable.ids["contract_id"],
            backend="codex",
            model="gpt-5",
            input_digest=state_v4.digest(141),
            attempt_kind=ContractAttemptKind.FORMAT_REPAIR,
        )
        second = commit_result(
            ActivationResult(
                stateful,
                {"still": "invalid"},
                attempt_id=repair_id,
                format_error_only=True,
            )
        )
        self.assertIsInstance(first, ReworkRoute)
        self.assertEqual("FORMAT_REPAIR", first.attempt_kind)
        self.assertIsInstance(second, Quarantine)
        self.assertEqual("format", second.category)
        self.assertEqual("format-circuit-open", second.reason_code)
        with durable.store.transaction() as connection:
            results = connection.execute(
                """
                SELECT disposition FROM activation_results
                WHERE contract_id = ? ORDER BY recorded_at
                """,
                (durable.ids["contract_id"],),
            ).fetchall()
            violations = connection.execute(
                """
                SELECT disposition FROM contract_violations
                WHERE contract_id = ? ORDER BY occurred_at
                """,
                (durable.ids["contract_id"],),
            ).fetchall()
        self.assertEqual(
            ["FORMAT_INVALID", "FORMAT_INVALID"],
            [row["disposition"] for row in results],
        )
        self.assertEqual(
            {"FORMAT_REPAIR", "CIRCUIT_OPEN"},
            {row["disposition"] for row in violations},
        )


if __name__ == "__main__":
    unittest.main()
