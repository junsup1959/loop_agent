from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import agent_team_taskflow as taskflow
from scripts.agent_team_domain import McpHealthStatus
from scripts.agent_team_runtime import (
    AgentRuntime,
    BackendCapabilities,
    BackendExecutionReceipt,
    McpInvocationBroker,
)
from scripts.agent_team_state import AxStateStore
from scripts.agent_team_workflow import WorkflowDefinitions
from scripts.project_agents import load_and_validate
from scripts.project_skills import resolve_selection


NOW = "2026-07-21T00:00:00+00:00"
EXPIRES = "2027-07-21T00:00:00+00:00"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class _SeatPolicy:
    def resolve(self, seat_id: str):
        if seat_id != "seat-pm":
            raise AssertionError(seat_id)
        return {
            "seat_id": "seat-pm",
            "role_key": "pm",
            "service_identity": False,
            "dynamic_confinement_required": True,
            "active_capability": "pm",
            "professional_skill_id": "professional-profile-runtime",
            "model": "gpt-5",
            "model_reasoning_effort": "high",
        }


class _NoMcpCalls:
    def invoke(self, server_name, tool_name, input_payload):
        raise AssertionError("pm_intake_goal has no required MCP invocation")


class _ProductionBackend:
    def __init__(
        self,
        *,
        backend_id: str = "production-fixture",
        production: bool = True,
    ) -> None:
        self.calls = 0
        self.contract = None
        self.backend_id = backend_id
        self.production = production

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            backend_id=self.backend_id,
            attestation_id="sandbox-fixture",
            production=self.production,
            trusted_test_fixture=False,
            enforces_cwd=True,
            enforces_writable_roots=True,
            enforces_protected_roots=True,
            enforces_prohibited_roots=True,
            enforces_minimal_environment=True,
            enforces_timeout=True,
            bounds_output=True,
        )

    def execute(self, request) -> BackendExecutionReceipt:
        self.calls += 1
        contract = self.contract
        if contract is None:
            raise AssertionError("contract must be installed before execution")
        payload = {
            "result_kind": "goal_defined",
            "result_oid": request.subject_oid,
            "evidence_refs": ["artifact-result-1"],
            "mcp_usage_receipts": [],
            "serena_consumption_receipts": [],
            "format_repair_used": False,
            "token_accounting": {
                "input_tokens": 3,
                "output_tokens": 2,
                "repair_tokens": 0,
            },
            "payload": {
                "outgoing_messages": [
                    {
                        "thread_id": "thread-1",
                        "work_item_id": "work-1",
                        "from_role": "pm",
                        "to_role": "pl",
                        "type": "result",
                        "payload": {"goal_id": "goal-1"},
                        "dedupe_key": "message-result-1",
                    }
                ]
            },
            "completed_at": "2026-07-21T00:00:01+00:00",
        }
        started = time.time_ns()
        return BackendExecutionReceipt(
            backend_id=self.backend_id,
            attestation_id="sandbox-fixture",
            observed_cwd=request.cwd,
            observed_environment_digest=request.environment_digest,
            enforced_writable_roots=request.writable_roots,
            enforced_protected_roots=request.protected_roots,
            enforced_prohibited_roots=request.prohibited_roots,
            started_at_ns=started,
            finished_at_ns=started + 1,
            exit_code=0,
            timed_out=False,
            stdout=json.dumps(payload, sort_keys=True),
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        )


class _ActivationLifecycle:
    def __init__(self, store: AxStateStore, *, revoke_sandbox=False) -> None:
        self.state_store = store
        self.path_authority = None
        self.revoke_sandbox = revoke_sandbox
        self.quarantines = []

    def mark_running(self, activation_id: str, attempt_id: str):
        with self.state_store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE activations SET state = 'RUNNING', updated_at = ? WHERE id = ?",
                (NOW, activation_id),
            )
            if self.revoke_sandbox:
                connection.execute(
                    """
                    UPDATE sandbox_bindings
                    SET state = 'QUARANTINED', released_at = ?
                    WHERE id = ?
                    """,
                    (NOW, "sandbox-1"),
                )
        return SimpleNamespace(state="RUNNING")

    def quarantine(self, activation_id: str, reason: str):
        self.quarantines.append((activation_id, reason))
        return SimpleNamespace(state="QUARANTINED")


class CanonicalTaskFlowProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="taskflow-v4-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = AxStateStore(self.root / "ax.db")
        self.store.initialize()
        self.definitions = WorkflowDefinitions.load()
        self.worktree = self.root / "worktree"
        self.source = self.worktree / "src"
        self.source.mkdir(parents=True)
        subprocess.run(
            ["git", "init"], cwd=self.worktree, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "TaskFlow Fixture"],
            cwd=self.worktree,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "taskflow-fixture@example.invalid"],
            cwd=self.worktree,
            check=True,
        )
        fixture = self.source / "fixture.txt"
        fixture.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        )
        self.base_oid = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        fixture.write_text("base\nsubject\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "subject"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        )
        self.subject_oid = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.target = self.root / "target"
        (self.target / ".git").mkdir(parents=True)
        self.activation_root = self.root / "activation"
        self.activation_root.mkdir()
        self.profile_path = self.activation_root / "professional-profile.json"
        self.profile_path.write_text(
            json.dumps(
                {
                    "professional_skill_id": "professional-profile-runtime",
                    "references": [
                        {"id": "role"},
                        {"id": "gate"},
                        {"id": "technology"},
                        {"id": "toolchain"},
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.profile_digest = hashlib.sha256(self.profile_path.read_bytes()).hexdigest()
        self._seed_store()
        self.conf = self._conf()
        self.allocation = self._allocation()
        self.profile = {
            "stage": "resolve_compile_profile",
            "activation_id": "activation-1",
            "activation_root": str(self.activation_root),
            "professional_skill_id": "professional-profile-runtime",
            "compiled_profile_ref": str(self.profile_path),
            "compiled_profile_digest": self.profile_digest,
            "reference_ids": ["role", "gate", "technology", "toolchain"],
        }
        self.context_artifact = self._context_artifact()
        self.seat_policy = _SeatPolicy()
        self.backend = _ProductionBackend()
        self.broker = McpInvocationBroker(
            state_store=self.store,
            invoker=_NoMcpCalls(),
        )
        self.runtime = AgentRuntime(
            backend=self.backend,
            seat_policy_provider=self.seat_policy,
            result_root=self.root / "runner-results",
            state_store=self.store,
            mcp_broker=self.broker,
        )

    def tearDown(self) -> None:
        taskflow._RUNTIME_SERVICES_FACTORY = None

    def _seed_store(self) -> None:
        workflow_definition = self.store.register_definition(
            kind="WORKFLOW",
            version=self.definitions.workflow["version"],
            sha256=self.definitions.workflow_sha256,
            source_ref="agents/workflows/delivery-v4.toml",
        )
        output_definition = self.store.register_definition(
            kind="SCHEMA",
            version="activation-result-v4-production-test",
            sha256=hashlib.sha256(
                self.definitions.activation_result_schema_path.read_bytes()
            ).hexdigest(),
            source_ref="agents/contracts/schemas/activation-result.schema.json",
        )
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO targets (
                    id, canonical_checkout_path, git_common_dir, source_ref,
                    observed_source_oid, state, created_at, updated_at
                ) VALUES (?, ?, ?, 'refs/heads/main', ?, 'ACTIVE', ?, ?)
                """,
                (
                    "target-1",
                    str(self.target),
                    str(self.target / ".git"),
                    self.base_oid,
                    NOW,
                    NOW,
                ),
            )
            connection.execute(
                "INSERT INTO goals VALUES ('goal-1','target-1',?,'ACTIVE',?,?)",
                (self.base_oid, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO runs (
                    id, goal_id, target_id, base_oid, state,
                    idempotency_key, created_at
                ) VALUES ('run-1','goal-1','target-1',?,'RUNNING','run-key',?)
                """,
                (self.base_oid, NOW),
            )
            connection.execute(
                """
                INSERT INTO physical_seats VALUES (
                    'physical-seat-1','seat-pm','ACTIVE',0,'seat-key',?
                )
                """,
                (NOW,),
            )
            connection.execute(
                """
                INSERT INTO logical_capabilities VALUES (
                    'capability-pm','pm','ACTIVE',1,0,0,'capability-key',?
                )
                """,
                (NOW,),
            )
            connection.execute(
                """
                INSERT INTO seat_capability_ownerships VALUES (
                    'physical-seat-1','capability-pm','ENABLED','ownership-key',?
                )
                """,
                (NOW,),
            )
            connection.execute(
                """
                INSERT INTO runtime_slots VALUES (
                    'slot-1','pm_ta','FIXED','physical-seat-1',NULL,
                    'OCCUPIED','slot-key',?
                )
                """,
                (NOW,),
            )
            connection.execute(
                """
                INSERT INTO worker_identities VALUES (
                    'worker-1','worker-pm','FIXED','physical-seat-1',
                    'ACTIVE','worker-key',?
                )
                """,
                (NOW,),
            )
            connection.execute(
                """
                INSERT INTO worker_fingerprints VALUES (
                    'fingerprint-1','worker-1',?,?,'ACTIVE',
                    'fingerprint-key',?,NULL
                )
                """,
                (_digest("fingerprint"), _digest("runtime-profile"), NOW),
            )
            connection.execute(
                """
                INSERT INTO worker_slot_assignments VALUES (
                    'assignment-1','worker-1','fingerprint-1','slot-1','run-1',
                    0,'ACTIVE','assignment-key',?,NULL
                )
                """,
                (NOW,),
            )
            connection.execute(
                """
                INSERT INTO seat_capability_activations VALUES (
                    'seat-activation-1','physical-seat-1','capability-pm',
                    'slot-1','assignment-1','goal-1','run-1','ACTIVE',
                    'seat-activation-key',?,NULL
                )
                """,
                (NOW,),
            )
            connection.execute(
                """
                INSERT INTO workflow_definitions VALUES (
                    'workflow-definition-1',?,'delivery-v4',?,'ACTIVE',
                    'workflow-definition-key',?
                )
                """,
                (workflow_definition, self.definitions.workflow["version"], NOW),
            )
            for ordinal, state in enumerate(("pm_intake", "pl_assignment", "blocked")):
                connection.execute(
                    "INSERT INTO workflow_states VALUES (?,?,?,?,?)",
                    (
                        "workflow-definition-1",
                        state,
                        ordinal,
                        int(state == "pm_intake"),
                        int(state == "blocked"),
                    ),
                )
            connection.execute(
                """
                INSERT INTO workflow_transitions VALUES (
                    'transition-1','workflow-definition-1','pm_intake_goal',
                    'pm_intake','pl_assignment','capability-pm','goal_defined',
                    'blocked',0,?,'ACTIVE','transition-key',?
                )
                """,
                (output_definition, NOW),
            )
            connection.execute(
                """
                INSERT INTO workflow_instances VALUES (
                    'workflow-instance-1','workflow-definition-1','goal-1','run-1',
                    'pm_intake','ACTIVE','workflow-instance-key',?,?,NULL
                )
                """,
                (NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO repository_registrations VALUES (
                    'repository-1','target-1',NULL,?,?,?,'ACTIVE','repository-key',?
                )
                """,
                (str(self.target), str(self.target / ".git"), self.base_oid, NOW),
            )
            connection.execute(
                """
                INSERT INTO runtime_leases VALUES (
                    'lease-1','repository-1','goal-1','run-1','slot-1',
                    'assignment-1','DEVELOPMENT','refs/heads/work',?,?,?, ?, ?,
                    'ACTIVE',?,'lease-key',?,NULL
                )
                """,
                (
                    str(self.worktree),
                    self.base_oid,
                    self.subject_oid,
                    json.dumps([str(self.source)]),
                    json.dumps([str(self.target)]),
                    EXPIRES,
                    NOW,
                ),
            )
            connection.execute(
                """
                INSERT INTO sandbox_bindings VALUES (
                    'sandbox-1','lease-1','repository-1','run-1','slot-1',?, ?, ?,
                    0,?,'production-fixture',?,'ACTIVE','sandbox-key',?,NULL
                )
                """,
                (
                    self.subject_oid,
                    str(self.worktree),
                    str(self.source),
                    json.dumps([str(self.source)]),
                    _digest("attestation"),
                    NOW,
                ),
            )
            connection.execute(
                """
                INSERT INTO oid_authorities VALUES (
                    'oid-authority-1','repository-1','goal-1','run-1','lease-1',
                    'sandbox-1','SUBJECT',?,?,'ACTIVE','oid-key',?
                )
                """,
                (self.subject_oid, _digest("oid-evidence"), NOW),
            )
            connection.execute(
                """
                INSERT INTO activations (
                    id,target_id,goal_id,run_id,workspace_id,sandbox_path,
                    subject_oid,role,gate_or_task,state,process_id,result_json,
                    idempotency_key,created_at,updated_at,terminated_at
                ) VALUES (
                    'activation-1','target-1','goal-1','run-1',NULL,NULL,?,
                    'pm','pm_intake_goal','WORKSPACE_BOUND',NULL,NULL,
                    'activation-key',?,?,NULL
                )
                """,
                (self.subject_oid, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO profile_bindings VALUES (
                    'activation-1','professional-profile-runtime',?,?,
                    'BOUND',?,NULL
                )
                """,
                (str(self.profile_path), self.profile_digest, NOW),
            )
            for ordinal, kind in enumerate(
                ("ROLE", "GATE_OR_TASK", "PRIMARY_TECHNOLOGY", "TOOLCHAIN")
            ):
                connection.execute(
                    "INSERT INTO profile_reference_bindings VALUES (?,?,?,?,?,?)",
                    (
                        "activation-1",
                        ordinal,
                        kind,
                        f"profiles/{kind.lower()}.md",
                        "1.0.0",
                        _digest(kind),
                    ),
                )
        for server in ("serena", "sequentialthinking"):
            definition_id = self.store.register_mcp_definition(
                server_name=server,
                tool_name="server-health",
                version="production-test",
                sha256=_digest(f"{server}-definition"),
            )
            self.store.record_mcp_health_observation(
                mcp_definition_id=definition_id,
                status=McpHealthStatus.HEALTHY,
                evidence_digest=_digest(f"{server}-health"),
                idempotency_key=f"{server}-health-key",
            )

    def _conf(self):
        return taskflow.validate_runtime_conf(
            {
                "target_id": "target-1",
                "goal_id": "goal-1",
                "work_item_id": "work-1",
                "thread_id": "thread-1",
                "revision": 1,
                "run_id": "run-1",
                "activation_id": "activation-1",
                "idempotency_key": "taskflow-production-1",
                "execution_kind": "development",
                "actor_role": "pm",
                "actor_seat_id": "seat-pm",
                "gate_or_task": "pm_intake_goal",
                "repo_id": "repository-1",
                "base_oid": self.base_oid,
                "head_oid": self.subject_oid,
                "subject_oid": self.subject_oid,
                "context_profile": "implementation",
                "db_path": str(self.store.db_path),
                "registry_path": str(self.root / "repositories.json"),
                "artifact_root": str(self.root / "artifacts"),
                "target_paths": ["src"],
                "source_write_scope": ["src"],
                "generated_write_scope": [],
                "repository_manifests": {},
                "build_evidence": {},
                "command": ["codex"],
                "command_prefixes": [["codex"]],
                "tool_policy_id": "codex-only",
                "allowed_tools": ["codex"],
                "workflow_instance_id": "workflow-instance-1",
                "transition_id": "pm_intake_goal",
                "worker_assignment_id": "assignment-1",
                "repository_registration_id": "repository-1",
                "runtime_lease_id": "lease-1",
                "sandbox_binding_id": "sandbox-1",
                "oid_authority_id": "oid-authority-1",
                "environment": {"PYTHONUTF8": "1"},
            }
        )

    def _allocation(self):
        return {
            "stage": "allocate_bind_activation",
            "execution_kind": "development",
            "activation_id": "activation-1",
            "workspace_id": "sandbox-1",
            "lease_id": "lease-1",
            "sandbox_id": None,
            "cwd": str(self.worktree),
            "resource_root": str(self.worktree),
            "source_roots": [str(self.source)],
            "generated_roots": [],
            "ephemeral_writable_roots": [],
            "writable_roots": [str(self.source)],
            "protected_roots": [str(self.target)],
            "prohibited_roots": [str(self.target)],
            "subject_oid": self.subject_oid,
        }

    def _context_artifact(self):
        bundle = load_and_validate()
        selected = resolve_selection(
            bundle["skill_catalog"],
            bundle["skill_index"],
            "pm",
            [],
            transition_id="pm_intake_goal",
        )
        skills = []
        total = 0
        for descriptor in selected["skills"]:
            source = Path("skills") / descriptor["path"]
            content = source.read_text(encoding="utf-8")
            total += len(content)
            skills.append({**descriptor, "content_chars": len(content)})
        context_path = self.activation_root / "context.json"
        environment = tuple(sorted(dict(self.conf["environment"]).items()))
        context_path.write_text(
            json.dumps(
                {
                    "professional_profile": {
                        "skill_id": "professional-profile-runtime",
                        "compiled_profile_ref": str(self.profile_path),
                        "compiled_profile_digest": self.profile_digest,
                    },
                    "target_seat_id": "seat-pm",
                    "target_role": "pm",
                    "runtime_binding": {
                        "activation_id": "activation-1",
                        "workspace_id": "sandbox-1",
                        "lease_id": "lease-1",
                        "sandbox_id": None,
                        "execution_kind": "development",
                        "base_oid": self.base_oid,
                        "head_oid": self.subject_oid,
                        "subject_oid": self.subject_oid,
                        "seat_id": "seat-pm",
                        "role_key": "pm",
                        "cwd": str(self.worktree),
                        "source_roots": [str(self.source)],
                        "generated_roots": [],
                        "ephemeral_writable_roots": [],
                        "protected_roots": [str(self.target)],
                        "prohibited_roots": [str(self.target)],
                        "environment_digest": taskflow.environment_fingerprint(
                            environment
                        ),
                        "tool_policy_id": "codex-only",
                    },
                    "skill_packet": {
                        "skills": skills,
                        "max_content_chars": total,
                    }
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return {
            "stage": "bounded_context",
            "activation_id": "activation-1",
            "context_ref": str(context_path),
            "context_digest": hashlib.sha256(context_path.read_bytes()).hexdigest(),
        }

    def _services(self, *, revoke_sandbox=False):
        activation = _ActivationLifecycle(
            self.store,
            revoke_sandbox=revoke_sandbox,
        )
        context = SimpleNamespace(
            queue=SimpleNamespace(state_store=self.store)
        )
        component = lambda: SimpleNamespace(state_store=self.store)
        services = taskflow.configure_canonical_runtime_services(
            state_store=self.store,
            workspace_manager=component(),
            review_materializer=component(),
            activation_manager=activation,
            profile_resolver=SimpleNamespace(),
            profile_compiler=component(),
            context_compiler=context,
            seat_policy_provider=self.seat_policy,
            runtime=self.runtime,
        )
        self.assertIs(taskflow._runtime_services(self.conf), services)
        return services

    def test_real_store_contract_runtime_and_atomic_result_outbox(self) -> None:
        services = self._services()
        control = taskflow.compile_admit_contract(
            self.conf, self.allocation, self.profile, services
        )
        self.assertTrue(control["accepted"])
        self.backend.contract = control["contract"]
        execution = taskflow.execute_agent_runner(
            self.conf,
            self.allocation,
            self.profile,
            self.context_artifact,
            services,
            control,
        )
        integrity = taskflow.verify_execution_integrity(
            self.conf, self.allocation, execution, services
        )
        persisted = taskflow.persist_agent_result(
            self.conf, execution, integrity, services, control
        )
        self.assertEqual(1, self.backend.calls)
        self.assertTrue(persisted["activation_result_id"])
        self.assertEqual(1, len(persisted["message_ids"]))
        self.assertEqual(1, len(persisted["outbox_ids"]))
        with self.store.transaction() as connection:
            activation = connection.execute(
                "SELECT state, result_json FROM activations WHERE id = 'activation-1'"
            ).fetchone()
            self.assertEqual("RESULT_PERSISTED", activation["state"])
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM activation_results").fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            )

    def test_taskflow_runtime_state_mismatch_calls_backend_zero(self) -> None:
        services = self._services(revoke_sandbox=True)
        control = taskflow.compile_admit_contract(
            self.conf, self.allocation, self.profile, services
        )
        self.backend.contract = control["contract"]
        with self.assertRaisesRegex(
            Exception,
            "v4 runtime relation is not active: sandbox_state",
        ):
            taskflow.execute_agent_runner(
                self.conf,
                self.allocation,
                self.profile,
                self.context_artifact,
                services,
                control,
            )
        self.assertEqual(0, self.backend.calls)

    def test_registered_factory_rejects_sandbox_backend_mismatch(self) -> None:
        backend = _ProductionBackend(backend_id="another-production-backend")
        runtime = AgentRuntime(
            backend=backend,
            seat_policy_provider=self.seat_policy,
            result_root=self.root / "mismatched-runner-results",
            state_store=self.store,
            mcp_broker=self.broker,
        )
        component = lambda: SimpleNamespace(state_store=self.store)
        taskflow.configure_canonical_runtime_services(
            state_store=self.store,
            workspace_manager=component(),
            review_materializer=component(),
            activation_manager=_ActivationLifecycle(self.store),
            profile_resolver=SimpleNamespace(),
            profile_compiler=component(),
            context_compiler=SimpleNamespace(
                queue=SimpleNamespace(state_store=self.store)
            ),
            seat_policy_provider=self.seat_policy,
            runtime=runtime,
        )
        with self.assertRaisesRegex(
            taskflow.TaskFlowContractError,
            "sandbox backend differs",
        ):
            taskflow._runtime_services(self.conf)
        self.assertEqual(0, backend.calls)

    def test_registration_rejects_nonproduction_backend(self) -> None:
        backend = _ProductionBackend(production=False)
        runtime = AgentRuntime(
            backend=backend,
            seat_policy_provider=self.seat_policy,
            result_root=self.root / "nonproduction-runner-results",
            state_store=self.store,
            mcp_broker=self.broker,
        )
        component = lambda: SimpleNamespace(state_store=self.store)
        with self.assertRaisesRegex(
            taskflow.TaskFlowContractError,
            "lacks production confinement evidence",
        ):
            taskflow.configure_canonical_runtime_services(
                state_store=self.store,
                workspace_manager=component(),
                review_materializer=component(),
                activation_manager=_ActivationLifecycle(self.store),
                profile_resolver=SimpleNamespace(),
                profile_compiler=component(),
                context_compiler=SimpleNamespace(
                    queue=SimpleNamespace(state_store=self.store)
                ),
                seat_policy_provider=self.seat_policy,
                runtime=runtime,
            )
        self.assertEqual(0, backend.calls)


if __name__ == "__main__":
    unittest.main()
