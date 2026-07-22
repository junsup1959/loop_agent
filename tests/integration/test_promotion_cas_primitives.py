from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.agent_team_git import (
    GitCASMismatchError,
    GitRefError,
    GitValidationError,
    ManagedRepositoryService,
    TargetRefAdapter,
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


def optional_hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


class PromotionCASPrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ax-promotion-cas-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "ax-source"
        self.runtime_root = self.root / "ax-runtime"
        self.checkout = self.root / "target"
        for path in (self.source_root, self.runtime_root, self.checkout):
            path.mkdir()

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
        self.adapter = TargetRefAdapter(
            state_store=self.store,
            path_authority=self.authority,
        )
        self.target = self.service.register_target(
            self.checkout,
            source_ref="refs/heads/main",
            idempotency_key="register-promotion-target",
        )
        self.service.import_snapshot(
            self.target.target_id,
            expected_source_oid=self.base_oid,
            idempotency_key="import-promotion-target",
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
            "heads": run_git(
                self.checkout,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/heads",
            ).stdout,
            "index": optional_hash(git_dir / "index"),
            "config": optional_hash(git_dir / "config"),
            "fetch_head": optional_hash(git_dir / "FETCH_HEAD"),
        }

    def all_refs(self) -> dict[str, str]:
        output = run_git(
            self.checkout,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
        ).stdout
        return {
            line.split(" ", 1)[0]: line.split(" ", 1)[1]
            for line in output.splitlines()
            if line
        }

    def create_managed_commit(self, ordinal: int, base_oid: str) -> str:
        path = self.authority.development_worktree(
            "goal-promotion", f"work-{ordinal}", 1
        )
        branch = (
            f"refs/heads/ax/work/goal-promotion/work-{ordinal}/1"
        )
        receipt = self.service.create_disposable_worktree(
            self.target.target_id,
            oid=base_oid,
            path=path,
            branch_ref=branch,
        )
        (path / f"approved-{ordinal}.txt").write_text(
            f"approved {ordinal}\n", encoding="utf-8"
        )
        run_git(path, "config", "user.name", "AX Fixture")
        run_git(path, "config", "user.email", "ax@example.invalid")
        run_git(path, "add", f"approved-{ordinal}.txt")
        run_git(path, "commit", "-m", f"approved {ordinal}")
        approved_oid = run_git(path, "rev-parse", "HEAD").stdout.strip()
        self.service.remove_disposable_worktree(
            receipt,
            expected_oid=approved_oid,
        )
        return approved_oid

    def test_transfer_updates_only_approved_namespace_with_source_and_dest_cas(self) -> None:
        first_oid = self.create_managed_commit(1, self.base_oid)
        second_oid = self.create_managed_commit(2, first_oid)
        destination = "refs/agentic-ax/approved/goal-1/1"
        before_checkout = self.checkout_state()
        before_refs = self.all_refs()
        self.assertNotIn(destination, before_refs)
        missing_in_target = run_git(
            self.checkout,
            "cat-file",
            "-e",
            f"{first_oid}^{{commit}}",
            check=False,
        )
        self.assertNotEqual(0, missing_in_target.returncode)

        first = self.adapter.transfer_object_and_update_namespaced_ref(
            target=self.target,
            approved_oid=first_oid,
            destination_ref=destination,
            expected_source_oid=self.base_oid,
            expected_destination_oid=None,
            idempotency_key="promote-1",
        )
        replay = self.adapter.transfer_object_and_update_namespaced_ref(
            target=self.target,
            approved_oid=first_oid,
            destination_ref=destination,
            expected_source_oid=self.base_oid,
            expected_destination_oid=None,
            idempotency_key="promote-1",
        )
        self.assertEqual(first, replay)
        self.assertEqual(first_oid, first.resulting_destination_oid)

        second = self.adapter.transfer_object_and_update_namespaced_ref(
            target=self.target,
            approved_oid=second_oid,
            destination_ref=destination,
            expected_source_oid=self.base_oid,
            expected_destination_oid=first_oid,
            idempotency_key="promote-2",
        )
        self.assertEqual(first_oid, second.previous_destination_oid)
        self.assertEqual(second_oid, second.resulting_destination_oid)

        after_refs = self.all_refs()
        self.assertEqual(second_oid, after_refs[destination])
        self.assertEqual(
            before_refs,
            {
                ref: oid
                for ref, oid in after_refs.items()
                if ref != destination
            },
        )
        self.assertEqual(before_checkout, self.checkout_state())
        self.assertEqual(
            0,
            run_git(
                self.checkout,
                "cat-file",
                "-e",
                f"{second_oid}^{{commit}}",
            ).returncode,
        )

        with self.assertRaises(IdempotencyConflictError):
            self.adapter.transfer_object_and_update_namespaced_ref(
                target=self.target,
                approved_oid=first_oid,
                destination_ref="refs/agentic-ax/approved/goal-1/other",
                expected_source_oid=self.base_oid,
                expected_destination_oid=None,
                idempotency_key="promote-1",
            )

    def test_stale_source_or_destination_and_arbitrary_refs_fail_closed(self) -> None:
        approved_oid = self.create_managed_commit(1, self.base_oid)
        destination = "refs/agentic-ax/approved/goal-stale/1"
        self.adapter.transfer_object_and_update_namespaced_ref(
            target=self.target,
            approved_oid=approved_oid,
            destination_ref=destination,
            expected_source_oid=self.base_oid,
            expected_destination_oid=None,
            idempotency_key="promote-stale-base",
        )

        refs_before_stale_destination = self.all_refs()
        with self.assertRaises(GitCASMismatchError):
            self.adapter.transfer_object_and_update_namespaced_ref(
                target=self.target,
                approved_oid=approved_oid,
                destination_ref=destination,
                expected_source_oid=self.base_oid,
                expected_destination_oid=None,
                idempotency_key="promote-stale-destination",
            )
        self.assertEqual(refs_before_stale_destination, self.all_refs())

        (self.checkout / "source-advance.txt").write_text(
            "advance\n", encoding="utf-8"
        )
        run_git(self.checkout, "add", "source-advance.txt")
        run_git(self.checkout, "commit", "-m", "source advance")
        refs_before_stale_source = self.all_refs()
        with self.assertRaises(GitCASMismatchError):
            self.adapter.transfer_object_and_update_namespaced_ref(
                target=self.target,
                approved_oid=approved_oid,
                destination_ref="refs/agentic-ax/approved/goal-stale/2",
                expected_source_oid=self.base_oid,
                expected_destination_oid=None,
                idempotency_key="promote-stale-source",
            )
        self.assertEqual(refs_before_stale_source, self.all_refs())

        with self.assertRaises(GitValidationError):
            self.adapter.transfer_object_and_update_namespaced_ref(
                target=self.target,
                approved_oid=approved_oid,
                destination_ref="refs/heads/main",
                expected_source_oid=run_git(
                    self.checkout, "rev-parse", "HEAD"
                ).stdout.strip(),
                expected_destination_oid=None,
                idempotency_key="promote-arbitrary-ref",
            )
        with self.assertRaises(GitValidationError):
            self.adapter.transfer_object_and_update_namespaced_ref(
                target=self.target,
                approved_oid=approved_oid[:12],
                destination_ref="refs/agentic-ax/approved/goal-stale/3",
                expected_source_oid=run_git(
                    self.checkout, "rev-parse", "HEAD"
                ).stdout.strip(),
                expected_destination_oid=None,
                idempotency_key="promote-abbreviated",
            )

        symbolic_destination = (
            "refs/agentic-ax/approved/goal-stale/symbolic"
        )
        run_git(
            self.checkout,
            "symbolic-ref",
            symbolic_destination,
            "refs/heads/main",
        )
        with self.assertRaises(GitRefError):
            self.adapter.transfer_object_and_update_namespaced_ref(
                target=self.target,
                approved_oid=approved_oid,
                destination_ref=symbolic_destination,
                expected_source_oid=run_git(
                    self.checkout, "rev-parse", "HEAD"
                ).stdout.strip(),
                expected_destination_oid=None,
                idempotency_key="promote-symbolic-destination",
            )


if __name__ == "__main__":
    unittest.main()
