from __future__ import annotations

import hashlib
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from scripts import agent_team_migration as migration_module
from scripts.agent_team_migration import MigrationError


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def durable_states(migrator) -> tuple[str, str]:
    with closing(sqlite3.connect(migrator.authority.state_database)) as connection:
        migration_state = connection.execute(
            "SELECT state FROM migration_runs WHERE id = ?",
            (migrator.migration_id,),
        ).fetchone()[0]
        deletion_state = connection.execute(
            "SELECT state FROM deletion_manifests WHERE id = ?",
            (f"deletion-{migrator.migration_id}",),
        ).fetchone()[0]
    return migration_state, deletion_state


class AxMigrationCutoverIntegrationTests(unittest.TestCase):
    def test_real_wal_dry_run_reads_committed_rows_without_source_mutation(self) -> None:
        from tests.unit.test_ax_migration import AxMigrationTests, file_observation

        fixture = AxMigrationTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        database = fixture.legacy / "state" / "agent-team.db"
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        self.assertEqual(
            "wal", connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        )
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.execute(
            "INSERT INTO activations(id, state) VALUES (?, ?)",
            ("activation-wal-only", "RUNNING"),
        )
        connection.commit()
        wal = database.with_name(database.name + "-wal")
        shm = database.with_name(database.name + "-shm")
        self.assertTrue(wal.is_file())
        self.assertTrue(shm.is_file())
        protected = (database, wal, shm)
        before = {path: file_observation(path) for path in protected}

        first = fixture.migrator().dry_run()
        second = fixture.migrator().dry_run()

        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertIn("activation-wal-only", first.quarantined_activation_ids)
        self.assertEqual(
            before, {path: file_observation(path) for path in protected}
        )
        self.assertFalse(fixture.ax_root.exists())

    def test_cutover_compensates_database_failure_and_retries_idempotently(self) -> None:
        from tests.unit.test_ax_migration import AxMigrationTests

        fixture = AxMigrationTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        migrator = fixture.migrator()
        migrator.apply()
        prior = b'{"legacy":"cutover-db-failure"}\n'
        migrator.pointer_path.write_bytes(prior)

        with mock.patch.object(
            migrator,
            "_mark_cut_over",
            side_effect=sqlite3.OperationalError("injected cutover DB failure"),
        ):
            with self.assertRaisesRegex(MigrationError, "prior pointer was restored"):
                migrator.cutover()

        self.assertEqual(prior, migrator.pointer_path.read_bytes())
        self.assertEqual(("COMPLETED", "VERIFIED"), durable_states(migrator))
        first = migrator.cutover()
        replay = migrator.cutover()
        self.assertTrue(first.cut_over)
        self.assertTrue(replay.cut_over)
        self.assertEqual(("CUT_OVER", "VERIFIED"), durable_states(migrator))

    def test_cutover_pointer_failure_preserves_completed_state(self) -> None:
        from tests.unit.test_ax_migration import AxMigrationTests

        fixture = AxMigrationTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        migrator = fixture.migrator()
        migrator.apply()
        prior = b'{"legacy":"cutover-pointer-failure"}\n'
        migrator.pointer_path.write_bytes(prior)
        original_atomic_write = migration_module._atomic_write

        def fail_pointer(path, content, *, read_only=False):
            if Path(path) == migrator.pointer_path:
                raise OSError("injected pointer failure")
            return original_atomic_write(path, content, read_only=read_only)

        with mock.patch(
            "scripts.agent_team_migration._atomic_write",
            side_effect=fail_pointer,
        ):
            with self.assertRaisesRegex(MigrationError, "pointer swap failed"):
                migrator.cutover()

        self.assertEqual(prior, migrator.pointer_path.read_bytes())
        self.assertEqual(("COMPLETED", "VERIFIED"), durable_states(migrator))
        self.assertTrue(migrator.cutover().cut_over)

    def test_rollback_compensates_db_failure_and_recutover_is_rejected(self) -> None:
        from tests.unit.test_ax_migration import AxMigrationTests

        fixture = AxMigrationTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        migrator = fixture.migrator()
        migrator.apply()
        prior = b'{"legacy":"rollback-db-failure"}\n'
        migrator.pointer_path.write_bytes(prior)
        migrator.cutover()
        desired = migrator.pointer_path.read_bytes()

        with mock.patch.object(
            migrator,
            "_mark_rolled_back",
            side_effect=sqlite3.OperationalError("injected rollback DB failure"),
        ):
            with self.assertRaisesRegex(MigrationError, "active pointer was restored"):
                migrator.rollback()

        self.assertEqual(desired, migrator.pointer_path.read_bytes())
        self.assertEqual(("CUT_OVER", "VERIFIED"), durable_states(migrator))
        first = migrator.rollback()
        replay = migrator.rollback()
        self.assertTrue(first.rolled_back)
        self.assertTrue(replay.rolled_back)
        self.assertEqual(prior, migrator.pointer_path.read_bytes())
        self.assertEqual(("ROLLED_BACK", "ROLLED_BACK"), durable_states(migrator))
        verification = migrator.verify()
        self.assertTrue(verification.verified)
        self.assertTrue(verification.rolled_back)
        replayed_apply = migrator.apply()
        self.assertTrue(replayed_apply.rolled_back)
        self.assertFalse(replayed_apply.cut_over)
        with self.assertRaisesRegex(MigrationError, "new migration generation"):
            migrator.cutover()
        self.assertEqual(("ROLLED_BACK", "ROLLED_BACK"), durable_states(migrator))

    def test_dry_run_apply_verify_cutover_and_rollback_preserve_v3(self) -> None:
        from tests.unit.test_ax_migration import AxMigrationTests

        fixture = AxMigrationTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        database = fixture.legacy / "state" / "agent-team.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
        wal = database.with_name(database.name + "-wal")
        shm = database.with_name(database.name + "-shm")
        wal.write_bytes(b"legacy-v3-wal-evidence\n")
        shm.write_bytes(b"legacy-v3-shm-evidence\n")
        extra_unknown = fixture.checkout / "nested" / "operator.keep"
        extra_unknown.parent.mkdir()
        extra_unknown.write_text("untracked operator bytes\n", encoding="utf-8")

        protected = (
            fixture.checkout / "README.md",
            fixture.checkout / "unknown.keep",
            extra_unknown,
            database,
            wal,
            shm,
            fixture.legacy / "artifacts" / "goal-1" / "evidence.txt",
        )
        before = {path: sha256(path) for path in protected}
        migrator = fixture.migrator()
        # Inject the activation-table observation so SQLite never opens or rewrites
        # the SHM fixture while the dry-run no-write boundary is under test.
        with mock.patch(
            "scripts.agent_team_migration._read_legacy_activations",
            return_value=(("activation-running",), ("activation-done",)),
        ):
            first = migrator.dry_run()
            second = migrator.dry_run()
            self.assertEqual(first.as_dict(), second.as_dict())
            self.assertFalse(fixture.ax_root.exists())
            self.assertEqual(before, {path: sha256(path) for path in protected})
            evidence_names = {Path(item.source).name for item in first.evidence_files}
            self.assertTrue({"agent-team.db", "agent-team.db-wal", "agent-team.db-shm"} <= evidence_names)
            self.assertIn("evidence.txt", evidence_names)
            self.assertEqual(("activation-running",), first.quarantined_activation_ids)
            self.assertTrue(first.as_dict()["pl_reissue_required"])
            self.assertTrue(
                all(
                    operation["target"] == "PL_REISSUE_REQUIRED"
                    for operation in first.migration_manifest["operations"]
                    if operation["kind"] == "quarantine"
                )
            )

            applied = migrator.apply()
            verified = migrator.verify()
            self.assertTrue(applied.verified)
            self.assertTrue(verified.verified)
            self.assertEqual(before, {path: sha256(path) for path in protected})
            for evidence in first.evidence_files:
                target = Path(evidence.target)
                self.assertTrue(target.is_file())
                self.assertEqual(evidence.sha256, sha256(target))

            prior_pointer = b'{"legacy":"control-plane-v3"}\n'
            migrator.pointer_path.write_bytes(prior_pointer)
            cutover = migrator.cutover()
            self.assertTrue(cutover.cut_over)
            rolled_back = migrator.rollback()
            self.assertTrue(rolled_back.rolled_back)
            self.assertEqual(prior_pointer, migrator.pointer_path.read_bytes())
            self.assertTrue(Path(applied.managed_repository).is_dir())
            self.assertTrue(all(Path(item.target).is_file() for item in first.evidence_files))
            self.assertEqual(before, {path: sha256(path) for path in protected})


if __name__ == "__main__":
    unittest.main()
