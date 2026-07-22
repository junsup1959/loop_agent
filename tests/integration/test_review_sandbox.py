from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from scripts.agent_team_activation import ReviewSandboxMaterializer
from scripts.agent_team_domain import SourceIntegrity
from scripts.agent_team_state import utc_now
from tests.unit.test_workspace_leases import WorkspaceFixture, run_git


class ReviewSandboxIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture(self)
        self.materializer = ReviewSandboxMaterializer(
            state_store=self.fixture.store,
            path_authority=self.fixture.authority,
            repository_service=self.fixture.service,
        )
        self._insert_run()

    def _insert_run(self) -> None:
        now = utc_now()
        with self.fixture.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, goal_id, target_id, base_oid, state,
                    idempotency_key, created_at
                ) VALUES (
                    'review-run', 'goal-1', ?, ?, 'RUNNING',
                    'review-run-key', ?
                )
                """,
                (
                    self.fixture.registration.target_id,
                    self.fixture.base_oid,
                    now,
                ),
            )

    def add_activation(self, activation_id: str) -> None:
        now = utc_now()
        with self.fixture.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO activations (
                    id, target_id, goal_id, run_id, subject_oid,
                    role, gate_or_task, state, idempotency_key,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, 'goal-1', 'review-run', ?,
                    'ta', 'code-review', 'PROFILE_BOUND', ?, ?, ?
                )
                """,
                (
                    activation_id,
                    self.fixture.registration.target_id,
                    self.fixture.base_oid,
                    f"activation-key:{activation_id}",
                    now,
                    now,
                ),
            )

    def refs(self) -> str:
        managed = Path(self.fixture.registration.managed_repository_path)
        return run_git(
            managed,
            f"--git-dir={managed}",
            "for-each-ref",
            "--format=%(refname) %(objectname)",
        ).stdout

    def materialize(self, activation_id: str):
        self.add_activation(activation_id)
        return self.materializer.materialize(
            activation_id=activation_id,
            target_id=self.fixture.registration.target_id,
            subject_oid=self.fixture.base_oid,
            generated_paths=(),
        )

    def test_executable_exact_oid_sandbox_has_independent_metadata(self) -> None:
        before_refs = self.refs()
        receipt = self.materialize("review-1")
        replay = self.materializer.materialize(
            activation_id="review-1",
            target_id=self.fixture.registration.target_id,
            subject_oid=self.fixture.base_oid,
            generated_paths=(),
        )
        self.assertEqual(receipt, replay)
        self.assertTrue(receipt.git_dir.is_dir())
        self.assertFalse((receipt.git_dir / "commondir").exists())
        self.assertFalse(
            (receipt.git_dir / "objects" / "info" / "alternates").exists()
        )
        self.assertEqual(
            "",
            run_git(receipt.source_root, "remote").stdout.strip(),
        )
        self.assertEqual(
            self.fixture.base_oid,
            run_git(receipt.source_root, "rev-parse", "HEAD").stdout.strip(),
        )
        contract = self.materializer.runner_contract(receipt.sandbox_id)
        self.assertEqual(receipt.source_root, contract.cwd)
        self.assertEqual(
            (
                receipt.build_root,
                receipt.test_database_root,
                receipt.cache_root,
                receipt.scratch_root,
                receipt.install_root,
            ),
            contract.ephemeral_writable_roots,
        )
        self.assertEqual((), contract.generated_write_roots)
        self.assertIn(
            Path(self.fixture.registration.managed_repository_path).resolve(),
            contract.prohibited_authority_roots,
        )

        command = (
            "from pathlib import Path; "
            f"Path({str(receipt.build_root / 'build.ok')!r}).write_text('ok')"
        )
        completed = subprocess.run(
            [sys.executable, "-c", command],
            cwd=contract.cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        integrity = self.materializer.verify_integrity(receipt.sandbox_id)
        self.assertEqual(SourceIntegrity.CLEAN, integrity.classification)
        self.assertTrue(integrity.gate_eligible)
        self.assertEqual((), integrity.ignored_generated_paths)
        self.assertEqual(before_refs, self.refs())

        destroyed = self.materializer.destroy(receipt.sandbox_id)
        replayed_destroy = self.materializer.destroy(receipt.sandbox_id)
        self.assertEqual(destroyed, replayed_destroy)
        self.assertFalse(receipt.sandbox_root.exists())

    def test_dirty_requires_new_activation_and_clean_recreation(self) -> None:
        dirty = self.materialize("review-dirty")
        (dirty.source_root / "README.md").write_text(
            "exploratory\n", encoding="utf-8"
        )
        integrity = self.materializer.verify_integrity(dirty.sandbox_id)
        self.assertEqual(SourceIntegrity.ANALYSIS_DIRTY, integrity.classification)
        self.assertFalse(integrity.gate_eligible)
        self.assertEqual(("README.md",), integrity.tracked_changes)

        clean = self.materialize("review-clean-rerun")
        clean_integrity = self.materializer.verify_integrity(clean.sandbox_id)
        self.assertEqual(SourceIntegrity.CLEAN, clean_integrity.classification)
        self.assertEqual(dirty.subject_oid, clean.subject_oid)
        self.assertNotEqual(dirty.sandbox_id, clean.sandbox_id)
        self.materializer.destroy(dirty.sandbox_id)
        self.materializer.destroy(clean.sandbox_id)

    def test_metadata_tampering_and_authority_link_invalidate(self) -> None:
        receipt = self.materialize("review-tampered")
        managed = Path(self.fixture.registration.managed_repository_path).resolve()
        with (receipt.git_dir / "config").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                '\n[remote "authority"]\n'
                f"\turl = {managed.as_posix()}\n"
                "\tfetch = +refs/heads/*:refs/remotes/authority/*\n"
            )
        integrity = self.materializer.verify_integrity(receipt.sandbox_id)
        self.assertEqual(SourceIntegrity.INVALIDATED, integrity.classification)
        self.assertFalse(integrity.gate_eligible)
        self.assertTrue(
            any(
                "metadata" in reason.casefold()
                or "authority" in reason.casefold()
                or "remote" in reason.casefold()
                for reason in integrity.reasons
            )
        )
        self.materializer.destroy(receipt.sandbox_id)


if __name__ == "__main__":
    unittest.main()
