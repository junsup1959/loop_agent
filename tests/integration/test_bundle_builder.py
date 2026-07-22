from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.build_agent_team_bundle import (
    BundleBuildError,
    GENERATED_MANIFEST,
    build_bundle,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class BundleBuilderTests(unittest.TestCase):
    def test_materialization_is_deterministic_and_preserves_unknown_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-bundle-") as temporary:
            destination = Path(temporary) / "bundle"

            first = build_bundle(PROJECT_ROOT, destination, check=False)
            first_manifest = (destination / GENERATED_MANIFEST).read_bytes()
            self.assertTrue(first.created)
            self.assertFalse(first.updated)

            clean = build_bundle(PROJECT_ROOT, destination, check=True)
            self.assertFalse(clean.has_drift)
            self.assertEqual(first.manifest_sha256, clean.manifest_sha256)
            self.assertEqual(first_manifest, (destination / GENERATED_MANIFEST).read_bytes())

            unknown = destination / "operator-note.txt"
            unknown.write_text("preserve me\n", encoding="utf-8")
            rebuilt = build_bundle(PROJECT_ROOT, destination, check=False)
            self.assertFalse(rebuilt.has_drift)
            self.assertIn("operator-note.txt", rebuilt.preserved_unknown)
            self.assertEqual("preserve me\n", unknown.read_text(encoding="utf-8"))

    def test_rejects_unsafe_or_unowned_destinations(self) -> None:
        with self.assertRaises(BundleBuildError):
            build_bundle(PROJECT_ROOT, PROJECT_ROOT, check=True)
        with self.assertRaises(BundleBuildError):
            build_bundle(PROJECT_ROOT, PROJECT_ROOT / "scratch-bundle", check=True)

        with tempfile.TemporaryDirectory(prefix="agent-team-unowned-") as temporary:
            destination = Path(temporary)
            (destination / "unrelated.txt").write_text("owner data", encoding="utf-8")
            with self.assertRaises(BundleBuildError):
                build_bundle(PROJECT_ROOT, destination, check=False)

    def test_generated_bundle_runs_canonical_validators(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-validator-") as temporary:
            destination = Path(temporary) / "bundle"
            build_bundle(PROJECT_ROOT, destination, check=False)

            skills = subprocess.run(
                [sys.executable, "-B", "scripts/project_skills.py", "validate"],
                cwd=destination,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            agents = subprocess.run(
                [sys.executable, "-B", "scripts/project_agents.py", "validate"],
                cwd=destination,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

            self.assertEqual(0, skills.returncode, skills.stderr or skills.stdout)
            self.assertEqual(0, agents.returncode, agents.stderr or agents.stdout)
            manifest = json.loads(
                (destination / GENERATED_MANIFEST).read_text(encoding="utf-8")
            )
            generated = set(manifest["generated_paths"])
            self.assertEqual(4, manifest["format_version"])
            self.assertEqual("six-slot-v1", manifest["source_contract"]["topology_id"])
            self.assertEqual(6, manifest["source_contract"]["max_threads"])
            self.assertIn(".agents/skills/goal-loop/SKILL.md", generated)
            self.assertIn("config/agent-team/serena-memory-boundary.md", generated)
            self.assertIn("config/agent-team/workflows/delivery-v4.toml", generated)
            self.assertIn(
                "config/agent-team/contracts/schemas/migration-manifest.schema.json",
                generated,
            )
            self.assertIn("scripts/agent_team_migration.py", generated)
            self.assertIn("sample_config.toml", generated)
            self.assertNotIn(".agent-team/state/agent-team.db", generated)
            self.assertFalse((destination / ".codex" / "skills").exists())
            self.assertFalse(any("__pycache__" in path for path in generated))

    def test_dirty_policy_semantics_are_canonical_before_generation(self) -> None:
        goal = (PROJECT_ROOT / "skills" / "goal-loop" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        serena_boundary = (
            PROJECT_ROOT / "agents" / "serena-memory-boundary.md"
        ).read_text(encoding="utf-8")
        recommended = (PROJECT_ROOT / "agents" / "recommended-tools.md").read_text(
            encoding="utf-8"
        )
        initializer = (PROJECT_ROOT / "scripts" / "init_agent_team.py").read_text(
            encoding="utf-8"
        )

        for anchor in ("create_goal", "update_goal", "/goal", "GOAL_BLOCKED"):
            self.assertIn(anchor, goal)
        self.assertIn("project-specific knowledge", serena_boundary)
        self.assertIn("agent-team operating rules", serena_boundary)
        self.assertIn("initial_instructions", recommended)
        self.assertIn('SERENA_REQUIRED_TOOL = "initial_instructions"', initializer)
        self.assertIn('"--enable-web-dashboard"', initializer)
        self.assertIn('"--open-web-dashboard"', initializer)

        sample = (PROJECT_ROOT / "sample_config.toml").read_text(encoding="utf-8")
        self.assertIn("max_threads = 6", sample)
        self.assertIn('enabled_tools = ["initial_instructions"', sample)
        self.assertEqual(2, sample.count("required = true"))
        self.assertNotIn("required = false", sample)


if __name__ == "__main__":
    unittest.main()
