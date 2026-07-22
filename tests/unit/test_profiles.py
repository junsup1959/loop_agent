from __future__ import annotations

import hashlib
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.agent_team_profiles import (
    PROFESSIONAL_SKILL_ID,
    ProfileCatalogError,
    ProfileCompilationError,
    ProfileResolutionError,
    ProfileResolutionRequest,
    ProfessionalProfileCatalog,
    ProfessionalProfileCompiler,
    ProfessionalProfileResolver,
)
from scripts.agent_team_state import AxStateStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_ROOT = PROJECT_ROOT / "profile"
OID = "a" * 40
NOW = "2026-07-21T00:00:00.000000+00:00"


class ProfessionalProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = ProfessionalProfileCatalog.load(PROFILE_ROOT)
        self.resolver = ProfessionalProfileResolver(self.catalog)

    @staticmethod
    def request(
        *,
        activation_id: str = "activation-1",
        role: str = "dev_1",
        gate: str = "implementation",
        target_paths: tuple[str, ...],
        manifests: dict[str, str],
        primary: str | None = None,
        secondary: str | None = None,
        toolchain: str | None = None,
        extra_evidence: dict[str, object] | None = None,
    ) -> ProfileResolutionRequest:
        evidence: dict[str, object] = {
            "subject_oid": OID,
            "repository_manifests_oid": OID,
        }
        if primary is not None:
            evidence["primary_technology"] = primary
        if secondary is not None:
            evidence["secondary_technology"] = secondary
        if toolchain is not None:
            evidence["toolchain"] = toolchain
        evidence.update(extra_evidence or {})
        return ProfileResolutionRequest(
            activation_id=activation_id,
            role=role,
            gate_or_task=gate,
            subject_oid=OID,
            target_paths=target_paths,
            write_scope=target_paths,
            repository_manifests=manifests,
            build_evidence=evidence,
        )

    def test_all_supported_technologies_and_toolchains_resolve(self) -> None:
        cases = {
            "python": (
                ("src/app.py",),
                {"pyproject.toml": "[project]\nname='sample'\n"},
                "python",
            ),
            "rust": (
                ("src/lib.rs",),
                {"Cargo.toml": "[package]\nname='sample'\n"},
                "rust",
            ),
            "cpp": (
                ("src/main.cpp",),
                {"CMakeLists.txt": "project(sample)\nadd_executable(sample main.cpp)\n"},
                "cpp",
            ),
            "dotnet": (
                ("src/App.cs",),
                {"src/App.csproj": "<Project Sdk=\"Microsoft.NET.Sdk\"><TargetFramework>net8.0</TargetFramework></Project>"},
                "dotnet",
            ),
            "powershell": (
                ("tools/Invoke-Build.ps1",),
                {"tools/Module.psd1": "@{ RootModule='Module.psm1'; ModuleVersion='1.0.0' }"},
                "powershell",
            ),
            "electron": (
                ("desktop/electron-main.ts",),
                {"package.json": "{\"devDependencies\":{\"electron\":\"1.0.0\"}}"},
                "node",
            ),
            "embedded-devices": (
                ("firmware/main.ino",),
                {"platformio.ini": "[env:device]\nplatform=native\n"},
                "cpp",
            ),
            "local-data": (
                ("database/migrations/001.sql",),
                {},
                "python",
            ),
            "desktop-ui": (
                ("src/Views/MainWindow.xaml",),
                {},
                "dotnet",
            ),
        }

        for technology, (paths, manifests, toolchain) in cases.items():
            with self.subTest(technology=technology):
                resolution = self.resolver.resolve(
                    self.request(
                        activation_id=f"activation-{technology}",
                        target_paths=paths,
                        manifests=manifests,
                        primary=technology,
                        toolchain=toolchain,
                    )
                )
                self.assertEqual(
                    f"technology/{technology}",
                    resolution.primary_technology_ref.reference_id,
                )
                self.assertEqual(
                    f"toolchain/{toolchain}",
                    resolution.toolchain_ref.reference_id,
                )

    def test_mixed_primary_and_secondary_are_explicit_and_ordered(self) -> None:
        resolution = self.resolver.resolve(
            self.request(
                target_paths=("src/app.py", "database/migrations/001.sql"),
                manifests={"pyproject.toml": "[project]\nname='sample'\n"},
                primary="python",
                secondary="local-data",
                toolchain="python",
            )
        )

        self.assertEqual(
            "technology/python",
            resolution.primary_technology_ref.reference_id,
        )
        self.assertEqual(
            "technology/local-data",
            resolution.secondary_technology_ref.reference_id,
        )
        self.assertEqual(
            (
                "role",
                "gate_or_task",
                "primary_technology",
                "secondary_technology",
                "toolchain",
            ),
            tuple(kind for kind, _ in resolution.ordered_references),
        )

    def test_elastic_capabilities_resolve_expertise_without_authority_data(self) -> None:
        worker = self.resolver.resolve(
            self.request(
                activation_id="activation-worker",
                role="worker",
                target_paths=("src/app.py",),
                manifests={"pyproject.toml": "[project]\nname='sample'\n"},
                primary="python",
            )
        )
        advisory = self.resolver.resolve(
            self.request(
                activation_id="activation-advisory",
                role="advisory",
                gate="code-review",
                target_paths=("src/app.py",),
                manifests={"pyproject.toml": "[project]\nname='sample'\n"},
                primary="python",
            )
        )
        self.assertEqual("role/developer", worker.role_ref.reference_id)
        self.assertEqual("role/ta", advisory.role_ref.reference_id)

    def test_profile_catalog_rejects_authority_or_model_expansion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="profile-authority-") as temporary:
            copied = Path(temporary) / "profile"
            shutil.copytree(PROFILE_ROOT, copied)
            catalog_path = copied / "catalog.toml"
            text = catalog_path.read_text(encoding="utf-8")
            catalog_path.write_text(
                text.replace(
                    "authority_expansion_allowed = false",
                    "authority_expansion_allowed = true",
                    1,
                ),
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(ProfileCatalogError):
                ProfessionalProfileCatalog.load(copied)

    def test_ambiguous_or_conflicting_evidence_fails_closed(self) -> None:
        ambiguous = self.request(
            target_paths=("src/app.py", "src/lib.rs"),
            manifests={},
        )
        with self.assertRaises(ProfileResolutionError):
            self.resolver.resolve(ambiguous)

        conflicting_toolchain = self.request(
            target_paths=("src/app.py",),
            manifests={"pyproject.toml": "[project]\nname='sample'\n"},
            primary="python",
            toolchain="rust",
        )
        with self.assertRaises(ProfileResolutionError):
            self.resolver.resolve(conflicting_toolchain)

        duplicate_toolchain = self.request(
            target_paths=("src/app.py",),
            manifests={"pyproject.toml": "[project]\nname='sample'\n"},
            primary="python",
            extra_evidence={"toolchains": ["python", "python"]},
        )
        with self.assertRaises(ProfileResolutionError):
            self.resolver.resolve(duplicate_toolchain)

        duplicate_technology = self.request(
            target_paths=("src/app.py",),
            manifests={"pyproject.toml": "[project]\nname='sample'\n"},
            extra_evidence={"technologies": ["python", "python"]},
        )
        with self.assertRaises(ProfileResolutionError):
            self.resolver.resolve(duplicate_technology)

        wrong_oid = self.request(
            target_paths=("src/app.py",),
            manifests={"pyproject.toml": "[project]\nname='sample'\n"},
            primary="python",
            extra_evidence={"repository_manifests_oid": "b" * 40},
        )
        with self.assertRaises(ProfileResolutionError):
            self.resolver.resolve(wrong_oid)

    def test_repository_and_catalog_path_traversal_are_rejected(self) -> None:
        with self.assertRaises(ProfileResolutionError):
            self.request(
                target_paths=("../outside.py",),
                manifests={},
                primary="python",
            )

        with tempfile.TemporaryDirectory(prefix="profile-traversal-") as temporary:
            copied = Path(temporary) / "profile"
            shutil.copytree(PROFILE_ROOT, copied)
            catalog_path = copied / "catalog.toml"
            text = catalog_path.read_text(encoding="utf-8")
            catalog_path.write_text(
                text.replace(
                    'path = "roles/developer.md"',
                    'path = "../developer.md"',
                    1,
                ),
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(ProfileCatalogError):
                ProfessionalProfileCatalog.load(copied)

        with tempfile.TemporaryDirectory(prefix="profile-category-") as temporary:
            copied = Path(temporary) / "profile"
            shutil.copytree(PROFILE_ROOT, copied)
            catalog_path = copied / "catalog.toml"
            text = catalog_path.read_text(encoding="utf-8")
            catalog_path.write_text(
                text.replace(
                    'category = "role"',
                    'category = "technology"',
                    1,
                ),
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(ProfileCatalogError):
                ProfessionalProfileCatalog.load(copied)

    def test_symlink_escape_is_rejected_when_host_supports_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="profile-symlink-") as temporary:
            root = Path(temporary)
            copied = root / "profile"
            shutil.copytree(PROFILE_ROOT, copied)
            source = copied / "roles" / "developer.md"
            outside = root / "outside-developer.md"
            outside.write_bytes(source.read_bytes())
            source.unlink()
            try:
                source.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"host cannot create file symlinks: {exc}")

            with self.assertRaises(ProfileCatalogError):
                ProfessionalProfileCatalog.load(copied)

    def test_missing_digest_mismatch_and_changed_reference_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="profile-digest-") as temporary:
            copied = Path(temporary) / "profile"
            shutil.copytree(PROFILE_ROOT, copied)
            python_profile = copied / "technologies" / "python.md"
            python_profile.write_text(
                python_profile.read_text(encoding="utf-8") + "\nchanged\n",
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(ProfileCatalogError):
                ProfessionalProfileCatalog.load(copied)

        with tempfile.TemporaryDirectory(prefix="profile-missing-") as temporary:
            copied = Path(temporary) / "profile"
            shutil.copytree(PROFILE_ROOT, copied)
            (copied / "roles" / "developer.md").unlink()
            with self.assertRaises(ProfileCatalogError):
                ProfessionalProfileCatalog.load(copied)

        with tempfile.TemporaryDirectory(prefix="profile-changed-") as temporary:
            copied = Path(temporary) / "profile"
            shutil.copytree(PROFILE_ROOT, copied)
            catalog = ProfessionalProfileCatalog.load(copied)
            resolver = ProfessionalProfileResolver(catalog)
            resolution = resolver.resolve(
                self.request(
                    target_paths=("src/app.py",),
                    manifests={"pyproject.toml": "[project]\nname='sample'\n"},
                    primary="python",
                )
            )
            path = copied / "technologies" / "python.md"
            path.write_text(
                path.read_text(encoding="utf-8") + "\nchanged\n",
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(ProfileCompilationError):
                ProfessionalProfileCompiler(catalog).compile(
                    resolution,
                    activation_root=Path(temporary) / "activation",
                )

    def test_compilation_is_byte_and_digest_deterministic_across_activations(self) -> None:
        first_resolution = self.resolver.resolve(
            self.request(
                activation_id="activation-first",
                target_paths=("src/app.py",),
                manifests={"pyproject.toml": "[project]\nname='sample'\n"},
                primary="python",
            )
        )
        second_resolution = self.resolver.resolve(
            self.request(
                activation_id="activation-second",
                target_paths=("src/app.py",),
                manifests={"pyproject.toml": "[project]\nname='sample'\n"},
                primary="python",
            )
        )

        with tempfile.TemporaryDirectory(prefix="profile-compile-") as temporary:
            root = Path(temporary)
            compiler = ProfessionalProfileCompiler(self.catalog)
            first = compiler.compile(
                first_resolution,
                activation_root=root / "first",
            )
            second = compiler.compile(
                second_resolution,
                activation_root=root / "second",
            )

            self.assertEqual(first.compiled_digest, second.compiled_digest)
            self.assertEqual(
                first.compiled_path.read_bytes(),
                second.compiled_path.read_bytes(),
            )
            self.assertEqual(
                first.compiled_digest,
                hashlib.sha256(first.compiled_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                root / "first" / "professional-profile.json",
                first.compiled_path,
            )

    def test_compiler_persists_profile_bindings_references_and_audit(self) -> None:
        resolution = self.resolver.resolve(
            self.request(
                target_paths=("src/app.py",),
                manifests={"pyproject.toml": "[project]\nname='sample'\n"},
                primary="python",
            )
        )
        with tempfile.TemporaryDirectory(prefix="profile-state-") as temporary:
            root = Path(temporary)
            store = AxStateStore(root / "ax.db")
            store.initialize()
            self._insert_activation(store)
            compiler = ProfessionalProfileCompiler(
                self.catalog,
                state_store=store,
            )

            compiled = compiler.compile(
                resolution,
                activation_root=root / "activation",
            )
            replay = compiler.compile(
                resolution,
                activation_root=root / "activation",
            )

            self.assertEqual(compiled.compiled_digest, replay.compiled_digest)
            with store.transaction() as connection:
                binding = connection.execute(
                    """
                    SELECT professional_skill_id, compiled_profile_digest, state
                    FROM profile_bindings
                    WHERE activation_id = 'activation-1'
                    """
                ).fetchone()
                references = connection.execute(
                    """
                    SELECT reference_kind, reference_sha256
                    FROM profile_reference_bindings
                    WHERE activation_id = 'activation-1'
                    ORDER BY ordinal
                    """
                ).fetchall()
                activation = connection.execute(
                    "SELECT state FROM activations WHERE id = 'activation-1'"
                ).fetchone()
                audits = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM audit_events
                    WHERE activation_id = 'activation-1'
                      AND event_type = 'PROFESSIONAL_PROFILE_BOUND'
                    """
                ).fetchone()["count"]

            self.assertEqual(PROFESSIONAL_SKILL_ID, binding["professional_skill_id"])
            self.assertEqual(compiled.compiled_digest, binding["compiled_profile_digest"])
            self.assertEqual("BOUND", binding["state"])
            self.assertEqual("PROFILE_BOUND", activation["state"])
            self.assertEqual(4, len(references))
            self.assertEqual(1, audits)

    def test_legacy_mapping_is_complete_and_not_active(self) -> None:
        with (PROFILE_ROOT / "legacy-mappings.toml").open("rb") as stream:
            legacy = tomllib.load(stream)
        with (PROJECT_ROOT / "skills" / "catalog.toml").open("rb") as stream:
            active = tomllib.load(stream)
        with (PROFILE_ROOT / "catalog.toml").open("rb") as stream:
            profiles = tomllib.load(stream)

        mappings = legacy["mappings"]
        legacy_ids = [mapping["legacy_skill_id"] for mapping in mappings]
        active_ids = {entry["id"] for entry in active["skills"]}
        reference_ids = {entry["id"] for entry in profiles["references"]}
        self.assertEqual(13, len(mappings))
        self.assertEqual(len(legacy_ids), len(set(legacy_ids)))
        self.assertTrue(set(legacy_ids).isdisjoint(active_ids))
        self.assertTrue(
            all(
                set(mapping["replacement_refs"]) <= reference_ids
                for mapping in mappings
            )
        )
        self.assertTrue(
            all(
                not (PROJECT_ROOT / "skills" / legacy_id / "SKILL.md").exists()
                for legacy_id in legacy_ids
            )
        )

    @staticmethod
    def _insert_activation(store: AxStateStore) -> None:
        with store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO targets (
                    id, canonical_checkout_path, git_common_dir, source_ref,
                    observed_source_oid, state, created_at, updated_at
                ) VALUES (
                    'target-1', 'C:/target', 'C:/target/.git',
                    'refs/heads/main', ?, 'ACTIVE', ?, ?
                )
                """,
                (OID, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO goals (
                    id, target_id, base_oid, state, created_at, updated_at
                ) VALUES ('goal-1', 'target-1', ?, 'ACTIVE', ?, ?)
                """,
                (OID, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO runs (
                    id, goal_id, target_id, base_oid, state,
                    idempotency_key, created_at
                ) VALUES (
                    'run-1', 'goal-1', 'target-1', ?, 'RUNNING',
                    'run-key-1', ?
                )
                """,
                (OID, NOW),
            )
            connection.execute(
                """
                INSERT INTO activations (
                    id, target_id, goal_id, run_id, subject_oid,
                    role, gate_or_task, state, idempotency_key,
                    created_at, updated_at
                ) VALUES (
                    'activation-1', 'target-1', 'goal-1', 'run-1', ?,
                    'dev_1', 'implementation', 'CREATED',
                    'activation-key-1', ?, ?
                )
                """,
                (OID, NOW, NOW),
            )


if __name__ == "__main__":
    unittest.main()
