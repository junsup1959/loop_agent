from __future__ import annotations

import os
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_team_taskflow as taskflow  # noqa: E402
import project_agents  # noqa: E402
from agent_team_domain import SourceIntegrity  # noqa: E402
from agent_team_workflow import RepositoryBinding  # noqa: E402
from agent_team_taskflow import (  # noqa: E402
    DeterministicServiceCommand,
    DeterministicServiceDispatcher,
    RuntimeTaskServices,
    TASKFLOW_STAGE_ORDER,
    TaskFlowContractError,
    allocate_and_bind_activation,
    assert_immutable_conf,
    compile_admit_contract,
    execute_agent_runner,
    persist_agent_result,
    revoke_release_terminate,
    validate_runtime_conf,
    verify_execution_integrity,
)
from project_agents import (  # noqa: E402
    load_and_validate,
    resolve_runtime_activation_contract,
)


OID_A = "a" * 40
OID_B = "b" * 40


class _WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict[str, object]] = []

    def allocate_development(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(lease_id="lease-1", workspace_id="workspace-1")

    def execution_contract(self, lease_id: str):
        return SimpleNamespace(
            target_id="target-1",
            goal_id="goal-1",
            work_item_id="work-1",
            revision=1,
            owner="developer-1",
            expected_head_oid=OID_B,
            cwd=self.root,
            source_write_paths=(self.root / "src",),
            generated_write_paths=(self.root / "build",),
            prohibited_roots=(self.root.parent / "user-checkout",),
        )


class _ReviewMaterializer:
    def __init__(self, root: Path, classification=SourceIntegrity.CLEAN) -> None:
        self.root = root
        self.classification = classification

    def materialize(self, **kwargs):
        return SimpleNamespace(
            sandbox_id="sandbox-1",
            metadata_digest="c" * 64,
        )

    def runner_contract(self, sandbox_id: str):
        return SimpleNamespace(
            activation_id="activation-1",
            sandbox_id=sandbox_id,
            subject_oid=OID_B,
            cwd=self.root,
            analysis_source_root=self.root,
            generated_write_roots=(self.root / "generated",),
            ephemeral_writable_roots=(self.root / "scratch",),
            protected_metadata_roots=(self.root.parent / "metadata",),
            prohibited_authority_roots=(self.root.parent / "user-checkout",),
        )

    def verify_integrity(self, sandbox_id: str):
        return SimpleNamespace(
            sandbox_id=sandbox_id,
            subject_oid=OID_B,
            classification=self.classification,
            observed_head_oid=OID_B,
            observed_tree_oid="d" * 40,
            tracked_changes=(),
            untracked_source_changes=(),
            ignored_generated_paths=(),
            reasons=(),
            checked_at="2026-07-21T00:00:00Z",
        )


class _ActivationManager:
    def __init__(self) -> None:
        self.persisted = None
        self.quarantines: list[tuple[str, str]] = []
        self.revoked: list[str] = []
        self.running: list[tuple[str, str]] = []

    def mark_running(self, activation_id: str, attempt_id: str):
        self.running.append((activation_id, attempt_id))
        return SimpleNamespace(state="RUNNING")

    def persist_result(self, activation_id: str, result):
        self.persisted = (activation_id, result)
        return SimpleNamespace(state="RESULT_PERSISTED")

    def quarantine(self, activation_id: str, reason: str):
        self.quarantines.append((activation_id, reason))
        return SimpleNamespace(state="QUARANTINED")

    def revoke_and_terminate(self, activation_id: str):
        self.revoked.append(activation_id)
        return SimpleNamespace(state=SimpleNamespace(value="TERMINATED"))


class _AcceptingController:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.begin_calls = 0
        self.commit_calls = 0

    def prepare(self, **kwargs):
        self.prepare_calls += 1
        return {
            "contract": {"id": "contract-1"},
            "packet": {"id": "packet-1"},
            "contract_ref": _test_artifact_ref(),
            "packet_ref": _test_artifact_ref(),
            "admission": {
                "accepted": True,
                "model_call_permitted": True,
            },
        }

    def begin_runner_attempt(self, **kwargs):
        self.begin_calls += 1
        return taskflow.RuntimeSandboxBinding(
            contract_id="contract-1",
            activation_id="activation-1",
            attempt_id="attempt-1",
            repository_id="repository-1",
            lease_id="lease-1",
            sandbox_binding_id="sandbox-1",
            oid_authority_id="oid-authority-1",
            slot_id="slot-1",
            capability_id="developer",
            attestation_digest="a" * 64,
            request=kwargs["request"],
        )

    def commit_runner_result(self, **kwargs):
        self.commit_calls += 1
        return SimpleNamespace(
            status="committed",
            activation_result_id="activation-result-1",
            message_ids=(),
            outbox_ids=(),
        )


_DEFAULT_CONTROLLER = object()


def _test_artifact_ref() -> dict[str, str]:
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {"ref": str(Path(__file__).resolve()), "digest": digest}


def _services(
    *,
    workspace=None,
    review=None,
    activation=None,
    controller=_DEFAULT_CONTROLLER,
    runtime=None,
) -> RuntimeTaskServices:
    if controller is _DEFAULT_CONTROLLER:
        controller = _AcceptingController()
    return RuntimeTaskServices(
        workspace_manager=workspace,
        review_materializer=review,
        activation_manager=activation or _ActivationManager(),
        profile_resolver=None,
        profile_compiler=None,
        context_compiler=None,  # type: ignore[arg-type]
        seat_policy_provider=None,
        runtime=runtime,  # type: ignore[arg-type]
        contract_controller=controller,
    )


def _accepted_contract_control() -> dict[str, object]:
    return {
        "stage": "compile_admit_contract",
        "activation_id": "activation-1",
        "enforced": True,
        "accepted": True,
        "model_call_permitted": True,
        "contract": {"id": "contract-1"},
        "admission": {
            "accepted": True,
            "model_call_permitted": True,
        },
        "packet": {"id": "packet-1"},
        "contract_ref": _test_artifact_ref(),
        "packet_ref": _test_artifact_ref(),
    }


class TaskFlowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        for name in ("artifacts", "worktree", "sandbox", "metadata", "user-checkout"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def conf(self, *, execution_kind="development") -> dict[str, object]:
        return {
            "target_id": "target-1",
            "goal_id": "goal-1",
            "work_item_id": "work-1",
            "thread_id": "thread-1",
            "revision": 1,
            "run_id": "run-1",
            "activation_id": "activation-1",
            "idempotency_key": "taskflow-1",
            "execution_kind": execution_kind,
            "actor_role": "developer",
            "actor_seat_id": "developer-1",
            "gate_or_task": "implementation",
            "repo_id": "repo-1",
            "base_oid": OID_A,
            "head_oid": OID_B,
            "subject_oid": OID_B,
            "context_profile": "implementation",
            "db_path": str(self.root / "queue.db"),
            "registry_path": str(self.root / "repositories.json"),
            "artifact_root": str(self.root / "artifacts"),
            "target_paths": ["src"],
            "source_write_scope": ["src"],
            "generated_write_scope": ["build"],
            "repository_manifests": {},
            "build_evidence": {},
            "command": [sys.executable, "-c", "print('{}')"],
            "command_prefixes": [[sys.executable]],
            "tool_policy_id": "python-only",
            "allowed_tools": ["python"],
            "environment": {"PYTHONUTF8": "1"},
            "generated_paths": ["generated"],
        }

    def test_conf_is_sealed_and_tampering_is_rejected(self) -> None:
        sealed = validate_runtime_conf(self.conf())
        self.assertRegex(sealed["_immutable_conf_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(assert_immutable_conf(sealed), sealed)
        sealed["artifact_root"] = str(self.root / "different-artifacts")
        with self.assertRaisesRegex(TaskFlowContractError, "changed"):
            assert_immutable_conf(sealed)

    def test_conf_rejects_unknown_fields_abbreviated_oids_and_credentials(self) -> None:
        unknown = self.conf()
        unknown["model"] = "scheduler-must-not-pick-model"
        with self.assertRaisesRegex(TaskFlowContractError, "unknown"):
            validate_runtime_conf(unknown)
        abbreviated = self.conf()
        abbreviated["head_oid"] = "b" * 12
        with self.assertRaisesRegex(TaskFlowContractError, "full lowercase"):
            validate_runtime_conf(abbreviated)
        credential = self.conf()
        credential["environment"] = {"GIT_ASKPASS": "helper"}
        with self.assertRaisesRegex(Exception, "credential or Git authority"):
            validate_runtime_conf(credential)

    def test_stage_order_is_explicit_and_airflow_is_optional(self) -> None:
        self.assertEqual(
            TASKFLOW_STAGE_ORDER,
            (
                "immutable_conf",
                "allocate_bind_activation",
                "resolve_compile_profile",
                "compile_admit_contract",
                "bounded_context",
                "confined_execute",
                "integrity_verify",
                "deterministic_persistence",
                "revoke_release_terminate",
            ),
        )
        self.assertTrue(
            taskflow.AIRFLOW_IMPORT_ERROR is None
            or taskflow.module_iteration_dag is None
        )

    def test_runtime_services_requires_well_formed_contract_controller(self) -> None:
        common = {
            "workspace_manager": None,
            "review_materializer": None,
            "activation_manager": _ActivationManager(),
            "profile_resolver": None,
            "profile_compiler": None,
            "context_compiler": None,
            "seat_policy_provider": None,
            "runtime": None,
        }
        with self.assertRaises(TypeError):
            RuntimeTaskServices(**common)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TaskFlowContractError, "controller is required"):
            _services(controller=None)
        with self.assertRaisesRegex(TaskFlowContractError, "misconfigured"):
            _services(controller=SimpleNamespace(prepare=lambda **kwargs: None))

    def test_runtime_services_factory_rejects_invalid_controller_wiring(self) -> None:
        with mock.patch.object(taskflow, "_RUNTIME_SERVICES_FACTORY", None):
            with self.assertRaisesRegex(TypeError, "factory must be callable"):
                taskflow.configure_runtime_services(None)  # type: ignore[arg-type]
            taskflow.configure_runtime_services(lambda conf: object())
            with self.assertRaisesRegex(TypeError, "wrong type"):
                taskflow._runtime_services({})

            services = _services()
            object.__setattr__(services, "contract_controller", None)
            taskflow.configure_runtime_services(lambda conf: services)
            with self.assertRaisesRegex(
                TaskFlowContractError,
                "controller is required",
            ):
                taskflow._runtime_services({})

    def test_canonical_controller_calls_public_contract_lifecycle(self) -> None:
        store = taskflow.AxStateStore(self.root / "canonical-lifecycle.db")
        store.initialize()
        inputs = taskflow.CanonicalTaskFlowContractInputs(
            instance=taskflow.WorkflowInstance(
                instance_id="instance-1",
                goal_id="goal-1",
                run_id="run-1",
                target_id="target-1",
                current_state="implementation",
                state_store=store,
            ),
            transition=taskflow.TransitionDefinition(
                transition_id="implementation",
                from_states=("implementation",),
                to_state="review",
                state_effect="advance",
                capabilities=("developer",),
                result_kinds=("implementation_complete",),
                failure_state="implementation",
                clause_ids=(),
                mcp_availability_binding_ids=(),
                mcp_required_use_binding_ids=(),
                exact_oid_required=True,
                workspace_kind="development_worktree",
            ),
            actor=taskflow.ActorBinding(
                activation_id="activation-1",
                capability_id="developer",
                slot_key="developer",
                slot_type="fixed",
                worker_id="worker-1",
                worker_fingerprint="f" * 64,
                worker_fingerprint_id="fingerprint-1",
                slot_id="slot-1",
                worker_assignment_id="assignment-1",
                seat_id="developer-1",
            ),
            evidence=taskflow.EvidenceSet(
                repository=RepositoryBinding(
                    "repo-1", OID_B, state_store=store
                ),
                lease_id="lease-1",
                sandbox_binding_id="sandbox-binding-1",
                oid_authority_id="oid-authority-1",
                base_oid=OID_A,
                subject_oid=OID_B,
                workspace={},
                mcp_health={},
            ),
        )
        contract = mock.Mock(spec=taskflow.ActivationContract)
        contract.clauses = ()
        contract.definitions = SimpleNamespace(template_version="4.0.0")
        contract.contract_id = "contract-1"
        contract.activation_id = "activation-1"
        contract.state_store = store
        contract.database_bindings = {
            "transition_database_id": "transition-1",
            "capability_id": "capability-1",
            "output_schema_definition_id": "schema-1",
            "mcp_bindings": {},
            "serena_bindings": {},
        }
        contract_json = "{}"
        contract.digest = hashlib.sha256(contract_json.encode()).hexdigest()
        markdown = "# packet\n"
        rendered_packet = SimpleNamespace(
            contract_ref=str(self.root / "artifacts" / "contract.json"),
            packet_ref=str(self.root / "artifacts" / "packet.md"),
            contract_json=contract_json,
            markdown=markdown,
            contract_sha256=contract.digest,
            packet_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
        )
        admission = taskflow.AdmissionReceipt(
            receipt_id="admission-1",
            contract_id="contract-1",
            contract_sha256=contract.digest,
            accepted=True,
            reason_code=None,
            model_call_permitted=True,
            checks={},
            violations=(),
            idempotency_key="admission:contract-1",
            recorded_at="2026-07-21T00:00:00Z",
        )

        class Provider:
            def __init__(self):
                self.state_store = store

            def contract_inputs(self, **kwargs):
                return inputs

            def attempt_inputs(self, **kwargs):
                request = kwargs["request"]
                return taskflow.CanonicalTaskFlowAttemptInputs(
                    backend="trusted-broker",
                    model=request.model,
                    input_digest=request.request_digest,
                )

            def runtime_binding(self, **kwargs):
                return taskflow.RuntimeSandboxBinding(
                    contract_id="contract-1",
                    activation_id="activation-1",
                    attempt_id=kwargs["attempt_id"],
                    repository_id="repository-1",
                    lease_id="lease-1",
                    sandbox_binding_id="sandbox-1",
                    oid_authority_id="oid-authority-1",
                    slot_id="slot-1",
                    capability_id="developer",
                    attestation_digest="a" * 64,
                    request=kwargs["request"],
                )

            def activation_result(self, **kwargs):
                return taskflow.ActivationResult(
                    contract=kwargs["contract"],
                    payload={},
                    attempt_id=kwargs["attempt_id"],
                )

        controller = taskflow.CanonicalTaskFlowContractController(Provider())
        request = mock.Mock(spec=taskflow.RunnerRequest)
        request.model = "gpt-5"
        request.request_digest = "e" * 64
        with (
            mock.patch.object(
                taskflow,
                "compile_canonical_transition",
                return_value=contract,
            ) as compile_call,
            mock.patch.object(
                taskflow,
                "render_canonical_contract",
                return_value=rendered_packet,
            ) as render_call,
            mock.patch.object(
                taskflow,
                "admit_canonical_contract",
                return_value=admission,
            ) as admit_call,
            mock.patch.object(
                taskflow,
                "begin_canonical_attempt",
                return_value="attempt-1",
            ) as begin_call,
            mock.patch.object(
                taskflow,
                "commit_canonical_result",
                return_value={"status": "accepted"},
            ) as commit_call,
        ):
            prepared = controller.prepare(conf={}, allocation={}, profile={})
            binding = controller.begin_runner_attempt(
                conf={},
                allocation={},
                profile={},
                context_artifact={},
                request=request,
                contract=prepared["contract"],
                admission=prepared["admission"],
            )
            committed = controller.commit_runner_result(
                contract=prepared["contract"],
                admission=prepared["admission"],
                attempt_id=binding.attempt_id,
                runner_result={},
                integrity={},
            )

        compile_call.assert_called_once_with(
            inputs.instance,
            inputs.transition,
            inputs.actor,
            inputs.evidence,
        )
        render_call.assert_called_once()
        admit_call.assert_called_once_with(contract)
        begin_call.assert_called_once()
        commit_call.assert_called_once()
        self.assertEqual({"status": "accepted"}, committed)

    def test_controllerless_or_missing_control_calls_no_backend(self) -> None:
        class CountingRuntime:
            def __init__(self):
                self.calls = 0

            def execute(self, request):
                self.calls += 1
                raise AssertionError("backend must not be called")

        sealed = validate_runtime_conf(self.conf())
        runtime = CountingRuntime()
        services = _services(runtime=runtime)
        control = compile_admit_contract(sealed, {}, {}, services)
        object.__setattr__(services, "contract_controller", None)
        with self.assertRaisesRegex(TaskFlowContractError, "controller is required"):
            execute_agent_runner(
                sealed,
                {},
                {},
                {"stage": "bounded_context"},
                services,
                control,
            )
        self.assertEqual(0, runtime.calls)

        services = _services(runtime=runtime)
        with self.assertRaisesRegex(TaskFlowContractError, "contract control"):
            execute_agent_runner(
                sealed,
                {},
                {},
                {"stage": "bounded_context"},
                services,
                None,
            )
        self.assertEqual(0, runtime.calls)

    def test_valid_controller_begins_attempt_before_backend(self) -> None:
        class CountingRuntime:
            def __init__(self):
                self.calls = 0

            def execute(self, contract_ref, packet_ref, binding):
                self.calls += 1
                return SimpleNamespace(
                    as_mapping=lambda: {
                        "result_id": "result-1",
                        "status": "succeeded",
                    }
                )

        controller = _AcceptingController()
        runtime = CountingRuntime()
        activation = _ActivationManager()
        services = _services(
            activation=activation,
            controller=controller,
            runtime=runtime,
        )
        sealed = validate_runtime_conf(self.conf())
        control = compile_admit_contract(sealed, {}, {}, services)
        self.assertTrue(control["enforced"])
        self.assertTrue(control["accepted"])
        with mock.patch.object(
            taskflow,
            "_runner_request",
            return_value=mock.Mock(spec=taskflow.RunnerRequest),
        ):
            execution = execute_agent_runner(
                sealed,
                {"execution_kind": "development"},
                {},
                {"stage": "bounded_context"},
                services,
                control,
            )
        self.assertEqual(1, controller.prepare_calls)
        self.assertEqual(1, controller.begin_calls)
        self.assertEqual(1, runtime.calls)
        self.assertEqual("attempt-1", execution["contract_attempt_id"])
        self.assertEqual(
            [("activation-1", "attempt-1")],
            activation.running,
        )

    def test_development_allocation_binds_only_assigned_worktree(self) -> None:
        workspace = _WorkspaceManager(self.root / "worktree")
        receipt = allocate_and_bind_activation(
            validate_runtime_conf(self.conf()),
            _services(workspace=workspace),
        )
        self.assertEqual(receipt["cwd"], str(self.root / "worktree"))
        self.assertIn(str(self.root / "worktree" / "src"), receipt["writable_roots"])
        self.assertNotIn(str(self.root / "user-checkout"), receipt["writable_roots"])
        self.assertEqual(workspace.calls[0]["owner_seat_id"], "developer-1")

    def test_review_source_is_read_only_and_generated_roots_are_writable(self) -> None:
        review = _ReviewMaterializer(self.root / "sandbox")
        receipt = allocate_and_bind_activation(
            validate_runtime_conf(self.conf(execution_kind="review")),
            _services(review=review),
        )
        self.assertEqual(receipt["source_roots"], [str(self.root / "sandbox")])
        self.assertNotIn(str(self.root / "sandbox"), receipt["writable_roots"])
        self.assertIn(
            str(self.root / "sandbox" / "generated"),
            receipt["writable_roots"],
        )

    def test_dirty_review_is_exploratory_and_invalid_review_is_quarantined(self) -> None:
        sealed = validate_runtime_conf(self.conf(execution_kind="review"))
        allocation = {
            "execution_kind": "review",
            "sandbox_id": "sandbox-1",
        }
        execution = {
            "stage": "confined_execute",
            "activation_id": "activation-1",
            "runner_result": {},
        }
        dirty_services = _services(
            review=_ReviewMaterializer(
                self.root / "sandbox", SourceIntegrity.ANALYSIS_DIRTY
            )
        )
        dirty = verify_execution_integrity(
            sealed, allocation, execution, dirty_services
        )
        self.assertFalse(dirty["gate_evidence_eligible"])
        self.assertTrue(dirty["clean_rerun_required"])

        invalid_activation = _ActivationManager()
        invalid = dict(dirty)
        invalid["classification"] = SourceIntegrity.INVALIDATED.value
        invalid["clean_rerun_required"] = False
        runner_result = {
            "result_id": "result-1",
            "status": "succeeded",
            "artifact_ref": str(self.root / "result.json"),
            "receipt": {"stdout": "{}"},
        }
        persisted = persist_agent_result(
            sealed,
            {
                "runner_result": runner_result,
                "contract_attempt_id": "attempt-1",
            },
            invalid,
            _services(activation=invalid_activation),
            _accepted_contract_control(),
        )
        self.assertEqual(persisted["classification"], "INVALIDATED")
        self.assertIsNone(invalid_activation.persisted)
        self.assertEqual(invalid_activation.quarantines, [])
        cleanup = revoke_release_terminate(
            sealed,
            persisted,
            _services(activation=invalid_activation),
        )
        self.assertTrue(cleanup["resource_preserved"])
        self.assertEqual(invalid_activation.revoked, [])

    def test_controller_commands_cannot_allocate_llm_seats_or_profiles(self) -> None:
        seen = []

        def handler(operation, payload, idempotency_key):
            seen.append((operation, payload, idempotency_key))
            return {"state": "COMPLETED"}

        dispatcher = DeterministicServiceDispatcher(
            {
                "integration-controller": handler,
                "promotion-controller": handler,
                "recovery-controller": handler,
            }
        )
        for identity in (
            "integration-controller",
            "promotion-controller",
            "recovery-controller",
        ):
            receipt = dispatcher.execute(
                DeterministicServiceCommand(
                    command_id=f"{identity}-1",
                    service_identity=identity,
                    operation="execute",
                    payload={"target_id": "target-1", "subject_oid": OID_B},
                    idempotency_key=f"{identity}:1",
                )
            )
            self.assertEqual(receipt["service_identity"], identity)
        self.assertEqual(len(seen), 3)
        with self.assertRaisesRegex(TaskFlowContractError, "LLM-seat"):
            DeterministicServiceCommand(
                command_id="bad-1",
                service_identity="integration-controller",
                operation="execute",
                payload={"seat_id": "TA_someone"},
                idempotency_key="bad:1",
            )

    def test_rejected_contract_admission_calls_no_backend(self) -> None:
        class RejectingController:
            @staticmethod
            def prepare(**kwargs):
                return {
                    "contract": {"id": "contract-1"},
                    "packet": {"id": "packet-1"},
                    "contract_ref": _test_artifact_ref(),
                    "packet_ref": _test_artifact_ref(),
                    "admission": {
                        "accepted": False,
                        "model_call_permitted": False,
                    },
                }

            @staticmethod
            def begin_runner_attempt(**kwargs):
                raise AssertionError("rejected admission cannot begin an attempt")

            @staticmethod
            def commit_runner_result(**kwargs):
                raise AssertionError("rejected admission cannot commit a result")

        class CountingRuntime:
            def __init__(self):
                self.calls = 0

            def execute(self, request):
                self.calls += 1
                raise AssertionError("backend must not be called")

        runtime = CountingRuntime()
        services = _services(
            controller=RejectingController(),
            runtime=runtime,
        )
        sealed = validate_runtime_conf(self.conf())
        control = compile_admit_contract(sealed, {}, {}, services)
        self.assertFalse(control["accepted"])
        with self.assertRaisesRegex(TaskFlowContractError, "forbidden"):
            execute_agent_runner(
                sealed,
                {},
                {},
                {"stage": "bounded_context"},
                services,
                control,
            )
        self.assertEqual(0, runtime.calls)

    def test_six_slot_model_exposes_dynamic_capability_contract(self) -> None:
        original_load_catalog = project_agents.load_catalog

        def load_catalog_with_pending_serena_v2(path):
            catalog = original_load_catalog(path)
            for skill in catalog["skills"]:
                if skill.get("id") == "serena-project-setup":
                    skill["version"] = "2.0.0"
                    skill["sha256"] = hashlib.sha256(
                        (ROOT / "skills" / skill["path"]).read_bytes()
                    ).hexdigest()
            return catalog

        with mock.patch.object(
            project_agents,
            "load_catalog",
            side_effect=load_catalog_with_pending_serena_v2,
        ):
            bundle = load_and_validate()
        self.assertEqual(len(bundle["seats"]), 5)
        self.assertEqual(set(bundle["capabilities"]), {
            "pm", "ta", "pl", "developer", "qa_sdet", "build_release",
            "worker", "advisory",
        })
        seat_id = next(
            key for key, seat in bundle["seats"].items()
            if seat["slot_key"] == "pm_ta"
        )
        contract = resolve_runtime_activation_contract(bundle, seat_id, "ta")
        self.assertEqual("ta", contract["active_capability"])
        self.assertTrue(contract["dynamic_cwd_required"])
        self.assertTrue(contract["dynamic_root_attestation_required"])
        self.assertFalse(contract["service_identity"])
        self.assertNotIn("cwd", contract)
        self.assertNotIn("writable_roots", contract)


if __name__ == "__main__":
    unittest.main()
