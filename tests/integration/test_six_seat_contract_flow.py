from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from typing import Mapping

from scripts.agent_team_contracts import (
    ActivationResult,
    AdmissionReceipt,
    ReworkRoute,
    TransitionReceipt,
    admit,
    begin_attempt,
    commit_result,
    compile_transition,
)
from scripts.agent_team_workflow import (
    ActorBinding,
    EvidenceSet,
    RepositoryBinding,
    WorkflowDefinitions,
    WorkflowInstance,
)
from scripts.serena_project_knowledge import required_memories_for_transition
from scripts.agent_team_state import AxStateStore
from scripts.agent_team_runtime import McpInvocationBroker, McpInvocationContext


OID_BASE = "a" * 40
ISSUED_AT = "2026-07-21T00:00:00+00:00"
COMPLETED_AT = "2026-07-21T00:00:01+00:00"


def oid(label: str) -> str:
    return hashlib.sha1(label.encode("utf-8")).hexdigest()


class _FixtureMcpInvoker:
    def invoke(self, server_name, tool_name, input_payload):
        return {
            "server_name": server_name,
            "tool_name": tool_name,
            "request_digest": input_payload["request_digest"],
        }


class SixSeatContractFixture:
    """Test-only compiler harness shared by the Phase 6 integration modules."""

    SLOT_BY_CAPABILITY = {
        "pm": "pm_ta",
        "ta": "pm_ta",
        "pl": "pl",
        "developer": "dev_1",
        "qa_sdet": "qa_build",
        "build_release": "qa_build",
    }

    def __init__(self, owner: unittest.TestCase) -> None:
        self.owner = owner
        self.temporary = tempfile.TemporaryDirectory(prefix="six-seat-contract-")
        owner.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.profile = self.root / "compiled-profile.json"
        self.profile.write_text(
            '{"professional_skill_id":"professional-profile-runtime"}',
            encoding="utf-8",
        )
        self.profile_sha256 = hashlib.sha256(self.profile.read_bytes()).hexdigest()
        self.definitions = WorkflowDefinitions.load()
        professional = Path("skills/professional-profile-runtime/SKILL.md")
        self.professional_skill = {
            "id": "professional-profile-runtime",
            "version": "1.0.0",
            "kind": "professional",
            "path": "professional-profile-runtime/SKILL.md",
            "sha256": hashlib.sha256(professional.read_bytes()).hexdigest(),
            "content_budget_chars": 4000,
            "mcp_prerequisites": [],
            "eligible_capabilities": [
                "pm",
                "ta",
                "pl",
                "developer",
                "qa_sdet",
                "build_release",
                "worker",
                "advisory",
            ],
        }
        self.source = self.root / "target-source.txt"
        self.source.write_text("operator checkout remains immutable\n", encoding="utf-8")
        self._scope_counter = 0

    def instance(self, state: str) -> WorkflowInstance:
        return WorkflowInstance(
            instance_id="workflow-e2e",
            goal_id="goal-e2e",
            run_id="run-e2e",
            target_id="target-e2e",
            current_state=state,
        )

    def _durable_scope(
        self,
        *,
        instance: WorkflowInstance,
        transition,
        actor: ActorBinding,
        evidence: EvidenceSet,
    ) -> AxStateStore:
        """Seed the minimum real v4 graph for one isolated contract test."""

        self._scope_counter += 1
        serial = f"{self._scope_counter}-{actor.activation_id}"
        store = AxStateStore(self.root / f"contract-scope-{self._scope_counter}.db")
        store.initialize()
        workflow_definition_ref = store.register_definition(
            kind="WORKFLOW",
            version="4.0.0",
            sha256=self.definitions.workflow_sha256,
            source_ref="agents/workflows/delivery-v4.toml",
        )
        output_schema_digest = hashlib.sha256(
            self.definitions.activation_result_schema_path.read_bytes()
        ).hexdigest()
        output_schema_ref = store.register_definition(
            kind="SCHEMA",
            version=f"activation-result-v4-{serial}",
            sha256=output_schema_digest,
            source_ref="agents/contracts/schemas/activation-result.schema.json",
        )
        workspace = evidence.workspace
        cwd = str(workspace["cwd"])
        source_roots = list(workspace["source_roots"])
        writable_roots = list(workspace["writable_roots"])
        protected_roots = list(workspace["protected_roots"])
        lease_kind = {
            "developer-worktree": "DEVELOPMENT",
            "integration-worktree": "INTEGRATION",
            "review-sandbox": "REVIEW",
            "advisory-sandbox": "ADVISORY",
        }.get(transition.workspace_kind, "ADVISORY")
        branch_ref = (
            f"refs/heads/ax/test/{self._scope_counter}"
            if lease_kind in {"DEVELOPMENT", "INTEGRATION"}
            else None
        )
        to_state = transition.to_state or instance.current_state
        state_keys = tuple(
            dict.fromkeys(
                (instance.current_state, to_state, transition.failure_state)
            )
        )
        now = ISSUED_AT
        repository_path = str(self.root / f"repository-{self._scope_counter}")
        with store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO targets (
                    id, canonical_checkout_path, git_common_dir, source_ref,
                    observed_source_oid, state, created_at, updated_at
                ) VALUES (?, ?, ?, 'refs/heads/main', ?, 'ACTIVE', ?, ?)
                """,
                (
                    instance.target_id,
                    repository_path,
                    f"{repository_path}/.git",
                    evidence.base_oid,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO goals (id, target_id, base_oid, state, created_at, updated_at)
                VALUES (?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (instance.goal_id, instance.target_id, evidence.base_oid, now, now),
            )
            connection.execute(
                """
                INSERT INTO runs (
                    id, goal_id, target_id, base_oid, state, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)
                """,
                (
                    instance.run_id,
                    instance.goal_id,
                    instance.target_id,
                    evidence.base_oid,
                    f"run:{serial}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO activations (
                    id, target_id, goal_id, run_id, workspace_id, sandbox_path,
                    subject_oid, role, gate_or_task, state, process_id, result_json,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, 'RUNNING', NULL, NULL, ?, ?, ?)
                """,
                (
                    actor.activation_id,
                    instance.target_id,
                    instance.goal_id,
                    instance.run_id,
                    cwd,
                    evidence.subject_oid,
                    actor.capability_id,
                    transition.transition_id,
                    f"activation:{serial}",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO physical_seats (
                    id, seat_key, state, is_merged, idempotency_key, created_at
                ) VALUES (?, ?, 'ACTIVE', 0, ?, ?)
                """,
                (
                    actor.physical_seat_id,
                    actor.seat_id or actor.slot_key,
                    f"seat:{serial}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO logical_capabilities (
                    id, capability_key, state, approval_authority,
                    merge_authority, nested_spawn_authority,
                    idempotency_key, created_at
                ) VALUES (?, ?, 'ACTIVE', 0, 0, 0, ?, ?)
                """,
                (
                    actor.capability_id,
                    actor.capability_id,
                    f"capability:{serial}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO seat_capability_ownerships (
                    physical_seat_id, capability_id, state, idempotency_key, assigned_at
                ) VALUES (?, ?, 'ENABLED', ?, ?)
                """,
                (
                    actor.physical_seat_id,
                    actor.capability_id,
                    f"ownership:{serial}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO runtime_slots (
                    id, slot_key, kind, physical_seat_id, elastic_singleton,
                    state, idempotency_key, created_at
                ) VALUES (?, ?, 'FIXED', ?, NULL, 'AVAILABLE', ?, ?)
                """,
                (actor.slot_id, actor.slot_key, actor.physical_seat_id, f"slot:{serial}", now),
            )
            connection.execute(
                """
                INSERT INTO worker_identities (
                    id, worker_key, kind, physical_seat_id, state,
                    idempotency_key, created_at
                ) VALUES (?, ?, 'FIXED', ?, 'ACTIVE', ?, ?)
                """,
                (
                    actor.worker_id,
                    actor.worker_id,
                    actor.physical_seat_id,
                    f"worker:{serial}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO worker_fingerprints (
                    id, worker_id, fingerprint_sha256, runtime_profile_digest,
                    state, idempotency_key, created_at, revoked_at
                ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, NULL)
                """,
                (
                    actor.worker_fingerprint_id,
                    actor.worker_id,
                    actor.worker_fingerprint,
                    hashlib.sha256(f"runtime:{serial}".encode()).hexdigest(),
                    f"fingerprint:{serial}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO worker_slot_assignments (
                    id, worker_id, worker_fingerprint_id, slot_id, run_id,
                    is_elastic, state, idempotency_key, assigned_at, released_at
                ) VALUES (?, ?, ?, ?, ?, 0, 'ACTIVE', ?, ?, NULL)
                """,
                (
                    actor.worker_assignment_id,
                    actor.worker_id,
                    actor.worker_fingerprint_id,
                    actor.slot_id,
                    instance.run_id,
                    f"assignment:{serial}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO seat_capability_activations (
                    id, physical_seat_id, capability_id, slot_id,
                    worker_assignment_id, goal_id, run_id, state,
                    idempotency_key, activated_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, NULL)
                """,
                (
                    actor.seat_capability_activation_id,
                    actor.physical_seat_id,
                    actor.capability_id,
                    actor.slot_id,
                    actor.worker_assignment_id,
                    instance.goal_id,
                    instance.run_id,
                    f"seat-activation:{serial}",
                    now,
                ),
            )
            workflow_definition_id = f"workflow-definition-{self._scope_counter}"
            connection.execute(
                """
                INSERT INTO workflow_definitions (
                    id, definition_id, workflow_key, version, state,
                    idempotency_key, created_at
                ) VALUES (?, ?, 'delivery-v4', '4.0.0', 'ACTIVE', ?, ?)
                """,
                (
                    workflow_definition_id,
                    workflow_definition_ref,
                    f"workflow-definition:{serial}",
                    now,
                ),
            )
            for ordinal, state_key in enumerate(state_keys):
                connection.execute(
                    """
                    INSERT INTO workflow_states (
                        workflow_definition_id, state_key, ordinal,
                        is_initial, is_terminal
                    ) VALUES (?, ?, ?, ?, 0)
                    """,
                    (
                        workflow_definition_id,
                        state_key,
                        ordinal,
                        int(state_key == instance.current_state),
                    ),
                )
            transition_database_id = f"transition-{self._scope_counter}"
            connection.execute(
                """
                INSERT INTO workflow_transitions (
                    id, workflow_definition_id, transition_key,
                    from_state_key, to_state_key, capability_id, result_kind,
                    failure_route, requires_serena_onboarding,
                    output_schema_definition_id, state, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (
                    transition_database_id,
                    workflow_definition_id,
                    transition.transition_id,
                    instance.current_state,
                    to_state,
                    actor.capability_id,
                    transition.result_kinds[0],
                    transition.failure_state,
                    int(bool(transition.serena_onboarding)),
                    output_schema_ref,
                    f"transition:{serial}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO workflow_instances (
                    id, workflow_definition_id, goal_id, run_id,
                    current_state_key, status, idempotency_key,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, NULL)
                """,
                (
                    instance.instance_id,
                    workflow_definition_id,
                    instance.goal_id,
                    instance.run_id,
                    instance.current_state,
                    f"workflow-instance:{serial}",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO repository_registrations (
                    id, target_id, managed_repository_id, canonical_path,
                    git_common_dir, source_oid, state, idempotency_key, registered_at
                ) VALUES (?, ?, NULL, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (
                    evidence.repository.repository_id,
                    instance.target_id,
                    repository_path,
                    f"{repository_path}/.git",
                    evidence.repository.source_oid,
                    f"repository:{serial}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO runtime_leases (
                    id, repository_id, goal_id, run_id, slot_id,
                    worker_assignment_id, lease_kind, branch_ref, worktree_path,
                    base_oid, expected_head_oid, write_roots_json,
                    protected_roots_json, state, expires_at,
                    idempotency_key, created_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, NULL)
                """,
                (
                    evidence.lease_id,
                    evidence.repository.repository_id,
                    instance.goal_id,
                    instance.run_id,
                    actor.slot_id,
                    actor.worker_assignment_id,
                    lease_kind,
                    branch_ref,
                    cwd,
                    evidence.base_oid,
                    evidence.head_oid or evidence.subject_oid,
                    json.dumps(writable_roots, separators=(",", ":")),
                    json.dumps(protected_roots, separators=(",", ":")),
                    "2027-07-21T00:00:00+00:00",
                    f"lease:{serial}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO sandbox_bindings (
                    id, lease_id, repository_id, run_id, slot_id, subject_oid,
                    cwd, source_root, source_read_only, writable_roots_json,
                    backend, attestation_digest, state, idempotency_key,
                    bound_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test', ?, 'ACTIVE', ?, ?, NULL)
                """,
                (
                    evidence.sandbox_binding_id,
                    evidence.lease_id,
                    evidence.repository.repository_id,
                    instance.run_id,
                    actor.slot_id,
                    evidence.subject_oid,
                    cwd,
                    source_roots[0],
                    int(transition.workspace_kind != "developer-worktree"),
                    json.dumps(writable_roots, separators=(",", ":")),
                    hashlib.sha256(f"sandbox:{serial}".encode()).hexdigest(),
                    f"sandbox:{serial}",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO oid_authorities (
                    id, repository_id, goal_id, run_id, lease_id,
                    sandbox_binding_id, authority_kind, oid, evidence_digest,
                    state, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'SUBJECT', ?, ?, 'ACTIVE', ?, ?)
                """,
                (
                    evidence.oid_authority_id,
                    evidence.repository.repository_id,
                    instance.goal_id,
                    instance.run_id,
                    evidence.lease_id,
                    evidence.sandbox_binding_id,
                    evidence.subject_oid,
                    hashlib.sha256(f"oid:{serial}".encode()).hexdigest(),
                    f"oid-authority:{serial}",
                    now,
                ),
            )
            snapshot = evidence.serena_snapshot
            if hasattr(snapshot, "as_contract_binding"):
                snapshot = snapshot.as_contract_binding(
                    consumption_required=(
                        transition.serena_consumption_receipt_required
                    )
                )
            if isinstance(snapshot, Mapping):
                memory_rows = [
                    {
                        "name": item.get("name") or item.get("memory_name"),
                        "sha256": item.get("sha256") or item.get("memory_sha256"),
                    }
                    for item in snapshot.get("memory_bindings", [])
                ]
                manifest_digest = hashlib.sha256(
                    json.dumps(memory_rows, sort_keys=True).encode()
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO serena_onboarding_snapshots (
                        id, repository_id, source_oid, policy_digest,
                        memory_manifest_digest, state, idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'ACCEPTED', ?, ?)
                    """,
                    (
                        snapshot["snapshot_id"],
                        evidence.repository.repository_id,
                        snapshot["source_oid"],
                        snapshot["policy_sha256"],
                        manifest_digest,
                        f"snapshot:{serial}",
                        now,
                    ),
                )
                for ordinal, item in enumerate(memory_rows):
                    connection.execute(
                        """
                        INSERT INTO serena_snapshot_memory_bindings (
                            snapshot_id, ordinal, memory_name, memory_ref, memory_sha256
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot["snapshot_id"],
                            ordinal,
                            item["name"],
                            f"serena://{item['name']}",
                            item["sha256"],
                        ),
                    )
        return store

    def actor(self, capability: str, serial: str) -> ActorBinding:
        slot = self.SLOT_BY_CAPABILITY[capability]
        return ActorBinding(
            activation_id=f"activation-{serial}",
            capability_id=capability,
            slot_key=slot,
            slot_type="fixed",
            seat_id=f"seat-{slot}",
            physical_seat_id=f"physical-{slot}",
            seat_capability_activation_id=f"seat-capability-{serial}",
            worker_id=f"worker-{slot}",
            worker_fingerprint=hashlib.sha256(
                f"{slot}:{capability}:{serial}".encode("utf-8")
            ).hexdigest(),
            worker_fingerprint_id=f"fingerprint-{serial}",
            slot_id=f"slot-{slot}",
            worker_assignment_id=f"assignment-{serial}",
            compiled_profile_ref=str(self.profile),
            compiled_profile_sha256=self.profile_sha256,
            profile_reference_sha256s=tuple(
                hashlib.sha256(f"profile-ref-{index}".encode("utf-8")).hexdigest()
                for index in range(4)
            ),
            selected_skills=(self.professional_skill,),
        )

    def snapshot(self, transition_id: str, subject_oid: str) -> dict[str, object]:
        names = required_memories_for_transition(transition_id)
        return {
            "snapshot_id": f"snapshot-{transition_id}-{subject_oid[:8]}",
            "source_oid": subject_oid,
            "policy_sha256": hashlib.sha256(b"serena-policy").hexdigest(),
            "memory_bindings": [
                {
                    "name": name,
                    "sha256": hashlib.sha256(
                        f"{transition_id}:{name}".encode("utf-8")
                    ).hexdigest(),
                }
                for name in names
            ],
        }

    def evidence(
        self,
        transition_id: str,
        subject_oid: str,
        *,
        failure_oid: str | None = None,
        include_snapshot: bool = True,
        snapshot_override: object | None = None,
        tools_by_server: Mapping[str, tuple[str, ...]] | None = None,
        status_by_server: Mapping[str, str] | None = None,
    ) -> EvidenceSet:
        transition = self.definitions.transition(transition_id)
        required_tools: dict[str, set[str]] = {
            "serena": set(),
            "sequentialthinking": set(),
        }
        for binding_id in (
            *transition.mcp_availability_binding_ids,
            *transition.mcp_required_use_binding_ids,
        ):
            binding = next(
                item
                for item in self.definitions.mcp_policy["required_use_bindings"]
                if item["id"] == binding_id
            )
            for server in binding["server_ids"]:
                required_tools.setdefault(server, set()).update(binding["tool_ids"])
        health = {}
        for server in ("serena", "sequentialthinking"):
            tools = (
                tools_by_server[server]
                if tools_by_server is not None and server in tools_by_server
                else tuple(sorted(required_tools[server]))
            )
            health[server] = {
                "status": (
                    status_by_server[server]
                    if status_by_server is not None and server in status_by_server
                    else "HEALTHY"
                ),
                "tools": list(tools),
                "evidence_digest": hashlib.sha256(
                    f"health:{server}".encode("utf-8")
                ).hexdigest(),
            }
        needs_snapshot = bool(
            transition.serena_onboarding
            or transition.serena_consumption_receipt_required
        )
        snapshot = None
        if include_snapshot and needs_snapshot:
            snapshot = (
                snapshot_override
                if snapshot_override is not None
                else self.snapshot(transition_id, subject_oid)
            )
        worktree = self.root / "workspaces" / transition.workspace_kind
        return EvidenceSet(
            repository=RepositoryBinding("repository-e2e", OID_BASE),
            lease_id=f"lease-{transition_id}",
            sandbox_binding_id=f"sandbox-binding-{transition_id}",
            oid_authority_id="oid-authority-e2e",
            base_oid=OID_BASE,
            subject_oid=subject_oid,
            head_oid=subject_oid,
            integration_oid=(
                subject_oid
                if transition_id
                in {
                    "qa_validate_integration",
                    "build_validate_integration",
                    "pm_accept_integration",
                }
                else None
            ),
            failure_oid=failure_oid,
            workspace={
                "workspace_id": f"workspace-{transition_id}",
                "lease_id": f"lease-{transition_id}",
                "sandbox_id": f"sandbox-{transition_id}",
                "cwd": str(worktree),
                "source_roots": [str(worktree)],
                "writable_roots": (
                    [str(worktree / "src")]
                    if transition.workspace_kind == "developer-worktree"
                    else [str(worktree / "generated")]
                ),
                "protected_roots": [str(self.root / "user-checkout")],
                "prohibited_roots": [
                    str(self.root / "user-checkout"),
                    str(self.root / "sibling-worktree"),
                ],
            },
            mcp_health=health,
            serena_snapshot=snapshot,
            evidence_refs=(f"artifact://{transition_id}/{subject_oid}",),
            issued_at=ISSUED_AT,
        )

    def contract(
        self,
        state: str,
        transition_id: str,
        capability: str,
        subject_oid: str,
        *,
        serial: str | None = None,
        failure_oid: str | None = None,
        include_snapshot: bool = True,
        snapshot_override: object | None = None,
        tools_by_server: Mapping[str, tuple[str, ...]] | None = None,
        status_by_server: Mapping[str, str] | None = None,
        actor_override: ActorBinding | None = None,
        evidence_override: EvidenceSet | None = None,
    ):
        serial = serial or f"{transition_id}-{subject_oid[:8]}"
        actor = actor_override or self.actor(capability, serial)
        evidence = evidence_override or self.evidence(
            transition_id,
            subject_oid,
            failure_oid=failure_oid,
            include_snapshot=include_snapshot,
            snapshot_override=snapshot_override,
            tools_by_server=tools_by_server,
            status_by_server=status_by_server,
        )
        instance = self.instance(state)
        transition = self.definitions.transition(transition_id)
        store = self._durable_scope(
            instance=instance,
            transition=transition,
            actor=actor,
            evidence=evidence,
        )
        instance = replace(instance, state_store=store)
        evidence = replace(
            evidence,
            repository=replace(evidence.repository, state_store=store),
        )
        return compile_transition(
            instance,
            transition,
            actor,
            evidence,
        )

    @staticmethod
    def result_payload(
        contract,
        result_kind: str,
        *,
        result_oid: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, object]:
        if attempt_id is not None:
            broker = McpInvocationBroker(
                state_store=contract.state_store,
                invoker=_FixtureMcpInvoker(),
            )
            mcp_receipts = [
                asdict(receipt)
                for receipt in broker.invoke_required_context(
                    McpInvocationContext(
                        contract_id=contract.contract_id,
                        activation_id=contract.activation_id,
                        attempt_id=attempt_id,
                        repository_id=contract.evidence.repository.repository_id,
                        request_digest=hashlib.sha256(
                            f"fixture-request:{contract.contract_id}:{attempt_id}".encode(
                                "utf-8"
                            )
                        ).hexdigest(),
                    )
                )
            ]
        else:
            mcp_receipts = []
            for binding in contract.document["mcp_bindings"]:
                if not (
                    binding["required_use"] and binding["usage_receipt_required"]
                ):
                    continue
                for tool in binding["tool_ids"]:
                    mcp_receipts.append(
                        {
                            "receipt_id": (
                                f"receipt-{binding['server_id']}-{tool}-"
                                f"{contract.activation_id}"
                            ),
                            "server_id": binding["server_id"],
                            "tool_id": tool,
                            "activation_id": contract.activation_id,
                            "evidence_sha256": hashlib.sha256(
                                f"{contract.activation_id}:{tool}".encode("utf-8")
                            ).hexdigest(),
                        }
                    )
        onboarding = contract.document.get("serena_onboarding")
        serena_receipts = []
        if onboarding is not None and onboarding["consumption_receipt_required"]:
            serena_receipts = [
                {
                    "snapshot_id": onboarding["snapshot_id"],
                    "memory_name": binding["name"],
                    "memory_sha256": binding["sha256"],
                    "consumed_at": ISSUED_AT,
                }
                for binding in onboarding["memory_bindings"]
            ]
        return {
            "schema_version": 4,
            "result_id": f"result-{contract.activation_id}-{result_kind}",
            "contract_id": contract.contract_id,
            "activation_id": contract.activation_id,
            "transition_id": contract.transition.transition_id,
            "capability_id": contract.actor.capability_id,
            "result_kind": result_kind,
            "subject_oid": contract.evidence.subject_oid,
            "result_oid": result_oid or contract.evidence.subject_oid,
            "evidence_refs": [f"artifact://result/{contract.activation_id}"],
            "mcp_usage_receipts": mcp_receipts,
            "serena_consumption_receipts": serena_receipts,
            "format_repair_used": False,
            "token_accounting": {
                "input_tokens": 1,
                "output_tokens": 1,
                "repair_tokens": 0,
            },
            "payload": {},
            "idempotency_key": contract.document["idempotency_key"],
            "completed_at": COMPLETED_AT,
        }

    def accepted_step(
        self,
        state: str,
        transition_id: str,
        capability: str,
        subject_oid: str,
        result_kind: str,
        *,
        serial: str | None = None,
        result_oid: str | None = None,
        failure_oid: str | None = None,
    ) -> tuple[object, TransitionReceipt]:
        contract = self.contract(
            state,
            transition_id,
            capability,
            subject_oid,
            serial=serial,
            failure_oid=failure_oid,
        )
        admission = admit(contract)
        self.owner.assertIsInstance(admission, AdmissionReceipt)
        attempt_id = begin_attempt(
            contract,
            admission,
            backend="codex-test",
            model="test-model",
            input_digest=hashlib.sha256(
                f"attempt:{contract.contract_id}".encode("utf-8")
            ).hexdigest(),
        )
        routed = commit_result(
            ActivationResult(
                contract,
                self.result_payload(
                    contract,
                    result_kind,
                    result_oid=result_oid,
                    attempt_id=attempt_id,
                ),
                attempt_id=attempt_id,
            )
        )
        self.owner.assertIsInstance(routed, TransitionReceipt)
        return contract, routed


class SixSeatContractFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow = SixSeatContractFixture(self)

    def test_full_six_seat_delivery_contract_reaches_completion(self) -> None:
        revision_oid = oid("developer-revision")
        integration_oid = oid("integration-result")
        steps = (
            ("pm_intake", "pm_intake_goal", "pm", OID_BASE, "goal_defined", OID_BASE),
            (
                "pl_assignment",
                "pl_assign_implementation",
                "pl",
                OID_BASE,
                "work_assigned",
                OID_BASE,
            ),
            (
                "dev_implementation",
                "dev_submit_revision",
                "developer",
                revision_oid,
                "revision_submitted",
                revision_oid,
            ),
            (
                "ta_review",
                "ta_review_exact_oid",
                "ta",
                revision_oid,
                "approved",
                revision_oid,
            ),
            (
                "pl_merge",
                "pl_merge_approved_oid",
                "pl",
                revision_oid,
                "merged",
                integration_oid,
            ),
            (
                "qa_validation",
                "qa_validate_integration",
                "qa_sdet",
                integration_oid,
                "approved",
                integration_oid,
            ),
            (
                "build_validation",
                "build_validate_integration",
                "build_release",
                integration_oid,
                "approved",
                integration_oid,
            ),
            (
                "pm_acceptance",
                "pm_accept_integration",
                "pm",
                integration_oid,
                "approved",
                integration_oid,
            ),
        )
        contracts = []
        receipts = []
        for index, step in enumerate(steps):
            contract, receipt = self.flow.accepted_step(
                *step[:-1],
                serial=f"happy-{index}",
                result_oid=step[-1],
            )
            contracts.append(contract)
            receipts.append(receipt)

        self.assertEqual("completed", receipts[-1].to_state)
        self.assertEqual(
            [
                "pl_assignment",
                "dev_implementation",
                "ta_review",
                "pl_merge",
                "qa_validation",
                "build_validation",
                "pm_acceptance",
                "completed",
            ],
            [receipt.to_state for receipt in receipts],
        )
        self.assertTrue(
            all(
                contract.document["git"]["subject_oid"]
                == contract.evidence.subject_oid
                for contract in contracts
            )
        )
        self.assertTrue(
            all(
                len(
                    {
                        contract.document["identity"]["capability_id"],
                    }
                )
                == 1
                for contract in contracts
            )
        )
        slots = self.flow.definitions.slots
        self.assertEqual(5, sum(item["slot_type"] == "fixed" for item in slots.values()))
        self.assertEqual(1, sum(item["slot_type"] == "elastic" for item in slots.values()))
        self.assertEqual(
            ["pm", "ta", "pm"],
            [
                item.actor.capability_id
                for item in contracts
                if item.actor.slot_key == "pm_ta"
            ],
        )
        self.assertEqual(
            ["qa_sdet", "build_release"],
            [
                item.actor.capability_id
                for item in contracts
                if item.actor.slot_key == "qa_build"
            ],
        )

    def test_every_review_or_integration_failure_routes_pl_owned_repair(self) -> None:
        source_before = hashlib.sha256(self.flow.source.read_bytes()).hexdigest()
        scenarios = (
            ("ta_review", "ta_review_exact_oid", "ta", "needs_rework"),
            ("pl_merge", "pl_merge_approved_oid", "pl", "merge_conflict"),
            ("pl_merge", "pl_merge_approved_oid", "pl", "broken_integration"),
            ("qa_validation", "qa_validate_integration", "qa_sdet", "needs_rework"),
            (
                "build_validation",
                "build_validate_integration",
                "build_release",
                "needs_rework",
            ),
        )
        for index, (state, transition_id, capability, failure_kind) in enumerate(scenarios):
            with self.subTest(failure_kind=failure_kind, capability=capability):
                subject_oid = oid(f"failed-subject-{index}")
                failure_oid = oid(f"failure-evidence-{index}")
                failed = self.flow.contract(
                    state,
                    transition_id,
                    capability,
                    subject_oid,
                    serial=f"failure-{index}",
                )
                self.assertFalse(failed.document["authority"]["source_write"])
                admission = admit(failed)
                self.assertIsInstance(admission, AdmissionReceipt)
                attempt_id = begin_attempt(
                    failed,
                    admission,
                    backend="codex-test",
                    model="test-model",
                    input_digest=hashlib.sha256(
                        f"failure-attempt:{failed.contract_id}".encode("utf-8")
                    ).hexdigest(),
                )
                route = commit_result(
                    ActivationResult(
                        failed,
                        self.flow.result_payload(
                            failed,
                            failure_kind,
                            result_oid=failure_oid,
                            attempt_id=attempt_id,
                        ),
                        attempt_id=attempt_id,
                    )
                )
                self.assertIsInstance(route, ReworkRoute)
                self.assertEqual("pl", route.owner_capability)
                self.assertEqual(subject_oid, route.subject_oid)
                self.assertEqual(failure_oid, route.failure_oid)
                self.assertFalse(route.direct_source_repair_allowed)

                repair_oid = oid(f"developer-repair-{index}")
                integration_oid = oid(f"reintegration-{index}")
                pl_contract, pl_receipt = self.flow.accepted_step(
                    "pl_rework",
                    "pl_issue_rework",
                    "pl",
                    failure_oid,
                    "work_assigned",
                    serial=f"pl-repair-{index}",
                    result_oid=failure_oid,
                    failure_oid=failure_oid,
                )
                dev_contract, dev_receipt = self.flow.accepted_step(
                    "dev_rework",
                    "dev_submit_rework",
                    "developer",
                    repair_oid,
                    "revision_submitted",
                    serial=f"dev-repair-{index}",
                    result_oid=repair_oid,
                    failure_oid=failure_oid,
                )
                ta_contract, ta_receipt = self.flow.accepted_step(
                    "ta_review",
                    "ta_review_exact_oid",
                    "ta",
                    repair_oid,
                    "approved",
                    serial=f"ta-rereview-{index}",
                    result_oid=repair_oid,
                    failure_oid=failure_oid,
                )
                merge_contract, merge_receipt = self.flow.accepted_step(
                    "pl_merge",
                    "pl_merge_approved_oid",
                    "pl",
                    repair_oid,
                    "merged",
                    serial=f"pl-remerge-{index}",
                    result_oid=integration_oid,
                    failure_oid=failure_oid,
                )
                qa_contract, qa_receipt = self.flow.accepted_step(
                    "qa_validation",
                    "qa_validate_integration",
                    "qa_sdet",
                    integration_oid,
                    "approved",
                    serial=f"qa-recheck-{index}",
                    result_oid=integration_oid,
                    failure_oid=failure_oid,
                )
                self.assertEqual(
                    [
                        "dev_rework",
                        "ta_review",
                        "pl_merge",
                        "qa_validation",
                        "build_validation",
                    ],
                    [
                        pl_receipt.to_state,
                        dev_receipt.to_state,
                        ta_receipt.to_state,
                        merge_receipt.to_state,
                        qa_receipt.to_state,
                    ],
                )
                self.assertEqual(repair_oid, ta_contract.evidence.subject_oid)
                self.assertEqual(repair_oid, merge_contract.evidence.subject_oid)
                self.assertEqual(integration_oid, qa_contract.evidence.subject_oid)
                self.assertTrue(dev_contract.document["authority"]["source_write"])
                self.assertTrue(
                    all(
                        not contract.document["authority"]["source_write"]
                        for contract in (pl_contract, ta_contract, merge_contract, qa_contract)
                    )
                )

        self.assertEqual(
            source_before,
            hashlib.sha256(self.flow.source.read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
