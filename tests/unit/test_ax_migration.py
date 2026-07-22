from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.agent_team_migration import (
    AgentTeamMigrator,
    MigrationError,
    _read_source_bytes,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def file_observation(path: Path) -> tuple[bytes, int, int, int, int, int]:
    content = _read_source_bytes(path)
    metadata = path.stat()
    return (
        content,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_atime_ns,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


class AxMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ax-migration-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "target"
        self.legacy = self.checkout / ".agent-team"
        self.ax_root = self.root / "runtime"
        self.checkout.mkdir()
        (self.legacy / "state").mkdir(parents=True)
        (self.legacy / "artifacts" / "goal-1").mkdir(parents=True)
        run_git(self.checkout, "init", "--initial-branch=main")
        run_git(self.checkout, "config", "user.name", "AX Fixture")
        run_git(self.checkout, "config", "user.email", "ax@example.invalid")
        (self.checkout / "README.md").write_text("base\n", encoding="utf-8")
        run_git(self.checkout, "add", "README.md")
        run_git(self.checkout, "commit", "-m", "base")
        (self.checkout / "unknown.keep").write_text(
            "operator-owned\n", encoding="utf-8"
        )
        self.unknown_before = (self.checkout / "unknown.keep").read_bytes()
        database = self.legacy / "state" / "agent-team.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TABLE activations (id TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO activations(id, state) VALUES (?, ?)",
                (("activation-running", "RUNNING"), ("activation-done", "TERMINATED")),
            )
            connection.commit()
        (self.legacy / "artifacts" / "goal-1" / "evidence.txt").write_text(
            "legacy evidence\n", encoding="utf-8"
        )

    def migrator(self) -> AgentTeamMigrator:
        return AgentTeamMigrator(
            ax_source_root=PROJECT_ROOT,
            target_checkout=self.checkout,
            legacy_root=self.legacy,
            ax_root=self.ax_root,
            repo_id="fixture-repo",
        )

    def test_dry_run_is_deterministic_and_writes_only_explicit_safe_output(self) -> None:
        migrator = self.migrator()
        database = self.legacy / "state" / "agent-team.db"
        wal = database.with_name(database.name + "-wal")
        shm = database.with_name(database.name + "-shm")
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        source_before = file_observation(database)
        first = migrator.dry_run().as_dict()
        second = migrator.dry_run().as_dict()
        self.assertEqual(first, second)
        self.assertEqual(source_before, file_observation(database))
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        self.assertFalse(self.ax_root.exists())
        self.assertEqual(self.unknown_before, (self.checkout / "unknown.keep").read_bytes())
        self.assertEqual(["activation-running"], first["quarantined_activation_ids"])
        self.assertTrue(first["pl_reissue_required"])
        mappings = {
            item["target_slot_key"]: item for item in first["migration_manifest"]["seat_mappings"]
        }
        self.assertEqual(["TA_권지호"], mappings["pm_ta"]["absorbed_seat_ids"])
        self.assertEqual(
            ["BUILD_RELEASE_정서준"], mappings["qa_build"]["absorbed_seat_ids"]
        )
        self.assertEqual(
            ["DEV_정예은"], first["migration_manifest"]["archived_seat_ids"]
        )
        self.assertTrue(
            all(
                entry["disposition"] == "RETAIN"
                for entry in first["deletion_manifest"]["entries"]
            )
        )

        output = self.root / "planning" / "manifests.json"
        migrator.dry_run(output=output)
        self.assertEqual(first, json.loads(output.read_text(encoding="utf-8")))
        with self.assertRaises(MigrationError):
            migrator.dry_run(output=self.checkout / "unsafe.json")

    def test_apply_registers_bare_repository_and_preserves_v3_evidence(self) -> None:
        result = self.migrator().apply()
        self.assertTrue(result.verified)
        self.assertTrue((self.ax_root / "repositories" / "fixture-repo.git").is_dir())
        for directory in ("repositories", "workspaces", "state", "artifacts", "activations"):
            self.assertTrue((self.ax_root / directory).is_dir())
        self.assertEqual(self.unknown_before, (self.checkout / "unknown.keep").read_bytes())

        state = json.loads(
            (
                self.ax_root
                / "state"
                / "migrations"
                / f"{result.migration_id}.json"
            ).read_text(encoding="utf-8")
        )
        for item in state["evidence_files"]:
            self.assertTrue(Path(item["target"]).is_file())
        with closing(sqlite3.connect(self.ax_root / "state" / "agent-team.db")) as connection:
            schema_version = connection.execute(
                "SELECT schema_version FROM ax_schema_meta WHERE singleton=1"
            ).fetchone()[0]
            quarantine = connection.execute(
                "SELECT action FROM migration_evidence WHERE legacy_record_id=?",
                ("activation-running",),
            ).fetchone()[0]
        self.assertEqual(4, schema_version)
        self.assertEqual("QUARANTINED", quarantine)
        replay = self.migrator().apply()
        self.assertEqual(result.migration_id, replay.migration_id)

    def test_cutover_and_rollback_restore_pointer_without_deleting_evidence(self) -> None:
        migrator = self.migrator()
        applied = migrator.apply()
        prior = b'{"legacy":"pointer"}\n'
        migrator.pointer_path.write_bytes(prior)

        cutover = migrator.cutover()
        self.assertTrue(cutover.cut_over)
        active = json.loads(migrator.pointer_path.read_text(encoding="utf-8"))
        self.assertEqual(6, active["max_threads"])
        self.assertTrue(active["mcp_servers"]["serena"]["required"])
        self.assertEqual(
            "initial_instructions",
            active["mcp_servers"]["serena"]["required_tool"],
        )
        evidence_targets = [
            Path(item["target"])
            for item in json.loads(migrator.state_path.read_text(encoding="utf-8"))[
                "evidence_files"
            ]
        ]

        rolled_back = migrator.rollback()
        self.assertTrue(rolled_back.rolled_back)
        self.assertEqual(prior, migrator.pointer_path.read_bytes())
        self.assertTrue(all(path.is_file() for path in evidence_targets))
        self.assertTrue((self.ax_root / "repositories" / "fixture-repo.git").is_dir())
        replay = migrator.rollback()
        self.assertTrue(replay.rolled_back)
        self.assertEqual(applied.migration_id, replay.migration_id)


if __name__ == "__main__":
    unittest.main()
