from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.agent_team_git import GitCASMismatchError, ManagedRepositoryService
from scripts.agent_team_paths import AxPathAuthority
from scripts.agent_team_state import AxStateStore, utc_now
from scripts.agent_team_workspace import (
    WorkspaceConflictError,
    WorkspaceGitFacade,
    WorkspaceLeaseStateError,
    WorkspaceManager,
    WorkspaceScopeError,
)


def run_git(
    cwd: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"fixture Git failed ({result.returncode}): {' '.join(arguments)}\n"
            f"{result.stderr}"
        )
    return result


class WorkspaceFixture:
    def __init__(self, case: unittest.TestCase) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ax-workspaces-")
        case.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "ax-source"
        self.runtime_root = self.root / "ax-runtime"
        self.checkout = self.root / "target"
        self.source_root.mkdir()
        self.runtime_root.mkdir()
        self.checkout.mkdir()
        run_git(self.checkout, "init", "--initial-branch=main")
        run_git(self.checkout, "config", "user.name", "AX Fixture")
        run_git(self.checkout, "config", "user.email", "ax@example.invalid")
        (self.checkout / "README.md").write_text("base\n", encoding="utf-8")
        (self.checkout / "src").mkdir()
        (self.checkout / "src" / "base.txt").write_text(
            "base\n", encoding="utf-8"
        )
        run_git(self.checkout, "add", "README.md", "src/base.txt")
        run_git(self.checkout, "commit", "-m", "base")
        self.base_oid = run_git(
            self.checkout, "rev-parse", "HEAD"
        ).stdout.strip()
        self.authority = AxPathAuthority(self.source_root, self.runtime_root)
        self.store = AxStateStore(self.authority.state_database)
        self.service = ManagedRepositoryService(
            state_store=self.store,
            path_authority=self.authority,
        )
        self.registration = self.service.register_target(
            self.checkout,
            source_ref="refs/heads/main",
            idempotency_key="register-target",
        )
        self.service.import_snapshot(
            self.registration.target_id,
            expected_source_oid=self.base_oid,
            idempotency_key="import-base",
        )
        self._insert_goal_and_work()
        self._insert_runtime_graph()
        self.manager = WorkspaceManager(
            state_store=self.store,
            path_authority=self.authority,
            repository_service=self.service,
        )
        self.facade = WorkspaceGitFacade(self.manager)

    def _insert_goal_and_work(self) -> None:
        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO goals (
                    id, target_id, base_oid, state, created_at, updated_at
                ) VALUES ('goal-1', ?, ?, 'ACTIVE', ?, ?)
                """,
                (self.registration.target_id, self.base_oid, now, now),
            )
            for ordinal, owner in ((1, "dev_1"), (2, "dev_2")):
                work_id = f"work-{ordinal}"
                revision_id = f"revision-{ordinal}"
                connection.execute(
                    """
                    INSERT INTO work_items (
                        id, goal_id, title, assigned_owner,
                        source_write_scope_json, state, created_at, updated_at
                    ) VALUES (?, 'goal-1', ?, ?, '["src"]', 'ASSIGNED', ?, ?)
                    """,
                    (work_id, f"Work {ordinal}", owner, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO work_revisions (
                        id, work_item_id, revision, owner, base_oid,
                        state, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?, 'CREATED', ?, ?, ?)
                    """,
                    (
                        revision_id,
                        work_id,
                        owner,
                        self.base_oid,
                        f"revision-key-{ordinal}",
                        now,
                        now,
                    ),
                )

    def _insert_runtime_graph(self) -> None:
        now = utc_now()
        digest = "d" * 64
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, goal_id, target_id, base_oid, state,
                    idempotency_key, created_at
                ) VALUES ('run-1', 'goal-1', ?, ?, 'RUNNING', 'run-1-key', ?)
                """,
                (self.registration.target_id, self.base_oid, now),
            )
            connection.execute(
                """
                INSERT INTO logical_capabilities (
                    id, capability_key, state, approval_authority,
                    merge_authority, nested_spawn_authority,
                    idempotency_key, created_at
                ) VALUES (
                    'capability-developer', 'developer', 'ACTIVE', 0, 0, 0,
                    'capability-developer-key', ?
                )
                """,
                (now,),
            )
            for ordinal in (1, 2):
                seat = f"dev_{ordinal}"
                slot = f"slot-dev-{ordinal}"
                worker = f"worker-dev-{ordinal}"
                fingerprint = f"fingerprint-dev-{ordinal}"
                assignment = f"assignment-dev-{ordinal}"
                connection.execute(
                    """
                    INSERT INTO physical_seats (
                        id, seat_key, state, is_merged, idempotency_key, created_at
                    ) VALUES (?, ?, 'ACTIVE', 0, ?, ?)
                    """,
                    (seat, seat, f"{seat}-key", now),
                )
                connection.execute(
                    """
                    INSERT INTO seat_capability_ownerships (
                        physical_seat_id, capability_id, state,
                        idempotency_key, assigned_at
                    ) VALUES (?, 'capability-developer', 'ENABLED', ?, ?)
                    """,
                    (seat, f"ownership-{seat}", now),
                )
                connection.execute(
                    """
                    INSERT INTO runtime_slots (
                        id, slot_key, kind, physical_seat_id, elastic_singleton,
                        state, idempotency_key, created_at
                    ) VALUES (?, ?, 'FIXED', ?, NULL, 'AVAILABLE', ?, ?)
                    """,
                    (slot, seat, seat, f"{slot}-key", now),
                )
                connection.execute(
                    """
                    INSERT INTO worker_identities (
                        id, worker_key, kind, physical_seat_id, state,
                        idempotency_key, created_at
                    ) VALUES (?, ?, 'FIXED', ?, 'ACTIVE', ?, ?)
                    """,
                    (worker, worker, seat, f"{worker}-key", now),
                )
                connection.execute(
                    """
                    INSERT INTO worker_fingerprints (
                        id, worker_id, fingerprint_sha256,
                        runtime_profile_digest, state, idempotency_key,
                        created_at, revoked_at
                    ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, NULL)
                    """,
                    (
                        fingerprint,
                        worker,
                        hashlib.sha256(f"fingerprint-{ordinal}".encode()).hexdigest(),
                        digest,
                        f"{fingerprint}-key",
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO worker_slot_assignments (
                        id, worker_id, worker_fingerprint_id, slot_id, run_id,
                        is_elastic, state, idempotency_key, assigned_at, released_at
                    ) VALUES (?, ?, ?, ?, 'run-1', 0, 'ACTIVE', ?, ?, NULL)
                    """,
                    (assignment, worker, fingerprint, slot, f"{assignment}-key", now),
                )
                connection.execute(
                    """
                    INSERT INTO seat_capability_activations (
                        id, physical_seat_id, capability_id, slot_id,
                        worker_assignment_id, goal_id, run_id, state,
                        idempotency_key, activated_at, released_at
                    ) VALUES (?, ?, 'capability-developer', ?, ?, 'goal-1',
                              'run-1', 'ACTIVE', ?, ?, NULL)
                    """,
                    (
                        f"activation-{seat}",
                        seat,
                        slot,
                        assignment,
                        f"activation-{seat}-key",
                        now,
                    ),
                )
    def allocate(
        self,
        ordinal: int,
        *,
        key: str | None = None,
        lease_seconds: int = 3600,
    ):
        return self.manager.allocate_development(
            goal_id="goal-1",
            work_item_id=f"work-{ordinal}",
            revision=1,
            owner_seat_id=f"dev_{ordinal}",
            target_id=self.registration.target_id,
            base_oid=self.base_oid,
            source_write_scope=("src",),
            generated_write_scope=("build",),
            lease_seconds=lease_seconds,
            idempotency_key=key or f"lease-key-{ordinal}",
        )

    def allocate_v4(self, ordinal: int):
        return self.manager.allocate_developer_workspace(
            self.registration.target_id,
            "run-1",
            f"work-{ordinal}",
            self.base_oid,
            f"slot-dev-{ordinal}",
        )


class WorkspaceLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture(self)

    def test_two_writers_get_distinct_worktrees_and_replay_is_exact(self) -> None:
        first = self.fixture.allocate_v4(1)
        second = self.fixture.allocate_v4(2)
        replay = self.fixture.allocate_v4(1)

        self.assertEqual(first, replay)
        self.assertNotEqual(first.branch_ref, second.branch_ref)
        self.assertNotEqual(first.worktree_path, second.worktree_path)
        self.assertTrue(Path(first.worktree_path).is_dir())
        self.assertTrue(Path(second.worktree_path).is_dir())
        self.assertEqual(
            ("workspaces", "goal-1", "run-1"),
            Path(first.worktree_path).relative_to(
                self.fixture.runtime_root
            ).parts[:3],
        )
        with self.fixture.store.transaction() as connection:
            runtime_leases = connection.execute(
                "SELECT id FROM runtime_leases WHERE state = 'ACTIVE'"
            ).fetchall()
        self.assertEqual(2, len(runtime_leases))
        first_contract = self.fixture.manager.execution_contract(first.lease_id)
        self.assertEqual(Path(first.worktree_path).resolve(), first_contract.cwd)
        self.assertIn(Path(second.worktree_path).resolve(), first_contract.prohibited_roots)
        self.assertIn(
            Path(self.fixture.registration.canonical_worktree_path).resolve(),
            first_contract.prohibited_roots,
        )
        self.assertIn(
            Path(self.fixture.registration.managed_repository_path).resolve(),
            first_contract.prohibited_roots,
        )

    def test_duplicate_writer_and_expired_lease_fail_closed(self) -> None:
        first = self.fixture.allocate(1)
        with self.assertRaises(WorkspaceConflictError):
            self.fixture.allocate(1, key="different-allocation-command")

        future = datetime.now(UTC) + timedelta(hours=2)
        manager = WorkspaceManager(
            state_store=self.fixture.store,
            path_authority=self.fixture.authority,
            repository_service=self.fixture.service,
            clock=lambda: future,
        )
        with self.assertRaises(WorkspaceLeaseStateError):
            manager.execution_contract(first.lease_id)
        findings = manager.reconcile()
        self.assertTrue(
            any(finding.resource_id == first.lease_id for finding in findings)
        )
        with self.fixture.store.transaction() as connection:
            state = connection.execute(
                "SELECT state FROM workspace_leases WHERE id = ?",
                (first.lease_id,),
            ).fetchone()["state"]
        self.assertEqual("EXPIRED", state)

    def test_out_of_scope_and_cross_worktree_symlink_are_rejected(self) -> None:
        first = self.fixture.allocate(1)
        second = self.fixture.allocate(2)
        first_root = Path(first.worktree_path)
        (first_root / "outside.txt").write_text("outside\n", encoding="utf-8")
        status = self.fixture.facade.status(first.lease_id)
        self.assertEqual(("outside.txt",), status.out_of_scope_paths)
        with self.assertRaises(WorkspaceScopeError):
            self.fixture.facade.checkpoint(
                first.lease_id,
                expected_head_oid=self.fixture.base_oid,
                message="unsafe checkpoint",
                idempotency_key="unsafe-checkpoint",
            )
        (first_root / "outside.txt").unlink()

        link = first_root / "src" / "other-worktree"
        try:
            link.symlink_to(Path(second.worktree_path) / "src", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"host cannot create directory symlinks: {exc}")
        status = self.fixture.facade.status(first.lease_id)
        self.assertIn("src/other-worktree", status.out_of_scope_paths)

    def test_stale_branch_head_is_rejected(self) -> None:
        lease = self.fixture.allocate(1)
        worktree = Path(lease.worktree_path)
        (worktree / "src" / "direct.txt").write_text("direct\n", encoding="utf-8")
        run_git(worktree, "config", "user.name", "Unsafe Fixture")
        run_git(worktree, "config", "user.email", "unsafe@example.invalid")
        run_git(worktree, "add", "src/direct.txt")
        run_git(worktree, "commit", "-m", "direct bypass")
        with self.assertRaises(GitCASMismatchError):
            self.fixture.facade.status(lease.lease_id)
        findings = self.fixture.manager.reconcile()
        self.assertTrue(
            any(finding.resource_id == lease.lease_id for finding in findings)
        )


if __name__ == "__main__":
    unittest.main()
