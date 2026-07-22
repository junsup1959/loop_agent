from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.agent_team_context import (
    ContextCompiler,
    ContextSelectionError,
    RepositoryRegistry,
)
from scripts.agent_team_profiles import (
    PROFESSIONAL_SKILL_ID,
    ProfileResolutionRequest,
    ProfessionalProfileCatalog,
    ProfessionalProfileCompiler,
    ProfessionalProfileResolver,
)
from scripts.agent_team_queue import SQLiteMessageQueue
from scripts.project_agents import load_and_validate, resolve_binding


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ProfessionalProfileContextTests(unittest.TestCase):
    def test_all_fixed_seat_capabilities_receive_one_professional_binding(self) -> None:
        with tempfile.TemporaryDirectory(prefix="profile-context-") as temporary:
            root = Path(temporary)
            base_oid, head_oid, bare_repo = self._create_repository(root)
            registry_path = root / "repositories.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "repositories": {
                            "target-1": {
                                "bare_repo": str(bare_repo),
                                "default_branch": "main",
                            }
                        }
                    }
                ),
                encoding="utf-8",
                newline="\n",
            )
            queue = SQLiteMessageQueue(root / "ax.db")
            context_compiler = ContextCompiler(
                queue=queue,
                registry=RepositoryRegistry(registry_path),
            )
            profile_catalog = ProfessionalProfileCatalog.load(
                PROJECT_ROOT / "profile"
            )
            profile_resolver = ProfessionalProfileResolver(profile_catalog)
            profile_compiler = ProfessionalProfileCompiler(profile_catalog)
            bundle = load_and_validate()
            capability_cases = {
                "pm": ("requirements", "goal-loop"),
                "pl": ("planning", "module-loop"),
                "ta": ("code-review", "research-loop"),
                "developer": ("implementation", "map-codebase"),
                "qa_sdet": ("integration-validation", "research-loop"),
                "build_release": ("build-validation", "research-loop"),
            }

            observed_cases: set[tuple[str, str]] = set()
            for seat_id, seat in bundle["seats"].items():
                for capability_id in seat["capabilities"]:
                    gate, workflow_skill = capability_cases[capability_id]
                    observed_cases.add((seat["slot_key"], capability_id))
                    with self.subTest(
                        slot=seat["slot_key"], capability=capability_id
                    ):
                        activation_id = (
                            f"activation-{seat['slot_key']}-{capability_id}"
                        )
                        resolution = profile_resolver.resolve(
                            ProfileResolutionRequest(
                                activation_id=activation_id,
                                role=capability_id,
                                gate_or_task=gate,
                                subject_oid=head_oid,
                                target_paths=("src/app.py",),
                                write_scope=("src/app.py",),
                                repository_manifests={
                                    "pyproject.toml": "[project]\nname='sample'\n"
                                },
                                build_evidence={
                                    "subject_oid": head_oid,
                                    "repository_manifests_oid": head_oid,
                                    "primary_technology": "python",
                                    "toolchain": "python",
                                },
                            )
                        )
                        compiled = profile_compiler.compile(
                            resolution,
                            activation_root=root / "activations" / activation_id,
                        )
                        queue.enqueue(
                            thread_id="thread-1",
                            work_item_id="work-1",
                            from_role="pl",
                            to_role=capability_id,
                            message_type="WORK_ASSIGNED",
                            payload={
                                "task": gate,
                                "changed_paths": ["src/app.py"],
                            },
                            dedupe_key=(
                                f"context-{seat['slot_key']}-{capability_id}"
                            ),
                        )
                        packet = context_compiler.compile(
                            thread_id="thread-1",
                            work_item_id="work-1",
                            target_role=capability_id,
                            actor_seat_id=seat_id,
                            repo_id="target-1",
                            base_oid=base_oid,
                            head_oid=head_oid,
                            context_profile="auto",
                            context_action=gate,
                            selected_skill_ids=(workflow_skill,),
                            compiled_profile_ref=compiled.compiled_path,
                            compiled_profile_digest=compiled.compiled_digest,
                        )

                        professional_skills = [
                            skill
                            for skill in packet["skill_packet"]["skills"]
                            if skill["kind"] == "professional"
                        ]
                        self.assertEqual(1, len(professional_skills))
                        self.assertEqual(
                            PROFESSIONAL_SKILL_ID,
                            professional_skills[0]["id"],
                        )
                        self.assertEqual(
                            {
                                "skill_id": PROFESSIONAL_SKILL_ID,
                                "compiled_profile_ref": str(
                                    compiled.compiled_path
                                ),
                                "compiled_profile_digest": (
                                    compiled.compiled_digest
                                ),
                            },
                            packet["professional_profile"],
                        )
                        self.assertEqual(
                            capability_id,
                            packet["agent_binding"]["active_capability"],
                        )
                        self.assertEqual(
                            bundle["profiles"][
                                seat["capability_runtime_profiles"][capability_id]
                            ]["model"],
                            packet["agent_binding"][
                                "runtime_activation_contract"
                            ]["model"],
                        )

            self.assertEqual(
                {
                    ("pm_ta", "pm"),
                    ("pm_ta", "ta"),
                    ("pl", "pl"),
                    ("dev_1", "developer"),
                    ("dev_2", "developer"),
                    ("qa_build", "qa_sdet"),
                    ("qa_build", "build_release"),
                },
                observed_cases,
            )

    def test_technology_selection_does_not_change_the_developer_model(self) -> None:
        bundle = load_and_validate()
        seat_id = next(
            seat_id
            for seat_id, seat in bundle["seats"].items()
            if seat["slot_key"] == "dev_1"
        )
        baseline = resolve_binding(
            bundle, seat_id, [], capability_id="developer"
        )
        with tempfile.TemporaryDirectory(prefix="profile-model-") as temporary:
            root = Path(temporary)
            catalog = ProfessionalProfileCatalog.load(PROJECT_ROOT / "profile")
            resolver = ProfessionalProfileResolver(catalog)
            compiler = ProfessionalProfileCompiler(catalog)
            selections = (
                (
                    "python",
                    ("src/app.py",),
                    {"pyproject.toml": "[project]\nname='sample'\n"},
                    "python",
                ),
                (
                    "rust",
                    ("src/lib.rs",),
                    {"Cargo.toml": "[package]\nname='sample'\n"},
                    "rust",
                ),
            )

            for technology, paths, manifests, toolchain in selections:
                resolution = resolver.resolve(
                    ProfileResolutionRequest(
                        activation_id=f"activation-{technology}",
                        role="developer",
                        gate_or_task="implementation",
                        subject_oid="a" * 40,
                        target_paths=paths,
                        write_scope=paths,
                        repository_manifests=manifests,
                        build_evidence={
                            "subject_oid": "a" * 40,
                            "primary_technology": technology,
                            "toolchain": toolchain,
                        },
                    )
                )
                compiler.compile(
                    resolution,
                    activation_root=root / technology,
                )
                binding = resolve_binding(
                    bundle, seat_id, [], capability_id="developer"
                )
                self.assertEqual(
                    baseline["runtime_activation_contract"]["model"],
                    binding["runtime_activation_contract"]["model"],
                )
                self.assertEqual(
                    "profile-pinned",
                    binding["runtime_activation_contract"]["model_policy"],
                )
                self.assertTrue(
                    binding["professional_profile_policy"]["model_invariant"]
                )

    def test_missing_or_changed_compiled_profile_blocks_context(self) -> None:
        with tempfile.TemporaryDirectory(prefix="profile-context-invalid-") as temporary:
            root = Path(temporary)
            base_oid, head_oid, bare_repo = self._create_repository(root)
            registry_path = root / "repositories.json"
            registry_path.write_text(
                json.dumps(
                    {"repositories": {"target-1": {"bare_repo": str(bare_repo)}}}
                ),
                encoding="utf-8",
                newline="\n",
            )
            queue = SQLiteMessageQueue(root / "ax.db")
            compiler = ContextCompiler(
                queue=queue,
                registry=RepositoryRegistry(registry_path),
            )
            bundle = load_and_validate()
            seat_id = next(
                seat_id
                for seat_id, seat in bundle["seats"].items()
                if seat["slot_key"] == "dev_1"
            )

            with self.assertRaises(ContextSelectionError):
                compiler.compile(
                    thread_id="thread-1",
                    work_item_id="work-1",
                    target_role="developer",
                    actor_seat_id=seat_id,
                    repo_id="target-1",
                    base_oid=base_oid,
                    head_oid=head_oid,
                    context_profile="auto",
                )

            with self.assertRaisesRegex(
                ContextSelectionError,
                "Compiled professional profile is missing",
            ):
                compiler.compile(
                    thread_id="thread-1",
                    work_item_id="work-1",
                    target_role="developer",
                    actor_seat_id=seat_id,
                    repo_id="target-1",
                    base_oid=base_oid,
                    head_oid=head_oid,
                    context_profile="auto",
                    compiled_profile_ref=root / "missing-professional-profile.json",
                    compiled_profile_digest="0" * 64,
                )

            profile_catalog = ProfessionalProfileCatalog.load(
                PROJECT_ROOT / "profile"
            )
            resolution = ProfessionalProfileResolver(profile_catalog).resolve(
                ProfileResolutionRequest(
                    activation_id="activation-invalid",
                    role="developer",
                    gate_or_task="implementation",
                    subject_oid=head_oid,
                    target_paths=("src/app.py",),
                    write_scope=("src/app.py",),
                    repository_manifests={
                        "pyproject.toml": "[project]\nname='sample'\n"
                    },
                    build_evidence={
                        "subject_oid": head_oid,
                        "primary_technology": "python",
                    },
                )
            )
            compiled = ProfessionalProfileCompiler(profile_catalog).compile(
                resolution,
                activation_root=root / "activation-invalid",
            )
            compiled.compiled_path.write_bytes(
                compiled.compiled_path.read_bytes() + b" "
            )
            with self.assertRaises(ContextSelectionError):
                compiler.compile(
                    thread_id="thread-1",
                    work_item_id="work-1",
                    target_role="developer",
                    actor_seat_id=seat_id,
                    repo_id="target-1",
                    base_oid=base_oid,
                    head_oid=head_oid,
                    context_profile="auto",
                    compiled_profile_ref=compiled.compiled_path,
                    compiled_profile_digest=compiled.compiled_digest,
                )

    @classmethod
    def _create_repository(cls, root: Path) -> tuple[str, str, Path]:
        checkout = root / "checkout"
        checkout.mkdir()
        cls._git(checkout, "init", "-b", "main")
        cls._git(checkout, "config", "user.name", "Agent Team Test")
        cls._git(checkout, "config", "user.email", "agent-team@example.invalid")
        (checkout / "src").mkdir()
        (checkout / "src" / "app.py").write_text(
            "value = 1\n",
            encoding="utf-8",
            newline="\n",
        )
        (checkout / "pyproject.toml").write_text(
            "[project]\nname='sample'\nversion='1.0.0'\n",
            encoding="utf-8",
            newline="\n",
        )
        cls._git(checkout, "add", ".")
        cls._git(checkout, "commit", "-m", "base")
        base_oid = cls._git(checkout, "rev-parse", "HEAD").strip()
        (checkout / "src" / "app.py").write_text(
            "value = 2\n",
            encoding="utf-8",
            newline="\n",
        )
        cls._git(checkout, "add", "src/app.py")
        cls._git(checkout, "commit", "-m", "change")
        head_oid = cls._git(checkout, "rev-parse", "HEAD").strip()
        bare_repo = root / "repository.git"
        result = subprocess.run(
            ["git", "clone", "--bare", str(checkout), str(bare_repo)],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        return base_oid, head_oid, bare_repo

    @staticmethod
    def _git(cwd: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        return result.stdout


if __name__ == "__main__":
    unittest.main()
