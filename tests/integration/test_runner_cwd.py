from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_team_runtime import (  # noqa: E402
    AgentRuntime,
    BackendCapabilities,
    BackendExecutionReceipt,
    ExecutionKind,
    OutputPolicy,
    RunnerReplayConflictError,
    RunnerRequest,
    ToolPolicy,
    TrustedSubprocessTestBackend,
    environment_fingerprint,
    minimal_environment,
)
from agent_team_profiles import PROFESSIONAL_SKILL_ID  # noqa: E402


OID_A = "a" * 40
OID_B = "b" * 40


class _SeatPolicies:
    def resolve(self, seat_id: str) -> dict[str, object]:
        return {
            "seat_id": seat_id,
            "role_key": "developer",
            "model": "test-model",
            "model_reasoning_effort": "low",
            "professional_skill_id": PROFESSIONAL_SKILL_ID,
            "service_identity": False,
            "dynamic_confinement_required": True,
        }


class _AttestedFakeBackend:
    """Non-production fixture that records roots while using a subprocess."""

    CAPABILITIES = BackendCapabilities(
        backend_id="attested-fake",
        attestation_id="test-attestation",
        production=False,
        trusted_test_fixture=True,
        enforces_cwd=True,
        enforces_writable_roots=True,
        enforces_protected_roots=True,
        enforces_prohibited_roots=True,
        enforces_minimal_environment=True,
        enforces_timeout=True,
        bounds_output=True,
    )

    def __init__(self) -> None:
        self.calls = 0
        self._process = TrustedSubprocessTestBackend()

    def capabilities(self) -> BackendCapabilities:
        return self.CAPABILITIES

    def execute(self, request: RunnerRequest) -> BackendExecutionReceipt:
        self.calls += 1
        receipt = self._process.execute(request)
        return replace(
            receipt,
            backend_id=self.CAPABILITIES.backend_id,
            attestation_id=self.CAPABILITIES.attestation_id,
            enforced_writable_roots=request.writable_roots,
            enforced_protected_roots=request.protected_roots,
            enforced_prohibited_roots=request.prohibited_roots,
        )


class RunnerCwdIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.resource = root / "assigned-worktree"
        self.source = self.resource / "src"
        self.generated = self.resource / "generated"
        self.ephemeral = self.resource / ".runtime"
        self.activation = root / "activation"
        self.protected = root / "user-checkout"
        self.prohibited = root / "other-worktree"
        self.results = root / "results"
        for path in (
            self.source,
            self.generated,
            self.ephemeral,
            self.activation,
            self.protected,
            self.prohibited,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _request(
        self,
        *,
        command: tuple[str, ...] | None = None,
        idempotency_key: str = "execution-1",
        timeout: float = 5.0,
        output_policy: OutputPolicy | None = None,
    ) -> RunnerRequest:
        environment = minimal_environment({"PYTHONUTF8": "1"})
        profile_path = self.activation / "compiled-profile.json"
        profile_path.write_text(
            json.dumps(
                {
                    "activation_id": "activation-1",
                    "seat_id": "developer-1",
                    "role_key": "developer",
                    "professional_skill_id": PROFESSIONAL_SKILL_ID,
                    "references": [
                        {"id": "language"},
                        {"id": "framework"},
                        {"id": "domain"},
                        {"id": "quality"},
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        profile_digest = hashlib.sha256(profile_path.read_bytes()).hexdigest()
        context_path = self.activation / "context.json"
        context_path.write_text(
            json.dumps(
                {
                    "target_seat_id": "developer-1",
                    "target_role": "developer",
                    "professional_profile": {
                        "skill_id": PROFESSIONAL_SKILL_ID,
                        "compiled_profile_ref": str(profile_path),
                        "compiled_profile_digest": profile_digest,
                    },
                    "runtime_binding": {
                        "activation_id": "activation-1",
                        "workspace_id": "workspace-1",
                        "lease_id": "lease-1",
                        "sandbox_id": None,
                        "execution_kind": "development",
                        "base_oid": OID_A,
                        "head_oid": OID_B,
                        "subject_oid": OID_B,
                        "seat_id": "developer-1",
                        "role_key": "developer",
                        "cwd": str(self.resource),
                        "source_roots": [str(self.source)],
                        "generated_roots": [str(self.generated)],
                        "ephemeral_writable_roots": [str(self.ephemeral)],
                        "protected_roots": [str(self.protected)],
                        "prohibited_roots": [str(self.prohibited)],
                        "environment_digest": environment_fingerprint(environment),
                        "tool_policy_id": "python-only",
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return RunnerRequest(
            target_id="target-1",
            goal_id="goal-1",
            work_item_id="work-1",
            revision=1,
            activation_id="activation-1",
            workspace_id="workspace-1",
            lease_id="lease-1",
            sandbox_id=None,
            execution_kind=ExecutionKind.DEVELOPMENT,
            base_oid=OID_A,
            head_oid=OID_B,
            subject_oid=OID_B,
            seat_id="developer-1",
            role_key="developer",
            gate_id="implementation",
            model="test-model",
            model_reasoning_effort="low",
            cwd=str(self.resource),
            resource_root=str(self.resource),
            activation_root=str(self.activation),
            source_scope=("src/**",),
            generated_scope=("generated/**",),
            source_roots=(str(self.source),),
            generated_roots=(str(self.generated),),
            ephemeral_writable_roots=(str(self.ephemeral),),
            writable_roots=(
                str(self.source),
                str(self.generated),
                str(self.ephemeral),
            ),
            protected_roots=(str(self.protected),),
            prohibited_roots=(str(self.prohibited),),
            professional_skill_id=PROFESSIONAL_SKILL_ID,
            compiled_profile_ref=str(profile_path),
            compiled_profile_digest=profile_digest,
            context_ref=str(context_path),
            context_digest=hashlib.sha256(context_path.read_bytes()).hexdigest(),
            tool_policy=ToolPolicy("python-only", ((sys.executable,),)),
            idempotency_key=idempotency_key,
            environment=environment,
            environment_digest=environment_fingerprint(environment),
            command=command
            or (
                sys.executable,
                "-c",
                "import json,os; print(json.dumps({'cwd':os.getcwd(),"
                "'secret':os.environ.get('AGENT_TEAM_TEST_SECRET')}))",
            ),
            stdin="",
            timeout_seconds=timeout,
            output_policy=output_policy or OutputPolicy(),
        )

    def test_real_subprocess_receives_exact_cwd_and_no_ambient_secret(self) -> None:
        prior = os.environ.get("AGENT_TEAM_TEST_SECRET")
        os.environ["AGENT_TEAM_TEST_SECRET"] = "must-not-cross-boundary"
        try:
            runtime = AgentRuntime(
                backend=TrustedSubprocessTestBackend(),
                seat_policy_provider=_SeatPolicies(),
                result_root=self.results,
                allow_trusted_test_backend=True,
            )
            result = runtime.execute_test_only(self._request())
        finally:
            if prior is None:
                os.environ.pop("AGENT_TEAM_TEST_SECRET", None)
            else:
                os.environ["AGENT_TEAM_TEST_SECRET"] = prior
        observed = json.loads(result.receipt.stdout)
        self.assertEqual(Path(observed["cwd"]).resolve(), self.resource)
        self.assertIsNone(observed["secret"])

    def test_trusted_backend_receipt_records_exact_authority(self) -> None:
        backend = _AttestedFakeBackend()
        result = AgentRuntime(
            backend=backend,
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
            allow_trusted_test_backend=True,
        ).execute_test_only(self._request())
        self.assertEqual(result.receipt.enforced_writable_roots, result.receipt.enforced_writable_roots)
        self.assertEqual(backend.calls, 1)

    def test_replay_is_deterministic_and_does_not_execute_twice(self) -> None:
        backend = _AttestedFakeBackend()
        runtime = AgentRuntime(
            backend=backend,
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
            allow_trusted_test_backend=True,
        )
        request = self._request()
        first = runtime.execute_test_only(request)
        second = runtime.execute_test_only(request)
        self.assertEqual(first.result_id, second.result_id)
        self.assertEqual(first.artifact_ref, second.artifact_ref)
        self.assertTrue(second.replayed)
        self.assertEqual(backend.calls, 1)

    def test_idempotency_key_conflict_is_rejected(self) -> None:
        backend = _AttestedFakeBackend()
        runtime = AgentRuntime(
            backend=backend,
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
            allow_trusted_test_backend=True,
        )
        request = self._request()
        runtime.execute_test_only(request)
        changed = replace(
            request,
            command=(sys.executable, "-c", "print('different')"),
        )
        with self.assertRaises(RunnerReplayConflictError):
            runtime.execute_test_only(changed)

    def test_timeout_and_output_redaction_are_enforced(self) -> None:
        runtime = AgentRuntime(
            backend=TrustedSubprocessTestBackend(),
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
            allow_trusted_test_backend=True,
        )
        timeout_result = runtime.execute_test_only(
            self._request(
                command=(sys.executable, "-c", "import time; time.sleep(2)"),
                idempotency_key="timeout-1",
                timeout=0.1,
            )
        )
        self.assertTrue(timeout_result.receipt.timed_out)
        redacted = runtime.execute_test_only(
            self._request(
                command=(sys.executable, "-c", "print('secret-value' * 20)"),
                idempotency_key="redaction-1",
                output_policy=OutputPolicy(
                    stdout_limit_bytes=80,
                    stderr_limit_bytes=80,
                    redaction_literals=("secret-value",),
                ),
            )
        )
        self.assertNotIn("secret-value", redacted.receipt.stdout)
        self.assertTrue(redacted.receipt.stdout_truncated)


if __name__ == "__main__":
    unittest.main()
