from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.agent_team_git import GitCASMismatchError
from scripts.agent_team_workspace import WorkspaceDirtyError, WorkspaceScopeError
from tests.unit.test_workspace_leases import WorkspaceFixture, run_git


class WorktreeIsolationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture(self)

    def test_scoped_checkpoint_candidate_receipt_replay_and_release(self) -> None:
        lease = self.fixture.allocate(1)
        root = Path(lease.worktree_path)
        (root / "src" / "feature.txt").write_text("feature\n", encoding="utf-8")
        (root / "build" / "generated.txt").parent.mkdir(exist_ok=True)
        (root / "build" / "generated.txt").write_text(
            "generated\n", encoding="utf-8"
        )

        checkpoint = self.fixture.facade.checkpoint(
            lease.lease_id,
            expected_head_oid=self.fixture.base_oid,
            message="Implement scoped feature",
            idempotency_key="checkpoint-work-1",
        )
        replayed_checkpoint = self.fixture.facade.checkpoint(
            lease.lease_id,
            expected_head_oid=self.fixture.base_oid,
            message="Implement scoped feature",
            idempotency_key="checkpoint-work-1",
        )
        self.assertEqual(checkpoint, replayed_checkpoint)
        self.assertEqual(self.fixture.base_oid, checkpoint.expected_previous_oid)
        self.assertNotEqual(self.fixture.base_oid, checkpoint.candidate_oid)

        submission = self.fixture.facade.submit_candidate(
            lease.lease_id,
            expected_head_oid=checkpoint.candidate_oid,
            evidence_ids=("self-test-1",),
            idempotency_key="candidate-work-1",
        )
        replay = self.fixture.facade.submit_candidate(
            lease.lease_id,
            expected_head_oid=checkpoint.candidate_oid,
            evidence_ids=("self-test-1",),
            idempotency_key="candidate-work-1",
        )
        self.assertEqual(submission, replay)
        self.assertEqual(checkpoint.candidate_oid, submission.candidate_oid)
        with self.fixture.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM candidate_submissions
                WHERE id = ?
                """,
                (submission.candidate_id,),
            ).fetchone()
            intent = connection.execute(
                """
                SELECT evidence_json FROM operation_intents
                WHERE idempotency_key = 'candidate-work-1'
                """
            ).fetchone()
        evidence = json.loads(intent["evidence_json"])
        self.assertEqual(submission.candidate_oid, row["candidate_oid"])
        self.assertEqual(lease.base_oid, evidence["base_oid"])
        self.assertEqual(lease.owner, evidence["owner"])
        self.assertEqual(
            ["build/generated.txt", "src/feature.txt"],
            evidence["changed_paths"],
        )
        managed = Path(self.fixture.registration.managed_repository_path)
        pinned = run_git(
            managed,
            f"--git-dir={managed}",
            "show-ref",
            "--verify",
            "--hash",
            "refs/agentic-ax/candidates/goal-1/work-1/1",
        ).stdout.strip()
        self.assertEqual(submission.candidate_oid, pinned)

        released = self.fixture.manager.release(
            lease.lease_id, expected_owner="dev_1"
        )
        replayed_release = self.fixture.manager.release(
            lease.lease_id, expected_owner="dev_1"
        )
        self.assertEqual(released.lease_id, replayed_release.lease_id)
        self.assertFalse(Path(lease.worktree_path).exists())
        self.assertEqual(submission.candidate_oid, released.final_oid)

    def test_candidate_requires_clean_current_head_and_scope(self) -> None:
        lease = self.fixture.allocate(1)
        root = Path(lease.worktree_path)
        (root / "src" / "feature.txt").write_text("feature\n", encoding="utf-8")
        with self.assertRaises(WorkspaceDirtyError):
            self.fixture.facade.submit_candidate(
                lease.lease_id,
                expected_head_oid=self.fixture.base_oid,
                evidence_ids=(),
                idempotency_key="dirty-candidate",
            )
        (root / "not-authorized.txt").write_text("bad\n", encoding="utf-8")
        with self.assertRaises(WorkspaceScopeError):
            self.fixture.facade.checkpoint(
                lease.lease_id,
                expected_head_oid=self.fixture.base_oid,
                message="bad",
                idempotency_key="out-of-scope-checkpoint",
            )
        with self.assertRaises(GitCASMismatchError):
            self.fixture.facade.checkpoint(
                lease.lease_id,
                expected_head_oid="f" * 40,
                message="stale",
                idempotency_key="stale-checkpoint",
            )


if __name__ == "__main__":
    unittest.main()
