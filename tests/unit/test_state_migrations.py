from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.agent_team_domain import AuditEvent, IntentStatus
from scripts.agent_team_queue import SQLiteMessageQueue
from scripts.agent_team_state import (
    AxStateStore,
    IdempotencyConflictError,
    LATEST_SCHEMA_VERSION,
    SchemaMigrationError,
)


OID_A = "a" * 40
OID_B = "b" * 40
NOW = "2026-07-21T00:00:00.000000+00:00"


LEGACY_SCHEMA = """
CREATE TABLE messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    parent_message_id TEXT,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    type TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    available_at TEXT NOT NULL,
    claimed_by TEXT,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    dedupe_key TEXT UNIQUE,
    last_error TEXT,
    created_at TEXT NOT NULL,
    processed_at TEXT,
    FOREIGN KEY(parent_message_id) REFERENCES messages(id)
);
CREATE TABLE outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    message_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    last_error TEXT,
    FOREIGN KEY(message_id) REFERENCES messages(id)
);
CREATE TABLE thread_snapshots (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    target_role TEXT NOT NULL,
    covered_through_seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(thread_id, target_role, covered_through_seq)
);
CREATE TABLE project_knowledge_state (
    repo_id TEXT PRIMARY KEY,
    project_path TEXT NOT NULL,
    baseline_oid TEXT,
    inspected_oid TEXT,
    source_fingerprint TEXT,
    state TEXT NOT NULL,
    memory_manifest_json TEXT NOT NULL DEFAULT '{}',
    owner_seat_id TEXT,
    evidence_artifact_ref TEXT,
    memory_manifest_sha256 TEXT,
    last_request_message_id TEXT,
    acknowledged_at TEXT,
    updated_at TEXT NOT NULL
);
"""


def create_populated_legacy_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            """
            INSERT INTO messages (
                id, thread_id, work_item_id, from_role, to_role, type,
                payload_json, status, available_at, created_at
            ) VALUES (
                'msg-legacy', 'thread-1', 'legacy-work', 'pl', 'dev_1',
                'ASSIGN', '{"value":1}', 'PENDING', ?, ?
            )
            """,
            (NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO outbox (
                id, message_id, event_type, payload_json,
                status, available_at, created_at
            ) VALUES (
                'evt-legacy', 'msg-legacy', 'MESSAGE_ENQUEUED',
                '{"message_id":"msg-legacy"}', 'PENDING', ?, ?
            )
            """,
            (NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO thread_snapshots (
                id, thread_id, work_item_id, target_role,
                covered_through_seq, payload_json, created_at
            ) VALUES (
                'snapshot-legacy', 'thread-1', 'legacy-work', 'dev_1',
                1, '{"summary":"kept"}', ?
            )
            """,
            (NOW,),
        )
        connection.execute(
            """
            INSERT INTO project_knowledge_state (
                repo_id, project_path, baseline_oid, inspected_oid,
                source_fingerprint, state, memory_manifest_json, updated_at
            ) VALUES (
                'repo-legacy', 'C:/legacy', ?, ?, 'fingerprint',
                'ready', '{"paths":["README.md"]}', ?
            )
            """,
            (OID_A, OID_A, NOW),
        )
        connection.commit()


class FailingInvariantStore(AxStateStore):
    def _migration_statement_applied(
        self,
        version: int,
        ordinal: int,
        connection: sqlite3.Connection,
    ) -> None:
        if version == 3 and ordinal == 2:
            raise RuntimeError("injected invariant migration failure")


class FailingV4Store(AxStateStore):
    def _migration_statement_applied(
        self,
        version: int,
        ordinal: int,
        connection: sqlite3.Connection,
    ) -> None:
        if version == 4 and ordinal == 2:
            raise RuntimeError("injected v4 migration failure")


class AxStateMigrationTests(unittest.TestCase):
    def test_empty_database_upgrade_is_complete_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-state-empty-") as temporary:
            db_path = Path(temporary) / "state" / "ax.db"
            store = AxStateStore(db_path)

            self.assertEqual(LATEST_SCHEMA_VERSION, store.initialize())
            self.assertEqual(LATEST_SCHEMA_VERSION, store.schema_version())
            with store.transaction() as connection:
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                migrations = connection.execute(
                    "SELECT version, name FROM ax_schema_migrations ORDER BY version"
                ).fetchall()

            expected = {
                "messages",
                "outbox",
                "thread_snapshots",
                "project_knowledge_state",
                "targets",
                "managed_repositories",
                "repository_snapshots",
                "goals",
                "runs",
                "work_items",
                "work_revisions",
                "workspaces",
                "workspace_leases",
                "activations",
                "profile_bindings",
                "profile_reference_bindings",
                "candidate_submissions",
                "reviews",
                "gate_decisions",
                "integration_plans",
                "integration_attempts",
                "attempt_candidates",
                "quality_runs",
                "build_runs",
                "promotions",
                "migration_runs",
                "migration_steps",
                "artifacts",
                "audit_events",
                "operation_intents",
                "reconciliation_findings",
                "physical_seats",
                "logical_capabilities",
                "runtime_slots",
                "workflow_definitions",
                "activation_contracts",
                "contract_admissions",
                "contract_attempts",
                "contract_violations",
                "worker_circuit_breakers",
                "token_ledger_entries",
                "mcp_usage_receipts",
                "serena_onboarding_snapshots",
            }
            self.assertTrue(expected.issubset(tables))
            self.assertEqual([1, 2, 3, 4], [row["version"] for row in migrations])

            self.assertEqual(LATEST_SCHEMA_VERSION, store.initialize())
            with store.transaction() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM ax_schema_migrations"
                ).fetchone()["count"]
            self.assertEqual(4, count)

    def test_populated_four_table_legacy_fixture_upgrades_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-state-legacy-") as temporary:
            db_path = Path(temporary) / "legacy.db"
            create_populated_legacy_database(db_path)

            store = AxStateStore(db_path)
            self.assertEqual(LATEST_SCHEMA_VERSION, store.initialize())

            with store.transaction() as connection:
                message = connection.execute(
                    "SELECT * FROM messages WHERE id = 'msg-legacy'"
                ).fetchone()
                outbox = connection.execute(
                    "SELECT * FROM outbox WHERE id = 'evt-legacy'"
                ).fetchone()
                snapshot = connection.execute(
                    "SELECT * FROM thread_snapshots WHERE id = 'snapshot-legacy'"
                ).fetchone()
                knowledge = connection.execute(
                    """
                    SELECT * FROM project_knowledge_state
                    WHERE repo_id = 'repo-legacy'
                    """
                ).fetchone()

            self.assertEqual({"value": 1}, json.loads(message["payload_json"]))
            self.assertEqual("msg-legacy", outbox["message_id"])
            self.assertEqual(
                {"summary": "kept"}, json.loads(snapshot["payload_json"])
            )
            self.assertEqual(
                {"paths": ["README.md"]},
                json.loads(knowledge["memory_manifest_json"]),
            )

            queue = SQLiteMessageQueue(db_path)
            self.assertEqual("msg-legacy", queue.get("msg-legacy").id)

    def test_v3_to_v4_is_additive_transactional_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-state-v4-upgrade-") as temporary:
            db_path = Path(temporary) / "ax.db"
            v3_store = AxStateStore(db_path)
            self.assertEqual(3, v3_store.initialize(target_version=3))
            with v3_store.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO audit_events (
                        id, event_type, actor, subject_type, subject_id,
                        payload_json, occurred_at
                    ) VALUES (
                        'audit-v3-evidence', 'LEGACY_IN_FLIGHT', 'legacy',
                        'activation', 'activation-v3', '{}', ?
                    )
                    """,
                    (NOW,),
                )

            with self.assertRaises(SchemaMigrationError):
                FailingV4Store(db_path).initialize()
            self.assertEqual(3, AxStateStore(db_path).schema_version())
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertIsNone(
                    connection.execute(
                        """
                        SELECT 1 FROM sqlite_master
                        WHERE type = 'table' AND name = 'physical_seats'
                        """
                    ).fetchone()
                )
                self.assertEqual(
                    1,
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM audit_events
                        WHERE id = 'audit-v3-evidence'
                        """
                    ).fetchone()[0],
                )

            recovered = AxStateStore(db_path)
            self.assertEqual(4, recovered.initialize())
            with recovered.transaction() as connection:
                self.assertEqual(
                    "activation-v3",
                    connection.execute(
                        """
                        SELECT subject_id FROM audit_events
                        WHERE id = 'audit-v3-evidence'
                        """
                    ).fetchone()["subject_id"],
                )
                self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_intent_begin_and_completion_are_idempotent_and_conflict_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-state-intent-") as temporary:
            store = AxStateStore(Path(temporary) / "ax.db")
            store.initialize()

            intent = store.begin_intent(
                operation="create-worktree",
                idempotency_key="intent-key-1",
                expected_state="ALLOCATED",
                expected_oid=OID_A,
                payload={"workspace": "workspace-1", "paths": ["src"]},
            )
            replay = store.begin_intent(
                operation="create-worktree",
                idempotency_key="intent-key-1",
                expected_state="ALLOCATED",
                expected_oid=OID_A,
                payload={"paths": ["src"], "workspace": "workspace-1"},
            )
            self.assertEqual(intent.intent_id, replay.intent_id)
            self.assertEqual(IntentStatus.PENDING, replay.status)

            with self.assertRaises(IdempotencyConflictError):
                store.begin_intent(
                    operation="remove-worktree",
                    idempotency_key="intent-key-1",
                    expected_state="ALLOCATED",
                    expected_oid=OID_A,
                    payload={"workspace": "workspace-1", "paths": ["src"]},
                )

            completed = store.complete_intent(
                intent.intent_id,
                resulting_state="READY",
                resulting_oid=OID_B,
                evidence={"receipt": "artifact-1"},
            )
            replayed_completion = store.complete_intent(
                intent.intent_id,
                resulting_state="READY",
                resulting_oid=OID_B,
                evidence={"receipt": "artifact-1"},
            )
            self.assertEqual(IntentStatus.COMPLETED, completed.status)
            self.assertEqual(completed, replayed_completion)

            with self.assertRaises(IdempotencyConflictError):
                store.complete_intent(
                    intent.intent_id,
                    resulting_state="QUARANTINED",
                    resulting_oid=OID_B,
                    evidence={"receipt": "artifact-1"},
                )

    def test_audit_events_are_persistent_idempotent_and_append_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-state-audit-") as temporary:
            store = AxStateStore(Path(temporary) / "ax.db")
            store.initialize()
            event = AuditEvent(
                event_id="audit-1",
                event_type="INTENT_COMPLETED",
                actor="service:integration-controller",
                subject_type="operation-intent",
                subject_id="intent-1",
                payload={"result": "READY"},
                occurred_at=NOW,
                idempotency_key="audit-key-1",
                subject_oid=OID_A,
            )

            store.record_audit_event(event)
            store.record_audit_event(event)
            with store.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM audit_events WHERE id = 'audit-1'"
                ).fetchone()
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE audit_events SET actor = 'other' WHERE id = 'audit-1'"
                    )

            self.assertEqual("service:integration-controller", row["actor"])
            self.assertEqual({"result": "READY"}, json.loads(row["payload_json"]))

            with self.assertRaises(IdempotencyConflictError):
                store.record_audit_event(
                    AuditEvent(
                        event_id="audit-1",
                        event_type="DIFFERENT",
                        actor="service:integration-controller",
                        subject_type="operation-intent",
                        subject_id="intent-1",
                        payload={"result": "READY"},
                        occurred_at=NOW,
                        idempotency_key="audit-key-1",
                        subject_oid=OID_A,
                    )
                )

    def test_active_writer_and_immutable_attempt_invariants(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-state-invariants-") as temporary:
            store = AxStateStore(Path(temporary) / "ax.db")
            store.initialize()
            self._insert_representative_graph(store)

            with self.assertRaises(sqlite3.IntegrityError):
                with store.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        INSERT INTO targets (
                            id, canonical_checkout_path, git_common_dir,
                            source_ref, observed_source_oid, state,
                            created_at, updated_at
                        ) VALUES (
                            'target-2', 'C:/target-2', 'C:/target-2/.git',
                            'refs/heads/main', ?, 'ACTIVE', ?, ?
                        )
                        """,
                        (OID_A, NOW, NOW),
                    )
                    connection.execute(
                        """
                        INSERT INTO runs (
                            id, goal_id, target_id, base_oid, state,
                            idempotency_key, created_at
                        ) VALUES (
                            'run-wrong-target', 'goal-1', 'target-2', ?,
                            'RUNNING', 'wrong-target-key', ?
                        )
                        """,
                        (OID_A, NOW),
                    )

            with self.assertRaises(sqlite3.IntegrityError):
                with store.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        INSERT INTO workspace_leases (
                            id, workspace_id, target_id, goal_id, work_item_id,
                            revision_id, owner, branch_ref, worktree_path,
                            base_oid, expected_head_oid, source_write_scope_json,
                            generated_write_scope_json, state, expires_at,
                            idempotency_key, created_at
                        ) VALUES (
                            'lease-2', 'workspace-2', 'target-1', 'goal-1',
                            'work-1', 'revision-1', 'dev_2',
                            'refs/heads/ax/work/goal-1/work-1/1',
                            'C:/ax/worktrees/workspace-2', ?, ?, '["src"]',
                            '[]', 'ACTIVE', ?, 'lease-key-2', ?
                        )
                        """,
                        (OID_A, OID_A, NOW, NOW),
                    )

            with store.transaction() as connection:
                row = connection.execute(
                    """
                    SELECT attempt_id, ordinal, candidate_oid
                    FROM attempt_candidates
                    WHERE attempt_id = 'attempt-1'
                    """
                ).fetchone()
            self.assertEqual(OID_B, row["candidate_oid"])

            with self.assertRaises(sqlite3.IntegrityError):
                with store.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        UPDATE attempt_candidates SET ordinal = 2
                        WHERE attempt_id = 'attempt-1' AND ordinal = 0
                        """
                    )

            with self.assertRaises(sqlite3.IntegrityError):
                with store.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        INSERT INTO gate_decisions (
                            id, goal_id, activation_id, gate_type, actor_role,
                            subject_oid, decision, profile_digest, evidence_json,
                            idempotency_key, decided_at
                        ) VALUES (
                            'gate-null', 'goal-1', 'activation-1', 'QA_QUALITY',
                            'qa_sdet', NULL, 'APPROVED', 'digest', '["artifact"]',
                            'gate-null-key', ?
                        )
                        """,
                        (NOW,),
                    )

            with self.assertRaises(sqlite3.IntegrityError):
                with store.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        INSERT INTO reviews (
                            id, goal_id, candidate_id, activation_id,
                            reviewer_role, review_type, subject_oid, decision,
                            source_integrity, profile_digest, evidence_json,
                            idempotency_key, created_at
                        ) VALUES (
                            'review-dirty', 'goal-1', 'candidate-1',
                            'activation-1', 'ta', 'CODE_QUALITY', ?,
                            'APPROVED', 'ANALYSIS_DIRTY', 'digest',
                            '["artifact"]', 'review-dirty-key', ?
                        )
                        """,
                        (OID_B, NOW),
                    )

    def test_failed_migration_rolls_back_and_prior_schema_remains_usable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-state-failure-") as temporary:
            db_path = Path(temporary) / "ax.db"
            failing = FailingInvariantStore(db_path)

            with self.assertRaises(SchemaMigrationError):
                failing.initialize()
            self.assertEqual(2, failing.schema_version())

            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO messages (
                        id, thread_id, work_item_id, from_role, to_role, type,
                        payload_json, status, available_at, created_at
                    ) VALUES (
                        'msg-after-failure', 'thread-1', 'work-1', 'pl', 'dev_1',
                        'ASSIGN', '{}', 'PENDING', ?, ?
                    )
                    """,
                    (NOW, NOW),
                )
                count = connection.execute(
                    """
                    SELECT COUNT(*) FROM messages
                    WHERE id = 'msg-after-failure'
                    """
                ).fetchone()[0]
                connection.commit()
            self.assertEqual(1, count)

            recovered = AxStateStore(db_path)
            self.assertEqual(LATEST_SCHEMA_VERSION, recovered.initialize())
            self.assertEqual(
                "msg-after-failure",
                SQLiteMessageQueue(db_path).get("msg-after-failure").id,
            )

    def test_queue_public_flow_survives_versioned_initialization(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-state-queue-") as temporary:
            db_path = Path(temporary) / "ax.db"
            AxStateStore(db_path).initialize()
            queue = SQLiteMessageQueue(db_path)

            message = queue.enqueue(
                thread_id="thread-1",
                work_item_id="legacy-freeform-work-id",
                from_role="pl",
                to_role="dev_1",
                message_type="ASSIGN",
                payload={"task": "implement"},
                dedupe_key="queue-smoke-1",
            )
            replay = queue.enqueue(
                thread_id="thread-1",
                work_item_id="legacy-freeform-work-id",
                from_role="pl",
                to_role="dev_1",
                message_type="ASSIGN",
                payload={"task": "implement"},
                dedupe_key="queue-smoke-1",
            )
            self.assertEqual(message.id, replay.id)
            claimed = queue.claim(to_role="dev_1", consumer_id="dev-1")
            self.assertEqual([message.id], [item.id for item in claimed])
            running = queue.mark_running(message.id, consumer_id="dev-1")
            self.assertEqual("RUNNING", running.status)
            acknowledged = queue.acknowledge(message.id, consumer_id="dev-1")
            self.assertEqual("ACKED", acknowledged.status)
            self.assertEqual(1, len(queue.pending_outbox()))

    @staticmethod
    def _insert_representative_graph(store: AxStateStore) -> None:
        with store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO targets (
                    id, canonical_checkout_path, git_common_dir, source_ref,
                    observed_source_oid, state, created_at, updated_at
                ) VALUES (
                    'target-1', 'C:/target', 'C:/target/.git',
                    'refs/heads/main', ?, 'ACTIVE', ?, ?
                )
                """,
                (OID_A, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO goals (
                    id, target_id, base_oid, state, created_at, updated_at
                ) VALUES ('goal-1', 'target-1', ?, 'ACTIVE', ?, ?)
                """,
                (OID_A, NOW, NOW),
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
                (OID_A, NOW),
            )
            connection.execute(
                """
                INSERT INTO work_items (
                    id, goal_id, title, assigned_owner,
                    source_write_scope_json, state, created_at, updated_at
                ) VALUES (
                    'work-1', 'goal-1', 'Work', 'dev_1',
                    '["src"]', 'IN_PROGRESS', ?, ?
                )
                """,
                (NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO work_revisions (
                    id, work_item_id, revision, owner, base_oid, head_oid,
                    state, idempotency_key, created_at, updated_at
                ) VALUES (
                    'revision-1', 'work-1', 1, 'dev_1', ?, ?,
                    'SUBMITTED', 'revision-key-1', ?, ?
                )
                """,
                (OID_A, OID_B, NOW, NOW),
            )
            for workspace_id, path in (
                ("workspace-1", "C:/ax/worktrees/workspace-1"),
                ("workspace-2", "C:/ax/worktrees/workspace-2"),
            ):
                connection.execute(
                    """
                    INSERT INTO workspaces (
                        id, target_id, goal_id, kind, path, branch_ref,
                        subject_oid, state, created_at, updated_at
                    ) VALUES (?, 'target-1', 'goal-1', 'DEVELOPMENT', ?,
                        NULL, ?, 'ACTIVE', ?, ?)
                    """,
                    (workspace_id, path, OID_A, NOW, NOW),
                )
            connection.execute(
                """
                INSERT INTO workspace_leases (
                    id, workspace_id, target_id, goal_id, work_item_id,
                    revision_id, owner, branch_ref, worktree_path,
                    base_oid, expected_head_oid, source_write_scope_json,
                    generated_write_scope_json, state, expires_at,
                    idempotency_key, created_at
                ) VALUES (
                    'lease-1', 'workspace-1', 'target-1', 'goal-1',
                    'work-1', 'revision-1', 'dev_1',
                    'refs/heads/ax/work/goal-1/work-1/1',
                    'C:/ax/worktrees/workspace-1', ?, ?, '["src"]',
                    '[]', 'ACTIVE', ?, 'lease-key-1', ?
                )
                """,
                (OID_A, OID_B, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO activations (
                    id, target_id, goal_id, run_id, workspace_id,
                    subject_oid, role, gate_or_task, state,
                    idempotency_key, created_at, updated_at
                ) VALUES (
                    'activation-1', 'target-1', 'goal-1', 'run-1',
                    'workspace-1', ?, 'pl', 'candidate-selection',
                    'RESULT_PERSISTED', 'activation-key-1', ?, ?
                )
                """,
                (OID_B, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO candidate_submissions (
                    id, goal_id, work_item_id, revision_id, lease_id,
                    branch_ref, expected_previous_oid, candidate_oid,
                    self_test_evidence_json, state, idempotency_key, created_at
                ) VALUES (
                    'candidate-1', 'goal-1', 'work-1', 'revision-1', 'lease-1',
                    'refs/heads/ax/work/goal-1/work-1/1', ?, ?,
                    '["test-1"]', 'APPROVED', 'candidate-key-1', ?
                )
                """,
                (OID_A, OID_B, NOW),
            )
            connection.execute(
                """
                INSERT INTO gate_decisions (
                    id, goal_id, activation_id, gate_type, actor_role,
                    subject_oid, decision, profile_digest, evidence_json,
                    idempotency_key, decided_at
                ) VALUES (
                    'gate-pl-1', 'goal-1', 'activation-1',
                    'PL_CANDIDATE_SELECTION', 'pl', ?, 'APPROVED',
                    'digest', '["artifact-1"]', 'gate-pl-key-1', ?
                )
                """,
                (OID_B, NOW),
            )
            connection.execute(
                """
                INSERT INTO integration_plans (
                    id, goal_id, base_oid, merge_strategy, pl_decision_id,
                    state, idempotency_key, created_at, approved_at
                ) VALUES (
                    'plan-1', 'goal-1', ?, 'no-ff', 'gate-pl-1',
                    'APPROVED', 'plan-key-1', ?, ?
                )
                """,
                (OID_A, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO integration_attempts (
                    id, plan_id, goal_id, base_oid, merge_strategy,
                    state, idempotency_key, created_at
                ) VALUES (
                    'attempt-1', 'plan-1', 'goal-1', ?, 'no-ff',
                    'PLANNED', 'attempt-key-1', ?
                )
                """,
                (OID_A, NOW),
            )
            connection.execute(
                """
                INSERT INTO attempt_candidates (
                    attempt_id, ordinal, candidate_id, candidate_oid
                ) VALUES ('attempt-1', 0, 'candidate-1', ?)
                """,
                (OID_B,),
            )


if __name__ == "__main__":
    unittest.main()
