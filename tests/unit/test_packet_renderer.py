from __future__ import annotations

import unittest

from scripts.agent_team_contracts import ContractCompilationError, render
from tests.unit.test_workflow_contracts import ContractFixture


class PacketRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ContractFixture(self)

    def test_json_and_markdown_are_byte_stable_and_template_derived(self) -> None:
        contract = self.fixture.contract()
        first = render(
            contract,
            contract.clauses,
            contract.definitions.template_version,
        )
        second = render(
            contract,
            contract.clauses,
            contract.definitions.template_version,
        )
        self.assertEqual(first.contract_json, second.contract_json)
        self.assertEqual(first.markdown, second.markdown)
        self.assertEqual(first.packet_sha256, second.packet_sha256)
        self.assertIn(f"# Activation {contract.contract_id}", first.markdown)
        self.assertNotIn("{{", first.markdown)
        self.assertLessEqual(
            first.combined_character_count,
            contract.effective_budget,
        )

    def test_wrong_template_version_and_clause_order_fail_closed(self) -> None:
        contract = self.fixture.contract()
        with self.assertRaisesRegex(ContractCompilationError, "template version"):
            render(contract, contract.clauses, "delivery-v4@3.0.0")
        with self.assertRaisesRegex(ContractCompilationError, "clauses differ"):
            render(
                contract,
                tuple(reversed(contract.clauses)),
                contract.definitions.template_version,
            )

    def test_renderer_rejects_reduced_budget(self) -> None:
        contract = self.fixture.contract()
        contract.document["budget"]["effective_limit_chars"] = 1
        with self.assertRaisesRegex(ContractCompilationError, "above"):
            render(
                contract,
                contract.clauses,
                contract.definitions.template_version,
            )


if __name__ == "__main__":
    unittest.main()
