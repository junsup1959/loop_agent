from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.agent_team_domain import (
    ContractAttemptKind,
    ContractViolationCode,
    McpHealthStatus,
    ResultDisposition,
    TokenLedgerEntryKind,
)
from scripts.agent_team_state import (
    AxStateStore,
    IdempotencyConflictError,
    IntentStateError,
    LATEST_SCHEMA_VERSION,
)
from scripts.agent_team_runtime import (
    McpInvocationBroker,
    McpInvocationContext,
    RunnerContractError,
)


OID_A = "a" * 40
OID_B = "b" * 40
NOW = "2026-07-21T00:00:00.000000+00:00"
EXPIRES_AT = "2027-07-21T00:00:00.000000+00:00"


def digest(value: int) -> str:
    return f"{value:064x}"


WORKFLOW_DEFINITION_VERSION = "delivery-v4"
WORKFLOW_DEFINITION_DIGEST = digest(1)
WORKFLOW_DEFINITION_SOURCE_REF = "definitions/workflows/delivery-v4.json"
CONTRACT_SCHEMA_DIGEST = digest(2)
OUTPUT_SCHEMA_DIGEST = digest(3)
PROFILE_DEFINITION_DIGEST = digest(4)
SKILL_DEFINITION_DIGEST = digest(5)
CLAUSE_DEFINITION_DIGEST = digest(6)
MCP_DEFINITION_DIGEST = digest(80)


class _StateFixtureMcpInvoker:
    def invoke(self, server_name, tool_name, input_payload):
        return {
            "server_name": server_name,
            "tool_name": tool_name,
            "request_digest": input_payload["request_digest"],
        }


class AxStateV4ConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ax-state-v4-")
        self.addCleanup(self.temporary.cleanup)
        self.store = AxStateStore(Path(self.temporary.name) / "ax.db")
        self.assertEqual(LATEST_SCHEMA_VERSION, self.store.initialize())
        self.ids = self._seed_control_plane_graph()
        self._mcp_receipt_digests: dict[str, str] = {}

    def _seed_control_plane_graph(self) -> dict[str, str]:
        definitions = {
            "workflow": self.store.register_definition(
                kind="WORKFLOW",
                version=WORKFLOW_DEFINITION_VERSION,
                sha256=WORKFLOW_DEFINITION_DIGEST,
                source_ref=WORKFLOW_DEFINITION_SOURCE_REF,
            ),
            "contract": self.store.register_definition(
                kind="SCHEMA",
                version="activation-contract-v4",
                sha256=CONTRACT_SCHEMA_DIGEST,
                source_ref="definitions/schemas/activation-contract-v4.json",
            ),
            "output": self.store.register_definition(
                kind="SCHEMA",
                version="activation-result-v4",
                sha256=OUTPUT_SCHEMA_DIGEST,
                source_ref="definitions/schemas/activation-result-v4.json",
            ),
            "profile": self.store.register_definition(
                kind="PROFILE",
                version="developer-profile-v4",
                sha256=PROFILE_DEFINITION_DIGEST,
                source_ref="definitions/profiles/developer-v4.json",
            ),
            "skill": self.store.register_definition(
                kind="SKILL",
                version="implementation-skill-v4",
                sha256=SKILL_DEFINITION_DIGEST,
                source_ref="definitions/skills/implementation-v4.md",
            ),
            "clause": self.store.register_definition(
                kind="CLAUSE",
                version="contract-clause-v4",
                sha256=CLAUSE_DEFINITION_DIGEST,
                source_ref="definitions/clauses/contract-v4.md",
            ),
        }

        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO targets (
                    id, canonical_checkout_path, git_common_dir, source_ref,
                    observed_source_oid, state, created_at, updated_at
                ) VALUES (
                    'target-v4', 'C:/fixture-v4', 'C:/fixture-v4/.git',
                    'refs/heads/main', ?, 'ACTIVE', ?, ?
                )
                """,
                (OID_A, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO goals (
                    id, target_id, base_oid, state, created_at, updated_at
                ) VALUES ('goal-v4', 'target-v4', ?, 'ACTIVE', ?, ?)
                """,
                (OID_A, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO runs (
                    id, goal_id, target_id, base_oid, state,
                    idempotency_key, created_at
                ) VALUES (
                    'run-v4', 'goal-v4', 'target-v4', ?, 'RUNNING',
                    'run-v4-key', ?
                )
                """,
                (OID_A, NOW),
            )
            connection.execute(
                """
                INSERT INTO activations (
                    id, target_id, goal_id, run_id, workspace_id, sandbox_path,
                    subject_oid, role, gate_or_task, state, process_id,
                    result_json, idempotency_key, created_at, updated_at
                ) VALUES (
                    'activation-v4', 'target-v4', 'goal-v4', 'run-v4', NULL,
                    'C:/fixture-v4/.ax/worktrees/v4', ?, 'developer',
                    'implementation', 'RUNNING', NULL, NULL,
                    'activation-v4-key', ?, ?
                )
                """,
                (OID_B, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO physical_seats (
                    id, seat_key, state, is_merged, idempotency_key, created_at
                ) VALUES (
                    'seat-v4', 'developer-seat', 'ACTIVE', 0,
                    'seat-v4-key', ?
                )
                """,
                (NOW,),
            )
            for capability_id, capability_key in (
                ("capability-v4", "implementation"),
                ("capability-extra-v4", "documentation"),
            ):
                connection.execute(
                    """
                    INSERT INTO logical_capabilities (
                        id, capability_key, state, approval_authority,
                        merge_authority, nested_spawn_authority,
                        idempotency_key, created_at
                    ) VALUES (?, ?, 'ACTIVE', 0, 0, 0, ?, ?)
                    """,
                    (
                        capability_id,
                        capability_key,
                        f"{capability_id}-key",
                        NOW,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO seat_capability_ownerships (
                        physical_seat_id, capability_id, state,
                        idempotency_key, assigned_at
                    ) VALUES ('seat-v4', ?, 'ENABLED', ?, ?)
                    """,
                    (capability_id, f"ownership-{capability_id}", NOW),
                )
            connection.execute(
                """
                INSERT INTO runtime_slots (
                    id, slot_key, kind, physical_seat_id, elastic_singleton,
                    state, idempotency_key, created_at
                ) VALUES (
                    'slot-v4', 'fixed-developer-slot', 'FIXED', 'seat-v4', NULL,
                    'AVAILABLE', 'slot-v4-key', ?
                )
                """,
                (NOW,),
            )
            connection.execute(
                """
                INSERT INTO worker_identities (
                    id, worker_key, kind, physical_seat_id, state,
                    idempotency_key, created_at
                ) VALUES (
                    'worker-v4', 'developer-worker', 'FIXED', 'seat-v4',
                    'ACTIVE', 'worker-v4-key', ?
                )
                """,
                (NOW,),
            )
            connection.execute(
                """
                INSERT INTO worker_fingerprints (
                    id, worker_id, fingerprint_sha256, runtime_profile_digest,
                    state, idempotency_key, created_at, revoked_at
                ) VALUES (
                    'fingerprint-v4', 'worker-v4', ?, ?, 'ACTIVE',
                    'fingerprint-v4-key', ?, NULL
                )
                """,
                (digest(10), digest(11), NOW),
            )
            connection.execute(
                """
                INSERT INTO worker_slot_assignments (
                    id, worker_id, worker_fingerprint_id, slot_id, run_id,
                    is_elastic, state, idempotency_key, assigned_at, released_at
                ) VALUES (
                    'assignment-v4', 'worker-v4', 'fingerprint-v4', 'slot-v4',
                    'run-v4', 0, 'ACTIVE', 'assignment-v4-key', ?, NULL
                )
                """,
                (NOW,),
            )
            connection.execute(
                """
                INSERT INTO seat_capability_activations (
                    id, physical_seat_id, capability_id, slot_id,
                    worker_assignment_id, goal_id, run_id, state,
                    idempotency_key, activated_at, released_at
                ) VALUES (
                    'seat-activation-v4', 'seat-v4', 'capability-v4', 'slot-v4',
                    'assignment-v4', 'goal-v4', 'run-v4', 'ACTIVE',
                    'seat-activation-v4-key', ?, NULL
                )
                """,
                (NOW,),
            )
            connection.execute(
                """
                INSERT INTO workflow_definitions (
                    id, definition_id, workflow_key, version, state,
                    idempotency_key, created_at
                ) VALUES (
                    'workflow-definition-v4', ?, 'delivery', ?,
                    'ACTIVE', 'workflow-definition-v4-key', ?
                )
                """,
                (definitions["workflow"], WORKFLOW_DEFINITION_VERSION, NOW),
            )
            for state_key, ordinal, is_initial, is_terminal in (
                ("START", 0, 1, 0),
                ("NEXT", 1, 0, 0),
                ("DONE", 2, 0, 1),
                ("FAILED", 3, 0, 1),
            ):
                connection.execute(
                    """
                    INSERT INTO workflow_states (
                        workflow_definition_id, state_key, ordinal,
                        is_initial, is_terminal
                    ) VALUES ('workflow-definition-v4', ?, ?, ?, ?)
                    """,
                    (state_key, ordinal, is_initial, is_terminal),
                )
            for transition in (
                ("transition-v4", "start-next", "START", "NEXT"),
                ("transition-done-v4", "next-done", "NEXT", "DONE"),
            ):
                connection.execute(
                    """
                    INSERT INTO workflow_transitions (
                        id, workflow_definition_id, transition_key,
                        from_state_key, to_state_key, capability_id, result_kind,
                        failure_route, requires_serena_onboarding,
                        output_schema_definition_id, state,
                        idempotency_key, created_at
                    ) VALUES (
                        ?, 'workflow-definition-v4', ?, ?, ?, 'capability-v4',
                        'activation-result', 'FAILED', 1, ?, 'ACTIVE', ?, ?
                    )
                    """,
                    (
                        transition[0],
                        transition[1],
                        transition[2],
                        transition[3],
                        definitions["output"],
                        f"{transition[0]}-key",
                        NOW,
                    ),
                )
            connection.execute(
                """
                INSERT INTO workflow_instances (
                    id, workflow_definition_id, goal_id, run_id,
                    current_state_key, status, idempotency_key,
                    created_at, updated_at, completed_at
                ) VALUES (
                    'workflow-instance-v4', 'workflow-definition-v4',
                    'goal-v4', 'run-v4', 'START', 'ACTIVE',
                    'workflow-instance-v4-key', ?, ?, NULL
                )
                """,
                (NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO repository_registrations (
                    id, target_id, managed_repository_id, canonical_path,
                    git_common_dir, source_oid, state, idempotency_key,
                    registered_at
                ) VALUES (
                    'repository-v4', 'target-v4', NULL, 'C:/fixture-v4',
                    'C:/fixture-v4/.git', ?, 'ACTIVE', 'repository-v4-key', ?
                )
                """,
                (OID_A, NOW),
            )
            connection.execute(
                """
                INSERT INTO runtime_leases (
                    id, repository_id, goal_id, run_id, slot_id,
                    worker_assignment_id, lease_kind, branch_ref, worktree_path,
                    base_oid, expected_head_oid, write_roots_json,
                    protected_roots_json, state, expires_at,
                    idempotency_key, created_at, released_at
                ) VALUES (
                    'lease-v4', 'repository-v4', 'goal-v4', 'run-v4', 'slot-v4',
                    'assignment-v4', 'DEVELOPMENT', 'refs/heads/ax/v4',
                    'C:/fixture-v4/.ax/worktrees/v4', ?, ?, '["src"]',
                    '[".git"]', 'ACTIVE', ?, 'lease-v4-key', ?, NULL
                )
                """,
                (OID_A, OID_B, EXPIRES_AT, NOW),
            )
            connection.execute(
                """
                INSERT INTO sandbox_bindings (
                    id, lease_id, repository_id, run_id, slot_id, subject_oid,
                    cwd, source_root, source_read_only, writable_roots_json,
                    backend, attestation_digest, state, idempotency_key,
                    bound_at, released_at
                ) VALUES (
                    'sandbox-v4', 'lease-v4', 'repository-v4', 'run-v4',
                    'slot-v4', ?, 'C:/fixture-v4/.ax/worktrees/v4',
                    'C:/fixture-v4', 0, '["src"]', 'local', ?, 'ACTIVE',
                    'sandbox-v4-key', ?, NULL
                )
                """,
                (OID_B, digest(12), NOW),
            )
            connection.execute(
                """
                INSERT INTO oid_authorities (
                    id, repository_id, goal_id, run_id, lease_id,
                    sandbox_binding_id, authority_kind, oid, evidence_digest,
                    state, idempotency_key, created_at
                ) VALUES (
                    'oid-authority-v4', 'repository-v4', 'goal-v4', 'run-v4',
                    'lease-v4', 'sandbox-v4', 'SUBJECT', ?, ?, 'ACTIVE',
                    'oid-authority-v4-key', ?
                )
                """,
                (OID_B, digest(13), NOW),
            )

        self.store.register_activation_contract(
            contract_id="contract-v4",
            workflow_instance_id="workflow-instance-v4",
            workflow_transition_id="transition-v4",
            goal_id="goal-v4",
            run_id="run-v4",
            physical_seat_id="seat-v4",
            capability_id="capability-v4",
            seat_capability_activation_id="seat-activation-v4",
            worker_id="worker-v4",
            worker_fingerprint_id="fingerprint-v4",
            slot_id="slot-v4",
            worker_assignment_id="assignment-v4",
            repository_id="repository-v4",
            lease_id="lease-v4",
            sandbox_binding_id="sandbox-v4",
            oid_authority_id="oid-authority-v4",
            base_oid=OID_A,
            subject_oid=OID_B,
            contract_definition_id=definitions["contract"],
            output_schema_definition_id=definitions["output"],
            contract_digest=digest(20),
            packet_digest=digest(21),
            context_char_budget=1_000,
            max_attempts=2,
            idempotency_key="contract-v4-key",
        )
        return {
            **definitions,
            "contract_id": "contract-v4",
            "fingerprint_id": "fingerprint-v4",
            "goal_id": "goal-v4",
            "run_id": "run-v4",
        }

    def _bind_profile_clause_and_skill(self) -> None:
        self.store.bind_contract_profile(
            contract_id=self.ids["contract_id"],
            profile_definition_id=self.ids["profile"],
            compiled_profile_ref=".codex/profiles/developer-v4.json",
            compiled_profile_digest=digest(30),
        )
        self.store.bind_contract_clause(
            contract_id=self.ids["contract_id"],
            ordinal=0,
            definition_id=self.ids["clause"],
            clause_digest=CLAUSE_DEFINITION_DIGEST,
            character_count=300,
        )
        self.store.bind_contract_skill(
            contract_id=self.ids["contract_id"],
            skill_definition_id=self.ids["skill"],
            capability_id="capability-v4",
            ordinal=0,
            bound_digest=SKILL_DEFINITION_DIGEST,
            content_character_count=250,
        )

    def _bind_serena_snapshot(self) -> tuple[str, str]:
        snapshot_id = self.store.record_serena_onboarding_snapshot(
            repo_id="repository-v4",
            source_oid=OID_A,
            policy_digest=digest(33),
            memory_bindings=[
                {
                    "name": "overview",
                    "reference": "serena://overview",
                    "sha256": digest(34),
                },
                {
                    "memory_name": "conventions",
                    "memory_ref": "serena://conventions",
                    "memory_sha256": digest(35),
                },
            ],
        )
        binding_id = self.store.bind_contract_serena_memory(
            contract_id=self.ids["contract_id"],
            snapshot_id=snapshot_id,
            memory_name="conventions",
            ordinal=0,
        )
        return snapshot_id, binding_id

    def _prepare_accepted_contract(self) -> tuple[str, str, str]:
        self._bind_profile_clause_and_skill()
        snapshot_id, memory_binding_id = self._bind_serena_snapshot()
        admission_id = self.store.record_contract_admission(
            contract_id=self.ids["contract_id"],
            accepted=True,
            reason_code=None,
        )
        return admission_id, snapshot_id, memory_binding_id

    def _prepare_mcp_attempt(self) -> tuple[str, str, str]:
        self._bind_profile_clause_and_skill()
        _, memory_binding_id = self._bind_serena_snapshot()
        definition_id = self.store.register_mcp_definition(
            server_name="serena",
            tool_name="initial_instructions",
            version="trusted-v1",
            sha256=MCP_DEFINITION_DIGEST,
        )
        self.store.bind_contract_mcp(
            contract_id=self.ids["contract_id"],
            mcp_definition_id=definition_id,
            required_availability=True,
            invocation_required=True,
            trigger_rule="before-project-work",
        )
        self.store.record_mcp_health_observation(
            mcp_definition_id=definition_id,
            contract_id=self.ids["contract_id"],
            status=McpHealthStatus.HEALTHY,
            evidence_digest=digest(91),
            idempotency_key="trusted-mcp-health-v4",
        )
        self.store.record_contract_admission(
            contract_id=self.ids["contract_id"],
            accepted=True,
            reason_code=None,
        )
        attempt_id = self.store.record_contract_attempt(
            contract_id=self.ids["contract_id"],
            backend="codex",
            model="gpt-5",
            input_digest=digest(92),
        )
        receipts = McpInvocationBroker(
            state_store=self.store,
            invoker=_StateFixtureMcpInvoker(),
        ).invoke_required_context(
            McpInvocationContext(
                contract_id=self.ids["contract_id"],
                activation_id="activation-v4",
                attempt_id=attempt_id,
                repository_id="repository-v4",
                request_digest=digest(93),
            )
        )
        self.assertEqual(1, len(receipts))
        self._mcp_receipt_digests[receipts[0].receipt_id] = (
            receipts[0].evidence_sha256
        )
        return attempt_id, receipts[0].receipt_id, memory_binding_id

    def test_schema_v4_initializes_with_expected_objects_and_clean_fks(self) -> None:
        expected_tables = {
            "registered_definitions",
            "physical_seats",
            "logical_capabilities",
            "worker_fingerprints",
            "workflow_transitions",
            "runtime_leases",
            "activation_contracts",
            "contract_admissions",
            "contract_attempts",
            "activation_results",
            "contract_violations",
            "token_ledger_entries",
            "mcp_usage_receipts",
            "serena_onboarding_snapshots",
            "control_plane_evidence",
        }
        expected_indexes = {
            "ux_worker_fingerprints_active_worker",
            "ux_worker_slot_assignments_active_elastic",
            "ux_seat_capability_activations_active_seat",
            "ux_runtime_leases_active_slot_run",
            "ux_worker_circuit_breakers_open_scope",
        }
        expected_triggers = {
            "trg_workflow_instances_legal_transition",
            "trg_activation_contracts_binding_integrity",
            "trg_contract_admissions_require_bindings",
            "trg_contract_attempts_format_repair_gate",
            "trg_contract_violations_second_format_circuit",
            "trg_mcp_usage_receipts_no_update",
            "trg_serena_consumption_no_update",
            "trg_control_plane_evidence_no_update",
        }
        with self.store.transaction() as connection:
            objects = {
                (row["type"], row["name"])
                for row in connection.execute(
                    "SELECT type, name FROM sqlite_master"
                )
            }
            self.assertTrue(
                expected_tables <= {name for kind, name in objects if kind == "table"}
            )
            self.assertTrue(
                expected_indexes <= {name for kind, name in objects if kind == "index"}
            )
            self.assertTrue(
                expected_triggers <= {name for kind, name in objects if kind == "trigger"}
            )
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_fk_check_and_partial_unique_constraints_are_enforced(self) -> None:
        replayed = self.store.register_definition(
            kind="WORKFLOW",
            version=WORKFLOW_DEFINITION_VERSION,
            sha256=WORKFLOW_DEFINITION_DIGEST,
            source_ref=WORKFLOW_DEFINITION_SOURCE_REF,
        )
        self.assertEqual(self.ids["workflow"], replayed)
        with self.assertRaises(IdempotencyConflictError):
            self.store.register_definition(
                kind="WORKFLOW",
                version="delivery-v4",
                sha256=digest(99),
                source_ref="definitions/workflows/delivery-v4.json",
            )

        with self.store.transaction(immediate=True) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO seat_capability_ownerships (
                        physical_seat_id, capability_id, state,
                        idempotency_key, assigned_at
                    ) VALUES (
                        'missing-seat', 'capability-v4', 'ENABLED',
                        'missing-seat-key', ?
                    )
                    """,
                    (NOW,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO physical_seats (
                        id, seat_key, state, is_merged,
                        idempotency_key, created_at
                    ) VALUES (
                        'invalid-seat', 'invalid-seat', 'ACTIVE', 2,
                        'invalid-seat-key', ?
                    )
                    """,
                    (NOW,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO worker_fingerprints (
                        id, worker_id, fingerprint_sha256,
                        runtime_profile_digest, state,
                        idempotency_key, created_at, revoked_at
                    ) VALUES (
                        'second-fingerprint-v4', 'worker-v4', ?, ?, 'ACTIVE',
                        'second-fingerprint-v4-key', ?, NULL
                    )
                    """,
                    (digest(40), digest(41), NOW),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO seat_capability_activations (
                        id, physical_seat_id, capability_id, slot_id,
                        worker_assignment_id, goal_id, run_id, state,
                        idempotency_key, activated_at, released_at
                    ) VALUES (
                        'second-seat-activation-v4', 'seat-v4',
                        'capability-extra-v4', 'slot-v4', 'assignment-v4',
                        'goal-v4', 'run-v4', 'ACTIVE',
                        'second-seat-activation-v4-key', ?, NULL
                    )
                    """,
                    (NOW,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO runtime_leases (
                        id, repository_id, goal_id, run_id, slot_id,
                        worker_assignment_id, lease_kind, branch_ref,
                        worktree_path, base_oid, expected_head_oid,
                        write_roots_json, protected_roots_json, state,
                        expires_at, idempotency_key, created_at, released_at
                    ) VALUES (
                        'second-lease-v4', 'repository-v4', 'goal-v4', 'run-v4',
                        'slot-v4', 'assignment-v4', 'DEVELOPMENT',
                        'refs/heads/ax/v4-second',
                        'C:/fixture-v4/.ax/worktrees/v4-second', ?, ?, '["src"]',
                        '[".git"]', 'ACTIVE', ?, 'second-lease-v4-key', ?, NULL
                    )
                    """,
                    (OID_A, OID_B, EXPIRES_AT, NOW),
                )

    def test_only_declared_workflow_transitions_are_legal(self) -> None:
        with self.store.transaction(immediate=True) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE workflow_instances
                    SET current_state_key = 'DONE', updated_at = ?
                    WHERE id = 'workflow-instance-v4'
                    """,
                    (NOW,),
                )
            connection.execute(
                """
                UPDATE workflow_instances
                SET current_state_key = 'NEXT', updated_at = ?
                WHERE id = 'workflow-instance-v4'
                """,
                (NOW,),
            )
            current = connection.execute(
                """
                SELECT current_state_key FROM workflow_instances
                WHERE id = 'workflow-instance-v4'
                """
            ).fetchone()["current_state_key"]
        self.assertEqual("NEXT", current)

    def test_rejected_admission_has_zero_backend_attempts_and_zero_tokens(self) -> None:
        admission_id = self.store.record_contract_admission(
            contract_id=self.ids["contract_id"],
            accepted=False,
            reason_code="MCP_HEALTH",
        )
        with self.assertRaises(IntentStateError):
            self.store.record_contract_attempt(
                contract_id=self.ids["contract_id"],
                backend="codex",
                model="gpt-5",
                input_digest=digest(50),
            )

        with self.store.transaction() as connection:
            admission = connection.execute(
                "SELECT * FROM contract_admissions WHERE id = ?",
                (admission_id,),
            ).fetchone()
            contract = connection.execute(
                "SELECT state FROM activation_contracts WHERE id = 'contract-v4'"
            ).fetchone()
            attempts = connection.execute(
                "SELECT COUNT(*) AS count FROM contract_attempts"
            ).fetchone()["count"]
            ledger = connection.execute(
                "SELECT * FROM token_ledger_entries WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
        self.assertEqual("REJECTED", admission["decision"])
        self.assertEqual("REJECTED", contract["state"])
        self.assertEqual(0, attempts)
        self.assertEqual("ADMISSION_REJECTED", ledger["entry_kind"])
        self.assertEqual((0, 0, 0), (
            ledger["input_tokens"],
            ledger["output_tokens"],
            ledger["model_calls"],
        ))

    def test_one_format_repair_then_second_format_violation_opens_circuit(self) -> None:
        self._prepare_accepted_contract()
        primary_id = self.store.record_contract_attempt(
            contract_id=self.ids["contract_id"],
            backend="codex",
            model="gpt-5",
            input_digest=digest(60),
        )
        _, first_violation_id, first_disposition, _, _ = (
            self.store.record_format_invalid_result_and_violation(
            activation_id="activation-v4",
            attempt_id=primary_id,
            result_kind="activation-result",
            output_digest=digest(61),
            evidence_digest=digest(62),
            payload={"format": "invalid", "attempt": 1},
            violation_evidence_digest=digest(63),
            violation_details={"schema_error": "missing field"},
            violation_idempotency_key="format-primary-v4",
            )
        )
        repair_id = self.store.record_contract_attempt(
            contract_id=self.ids["contract_id"],
            backend="codex",
            model="gpt-5",
            input_digest=digest(64),
            attempt_kind=ContractAttemptKind.FORMAT_REPAIR,
        )
        _, second_violation_id, second_disposition, _, _ = (
            self.store.record_format_invalid_result_and_violation(
            activation_id="activation-v4",
            attempt_id=repair_id,
            result_kind="activation-result",
            output_digest=digest(65),
            evidence_digest=digest(66),
            payload={"format": "invalid", "attempt": 2},
            violation_evidence_digest=digest(67),
            violation_details={"schema_error": "still missing field"},
            violation_idempotency_key="format-repair-v4",
            )
        )

        circuit = self.store.get_open_circuit(
            worker_fingerprint_id=self.ids["fingerprint_id"],
            goal_id=self.ids["goal_id"],
            run_id=self.ids["run_id"],
        )
        with self.store.transaction() as connection:
            attempts = connection.execute(
                """
                SELECT attempt_kind, output_only FROM contract_attempts
                WHERE contract_id = 'contract-v4'
                ORDER BY attempt_number
                """
            ).fetchall()
            violations = connection.execute(
                """
                SELECT id, disposition FROM contract_violations
                WHERE id IN (?, ?)
                ORDER BY occurred_at, id
                """,
                (first_violation_id, second_violation_id),
            ).fetchall()
            contract_state = connection.execute(
                "SELECT state FROM activation_contracts WHERE id = 'contract-v4'"
            ).fetchone()["state"]
        self.assertEqual(
            [("PRIMARY", 0), ("FORMAT_REPAIR", 1)],
            [(row["attempt_kind"], row["output_only"]) for row in attempts],
        )
        self.assertEqual(
            {"FORMAT_REPAIR", "CIRCUIT_OPEN"},
            {row["disposition"] for row in violations},
        )
        self.assertEqual("FORMAT_REPAIR", first_disposition)
        self.assertEqual("CIRCUIT_OPEN", second_disposition)
        self.assertIsNotNone(circuit)
        self.assertEqual("OPEN", circuit["state"])
        self.assertEqual("QUARANTINED", contract_state)

    def test_authority_violation_quarantines_contract_immediately(self) -> None:
        self._prepare_accepted_contract()
        attempt_id = self.store.record_contract_attempt(
            contract_id=self.ids["contract_id"],
            backend="codex",
            model="gpt-5",
            input_digest=digest(70),
        )
        self.store.record_contract_violation(
            contract_id=self.ids["contract_id"],
            attempt_id=attempt_id,
            violation_code=ContractViolationCode.AUTHORITY,
            evidence_digest=digest(71),
            details={"operation": "merge", "authorized": False},
        )
        with self.store.transaction() as connection:
            state = connection.execute(
                "SELECT state FROM activation_contracts WHERE id = 'contract-v4'"
            ).fetchone()["state"]
        self.assertEqual("QUARANTINED", state)
        self.assertIsNone(
            self.store.get_open_circuit(
                worker_fingerprint_id=self.ids["fingerprint_id"],
                goal_id=self.ids["goal_id"],
                run_id=self.ids["run_id"],
            )
        )

    def test_contract_base_oid_is_bound_to_lease_and_hardening_reloads(self) -> None:
        with self.assertRaisesRegex(sqlite3.IntegrityError, "base OID"):
            self.store.register_activation_contract(
                contract_id="contract-base-mismatch-v4",
                workflow_instance_id="workflow-instance-v4",
                workflow_transition_id="transition-v4",
                goal_id="goal-v4",
                run_id="run-v4",
                physical_seat_id="seat-v4",
                capability_id="capability-v4",
                seat_capability_activation_id="seat-activation-v4",
                worker_id="worker-v4",
                worker_fingerprint_id="fingerprint-v4",
                slot_id="slot-v4",
                worker_assignment_id="assignment-v4",
                repository_id="repository-v4",
                lease_id="lease-v4",
                sandbox_binding_id="sandbox-v4",
                oid_authority_id="oid-authority-v4",
                base_oid=OID_B,
                subject_oid=OID_B,
                contract_definition_id=self.ids["contract"],
                output_schema_definition_id=self.ids["output"],
                contract_digest=digest(101),
                packet_digest=digest(102),
                context_char_budget=1_000,
                max_attempts=2,
                idempotency_key="contract-base-mismatch-v4-key",
            )

        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "DROP TRIGGER trg_activation_contracts_base_oid_integrity"
            )
        reloaded = AxStateStore(self.store.db_path)
        self.assertEqual(LATEST_SCHEMA_VERSION, reloaded.initialize())
        with reloaded.transaction() as connection:
            restored = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'trigger'
                  AND name = 'trg_activation_contracts_base_oid_integrity'
                """
            ).fetchone()
        self.assertIsNotNone(restored)

    def test_trusted_mcp_receipt_is_preexisting_and_bound_to_exact_evidence(self) -> None:
        attempt_id, receipt_id, _ = self._prepare_mcp_attempt()
        valid = [{
            "receipt_id": receipt_id,
            "server_id": "serena",
            "tool_id": "initial_instructions",
            "evidence_sha256": self._mcp_receipt_digests[receipt_id],
        }]
        self.store.validate_trusted_mcp_receipt_references(
            contract_id=self.ids["contract_id"],
            attempt_id=attempt_id,
            receipts=valid,
        )
        fabricated = [{**valid[0], "receipt_id": "fabricated-receipt-v4"}]
        with self.assertRaisesRegex(IntentStateError, "fabricated or mismatched"):
            self.store.validate_trusted_mcp_receipt_references(
                contract_id=self.ids["contract_id"],
                attempt_id=attempt_id,
                receipts=fabricated,
            )
        wrong_digest = [{**valid[0], "evidence_sha256": digest(999)}]
        with self.assertRaisesRegex(IntentStateError, "fabricated or mismatched"):
            self.store.validate_trusted_mcp_receipt_references(
                contract_id=self.ids["contract_id"],
                attempt_id=attempt_id,
                receipts=wrong_digest,
            )

        self.store.record_activation_result(
            attempt_id=attempt_id,
            result_kind="activation-result",
            output_digest=digest(103),
            evidence_digest=digest(104),
            payload={"accepted": True},
            disposition=ResultDisposition.ACCEPTED,
        )
        with self.assertRaisesRegex(RunnerContractError, "not active"):
            McpInvocationBroker(
                state_store=self.store,
                invoker=_StateFixtureMcpInvoker(),
            ).invoke_required_context(
                McpInvocationContext(
                    contract_id=self.ids["contract_id"],
                    activation_id="activation-v4",
                    attempt_id=attempt_id,
                    repository_id="repository-v4",
                    request_digest=digest(105),
                )
            )

    def test_private_mcp_receipt_primitive_rejects_wrong_authority(self) -> None:
        for authority in (None, object()):
            with self.subTest(authority=authority):
                with self.assertRaisesRegex(IntentStateError, "authority"):
                    self.store._record_trusted_mcp_invocation_receipt(
                        _authority=authority,
                        contract_id=self.ids["contract_id"],
                        attempt_id="attempt-v4",
                        server_name="serena",
                        tool_name="initial_instructions",
                        input_digest=digest(105),
                        output_digest=digest(106),
                        idempotency_key="unauthorized-mcp-invocation-v4",
                    )

    def test_result_commit_rolls_back_every_new_fact_when_workflow_cas_is_stale(self) -> None:
        attempt_id, receipt_id, memory_binding_id = self._prepare_mcp_attempt()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE workflow_instances
                SET current_state_key = 'NEXT', updated_at = ?
                WHERE id = 'workflow-instance-v4'
                """,
                (NOW,),
            )
        with self.assertRaisesRegex(IntentStateError, "CAS"):
            self.store.commit_activation_result_transaction(
                activation_id="activation-v4",
                contract_id=self.ids["contract_id"],
                attempt_id=attempt_id,
                result_kind="activation-result",
                output_digest=digest(110),
                evidence_digest=digest(111),
                payload={"result": "accepted"},
                input_tokens=10,
                output_tokens=5,
                model_calls=1,
                mcp_receipts=[{
                    "receipt_id": receipt_id,
                    "server_id": "serena",
                    "tool_id": "initial_instructions",
                    "evidence_sha256": self._mcp_receipt_digests[receipt_id],
                }],
                serena_receipts=[{
                    "memory_binding_id": memory_binding_id,
                    "receipt_digest": digest(112),
                    "idempotency_key": "serena-atomic-rollback-v4",
                }],
                workflow_instance_id="workflow-instance-v4",
                workflow_transition_id="transition-v4",
                from_state="START",
                to_state="NEXT",
                transition_receipt_id="transition-atomic-rollback-v4",
                result_idempotency_key="result-atomic-rollback-v4",
                token_idempotency_key="token-atomic-rollback-v4",
                transition_idempotency_key="transition-atomic-rollback-v4-key",
                outgoing_messages=[{
                    "thread_id": "goal-v4:run-v4",
                    "work_item_id": "activation-v4",
                    "from_role": "developer",
                    "to_role": "pl",
                    "type": "result_ready",
                    "payload": {"result": "accepted"},
                }],
            )
        with self.store.transaction() as connection:
            counts = {
                table: connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()["count"]
                for table in (
                    "activation_results",
                    "token_ledger_entries",
                    "workflow_transition_receipts",
                    "serena_consumption_receipts",
                    "messages",
                    "outbox",
                )
            }
            contract_state = connection.execute(
                "SELECT state FROM activation_contracts WHERE id = 'contract-v4'"
            ).fetchone()["state"]
            attempt_state = connection.execute(
                "SELECT state FROM contract_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()["state"]
            trusted_count = connection.execute(
                "SELECT COUNT(*) AS count FROM mcp_usage_receipts"
            ).fetchone()["count"]
            activation = connection.execute(
                "SELECT state, result_json FROM activations WHERE id = 'activation-v4'"
            ).fetchone()
        self.assertEqual(
            {
                "activation_results": 0,
                "token_ledger_entries": 0,
                "workflow_transition_receipts": 0,
                "serena_consumption_receipts": 0,
                "messages": 0,
                "outbox": 0,
            },
            counts,
        )
        self.assertEqual("RUNNING", contract_state)
        self.assertEqual("CREATED", attempt_state)
        self.assertEqual(1, trusted_count)
        self.assertEqual("RUNNING", activation["state"])
        self.assertIsNone(activation["result_json"])

    def test_rework_result_receipt_cas_and_finalization_commit_together(self) -> None:
        attempt_id, receipt_id, memory_binding_id = self._prepare_mcp_attempt()
        durable = self.store.commit_activation_result_transaction(
            activation_id="activation-v4",
            contract_id=self.ids["contract_id"],
            attempt_id=attempt_id,
            result_kind="needs_rework",
            output_digest=digest(120),
            evidence_digest=digest(121),
            payload={"result": "needs_rework"},
            input_tokens=10,
            output_tokens=5,
            model_calls=1,
            mcp_receipts=[{
                "receipt_id": receipt_id,
                "server_id": "serena",
                "tool_id": "initial_instructions",
                "evidence_sha256": self._mcp_receipt_digests[receipt_id],
            }],
            serena_receipts=[{
                "memory_binding_id": memory_binding_id,
                "receipt_digest": digest(122),
                "idempotency_key": "serena-rework-v4",
            }],
            workflow_instance_id="workflow-instance-v4",
            workflow_transition_id="transition-v4",
            from_state="START",
            to_state="FAILED",
            transition_receipt_id="transition-rework-v4",
            result_idempotency_key="result-rework-v4",
            token_idempotency_key="token-rework-v4",
            transition_idempotency_key="transition-rework-v4-key",
            outgoing_messages=[],
        )
        result_id = durable.activation_result_id
        with self.store.transaction() as connection:
            result = connection.execute(
                "SELECT * FROM activation_results WHERE id = ?",
                (result_id,),
            ).fetchone()
            token = connection.execute(
                "SELECT * FROM token_ledger_entries WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            transition = connection.execute(
                """
                SELECT * FROM workflow_transition_receipts
                WHERE id = 'transition-rework-v4'
                """
            ).fetchone()
            state = connection.execute(
                """
                SELECT activation_contracts.state AS contract_state,
                       workflow_instances.current_state_key
                FROM activation_contracts
                JOIN workflow_instances
                  ON workflow_instances.id =
                     activation_contracts.workflow_instance_id
                WHERE activation_contracts.id = 'contract-v4'
                """
            ).fetchone()
        self.assertEqual("ACCEPTED", result["disposition"])
        self.assertEqual("CONSUMED", token["entry_kind"])
        self.assertEqual("FAILED", transition["to_state_key"])
        self.assertEqual("COMPLETED", state["contract_state"])
        self.assertEqual("FAILED", state["current_state_key"])

    def test_invalid_result_outbox_failure_rolls_back_every_terminal_fact(self) -> None:
        self._prepare_accepted_contract()
        attempt_id = self.store.record_contract_attempt(
            contract_id=self.ids["contract_id"],
            backend="codex",
            model="gpt-5",
            input_digest=digest(124),
        )
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_invalid_outbox_test
                BEFORE INSERT ON outbox
                BEGIN
                    SELECT RAISE(ABORT, 'injected invalid outbox failure');
                END
                """
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            self.store.commit_invalid_activation_result_transaction(
                activation_id="activation-v4",
                contract_id=self.ids["contract_id"],
                attempt_id=attempt_id,
                disposition=ResultDisposition.REJECTED,
                result_kind="invalid-result",
                output_digest=digest(125),
                evidence_digest=digest(126),
                payload={"invalid": True},
                violation_code=ContractViolationCode.OTHER,
                violation_evidence_digest=digest(127),
                violation_details={"reason": "invalid"},
                violation_idempotency_key="invalid-violation-rollback-v4",
                workflow_instance_id="workflow-instance-v4",
                workflow_transition_id="transition-v4",
                from_state="START",
                failure_state="FAILED",
                transition_receipt_id="invalid-transition-rollback-v4",
                result_idempotency_key="invalid-result-rollback-v4",
                transition_idempotency_key="invalid-transition-rollback-v4-key",
                outgoing_messages=[{
                    "thread_id": "goal-v4:run-v4",
                    "work_item_id": "activation-v4",
                    "from_role": "developer",
                    "to_role": "pl",
                    "type": "rework_required",
                    "payload": {"reason": "invalid"},
                }],
            )
        with self.store.transaction() as connection:
            counts = {
                table: connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()["count"]
                for table in (
                    "activation_results",
                    "contract_violations",
                    "workflow_transition_receipts",
                    "messages",
                    "outbox",
                )
            }
            states = connection.execute(
                """
                SELECT activation_contracts.state AS contract_state,
                       contract_attempts.state AS attempt_state,
                       workflow_instances.current_state_key,
                       activations.state AS activation_state,
                       activations.result_json
                FROM activation_contracts
                JOIN contract_attempts ON contract_attempts.contract_id = activation_contracts.id
                JOIN workflow_instances ON workflow_instances.id = activation_contracts.workflow_instance_id
                JOIN activations ON activations.id = 'activation-v4'
                WHERE activation_contracts.id = 'contract-v4'
                """
            ).fetchone()
        self.assertEqual(
            {
                "activation_results": 0,
                "contract_violations": 0,
                "workflow_transition_receipts": 0,
                "messages": 0,
                "outbox": 0,
            },
            counts,
        )
        self.assertEqual("RUNNING", states["contract_state"])
        self.assertEqual("CREATED", states["attempt_state"])
        self.assertEqual("START", states["current_state_key"])
        self.assertEqual("RUNNING", states["activation_state"])
        self.assertIsNone(states["result_json"])

    def test_format_result_is_rolled_back_when_violation_insert_fails(self) -> None:
        self._prepare_accepted_contract()
        attempt_id = self.store.record_contract_attempt(
            contract_id=self.ids["contract_id"],
            backend="codex",
            model="gpt-5",
            input_digest=digest(130),
        )
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_format_test
                BEFORE INSERT ON contract_violations
                WHEN NEW.violation_code = 'FORMAT'
                BEGIN
                    SELECT RAISE(ABORT, 'injected format evidence failure');
                END
                """
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            self.store.record_format_invalid_result_and_violation(
                activation_id="activation-v4",
                attempt_id=attempt_id,
                result_kind="activation-result",
                output_digest=digest(131),
                evidence_digest=digest(132),
                payload={"bad": True},
                violation_evidence_digest=digest(133),
                violation_details={"schema_error": "bad"},
                violation_idempotency_key="format-rollback-v4",
            )
        with self.store.transaction() as connection:
            result_count = connection.execute(
                "SELECT COUNT(*) AS count FROM activation_results"
            ).fetchone()["count"]
            violation_count = connection.execute(
                "SELECT COUNT(*) AS count FROM contract_violations"
            ).fetchone()["count"]
            attempt_state = connection.execute(
                "SELECT state FROM contract_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()["state"]
        self.assertEqual(0, result_count)
        self.assertEqual(0, violation_count)
        self.assertEqual("CREATED", attempt_state)

    def test_mcp_serena_result_token_and_evidence_records_are_append_only(self) -> None:
        self._bind_profile_clause_and_skill()
        snapshot_id, memory_binding_id = self._bind_serena_snapshot()
        mcp_definition_id = self.store.register_mcp_definition(
            server_name="serena",
            tool_name="initial_instructions",
            version="1",
            sha256=MCP_DEFINITION_DIGEST,
        )
        mcp_binding_id = self.store.bind_contract_mcp(
            contract_id=self.ids["contract_id"],
            mcp_definition_id=mcp_definition_id,
            required_availability=True,
            invocation_required=True,
            trigger_rule="before-project-work",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_contract_admission(
                contract_id=self.ids["contract_id"],
                accepted=True,
                reason_code=None,
            )
        health_id = self.store.record_mcp_health_observation(
            mcp_definition_id=mcp_definition_id,
            contract_id=self.ids["contract_id"],
            status=McpHealthStatus.HEALTHY,
            evidence_digest=digest(81),
            idempotency_key="mcp-health-v4-key",
        )
        admission_id = self.store.record_contract_admission(
            contract_id=self.ids["contract_id"],
            accepted=True,
            reason_code=None,
        )
        attempt_id = self.store.record_contract_attempt(
            contract_id=self.ids["contract_id"],
            backend="codex",
            model="gpt-5",
            input_digest=digest(82),
        )
        with self.assertRaisesRegex(IntentStateError, "disabled"):
            self.store.record_mcp_usage_receipt(
                contract_id=self.ids["contract_id"],
                attempt_id=attempt_id,
                mcp_binding_id=mcp_binding_id,
                tool_name="initial_instructions",
                input_digest=digest(83),
                output_digest=digest(84),
                idempotency_key="disabled-mcp-receipt-v4-key",
            )
        mcp_receipts = McpInvocationBroker(
            state_store=self.store,
            invoker=_StateFixtureMcpInvoker(),
        ).invoke_required_context(
            McpInvocationContext(
                contract_id=self.ids["contract_id"],
                activation_id="activation-v4",
                attempt_id=attempt_id,
                repository_id="repository-v4",
                request_digest=digest(83),
            )
        )
        self.assertEqual(1, len(mcp_receipts))
        mcp_receipt_id = mcp_receipts[0].receipt_id
        serena_receipt_id = self.store.record_serena_consumption_receipt(
            contract_id=self.ids["contract_id"],
            memory_binding_id=memory_binding_id,
            receipt_digest=digest(85),
            idempotency_key="serena-consumption-v4-key",
        )
        token_entry_id = self.store.record_token_ledger_entry(
            contract_id=self.ids["contract_id"],
            attempt_id=attempt_id,
            entry_kind=TokenLedgerEntryKind.CONSUMED,
            input_tokens=100,
            output_tokens=25,
            model_calls=1,
            idempotency_key="token-attempt-v4-key",
        )
        result_id = self.store.record_activation_result(
            attempt_id=attempt_id,
            result_kind="activation-result",
            output_digest=digest(86),
            evidence_digest=digest(87),
            payload={"status": "accepted"},
            disposition=ResultDisposition.ACCEPTED,
        )

        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO control_plane_evidence (
                    id, subject_type, subject_id, contract_id, attempt_id,
                    disposition, evidence_digest, payload_json,
                    idempotency_key, occurred_at
                ) VALUES (
                    'evidence-v4', 'activation', 'contract-v4', 'contract-v4',
                    ?, 'ACCEPTED', ?, '{}', 'evidence-v4-key', ?
                )
                """,
                (attempt_id, digest(88), NOW),
            )
            immutable_updates = (
                ("activation_results", "payload_json = '{}'", result_id),
                ("mcp_health_observations", "status = 'UNHEALTHY'", health_id),
                ("mcp_usage_receipts", "tool_name = 'other'", mcp_receipt_id),
                (
                    "serena_onboarding_snapshots",
                    f"policy_digest = '{digest(89)}'",
                    snapshot_id,
                ),
                (
                    "serena_consumption_receipts",
                    f"receipt_digest = '{digest(90)}'",
                    serena_receipt_id,
                ),
                ("token_ledger_entries", "input_tokens = 0", token_entry_id),
                ("control_plane_evidence", "payload_json = '{}'", "evidence-v4"),
            )
            for table, assignment, row_id in immutable_updates:
                with self.assertRaises(sqlite3.IntegrityError, msg=table):
                    connection.execute(
                        f"UPDATE {table} SET {assignment} WHERE id = ?",
                        (row_id,),
                    )
            self.assertEqual(
                [],
                connection.execute("PRAGMA foreign_key_check").fetchall(),
            )
            self.assertEqual(
                "ACCEPTED",
                connection.execute(
                    "SELECT decision FROM contract_admissions WHERE id = ?",
                    (admission_id,),
                ).fetchone()["decision"],
            )


if __name__ == "__main__":
    unittest.main()
