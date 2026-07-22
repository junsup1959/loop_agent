from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.agent_team_git import (
    GitCASMismatchError,
    GitImmutableRefError,
    GitObjectError,
    GitValidationError,
    GitWorktreeError,
    ManagedRepositoryService,
    TargetRegistrationConflictError,
)
from scripts.agent_team_paths import AxPathAuthority
from scripts.agent_team_state import AxStateStore, IdempotencyConflictError


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
        check=False,
        shell=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"temporary fixture Git failed ({result.returncode}): "
            f"{' '.join(arguments)}\n{result.stderr}"
        )
    return result


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ManagedRepositoryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ax-managed-git-")
        self.addCleanup(self.temporary.cleanup)
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
        run_git(self.checkout, "add", "README.md")
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

    def register(self, *, key: str = "register-target-1"):
        return self.service.register_target(
            self.checkout,
            source_ref="refs/heads/main",
            idempotency_key=key,
        )

    def checkout_state(self) -> dict[str, str | None]:
        git_dir = self.checkout / ".git"
        return {
            "head": run_git(self.checkout, "rev-parse", "HEAD").stdout,
            "symbolic_head": run_git(
                self.checkout, "symbolic-ref", "HEAD"
            ).stdout,
            "status": run_git(
                self.checkout,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ).stdout,
            "head_refs": run_git(
                self.checkout,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/heads",
            ).stdout,
            "index_sha256": file_sha256(git_dir / "index"),
            "config_sha256": file_sha256(git_dir / "config"),
        }

    def test_two_linked_checkouts_register_as_one_target_and_conflicts_fail(self) -> None:
        registration = self.register()
        replay = self.register()
        self.assertEqual(registration, replay)

        linked = self.root / "linked"
        run_git(
            self.checkout,
            "worktree",
            "add",
            "-b",
            "linked",
            str(linked),
            self.base_oid,
        )
        from_linked = self.service.register_target(
            linked,
            source_ref="refs/heads/main",
            idempotency_key="register-linked",
        )
        self.assertEqual(registration.target_id, from_linked.target_id)
        self.assertEqual(registration.git_common_dir, from_linked.git_common_dir)

        with self.store.transaction() as connection:
            target_count = connection.execute(
                "SELECT COUNT(*) AS count FROM targets"
            ).fetchone()["count"]
            repository_count = connection.execute(
                "SELECT COUNT(*) AS count FROM managed_repositories"
            ).fetchone()["count"]
        self.assertEqual(1, target_count)
        self.assertEqual(1, repository_count)

        with self.assertRaises(TargetRegistrationConflictError):
            self.service.register_target(
                linked,
                source_ref="refs/heads/main",
                requested_target_id="different-target",
                idempotency_key="register-linked-conflict",
            )
        with self.assertRaises(IdempotencyConflictError):
            self.service.register_target(
                self.checkout,
                source_ref="refs/heads/linked",
                idempotency_key="register-target-1",
            )
        with self.assertRaises(GitValidationError):
            self.service.register_target(
                self.checkout,
                source_ref="main",
                idempotency_key="unsafe-source-ref",
            )
        with self.assertRaises(GitValidationError):
            self.service.register_target(
                self.checkout,
                source_ref="refs/heads/main",
                requested_target_id="unsafe:target",
                idempotency_key="unsafe-target-id",
            )

        other = self.root / "other-target"
        other.mkdir()
        run_git(other, "init", "--initial-branch=main")
        run_git(other, "config", "user.name", "AX Fixture")
        run_git(other, "config", "user.email", "ax@example.invalid")
        (other / "README.md").write_text("other\n", encoding="utf-8")
        run_git(other, "add", "README.md")
        run_git(other, "commit", "-m", "other")
        with self.assertRaises(TargetRegistrationConflictError):
            self.service.register_target(
                other,
                source_ref="refs/heads/main",
                requested_target_id=registration.target_id,
                idempotency_key="duplicate-target-id",
            )

    def test_import_and_resync_preserve_user_checkout_and_retain_snapshots(self) -> None:
        registration = self.register()
        (self.checkout / "untracked.keep").write_text(
            "do not touch\n", encoding="utf-8"
        )
        before_import = self.checkout_state()

        snapshot = self.service.import_snapshot(
            registration.target_id,
            expected_source_oid=self.base_oid,
            idempotency_key="snapshot-1",
        )
        replay = self.service.import_snapshot(
            registration.target_id,
            expected_source_oid=self.base_oid,
            idempotency_key="snapshot-1",
        )
        self.assertEqual(snapshot, replay)
        self.assertEqual(self.base_oid, snapshot.imported_oid)
        self.assertEqual(before_import, self.checkout_state())
        self.assertEqual(
            self.base_oid,
            self.service.resolve_commit(registration.target_id, self.base_oid),
        )

        managed = Path(registration.managed_repository_path)
        imported_refs = run_git(
            managed,
            f"--git-dir={managed}",
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/agentic-ax/imported",
        ).stdout.splitlines()
        self.assertEqual(1, len(imported_refs))
        self.assertTrue(imported_refs[0].endswith(self.base_oid))

        (self.checkout / "second.txt").write_text("second\n", encoding="utf-8")
        run_git(self.checkout, "add", "second.txt")
        run_git(self.checkout, "commit", "-m", "second")
        second_oid = run_git(self.checkout, "rev-parse", "HEAD").stdout.strip()
        before_resync = self.checkout_state()

        second = self.service.resync_target(
            registration.target_id,
            expected_previous_snapshot_oid=self.base_oid,
            idempotency_key="snapshot-2",
        )
        self.assertEqual(second_oid, second.imported_oid)
        self.assertEqual(before_resync, self.checkout_state())
        self.assertEqual(
            second_oid,
            self.service.resolve_commit(registration.target_id, second_oid),
        )
        with self.store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT imported_oid, evidence_ref
                FROM repository_snapshots
                WHERE target_id = ?
                ORDER BY created_at
                """,
                (registration.target_id,),
            ).fetchall()
        self.assertEqual([self.base_oid, second_oid], [row["imported_oid"] for row in rows])
        self.assertEqual(2, len({row["evidence_ref"] for row in rows}))
        first_evidence_ref = rows[0]["evidence_ref"]
        self.service._ensure_immutable_ref(  # Intentional boundary contract probe.
            managed,
            first_evidence_ref,
            self.base_oid,
        )
        with self.assertRaises(GitImmutableRefError):
            self.service._ensure_immutable_ref(
                managed,
                first_evidence_ref,
                second_oid,
            )

        with self.assertRaises(GitCASMismatchError):
            self.service.resync_target(
                registration.target_id,
                expected_previous_snapshot_oid=self.base_oid,
                idempotency_key="snapshot-stale",
            )

    def test_exact_commit_resolution_rejects_abbreviations_and_non_commits(self) -> None:
        registration = self.register()
        self.service.import_snapshot(
            registration.target_id,
            expected_source_oid=self.base_oid,
            idempotency_key="snapshot-exact",
        )
        self.assertEqual(
            self.base_oid,
            self.service.resolve_commit(registration.target_id, self.base_oid),
        )
        with self.assertRaises(GitValidationError):
            self.service.resolve_commit(
                registration.target_id, self.base_oid[:12]
            )

        blob_oid = run_git(
            self.checkout, "rev-parse", f"{self.base_oid}:README.md"
        ).stdout.strip()
        with self.assertRaises(GitObjectError):
            self.service.resolve_commit(registration.target_id, blob_oid)

    def test_disposable_worktree_replay_branch_guards_and_removal(self) -> None:
        registration = self.register()
        self.service.import_snapshot(
            registration.target_id,
            expected_source_oid=self.base_oid,
            idempotency_key="snapshot-worktree",
        )
        detached_path = self.authority.integration_worktree(
            "goal-1", "attempt-1"
        )
        receipt = self.service.create_disposable_worktree(
            registration.target_id,
            oid=self.base_oid,
            path=detached_path,
            branch_ref=None,
        )
        replay = self.service.create_disposable_worktree(
            registration.target_id,
            oid=self.base_oid,
            path=detached_path,
            branch_ref=None,
        )
        self.assertEqual(receipt, replay)
        self.assertEqual(
            self.base_oid,
            run_git(detached_path, "rev-parse", "HEAD").stdout.strip(),
        )
        symbolic = run_git(
            detached_path, "symbolic-ref", "--quiet", "HEAD", check=False
        )
        self.assertNotEqual(0, symbolic.returncode)

        forged = replace(
            receipt,
            worktree_path=str(
                self.authority.integration_worktree("goal-1", "attempt-forged")
            ),
        )
        with self.assertRaises(GitWorktreeError):
            self.service.remove_disposable_worktree(
                forged,
                expected_oid=self.base_oid,
            )
        with self.assertRaises(GitCASMismatchError):
            self.service.remove_disposable_worktree(
                receipt,
                expected_oid="f" * 40,
            )
        self.assertTrue(detached_path.is_dir())
        self.service.remove_disposable_worktree(
            receipt,
            expected_oid=self.base_oid,
        )
        self.service.remove_disposable_worktree(
            receipt,
            expected_oid=self.base_oid,
        )
        self.assertFalse(detached_path.exists())
        with self.assertRaises(GitCASMismatchError):
            self.service.remove_disposable_worktree(
                receipt,
                expected_oid="e" * 40,
            )

        with self.assertRaises(GitValidationError):
            self.service.create_disposable_worktree(
                registration.target_id,
                oid=self.base_oid,
                path=self.root / "escaped",
                branch_ref=None,
            )
        with self.assertRaises(GitValidationError):
            self.service.create_disposable_worktree(
                registration.target_id,
                oid=self.base_oid,
                path=self.authority.development_worktree(
                    "goal-1", "work-invalid", 1
                ),
                branch_ref="refs/heads/main",
            )

        branch_path = self.authority.development_worktree(
            "goal-1", "work-1", 1
        )
        branch_ref = "refs/heads/ax/work/goal-1/work-1/1"
        branch_receipt = self.service.create_disposable_worktree(
            registration.target_id,
            oid=self.base_oid,
            path=branch_path,
            branch_ref=branch_ref,
        )
        self.assertEqual(
            branch_ref,
            run_git(branch_path, "symbolic-ref", "HEAD").stdout.strip(),
        )
        (branch_path / "branch-change.txt").write_text(
            "branch\n", encoding="utf-8"
        )
        run_git(branch_path, "config", "user.name", "AX Fixture")
        run_git(branch_path, "config", "user.email", "ax@example.invalid")
        run_git(branch_path, "add", "branch-change.txt")
        run_git(branch_path, "commit", "-m", "branch change")
        branch_head = run_git(branch_path, "rev-parse", "HEAD").stdout.strip()
        self.service.remove_disposable_worktree(
            branch_receipt,
            expected_oid=branch_head,
        )
        managed = Path(registration.managed_repository_path)
        self.assertEqual(
            branch_head,
            run_git(
                managed,
                f"--git-dir={managed}",
                "show-ref",
                "--verify",
                "--hash",
                branch_ref,
            ).stdout.strip(),
        )


if __name__ == "__main__":
    unittest.main()
