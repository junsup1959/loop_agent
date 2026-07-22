from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
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
    BackendCapabilityError,
    BackendExecutionReceipt,
    ExecutionKind,
    McpInvocationBroker,
    OpaqueArtifactRef,
    OutputPolicy,
    RunnerContractError,
    RunnerRequest,
    RunnerResult,
    RuntimeSandboxBinding,
    ToolPolicy,
    TrustedSubprocessTestBackend,
    environment_fingerprint,
    minimal_environment,
)
from agent_team_profiles import PROFESSIONAL_SKILL_ID  # noqa: E402
import tests.unit.test_state_v4_constraints as state_v4  # noqa: E402


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


class _UnconfinedBackend:
    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            backend_id="unconfined-production",
            attestation_id="missing-filesystem-proof",
            production=True,
            trusted_test_fixture=False,
            enforces_cwd=True,
            enforces_writable_roots=False,
            enforces_protected_roots=False,
            enforces_prohibited_roots=False,
            enforces_minimal_environment=True,
            enforces_timeout=True,
            bounds_output=True,
        )

    def execute(self, request: RunnerRequest):  # pragma: no cover - must never execute
        self.calls += 1
        raise AssertionError("fail-closed capability validation did not run")


class _NoCallMcpInvoker:
    def invoke(self, server_name, tool_name, input_payload):  # pragma: no cover
        raise AssertionError("MCP invocation must not run before preflight succeeds")


class _RecordingMcpInvoker:
    def __init__(self) -> None:
        self.calls = []

    def invoke(self, server_name, tool_name, input_payload):
        self.calls.append((server_name, tool_name, dict(input_payload)))
        return {"server": server_name, "tool": tool_name, "consumed": True}


class _MutatingMcpInvoker(_RecordingMcpInvoker):
    def __init__(self, mutation) -> None:
        super().__init__()
        self._mutation = mutation

    def invoke(self, server_name, tool_name, input_payload):
        self._mutation()
        return super().invoke(server_name, tool_name, input_payload)


class _AttestedProductionBackend:
    CAPABILITIES = BackendCapabilities(
        backend_id="attested-production",
        attestation_id="test-production-attestation",
        production=True,
        trusted_test_fixture=False,
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

    def capabilities(self):
        return self.CAPABILITIES

    def execute(self, request):
        self.calls += 1
        started = time.time_ns()
        return BackendExecutionReceipt(
            backend_id=self.CAPABILITIES.backend_id,
            attestation_id=self.CAPABILITIES.attestation_id,
            observed_cwd=request.cwd,
            observed_environment_digest=request.environment_digest,
            enforced_writable_roots=request.writable_roots,
            enforced_protected_roots=request.protected_roots,
            enforced_prohibited_roots=request.prohibited_roots,
            started_at_ns=started,
            finished_at_ns=time.time_ns(),
            exit_code=0,
            timed_out=False,
            stdout="ok\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        )


class RuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.resource = root / "resource"
        self.source = self.resource / "source"
        self.generated = self.resource / "generated"
        self.ephemeral = self.resource / "ephemeral"
        self.activation = root / "activations" / "activation-1"
        self.protected = root / "protected"
        self.prohibited = root / "other-worktree"
        self.results = root / "results"
        for path in (
            self.source,
            self.generated,
            self.ephemeral,
            self.activation,
            self.protected,
            self.prohibited,
            self.results,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_artifacts(self, *, binding_overrides=None) -> tuple[str, str, str, str]:
        profile_path = self.activation / "compiled-profile.json"
        profile = {
            "activation_id": "activation-1",
            "seat_id": "developer-1",
            "role_key": "developer",
            "professional_skill_id": PROFESSIONAL_SKILL_ID,
            "references": [
                {"id": "language-python"},
                {"id": "framework-stdlib"},
                {"id": "domain-runtime"},
                {"id": "quality-testing"},
            ],
        }
        profile_path.write_text(
            json.dumps(profile, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        profile_digest = hashlib.sha256(profile_path.read_bytes()).hexdigest()
        environment = minimal_environment({"PYTHONUTF8": "1"})
        binding = {
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
        }
        binding.update(binding_overrides or {})
        context_path = self.activation / "context.json"
        context = {
            "target_seat_id": "developer-1",
            "target_role": "developer",
            "professional_profile": {
                "skill_id": PROFESSIONAL_SKILL_ID,
                "compiled_profile_ref": str(profile_path),
                "compiled_profile_digest": profile_digest,
            },
            "runtime_binding": binding,
        }
        context_path.write_text(
            json.dumps(context, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return (
            str(profile_path),
            profile_digest,
            str(context_path),
            hashlib.sha256(context_path.read_bytes()).hexdigest(),
        )

    def make_request(self, *, binding_overrides=None) -> RunnerRequest:
        profile_ref, profile_digest, context_ref, context_digest = self._write_artifacts(
            binding_overrides=binding_overrides
        )
        environment = minimal_environment({"PYTHONUTF8": "1"})
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
            generated_scope=("build/**",),
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
            compiled_profile_ref=profile_ref,
            compiled_profile_digest=profile_digest,
            context_ref=context_ref,
            context_digest=context_digest,
            tool_policy=ToolPolicy("python-only", ((sys.executable,),)),
            idempotency_key="run-1",
            environment=environment,
            environment_digest=environment_fingerprint(environment),
            command=(sys.executable, "-c", "print('ok')"),
            stdin="",
            timeout_seconds=5.0,
            output_policy=OutputPolicy(),
        )

    def test_valid_request_is_immutable_and_fully_bound(self) -> None:
        request = self.make_request()
        self.assertEqual(request.subject_oid, OID_B)
        with self.assertRaises(Exception):
            request.cwd = str(self.source)  # type: ignore[misc]

    def test_rejects_abbreviated_oid(self) -> None:
        request = self.make_request()
        with self.assertRaisesRegex(RunnerContractError, "full 40-64"):
            replace(request, head_oid="b" * 12, subject_oid="b" * 12)

    def test_rejects_credentials_and_git_authority_in_environment(self) -> None:
        request = self.make_request()
        with self.assertRaisesRegex(RunnerContractError, "credential or Git authority"):
            replace(
                request,
                environment=request.environment + (("GIT_ASKPASS", "helper"),),
                environment_digest="0" * 64,
            )

    def test_rejects_source_write_overlap(self) -> None:
        request = self.make_request()
        with self.assertRaisesRegex(RunnerContractError, "disjoint from read-only"):
            replace(
                request,
                execution_kind=ExecutionKind.REVIEW,
                workspace_id="sandbox-1",
                lease_id=None,
                sandbox_id="sandbox-1",
                generated_roots=(str(self.source),),
                writable_roots=(str(self.source), str(self.ephemeral)),
            )

    def test_rejects_review_workspace_mismatch(self) -> None:
        request = self.make_request()
        with self.assertRaisesRegex(RunnerContractError, "workspace_id"):
            replace(
                request,
                execution_kind=ExecutionKind.REVIEW,
                lease_id=None,
                sandbox_id="sandbox-1",
            )

    def test_production_backend_without_filesystem_proof_fails_closed(self) -> None:
        backend = _UnconfinedBackend()
        runtime = AgentRuntime(
            backend=backend,
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
        )
        with self.assertRaises(BackendCapabilityError):
            runtime.execute_test_only(self.make_request())
        self.assertEqual(0, backend.calls)

    def test_context_binding_mismatch_is_rejected_before_execution(self) -> None:
        runtime = AgentRuntime(
            backend=TrustedSubprocessTestBackend(),
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
            allow_trusted_test_backend=True,
        )
        with self.assertRaisesRegex(RunnerContractError, "runtime binding mismatch"):
            runtime.execute_test_only(
                self.make_request(binding_overrides={"head_oid": OID_A})
            )

    def test_three_argument_digest_preflight_makes_zero_backend_calls(self) -> None:
        contract_path = self.activation / "activation-contract.json"
        packet_path = self.activation / "activation-packet.md"
        contract_path.write_bytes(b"contract-v1")
        packet_path.write_bytes(b"packet-v1")
        contract_ref = OpaqueArtifactRef(
            str(contract_path), hashlib.sha256(contract_path.read_bytes()).hexdigest()
        )
        packet_ref = OpaqueArtifactRef(
            str(packet_path), hashlib.sha256(packet_path.read_bytes()).hexdigest()
        )
        binding = RuntimeSandboxBinding(
            contract_id="activation-1",
            activation_id="activation-1",
            attempt_id="attempt-1",
            repository_id="repository-1",
            lease_id="lease-1",
            sandbox_binding_id="sandbox-1",
            oid_authority_id="oid-authority-1",
            slot_id="slot-1",
            capability_id="capability-1",
            attestation_digest="c" * 64,
            request=self.make_request(),
        )
        contract_path.write_bytes(b"tampered")
        backend = _UnconfinedBackend()
        durable = state_v4.AxStateV4ConstraintTests(methodName="runTest")
        durable.setUp()
        self.addCleanup(durable.doCleanups)
        runtime = AgentRuntime(
            backend=backend,
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
            state_store=durable.store,
            mcp_broker=McpInvocationBroker(
                state_store=durable.store,
                invoker=_NoCallMcpInvoker(),
            ),
        )
        with self.assertRaisesRegex(RunnerContractError, "contract digest"):
            runtime.execute(contract_ref, packet_ref, binding)
        self.assertEqual(0, backend.calls)

    def test_lease_contract_request_base_oid_mismatch_makes_zero_backend_calls(self) -> None:
        durable = state_v4.AxStateV4ConstraintTests(methodName="runTest")
        durable.setUp()
        self.addCleanup(durable.doCleanups)
        durable._prepare_accepted_contract()

        contract_path = self.activation / "durable-activation-contract.json"
        packet_path = self.activation / "durable-activation-packet.md"
        contract_path.write_text(
            json.dumps({
                "contract_id": "contract-v4",
                "activation_id": "activation-v4",
            }),
            encoding="utf-8",
        )
        packet_path.write_bytes(b"durable-packet")
        contract_digest = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        packet_digest = hashlib.sha256(packet_path.read_bytes()).hexdigest()
        with durable.store.transaction(immediate=True) as connection:
            # Model a pre-hardening v4 database whose immutable rows were
            # already inconsistent; the runner must still fail before backend
            # execution even when artifact digests are otherwise valid.
            connection.execute(
                "DROP TRIGGER trg_activation_contract_identity_immutable"
            )
            connection.execute("DROP TRIGGER trg_runtime_lease_identity_immutable")
            connection.execute(
                """
                UPDATE activation_contracts
                SET contract_digest = ?, packet_digest = ?
                WHERE id = 'contract-v4'
                """,
                (contract_digest, packet_digest),
            )
            connection.execute(
                """
                UPDATE runtime_leases SET base_oid = ?
                WHERE id = 'lease-v4'
                """,
                (OID_B,),
            )
        attempt_id = durable.store.record_contract_attempt(
            contract_id="contract-v4",
            backend="codex",
            model="gpt-5",
            input_digest=state_v4.digest(140),
        )

        request = self.make_request(
            binding_overrides={
                "activation_id": "activation-v4",
                "lease_id": "lease-v4",
            }
        )
        request = replace(
            request,
            activation_id="activation-v4",
            lease_id="lease-v4",
        )
        binding = RuntimeSandboxBinding(
            contract_id="contract-v4",
            activation_id="activation-v4",
            attempt_id=attempt_id,
            repository_id="repository-v4",
            lease_id="lease-v4",
            sandbox_binding_id="sandbox-v4",
            oid_authority_id="oid-authority-v4",
            slot_id="slot-v4",
            capability_id="capability-v4",
            attestation_digest=state_v4.digest(12),
            request=request,
        )
        backend = _UnconfinedBackend()
        runtime = AgentRuntime(
            backend=backend,
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
            state_store=durable.store,
            mcp_broker=McpInvocationBroker(
                state_store=durable.store,
                invoker=_NoCallMcpInvoker(),
            ),
        )
        with self.assertRaisesRegex(
            RunnerContractError, "request OID or runtime identity"
        ):
            runtime.execute(
                OpaqueArtifactRef(str(contract_path), contract_digest),
                OpaqueArtifactRef(str(packet_path), packet_digest),
                binding,
            )
        self.assertEqual(0, backend.calls)

    def test_production_runtime_broker_invokes_and_returns_only_durable_refs(self) -> None:
        subprocess.run(
            ["git", "init"],
            cwd=self.resource,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "runtime@test.invalid"],
            cwd=self.resource,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Runtime Test"],
            cwd=self.resource,
            check=True,
            capture_output=True,
            text=True,
        )
        tracked = self.resource / "tracked.txt"
        tracked.write_text("runtime broker evidence\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked.txt"],
            cwd=self.resource,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "runtime fixture"],
            cwd=self.resource,
            check=True,
            capture_output=True,
            text=True,
        )
        head_oid = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.resource,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        durable = state_v4.AxStateV4ConstraintTests(methodName="runTest")
        durable.setUp()
        self.addCleanup(durable.doCleanups)
        contract_path = self.activation / "broker-contract.json"
        packet_path = self.activation / "broker-packet.md"
        contract_path.write_text(
            json.dumps(
                {"contract_id": "contract-v4", "activation_id": "activation-v4"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        packet_path.write_text("broker packet\n", encoding="utf-8")
        contract_digest = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        packet_digest = hashlib.sha256(packet_path.read_bytes()).hexdigest()
        request = self.make_request(
            binding_overrides={
                "activation_id": "activation-v4",
                "lease_id": "lease-v4",
                "base_oid": head_oid,
                "head_oid": head_oid,
                "subject_oid": head_oid,
            }
        )
        request = replace(
            request,
            activation_id="activation-v4",
            lease_id="lease-v4",
            base_oid=head_oid,
            head_oid=head_oid,
            subject_oid=head_oid,
        )
        with durable.store.transaction(immediate=True) as connection:
            for trigger in (
                "trg_activation_contract_identity_immutable",
                "trg_runtime_lease_identity_immutable",
                "trg_sandbox_binding_identity_immutable",
                "trg_oid_authority_identity_immutable",
            ):
                connection.execute(f"DROP TRIGGER {trigger}")
            connection.execute(
                """
                UPDATE runtime_leases
                SET worktree_path = ?, base_oid = ?, expected_head_oid = ?,
                    write_roots_json = ?, protected_roots_json = ?
                WHERE id = 'lease-v4'
                """,
                (
                    request.cwd,
                    head_oid,
                    head_oid,
                    json.dumps(list(request.writable_roots), separators=(",", ":")),
                    json.dumps(list(request.protected_roots), separators=(",", ":")),
                ),
            )
            connection.execute(
                """
                UPDATE sandbox_bindings
                SET subject_oid = ?, cwd = ?, source_root = ?,
                    writable_roots_json = ?
                WHERE id = 'sandbox-v4'
                """,
                (
                    head_oid,
                    request.cwd,
                    request.source_roots[0],
                    json.dumps(list(request.writable_roots), separators=(",", ":")),
                ),
            )
            connection.execute(
                "UPDATE oid_authorities SET oid = ? WHERE id = 'oid-authority-v4'",
                (head_oid,),
            )
            connection.execute(
                """
                UPDATE activation_contracts
                SET base_oid = ?, subject_oid = ?, contract_digest = ?, packet_digest = ?
                WHERE id = 'contract-v4'
                """,
                (head_oid, head_oid, contract_digest, packet_digest),
            )
            connection.execute(
                "UPDATE activations SET subject_oid = ? WHERE id = 'activation-v4'",
                (head_oid,),
            )
        durable._bind_profile_clause_and_skill()
        snapshot_id = durable.store.record_serena_onboarding_snapshot(
            repo_id="repository-v4",
            source_oid=head_oid,
            policy_digest=state_v4.digest(153),
            memory_bindings=[{
                "name": "conventions",
                "reference": "serena://conventions",
                "sha256": state_v4.digest(154),
            }],
        )
        durable.store.bind_contract_serena_memory(
            contract_id="contract-v4",
            snapshot_id=snapshot_id,
            memory_name="conventions",
            ordinal=0,
        )
        mcp_definition_id = durable.store.register_mcp_definition(
            server_name="serena",
            tool_name="initial_instructions",
            version="broker-v1",
            sha256=state_v4.digest(150),
        )
        durable.store.bind_contract_mcp(
            contract_id="contract-v4",
            mcp_definition_id=mcp_definition_id,
            required_availability=True,
            invocation_required=True,
            trigger_rule="before-project-work",
        )
        durable.store.record_mcp_health_observation(
            mcp_definition_id=mcp_definition_id,
            contract_id="contract-v4",
            status=state_v4.McpHealthStatus.HEALTHY,
            evidence_digest=state_v4.digest(151),
            idempotency_key="runtime-broker-health-v4",
        )
        durable.store.record_contract_admission(
            contract_id="contract-v4", accepted=True, reason_code=None
        )
        attempt_id = durable.store.record_contract_attempt(
            contract_id="contract-v4",
            backend="codex",
            model="gpt-5",
            input_digest=state_v4.digest(152),
        )
        binding = RuntimeSandboxBinding(
            contract_id="contract-v4",
            activation_id="activation-v4",
            attempt_id=attempt_id,
            repository_id="repository-v4",
            lease_id="lease-v4",
            sandbox_binding_id="sandbox-v4",
            oid_authority_id="oid-authority-v4",
            slot_id="slot-v4",
            capability_id="capability-v4",
            attestation_digest=state_v4.digest(12),
            request=request,
        )
        contract_ref = OpaqueArtifactRef(str(contract_path), contract_digest)
        packet_ref = OpaqueArtifactRef(str(packet_path), packet_digest)

        with durable.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE mcp_definitions SET state = 'RETIRED' WHERE id = ?",
                (mcp_definition_id,),
            )
        inactive_invoker = _RecordingMcpInvoker()
        inactive_backend = _AttestedProductionBackend()
        inactive_runtime = AgentRuntime(
            backend=inactive_backend,
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
            state_store=durable.store,
            mcp_broker=McpInvocationBroker(
                state_store=durable.store,
                invoker=inactive_invoker,
            ),
        )
        with self.assertRaisesRegex(RunnerContractError, "not active"):
            inactive_runtime.execute(contract_ref, packet_ref, binding)
        self.assertEqual([], inactive_invoker.calls)
        self.assertEqual(0, inactive_backend.calls)
        with durable.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE mcp_definitions SET state = 'ACTIVE' WHERE id = ?",
                (mcp_definition_id,),
            )

        durable.store.record_mcp_health_observation(
            mcp_definition_id=mcp_definition_id,
            contract_id="contract-v4",
            status=state_v4.McpHealthStatus.UNHEALTHY,
            evidence_digest=state_v4.digest(155),
            idempotency_key="runtime-broker-unhealthy-v4",
        )
        unhealthy_invoker = _RecordingMcpInvoker()
        unhealthy_backend = _AttestedProductionBackend()
        unhealthy_runtime = AgentRuntime(
            backend=unhealthy_backend,
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
            state_store=durable.store,
            mcp_broker=McpInvocationBroker(
                state_store=durable.store,
                invoker=unhealthy_invoker,
            ),
        )
        with self.assertRaisesRegex(RunnerContractError, "healthy evidence"):
            unhealthy_runtime.execute(contract_ref, packet_ref, binding)
        self.assertEqual([], unhealthy_invoker.calls)
        self.assertEqual(0, unhealthy_backend.calls)
        durable.store.record_mcp_health_observation(
            mcp_definition_id=mcp_definition_id,
            contract_id="contract-v4",
            status=state_v4.McpHealthStatus.HEALTHY,
            evidence_digest=state_v4.digest(156),
            idempotency_key="runtime-broker-recovered-v4",
        )

        def release_sandbox_during_mcp() -> None:
            with durable.store.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE sandbox_bindings
                    SET state = 'RELEASED', released_at = ?
                    WHERE id = 'sandbox-v4' AND state = 'ACTIVE'
                    """,
                    (state_v4.NOW,),
                )

        mutating_invoker = _MutatingMcpInvoker(release_sandbox_during_mcp)
        toctou_backend = _AttestedProductionBackend()
        toctou_runtime = AgentRuntime(
            backend=toctou_backend,
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
            state_store=durable.store,
            mcp_broker=McpInvocationBroker(
                state_store=durable.store,
                invoker=mutating_invoker,
            ),
        )
        with self.assertRaisesRegex(RunnerContractError, "sandbox_state"):
            toctou_runtime.execute(contract_ref, packet_ref, binding)
        self.assertEqual(1, len(mutating_invoker.calls))
        self.assertEqual(0, toctou_backend.calls)
        with durable.store.transaction(immediate=True) as connection:
            receipt_count = connection.execute(
                "SELECT COUNT(*) AS count FROM mcp_usage_receipts"
            ).fetchone()["count"]
            connection.execute(
                """
                UPDATE sandbox_bindings
                SET state = 'ACTIVE', released_at = NULL
                WHERE id = 'sandbox-v4'
                """
            )
        self.assertEqual(1, receipt_count)

        invoker = _RecordingMcpInvoker()
        backend = _AttestedProductionBackend()
        runtime = AgentRuntime(
            backend=backend,
            seat_policy_provider=_SeatPolicies(),
            result_root=self.results,
            state_store=durable.store,
            mcp_broker=McpInvocationBroker(
                state_store=durable.store,
                invoker=invoker,
            ),
        )
        result = runtime.execute(
            contract_ref,
            packet_ref,
            binding,
        )
        restored = RunnerResult.from_mapping(result.as_mapping())
        self.assertEqual(1, len(invoker.calls))
        self.assertEqual(1, backend.calls)
        self.assertEqual(1, len(result.trusted_mcp_receipts))
        self.assertEqual(result.trusted_mcp_receipts, restored.trusted_mcp_receipts)
        trusted = result.trusted_mcp_receipts[0]
        self.assertEqual("activation-v4", trusted.activation_id)
        with durable.store.transaction() as connection:
            stored = connection.execute(
                "SELECT * FROM mcp_usage_receipts WHERE id = ?",
                (trusted.receipt_id,),
            ).fetchone()
        self.assertIsNotNone(stored)
        self.assertEqual(trusted.evidence_sha256, stored["output_digest"])


if __name__ == "__main__":
    unittest.main()
