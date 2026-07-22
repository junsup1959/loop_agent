from __future__ import annotations

import copy
import unittest
from unittest import mock

from scripts import agent_team_context
from scripts.agent_team_context import materialize_skill_instructions
from scripts.project_skills import (
    SkillConfigurationError,
    resolve_selection,
    validate_catalog,
)


class SkillExtensionIntegrationTests(unittest.TestCase):
    def _fixture(self):
        from tests.unit.test_skill_catalog_extensions import (
            SkillCatalogExtensionTests,
        )

        fixture = SkillCatalogExtensionTests(methodName="runTest")
        fixture.setUp()
        temporary, root, data = fixture._fixture()
        self.addCleanup(temporary.cleanup)
        data["catalog"]["revision"] = "fixture-extension-revision-1"
        return fixture, root, data

    def test_catalog_revision_resolves_injects_and_pins_eligible_skill(self) -> None:
        fixture, root, data = self._fixture()
        index = validate_catalog(data, source_root=root, mcp_policy=fixture.policy)
        resolved = resolve_selection(
            data,
            index,
            "advisory",
            ["fixture-audit"],
            transition_id="elastic_execute_bounded_task",
            mcp_policy=fixture.policy,
        )
        self.assertEqual("fixture-extension-revision-1", resolved["catalog_revision"])
        self.assertTrue(resolved["explicit_injection"])

        projected = []
        for descriptor in resolved["skills"]:
            content = (root / descriptor["path"]).read_text(encoding="utf-8")
            projected.append(
                {
                    **descriptor,
                    "content_chars": len(content),
                }
            )
        injection_packet = {
            "explicit_injection": True,
            "max_content_chars": sum(item["content_chars"] for item in projected),
            "skills": projected,
        }
        with mock.patch.object(
            agent_team_context,
            "_project_relative_path",
            side_effect=lambda value: (root.parent / value).resolve(),
        ):
            injected = materialize_skill_instructions(injection_packet)
        fixture_item = next(item for item in injected if item["id"] == "fixture-audit")
        pinned_digest = fixture_item["sha256"]
        self.assertIn("Inspect only the supplied evidence", fixture_item["content"])

        skill_path = root / "fixture-audit" / "SKILL.md"
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8") + "\nChanged after activation.\n",
            encoding="utf-8",
            newline="\n",
        )
        self.assertEqual(
            pinned_digest,
            next(
                item["sha256"]
                for item in resolved["skills"]
                if item["id"] == "fixture-audit"
            ),
        )
        with self.assertRaisesRegex(SkillConfigurationError, "digest mismatch"):
            validate_catalog(data, source_root=root, mcp_policy=fixture.policy)

    def test_extension_cannot_expand_budget_mcp_or_authority(self) -> None:
        fixture, root, data = self._fixture()
        index = validate_catalog(data, source_root=root, mcp_policy=fixture.policy)
        with self.assertRaises(SkillConfigurationError):
            resolve_selection(
                data,
                index,
                "developer",
                ["fixture-audit"],
                mcp_policy=fixture.policy,
            )
        with self.assertRaisesRegex(SkillConfigurationError, "prerequisites exceed"):
            resolve_selection(
                data,
                index,
                "advisory",
                ["fixture-audit"],
                authorized_mcp_servers=set(),
                mcp_policy=fixture.policy,
            )

        mutations = (
            ("content_budget_chars", 1, "budget"),
            ("mcp_prerequisites", ["undeclared-server"], "MCP"),
            ("sha256", "0" * 64, "digest mismatch"),
            ("approval_authorities", ["quality_gate"], "authority"),
        )
        for field, value, message in mutations:
            with self.subTest(field=field):
                changed = copy.deepcopy(data)
                changed["skills"][-1][field] = value
                with self.assertRaisesRegex(SkillConfigurationError, message):
                    validate_catalog(
                        changed,
                        source_root=root,
                        mcp_policy=fixture.policy,
                    )


if __name__ == "__main__":
    unittest.main()
