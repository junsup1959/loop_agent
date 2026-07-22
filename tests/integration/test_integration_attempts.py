from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.agent_team_domain import (
    GateDecision,
    GateDecisionValue,
    GateType,
    IntegrationAttemptState,
    IntegrationPlan,
    IntegrationPlanState,
    McpHealthStatus,
)
from scripts.agent_team_gates import GateCoordinator
from scripts.agent_team_git import ManagedRepositoryService
from scripts.agent_team_integration import (
    IntegrationAuthorizationError,
    IntegrationController,
)
from scripts.agent_team_paths import AxPathAuthority
from scripts.agent_team_runtime import McpInvocationBroker, McpInvocationContext
from scripts.agent_team_state import AxStateStore, utc_now
from scripts.agent_team_workflow import WorkflowDefinitions


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


class _FixtureMcpInvoker:
    def invoke(self, server_name, tool_name, input_payload):
        return {
            "server_name": server_name,
            "tool_name": tool_name,
            "request_digest": input_payload["request_digest"],
        }


class IntegrationHarness:
    """Reusable local-only Git/SQLite fixture for Phase 6 integration tests."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ax-integration-")
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
        self.target = self.service.register_target(
            self.checkout,
            source_ref="refs/heads/main",
            idempotency_key="register-integration-target",
        )
        self.service.import_snapshot(
            self.target.target_id,
            expected_source_oid=self.base_oid,
            idempotency_key="import-integration-target",
        )
        self.gates = GateCoordinator(self.store)
        self.controller = IntegrationController(
            state_store=self.store,
            repository_service=self.service,
            path_authority=self.authority,
            gate_coordinator=self.gates,
        )
        self._insert_goal_and_run()
        self._insert_v4_control_plane()

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
        }

    def create_candidate(
        self,
        ordinal: int,
        *,
        path: str,
        content: str,
        owner: str | None = None,
    ) -> tuple[str, str]:
        owner = owner or f"dev_{ordinal}"
        work_item_id = f"work-{ordinal}"
        revision_id = f"revision-{ordinal}"
        workspace_id = f"workspace-{ordinal}"
        lease_id = f"lease-{ordinal}"
        candidate_id = f"candidate-{ordinal}"
        worktree = self.authority.development_worktree(
            "goal-1", work_item_id, 1
        )
        branch_ref = (
            f"refs/heads/ax/work/goal-1/{work_item_id}/1"
        )
        receipt = self.service.create_disposable_worktree(
            self.target.target_id,
            oid=self.base_oid,
            path=worktree,
            branch_ref=branch_ref,
        )
        target_path = worktree / path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
        run_git(worktree, "config", "user.name", "AX Candidate Fixture")
        run_git(worktree, "config", "user.email", "candidate@example.invalid")
        run_git(worktree, "add", "--", path)
        run_git(worktree, "commit", "-m", f"candidate {ordinal}")
        candidate_oid = run_git(
            worktree, "rev-parse", "HEAD"
        ).stdout.strip()
        self.service.remove_disposable_worktree(
            receipt, expected_oid=candidate_oid
        )

        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO work_items (
                    id, goal_id, title, assigned_owner,
                    source_write_scope_json, state, created_at, updated_at
                ) VALUES (?, 'goal-1', ?, ?, ?, 'REVIEW_PENDING', ?, ?)
                """,
                (
                    work_item_id,
                    f"Candidate {ordinal}",
                    owner,
                    json.dumps([path]),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO work_revisions (
                    id, work_item_id, revision, owner, base_oid, head_oid, state,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?, 'SUBMITTED', ?, ?, ?)
                """,
                (
                    revision_id,
                    work_item_id,
                    owner,
                    self.base_oid,
                    candidate_oid,
                    f"revision-key:{ordinal}",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, target_id, goal_id, kind, path, branch_ref, subject_oid,
                    state, created_at, updated_at
                ) VALUES (
                    ?, ?, 'goal-1', 'DEVELOPMENT', ?, ?, ?,
                    'RELEASED', ?, ?
                )
                """,
                (
                    workspace_id,
                    self.target.target_id,
                    str(worktree),
                    branch_ref,
                    candidate_oid,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO workspace_leases (
                    id, workspace_id, target_id, goal_id, work_item_id,
                    revision_id, owner, branch_ref, worktree_path, base_oid,
                    expected_head_oid, source_write_scope_json,
                    generated_write_scope_json, state, expires_at,
                    idempotency_key, created_at, released_at
                ) VALUES (
                    ?, ?, ?, 'goal-1', ?, ?, ?, ?, ?, ?, ?, ?, '[]',
                    'RELEASED', ?, ?, ?, ?
                )
                """,
                (
                    lease_id,
                    workspace_id,
                    self.target.target_id,
                    work_item_id,
                    revision_id,
                    owner,
                    branch_ref,
                    str(worktree),
                    self.base_oid,
                    candidate_oid,
                    json.dumps([path]),
                    now,
                    f"lease-key:{ordinal}",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO candidate_submissions (
                    id, goal_id, work_item_id, revision_id, lease_id, branch_ref,
                    expected_previous_oid, candidate_oid,
                    self_test_evidence_json, state, idempotency_key, created_at
                ) VALUES (
                    ?, 'goal-1', ?, ?, ?, ?, ?, ?,
                    '["self-test"]', 'SUBMITTED', ?, ?
                )
                """,
                (
                    candidate_id,
                    work_item_id,
                    revision_id,
                    lease_id,
                    branch_ref,
                    self.base_oid,
                    candidate_oid,
                    f"candidate-key:{ordinal}",
                    now,
                ),
            )
        profile_digest = self.record_delivery_v4_result(
            transition_id="ta_review_exact_oid",
            capability_id="ta",
            subject_oid=candidate_oid,
            result_kind="approved",
            suffix=f"ta-{ordinal}",
            base_oid=self.base_oid,
        )
        self._approve_candidate(
            ordinal, candidate_id, candidate_oid, profile_digest
        )
        return candidate_id, candidate_oid

    def approve_plan(
        self,
        candidate_ids: tuple[str, ...],
        candidate_oids: tuple[str, ...],
        *,
        plan_id: str = "plan-1",
        attempt_id: str = "attempt-1",
    ) -> IntegrationPlan:
        activation_id = "activation-pl-selection"
        self._insert_activation(
            activation_id,
            role="pl",
            subject_oid=self.base_oid,
            gate_or_task="candidate-selection",
        )
        selection = self.gates.record_decision(
            GateDecision(
                decision_id="gate-pl-selection",
                goal_id="goal-1",
                activation_id=activation_id,
                gate_type=GateType.PL_CANDIDATE_SELECTION,
                actor_role="pl",
                subject_oid=self.base_oid,
                decision=GateDecisionValue.APPROVED,
                profile_digest="profile-digest",
                evidence_ids=candidate_ids,
                idempotency_key="decision-key:gate-pl-selection",
            )
        )
        return IntegrationPlan(
            plan_id=plan_id,
            attempt_id=attempt_id,
            goal_id="goal-1",
            base_oid=self.base_oid,
            ordered_candidate_oids=candidate_oids,
            merge_strategy="no-ff",
            pl_decision_id=selection.decision_id,
            idempotency_key=f"integration-key:{attempt_id}",
            state=IntegrationPlanState.APPROVED,
        )

    def clone_plan_for_attempt(
        self, plan: IntegrationPlan, attempt_id: str
    ) -> IntegrationPlan:
        return IntegrationPlan(
            plan_id=plan.plan_id,
            attempt_id=attempt_id,
            goal_id=plan.goal_id,
            base_oid=plan.base_oid,
            ordered_candidate_oids=plan.ordered_candidate_oids,
            merge_strategy=plan.merge_strategy,
            pl_decision_id=plan.pl_decision_id,
            idempotency_key=f"integration-key:{attempt_id}",
            state=IntegrationPlanState.APPROVED,
        )

    def _insert_goal_and_run(self) -> None:
        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO goals (
                    id, target_id, base_oid, state, created_at, updated_at
                ) VALUES ('goal-1', ?, ?, 'ACTIVE', ?, ?)
                """,
                (self.target.target_id, self.base_oid, now, now),
            )
            connection.execute(
                """
                INSERT INTO runs (
                    id, goal_id, target_id, base_oid, state,
                    idempotency_key, created_at
                ) VALUES (
                    'run-1', 'goal-1', ?, ?, 'RUNNING', 'run-key-1', ?
                )
                """,
                (self.target.target_id, self.base_oid, now),
            )

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _insert_v4_control_plane(self) -> None:
        self.v4_definitions = WorkflowDefinitions.load()
        workflow_definition = self.store.register_definition(
            kind="WORKFLOW",
            version="4.0.0",
            sha256=self.v4_definitions.workflow_sha256,
            source_ref="agents/workflows/delivery-v4.toml",
        )
        self.v4_contract_definition = self.store.register_definition(
            kind="SCHEMA",
            version="integration-fixture-contract-v4",
            sha256=self._digest("integration-fixture-contract-v4"),
            source_ref="agents/contracts/schemas/activation-contract.schema.json",
        )
        output_path = self.v4_definitions.activation_result_schema_path
        self.v4_output_definition = self.store.register_definition(
            kind="SCHEMA",
            version="integration-fixture-result-v4",
            sha256=hashlib.sha256(output_path.read_bytes()).hexdigest(),
            source_ref="agents/contracts/schemas/activation-result.schema.json",
        )
        self.v4_profile_definition = self.store.register_definition(
            kind="PROFILE",
            version="integration-fixture-profile-v4",
            sha256=self._digest("integration-fixture-profile-v4"),
            source_ref=".codex/profiles/integration-fixture.json",
        )
        now = utc_now()
        capabilities = ("ta", "pl", "qa_sdet", "build_release", "pm")
        with self.store.transaction(immediate=True) as connection:
            for capability in capabilities:
                capability_id = f"capability-{capability}"
                seat_id = f"seat-{capability}"
                slot_id = f"slot-{capability}"
                worker_id = f"worker-{capability}"
                fingerprint_id = f"fingerprint-{capability}"
                assignment_id = f"assignment-{capability}"
                connection.execute(
                    """
                    INSERT INTO physical_seats (
                        id, seat_key, state, is_merged, idempotency_key, created_at
                    ) VALUES (?, ?, 'ACTIVE', 0, ?, ?)
                    """,
                    (seat_id, f"fixture-{capability}", f"seat-key-{capability}", now),
                )
                connection.execute(
                    """
                    INSERT INTO logical_capabilities (
                        id, capability_key, state, approval_authority,
                        merge_authority, nested_spawn_authority,
                        idempotency_key, created_at
                    ) VALUES (?, ?, 'ACTIVE', ?, ?, 0, ?, ?)
                    """,
                    (
                        capability_id,
                        capability,
                        int(capability != "pl"),
                        int(capability == "pl"),
                        f"capability-key-{capability}",
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO seat_capability_ownerships (
                        physical_seat_id, capability_id, state,
                        idempotency_key, assigned_at
                    ) VALUES (?, ?, 'ENABLED', ?, ?)
                    """,
                    (seat_id, capability_id, f"ownership-{capability}", now),
                )
                connection.execute(
                    """
                    INSERT INTO runtime_slots (
                        id, slot_key, kind, physical_seat_id,
                        elastic_singleton, state, idempotency_key, created_at
                    ) VALUES (?, ?, 'FIXED', ?, NULL, 'OCCUPIED', ?, ?)
                    """,
                    (slot_id, f"fixture-{capability}", seat_id,
                     f"slot-key-{capability}", now),
                )
                connection.execute(
                    """
                    INSERT INTO worker_identities (
                        id, worker_key, kind, physical_seat_id, state,
                        idempotency_key, created_at
                    ) VALUES (?, ?, 'FIXED', ?, 'ACTIVE', ?, ?)
                    """,
                    (worker_id, f"fixture-{capability}", seat_id,
                     f"worker-key-{capability}", now),
                )
                connection.execute(
                    """
                    INSERT INTO worker_fingerprints (
                        id, worker_id, fingerprint_sha256,
                        runtime_profile_digest, state, idempotency_key,
                        created_at, revoked_at
                    ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, NULL)
                    """,
                    (
                        fingerprint_id,
                        worker_id,
                        self._digest(f"fingerprint-{capability}"),
                        self._digest(f"runtime-profile-{capability}"),
                        f"fingerprint-key-{capability}",
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO worker_slot_assignments (
                        id, worker_id, worker_fingerprint_id, slot_id, run_id,
                        is_elastic, state, idempotency_key, assigned_at, released_at
                    ) VALUES (?, ?, ?, ?, 'run-1', 0, 'ACTIVE', ?, ?, NULL)
                    """,
                    (assignment_id, worker_id, fingerprint_id, slot_id,
                     f"assignment-key-{capability}", now),
                )
                connection.execute(
                    """
                    INSERT INTO seat_capability_activations (
                        id, physical_seat_id, capability_id, slot_id,
                        worker_assignment_id, goal_id, run_id, state,
                        idempotency_key, activated_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, 'goal-1', 'run-1', 'ACTIVE',
                              ?, ?, NULL)
                    """,
                    (f"seat-activation-{capability}", seat_id, capability_id,
                     slot_id, assignment_id, f"seat-activation-key-{capability}", now),
                )
            connection.execute(
                """
                INSERT INTO workflow_definitions (
                    id, definition_id, workflow_key, version, state,
                    idempotency_key, created_at
                ) VALUES (
                    'workflow-definition-delivery-v4', ?, 'delivery-v4',
                    '4.0.0', 'ACTIVE', 'workflow-definition-delivery-v4-key', ?
                )
                """,
                (workflow_definition, now),
            )
            states = tuple(
                item
                for item in (
                    "pm_intake", "pl_assignment", "dev_implementation",
                    "dev_rework", "ta_review", "pl_merge", "qa_validation",
                    "build_validation", "pm_acceptance", "pl_rework",
                    "completed", "blocked", "quarantined",
                )
            )
            for ordinal, state in enumerate(states):
                connection.execute(
                    """
                    INSERT INTO workflow_states (
                        workflow_definition_id, state_key, ordinal,
                        is_initial, is_terminal
                    ) VALUES ('workflow-definition-delivery-v4', ?, ?, ?, ?)
                    """,
                    (state, ordinal, int(state == "pm_intake"),
                     int(state in {"completed", "blocked", "quarantined"})),
                )
            for transition_id in (
                "ta_review_exact_oid", "qa_validate_integration",
                "build_validate_integration", "pm_accept_integration",
            ):
                transition = self.v4_definitions.transition(transition_id)
                capability = transition.capabilities[0]
                connection.execute(
                    """
                    INSERT INTO workflow_transitions (
                        id, workflow_definition_id, transition_key,
                        from_state_key, to_state_key, capability_id,
                        result_kind, failure_route, requires_serena_onboarding,
                        output_schema_definition_id, state,
                        idempotency_key, created_at
                    ) VALUES (?, 'workflow-definition-delivery-v4', ?, ?, ?, ?,
                              'activation-result', ?, 0, ?, 'ACTIVE', ?, ?)
                    """,
                    (
                        f"transition-{transition_id}", transition_id,
                        transition.from_states[0], transition.to_state,
                        f"capability-{capability}", transition.failure_state,
                        self.v4_output_definition,
                        f"transition-key-{transition_id}", now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO workflow_instances (
                    id, workflow_definition_id, goal_id, run_id,
                    current_state_key, status, idempotency_key,
                    created_at, updated_at, completed_at
                ) VALUES (
                    'workflow-instance-delivery-v4',
                    'workflow-definition-delivery-v4', 'goal-1', 'run-1',
                    'pm_intake', 'ACTIVE', 'workflow-instance-delivery-v4-key',
                    ?, ?, NULL
                )
                """,
                (now, now),
            )

    def record_delivery_v4_result(
        self,
        *,
        transition_id: str,
        capability_id: str,
        subject_oid: str,
        result_kind: str,
        suffix: str,
        base_oid: str | None = None,
    ) -> str:
        transition = self.v4_definitions.transition(transition_id)
        self.assertIn(capability_id, transition.capabilities)
        base = base_oid or self.base_oid
        contract_id = f"contract-{suffix}"
        lease_id = f"review-lease-{suffix}"
        sandbox_id = f"review-sandbox-{suffix}"
        authority_id = f"review-authority-{suffix}"
        slot_id = f"slot-{capability_id}"
        assignment_id = f"assignment-{capability_id}"
        repository_id = self.target.target_id
        review_root = self.authority.review_source_root(contract_id)
        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO runtime_leases (
                    id, repository_id, goal_id, run_id, slot_id,
                    worker_assignment_id, lease_kind, branch_ref,
                    worktree_path, base_oid, expected_head_oid,
                    write_roots_json, protected_roots_json, state,
                    expires_at, idempotency_key, created_at, released_at
                ) VALUES (?, ?, 'goal-1', 'run-1', ?, ?, 'REVIEW', NULL,
                          ?, ?, ?, '[]', '[]', 'ACTIVE', ?, ?, ?, NULL)
                """,
                (lease_id, repository_id, slot_id, assignment_id,
                 str(review_root), base, subject_oid,
                 "2027-07-21T00:00:00+00:00", f"review-lease-key-{suffix}", now),
            )
            connection.execute(
                """
                INSERT INTO sandbox_bindings (
                    id, lease_id, repository_id, run_id, slot_id, subject_oid,
                    cwd, source_root, source_read_only, writable_roots_json,
                    backend, attestation_digest, state, idempotency_key,
                    bound_at, released_at
                ) VALUES (?, ?, ?, 'run-1', ?, ?, ?, ?, 1, '[]',
                          'fixture-review', ?, 'ACTIVE', ?, ?, NULL)
                """,
                (sandbox_id, lease_id, repository_id, slot_id, subject_oid,
                 str(review_root), str(review_root), self._digest(f"sandbox-{suffix}"),
                 f"sandbox-key-{suffix}", now),
            )
            connection.execute(
                """
                INSERT INTO oid_authorities (
                    id, repository_id, goal_id, run_id, lease_id,
                    sandbox_binding_id, authority_kind, oid, evidence_digest,
                    state, idempotency_key, created_at
                ) VALUES (?, ?, 'goal-1', 'run-1', ?, ?, 'SUBJECT', ?, ?,
                          'ACTIVE', ?, ?)
                """,
                (authority_id, repository_id, lease_id, sandbox_id, subject_oid,
                 self._digest(f"authority-{suffix}"),
                 f"authority-key-{suffix}", now),
            )
        contract_digest = self._digest(f"contract-digest-{suffix}")
        profile_digest = self._digest(f"compiled-profile-{capability_id}")
        self.store.register_activation_contract(
            contract_id=contract_id,
            workflow_instance_id="workflow-instance-delivery-v4",
            workflow_transition_id=f"transition-{transition_id}",
            goal_id="goal-1", run_id="run-1",
            physical_seat_id=f"seat-{capability_id}",
            capability_id=f"capability-{capability_id}",
            seat_capability_activation_id=f"seat-activation-{capability_id}",
            worker_id=f"worker-{capability_id}",
            worker_fingerprint_id=f"fingerprint-{capability_id}",
            slot_id=slot_id, worker_assignment_id=assignment_id,
            repository_id=repository_id, lease_id=lease_id,
            sandbox_binding_id=sandbox_id, oid_authority_id=authority_id,
            base_oid=base, subject_oid=subject_oid,
            contract_definition_id=self.v4_contract_definition,
            output_schema_definition_id=self.v4_output_definition,
            contract_digest=contract_digest,
            packet_digest=self._digest(f"packet-{suffix}"),
            context_char_budget=1000, max_attempts=2,
            idempotency_key=f"contract-key-{suffix}",
        )
        self.store.bind_contract_profile(
            contract_id=contract_id,
            profile_definition_id=self.v4_profile_definition,
            compiled_profile_ref=f".codex/profiles/{capability_id}.json",
            compiled_profile_digest=profile_digest,
        )
        mcp_bindings: list[tuple[str, bool, str]] = []
        for server in ("serena", "sequentialthinking"):
            tool = "server-health"
            definition_id = self.store.register_mcp_definition(
                server_name=server, tool_name=tool, version="fixture-health-v1",
                sha256=self._digest(f"mcp-{server}-{tool}"),
            )
            binding_id = self.store.bind_contract_mcp(
                contract_id=contract_id, mcp_definition_id=definition_id,
                required_availability=True, invocation_required=False,
                trigger_rule="required-mcp-health",
            )
            self.store.record_mcp_health_observation(
                mcp_definition_id=definition_id, contract_id=contract_id,
                status=McpHealthStatus.HEALTHY,
                evidence_digest=self._digest(f"health-{suffix}-{server}-{tool}"),
                idempotency_key=f"health-{suffix}-{server}-{tool}",
            )
            mcp_bindings.append((binding_id, False, tool))
        if transition.mcp_required_use_binding_ids:
            for tool in ("initial_instructions", "find_symbol", "find_referencing_symbols"):
                definition_id = self.store.register_mcp_definition(
                    server_name="serena", tool_name=tool,
                    version="fixture-semantic-v1",
                    sha256=self._digest(f"mcp-serena-{tool}"),
                )
                binding_id = self.store.bind_contract_mcp(
                    contract_id=contract_id, mcp_definition_id=definition_id,
                    required_availability=True, invocation_required=True,
                    trigger_rule=transition.mcp_required_use_binding_ids[0],
                )
                self.store.record_mcp_health_observation(
                    mcp_definition_id=definition_id, contract_id=contract_id,
                    status=McpHealthStatus.HEALTHY,
                    evidence_digest=self._digest(f"health-{suffix}-{tool}"),
                    idempotency_key=f"health-{suffix}-{tool}",
                )
                mcp_bindings.append((binding_id, True, tool))
        self.store.record_contract_admission(
            contract_id=contract_id, accepted=True, reason_code=None
        )
        attempt_id = self.store.record_contract_attempt(
            contract_id=contract_id, backend="fixture", model="fixture-model",
            input_digest=self._digest(f"input-{suffix}"),
        )
        required_invocations = sum(invoked for _, invoked, _ in mcp_bindings)
        if required_invocations:
            broker = McpInvocationBroker(
                state_store=self.store,
                invoker=_FixtureMcpInvoker(),
            )
            receipts = broker.invoke_required_context(
                McpInvocationContext(
                    contract_id=contract_id,
                    activation_id=f"activation-{suffix}",
                    attempt_id=attempt_id,
                    repository_id=repository_id,
                    request_digest=self._digest(f"input-{suffix}"),
                )
            )
            self.assertEqual(required_invocations, len(receipts))
        payload = {
            "schema_version": 4,
            "result_id": f"result-{suffix}",
            "contract_id": contract_id,
            "activation_id": f"activation-{suffix}",
            "transition_id": transition_id,
            "capability_id": capability_id,
            "result_kind": result_kind,
            "subject_oid": subject_oid,
            "result_oid": subject_oid,
            "evidence_refs": [f"evidence-{suffix}"],
            "mcp_usage_receipts": [],
            "serena_consumption_receipts": [],
            "token_accounting": {
                "input_tokens": 1, "output_tokens": 1, "repair_tokens": 0
            },
            "payload": {"lease_id": lease_id},
            "idempotency_key": f"contract-key-{suffix}",
        }
        self.store.record_activation_result(
            attempt_id=attempt_id, result_kind=result_kind,
            output_digest=self._digest(json.dumps(payload, sort_keys=True)),
            evidence_digest=self._digest(f"result-evidence-{suffix}"),
            payload=payload, accepted=True,
            idempotency_key=f"result-key-{suffix}",
        )
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE sandbox_bindings SET state = 'RELEASED', released_at = ?
                WHERE id = ? AND state = 'ACTIVE'
                """,
                (utc_now(), sandbox_id),
            )
            connection.execute(
                """
                UPDATE runtime_leases SET state = 'RELEASED', released_at = ?
                WHERE id = ? AND state = 'ACTIVE'
                """,
                (utc_now(), lease_id),
            )
        return profile_digest

    def _approve_candidate(
        self,
        ordinal: int,
        candidate_id: str,
        candidate_oid: str,
        profile_digest: str,
    ) -> None:
        for suffix, gate_type, review_type in (
            ("code", GateType.TA_CODE_QUALITY, "CODE_QUALITY"),
            ("architecture", GateType.TA_ARCHITECTURE, "ARCHITECTURE"),
        ):
            activation_id = f"activation-ta-{ordinal}-{suffix}"
            review_id = f"review-ta-{ordinal}-{suffix}"
            self._insert_activation(
                activation_id,
                role="ta",
                subject_oid=candidate_oid,
                gate_or_task=f"{suffix}-review",
                seat_id="ta_1",
            )
            with self.store.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO reviews (
                        id, goal_id, candidate_id, activation_id, reviewer_role,
                        review_type, subject_oid, decision, source_integrity,
                        profile_digest, evidence_json, idempotency_key, created_at
                    ) VALUES (
                        ?, 'goal-1', ?, ?, 'ta', ?, ?, 'APPROVED', 'CLEAN',
                        ?, '["review-artifact"]', ?, ?
                    )
                    """,
                    (
                        review_id,
                        candidate_id,
                        activation_id,
                        review_type,
                        candidate_oid,
                        profile_digest,
                        f"review-key:{review_id}",
                        utc_now(),
                    ),
                )
            self.gates.record_decision(
                GateDecision(
                    decision_id=f"gate-ta-{ordinal}-{suffix}",
                    goal_id="goal-1",
                    activation_id=activation_id,
                    gate_type=gate_type,
                    actor_role="ta",
                    subject_oid=candidate_oid,
                    decision=GateDecisionValue.APPROVED,
                    profile_digest=profile_digest,
                    evidence_ids=(review_id,),
                    idempotency_key=f"decision-key:gate-ta-{ordinal}-{suffix}",
                )
            )

    def _insert_activation(
        self,
        activation_id: str,
        *,
        role: str,
        subject_oid: str,
        gate_or_task: str,
        seat_id: str | None = None,
    ) -> None:
        now = utc_now()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO activations (
                    id, target_id, goal_id, run_id, sandbox_path, subject_oid,
                    role, gate_or_task, state, result_json, idempotency_key,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, 'goal-1', 'run-1', ?, ?, ?, ?,
                    'RESULT_PERSISTED', ?, ?, ?, ?
                )
                """,
                (
                    activation_id,
                    self.target.target_id,
                    str(self.authority.review_sandbox(activation_id)),
                    subject_oid,
                    role,
                    gate_or_task,
                    json.dumps({"seat_id": seat_id} if seat_id else {}),
                    f"activation-key:{activation_id}",
                    now,
                    now,
                ),
            )


class IntegrationAttemptTests(IntegrationHarness, unittest.TestCase):
    def test_clean_multi_candidate_merge_is_deterministic_and_preserves_checkout(self) -> None:
        first_id, first_oid = self.create_candidate(
            1, path="feature-one.txt", content="one\n"
        )
        second_id, second_oid = self.create_candidate(
            2, path="feature-two.txt", content="two\n"
        )
        plan = self.approve_plan(
            (first_id, second_id), (first_oid, second_oid)
        )
        before = self.checkout_state()

        first = self.controller.execute(plan)
        replay = self.controller.execute(plan)
        second_attempt = self.controller.execute(
            self.clone_plan_for_attempt(plan, "attempt-2")
        )

        self.assertEqual(first, replay)
        self.assertEqual(IntegrationAttemptState.QA_PENDING, first.state)
        self.assertEqual(first.result_oid, second_attempt.result_oid)
        self.assertEqual(before, self.checkout_state())
        self.assertFalse(
            self.authority.integration_worktree("goal-1", "attempt-1").exists()
        )
        managed = Path(self.target.managed_repository_path)
        self.assertEqual(
            first.result_oid,
            run_git(
                managed,
                f"--git-dir={managed}",
                "show-ref",
                "--verify",
                "--hash",
                "refs/agentic-ax/integration/goal-1/attempt-1",
            ).stdout.strip(),
        )
        tree = run_git(
            managed,
            f"--git-dir={managed}",
            "ls-tree",
            "-r",
            "--name-only",
            first.result_oid,
        ).stdout.splitlines()
        self.assertIn("feature-one.txt", tree)
        self.assertIn("feature-two.txt", tree)

    def test_conflict_captures_index_logs_refs_and_never_edits_source(self) -> None:
        first_id, first_oid = self.create_candidate(
            1, path="README.md", content="candidate one\n"
        )
        second_id, second_oid = self.create_candidate(
            2, path="README.md", content="candidate two\n"
        )
        plan = self.approve_plan(
            (first_id, second_id), (first_oid, second_oid)
        )
        before = self.checkout_state()

        attempt = self.controller.execute(plan)

        self.assertEqual(IntegrationAttemptState.REWORK_REQUIRED, attempt.state)
        self.assertIsNone(attempt.result_oid)
        self.assertEqual(before, self.checkout_state())
        self.assertFalse(
            self.authority.integration_worktree("goal-1", "attempt-1").exists()
        )
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM integration_attempts WHERE id = 'attempt-1'"
            ).fetchone()
            rework = connection.execute(
                """
                SELECT * FROM reconciliation_findings
                WHERE resource_type = 'pl-rework-request'
                """
            ).fetchone()
        evidence = json.loads(row["evidence_json"])
        conflict = evidence["conflict"]
        self.assertEqual(self.base_oid, conflict["base_oid"])
        self.assertEqual(second_oid, conflict["candidate_oid"])
        self.assertEqual([first_oid, second_oid], conflict["ordered_candidate_oids"])
        self.assertEqual(["README.md"], conflict["conflict_paths"])
        self.assertEqual({1, 2, 3}, {
            item["stage"] for item in conflict["unmerged_index"]
        })
        self.assertTrue(conflict["commands"])
        self.assertTrue(conflict["git_version"].startswith("git version"))
        self.assertEqual(64, len(conflict["environment_fingerprint"]))
        managed = Path(self.target.managed_repository_path)
        self.assertEqual(
            conflict["partial_head_oid"],
            run_git(
                managed,
                f"--git-dir={managed}",
                "show-ref",
                "--verify",
                "--hash",
                conflict["failed_ref"],
            ).stdout.strip(),
        )
        self.assertIsNotNone(rework)

    def test_controller_rejects_candidate_order_not_approved_by_pl(self) -> None:
        first_id, first_oid = self.create_candidate(
            1, path="one.txt", content="one\n"
        )
        second_id, second_oid = self.create_candidate(
            2, path="two.txt", content="two\n"
        )
        plan = self.approve_plan(
            (first_id, second_id), (first_oid, second_oid)
        )
        unauthorized = IntegrationPlan(
            plan_id=plan.plan_id,
            attempt_id="attempt-reversed",
            goal_id=plan.goal_id,
            base_oid=plan.base_oid,
            ordered_candidate_oids=(second_oid, first_oid),
            merge_strategy=plan.merge_strategy,
            pl_decision_id=plan.pl_decision_id,
            idempotency_key="integration-key:attempt-reversed",
            state=IntegrationPlanState.APPROVED,
        )
        with self.assertRaises(IntegrationAuthorizationError):
            self.controller.execute(unauthorized)


if __name__ == "__main__":
    unittest.main()
