from __future__ import annotations

import copy
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.project_skills import (
    SkillConfigurationError,
    load_catalog,
    load_mcp_policy,
    resolve_selection,
    validate_catalog,
)


ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = ROOT / "skills"


class SkillCatalogExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog()
        self.policy = load_mcp_policy()

    def _fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path, dict]:
        temporary = tempfile.TemporaryDirectory(prefix="skill-extension-")
        root = Path(temporary.name) / "skills"
        shutil.copytree(SKILLS_ROOT, root)
        package = root / "fixture-audit"
        (package / "agents").mkdir(parents=True)
        skill_text = (
            "---\n"
            "name: fixture-audit\n"
            "description: Inspect a bounded fixture and return evidence.\n"
            "---\n\n"
            "# Fixture Audit\n\n"
            "Inspect only the supplied evidence and return a bounded result.\n"
        )
        (package / "SKILL.md").write_text(skill_text, encoding="utf-8", newline="\n")
        (package / "agents" / "openai.yaml").write_text(
            'interface:\n'
            '  display_name: "Fixture Audit"\n'
            '  short_description: "Inspect one bounded fixture with evidence"\n'
            '  default_prompt: "Use $fixture-audit for the supplied fixture."\n'
            "policy:\n"
            "  allow_implicit_invocation: false\n",
            encoding="utf-8",
            newline="\n",
        )
        data = copy.deepcopy(self.catalog)
        data["skills"].append(
            {
                "id": "fixture-audit",
                "version": "1.0.0",
                "sha256": hashlib.sha256(
                    (package / "SKILL.md").read_bytes()
                ).hexdigest(),
                "path": "fixture-audit/SKILL.md",
                "kind": "workflow",
                "eligible_capabilities": ["advisory"],
                "content_budget_chars": 1000,
                "mcp_prerequisites": ["serena"],
                "summary": "Inspect one bounded fixture.",
            }
        )
        return temporary, root, data

    def test_valid_package_and_entry_require_no_core_python_change(self) -> None:
        temporary, root, data = self._fixture()
        self.addCleanup(temporary.cleanup)
        index = validate_catalog(data, source_root=root, mcp_policy=self.policy)
        packet = resolve_selection(
            data,
            index,
            "advisory",
            ["fixture-audit"],
            transition_id="elastic_execute_bounded_task",
            mcp_policy=self.policy,
        )
        selected = {item["id"]: item for item in packet["skills"]}
        self.assertEqual(
            {"professional-profile-runtime", "fixture-audit"},
            set(selected),
        )
        self.assertRegex(selected["fixture-audit"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(["serena"], selected["fixture-audit"]["mcp_prerequisites"])

    def test_ineligible_capability_and_mcp_intersection_fail(self) -> None:
        temporary, root, data = self._fixture()
        self.addCleanup(temporary.cleanup)
        index = validate_catalog(data, source_root=root, mcp_policy=self.policy)
        with self.assertRaises(SkillConfigurationError):
            resolve_selection(
                data,
                index,
                "developer",
                ["fixture-audit"],
                mcp_policy=self.policy,
            )
        with self.assertRaisesRegex(SkillConfigurationError, "prerequisites exceed"):
            resolve_selection(
                data,
                index,
                "advisory",
                ["fixture-audit"],
                authorized_mcp_servers=set(),
                mcp_policy=self.policy,
            )

    def test_digest_budget_prerequisite_and_authority_expansion_fail(self) -> None:
        mutations = (
            ("sha256", "0" * 64),
            ("content_budget_chars", 1),
            ("mcp_prerequisites", ["unknown-server"]),
            ("approval_authorities", ["quality_gate"]),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                temporary, root, data = self._fixture()
                try:
                    data["skills"][-1][field] = value
                    with self.assertRaises(SkillConfigurationError):
                        validate_catalog(
                            data,
                            source_root=root,
                            mcp_policy=self.policy,
                        )
                finally:
                    temporary.cleanup()

    def test_active_packet_remains_digest_pinned_when_bytes_change(self) -> None:
        temporary, root, data = self._fixture()
        self.addCleanup(temporary.cleanup)
        index = validate_catalog(data, source_root=root, mcp_policy=self.policy)
        packet = resolve_selection(
            data,
            index,
            "advisory",
            ["fixture-audit"],
            mcp_policy=self.policy,
        )
        pinned = next(
            item["sha256"]
            for item in packet["skills"]
            if item["id"] == "fixture-audit"
        )
        skill_path = root / "fixture-audit" / "SKILL.md"
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8") + "\nChanged.\n",
            encoding="utf-8",
            newline="\n",
        )
        self.assertEqual(
            pinned,
            next(
                item["sha256"]
                for item in packet["skills"]
                if item["id"] == "fixture-audit"
            ),
        )
        with self.assertRaisesRegex(SkillConfigurationError, "digest mismatch"):
            validate_catalog(data, source_root=root, mcp_policy=self.policy)


if __name__ == "__main__":
    unittest.main()
