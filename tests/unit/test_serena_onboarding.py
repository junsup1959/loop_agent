from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.agent_team_state import AxStateStore
from scripts.serena_project_knowledge import (
    ProjectKnowledgeError,
    ensure_serena_onboarding,
    load_policy,
    required_memories_for_transition,
)


OID = "a" * 40
NOW = "2026-07-21T00:00:00.000000+00:00"
TRANSITION = "pl_assign_implementation"


class SerenaOnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="serena-onboarding-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = AxStateStore(self.root / "ax.db")
        self.store.initialize()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO targets (
                    id, canonical_checkout_path, git_common_dir, source_ref,
                    observed_source_oid, state, created_at, updated_at
                ) VALUES (
                    'target-1', ?, ?, 'refs/heads/main', ?, 'ACTIVE', ?, ?
                )
                """,
                (str(self.root), str(self.root / ".git"), OID, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO repository_registrations (
                    id, target_id, managed_repository_id, canonical_path,
                    git_common_dir, source_oid, state, idempotency_key,
                    registered_at
                ) VALUES (
                    'repository-1', 'target-1', NULL, ?, ?, ?, 'ACTIVE',
                    'repository-1-key', ?
                )
                """,
                (str(self.root), str(self.root / ".git"), OID, NOW),
            )
        self.policy = load_policy()
        self.repo = {
            "repository_id": "repository-1",
            "source_oid": OID,
            "state_store": self.store,
        }

    @staticmethod
    def _content() -> dict[str, tuple[str, str]]:
        return {
            "core": ("Project sources are under src and tests.", "project_core"),
            "tech_stack": ("Python is the primary implementation language.", "tech_stack"),
            "suggested_commands": ("Run python -m unittest for verification.", "build_test_commands"),
            "conventions": ("Use snake_case names and small modules.", "project_conventions"),
            "task_completion": ("Run formatting and tests before release.", "build_test_commands"),
        }

    def evidence(self) -> dict[str, object]:
        bindings = []
        for name, (content, content_class) in self._content().items():
            bindings.append(
                {
                    "name": name,
                    "ref": f"serena://{name}",
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "content": content,
                    "content_class": content_class,
                }
            )
        return {
            "publisher_capability": "pl",
            "transition_id": TRANSITION,
            "source_oid": OID,
            "policy_sha256": self.policy.policy_sha256,
            "refresh_completed": True,
            "initial_instructions": {
                "tool_name": "initial_instructions",
                "available": True,
                "invoked": True,
                "evidence_sha256": "b" * 64,
            },
            "memory_bindings": bindings,
            "evidence_refs": ["artifact://serena/onboarding-1"],
        }

    def required(self) -> tuple[str, ...]:
        return required_memories_for_transition(TRANSITION)

    def test_new_snapshot_persists_selected_named_bindings(self) -> None:
        snapshot = ensure_serena_onboarding(
            self.repo, self.evidence(), self.required()
        )
        self.assertTrue(snapshot.refreshed)
        self.assertEqual(self.required(), tuple(item.name for item in snapshot.memory_bindings))
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT source_oid, policy_digest FROM serena_onboarding_snapshots WHERE id = ?",
                (snapshot.snapshot_id,),
            ).fetchone()
            bindings = connection.execute(
                "SELECT memory_name, memory_ref, memory_sha256 "
                "FROM serena_snapshot_memory_bindings "
                "WHERE snapshot_id = ? ORDER BY ordinal",
                (snapshot.snapshot_id,),
            ).fetchall()
        self.assertEqual((OID, self.policy.policy_sha256), tuple(row))
        self.assertEqual(
            set(self.required()),
            {item["memory_name"] for item in bindings},
        )

    def test_fresh_unchanged_snapshot_is_reused(self) -> None:
        first = ensure_serena_onboarding(self.repo, self.evidence(), self.required())
        evidence = self.evidence()
        evidence["previous_snapshot_id"] = first.snapshot_id
        second = ensure_serena_onboarding(self.repo, evidence, self.required())
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertFalse(second.refreshed)
        with self.store.transaction() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM serena_onboarding_snapshots"
            ).fetchone()["count"]
        self.assertEqual(1, count)

    def test_stale_snapshot_requires_refresh_and_creates_new_bytes(self) -> None:
        first = ensure_serena_onboarding(self.repo, self.evidence(), self.required())
        evidence = self.evidence()
        evidence["previous_snapshot_id"] = first.snapshot_id
        evidence["stale"] = True
        for binding in evidence["memory_bindings"]:
            if binding["name"] == "conventions":
                binding["content"] = "Use snake_case names and explicit type hints."
                binding["sha256"] = hashlib.sha256(
                    binding["content"].encode("utf-8")
                ).hexdigest()
        second = ensure_serena_onboarding(self.repo, evidence, self.required())
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertIn("stale-project-knowledge", second.trigger_ids)

    def test_material_configuration_change_requires_refresh(self) -> None:
        first = ensure_serena_onboarding(self.repo, self.evidence(), self.required())
        evidence = self.evidence()
        evidence["previous_snapshot_id"] = first.snapshot_id
        evidence["material_config_change"] = True
        refreshed = ensure_serena_onboarding(self.repo, evidence, self.required())
        self.assertTrue(refreshed.refreshed)
        self.assertIn("material-project-change", refreshed.trigger_ids)

    def test_missing_required_memory_is_rejected(self) -> None:
        evidence = self.evidence()
        evidence["memory_bindings"] = [
            item for item in evidence["memory_bindings"] if item["name"] != "conventions"
        ]
        with self.assertRaisesRegex(ProjectKnowledgeError, "missing"):
            ensure_serena_onboarding(self.repo, evidence, self.required())

    def test_live_team_state_content_is_rejected(self) -> None:
        evidence = self.evidence()
        binding = next(
            item for item in evidence["memory_bindings"] if item["name"] == "conventions"
        )
        binding["content"] = "The active work item is assigned to the developer seat."
        binding["sha256"] = hashlib.sha256(binding["content"].encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(ProjectKnowledgeError, "prohibited"):
            ensure_serena_onboarding(self.repo, evidence, self.required())

    def test_only_pl_may_publish(self) -> None:
        evidence = self.evidence()
        evidence["publisher_capability"] = "developer"
        with self.assertRaisesRegex(ProjectKnowledgeError, "only the PL"):
            ensure_serena_onboarding(self.repo, evidence, self.required())

    def test_initial_instructions_availability_and_use_receipt_are_required(self) -> None:
        for field, value in (
            ("available", False),
            ("invoked", False),
            ("evidence_sha256", "invalid"),
        ):
            with self.subTest(field=field):
                evidence = self.evidence()
                evidence["initial_instructions"][field] = value
                with self.assertRaisesRegex(
                    ProjectKnowledgeError,
                    "initial_instructions",
                ):
                    ensure_serena_onboarding(
                        self.repo, evidence, self.required()
                    )

    def test_transition_minimum_and_ref_integrity_fail_closed(self) -> None:
        with self.assertRaisesRegex(ProjectKnowledgeError, "transition-specific"):
            ensure_serena_onboarding(self.repo, self.evidence(), ("core",))
        for mutation in ("docs", "digest", "duplicate"):
            with self.subTest(mutation=mutation):
                evidence = copy.deepcopy(self.evidence())
                selected = [
                    item
                    for item in evidence["memory_bindings"]
                    if item["name"] in self.required()
                ]
                if mutation == "docs":
                    selected[0]["ref"] = "serena://docs/core"
                elif mutation == "digest":
                    selected[0]["sha256"] = "not-a-digest"
                else:
                    selected[1]["ref"] = selected[0]["ref"]
                with self.assertRaisesRegex(ProjectKnowledgeError, "invalid"):
                    ensure_serena_onboarding(self.repo, evidence, self.required())


if __name__ == "__main__":
    unittest.main()
