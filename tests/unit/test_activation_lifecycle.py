from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.agent_team_activation import (
    ActivationIdentityError,
    ActivationManager,
    ActivationQuarantinedError,
    ActivationStateError,
    ProcessIdentity,
    ProcessIdentityState,
)
from scripts.agent_team_domain import ActivationSpec, ActivationState
from scripts.agent_team_paths import AxPathAuthority
from scripts.agent_team_profiles import PROFESSIONAL_SKILL_ID
from scripts.agent_team_state import AxStateStore, utc_now


OID = "a" * 40


class FakeProcessController:
    def __init__(self, *, mismatch: bool = False) -> None:
        self.mismatch = mismatch
        self.running = True
        self.terminated: list[int] = []

    def capture(self, pid: int) -> ProcessIdentity:
        return ProcessIdentity(pid=pid, identity_token=f"token:{pid}:created")

    def inspect(self, identity: ProcessIdentity) -> ProcessIdentityState:
        if not self.running:
            return ProcessIdentityState.EXITED
        if self.mismatch:
            return ProcessIdentityState.RUNNING_MISMATCH
        return ProcessIdentityState.RUNNING_MATCH

    def terminate(self, identity: ProcessIdentity) -> None:
        if self.mismatch:
            raise AssertionError("mismatched process must never be terminated")
        self.terminated.append(identity.pid)
        self.running = False


class FakeResourceController:
    def __init__(self, *, fail_release: bool = False) -> None:
        self.fail_release = fail_release
        self.released: list[str] = []
        self.quarantined: list[tuple[str, str]] = []
        self.cleaned: list[str] = []

    def release(self, activation) -> None:
        if self.fail_release:
            raise RuntimeError("injected resource release failure")
        self.released.append(activation.activation_id)

    def quarantine(self, activation, reason: str) -> None:
        self.quarantined.append((activation.activation_id, reason))

    def recovery_cleanup(self, activation) -> None:
        self.cleaned.append(activation.activation_id)


class FailingProfileController:
    def revoke(self, **kwargs) -> None:
        raise RuntimeError("injected profile revoke failure")


class ActivationFixture:
    def __init__(
        self,
        case: unittest.TestCase,
        *,
        process_controller=None,
        resource_controller=None,
        profile_controller=None,
    ) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ax-activation-")
        case.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "ax-source"
        self.runtime_root = self.root / "ax-runtime"
        self.source_root.mkdir()
        self.runtime_root.mkdir()
        self.authority = AxPathAuthority(self.source_root, self.runtime_root)
        self.store = AxStateStore(self.authority.state_database)
        self.store.initialize()
        self._insert_run()
        self.manager = ActivationManager(
            state_store=self.store,
            path_authority=self.authority,
            process_controller=process_controller,
            resource_controller=resource_controller,
            profile_controller=profile_controller,
        )

    def _insert_run(self) -> None:
        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO targets (
                    id, canonical_checkout_path, git_common_dir, source_ref,
                    observed_source_oid, state, created_at, updated_at
                ) VALUES (
                    'target-1', 'C:/fixture-target', 'C:/fixture-target/.git',
                    'refs/heads/main', ?, 'ACTIVE', ?, ?
                )
                """,
                (OID, now, now),
            )
            connection.execute(
                """
                INSERT INTO goals (
                    id, target_id, base_oid, state, created_at, updated_at
                ) VALUES ('goal-1', 'target-1', ?, 'ACTIVE', ?, ?)
                """,
                (OID, now, now),
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
                (OID, now),
            )

    def prepare(
        self,
        activation_id: str,
        workspace_id: str,
    ):
        profile_root = self.authority.activation_root(
            "goal-1", activation_id
        )
        profile_root.mkdir(parents=True)
        profile_path = profile_root / "professional-profile.json"
        profile_path.write_bytes(b'{"profile":"fixture"}\n')
        digest = hashlib.sha256(profile_path.read_bytes()).hexdigest()
        spec = ActivationSpec(
            activation_id=activation_id,
            subject_oid=OID,
            workspace_or_sandbox_id=workspace_id,
            professional_skill_id=PROFESSIONAL_SKILL_ID,
            compiled_profile_ref=str(profile_path.resolve()),
            compiled_profile_digest=digest,
            allowed_tools=("shell",),
            commands=("python -m unittest",),
        )
        created = self.manager.create(
            spec,
            target_id="target-1",
            goal_id="goal-1",
            run_id="run-1",
            role="qa_sdet",
            gate_or_task="quality",
            idempotency_key=f"create:{activation_id}",
        )
        self.assert_state(created.state, ActivationState.CREATED)
        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO profile_bindings (
                    activation_id, professional_skill_id,
                    compiled_profile_ref, compiled_profile_digest,
                    state, bound_at
                ) VALUES (?, ?, ?, ?, 'BOUND', ?)
                """,
                (
                    activation_id,
                    PROFESSIONAL_SKILL_ID,
                    str(profile_path.resolve()),
                    digest,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, target_id, goal_id, kind, path, subject_oid,
                    state, created_at, updated_at
                ) VALUES (?, 'target-1', 'goal-1', 'REVIEW', ?, ?,
                          'ACTIVE', ?, ?)
                """,
                (
                    workspace_id,
                    str(self.authority.review_sandbox(activation_id)),
                    OID,
                    now,
                    now,
                ),
            )
        bound = self.manager.bind_profile(activation_id, digest)
        self.assert_state(bound.state, ActivationState.PROFILE_BOUND)
        workspace_bound = self.manager.bind_workspace(
            activation_id, workspace_id
        )
        self.assert_state(
            workspace_bound.state, ActivationState.WORKSPACE_BOUND
        )
        return spec, profile_path

    @staticmethod
    def assert_state(actual, expected) -> None:
        if actual is not expected:
            raise AssertionError(f"state is {actual}, expected {expected}")


class ActivationLifecycleTests(unittest.TestCase):
    def test_exact_lifecycle_persists_result_before_revocation(self) -> None:
        process = FakeProcessController()
        resources = FakeResourceController()
        fixture = ActivationFixture(
            self,
            process_controller=process,
            resource_controller=resources,
        )
        _, profile_path = fixture.prepare("activation-1", "sandbox-1")
        running = fixture.manager.mark_running("activation-1", 4242)
        self.assertEqual(ActivationState.RUNNING, running.state)
        persisted = fixture.manager.persist_result(
            "activation-1", {"exit_code": 0, "evidence": ["test-1"]}
        )
        self.assertEqual(ActivationState.RESULT_PERSISTED, persisted.state)
        terminated = fixture.manager.revoke_and_terminate("activation-1")
        self.assertEqual(ActivationState.TERMINATED, terminated.state)
        self.assertFalse(profile_path.exists())
        self.assertEqual([4242], process.terminated)
        self.assertEqual(["activation-1"], resources.released)
        replay = fixture.manager.revoke_and_terminate("activation-1")
        self.assertEqual(terminated, replay)
        receipt = fixture.manager.teardown_receipt("activation-1")
        self.assertTrue(receipt.profile_revoked)
        self.assertTrue(receipt.resources_released)
        with fixture.store.transaction() as connection:
            state = connection.execute(
                """
                SELECT state FROM profile_bindings
                WHERE activation_id = 'activation-1'
                """
            ).fetchone()["state"]
            result = connection.execute(
                """
                SELECT result_json FROM activations
                WHERE id = 'activation-1'
                """
            ).fetchone()["result_json"]
        self.assertEqual("REVOKED", state)
        self.assertIn('"exit_code":0', result)

    def test_profile_revoke_failure_quarantines_and_blocks_reuse(self) -> None:
        resources = FakeResourceController()
        fixture = ActivationFixture(
            self,
            resource_controller=resources,
            profile_controller=FailingProfileController(),
        )
        fixture.prepare("activation-profile-fail", "sandbox-profile-fail")
        fixture.manager.mark_running("activation-profile-fail", None)
        fixture.manager.persist_result("activation-profile-fail", {"ok": True})
        result = fixture.manager.revoke_and_terminate(
            "activation-profile-fail"
        )
        self.assertEqual(ActivationState.QUARANTINED, result.state)
        self.assertTrue(resources.quarantined)
        with self.assertRaises(ActivationStateError):
            fixture.manager.mark_running("activation-profile-fail", None)
        cleaned = fixture.manager.recovery_cleaned("activation-profile-fail")
        self.assertEqual(ActivationState.RECOVERY_CLEANED, cleaned.state)
        self.assertEqual(["activation-profile-fail"], resources.cleaned)

    def test_pid_identity_mismatch_never_terminates_unrelated_process(self) -> None:
        process = FakeProcessController(mismatch=True)
        resources = FakeResourceController()
        fixture = ActivationFixture(
            self,
            process_controller=process,
            resource_controller=resources,
        )
        fixture.prepare("activation-pid-mismatch", "sandbox-pid-mismatch")
        fixture.manager.mark_running("activation-pid-mismatch", 8888)
        fixture.manager.persist_result("activation-pid-mismatch", {"ok": True})
        result = fixture.manager.revoke_and_terminate(
            "activation-pid-mismatch"
        )
        self.assertEqual(ActivationState.QUARANTINED, result.state)
        self.assertEqual([], process.terminated)
        self.assertTrue(resources.quarantined)

    def test_resource_release_failure_quarantines_after_profile_revoke(self) -> None:
        resources = FakeResourceController(fail_release=True)
        fixture = ActivationFixture(self, resource_controller=resources)
        _, profile_path = fixture.prepare(
            "activation-resource-fail", "sandbox-resource-fail"
        )
        fixture.manager.mark_running("activation-resource-fail", None)
        fixture.manager.persist_result("activation-resource-fail", {"ok": True})
        result = fixture.manager.revoke_and_terminate(
            "activation-resource-fail"
        )
        self.assertEqual(ActivationState.QUARANTINED, result.state)
        self.assertFalse(profile_path.exists())
        with fixture.store.transaction() as connection:
            binding = connection.execute(
                """
                SELECT state FROM profile_bindings
                WHERE activation_id = 'activation-resource-fail'
                """
            ).fetchone()["state"]
        self.assertEqual("REVOKED", binding)


if __name__ == "__main__":
    unittest.main()
