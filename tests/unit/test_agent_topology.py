from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.project_agents as project_agents
from scripts.project_agents import (
    AgentConfigurationError,
    load_and_validate,
    resolve_runtime_activation_contract,
    synchronize,
)


ROOT = Path(__file__).resolve().parents[2]


class SixSlotTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_and_validate()

    def test_exactly_five_fixed_seats_and_one_elastic_slot(self) -> None:
        self.assertEqual(5, len(self.bundle["seats"]))
        self.assertEqual(1, len(self.bundle["elastic_slots"]))
        self.assertEqual(6, len(self.bundle["slots"]))
        self.assertEqual(
            {"pm_ta", "pl", "dev_1", "dev_2", "qa_build"},
            {
                slot["slot_key"]
                for slot in self.bundle["seats"].values()
            },
        )
        self.assertEqual({"elastic"}, {
            slot["slot_key"] for slot in self.bundle["elastic_slots"].values()
        })

    def test_merged_seats_require_one_explicit_capability(self) -> None:
        pm_seat = next(
            seat_id
            for seat_id, seat in self.bundle["seats"].items()
            if seat["slot_key"] == "pm_ta"
        )
        with self.assertRaisesRegex(AgentConfigurationError, "explicit active capability"):
            resolve_runtime_activation_contract(self.bundle, pm_seat)
        pm = resolve_runtime_activation_contract(self.bundle, pm_seat, "pm")
        ta = resolve_runtime_activation_contract(self.bundle, pm_seat, "ta")
        self.assertEqual(["pm", "ta"], pm["mutually_exclusive_capabilities"])
        self.assertEqual(["requirement_gate", "scope_gate"], pm["approval_authorities"])
        self.assertEqual(
            ["architecture_gate", "code_quality_gate"],
            ta["approval_authorities"],
        )
        with self.assertRaises(AgentConfigurationError):
            resolve_runtime_activation_contract(self.bundle, pm_seat, "developer")

    def test_elastic_is_singleton_and_has_no_standing_authority(self) -> None:
        elastic = self.bundle["elastic_slots"]["ELASTIC"]
        self.assertEqual(["worker", "advisory"], elastic["capabilities"])
        self.assertEqual(1, elastic["max_active_workers_per_goal_run"])
        self.assertFalse(elastic["nested_spawn_allowed"])
        self.assertFalse(elastic["standing_approval_authority"])
        for capability_id in elastic["capabilities"]:
            capability = self.bundle["capabilities"][capability_id]
            self.assertEqual([], capability["approval_authorities"])
            self.assertFalse(capability["merge_control"])
            self.assertFalse(capability["nested_spawn_allowed"])

    def test_workflow_covers_delivery_rework_and_elastic_boundaries(self) -> None:
        transitions = {
            item["id"]: item for item in self.bundle["workflow"]["transitions"]
        }
        self.assertEqual(
            {
                "pm_intake_goal",
                "pl_assign_implementation",
                "dev_submit_revision",
                "dev_submit_rework",
                "ta_review_exact_oid",
                "pl_merge_approved_oid",
                "qa_validate_integration",
                "build_validate_integration",
                "pm_accept_integration",
                "pl_issue_rework",
                "elastic_execute_bounded_task",
            },
            set(transitions),
        )
        self.assertEqual("pl_rework", transitions["ta_review_exact_oid"]["failure_state"])
        self.assertEqual("pl_rework", transitions["pl_merge_approved_oid"]["failure_state"])
        self.assertEqual("pl_rework", transitions["qa_validate_integration"]["failure_state"])
        self.assertEqual("pl_rework", transitions["build_validate_integration"]["failure_state"])
        elastic = transitions["elastic_execute_bounded_task"]
        self.assertEqual([], elastic["approval_authorities"])
        self.assertFalse(elastic["merge_control"])
        self.assertFalse(elastic["nested_spawn_allowed"])

    def test_mcp_policy_is_mandatory_and_has_initial_instructions(self) -> None:
        mcp = self.bundle["mcp"]
        self.assertEqual(
            {"serena", "sequentialthinking"},
            set(mcp["policy"]["required_servers"]),
        )
        self.assertFalse(mcp["policy"]["fallback_allowed"])
        self.assertIn(
            "initial_instructions",
            mcp["servers"]["serena"]["tool_allowlist"],
        )
        self.assertIn("serena-coding-start", mcp["required_use_bindings"])
        self.assertIn("sequential-rework", mcp["required_use_bindings"])

    def test_contract_schemas_template_and_budget_are_strict(self) -> None:
        for path in self.bundle["schema_paths"].values():
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                "https://json-schema.org/draft/2020-12/schema",
                schema["$schema"],
            )
            self.assertFalse(schema["additionalProperties"])
            self.assertTrue(schema["required"])
        budget = self.bundle["context_profile_policy"]["contract_budget"]
        self.assertEqual(
            "min(role_packet_limit * 0.25, 12000)",
            budget["formula"],
        )
        self.assertEqual(12000, budget["ceiling_chars"])
        template = self.bundle["template_path"].read_text(encoding="utf-8")
        self.assertIn("{{contract_ref}}", template)
        self.assertIn("{{clauses_markdown}}", template)
        self.assertNotIn("approval_authorities = ", template)

    def test_sync_prunes_only_registry_retired_runtime_agents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-agent-sync-") as temporary:
            temporary_root = Path(temporary)
            runtime_root = temporary_root / ".codex" / "agents"
            runtime_root.mkdir(parents=True)
            retired = {
                seat_id
                for seat in self.bundle["seats"].values()
                for seat_id in seat["absorbed_seat_ids"]
            } | set(self.bundle["legacy_seat_mapping"]["archived_seat_ids"])
            for seat_id in retired:
                (runtime_root / f"{seat_id}.toml").write_text(
                    "retired = true\n",
                    encoding="utf-8",
                )
            unknown = runtime_root / "OPERATOR_김민수.toml"
            unknown.write_text("owner = \"operator\"\n", encoding="utf-8")
            lookalike = runtime_root / "TA_권지호.backup.toml"
            lookalike.write_text("preserve = true\n", encoding="utf-8")
            bundle = {**self.bundle, "runtime_root": runtime_root}

            with patch.object(project_agents, "PROJECT_ROOT", temporary_root):
                pruned = synchronize(bundle)

            self.assertEqual(sorted(retired), pruned)
            self.assertTrue(
                all((runtime_root / f"{seat_id}.toml").is_file() for seat_id in self.bundle["seats"])
            )
            self.assertTrue(
                all(not (runtime_root / f"{seat_id}.toml").exists() for seat_id in retired)
            )
            self.assertEqual("owner = \"operator\"\n", unknown.read_text(encoding="utf-8"))
            self.assertEqual("preserve = true\n", lookalike.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
