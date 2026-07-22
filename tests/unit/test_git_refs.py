from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.agent_team_git import (
    GitCommandRunner,
    GitRefUpdateReceipt,
    GitValidationError,
    GitWorktreeReceipt,
)


OID_A = "a" * 40


class GitRefContractTests(unittest.TestCase):
    def test_receipts_require_exact_oids_and_managed_namespaces(self) -> None:
        worktree = GitWorktreeReceipt(
            target_id="target-1",
            managed_repository_path="C:/ax/targets/target-1/repository.git",
            worktree_path="C:/ax/goals/goal-1/worktrees/development/work-1/1",
            oid=OID_A,
            branch_ref="refs/heads/ax/work/goal-1/work-1/1",
            intent_id="intent-1",
            idempotency_key="worktree-key",
        )
        self.assertEqual(OID_A, worktree.oid)

        with self.assertRaises(ValueError):
            GitWorktreeReceipt(
                target_id="target-1",
                managed_repository_path="C:/ax/targets/target-1/repository.git",
                worktree_path="C:/ax/worktree",
                oid=OID_A[:12],
                branch_ref=None,
                intent_id="intent-1",
                idempotency_key="worktree-key",
            )
        with self.assertRaises(GitValidationError):
            GitWorktreeReceipt(
                target_id="target-1",
                managed_repository_path="C:/ax/targets/target-1/repository.git",
                worktree_path="C:/ax/worktree",
                oid=OID_A,
                branch_ref="refs/heads/main",
                intent_id="intent-1",
                idempotency_key="worktree-key",
            )

        update = GitRefUpdateReceipt(
            target_id="target-1",
            source_ref="refs/heads/main",
            destination_ref="refs/agentic-ax/approved/goal-1/1",
            approved_oid=OID_A,
            expected_source_oid=OID_A,
            previous_destination_oid=None,
            resulting_destination_oid=OID_A,
            intent_id="intent-2",
            idempotency_key="promotion-key",
        )
        self.assertEqual(
            "refs/agentic-ax/approved/goal-1/1", update.destination_ref
        )
        with self.assertRaises(GitValidationError):
            GitRefUpdateReceipt(
                target_id="target-1",
                source_ref="refs/heads/main",
                destination_ref="refs/heads/main",
                approved_oid=OID_A,
                expected_source_oid=OID_A,
                previous_destination_oid=None,
                resulting_destination_oid=OID_A,
                intent_id="intent-2",
                idempotency_key="promotion-key",
            )

    def test_command_runner_rejects_control_arguments_and_bounds_output(self) -> None:
        runner = GitCommandRunner(max_output_bytes=1024)
        with tempfile.TemporaryDirectory(prefix="ax-git-runner-") as temporary:
            cwd = Path(temporary)
            version = runner.run(["--version"], cwd=cwd)
            self.assertEqual(0, version.returncode)
            self.assertIn("git version", version.stdout.lower())
            with self.assertRaises(GitValidationError):
                runner.run(["status\n--porcelain"], cwd=cwd)


if __name__ == "__main__":
    unittest.main()
