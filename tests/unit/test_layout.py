from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.agent_team_layout import (
    AgentTeamLayout,
    AgentTeamLayoutError,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AgentTeamLayoutTests(unittest.TestCase):
    def test_discovers_canonical_root_layout(self) -> None:
        layout = AgentTeamLayout.discover(PROJECT_ROOT / "scripts")

        self.assertFalse(layout.bundle_mode)
        self.assertEqual(PROJECT_ROOT, layout.source_root)
        self.assertEqual(PROJECT_ROOT / "agents", layout.config_root)
        self.assertEqual(PROJECT_ROOT / "skills", layout.skill_root)
        self.assertEqual(
            PROJECT_ROOT / "agents" / "team.toml",
            layout.resolve_source_path("agents/team.toml"),
        )

    def test_discovers_bundle_layout_and_maps_logical_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-layout-") as temporary:
            root = Path(temporary)
            (root / "config" / "agent-team").mkdir(parents=True)
            (root / ".agents" / "skills").mkdir(parents=True)
            (root / "config" / "agent-team" / "team.toml").write_text(
                "[team]\n", encoding="utf-8"
            )
            (root / ".agents" / "skills" / "catalog.toml").write_text(
                "[catalog]\n", encoding="utf-8"
            )

            layout = AgentTeamLayout.discover(root / "scripts")

            self.assertTrue(layout.bundle_mode)
            self.assertEqual(
                root / "config" / "agent-team" / "roles" / "pl.toml",
                layout.resolve_source_path("agents/roles/pl.toml"),
            )
            self.assertEqual(
                root / ".agents" / "skills" / "goal-loop" / "SKILL.md",
                layout.resolve_source_path("skills/goal-loop/SKILL.md"),
            )

    def test_classifies_runtime_and_frozen_legacy_paths(self) -> None:
        layout = AgentTeamLayout.discover(PROJECT_ROOT)

        self.assertEqual(
            "runtime-state",
            layout.classify(PROJECT_ROOT / ".agent-team" / "state" / "agent-team.db"),
        )
        self.assertEqual(
            "legacy-skills",
            layout.classify(PROJECT_ROOT / ".codex" / "skills" / "legacy" / "SKILL.md"),
        )
        self.assertEqual(
            "runtime-agents",
            layout.classify(PROJECT_ROOT / ".codex" / "agents" / "seat.toml"),
        )
        self.assertEqual(
            "config", layout.classify(PROJECT_ROOT / "agents" / "team.toml")
        )

    def test_rejects_runtime_state_legacy_skills_and_escape(self) -> None:
        layout = AgentTeamLayout.discover(PROJECT_ROOT)

        with self.assertRaises(AgentTeamLayoutError):
            layout.require_within_source(PROJECT_ROOT / ".agent-team" / "state")
        with self.assertRaises(AgentTeamLayoutError):
            layout.require_within_source(PROJECT_ROOT / ".codex" / "skills" / "old")
        with self.assertRaises(AgentTeamLayoutError):
            layout.require_within_source(PROJECT_ROOT.parent / "outside")
        with self.assertRaises(AgentTeamLayoutError):
            layout.resolve_source_path("../agents/team.toml")


if __name__ == "__main__":
    unittest.main()
