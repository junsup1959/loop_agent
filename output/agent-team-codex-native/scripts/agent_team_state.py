from __future__ import annotations

"""Versioned SQLite state and journal contracts for Agent-Team worktrees.

The first migration is the pre-existing message queue schema.  Later
migrations extend that same database with worktree execution state; they do
not replace the queue or establish a second control plane.
"""

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from .agent_team_domain import (
        AdmissionDecision,
        AuditEvent,
        CircuitState,
        ContractAttemptKind,
        ContractAttemptState,
        ContractState,
        ContractViolationCode,
        DefinitionKind,
        EvidenceDisposition,
        IntentRecord,
        IntentStatus,
        McpHealthStatus,
        ResultDisposition,
        SerenaMemoryBinding,
        TokenLedgerEntryKind,
        ViolationDisposition,
        require_boolean,
        require_definition_kind,
        require_identifier,
        require_nonnegative,
        require_nonempty,
        require_oid,
        require_positive,
        require_sha256,
        thaw_json,
    )
except ImportError:  # Direct script/module execution from the repository root.
    from agent_team_domain import (
        AdmissionDecision,
        AuditEvent,
        CircuitState,
        ContractAttemptKind,
        ContractAttemptState,
        ContractState,
        ContractViolationCode,
        DefinitionKind,
        EvidenceDisposition,
        IntentRecord,
        IntentStatus,
        McpHealthStatus,
        ResultDisposition,
        SerenaMemoryBinding,
        TokenLedgerEntryKind,
        ViolationDisposition,
        require_boolean,
        require_definition_kind,
        require_identifier,
        require_nonnegative,
        require_nonempty,
        require_oid,
        require_positive,
        require_sha256,
        thaw_json,
    )


LATEST_SCHEMA_VERSION = 4
BackupHook = Callable[[Path, int], Path]


class AxStateError(RuntimeError):
    """Base error for versioned Agent-Team state."""


class SchemaMigrationError(AxStateError):
    """Raised when a schema cannot be upgraded atomically."""


class SchemaCompatibilityError(AxStateError):
    """Raised when an existing database has an unknown or malformed schema."""


class IdempotencyConflictError(AxStateError):
    """Raised when a reused idempotency key describes a different operation."""


class IntentStateError(AxStateError):
    """Raised when an operation intent cannot make the requested transition."""


_MCP_RECEIPT_BROKER_AUTHORITY = object()


class _McpReceiptWriter:
    """Opaque per-store handle held only by the runtime MCP broker."""

    __slots__ = ("__store", "__authority")

    def __init__(self, store: "AxStateStore", authority: object) -> None:
        self.__store = store
        self.__authority = authority

    def record(
        self,
        *,
        contract_id: str,
        attempt_id: str,
        server_name: str,
        tool_name: str,
        input_digest: str,
        output_digest: str,
        idempotency_key: str,
    ) -> str:
        return self.__store._record_trusted_mcp_invocation_receipt(
            _authority=self.__authority,
            contract_id=contract_id,
            attempt_id=attempt_id,
            server_name=server_name,
            tool_name=tool_name,
            input_digest=input_digest,
            output_digest=output_digest,
            idempotency_key=idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class DurableActivationCommit:
    """Identifiers proved durable by one activation result transaction."""

    activation_result_id: str
    message_ids: tuple[str, ...] = ()
    outbox_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SchemaMigration:
    version: int
    name: str
    statements: tuple[str, ...]
    destructive: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version < 1
        ):
            raise ValueError("migration version must be a positive integer")
        require_identifier(self.name, "migration name")
        if not self.statements or any(not statement.strip() for statement in self.statements):
            raise ValueError("migration statements must be non-empty")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def compact_json(value: Any) -> str:
    return json.dumps(
        thaw_json(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def stable_identifier(prefix: str, *parts: Any) -> str:
    """Return a deterministic, domain-safe identifier for immutable v4 records."""

    prefix = require_identifier(prefix, "prefix")
    payload = compact_json(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:32]}"


def _normalize_outgoing_messages(
    outgoing_messages: Sequence[Mapping[str, Any]],
    *,
    transaction_key: str,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(outgoing_messages, Sequence) or isinstance(
        outgoing_messages, (str, bytes)
    ):
        raise ValueError("outgoing_messages must be a sequence")
    normalized: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(outgoing_messages):
        if not isinstance(raw, Mapping):
            raise ValueError("each outgoing message must be an object")
        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("outgoing message payload must be an object")
        priority = raw.get("priority", 0)
        max_attempts = raw.get("max_attempts", 5)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ValueError("outgoing message priority must be an integer")
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
        ):
            raise ValueError("outgoing message max_attempts must be positive")
        parent_message_id = raw.get("parent_message_id")
        if parent_message_id is not None:
            parent_message_id = require_identifier(
                parent_message_id, "parent_message_id"
            )
        dedupe_key = require_nonempty(
            raw.get("dedupe_key") or f"{transaction_key}:message:{ordinal}",
            "dedupe_key",
        )
        message_id = stable_identifier("message", dedupe_key)
        outbox_id = stable_identifier("outbox", message_id)
        message = {
            "message_id": message_id,
            "outbox_id": outbox_id,
            "thread_id": require_nonempty(raw.get("thread_id"), "thread_id"),
            "work_item_id": require_nonempty(
                raw.get("work_item_id"), "work_item_id"
            ),
            "parent_message_id": parent_message_id,
            "from_role": require_nonempty(raw.get("from_role"), "from_role"),
            "to_role": require_nonempty(raw.get("to_role"), "to_role"),
            "message_type": require_nonempty(raw.get("type"), "type"),
            "priority": priority,
            "payload_json": compact_json(dict(payload)),
            "max_attempts": max_attempts,
            "dedupe_key": dedupe_key,
        }
        message["outbox_payload_json"] = compact_json(
            {
                "message_id": message_id,
                "thread_id": message["thread_id"],
                "work_item_id": message["work_item_id"],
                "to_role": message["to_role"],
                "message_type": message["message_type"],
            }
        )
        normalized.append(message)
    if len({item["dedupe_key"] for item in normalized}) != len(normalized):
        raise ValueError("outgoing message dedupe keys must be unique")
    return tuple(normalized)


def _persist_or_verify_outgoing_messages(
    connection: sqlite3.Connection,
    messages: Sequence[Mapping[str, Any]],
    *,
    occurred_at: str,
    replay: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    message_ids: list[str] = []
    outbox_ids: list[str] = []
    for item in messages:
        existing_message = connection.execute(
            "SELECT * FROM messages WHERE id = ? OR dedupe_key = ?",
            (item["message_id"], item["dedupe_key"]),
        ).fetchone()
        existing_outbox = connection.execute(
            "SELECT * FROM outbox WHERE id = ? OR message_id = ?",
            (item["outbox_id"], item["message_id"]),
        ).fetchone()
        if replay and (existing_message is None or existing_outbox is None):
            raise IntentStateError(
                "activation result replay is missing a message or outbox fact"
            )
        if not replay and (existing_message is not None or existing_outbox is not None):
            raise IntentStateError(
                "partial message/outbox state predates activation result commit"
            )
        if existing_message is not None:
            expected_message = {
                "id": item["message_id"],
                "thread_id": item["thread_id"],
                "work_item_id": item["work_item_id"],
                "parent_message_id": item["parent_message_id"],
                "from_role": item["from_role"],
                "to_role": item["to_role"],
                "type": item["message_type"],
                "priority": item["priority"],
                "payload_json": item["payload_json"],
                "status": "PENDING",
                "max_attempts": item["max_attempts"],
                "dedupe_key": item["dedupe_key"],
            }
            if any(existing_message[key] != value for key, value in expected_message.items()):
                raise IdempotencyConflictError(
                    "activation result message was replayed differently"
                )
        else:
            connection.execute(
                """
                INSERT INTO messages (
                    id, thread_id, work_item_id, parent_message_id,
                    from_role, to_role, type, priority, payload_json,
                    status, available_at, max_attempts, dedupe_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
                """,
                (
                    item["message_id"], item["thread_id"], item["work_item_id"],
                    item["parent_message_id"], item["from_role"], item["to_role"],
                    item["message_type"], item["priority"], item["payload_json"],
                    occurred_at, item["max_attempts"], item["dedupe_key"], occurred_at,
                ),
            )
        if existing_outbox is not None:
            expected_outbox = {
                "id": item["outbox_id"],
                "message_id": item["message_id"],
                "event_type": "MESSAGE_ENQUEUED",
                "payload_json": item["outbox_payload_json"],
                "status": "PENDING",
            }
            if any(existing_outbox[key] != value for key, value in expected_outbox.items()):
                raise IdempotencyConflictError(
                    "activation result outbox was replayed differently"
                )
        else:
            connection.execute(
                """
                INSERT INTO outbox (
                    id, message_id, event_type, payload_json,
                    status, available_at, created_at
                ) VALUES (?, ?, 'MESSAGE_ENQUEUED', ?, 'PENDING', ?, ?)
                """,
                (
                    item["outbox_id"], item["message_id"],
                    item["outbox_payload_json"], occurred_at, occurred_at,
                ),
            )
        message_ids.append(item["message_id"])
        outbox_ids.append(item["outbox_id"])
    return tuple(message_ids), tuple(outbox_ids)


def _enum_text(value: Any, enum_type: type[Any], field: str) -> str:
    if isinstance(value, enum_type):
        return value.value
    candidate = require_nonempty(value, field).upper().replace("-", "_")
    try:
        return enum_type(candidate).value
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field} must be one of: {allowed}") from exc


LEGACY_QUEUE_MIGRATION = SchemaMigration(
    version=1,
    name="legacy-queue",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS messages (
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
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN (
                    'PENDING', 'CLAIMED', 'RUNNING',
                    'ACKED', 'RETRY', 'DEAD_LETTER'
                )),
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
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_messages_delivery
            ON messages(to_role, status, available_at, priority DESC, seq)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_messages_thread
            ON messages(thread_id, seq)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_messages_work_item
            ON messages(work_item_id, seq)
        """,
        """
        CREATE TABLE IF NOT EXISTS outbox (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            message_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING', 'PUBLISHED', 'RETRY', 'DEAD_LETTER')),
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            published_at TEXT,
            last_error TEXT,
            FOREIGN KEY(message_id) REFERENCES messages(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_outbox_delivery
            ON outbox(status, available_at, seq)
        """,
        """
        CREATE TABLE IF NOT EXISTS thread_snapshots (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            target_role TEXT NOT NULL,
            covered_through_seq INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(thread_id, target_role, covered_through_seq)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_thread_snapshots_latest
            ON thread_snapshots(thread_id, target_role, covered_through_seq DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_messages_context_projection
            ON messages(thread_id, work_item_id, seq)
        """,
        """
        CREATE TABLE IF NOT EXISTS project_knowledge_state (
            repo_id TEXT PRIMARY KEY,
            project_path TEXT NOT NULL,
            baseline_oid TEXT,
            inspected_oid TEXT,
            source_fingerprint TEXT,
            state TEXT NOT NULL CHECK(state IN (
                'new', 'refresh_required', 'ready', 'deferred'
            )),
            memory_manifest_json TEXT NOT NULL DEFAULT '{}',
            owner_seat_id TEXT,
            evidence_artifact_ref TEXT,
            memory_manifest_sha256 TEXT,
            last_request_message_id TEXT,
            acknowledged_at TEXT,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_project_knowledge_state_state
            ON project_knowledge_state(state, updated_at DESC)
        """,
    ),
)


AX_DOMAIN_MIGRATION = SchemaMigration(
    version=2,
    name="ax-domain",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS targets (
            id TEXT PRIMARY KEY,
            canonical_checkout_path TEXT NOT NULL UNIQUE,
            git_common_dir TEXT NOT NULL UNIQUE,
            source_ref TEXT NOT NULL,
            observed_source_oid TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'REGISTERED', 'ACTIVE', 'RESYNC_REQUIRED', 'QUARANTINED', 'RETIRED'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS managed_repositories (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL UNIQUE,
            repository_path TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK(state IN (
                'PROVISIONING', 'READY', 'RESYNC_REQUIRED', 'QUARANTINED'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS repository_snapshots (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            managed_repository_id TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            source_oid TEXT NOT NULL,
            imported_oid TEXT NOT NULL,
            evidence_ref TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE RESTRICT,
            FOREIGN KEY(managed_repository_id)
                REFERENCES managed_repositories(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS goals (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            base_oid TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'CREATED', 'ACTIVE', 'BLOCKED', 'APPROVED',
                'COMPLETED', 'ABORTED'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            base_oid TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'CREATED', 'RUNNING', 'BLOCKED', 'COMPLETED',
                'FAILED', 'CANCELLED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS work_items (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            title TEXT NOT NULL,
            assigned_owner TEXT NOT NULL,
            source_write_scope_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'PLANNED', 'ASSIGNED', 'IN_PROGRESS', 'REVIEW_PENDING',
                'REWORK_REQUIRED', 'ACCEPTED', 'CANCELLED'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS work_revisions (
            id TEXT PRIMARY KEY,
            work_item_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision >= 1),
            owner TEXT NOT NULL,
            base_oid TEXT NOT NULL,
            head_oid TEXT,
            state TEXT NOT NULL CHECK(state IN (
                'CREATED', 'ACTIVE', 'SUBMITTED', 'REVIEWED',
                'SUPERSEDED', 'REJECTED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(work_item_id, revision),
            FOREIGN KEY(work_item_id) REFERENCES work_items(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            goal_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('DEVELOPMENT', 'INTEGRATION', 'REVIEW')),
            path TEXT NOT NULL UNIQUE,
            branch_ref TEXT,
            subject_oid TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'PROVISIONING', 'READY', 'ACTIVE', 'RELEASING',
                'RELEASED', 'QUARANTINED'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE RESTRICT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workspace_leases (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            goal_id TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            branch_ref TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            base_oid TEXT NOT NULL,
            expected_head_oid TEXT NOT NULL,
            source_write_scope_json TEXT NOT NULL,
            generated_write_scope_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'ACTIVE', 'RELEASED', 'EXPIRED', 'QUARANTINED'
            )),
            expires_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            released_at TEXT,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE RESTRICT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(work_item_id) REFERENCES work_items(id) ON DELETE RESTRICT,
            FOREIGN KEY(revision_id) REFERENCES work_revisions(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS activations (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            goal_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            workspace_id TEXT,
            sandbox_path TEXT,
            subject_oid TEXT NOT NULL,
            role TEXT NOT NULL,
            gate_or_task TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'CREATED', 'PROFILE_BOUND', 'WORKSPACE_BOUND', 'RUNNING',
                'RESULT_PERSISTED', 'PROFILE_REVOKED', 'RESOURCES_RELEASED',
                'TERMINATED', 'REVOKE_FAILED', 'QUARANTINED',
                'RECOVERY_CLEANED'
            )),
            process_id INTEGER,
            result_json TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            terminated_at TEXT,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE RESTRICT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS profile_bindings (
            activation_id TEXT PRIMARY KEY,
            professional_skill_id TEXT NOT NULL
                CHECK(professional_skill_id = 'professional-profile-runtime'),
            compiled_profile_ref TEXT NOT NULL,
            compiled_profile_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'BOUND', 'REVOKED', 'REVOKE_FAILED'
            )),
            bound_at TEXT NOT NULL,
            revoked_at TEXT,
            FOREIGN KEY(activation_id) REFERENCES activations(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS profile_reference_bindings (
            activation_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            reference_kind TEXT NOT NULL CHECK(reference_kind IN (
                'ROLE', 'GATE_OR_TASK', 'PRIMARY_TECHNOLOGY',
                'SECONDARY_TECHNOLOGY', 'TOOLCHAIN'
            )),
            reference_path TEXT NOT NULL,
            reference_version TEXT NOT NULL,
            reference_sha256 TEXT NOT NULL,
            PRIMARY KEY(activation_id, ordinal),
            UNIQUE(activation_id, reference_kind),
            FOREIGN KEY(activation_id)
                REFERENCES profile_bindings(activation_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS candidate_submissions (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            lease_id TEXT NOT NULL,
            branch_ref TEXT NOT NULL,
            expected_previous_oid TEXT NOT NULL,
            candidate_oid TEXT NOT NULL,
            self_test_evidence_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'SUBMITTED', 'REVIEW_PENDING', 'APPROVED',
                'REJECTED', 'SUPERSEDED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(work_item_id) REFERENCES work_items(id) ON DELETE RESTRICT,
            FOREIGN KEY(revision_id) REFERENCES work_revisions(id) ON DELETE RESTRICT,
            FOREIGN KEY(lease_id) REFERENCES workspace_leases(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            candidate_id TEXT,
            activation_id TEXT NOT NULL,
            reviewer_role TEXT NOT NULL,
            review_type TEXT NOT NULL CHECK(review_type IN (
                'CODE_QUALITY', 'ARCHITECTURE', 'QUALITY', 'BUILD', 'REQUIREMENTS'
            )),
            subject_oid TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN (
                'PENDING', 'APPROVED', 'REJECTED', 'NEEDS_REWORK', 'INVALIDATED'
            )),
            source_integrity TEXT NOT NULL CHECK(source_integrity IN (
                'CLEAN', 'ANALYSIS_DIRTY', 'INVALIDATED'
            )),
            profile_digest TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            CHECK(decision <> 'APPROVED' OR source_integrity = 'CLEAN'),
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(candidate_id)
                REFERENCES candidate_submissions(id) ON DELETE RESTRICT,
            FOREIGN KEY(activation_id) REFERENCES activations(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS gate_decisions (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            activation_id TEXT NOT NULL,
            review_id TEXT,
            gate_type TEXT NOT NULL CHECK(gate_type IN (
                'TA_CODE_QUALITY', 'TA_ARCHITECTURE', 'QA_QUALITY', 'BUILD',
                'PL_CANDIDATE_SELECTION', 'PL_INTEGRATION', 'PM_REQUIREMENTS'
            )),
            actor_role TEXT NOT NULL,
            subject_oid TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN (
                'APPROVED', 'REJECTED', 'NEEDS_REWORK'
            )),
            profile_digest TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            decided_at TEXT NOT NULL,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(activation_id) REFERENCES activations(id) ON DELETE RESTRICT,
            FOREIGN KEY(review_id) REFERENCES reviews(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS integration_plans (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            base_oid TEXT NOT NULL,
            merge_strategy TEXT NOT NULL,
            pl_decision_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'PLANNED', 'APPROVED', 'EXECUTING', 'COMPLETED', 'SUPERSEDED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(pl_decision_id) REFERENCES gate_decisions(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS integration_attempts (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            goal_id TEXT NOT NULL,
            base_oid TEXT NOT NULL,
            merge_strategy TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'PLANNED', 'PREFLIGHTING', 'MERGING', 'CONFLICTED',
                'EVIDENCE_PERSISTED', 'REWORK_REQUIRED', 'INTERRUPTED',
                'QUARANTINED', 'RECREATED', 'MERGED', 'QA_PENDING',
                'QA_FAILED', 'QA_PASSED', 'BUILD_PENDING', 'BUILD_FAILED',
                'BUILD_PASSED', 'GATE_PENDING', 'APPROVED'
            )),
            result_oid TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            environment_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(plan_id) REFERENCES integration_plans(id) ON DELETE RESTRICT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS attempt_candidates (
            attempt_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            candidate_id TEXT NOT NULL,
            candidate_oid TEXT NOT NULL,
            PRIMARY KEY(attempt_id, ordinal),
            UNIQUE(attempt_id, candidate_id),
            UNIQUE(attempt_id, candidate_oid),
            FOREIGN KEY(attempt_id)
                REFERENCES integration_attempts(id) ON DELETE RESTRICT,
            FOREIGN KEY(candidate_id)
                REFERENCES candidate_submissions(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS quality_runs (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            activation_id TEXT NOT NULL,
            subject_oid TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'CREATED', 'RUNNING', 'PASSED', 'FAILED', 'INVALIDATED'
            )),
            source_integrity TEXT NOT NULL CHECK(source_integrity IN (
                'CLEAN', 'ANALYSIS_DIRTY', 'INVALIDATED'
            )),
            evidence_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            CHECK(state <> 'PASSED' OR source_integrity = 'CLEAN'),
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(attempt_id)
                REFERENCES integration_attempts(id) ON DELETE RESTRICT,
            FOREIGN KEY(activation_id) REFERENCES activations(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS build_runs (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            activation_id TEXT NOT NULL,
            subject_oid TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'CREATED', 'RUNNING', 'PASSED', 'FAILED', 'INVALIDATED'
            )),
            source_integrity TEXT NOT NULL CHECK(source_integrity IN (
                'CLEAN', 'ANALYSIS_DIRTY', 'INVALIDATED'
            )),
            evidence_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            CHECK(state <> 'PASSED' OR source_integrity = 'CLEAN'),
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(attempt_id)
                REFERENCES integration_attempts(id) ON DELETE RESTRICT,
            FOREIGN KEY(activation_id) REFERENCES activations(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS promotions (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            approved_oid TEXT NOT NULL,
            expected_source_oid TEXT NOT NULL,
            destination_ref TEXT NOT NULL,
            expected_destination_oid TEXT,
            promoted_oid TEXT,
            required_gate_decision_ids_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'REQUESTED', 'VALIDATING', 'PROMOTED', 'BLOCKED', 'ROLLED_BACK'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS migration_runs (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            legacy_root TEXT NOT NULL,
            runtime_root TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'PLANNED', 'FROZEN', 'RUNNING', 'VERIFYING', 'CUT_OVER',
                'COMPLETED', 'ROLLING_BACK', 'ROLLED_BACK', 'FAILED'
            )),
            manifest_digest TEXT,
            recovery_snapshot_ref TEXT,
            active_pointer_before TEXT,
            active_pointer_after TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS migration_steps (
            id TEXT PRIMARY KEY,
            migration_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
            name TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'ROLLED_BACK'
            )),
            evidence_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT NOT NULL UNIQUE,
            started_at TEXT,
            completed_at TEXT,
            UNIQUE(migration_id, ordinal),
            UNIQUE(migration_id, name),
            FOREIGN KEY(migration_id) REFERENCES migration_runs(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            goal_id TEXT,
            run_id TEXT,
            activation_id TEXT,
            kind TEXT NOT NULL CHECK(kind IN (
                'CONTEXT', 'LOG', 'TEST', 'BUILD', 'REVIEW',
                'INTEGRATION', 'MIGRATION', 'AUDIT', 'OTHER'
            )),
            relative_path TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL,
            byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
            subject_oid TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT,
            FOREIGN KEY(activation_id) REFERENCES activations(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            goal_id TEXT,
            run_id TEXT,
            activation_id TEXT,
            subject_oid TEXT,
            payload_json TEXT NOT NULL,
            idempotency_key TEXT UNIQUE,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT,
            FOREIGN KEY(activation_id) REFERENCES activations(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS operation_intents (
            id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            expected_state TEXT NOT NULL,
            expected_oid TEXT,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'PENDING', 'COMPLETED', 'FAILED', 'QUARANTINED'
            )),
            resulting_state TEXT,
            resulting_oid TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            CHECK(
                (
                    status = 'PENDING'
                    AND completed_at IS NULL
                    AND resulting_state IS NULL
                    AND resulting_oid IS NULL
                )
                OR (
                    status <> 'PENDING'
                    AND completed_at IS NOT NULL
                )
            )
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reconciliation_findings (
            id TEXT PRIMARY KEY,
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            severity TEXT NOT NULL CHECK(severity IN (
                'INFO', 'WARNING', 'ERROR', 'CRITICAL'
            )),
            state TEXT NOT NULL CHECK(state IN (
                'OPEN', 'RECONCILING', 'RESOLVED', 'QUARANTINED'
            )),
            expected_json TEXT NOT NULL,
            observed_json TEXT NOT NULL,
            resolution_json TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            detected_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """,
    ),
)


AX_INVARIANTS_MIGRATION = SchemaMigration(
    version=3,
    name="ax-invariants",
    statements=(
        # Composite ownership is enforced without rebuilding additive v2 tables.
        """
        CREATE TRIGGER IF NOT EXISTS trg_runs_goal_target_insert
        BEFORE INSERT ON runs
        WHEN NOT EXISTS (
            SELECT 1 FROM goals
            WHERE goals.id = NEW.goal_id AND goals.target_id = NEW.target_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'run target must match goal target');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_runs_goal_target_update
        BEFORE UPDATE OF goal_id, target_id ON runs
        WHEN NOT EXISTS (
            SELECT 1 FROM goals
            WHERE goals.id = NEW.goal_id AND goals.target_id = NEW.target_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'run target must match goal target');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_snapshot_repository_target_insert
        BEFORE INSERT ON repository_snapshots
        WHEN NOT EXISTS (
            SELECT 1 FROM managed_repositories
            WHERE managed_repositories.id = NEW.managed_repository_id
              AND managed_repositories.target_id = NEW.target_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'snapshot repository must belong to target');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_workspaces_goal_target_insert
        BEFORE INSERT ON workspaces
        WHEN NOT EXISTS (
            SELECT 1 FROM goals
            WHERE goals.id = NEW.goal_id AND goals.target_id = NEW.target_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'workspace target must match goal target');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_workspace_lease_ownership_insert
        BEFORE INSERT ON workspace_leases
        WHEN NOT EXISTS (
            SELECT 1
            FROM workspaces
            JOIN work_items ON work_items.id = NEW.work_item_id
            JOIN work_revisions ON work_revisions.id = NEW.revision_id
            WHERE workspaces.id = NEW.workspace_id
              AND workspaces.target_id = NEW.target_id
              AND workspaces.goal_id = NEW.goal_id
              AND work_items.goal_id = NEW.goal_id
              AND work_revisions.work_item_id = NEW.work_item_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'lease ownership graph is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_activation_run_ownership_insert
        BEFORE INSERT ON activations
        WHEN NOT EXISTS (
            SELECT 1 FROM runs
            WHERE runs.id = NEW.run_id
              AND runs.goal_id = NEW.goal_id
              AND runs.target_id = NEW.target_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'activation run, goal, and target must match');
        END
        """,
        # Competing ownership lookups and one-active-writer guarantees.
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workspace_leases_active_branch
            ON workspace_leases(target_id, branch_ref)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workspace_leases_active_worktree
            ON workspace_leases(worktree_path)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workspace_leases_active_workspace
            ON workspace_leases(workspace_id)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_workspace_leases_owner_expiry
            ON workspace_leases(owner, state, expires_at)
        """,
        # Exact-OID evidence and promotion aggregation hot paths.
        """
        CREATE INDEX IF NOT EXISTS ix_repository_snapshots_target_created
            ON repository_snapshots(target_id, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_runs_goal_state
            ON runs(goal_id, state, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_work_items_goal_state
            ON work_items(goal_id, state, updated_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_work_revisions_item_state
            ON work_revisions(work_item_id, state, revision DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_activations_subject_state
            ON activations(subject_oid, state, updated_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_candidates_goal_oid
            ON candidate_submissions(goal_id, candidate_oid, state)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_reviews_subject
            ON reviews(goal_id, subject_oid, review_type, decision)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_gate_decisions_subject
            ON gate_decisions(goal_id, subject_oid, gate_type, decision)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_attempts_goal_state
            ON integration_attempts(goal_id, state, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_quality_runs_subject
            ON quality_runs(goal_id, subject_oid, state)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_build_runs_subject
            ON build_runs(goal_id, subject_oid, state)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_promotions_goal_state
            ON promotions(goal_id, approved_oid, state)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_audit_events_subject
            ON audit_events(subject_type, subject_id, seq)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_audit_events_goal
            ON audit_events(goal_id, seq)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_operation_intents_status
            ON operation_intents(status, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_reconciliation_open
            ON reconciliation_findings(state, severity, detected_at)
        """,
        # Ordered candidate lists and already-issued evidence are append-only.
        """
        CREATE TRIGGER IF NOT EXISTS trg_attempt_candidates_no_update
        BEFORE UPDATE ON attempt_candidates
        BEGIN
            SELECT RAISE(ABORT, 'attempt candidate ordering is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_attempt_candidates_no_delete
        BEFORE DELETE ON attempt_candidates
        BEGIN
            SELECT RAISE(ABORT, 'attempt candidate ordering is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_candidate_identity_immutable
        BEFORE UPDATE ON candidate_submissions
        WHEN NEW.goal_id <> OLD.goal_id
          OR NEW.work_item_id <> OLD.work_item_id
          OR NEW.revision_id <> OLD.revision_id
          OR NEW.lease_id <> OLD.lease_id
          OR NEW.branch_ref <> OLD.branch_ref
          OR NEW.expected_previous_oid <> OLD.expected_previous_oid
          OR NEW.candidate_oid <> OLD.candidate_oid
          OR NEW.self_test_evidence_json <> OLD.self_test_evidence_json
        BEGIN
            SELECT RAISE(ABORT, 'candidate identity and OID evidence are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_integration_plan_identity_immutable
        BEFORE UPDATE ON integration_plans
        WHEN NEW.goal_id <> OLD.goal_id
          OR NEW.base_oid <> OLD.base_oid
          OR NEW.merge_strategy <> OLD.merge_strategy
          OR NEW.pl_decision_id <> OLD.pl_decision_id
        BEGIN
            SELECT RAISE(ABORT, 'integration plan inputs are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_integration_attempt_identity_immutable
        BEFORE UPDATE ON integration_attempts
        WHEN NEW.plan_id <> OLD.plan_id
          OR NEW.goal_id <> OLD.goal_id
          OR NEW.base_oid <> OLD.base_oid
          OR NEW.merge_strategy <> OLD.merge_strategy
          OR NEW.environment_json <> OLD.environment_json
        BEGIN
            SELECT RAISE(ABORT, 'integration attempt inputs are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_integration_attempts_no_delete
        BEFORE DELETE ON integration_attempts
        BEGIN
            SELECT RAISE(ABORT, 'integration attempts are retained evidence');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_quality_subject_immutable
        BEFORE UPDATE ON quality_runs
        WHEN NEW.goal_id <> OLD.goal_id
          OR NEW.attempt_id <> OLD.attempt_id
          OR NEW.activation_id <> OLD.activation_id
          OR NEW.subject_oid <> OLD.subject_oid
        BEGIN
            SELECT RAISE(ABORT, 'quality-run subject OID is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_build_subject_immutable
        BEFORE UPDATE ON build_runs
        WHEN NEW.goal_id <> OLD.goal_id
          OR NEW.attempt_id <> OLD.attempt_id
          OR NEW.activation_id <> OLD.activation_id
          OR NEW.subject_oid <> OLD.subject_oid
        BEGIN
            SELECT RAISE(ABORT, 'build-run subject OID is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_promotion_request_immutable
        BEFORE UPDATE ON promotions
        WHEN NEW.goal_id <> OLD.goal_id
          OR NEW.target_id <> OLD.target_id
          OR NEW.approved_oid <> OLD.approved_oid
          OR NEW.expected_source_oid <> OLD.expected_source_oid
          OR NEW.destination_ref <> OLD.destination_ref
          OR NEW.expected_destination_oid IS NOT OLD.expected_destination_oid
          OR NEW.required_gate_decision_ids_json
                <> OLD.required_gate_decision_ids_json
        BEGIN
            SELECT RAISE(ABORT, 'promotion request inputs are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_operation_intent_input_immutable
        BEFORE UPDATE ON operation_intents
        WHEN NEW.operation <> OLD.operation
          OR NEW.idempotency_key <> OLD.idempotency_key
          OR NEW.expected_state <> OLD.expected_state
          OR NEW.expected_oid IS NOT OLD.expected_oid
          OR NEW.payload_json <> OLD.payload_json
        BEGIN
            SELECT RAISE(ABORT, 'operation intent inputs are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_repository_snapshots_no_update
        BEFORE UPDATE ON repository_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'repository snapshots are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_repository_snapshots_no_delete
        BEFORE DELETE ON repository_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'repository snapshots are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_gate_decisions_no_update
        BEFORE UPDATE ON gate_decisions
        BEGIN
            SELECT RAISE(ABORT, 'gate decisions are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_gate_decisions_no_delete
        BEFORE DELETE ON gate_decisions
        BEGIN
            SELECT RAISE(ABORT, 'gate decisions are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit events are append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_delete
        BEFORE DELETE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit events are append-only');
        END
        """,
    ),
)


AX_V4_CONTROL_PLANE_MIGRATION = SchemaMigration(
    version=4,
    name="ax-v4-control-plane",
    statements=(
        # Immutable definition bytes are registered independently of runtime use.
        """
        CREATE TABLE IF NOT EXISTS registered_definitions (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN (
                'WORKFLOW', 'CLAUSE', 'SCHEMA', 'TEMPLATE', 'PROFILE',
                'SKILL', 'MCP_POLICY', 'SERENA_POLICY', 'OTHER'
            )),
            version TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK(
                length(sha256) = 64
                AND sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            source_ref TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            registered_at TEXT NOT NULL,
            UNIQUE(kind, version)
        )
        """,
        # Physical scheduling identities and logical workflow authority stay separate.
        """
        CREATE TABLE IF NOT EXISTS physical_seats (
            id TEXT PRIMARY KEY,
            seat_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'RETIRED')),
            is_merged INTEGER NOT NULL CHECK(is_merged IN (0, 1)),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS logical_capabilities (
            id TEXT PRIMARY KEY,
            capability_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'RETIRED')),
            approval_authority INTEGER NOT NULL
                CHECK(approval_authority IN (0, 1)),
            merge_authority INTEGER NOT NULL CHECK(merge_authority IN (0, 1)),
            nested_spawn_authority INTEGER NOT NULL
                CHECK(nested_spawn_authority IN (0, 1)),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS seat_capability_ownerships (
            physical_seat_id TEXT NOT NULL,
            capability_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('ENABLED', 'DISABLED')),
            idempotency_key TEXT NOT NULL UNIQUE,
            assigned_at TEXT NOT NULL,
            PRIMARY KEY(physical_seat_id, capability_id),
            FOREIGN KEY(physical_seat_id)
                REFERENCES physical_seats(id) ON DELETE RESTRICT,
            FOREIGN KEY(capability_id)
                REFERENCES logical_capabilities(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runtime_slots (
            id TEXT PRIMARY KEY,
            slot_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL CHECK(kind IN ('FIXED', 'ELASTIC')),
            physical_seat_id TEXT UNIQUE,
            elastic_singleton INTEGER UNIQUE,
            state TEXT NOT NULL CHECK(state IN (
                'AVAILABLE', 'OCCUPIED', 'QUARANTINED', 'RETIRED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            CHECK(
                (
                    kind = 'FIXED'
                    AND physical_seat_id IS NOT NULL
                    AND elastic_singleton IS NULL
                )
                OR (
                    kind = 'ELASTIC'
                    AND physical_seat_id IS NULL
                    AND elastic_singleton = 1
                )
            ),
            FOREIGN KEY(physical_seat_id)
                REFERENCES physical_seats(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS worker_identities (
            id TEXT PRIMARY KEY,
            worker_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL CHECK(kind IN ('FIXED', 'ELASTIC')),
            physical_seat_id TEXT,
            state TEXT NOT NULL CHECK(state IN (
                'REGISTERED', 'ACTIVE', 'QUARANTINED', 'RETIRED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            CHECK(
                (kind = 'FIXED' AND physical_seat_id IS NOT NULL)
                OR (kind = 'ELASTIC' AND physical_seat_id IS NULL)
            ),
            FOREIGN KEY(physical_seat_id)
                REFERENCES physical_seats(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS worker_fingerprints (
            id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            fingerprint_sha256 TEXT NOT NULL UNIQUE CHECK(
                length(fingerprint_sha256) = 64
                AND fingerprint_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            runtime_profile_digest TEXT NOT NULL CHECK(
                length(runtime_profile_digest) = 64
                AND runtime_profile_digest NOT GLOB '*[^0-9a-f]*'
            ),
            state TEXT NOT NULL CHECK(state IN (
                'ACTIVE', 'REVOKED', 'QUARANTINED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            CHECK(
                (state = 'ACTIVE' AND revoked_at IS NULL)
                OR (state <> 'ACTIVE')
            ),
            FOREIGN KEY(worker_id)
                REFERENCES worker_identities(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS worker_slot_assignments (
            id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            worker_fingerprint_id TEXT NOT NULL,
            slot_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            is_elastic INTEGER NOT NULL CHECK(is_elastic IN (0, 1)),
            state TEXT NOT NULL CHECK(state IN (
                'ACTIVE', 'RELEASED', 'QUARANTINED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            assigned_at TEXT NOT NULL,
            released_at TEXT,
            CHECK(
                (state = 'ACTIVE' AND released_at IS NULL)
                OR (state <> 'ACTIVE' AND released_at IS NOT NULL)
            ),
            FOREIGN KEY(worker_id)
                REFERENCES worker_identities(id) ON DELETE RESTRICT,
            FOREIGN KEY(worker_fingerprint_id)
                REFERENCES worker_fingerprints(id) ON DELETE RESTRICT,
            FOREIGN KEY(slot_id)
                REFERENCES runtime_slots(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS seat_capability_activations (
            id TEXT PRIMARY KEY,
            physical_seat_id TEXT NOT NULL,
            capability_id TEXT NOT NULL,
            slot_id TEXT NOT NULL,
            worker_assignment_id TEXT NOT NULL,
            goal_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'ACTIVE', 'RELEASED', 'QUARANTINED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            activated_at TEXT NOT NULL,
            released_at TEXT,
            CHECK(
                (state = 'ACTIVE' AND released_at IS NULL)
                OR (state <> 'ACTIVE' AND released_at IS NOT NULL)
            ),
            FOREIGN KEY(physical_seat_id, capability_id)
                REFERENCES seat_capability_ownerships(
                    physical_seat_id, capability_id
                ) ON DELETE RESTRICT,
            FOREIGN KEY(slot_id)
                REFERENCES runtime_slots(id) ON DELETE RESTRICT,
            FOREIGN KEY(worker_assignment_id)
                REFERENCES worker_slot_assignments(id) ON DELETE RESTRICT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT
        )
        """,
        # Versioned workflow definitions own legal states and transitions.
        """
        CREATE TABLE IF NOT EXISTS workflow_definitions (
            id TEXT PRIMARY KEY,
            definition_id TEXT NOT NULL UNIQUE,
            workflow_key TEXT NOT NULL,
            version TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'REGISTERED', 'ACTIVE', 'RETIRED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            UNIQUE(workflow_key, version),
            FOREIGN KEY(definition_id)
                REFERENCES registered_definitions(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workflow_states (
            workflow_definition_id TEXT NOT NULL,
            state_key TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            is_initial INTEGER NOT NULL CHECK(is_initial IN (0, 1)),
            is_terminal INTEGER NOT NULL CHECK(is_terminal IN (0, 1)),
            PRIMARY KEY(workflow_definition_id, state_key),
            UNIQUE(workflow_definition_id, ordinal),
            FOREIGN KEY(workflow_definition_id)
                REFERENCES workflow_definitions(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workflow_transitions (
            id TEXT PRIMARY KEY,
            workflow_definition_id TEXT NOT NULL,
            transition_key TEXT NOT NULL,
            from_state_key TEXT NOT NULL,
            to_state_key TEXT NOT NULL,
            capability_id TEXT NOT NULL,
            result_kind TEXT NOT NULL,
            failure_route TEXT NOT NULL,
            requires_serena_onboarding INTEGER NOT NULL
                CHECK(requires_serena_onboarding IN (0, 1)),
            output_schema_definition_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'RETIRED')),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            UNIQUE(workflow_definition_id, transition_key),
            UNIQUE(
                workflow_definition_id,
                from_state_key,
                to_state_key,
                capability_id
            ),
            FOREIGN KEY(workflow_definition_id, from_state_key)
                REFERENCES workflow_states(
                    workflow_definition_id, state_key
                ) ON DELETE RESTRICT,
            FOREIGN KEY(workflow_definition_id, to_state_key)
                REFERENCES workflow_states(
                    workflow_definition_id, state_key
                ) ON DELETE RESTRICT,
            FOREIGN KEY(capability_id)
                REFERENCES logical_capabilities(id) ON DELETE RESTRICT,
            FOREIGN KEY(output_schema_definition_id)
                REFERENCES registered_definitions(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workflow_instances (
            id TEXT PRIMARY KEY,
            workflow_definition_id TEXT NOT NULL,
            goal_id TEXT NOT NULL,
            run_id TEXT NOT NULL UNIQUE,
            current_state_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'CREATED', 'ACTIVE', 'COMPLETED', 'CANCELLED', 'QUARANTINED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            CHECK(
                (status IN ('CREATED', 'ACTIVE') AND completed_at IS NULL)
                OR (status NOT IN ('CREATED', 'ACTIVE') AND completed_at IS NOT NULL)
            ),
            FOREIGN KEY(workflow_definition_id, current_state_key)
                REFERENCES workflow_states(
                    workflow_definition_id, state_key
                ) ON DELETE RESTRICT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workflow_transition_receipts (
            id TEXT PRIMARY KEY,
            workflow_instance_id TEXT NOT NULL,
            workflow_transition_id TEXT NOT NULL,
            from_state_key TEXT NOT NULL,
            to_state_key TEXT NOT NULL,
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest) = 64
                AND evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            idempotency_key TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY(workflow_instance_id)
                REFERENCES workflow_instances(id) ON DELETE RESTRICT,
            FOREIGN KEY(workflow_transition_id)
                REFERENCES workflow_transitions(id) ON DELETE RESTRICT
        )
        """,
        # Repository and runtime confinement records are explicit v4 bindings.
        """
        CREATE TABLE IF NOT EXISTS repository_registrations (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            managed_repository_id TEXT UNIQUE,
            canonical_path TEXT NOT NULL UNIQUE,
            git_common_dir TEXT NOT NULL UNIQUE,
            source_oid TEXT NOT NULL CHECK(
                length(source_oid) IN (40, 64)
                AND source_oid = lower(source_oid)
                AND source_oid NOT GLOB '*[^0-9a-f]*'
            ),
            state TEXT NOT NULL CHECK(state IN (
                'REGISTERED', 'ACTIVE', 'RESYNC_REQUIRED',
                'QUARANTINED', 'RETIRED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            registered_at TEXT NOT NULL,
            FOREIGN KEY(target_id)
                REFERENCES targets(id) ON DELETE RESTRICT,
            FOREIGN KEY(managed_repository_id)
                REFERENCES managed_repositories(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runtime_leases (
            id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL,
            goal_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            slot_id TEXT NOT NULL,
            worker_assignment_id TEXT NOT NULL,
            lease_kind TEXT NOT NULL CHECK(lease_kind IN (
                'DEVELOPMENT', 'INTEGRATION', 'REVIEW', 'ADVISORY'
            )),
            branch_ref TEXT,
            worktree_path TEXT NOT NULL,
            base_oid TEXT NOT NULL CHECK(
                length(base_oid) IN (40, 64)
                AND base_oid = lower(base_oid)
                AND base_oid NOT GLOB '*[^0-9a-f]*'
            ),
            expected_head_oid TEXT NOT NULL CHECK(
                length(expected_head_oid) IN (40, 64)
                AND expected_head_oid = lower(expected_head_oid)
                AND expected_head_oid NOT GLOB '*[^0-9a-f]*'
            ),
            write_roots_json TEXT NOT NULL,
            protected_roots_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'ACTIVE', 'RELEASED', 'EXPIRED', 'QUARANTINED'
            )),
            expires_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            released_at TEXT,
            CHECK(
                lease_kind NOT IN ('DEVELOPMENT', 'INTEGRATION')
                OR branch_ref IS NOT NULL
            ),
            CHECK(
                (state = 'ACTIVE' AND released_at IS NULL)
                OR (state <> 'ACTIVE' AND released_at IS NOT NULL)
            ),
            FOREIGN KEY(repository_id)
                REFERENCES repository_registrations(id) ON DELETE RESTRICT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT,
            FOREIGN KEY(slot_id)
                REFERENCES runtime_slots(id) ON DELETE RESTRICT,
            FOREIGN KEY(worker_assignment_id)
                REFERENCES worker_slot_assignments(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sandbox_bindings (
            id TEXT PRIMARY KEY,
            lease_id TEXT NOT NULL UNIQUE,
            repository_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            slot_id TEXT NOT NULL,
            subject_oid TEXT NOT NULL CHECK(
                length(subject_oid) IN (40, 64)
                AND subject_oid = lower(subject_oid)
                AND subject_oid NOT GLOB '*[^0-9a-f]*'
            ),
            cwd TEXT NOT NULL,
            source_root TEXT NOT NULL,
            source_read_only INTEGER NOT NULL CHECK(source_read_only IN (0, 1)),
            writable_roots_json TEXT NOT NULL,
            backend TEXT NOT NULL,
            attestation_digest TEXT NOT NULL CHECK(
                length(attestation_digest) = 64
                AND attestation_digest NOT GLOB '*[^0-9a-f]*'
            ),
            state TEXT NOT NULL CHECK(state IN (
                'ACTIVE', 'RELEASED', 'QUARANTINED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            bound_at TEXT NOT NULL,
            released_at TEXT,
            CHECK(
                (state = 'ACTIVE' AND released_at IS NULL)
                OR (state <> 'ACTIVE' AND released_at IS NOT NULL)
            ),
            FOREIGN KEY(lease_id)
                REFERENCES runtime_leases(id) ON DELETE RESTRICT,
            FOREIGN KEY(repository_id)
                REFERENCES repository_registrations(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT,
            FOREIGN KEY(slot_id)
                REFERENCES runtime_slots(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS oid_authorities (
            id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL,
            goal_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            lease_id TEXT NOT NULL,
            sandbox_binding_id TEXT NOT NULL,
            authority_kind TEXT NOT NULL CHECK(authority_kind IN (
                'BASE', 'SUBJECT', 'APPROVED', 'INTEGRATION', 'FAILURE'
            )),
            oid TEXT NOT NULL CHECK(
                length(oid) IN (40, 64)
                AND oid = lower(oid)
                AND oid NOT GLOB '*[^0-9a-f]*'
            ),
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest) = 64
                AND evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            state TEXT NOT NULL CHECK(state IN (
                'ACTIVE', 'SUPERSEDED', 'QUARANTINED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY(repository_id)
                REFERENCES repository_registrations(id) ON DELETE RESTRICT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT,
            FOREIGN KEY(lease_id)
                REFERENCES runtime_leases(id) ON DELETE RESTRICT,
            FOREIGN KEY(sandbox_binding_id)
                REFERENCES sandbox_bindings(id) ON DELETE RESTRICT
        )
        """,
        # A compiled contract binds every scheduling, authority, OID, and digest input.
        """
        CREATE TABLE IF NOT EXISTS activation_contracts (
            id TEXT PRIMARY KEY,
            workflow_instance_id TEXT NOT NULL,
            workflow_transition_id TEXT NOT NULL,
            goal_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            physical_seat_id TEXT,
            capability_id TEXT NOT NULL,
            seat_capability_activation_id TEXT,
            worker_id TEXT NOT NULL,
            worker_fingerprint_id TEXT NOT NULL,
            slot_id TEXT NOT NULL,
            worker_assignment_id TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            lease_id TEXT NOT NULL,
            sandbox_binding_id TEXT NOT NULL,
            oid_authority_id TEXT NOT NULL,
            base_oid TEXT NOT NULL CHECK(
                length(base_oid) IN (40, 64)
                AND base_oid = lower(base_oid)
                AND base_oid NOT GLOB '*[^0-9a-f]*'
            ),
            subject_oid TEXT NOT NULL CHECK(
                length(subject_oid) IN (40, 64)
                AND subject_oid = lower(subject_oid)
                AND subject_oid NOT GLOB '*[^0-9a-f]*'
            ),
            contract_definition_id TEXT NOT NULL,
            output_schema_definition_id TEXT NOT NULL,
            contract_digest TEXT NOT NULL CHECK(
                length(contract_digest) = 64
                AND contract_digest NOT GLOB '*[^0-9a-f]*'
            ),
            packet_digest TEXT NOT NULL CHECK(
                length(packet_digest) = 64
                AND packet_digest NOT GLOB '*[^0-9a-f]*'
            ),
            context_char_budget INTEGER NOT NULL
                CHECK(context_char_budget BETWEEN 1 AND 12000),
            max_attempts INTEGER NOT NULL CHECK(max_attempts IN (1, 2)),
            state TEXT NOT NULL CHECK(state IN (
                'ISSUED', 'ADMITTED', 'REJECTED', 'RUNNING',
                'RESULT_RECORDED', 'COMPLETED', 'QUARANTINED', 'CANCELLED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            issued_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            CHECK(
                (
                    state IN ('ISSUED', 'ADMITTED', 'RUNNING', 'RESULT_RECORDED')
                    AND completed_at IS NULL
                )
                OR (
                    state IN (
                        'REJECTED', 'COMPLETED', 'QUARANTINED', 'CANCELLED'
                    )
                    AND completed_at IS NOT NULL
                )
            ),
            FOREIGN KEY(workflow_instance_id)
                REFERENCES workflow_instances(id) ON DELETE RESTRICT,
            FOREIGN KEY(workflow_transition_id)
                REFERENCES workflow_transitions(id) ON DELETE RESTRICT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT,
            FOREIGN KEY(physical_seat_id)
                REFERENCES physical_seats(id) ON DELETE RESTRICT,
            FOREIGN KEY(capability_id)
                REFERENCES logical_capabilities(id) ON DELETE RESTRICT,
            FOREIGN KEY(seat_capability_activation_id)
                REFERENCES seat_capability_activations(id) ON DELETE RESTRICT,
            FOREIGN KEY(worker_id)
                REFERENCES worker_identities(id) ON DELETE RESTRICT,
            FOREIGN KEY(worker_fingerprint_id)
                REFERENCES worker_fingerprints(id) ON DELETE RESTRICT,
            FOREIGN KEY(slot_id)
                REFERENCES runtime_slots(id) ON DELETE RESTRICT,
            FOREIGN KEY(worker_assignment_id)
                REFERENCES worker_slot_assignments(id) ON DELETE RESTRICT,
            FOREIGN KEY(repository_id)
                REFERENCES repository_registrations(id) ON DELETE RESTRICT,
            FOREIGN KEY(lease_id)
                REFERENCES runtime_leases(id) ON DELETE RESTRICT,
            FOREIGN KEY(sandbox_binding_id)
                REFERENCES sandbox_bindings(id) ON DELETE RESTRICT,
            FOREIGN KEY(oid_authority_id)
                REFERENCES oid_authorities(id) ON DELETE RESTRICT,
            FOREIGN KEY(contract_definition_id)
                REFERENCES registered_definitions(id) ON DELETE RESTRICT,
            FOREIGN KEY(output_schema_definition_id)
                REFERENCES registered_definitions(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_clause_bindings (
            contract_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            definition_id TEXT NOT NULL,
            clause_digest TEXT NOT NULL CHECK(
                length(clause_digest) = 64
                AND clause_digest NOT GLOB '*[^0-9a-f]*'
            ),
            character_count INTEGER NOT NULL CHECK(character_count >= 0),
            idempotency_key TEXT NOT NULL UNIQUE,
            bound_at TEXT NOT NULL,
            PRIMARY KEY(contract_id, ordinal),
            UNIQUE(contract_id, definition_id),
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(definition_id)
                REFERENCES registered_definitions(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_profile_bindings (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL UNIQUE,
            profile_definition_id TEXT NOT NULL,
            professional_skill_id TEXT NOT NULL
                CHECK(professional_skill_id = 'professional-profile-runtime'),
            compiled_profile_ref TEXT NOT NULL,
            compiled_profile_digest TEXT NOT NULL CHECK(
                length(compiled_profile_digest) = 64
                AND compiled_profile_digest NOT GLOB '*[^0-9a-f]*'
            ),
            state TEXT NOT NULL CHECK(state IN (
                'BOUND', 'REVOKED', 'QUARANTINED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            bound_at TEXT NOT NULL,
            revoked_at TEXT,
            CHECK(
                (state = 'BOUND' AND revoked_at IS NULL)
                OR (state <> 'BOUND' AND revoked_at IS NOT NULL)
            ),
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(profile_definition_id)
                REFERENCES registered_definitions(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_skill_bindings (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            skill_definition_id TEXT NOT NULL,
            capability_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            bound_digest TEXT NOT NULL CHECK(
                length(bound_digest) = 64
                AND bound_digest NOT GLOB '*[^0-9a-f]*'
            ),
            content_character_count INTEGER NOT NULL
                CHECK(content_character_count >= 0),
            state TEXT NOT NULL CHECK(state IN (
                'BOUND', 'REVOKED', 'QUARANTINED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            bound_at TEXT NOT NULL,
            revoked_at TEXT,
            CHECK(
                (state = 'BOUND' AND revoked_at IS NULL)
                OR (state <> 'BOUND' AND revoked_at IS NOT NULL)
            ),
            UNIQUE(contract_id, ordinal),
            UNIQUE(contract_id, skill_definition_id),
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(skill_definition_id)
                REFERENCES registered_definitions(id) ON DELETE RESTRICT,
            FOREIGN KEY(capability_id)
                REFERENCES logical_capabilities(id) ON DELETE RESTRICT
        )
        """,
        # MCP availability, requirements, health, and usage remain separate facts.
        """
        CREATE TABLE IF NOT EXISTS mcp_definitions (
            id TEXT PRIMARY KEY,
            server_name TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            version TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK(
                length(sha256) = 64
                AND sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'RETIRED')),
            idempotency_key TEXT NOT NULL UNIQUE,
            registered_at TEXT NOT NULL,
            UNIQUE(server_name, tool_name, version)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_mcp_bindings (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            mcp_definition_id TEXT NOT NULL,
            required_availability INTEGER NOT NULL
                CHECK(required_availability IN (0, 1)),
            invocation_required INTEGER NOT NULL
                CHECK(invocation_required IN (0, 1)),
            trigger_rule TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            bound_at TEXT NOT NULL,
            UNIQUE(contract_id, mcp_definition_id),
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(mcp_definition_id)
                REFERENCES mcp_definitions(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS mcp_health_observations (
            id TEXT PRIMARY KEY,
            mcp_definition_id TEXT NOT NULL,
            contract_id TEXT,
            status TEXT NOT NULL CHECK(status IN (
                'HEALTHY', 'UNHEALTHY', 'UNKNOWN'
            )),
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest) = 64
                AND evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            idempotency_key TEXT NOT NULL UNIQUE,
            observed_at TEXT NOT NULL,
            FOREIGN KEY(mcp_definition_id)
                REFERENCES mcp_definitions(id) ON DELETE RESTRICT,
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT
        )
        """,
        # Serena snapshots are repository evidence; contracts select a strict subset.
        """
        CREATE TABLE IF NOT EXISTS serena_onboarding_snapshots (
            id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL,
            source_oid TEXT NOT NULL CHECK(
                length(source_oid) IN (40, 64)
                AND source_oid = lower(source_oid)
                AND source_oid NOT GLOB '*[^0-9a-f]*'
            ),
            policy_digest TEXT NOT NULL CHECK(
                length(policy_digest) = 64
                AND policy_digest NOT GLOB '*[^0-9a-f]*'
            ),
            memory_manifest_digest TEXT NOT NULL CHECK(
                length(memory_manifest_digest) = 64
                AND memory_manifest_digest NOT GLOB '*[^0-9a-f]*'
            ),
            state TEXT NOT NULL CHECK(state IN (
                'ACCEPTED', 'STALE', 'QUARANTINED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            UNIQUE(
                repository_id,
                source_oid,
                policy_digest,
                memory_manifest_digest
            ),
            FOREIGN KEY(repository_id)
                REFERENCES repository_registrations(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS serena_snapshot_memory_bindings (
            snapshot_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            memory_name TEXT NOT NULL,
            memory_ref TEXT NOT NULL,
            memory_sha256 TEXT NOT NULL CHECK(
                length(memory_sha256) = 64
                AND memory_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            PRIMARY KEY(snapshot_id, ordinal),
            UNIQUE(snapshot_id, memory_name),
            UNIQUE(snapshot_id, memory_ref),
            FOREIGN KEY(snapshot_id)
                REFERENCES serena_onboarding_snapshots(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_serena_memory_bindings (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            memory_name TEXT NOT NULL,
            memory_ref TEXT NOT NULL,
            memory_sha256 TEXT NOT NULL CHECK(
                length(memory_sha256) = 64
                AND memory_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            idempotency_key TEXT NOT NULL UNIQUE,
            bound_at TEXT NOT NULL,
            UNIQUE(contract_id, ordinal),
            UNIQUE(contract_id, memory_name),
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(snapshot_id)
                REFERENCES serena_onboarding_snapshots(id) ON DELETE RESTRICT,
            FOREIGN KEY(snapshot_id, memory_name)
                REFERENCES serena_snapshot_memory_bindings(
                    snapshot_id, memory_name
                ) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS serena_consumption_receipts (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            memory_binding_id TEXT NOT NULL,
            worker_fingerprint_id TEXT NOT NULL,
            receipt_digest TEXT NOT NULL CHECK(
                length(receipt_digest) = 64
                AND receipt_digest NOT GLOB '*[^0-9a-f]*'
            ),
            idempotency_key TEXT NOT NULL UNIQUE,
            consumed_at TEXT NOT NULL,
            UNIQUE(contract_id, memory_binding_id),
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(memory_binding_id)
                REFERENCES contract_serena_memory_bindings(id)
                ON DELETE RESTRICT,
            FOREIGN KEY(worker_fingerprint_id)
                REFERENCES worker_fingerprints(id) ON DELETE RESTRICT
        )
        """,
        # Admission is immutable and may reject deterministically without an attempt.
        """
        CREATE TABLE IF NOT EXISTS contract_admissions (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL UNIQUE,
            decision TEXT NOT NULL CHECK(decision IN ('ACCEPTED', 'REJECTED')),
            reason_code TEXT,
            deterministic INTEGER NOT NULL CHECK(deterministic IN (0, 1)),
            contract_digest TEXT NOT NULL CHECK(
                length(contract_digest) = 64
                AND contract_digest NOT GLOB '*[^0-9a-f]*'
            ),
            idempotency_key TEXT NOT NULL UNIQUE,
            admitted_at TEXT NOT NULL,
            CHECK(
                (decision = 'ACCEPTED' AND reason_code IS NULL)
                OR (decision = 'REJECTED' AND reason_code IS NOT NULL)
            ),
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS worker_circuit_breakers (
            id TEXT PRIMARY KEY,
            worker_fingerprint_id TEXT NOT NULL,
            goal_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('OPEN', 'CLOSED')),
            reason_code TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            CHECK(
                (state = 'OPEN' AND closed_at IS NULL)
                OR (state = 'CLOSED' AND closed_at IS NOT NULL)
            ),
            FOREIGN KEY(worker_fingerprint_id)
                REFERENCES worker_fingerprints(id) ON DELETE RESTRICT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_attempts (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            admission_id TEXT NOT NULL,
            worker_fingerprint_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK(attempt_number IN (1, 2)),
            attempt_kind TEXT NOT NULL CHECK(attempt_kind IN (
                'PRIMARY', 'FORMAT_REPAIR'
            )),
            output_only INTEGER NOT NULL CHECK(output_only IN (0, 1)),
            backend TEXT NOT NULL,
            model TEXT NOT NULL,
            input_digest TEXT NOT NULL CHECK(
                length(input_digest) = 64
                AND input_digest NOT GLOB '*[^0-9a-f]*'
            ),
            state TEXT NOT NULL CHECK(state IN (
                'CREATED', 'RUNNING', 'SUCCEEDED', 'FAILED',
                'QUARANTINED', 'CANCELLED'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            CHECK(
                (
                    attempt_kind = 'PRIMARY'
                    AND attempt_number = 1
                    AND output_only = 0
                )
                OR (
                    attempt_kind = 'FORMAT_REPAIR'
                    AND attempt_number = 2
                    AND output_only = 1
                )
            ),
            CHECK(
                (state IN ('CREATED', 'RUNNING') AND completed_at IS NULL)
                OR (
                    state IN (
                        'SUCCEEDED', 'FAILED', 'QUARANTINED', 'CANCELLED'
                    )
                    AND completed_at IS NOT NULL
                )
            ),
            UNIQUE(contract_id, attempt_number),
            UNIQUE(contract_id, attempt_kind),
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(admission_id)
                REFERENCES contract_admissions(id) ON DELETE RESTRICT,
            FOREIGN KEY(worker_fingerprint_id)
                REFERENCES worker_fingerprints(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS activation_results (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL UNIQUE,
            disposition TEXT NOT NULL CHECK(disposition IN (
                'ACCEPTED', 'REJECTED', 'FORMAT_INVALID', 'QUARANTINED'
            )),
            result_kind TEXT NOT NULL,
            output_digest TEXT NOT NULL CHECK(
                length(output_digest) = 64
                AND output_digest NOT GLOB '*[^0-9a-f]*'
            ),
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest) = 64
                AND evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            payload_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(attempt_id)
                REFERENCES contract_attempts(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_violations (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            attempt_id TEXT,
            goal_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            worker_fingerprint_id TEXT NOT NULL,
            violation_code TEXT NOT NULL CHECK(violation_code IN (
                'ADMISSION', 'FORMAT', 'AUTHORITY', 'OID', 'WRITE_ROOT',
                'NESTED_SPAWN', 'MCP_HEALTH', 'MCP_USAGE',
                'SERENA_ONBOARDING', 'SERENA_CONSUMPTION', 'OTHER'
            )),
            disposition TEXT NOT NULL CHECK(disposition IN (
                'REJECTED', 'FORMAT_REPAIR', 'QUARANTINED', 'CIRCUIT_OPEN'
            )),
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest) = 64
                AND evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            details_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            CHECK(
                (
                    violation_code = 'FORMAT'
                    AND disposition IN ('FORMAT_REPAIR', 'CIRCUIT_OPEN')
                )
                OR (
                    violation_code IN (
                        'AUTHORITY', 'OID', 'WRITE_ROOT', 'NESTED_SPAWN'
                    )
                    AND disposition = 'QUARANTINED'
                )
                OR (
                    violation_code NOT IN (
                        'FORMAT', 'AUTHORITY', 'OID',
                        'WRITE_ROOT', 'NESTED_SPAWN'
                    )
                    AND disposition = 'REJECTED'
                )
            ),
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(attempt_id)
                REFERENCES contract_attempts(id) ON DELETE RESTRICT,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE RESTRICT,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE RESTRICT,
            FOREIGN KEY(worker_fingerprint_id)
                REFERENCES worker_fingerprints(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS token_ledger_entries (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            admission_id TEXT NOT NULL,
            attempt_id TEXT,
            worker_fingerprint_id TEXT NOT NULL,
            entry_kind TEXT NOT NULL CHECK(entry_kind IN (
                'BUDGET', 'RESERVED', 'CONSUMED',
                'RELEASED', 'ADMISSION_REJECTED'
            )),
            input_tokens INTEGER NOT NULL CHECK(input_tokens >= 0),
            output_tokens INTEGER NOT NULL CHECK(output_tokens >= 0),
            model_calls INTEGER NOT NULL CHECK(model_calls IN (0, 1)),
            idempotency_key TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            CHECK(
                entry_kind <> 'ADMISSION_REJECTED'
                OR (
                    attempt_id IS NULL
                    AND input_tokens = 0
                    AND output_tokens = 0
                    AND model_calls = 0
                )
            ),
            CHECK(model_calls = 0 OR attempt_id IS NOT NULL),
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(admission_id)
                REFERENCES contract_admissions(id) ON DELETE RESTRICT,
            FOREIGN KEY(attempt_id)
                REFERENCES contract_attempts(id) ON DELETE RESTRICT,
            FOREIGN KEY(worker_fingerprint_id)
                REFERENCES worker_fingerprints(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS mcp_usage_receipts (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            mcp_binding_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            input_digest TEXT NOT NULL CHECK(
                length(input_digest) = 64
                AND input_digest NOT GLOB '*[^0-9a-f]*'
            ),
            output_digest TEXT NOT NULL CHECK(
                length(output_digest) = 64
                AND output_digest NOT GLOB '*[^0-9a-f]*'
            ),
            idempotency_key TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            UNIQUE(attempt_id, mcp_binding_id, tool_name),
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(attempt_id)
                REFERENCES contract_attempts(id) ON DELETE RESTRICT,
            FOREIGN KEY(mcp_binding_id)
                REFERENCES contract_mcp_bindings(id) ON DELETE RESTRICT
        )
        """,
        # Migration and deletion records preserve ambiguous v3 facts without guessing.
        """
        CREATE TABLE IF NOT EXISTS migration_evidence (
            id TEXT PRIMARY KEY,
            legacy_migration_id TEXT,
            legacy_table TEXT NOT NULL,
            legacy_record_id TEXT NOT NULL,
            observed_state TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN (
                'PRESERVED', 'QUARANTINED', 'REISSUED', 'ROLLED_BACK'
            )),
            reissued_contract_id TEXT,
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest) = 64
                AND evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            idempotency_key TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            UNIQUE(legacy_table, legacy_record_id, action),
            FOREIGN KEY(legacy_migration_id)
                REFERENCES migration_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY(reissued_contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS deletion_manifests (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            manifest_digest TEXT NOT NULL UNIQUE CHECK(
                length(manifest_digest) = 64
                AND manifest_digest NOT GLOB '*[^0-9a-f]*'
            ),
            state TEXT NOT NULL CHECK(state IN (
                'DRY_RUN', 'APPROVED', 'APPLIED', 'VERIFIED', 'ROLLED_BACK'
            )),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY(target_id)
                REFERENCES targets(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS deletion_manifest_entries (
            manifest_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            relative_path TEXT NOT NULL,
            ownership_digest TEXT NOT NULL CHECK(
                length(ownership_digest) = 64
                AND ownership_digest NOT GLOB '*[^0-9a-f]*'
            ),
            reference_evidence_digest TEXT NOT NULL CHECK(
                length(reference_evidence_digest) = 64
                AND reference_evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            replacement_path TEXT,
            disposition TEXT NOT NULL CHECK(disposition IN (
                'RETAIN', 'DELETE'
            )),
            PRIMARY KEY(manifest_id, ordinal),
            UNIQUE(manifest_id, relative_path),
            FOREIGN KEY(manifest_id)
                REFERENCES deletion_manifests(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS control_plane_evidence (
            id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            contract_id TEXT,
            attempt_id TEXT,
            disposition TEXT NOT NULL CHECK(disposition IN (
                'ACCEPTED', 'REJECTED', 'QUARANTINED'
            )),
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest) = 64
                AND evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            payload_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY(contract_id)
                REFERENCES activation_contracts(id) ON DELETE RESTRICT,
            FOREIGN KEY(attempt_id)
                REFERENCES contract_attempts(id) ON DELETE RESTRICT
        )
        """,
        # Partial unique indexes are the concurrency boundary, not advisory lookups.
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_worker_fingerprints_active_worker
            ON worker_fingerprints(worker_id)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_worker_slot_assignments_active_slot
            ON worker_slot_assignments(slot_id)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_worker_slot_assignments_active_worker
            ON worker_slot_assignments(worker_id)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_worker_slot_assignments_active_elastic
            ON worker_slot_assignments(is_elastic)
            WHERE state = 'ACTIVE' AND is_elastic = 1
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_seat_capability_activations_active_seat
            ON seat_capability_activations(physical_seat_id)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_seat_capability_activations_active_slot
            ON seat_capability_activations(slot_id)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_states_initial
            ON workflow_states(workflow_definition_id)
            WHERE is_initial = 1
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_leases_active_slot_run
            ON runtime_leases(slot_id, run_id)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_leases_active_slot
            ON runtime_leases(slot_id)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_leases_active_worktree
            ON runtime_leases(worktree_path)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_sandbox_bindings_active_cwd
            ON sandbox_bindings(cwd)
            WHERE state = 'ACTIVE'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_worker_circuit_breakers_open_scope
            ON worker_circuit_breakers(worker_fingerprint_id, goal_id, run_id)
            WHERE state = 'OPEN'
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_activation_contracts_run_state
            ON activation_contracts(goal_id, run_id, state, issued_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_contract_attempts_contract_state
            ON contract_attempts(contract_id, state, attempt_number)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_contract_violations_worker_scope
            ON contract_violations(
                worker_fingerprint_id, goal_id, run_id, violation_code, occurred_at
            )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_token_ledger_contract
            ON token_ledger_entries(contract_id, occurred_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_mcp_health_definition
            ON mcp_health_observations(
                mcp_definition_id, status, observed_at DESC
            )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_serena_snapshots_repository
            ON serena_onboarding_snapshots(
                repository_id, source_oid, state, created_at DESC
            )
        """,
        # Definition and already-issued workflow evidence are immutable.
        """
        CREATE TRIGGER IF NOT EXISTS trg_registered_definitions_no_update
        BEFORE UPDATE ON registered_definitions
        BEGIN
            SELECT RAISE(ABORT, 'registered definitions are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_registered_definitions_no_delete
        BEFORE DELETE ON registered_definitions
        BEGIN
            SELECT RAISE(ABORT, 'registered definitions are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_workflow_definitions_definition_kind
        BEFORE INSERT ON workflow_definitions
        WHEN NOT EXISTS (
            SELECT 1
            FROM registered_definitions
            WHERE registered_definitions.id = NEW.definition_id
              AND registered_definitions.kind = 'WORKFLOW'
              AND registered_definitions.version = NEW.version
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'workflow definition must reference matching WORKFLOW bytes'
            );
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_workflow_transitions_schema_kind
        BEFORE INSERT ON workflow_transitions
        WHEN NOT EXISTS (
            SELECT 1
            FROM registered_definitions
            WHERE registered_definitions.id = NEW.output_schema_definition_id
              AND registered_definitions.kind = 'SCHEMA'
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'workflow transition output must reference SCHEMA bytes'
            );
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_workflow_instances_run_ownership
        BEFORE INSERT ON workflow_instances
        WHEN NOT EXISTS (
            SELECT 1
            FROM runs
            WHERE runs.id = NEW.run_id
              AND runs.goal_id = NEW.goal_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'workflow instance run must belong to goal');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_workflow_instances_legal_transition
        BEFORE UPDATE OF current_state_key ON workflow_instances
        WHEN NEW.current_state_key <> OLD.current_state_key
          AND NOT EXISTS (
              SELECT 1
              FROM workflow_transitions
              WHERE workflow_transitions.workflow_definition_id =
                        OLD.workflow_definition_id
                AND workflow_transitions.from_state_key = OLD.current_state_key
                AND (
                    workflow_transitions.to_state_key = NEW.current_state_key
                    OR workflow_transitions.failure_route = NEW.current_state_key
                )
                AND workflow_transitions.state = 'ACTIVE'
          )
        BEGIN
            SELECT RAISE(ABORT, 'illegal workflow state transition');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_workflow_instances_status_transition
        BEFORE UPDATE OF status ON workflow_instances
        WHEN NEW.status <> OLD.status
          AND NOT (
              (OLD.status = 'CREATED' AND NEW.status IN (
                  'ACTIVE', 'CANCELLED', 'QUARANTINED'
              ))
              OR (OLD.status = 'ACTIVE' AND NEW.status IN (
                  'COMPLETED', 'CANCELLED', 'QUARANTINED'
              ))
          )
        BEGIN
            SELECT RAISE(ABORT, 'illegal workflow instance status transition');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_workflow_transition_receipts_integrity
        BEFORE INSERT ON workflow_transition_receipts
        WHEN NOT EXISTS (
            SELECT 1
            FROM workflow_instances
            JOIN workflow_transitions
              ON workflow_transitions.id = NEW.workflow_transition_id
            WHERE workflow_instances.id = NEW.workflow_instance_id
              AND workflow_instances.workflow_definition_id =
                    workflow_transitions.workflow_definition_id
              AND workflow_transitions.from_state_key = NEW.from_state_key
              AND (
                  workflow_transitions.to_state_key = NEW.to_state_key
                  OR workflow_transitions.failure_route = NEW.to_state_key
              )
              AND EXISTS (
                  SELECT 1
                  FROM workflow_states
                  WHERE workflow_states.workflow_definition_id =
                        workflow_instances.workflow_definition_id
                    AND workflow_states.state_key = NEW.to_state_key
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'transition receipt does not match workflow');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_workflow_transition_receipts_no_update
        BEFORE UPDATE ON workflow_transition_receipts
        BEGIN
            SELECT RAISE(ABORT, 'transition receipts are append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_workflow_transition_receipts_no_delete
        BEFORE DELETE ON workflow_transition_receipts
        BEGIN
            SELECT RAISE(ABORT, 'transition receipts are append-only');
        END
        """,
        # Worker/slot and merged-seat activation graphs are checked at insertion.
        """
        CREATE TRIGGER IF NOT EXISTS trg_worker_slot_assignments_integrity
        BEFORE INSERT ON worker_slot_assignments
        WHEN NOT EXISTS (
            SELECT 1
            FROM worker_identities
            JOIN worker_fingerprints
              ON worker_fingerprints.id = NEW.worker_fingerprint_id
            JOIN runtime_slots
              ON runtime_slots.id = NEW.slot_id
            WHERE worker_identities.id = NEW.worker_id
              AND worker_fingerprints.worker_id = NEW.worker_id
              AND worker_fingerprints.state = 'ACTIVE'
              AND runtime_slots.state <> 'RETIRED'
              AND (
                  (
                      NEW.is_elastic = 1
                      AND worker_identities.kind = 'ELASTIC'
                      AND runtime_slots.kind = 'ELASTIC'
                  )
                  OR (
                      NEW.is_elastic = 0
                      AND worker_identities.kind = 'FIXED'
                      AND runtime_slots.kind = 'FIXED'
                      AND worker_identities.physical_seat_id =
                            runtime_slots.physical_seat_id
                  )
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'worker, fingerprint, and slot are inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_worker_slot_assignments_transition
        BEFORE UPDATE OF state ON worker_slot_assignments
        WHEN NEW.state <> OLD.state
          AND NOT (
              OLD.state = 'ACTIVE'
              AND NEW.state IN ('RELEASED', 'QUARANTINED')
          )
        BEGIN
            SELECT RAISE(ABORT, 'illegal worker-slot assignment transition');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_worker_slot_assignments_identity_immutable
        BEFORE UPDATE ON worker_slot_assignments
        WHEN NEW.worker_id <> OLD.worker_id
          OR NEW.worker_fingerprint_id <> OLD.worker_fingerprint_id
          OR NEW.slot_id <> OLD.slot_id
          OR NEW.run_id <> OLD.run_id
          OR NEW.is_elastic <> OLD.is_elastic
          OR NEW.idempotency_key <> OLD.idempotency_key
          OR NEW.assigned_at <> OLD.assigned_at
        BEGIN
            SELECT RAISE(ABORT, 'worker-slot assignment identity is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_seat_capability_activations_integrity
        BEFORE INSERT ON seat_capability_activations
        WHEN NOT EXISTS (
            SELECT 1
            FROM seat_capability_ownerships
            JOIN runtime_slots
              ON runtime_slots.id = NEW.slot_id
            JOIN worker_slot_assignments
              ON worker_slot_assignments.id = NEW.worker_assignment_id
            JOIN runs
              ON runs.id = NEW.run_id
            WHERE seat_capability_ownerships.physical_seat_id =
                    NEW.physical_seat_id
              AND seat_capability_ownerships.capability_id = NEW.capability_id
              AND seat_capability_ownerships.state = 'ENABLED'
              AND runtime_slots.kind = 'FIXED'
              AND runtime_slots.physical_seat_id = NEW.physical_seat_id
              AND worker_slot_assignments.slot_id = NEW.slot_id
              AND worker_slot_assignments.run_id = NEW.run_id
              AND worker_slot_assignments.state = 'ACTIVE'
              AND runs.goal_id = NEW.goal_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'seat capability activation graph is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_seat_capability_activations_transition
        BEFORE UPDATE OF state ON seat_capability_activations
        WHEN NEW.state <> OLD.state
          AND NOT (
              OLD.state = 'ACTIVE'
              AND NEW.state IN ('RELEASED', 'QUARANTINED')
          )
        BEGIN
            SELECT RAISE(ABORT, 'illegal seat capability transition');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_seat_capability_activation_identity
        BEFORE UPDATE ON seat_capability_activations
        WHEN NEW.physical_seat_id <> OLD.physical_seat_id
          OR NEW.capability_id <> OLD.capability_id
          OR NEW.slot_id <> OLD.slot_id
          OR NEW.worker_assignment_id <> OLD.worker_assignment_id
          OR NEW.goal_id <> OLD.goal_id
          OR NEW.run_id <> OLD.run_id
          OR NEW.idempotency_key <> OLD.idempotency_key
          OR NEW.activated_at <> OLD.activated_at
        BEGIN
            SELECT RAISE(ABORT, 'seat capability activation identity is immutable');
        END
        """,
        # Lease, sandbox, and OID records must describe the same run and slot.
        """
        CREATE TRIGGER IF NOT EXISTS trg_runtime_leases_integrity
        BEFORE INSERT ON runtime_leases
        WHEN NOT EXISTS (
            SELECT 1
            FROM repository_registrations
            JOIN goals
              ON goals.id = NEW.goal_id
            JOIN runs
              ON runs.id = NEW.run_id
            JOIN worker_slot_assignments
              ON worker_slot_assignments.id = NEW.worker_assignment_id
            WHERE repository_registrations.id = NEW.repository_id
              AND goals.target_id = repository_registrations.target_id
              AND runs.goal_id = NEW.goal_id
              AND worker_slot_assignments.slot_id = NEW.slot_id
              AND worker_slot_assignments.run_id = NEW.run_id
              AND worker_slot_assignments.state = 'ACTIVE'
        )
        BEGIN
            SELECT RAISE(ABORT, 'runtime lease ownership graph is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_runtime_leases_transition
        BEFORE UPDATE OF state ON runtime_leases
        WHEN NEW.state <> OLD.state
          AND NOT (
              OLD.state = 'ACTIVE'
              AND NEW.state IN ('RELEASED', 'EXPIRED', 'QUARANTINED')
          )
        BEGIN
            SELECT RAISE(ABORT, 'illegal runtime lease transition');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_runtime_lease_identity_immutable
        BEFORE UPDATE ON runtime_leases
        WHEN NEW.repository_id <> OLD.repository_id
          OR NEW.goal_id <> OLD.goal_id
          OR NEW.run_id <> OLD.run_id
          OR NEW.slot_id <> OLD.slot_id
          OR NEW.worker_assignment_id <> OLD.worker_assignment_id
          OR NEW.lease_kind <> OLD.lease_kind
          OR NEW.branch_ref IS NOT OLD.branch_ref
          OR NEW.worktree_path <> OLD.worktree_path
          OR NEW.base_oid <> OLD.base_oid
          OR NEW.expected_head_oid <> OLD.expected_head_oid
          OR NEW.write_roots_json <> OLD.write_roots_json
          OR NEW.protected_roots_json <> OLD.protected_roots_json
          OR NEW.idempotency_key <> OLD.idempotency_key
        BEGIN
            SELECT RAISE(ABORT, 'runtime lease authority is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_sandbox_bindings_integrity
        BEFORE INSERT ON sandbox_bindings
        WHEN NOT EXISTS (
            SELECT 1
            FROM runtime_leases
            WHERE runtime_leases.id = NEW.lease_id
              AND runtime_leases.repository_id = NEW.repository_id
              AND runtime_leases.run_id = NEW.run_id
              AND runtime_leases.slot_id = NEW.slot_id
              AND runtime_leases.state = 'ACTIVE'
              AND (
                  runtime_leases.lease_kind <> 'REVIEW'
                  OR NEW.source_read_only = 1
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'sandbox binding does not match active lease');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_sandbox_binding_identity_immutable
        BEFORE UPDATE ON sandbox_bindings
        WHEN NEW.lease_id <> OLD.lease_id
          OR NEW.repository_id <> OLD.repository_id
          OR NEW.run_id <> OLD.run_id
          OR NEW.slot_id <> OLD.slot_id
          OR NEW.subject_oid <> OLD.subject_oid
          OR NEW.cwd <> OLD.cwd
          OR NEW.source_root <> OLD.source_root
          OR NEW.source_read_only <> OLD.source_read_only
          OR NEW.writable_roots_json <> OLD.writable_roots_json
          OR NEW.backend <> OLD.backend
          OR NEW.attestation_digest <> OLD.attestation_digest
          OR NEW.idempotency_key <> OLD.idempotency_key
        BEGIN
            SELECT RAISE(ABORT, 'sandbox confinement binding is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_oid_authorities_integrity
        BEFORE INSERT ON oid_authorities
        WHEN NOT EXISTS (
            SELECT 1
            FROM runtime_leases
            JOIN sandbox_bindings
              ON sandbox_bindings.id = NEW.sandbox_binding_id
            WHERE runtime_leases.id = NEW.lease_id
              AND runtime_leases.repository_id = NEW.repository_id
              AND runtime_leases.goal_id = NEW.goal_id
              AND runtime_leases.run_id = NEW.run_id
              AND sandbox_bindings.lease_id = NEW.lease_id
              AND sandbox_bindings.repository_id = NEW.repository_id
              AND sandbox_bindings.run_id = NEW.run_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'OID authority does not match lease and sandbox');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_oid_authority_identity_immutable
        BEFORE UPDATE ON oid_authorities
        WHEN NEW.repository_id <> OLD.repository_id
          OR NEW.goal_id <> OLD.goal_id
          OR NEW.run_id <> OLD.run_id
          OR NEW.lease_id <> OLD.lease_id
          OR NEW.sandbox_binding_id <> OLD.sandbox_binding_id
          OR NEW.authority_kind <> OLD.authority_kind
          OR NEW.oid <> OLD.oid
          OR NEW.evidence_digest <> OLD.evidence_digest
          OR NEW.idempotency_key <> OLD.idempotency_key
        BEGIN
            SELECT RAISE(ABORT, 'OID authority evidence is immutable');
        END
        """,
        # Contract insertion revalidates every normalized binding.
        """
        CREATE TRIGGER IF NOT EXISTS trg_activation_contracts_binding_integrity
        BEFORE INSERT ON activation_contracts
        WHEN NOT EXISTS (
            SELECT 1
            FROM workflow_instances
            JOIN workflow_transitions
              ON workflow_transitions.id = NEW.workflow_transition_id
            JOIN worker_identities
              ON worker_identities.id = NEW.worker_id
            JOIN worker_fingerprints
              ON worker_fingerprints.id = NEW.worker_fingerprint_id
            JOIN worker_slot_assignments
              ON worker_slot_assignments.id = NEW.worker_assignment_id
            JOIN runtime_slots
              ON runtime_slots.id = NEW.slot_id
            JOIN runtime_leases
              ON runtime_leases.id = NEW.lease_id
            JOIN sandbox_bindings
              ON sandbox_bindings.id = NEW.sandbox_binding_id
            JOIN oid_authorities
              ON oid_authorities.id = NEW.oid_authority_id
            WHERE workflow_instances.id = NEW.workflow_instance_id
              AND workflow_instances.goal_id = NEW.goal_id
              AND workflow_instances.run_id = NEW.run_id
              AND workflow_instances.workflow_definition_id =
                    workflow_transitions.workflow_definition_id
              AND workflow_transitions.capability_id = NEW.capability_id
              AND workflow_transitions.output_schema_definition_id =
                    NEW.output_schema_definition_id
              AND workflow_transitions.state = 'ACTIVE'
              AND worker_fingerprints.worker_id = NEW.worker_id
              AND worker_fingerprints.state = 'ACTIVE'
              AND worker_slot_assignments.worker_id = NEW.worker_id
              AND worker_slot_assignments.worker_fingerprint_id =
                    NEW.worker_fingerprint_id
              AND worker_slot_assignments.slot_id = NEW.slot_id
              AND worker_slot_assignments.run_id = NEW.run_id
              AND worker_slot_assignments.state = 'ACTIVE'
              AND runtime_leases.repository_id = NEW.repository_id
              AND runtime_leases.goal_id = NEW.goal_id
              AND runtime_leases.run_id = NEW.run_id
              AND runtime_leases.slot_id = NEW.slot_id
              AND runtime_leases.worker_assignment_id =
                    NEW.worker_assignment_id
              AND runtime_leases.base_oid = NEW.base_oid
              AND runtime_leases.state = 'ACTIVE'
              AND sandbox_bindings.lease_id = NEW.lease_id
              AND sandbox_bindings.repository_id = NEW.repository_id
              AND sandbox_bindings.run_id = NEW.run_id
              AND sandbox_bindings.slot_id = NEW.slot_id
              AND sandbox_bindings.subject_oid = NEW.subject_oid
              AND sandbox_bindings.state = 'ACTIVE'
              AND oid_authorities.repository_id = NEW.repository_id
              AND oid_authorities.goal_id = NEW.goal_id
              AND oid_authorities.run_id = NEW.run_id
              AND oid_authorities.lease_id = NEW.lease_id
              AND oid_authorities.sandbox_binding_id =
                    NEW.sandbox_binding_id
              AND oid_authorities.oid = NEW.subject_oid
              AND oid_authorities.state = 'ACTIVE'
              AND (
                  (
                      NEW.physical_seat_id IS NULL
                      AND NEW.seat_capability_activation_id IS NULL
                      AND runtime_slots.kind = 'ELASTIC'
                      AND worker_identities.kind = 'ELASTIC'
                  )
                  OR (
                      NEW.physical_seat_id IS NOT NULL
                      AND NEW.seat_capability_activation_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1
                          FROM seat_capability_activations
                          WHERE seat_capability_activations.id =
                                NEW.seat_capability_activation_id
                            AND seat_capability_activations.physical_seat_id =
                                NEW.physical_seat_id
                            AND seat_capability_activations.capability_id =
                                NEW.capability_id
                            AND seat_capability_activations.slot_id = NEW.slot_id
                            AND seat_capability_activations.worker_assignment_id =
                                NEW.worker_assignment_id
                            AND seat_capability_activations.goal_id = NEW.goal_id
                            AND seat_capability_activations.run_id = NEW.run_id
                            AND seat_capability_activations.state = 'ACTIVE'
                      )
                  )
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'activation contract binding graph is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_activation_contract_identity_immutable
        BEFORE UPDATE ON activation_contracts
        WHEN NEW.workflow_instance_id <> OLD.workflow_instance_id
          OR NEW.workflow_transition_id <> OLD.workflow_transition_id
          OR NEW.goal_id <> OLD.goal_id
          OR NEW.run_id <> OLD.run_id
          OR NEW.physical_seat_id IS NOT OLD.physical_seat_id
          OR NEW.capability_id <> OLD.capability_id
          OR NEW.seat_capability_activation_id
                IS NOT OLD.seat_capability_activation_id
          OR NEW.worker_id <> OLD.worker_id
          OR NEW.worker_fingerprint_id <> OLD.worker_fingerprint_id
          OR NEW.slot_id <> OLD.slot_id
          OR NEW.worker_assignment_id <> OLD.worker_assignment_id
          OR NEW.repository_id <> OLD.repository_id
          OR NEW.lease_id <> OLD.lease_id
          OR NEW.sandbox_binding_id <> OLD.sandbox_binding_id
          OR NEW.oid_authority_id <> OLD.oid_authority_id
          OR NEW.base_oid <> OLD.base_oid
          OR NEW.subject_oid <> OLD.subject_oid
          OR NEW.contract_definition_id <> OLD.contract_definition_id
          OR NEW.output_schema_definition_id <> OLD.output_schema_definition_id
          OR NEW.contract_digest <> OLD.contract_digest
          OR NEW.packet_digest <> OLD.packet_digest
          OR NEW.context_char_budget <> OLD.context_char_budget
          OR NEW.max_attempts <> OLD.max_attempts
          OR NEW.idempotency_key <> OLD.idempotency_key
          OR NEW.issued_at <> OLD.issued_at
        BEGIN
            SELECT RAISE(ABORT, 'activation contract inputs are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_activation_contracts_legal_transition
        BEFORE UPDATE OF state ON activation_contracts
        WHEN NEW.state <> OLD.state
          AND NOT (
              (OLD.state = 'ISSUED' AND NEW.state IN (
                  'ADMITTED', 'REJECTED', 'QUARANTINED', 'CANCELLED'
              ))
              OR (OLD.state = 'ADMITTED' AND NEW.state IN (
                  'RUNNING', 'QUARANTINED', 'CANCELLED'
              ))
              OR (OLD.state = 'RUNNING' AND NEW.state IN (
                  'RESULT_RECORDED', 'QUARANTINED', 'CANCELLED'
              ))
              OR (OLD.state = 'RESULT_RECORDED' AND NEW.state IN (
                  'COMPLETED', 'QUARANTINED'
              ))
          )
        BEGIN
            SELECT RAISE(ABORT, 'illegal activation contract transition');
        END
        """,
        # Expertise bindings are digest pins only and cannot grant authority.
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_clause_definition
        BEFORE INSERT ON contract_clause_bindings
        WHEN NOT EXISTS (
            SELECT 1
            FROM registered_definitions
            WHERE registered_definitions.id = NEW.definition_id
              AND registered_definitions.kind = 'CLAUSE'
              AND registered_definitions.sha256 = NEW.clause_digest
        )
        BEGIN
            SELECT RAISE(ABORT, 'clause binding must match registered CLAUSE bytes');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_clause_bindings_no_update
        BEFORE UPDATE ON contract_clause_bindings
        BEGIN
            SELECT RAISE(ABORT, 'contract clause bindings are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_clause_bindings_no_delete
        BEFORE DELETE ON contract_clause_bindings
        BEGIN
            SELECT RAISE(ABORT, 'contract clause bindings are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_profile_definition
        BEFORE INSERT ON contract_profile_bindings
        WHEN NOT EXISTS (
            SELECT 1
            FROM registered_definitions
            WHERE registered_definitions.id = NEW.profile_definition_id
              AND registered_definitions.kind = 'PROFILE'
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile binding must reference PROFILE bytes');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_profile_identity_immutable
        BEFORE UPDATE ON contract_profile_bindings
        WHEN NEW.contract_id <> OLD.contract_id
          OR NEW.profile_definition_id <> OLD.profile_definition_id
          OR NEW.professional_skill_id <> OLD.professional_skill_id
          OR NEW.compiled_profile_ref <> OLD.compiled_profile_ref
          OR NEW.compiled_profile_digest <> OLD.compiled_profile_digest
          OR NEW.idempotency_key <> OLD.idempotency_key
          OR NEW.bound_at <> OLD.bound_at
        BEGIN
            SELECT RAISE(ABORT, 'contract profile binding is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_skill_definition
        BEFORE INSERT ON contract_skill_bindings
        WHEN NOT EXISTS (
            SELECT 1
            FROM activation_contracts
            JOIN registered_definitions
              ON registered_definitions.id = NEW.skill_definition_id
            WHERE activation_contracts.id = NEW.contract_id
              AND activation_contracts.capability_id = NEW.capability_id
              AND registered_definitions.kind = 'SKILL'
              AND registered_definitions.sha256 = NEW.bound_digest
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'Skill binding must match contract capability and registered bytes'
            );
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_skill_identity_immutable
        BEFORE UPDATE ON contract_skill_bindings
        WHEN NEW.contract_id <> OLD.contract_id
          OR NEW.skill_definition_id <> OLD.skill_definition_id
          OR NEW.capability_id <> OLD.capability_id
          OR NEW.ordinal <> OLD.ordinal
          OR NEW.bound_digest <> OLD.bound_digest
          OR NEW.content_character_count <> OLD.content_character_count
          OR NEW.idempotency_key <> OLD.idempotency_key
          OR NEW.bound_at <> OLD.bound_at
        BEGIN
            SELECT RAISE(ABORT, 'contract Skill binding is immutable');
        END
        """,
        # Accepted admission validates all required pre-model dependencies.
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_admissions_require_bindings
        BEFORE INSERT ON contract_admissions
        WHEN NEW.decision = 'ACCEPTED'
          AND (
              NOT EXISTS (
                  SELECT 1
                  FROM activation_contracts
                  WHERE activation_contracts.id = NEW.contract_id
                    AND activation_contracts.state = 'ISSUED'
                    AND activation_contracts.contract_digest =
                          NEW.contract_digest
              )
              OR NOT EXISTS (
                  SELECT 1
                  FROM contract_profile_bindings
                  WHERE contract_profile_bindings.contract_id = NEW.contract_id
                    AND contract_profile_bindings.state = 'BOUND'
              )
              OR EXISTS (
                  SELECT 1
                  FROM contract_mcp_bindings
                  WHERE contract_mcp_bindings.contract_id = NEW.contract_id
                    AND contract_mcp_bindings.required_availability = 1
                    AND NOT EXISTS (
                        SELECT 1
                        FROM mcp_health_observations
                        WHERE mcp_health_observations.mcp_definition_id =
                              contract_mcp_bindings.mcp_definition_id
                          AND mcp_health_observations.status = 'HEALTHY'
                          AND (
                              mcp_health_observations.contract_id IS NULL
                              OR mcp_health_observations.contract_id =
                                 NEW.contract_id
                          )
                    )
              )
              OR EXISTS (
                  SELECT 1
                  FROM activation_contracts
                  JOIN workflow_transitions
                    ON workflow_transitions.id =
                       activation_contracts.workflow_transition_id
                  WHERE activation_contracts.id = NEW.contract_id
                    AND workflow_transitions.requires_serena_onboarding = 1
                    AND NOT EXISTS (
                        SELECT 1
                        FROM contract_serena_memory_bindings
                        WHERE contract_serena_memory_bindings.contract_id =
                              NEW.contract_id
                    )
              )
              OR EXISTS (
                  SELECT 1
                  FROM activation_contracts
                  WHERE activation_contracts.id = NEW.contract_id
                    AND (
                        SELECT COALESCE(SUM(character_count), 0)
                        FROM contract_clause_bindings
                        WHERE contract_clause_bindings.contract_id =
                              NEW.contract_id
                    ) > activation_contracts.context_char_budget
              )
              OR EXISTS (
                  SELECT 1
                  FROM activation_contracts
                  JOIN worker_circuit_breakers
                    ON worker_circuit_breakers.worker_fingerprint_id =
                       activation_contracts.worker_fingerprint_id
                   AND worker_circuit_breakers.goal_id =
                       activation_contracts.goal_id
                   AND worker_circuit_breakers.run_id =
                       activation_contracts.run_id
                  WHERE activation_contracts.id = NEW.contract_id
                    AND worker_circuit_breakers.state = 'OPEN'
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'accepted contract admission prerequisites failed');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_admissions_rejection_deterministic
        BEFORE INSERT ON contract_admissions
        WHEN NEW.decision = 'REJECTED'
          AND NEW.deterministic <> 1
        BEGIN
            SELECT RAISE(ABORT, 'admission rejection must be deterministic');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_admissions_no_update
        BEFORE UPDATE ON contract_admissions
        BEGIN
            SELECT RAISE(ABORT, 'contract admissions are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_admissions_no_delete
        BEFORE DELETE ON contract_admissions
        BEGIN
            SELECT RAISE(ABORT, 'contract admissions are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_admissions_apply_state
        AFTER INSERT ON contract_admissions
        BEGIN
            UPDATE activation_contracts
            SET state = CASE
                    WHEN NEW.decision = 'ACCEPTED' THEN 'ADMITTED'
                    ELSE 'REJECTED'
                END,
                updated_at = NEW.admitted_at,
                completed_at = CASE
                    WHEN NEW.decision = 'REJECTED' THEN NEW.admitted_at
                    ELSE NULL
                END
            WHERE id = NEW.contract_id;
        END
        """,
        # An attempt can exist only behind one accepted immutable admission.
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_attempts_require_admission
        BEFORE INSERT ON contract_attempts
        WHEN NOT EXISTS (
            SELECT 1
            FROM contract_admissions
            JOIN activation_contracts
              ON activation_contracts.id = NEW.contract_id
            WHERE contract_admissions.id = NEW.admission_id
              AND contract_admissions.contract_id = NEW.contract_id
              AND contract_admissions.decision = 'ACCEPTED'
              AND activation_contracts.worker_fingerprint_id =
                    NEW.worker_fingerprint_id
              AND activation_contracts.state IN ('ADMITTED', 'RUNNING')
              AND activation_contracts.max_attempts >= NEW.attempt_number
              AND NOT EXISTS (
                  SELECT 1
                  FROM worker_circuit_breakers
                  WHERE worker_circuit_breakers.worker_fingerprint_id =
                        NEW.worker_fingerprint_id
                    AND worker_circuit_breakers.goal_id =
                        activation_contracts.goal_id
                    AND worker_circuit_breakers.run_id =
                        activation_contracts.run_id
                    AND worker_circuit_breakers.state = 'OPEN'
              )
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'backend attempt requires accepted admission and closed circuit'
            );
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_attempts_format_repair_gate
        BEFORE INSERT ON contract_attempts
        WHEN NEW.attempt_kind = 'FORMAT_REPAIR'
          AND NOT EXISTS (
              SELECT 1
              FROM contract_attempts AS primary_attempt
              JOIN activation_results
                ON activation_results.attempt_id = primary_attempt.id
              JOIN contract_violations
                ON contract_violations.contract_id = NEW.contract_id
              WHERE primary_attempt.contract_id = NEW.contract_id
                AND primary_attempt.attempt_kind = 'PRIMARY'
                AND activation_results.disposition = 'FORMAT_INVALID'
                AND contract_violations.violation_code = 'FORMAT'
                AND contract_violations.disposition = 'FORMAT_REPAIR'
          )
        BEGIN
            SELECT RAISE(
                ABORT,
                'format repair requires one format-invalid primary result'
            );
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_attempts_identity_immutable
        BEFORE UPDATE ON contract_attempts
        WHEN NEW.contract_id <> OLD.contract_id
          OR NEW.admission_id <> OLD.admission_id
          OR NEW.worker_fingerprint_id <> OLD.worker_fingerprint_id
          OR NEW.attempt_number <> OLD.attempt_number
          OR NEW.attempt_kind <> OLD.attempt_kind
          OR NEW.output_only <> OLD.output_only
          OR NEW.backend <> OLD.backend
          OR NEW.model <> OLD.model
          OR NEW.input_digest <> OLD.input_digest
          OR NEW.idempotency_key <> OLD.idempotency_key
          OR NEW.started_at <> OLD.started_at
        BEGIN
            SELECT RAISE(ABORT, 'contract attempt inputs are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_attempts_legal_transition
        BEFORE UPDATE OF state ON contract_attempts
        WHEN NEW.state <> OLD.state
          AND NOT (
              (OLD.state = 'CREATED' AND NEW.state IN (
                  'RUNNING', 'SUCCEEDED', 'FAILED',
                  'QUARANTINED', 'CANCELLED'
              ))
              OR (OLD.state = 'RUNNING' AND NEW.state IN (
                  'SUCCEEDED', 'FAILED', 'QUARANTINED', 'CANCELLED'
              ))
          )
        BEGIN
            SELECT RAISE(ABORT, 'illegal contract attempt transition');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_attempts_mark_running
        AFTER INSERT ON contract_attempts
        WHEN (SELECT state FROM activation_contracts WHERE id = NEW.contract_id) =
             'ADMITTED'
        BEGIN
            UPDATE activation_contracts
            SET state = 'RUNNING',
                updated_at = NEW.started_at
            WHERE id = NEW.contract_id;
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_activation_results_integrity
        BEFORE INSERT ON activation_results
        WHEN NOT EXISTS (
            SELECT 1
            FROM contract_attempts
            WHERE contract_attempts.id = NEW.attempt_id
              AND contract_attempts.contract_id = NEW.contract_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'activation result does not match attempt contract');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_activation_results_no_update
        BEFORE UPDATE ON activation_results
        BEGIN
            SELECT RAISE(ABORT, 'activation results are append-only evidence');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_activation_results_no_delete
        BEFORE DELETE ON activation_results
        BEGIN
            SELECT RAISE(ABORT, 'activation results are append-only evidence');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_activation_results_apply_state
        AFTER INSERT ON activation_results
        BEGIN
            UPDATE contract_attempts
            SET state = CASE NEW.disposition
                    WHEN 'ACCEPTED' THEN 'SUCCEEDED'
                    WHEN 'QUARANTINED' THEN 'QUARANTINED'
                    ELSE 'FAILED'
                END,
                completed_at = NEW.recorded_at
            WHERE id = NEW.attempt_id;

            UPDATE activation_contracts
            SET state = CASE NEW.disposition
                    WHEN 'ACCEPTED' THEN 'RESULT_RECORDED'
                    WHEN 'QUARANTINED' THEN 'QUARANTINED'
                    ELSE state
                END,
                updated_at = NEW.recorded_at,
                completed_at = CASE
                    WHEN NEW.disposition = 'QUARANTINED'
                    THEN NEW.recorded_at
                    ELSE completed_at
                END
            WHERE id = NEW.contract_id;
        END
        """,
        # Violation disposition is determined by class and prior fingerprint history.
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_violations_integrity
        BEFORE INSERT ON contract_violations
        WHEN NOT EXISTS (
            SELECT 1
            FROM activation_contracts
            WHERE activation_contracts.id = NEW.contract_id
              AND activation_contracts.goal_id = NEW.goal_id
              AND activation_contracts.run_id = NEW.run_id
              AND activation_contracts.worker_fingerprint_id =
                    NEW.worker_fingerprint_id
              AND (
                  NEW.attempt_id IS NULL
                  OR EXISTS (
                      SELECT 1
                      FROM contract_attempts
                      WHERE contract_attempts.id = NEW.attempt_id
                        AND contract_attempts.contract_id = NEW.contract_id
                  )
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'contract violation scope is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_violations_format_sequence
        BEFORE INSERT ON contract_violations
        WHEN NEW.violation_code = 'FORMAT'
          AND (
              (
                  NEW.disposition = 'FORMAT_REPAIR'
                  AND EXISTS (
                      SELECT 1
                      FROM contract_violations
                      WHERE contract_violations.worker_fingerprint_id =
                            NEW.worker_fingerprint_id
                        AND contract_violations.goal_id = NEW.goal_id
                        AND contract_violations.run_id = NEW.run_id
                        AND contract_violations.violation_code = 'FORMAT'
                  )
              )
              OR (
                  NEW.disposition = 'CIRCUIT_OPEN'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM contract_violations
                      WHERE contract_violations.worker_fingerprint_id =
                            NEW.worker_fingerprint_id
                        AND contract_violations.goal_id = NEW.goal_id
                        AND contract_violations.run_id = NEW.run_id
                        AND contract_violations.violation_code = 'FORMAT'
                  )
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'format violation disposition is out of sequence');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_violations_no_update
        BEFORE UPDATE ON contract_violations
        BEGIN
            SELECT RAISE(ABORT, 'contract violations are append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_violations_no_delete
        BEFORE DELETE ON contract_violations
        BEGIN
            SELECT RAISE(ABORT, 'contract violations are append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_violations_immediate_quarantine
        AFTER INSERT ON contract_violations
        WHEN NEW.violation_code IN (
            'AUTHORITY', 'OID', 'WRITE_ROOT', 'NESTED_SPAWN'
        )
        BEGIN
            UPDATE activation_contracts
            SET state = 'QUARANTINED',
                updated_at = NEW.occurred_at,
                completed_at = NEW.occurred_at
            WHERE id = NEW.contract_id
              AND state NOT IN (
                  'REJECTED', 'COMPLETED', 'QUARANTINED', 'CANCELLED'
              );
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_violations_second_format_circuit
        AFTER INSERT ON contract_violations
        WHEN NEW.violation_code = 'FORMAT'
          AND NEW.disposition = 'CIRCUIT_OPEN'
        BEGIN
            INSERT INTO worker_circuit_breakers (
                id, worker_fingerprint_id, goal_id, run_id, state,
                reason_code, idempotency_key, opened_at
            )
            SELECT
                'circuit:' || NEW.worker_fingerprint_id || ':' ||
                    NEW.goal_id || ':' || NEW.run_id,
                NEW.worker_fingerprint_id,
                NEW.goal_id,
                NEW.run_id,
                'OPEN',
                'SECOND_FORMAT_VIOLATION',
                'circuit:' || NEW.worker_fingerprint_id || ':' ||
                    NEW.goal_id || ':' || NEW.run_id,
                NEW.occurred_at
            WHERE NOT EXISTS (
                SELECT 1
                FROM worker_circuit_breakers
                WHERE worker_circuit_breakers.worker_fingerprint_id =
                      NEW.worker_fingerprint_id
                  AND worker_circuit_breakers.goal_id = NEW.goal_id
                  AND worker_circuit_breakers.run_id = NEW.run_id
                  AND worker_circuit_breakers.state = 'OPEN'
            );

            UPDATE activation_contracts
            SET state = 'QUARANTINED',
                updated_at = NEW.occurred_at,
                completed_at = NEW.occurred_at
            WHERE id = NEW.contract_id
              AND state NOT IN (
                  'REJECTED', 'COMPLETED', 'QUARANTINED', 'CANCELLED'
              );
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_worker_circuit_breakers_identity
        BEFORE UPDATE ON worker_circuit_breakers
        WHEN NEW.worker_fingerprint_id <> OLD.worker_fingerprint_id
          OR NEW.goal_id <> OLD.goal_id
          OR NEW.run_id <> OLD.run_id
          OR NEW.reason_code <> OLD.reason_code
          OR NEW.idempotency_key <> OLD.idempotency_key
          OR NEW.opened_at <> OLD.opened_at
        BEGIN
            SELECT RAISE(ABORT, 'circuit-breaker identity is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_worker_circuit_breakers_transition
        BEFORE UPDATE OF state ON worker_circuit_breakers
        WHEN NEW.state <> OLD.state
          AND NOT (OLD.state = 'OPEN' AND NEW.state = 'CLOSED')
        BEGIN
            SELECT RAISE(ABORT, 'illegal circuit-breaker transition');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_worker_circuit_breakers_no_delete
        BEFORE DELETE ON worker_circuit_breakers
        BEGIN
            SELECT RAISE(ABORT, 'circuit breakers are retained evidence');
        END
        """,
        # Token rows cannot manufacture model calls for rejected admissions.
        """
        CREATE TRIGGER IF NOT EXISTS trg_token_ledger_scope
        BEFORE INSERT ON token_ledger_entries
        WHEN NOT EXISTS (
            SELECT 1
            FROM activation_contracts
            JOIN contract_admissions
              ON contract_admissions.id = NEW.admission_id
            WHERE activation_contracts.id = NEW.contract_id
              AND contract_admissions.contract_id = NEW.contract_id
              AND activation_contracts.worker_fingerprint_id =
                    NEW.worker_fingerprint_id
              AND (
                  NEW.attempt_id IS NULL
                  OR EXISTS (
                      SELECT 1
                      FROM contract_attempts
                      WHERE contract_attempts.id = NEW.attempt_id
                        AND contract_attempts.contract_id = NEW.contract_id
                  )
              )
              AND (
                  contract_admissions.decision <> 'REJECTED'
                  OR (
                      NEW.entry_kind = 'ADMISSION_REJECTED'
                      AND NEW.attempt_id IS NULL
                      AND NEW.input_tokens = 0
                      AND NEW.output_tokens = 0
                      AND NEW.model_calls = 0
                  )
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'token ledger entry conflicts with admission');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_token_ledger_no_update
        BEFORE UPDATE ON token_ledger_entries
        BEGIN
            SELECT RAISE(ABORT, 'token ledger is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_token_ledger_no_delete
        BEFORE DELETE ON token_ledger_entries
        BEGIN
            SELECT RAISE(ABORT, 'token ledger is append-only');
        END
        """,
        # MCP observations and receipts are immutable and contract-scoped.
        """
        CREATE TRIGGER IF NOT EXISTS trg_mcp_health_observations_no_update
        BEFORE UPDATE ON mcp_health_observations
        BEGIN
            SELECT RAISE(ABORT, 'MCP health observations are append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_mcp_health_observations_no_delete
        BEFORE DELETE ON mcp_health_observations
        BEGIN
            SELECT RAISE(ABORT, 'MCP health observations are append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_mcp_usage_receipts_integrity
        BEFORE INSERT ON mcp_usage_receipts
        WHEN NOT EXISTS (
            SELECT 1
            FROM contract_attempts
            JOIN contract_mcp_bindings
              ON contract_mcp_bindings.id = NEW.mcp_binding_id
            JOIN mcp_definitions
              ON mcp_definitions.id =
                 contract_mcp_bindings.mcp_definition_id
            WHERE contract_attempts.id = NEW.attempt_id
              AND contract_attempts.contract_id = NEW.contract_id
              AND contract_attempts.state IN ('CREATED', 'RUNNING')
              AND contract_mcp_bindings.contract_id = NEW.contract_id
              AND mcp_definitions.server_name <> ''
              AND mcp_definitions.tool_name = NEW.tool_name
              AND mcp_definitions.state = 'ACTIVE'
        )
        BEGIN
            SELECT RAISE(ABORT, 'MCP usage receipt does not match binding');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_mcp_usage_receipts_no_update
        BEFORE UPDATE ON mcp_usage_receipts
        BEGIN
            SELECT RAISE(ABORT, 'MCP usage receipts are append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_mcp_usage_receipts_no_delete
        BEFORE DELETE ON mcp_usage_receipts
        BEGIN
            SELECT RAISE(ABORT, 'MCP usage receipts are append-only');
        END
        """,
        # Serena snapshot bytes, selections, and consumption are append-only.
        """
        CREATE TRIGGER IF NOT EXISTS trg_serena_snapshots_no_update
        BEFORE UPDATE ON serena_onboarding_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'Serena onboarding snapshots are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_serena_snapshots_no_delete
        BEFORE DELETE ON serena_onboarding_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'Serena onboarding snapshots are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_serena_snapshot_memories_no_update
        BEFORE UPDATE ON serena_snapshot_memory_bindings
        BEGIN
            SELECT RAISE(ABORT, 'Serena snapshot memories are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_serena_snapshot_memories_no_delete
        BEFORE DELETE ON serena_snapshot_memory_bindings
        BEGIN
            SELECT RAISE(ABORT, 'Serena snapshot memories are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_serena_memory_integrity
        BEFORE INSERT ON contract_serena_memory_bindings
        WHEN NOT EXISTS (
            SELECT 1
            FROM activation_contracts
            JOIN serena_onboarding_snapshots
              ON serena_onboarding_snapshots.id = NEW.snapshot_id
            JOIN serena_snapshot_memory_bindings
              ON serena_snapshot_memory_bindings.snapshot_id = NEW.snapshot_id
             AND serena_snapshot_memory_bindings.memory_name = NEW.memory_name
            WHERE activation_contracts.id = NEW.contract_id
              AND activation_contracts.repository_id =
                    serena_onboarding_snapshots.repository_id
              AND serena_onboarding_snapshots.state = 'ACCEPTED'
              AND serena_onboarding_snapshots.source_oid IN (
                  activation_contracts.base_oid,
                  activation_contracts.subject_oid
              )
              AND serena_snapshot_memory_bindings.memory_ref = NEW.memory_ref
              AND serena_snapshot_memory_bindings.memory_sha256 =
                    NEW.memory_sha256
        )
        BEGIN
            SELECT RAISE(ABORT, 'selected Serena memory does not match snapshot');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_serena_memories_no_update
        BEFORE UPDATE ON contract_serena_memory_bindings
        BEGIN
            SELECT RAISE(ABORT, 'selected Serena memories are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_contract_serena_memories_no_delete
        BEFORE DELETE ON contract_serena_memory_bindings
        BEGIN
            SELECT RAISE(ABORT, 'selected Serena memories are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_serena_consumption_integrity
        BEFORE INSERT ON serena_consumption_receipts
        WHEN NOT EXISTS (
            SELECT 1
            FROM activation_contracts
            JOIN contract_serena_memory_bindings
              ON contract_serena_memory_bindings.id = NEW.memory_binding_id
            WHERE activation_contracts.id = NEW.contract_id
              AND activation_contracts.worker_fingerprint_id =
                    NEW.worker_fingerprint_id
              AND contract_serena_memory_bindings.contract_id = NEW.contract_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'Serena consumption does not match contract');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_serena_consumption_no_update
        BEFORE UPDATE ON serena_consumption_receipts
        BEGIN
            SELECT RAISE(ABORT, 'Serena consumption receipts are append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_serena_consumption_no_delete
        BEFORE DELETE ON serena_consumption_receipts
        BEGIN
            SELECT RAISE(ABORT, 'Serena consumption receipts are append-only');
        END
        """,
        # Migration, deletion, and generic evidence cannot be rewritten.
        """
        CREATE TRIGGER IF NOT EXISTS trg_migration_evidence_no_update
        BEFORE UPDATE ON migration_evidence
        BEGIN
            SELECT RAISE(ABORT, 'migration evidence is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_migration_evidence_no_delete
        BEFORE DELETE ON migration_evidence
        BEGIN
            SELECT RAISE(ABORT, 'migration evidence is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_deletion_manifest_entries_no_update
        BEFORE UPDATE ON deletion_manifest_entries
        BEGIN
            SELECT RAISE(ABORT, 'deletion manifest entries are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_deletion_manifest_entries_no_delete
        BEFORE DELETE ON deletion_manifest_entries
        BEGIN
            SELECT RAISE(ABORT, 'deletion manifest entries are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_control_plane_evidence_no_update
        BEFORE UPDATE ON control_plane_evidence
        BEGIN
            SELECT RAISE(ABORT, 'control-plane evidence is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_control_plane_evidence_no_delete
        BEFORE DELETE ON control_plane_evidence
        BEGIN
            SELECT RAISE(ABORT, 'control-plane evidence is append-only');
        END
        """,
    ),
)


DEFAULT_MIGRATIONS = (
    LEGACY_QUEUE_MIGRATION,
    AX_DOMAIN_MIGRATION,
    AX_INVARIANTS_MIGRATION,
    AX_V4_CONTROL_PLANE_MIGRATION,
)


# These triggers close v4 integrity gaps without introducing a parallel schema
# version.  ``initialize`` refreshes them for databases that were already at v4
# before the hardening shipped, while the migration definitions above keep new
# databases correct from their first transaction.
_V4_REFRESHED_TRIGGERS = (
    "trg_workflow_instances_legal_transition",
    "trg_workflow_transition_receipts_integrity",
    "trg_mcp_usage_receipts_integrity",
)

_V4_HARDENED_TRIGGER_SQL = (
    """
    CREATE TRIGGER trg_workflow_instances_legal_transition
    BEFORE UPDATE OF current_state_key ON workflow_instances
    WHEN NEW.current_state_key <> OLD.current_state_key
      AND NOT EXISTS (
          SELECT 1
          FROM workflow_transitions
          WHERE workflow_transitions.workflow_definition_id =
                    OLD.workflow_definition_id
            AND workflow_transitions.from_state_key = OLD.current_state_key
            AND (
                workflow_transitions.to_state_key = NEW.current_state_key
                OR workflow_transitions.failure_route = NEW.current_state_key
            )
            AND workflow_transitions.state = 'ACTIVE'
      )
    BEGIN
        SELECT RAISE(ABORT, 'illegal workflow state transition');
    END
    """,
    """
    CREATE TRIGGER trg_workflow_transition_receipts_integrity
    BEFORE INSERT ON workflow_transition_receipts
    WHEN NOT EXISTS (
        SELECT 1
        FROM workflow_instances
        JOIN workflow_transitions
          ON workflow_transitions.id = NEW.workflow_transition_id
        WHERE workflow_instances.id = NEW.workflow_instance_id
          AND workflow_instances.workflow_definition_id =
                workflow_transitions.workflow_definition_id
          AND workflow_transitions.from_state_key = NEW.from_state_key
          AND (
              workflow_transitions.to_state_key = NEW.to_state_key
              OR workflow_transitions.failure_route = NEW.to_state_key
          )
          AND EXISTS (
              SELECT 1
              FROM workflow_states
              WHERE workflow_states.workflow_definition_id =
                    workflow_instances.workflow_definition_id
                AND workflow_states.state_key = NEW.to_state_key
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'transition receipt does not match workflow');
    END
    """,
    """
    CREATE TRIGGER trg_mcp_usage_receipts_integrity
    BEFORE INSERT ON mcp_usage_receipts
    WHEN NOT EXISTS (
        SELECT 1
        FROM contract_attempts
        JOIN contract_mcp_bindings
          ON contract_mcp_bindings.id = NEW.mcp_binding_id
        JOIN mcp_definitions
          ON mcp_definitions.id = contract_mcp_bindings.mcp_definition_id
        WHERE contract_attempts.id = NEW.attempt_id
          AND contract_attempts.contract_id = NEW.contract_id
          AND contract_attempts.state IN ('CREATED', 'RUNNING')
          AND contract_mcp_bindings.contract_id = NEW.contract_id
          AND mcp_definitions.tool_name = NEW.tool_name
          AND mcp_definitions.state = 'ACTIVE'
    )
    BEGIN
        SELECT RAISE(ABORT, 'MCP usage receipt does not match active invocation');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_activation_contracts_base_oid_integrity
    BEFORE INSERT ON activation_contracts
    WHEN NOT EXISTS (
        SELECT 1
        FROM runtime_leases
        WHERE runtime_leases.id = NEW.lease_id
          AND runtime_leases.base_oid = NEW.base_oid
    )
    BEGIN
        SELECT RAISE(ABORT, 'activation contract base OID differs from lease');
    END
    """,
)


# Rationale is kept next to the schema so later migrations preserve the intent.
INDEX_RATIONALE = {
    "ux_workspace_leases_active_branch": (
        "At most one active writer may own a target-local branch ref."
    ),
    "ux_workspace_leases_active_worktree": (
        "At most one active lease may own a physical worktree path."
    ),
    "ux_workspace_leases_active_workspace": (
        "A workspace cannot be concurrently leased to two writers."
    ),
    "ix_gate_decisions_subject": (
        "Promotion and recovery aggregate independent gate evidence by exact OID."
    ),
    "ix_operation_intents_status": (
        "Startup reconciliation scans incomplete cross-SQLite/Git operations."
    ),
}


LEGACY_REQUIRED_COLUMNS = {
    "messages": {
        "seq",
        "id",
        "thread_id",
        "work_item_id",
        "parent_message_id",
        "from_role",
        "to_role",
        "type",
        "priority",
        "payload_json",
        "status",
        "available_at",
        "claimed_by",
        "lease_until",
        "attempts",
        "max_attempts",
        "dedupe_key",
        "last_error",
        "created_at",
        "processed_at",
    },
    "outbox": {
        "seq",
        "id",
        "message_id",
        "event_type",
        "payload_json",
        "status",
        "attempts",
        "available_at",
        "created_at",
        "published_at",
        "last_error",
    },
    "thread_snapshots": {
        "id",
        "thread_id",
        "work_item_id",
        "target_role",
        "covered_through_seq",
        "payload_json",
        "created_at",
    },
    "project_knowledge_state": {
        "repo_id",
        "project_path",
        "baseline_oid",
        "inspected_oid",
        "source_fingerprint",
        "state",
        "memory_manifest_json",
        "owner_seat_id",
        "evidence_artifact_ref",
        "memory_manifest_sha256",
        "last_request_message_id",
        "acknowledged_at",
        "updated_at",
    },
}


class AxStateStore:
    """Shared, versioned SQLite state layer used by queue and worktree services.

    Every migration is monotonic and runs in its own `BEGIN IMMEDIATE`
    transaction.  A future migration marked ``destructive=True`` is refused
    unless ``backup_hook`` returns an existing backup path before the
    transaction starts.  Current migrations are additive and non-destructive.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        backup_hook: BackupHook | None = None,
        migrations: Sequence[SchemaMigration] | None = None,
    ) -> None:
        if (
            not isinstance(busy_timeout_ms, int)
            or isinstance(busy_timeout_ms, bool)
            or busy_timeout_ms < 1
        ):
            raise ValueError("busy_timeout_ms must be a positive integer")
        selected = tuple(migrations or DEFAULT_MIGRATIONS)
        versions = tuple(migration.version for migration in selected)
        if versions != tuple(range(1, len(selected) + 1)):
            raise ValueError("migrations must be contiguous and start at version 1")
        self.db_path = Path(db_path).expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self.backup_hook = backup_hook
        self._migrations = selected
        self._initialized = False
        self.__mcp_receipt_authority = object()

    @property
    def latest_schema_version(self) -> int:
        return self._migrations[-1].version

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def connect(self) -> sqlite3.Connection:
        """Return a configured connection after ensuring the latest schema."""

        if not self._initialized:
            self.initialize()
        return self._connect()

    def initialize(self, target_version: int = LATEST_SCHEMA_VERSION) -> int:
        if (
            not isinstance(target_version, int)
            or isinstance(target_version, bool)
            or target_version < 1
            or target_version > self.latest_schema_version
        ):
            raise ValueError(
                "target_version must be between 1 and "
                f"{self.latest_schema_version}"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            self._ensure_metadata(connection)

        current = self.schema_version()
        if current > self.latest_schema_version:
            raise SchemaCompatibilityError(
                f"database schema {current} is newer than supported "
                f"{self.latest_schema_version}"
            )
        if current > target_version:
            raise SchemaCompatibilityError(
                f"database schema {current} cannot be downgraded in place to "
                f"{target_version}; restore the pre-migration SQLite backup"
            )
        for migration in self._migrations:
            if migration.version <= current or migration.version > target_version:
                continue
            if migration.destructive:
                self._require_backup(migration)
            self._apply_migration(migration)
            current = migration.version

        with closing(self._connect()) as connection:
            if current >= 4 and target_version >= 4:
                self._ensure_v4_hardening(connection)
            self._validate_legacy_schema(connection)
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                first = violations[0]
                raise SchemaCompatibilityError(
                    "foreign-key validation failed after initialization: "
                    f"{tuple(first)}"
                )
        self._initialized = True
        return current

    @staticmethod
    def _ensure_v4_hardening(connection: sqlite3.Connection) -> None:
        """Idempotently harden already-versioned v4 databases.

        The schema version remains v4 because this only tightens the declared
        v4 invariants.  Refreshing the named triggers in one immediate
        transaction prevents an old process from observing a partially updated
        integrity boundary.
        """

        try:
            connection.execute("BEGIN IMMEDIATE")
            for trigger_name in _V4_REFRESHED_TRIGGERS:
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            for statement in _V4_HARDENED_TRIGGER_SQL:
                connection.execute(statement)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def schema_version(self) -> int:
        if not self.db_path.exists():
            return 0
        with closing(self._connect()) as connection:
            exists = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'ax_schema_meta'
                """
            ).fetchone()
            if exists is None:
                return 0
            row = connection.execute(
                "SELECT schema_version FROM ax_schema_meta WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise SchemaCompatibilityError("ax_schema_meta singleton row is missing")
        version = row["schema_version"]
        if not isinstance(version, int) or version < 0:
            raise SchemaCompatibilityError(f"invalid schema version: {version!r}")
        return version

    @contextmanager
    def transaction(
        self,
        *,
        immediate: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        if not self._initialized:
            self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def backup_to(self, destination: str | Path) -> Path:
        """Create a consistent SQLite backup suitable for destructive upgrades."""

        if not self._initialized:
            self.initialize()
        target = Path(destination).expanduser().resolve()
        if target == self.db_path:
            raise ValueError("backup destination must differ from the live database")
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as source:
            with closing(sqlite3.connect(target)) as backup:
                source.backup(backup)
        if not target.is_file() or target.stat().st_size == 0:
            raise SchemaMigrationError(f"SQLite backup was not created: {target}")
        return target

    def begin_intent(
        self,
        *,
        operation: str,
        idempotency_key: str,
        expected_state: str,
        expected_oid: str | None,
        payload: Mapping[str, Any],
    ) -> IntentRecord:
        operation = require_identifier(operation, "operation")
        idempotency_key = require_nonempty(idempotency_key, "idempotency_key")
        expected_state = require_nonempty(expected_state, "expected_state")
        expected_oid = require_oid(expected_oid, "expected_oid", optional=True)
        payload_json = compact_json(payload)

        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM operation_intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                self._assert_same_intent(
                    existing,
                    operation=operation,
                    expected_state=expected_state,
                    expected_oid=expected_oid,
                    payload_json=payload_json,
                )
                return self._intent_from_row(existing)

            intent_id = f"intent-{uuid.uuid4().hex}"
            created_at = utc_now()
            connection.execute(
                """
                INSERT INTO operation_intents (
                    id, operation, idempotency_key, expected_state,
                    expected_oid, payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    intent_id,
                    operation,
                    idempotency_key,
                    expected_state,
                    expected_oid,
                    payload_json,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM operation_intents WHERE id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise IntentStateError("intent insert did not return a row")
            return self._intent_from_row(row)

    def complete_intent(
        self,
        intent_id: str,
        *,
        resulting_state: str,
        resulting_oid: str | None,
        evidence: Mapping[str, Any],
    ) -> IntentRecord:
        intent_id = require_identifier(intent_id, "intent_id")
        resulting_state = require_nonempty(resulting_state, "resulting_state")
        resulting_oid = require_oid(resulting_oid, "resulting_oid", optional=True)
        evidence_json = compact_json(evidence)

        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM operation_intents WHERE id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(intent_id)
            if row["status"] == IntentStatus.COMPLETED.value:
                if (
                    row["resulting_state"] != resulting_state
                    or row["resulting_oid"] != resulting_oid
                    or row["evidence_json"] != evidence_json
                ):
                    raise IdempotencyConflictError(
                        f"completed intent {intent_id} has a different outcome"
                    )
                return self._intent_from_row(row)
            if row["status"] != IntentStatus.PENDING.value:
                raise IntentStateError(
                    f"intent {intent_id} cannot complete from {row['status']}"
                )

            completed_at = utc_now()
            updated = connection.execute(
                """
                UPDATE operation_intents
                SET status = 'COMPLETED',
                    resulting_state = ?,
                    resulting_oid = ?,
                    evidence_json = ?,
                    completed_at = ?
                WHERE id = ? AND status = 'PENDING'
                """,
                (
                    resulting_state,
                    resulting_oid,
                    evidence_json,
                    completed_at,
                    intent_id,
                ),
            ).rowcount
            if updated != 1:
                raise IntentStateError(
                    f"concurrent completion rejected for intent {intent_id}"
                )
            completed = connection.execute(
                "SELECT * FROM operation_intents WHERE id = ?",
                (intent_id,),
            ).fetchone()
            if completed is None:
                raise IntentStateError("completed intent row disappeared")
            return self._intent_from_row(completed)

    def record_audit_event(self, event: AuditEvent) -> None:
        if not isinstance(event, AuditEvent):
            raise TypeError("event must be an AuditEvent")
        payload_json = compact_json(event.payload)
        values = (
            event.event_id,
            event.event_type,
            event.actor,
            event.subject_type,
            event.subject_id,
            event.goal_id,
            event.run_id,
            event.activation_id,
            event.subject_oid,
            payload_json,
            event.idempotency_key,
            event.occurred_at,
        )
        with self.transaction(immediate=True) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO audit_events (
                        id, event_type, actor, subject_type, subject_id,
                        goal_id, run_id, activation_id, subject_oid,
                        payload_json, idempotency_key, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                row = connection.execute(
                    """
                    SELECT * FROM audit_events
                    WHERE id = ? OR (
                        ? IS NOT NULL AND idempotency_key = ?
                    )
                    """,
                    (
                        event.event_id,
                        event.idempotency_key,
                        event.idempotency_key,
                    ),
                ).fetchone()
                if row is None or self._audit_signature(row) != values:
                    raise IdempotencyConflictError(
                        "audit event ID/idempotency key was reused with different data"
                    ) from exc

    def register_definition(
        self,
        *,
        kind: str,
        version: str,
        sha256: str,
        source_ref: str,
    ) -> str:
        """Idempotently register immutable versioned definition bytes."""

        kind_value = require_definition_kind(kind).value
        version = require_nonempty(version, "version")
        digest = require_sha256(sha256, "sha256")
        source_ref = require_nonempty(source_ref, "source_ref")
        definition_id = stable_identifier("definition", kind_value, version)
        idempotency_key = f"definition:{kind_value}:{version}"
        registered_at = utc_now()

        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM registered_definitions
                WHERE kind = ? AND version = ?
                """,
                (kind_value, version),
            ).fetchone()
            if existing is not None:
                if (
                    existing["id"] != definition_id
                    or existing["sha256"] != digest
                    or existing["source_ref"] != source_ref
                ):
                    raise IdempotencyConflictError(
                        f"definition {kind_value}:{version} was registered "
                        "with different immutable bytes"
                    )
                return existing["id"]
            connection.execute(
                """
                INSERT INTO registered_definitions (
                    id, kind, version, sha256, source_ref,
                    idempotency_key, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    definition_id,
                    kind_value,
                    version,
                    digest,
                    source_ref,
                    idempotency_key,
                    registered_at,
                ),
            )
        return definition_id

    def register_activation_contract(
        self,
        *,
        contract_id: str,
        workflow_instance_id: str,
        workflow_transition_id: str,
        goal_id: str,
        run_id: str,
        capability_id: str,
        worker_id: str,
        worker_fingerprint_id: str,
        slot_id: str,
        worker_assignment_id: str,
        repository_id: str,
        lease_id: str,
        sandbox_binding_id: str,
        oid_authority_id: str,
        base_oid: str,
        subject_oid: str,
        contract_definition_id: str,
        output_schema_definition_id: str,
        contract_digest: str,
        packet_digest: str,
        context_char_budget: int,
        max_attempts: int,
        idempotency_key: str,
        physical_seat_id: str | None = None,
        seat_capability_activation_id: str | None = None,
    ) -> str:
        """Persist one fully resolved immutable activation contract."""

        identifiers = {
            "contract_id": contract_id,
            "workflow_instance_id": workflow_instance_id,
            "workflow_transition_id": workflow_transition_id,
            "goal_id": goal_id,
            "run_id": run_id,
            "capability_id": capability_id,
            "worker_id": worker_id,
            "worker_fingerprint_id": worker_fingerprint_id,
            "slot_id": slot_id,
            "worker_assignment_id": worker_assignment_id,
            "repository_id": repository_id,
            "lease_id": lease_id,
            "sandbox_binding_id": sandbox_binding_id,
            "oid_authority_id": oid_authority_id,
            "contract_definition_id": contract_definition_id,
            "output_schema_definition_id": output_schema_definition_id,
        }
        normalized_ids = {
            field: require_identifier(value, field)
            for field, value in identifiers.items()
        }
        if physical_seat_id is not None:
            physical_seat_id = require_identifier(
                physical_seat_id, "physical_seat_id"
            )
        if seat_capability_activation_id is not None:
            seat_capability_activation_id = require_identifier(
                seat_capability_activation_id,
                "seat_capability_activation_id",
            )
        if (physical_seat_id is None) != (
            seat_capability_activation_id is None
        ):
            raise ValueError(
                "physical_seat_id and seat_capability_activation_id "
                "must either both be set or both be absent"
            )
        base_oid = str(require_oid(base_oid, "base_oid"))
        subject_oid = str(require_oid(subject_oid, "subject_oid"))
        contract_digest = require_sha256(contract_digest, "contract_digest")
        packet_digest = require_sha256(packet_digest, "packet_digest")
        context_char_budget = require_positive(
            context_char_budget, "context_char_budget"
        )
        if context_char_budget > 12_000:
            raise ValueError("context_char_budget must not exceed 12000")
        if max_attempts not in (1, 2) or isinstance(max_attempts, bool):
            raise ValueError("max_attempts must be 1 or 2")
        idempotency_key = require_nonempty(idempotency_key, "idempotency_key")
        issued_at = utc_now()
        values = (
            normalized_ids["contract_id"],
            normalized_ids["workflow_instance_id"],
            normalized_ids["workflow_transition_id"],
            normalized_ids["goal_id"],
            normalized_ids["run_id"],
            physical_seat_id,
            normalized_ids["capability_id"],
            seat_capability_activation_id,
            normalized_ids["worker_id"],
            normalized_ids["worker_fingerprint_id"],
            normalized_ids["slot_id"],
            normalized_ids["worker_assignment_id"],
            normalized_ids["repository_id"],
            normalized_ids["lease_id"],
            normalized_ids["sandbox_binding_id"],
            normalized_ids["oid_authority_id"],
            base_oid,
            subject_oid,
            normalized_ids["contract_definition_id"],
            normalized_ids["output_schema_definition_id"],
            contract_digest,
            packet_digest,
            context_char_budget,
            max_attempts,
            idempotency_key,
        )

        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM activation_contracts
                WHERE id = ? OR idempotency_key = ?
                """,
                (normalized_ids["contract_id"], idempotency_key),
            ).fetchone()
            if existing is not None:
                signature = tuple(
                    existing[column]
                    for column in (
                        "id",
                        "workflow_instance_id",
                        "workflow_transition_id",
                        "goal_id",
                        "run_id",
                        "physical_seat_id",
                        "capability_id",
                        "seat_capability_activation_id",
                        "worker_id",
                        "worker_fingerprint_id",
                        "slot_id",
                        "worker_assignment_id",
                        "repository_id",
                        "lease_id",
                        "sandbox_binding_id",
                        "oid_authority_id",
                        "base_oid",
                        "subject_oid",
                        "contract_definition_id",
                        "output_schema_definition_id",
                        "contract_digest",
                        "packet_digest",
                        "context_char_budget",
                        "max_attempts",
                        "idempotency_key",
                    )
                )
                if signature != values:
                    raise IdempotencyConflictError(
                        "activation contract ID/idempotency key was reused"
                    )
                return existing["id"]
            connection.execute(
                """
                INSERT INTO activation_contracts (
                    id, workflow_instance_id, workflow_transition_id,
                    goal_id, run_id, physical_seat_id, capability_id,
                    seat_capability_activation_id, worker_id,
                    worker_fingerprint_id, slot_id, worker_assignment_id,
                    repository_id, lease_id, sandbox_binding_id,
                    oid_authority_id, base_oid, subject_oid,
                    contract_definition_id, output_schema_definition_id,
                    contract_digest, packet_digest, context_char_budget,
                    max_attempts, state, idempotency_key, issued_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, 'ISSUED', ?, ?, ?
                )
                """,
                (*values, issued_at, issued_at),
            )
        return normalized_ids["contract_id"]

    def bind_contract_clause(
        self,
        *,
        contract_id: str,
        ordinal: int,
        definition_id: str,
        clause_digest: str,
        character_count: int,
        idempotency_key: str | None = None,
    ) -> str:
        contract_id = require_identifier(contract_id, "contract_id")
        definition_id = require_identifier(definition_id, "definition_id")
        ordinal = require_nonnegative(ordinal, "ordinal")
        clause_digest = require_sha256(clause_digest, "clause_digest")
        character_count = require_nonnegative(character_count, "character_count")
        binding_key = idempotency_key or (
            f"contract-clause:{contract_id}:{ordinal}:{definition_id}"
        )
        binding_key = require_nonempty(binding_key, "idempotency_key")
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM contract_clause_bindings
                WHERE contract_id = ? AND ordinal = ?
                """,
                (contract_id, ordinal),
            ).fetchone()
            if existing is not None:
                if (
                    existing["definition_id"] != definition_id
                    or existing["clause_digest"] != clause_digest
                    or existing["character_count"] != character_count
                ):
                    raise IdempotencyConflictError(
                        "contract clause ordinal was reused with different bytes"
                    )
                return binding_key
            connection.execute(
                """
                INSERT INTO contract_clause_bindings (
                    contract_id, ordinal, definition_id, clause_digest,
                    character_count, idempotency_key, bound_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract_id,
                    ordinal,
                    definition_id,
                    clause_digest,
                    character_count,
                    binding_key,
                    utc_now(),
                ),
            )
        return binding_key

    def bind_contract_profile(
        self,
        *,
        contract_id: str,
        profile_definition_id: str,
        compiled_profile_ref: str,
        compiled_profile_digest: str,
        idempotency_key: str | None = None,
    ) -> str:
        contract_id = require_identifier(contract_id, "contract_id")
        profile_definition_id = require_identifier(
            profile_definition_id, "profile_definition_id"
        )
        compiled_profile_ref = require_nonempty(
            compiled_profile_ref, "compiled_profile_ref"
        )
        compiled_profile_digest = require_sha256(
            compiled_profile_digest, "compiled_profile_digest"
        )
        binding_id = stable_identifier("profile-binding", contract_id)
        binding_key = require_nonempty(
            idempotency_key or f"profile-binding:{contract_id}",
            "idempotency_key",
        )
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM contract_profile_bindings WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["profile_definition_id"] != profile_definition_id
                    or existing["compiled_profile_ref"] != compiled_profile_ref
                    or existing["compiled_profile_digest"]
                    != compiled_profile_digest
                ):
                    raise IdempotencyConflictError(
                        "contract already has a different profile binding"
                    )
                return existing["id"]
            connection.execute(
                """
                INSERT INTO contract_profile_bindings (
                    id, contract_id, profile_definition_id,
                    professional_skill_id, compiled_profile_ref,
                    compiled_profile_digest, state, idempotency_key, bound_at
                ) VALUES (
                    ?, ?, ?, 'professional-profile-runtime', ?, ?,
                    'BOUND', ?, ?
                )
                """,
                (
                    binding_id,
                    contract_id,
                    profile_definition_id,
                    compiled_profile_ref,
                    compiled_profile_digest,
                    binding_key,
                    utc_now(),
                ),
            )
        return binding_id

    def bind_contract_skill(
        self,
        *,
        contract_id: str,
        skill_definition_id: str,
        capability_id: str,
        ordinal: int,
        bound_digest: str,
        content_character_count: int,
        idempotency_key: str | None = None,
    ) -> str:
        contract_id = require_identifier(contract_id, "contract_id")
        skill_definition_id = require_identifier(
            skill_definition_id, "skill_definition_id"
        )
        capability_id = require_identifier(capability_id, "capability_id")
        ordinal = require_nonnegative(ordinal, "ordinal")
        bound_digest = require_sha256(bound_digest, "bound_digest")
        content_character_count = require_nonnegative(
            content_character_count, "content_character_count"
        )
        binding_id = stable_identifier(
            "skill-binding", contract_id, skill_definition_id
        )
        binding_key = require_nonempty(
            idempotency_key
            or f"skill-binding:{contract_id}:{skill_definition_id}",
            "idempotency_key",
        )
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM contract_skill_bindings
                WHERE contract_id = ? AND skill_definition_id = ?
                """,
                (contract_id, skill_definition_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["capability_id"] != capability_id
                    or existing["ordinal"] != ordinal
                    or existing["bound_digest"] != bound_digest
                    or existing["content_character_count"]
                    != content_character_count
                ):
                    raise IdempotencyConflictError(
                        "contract Skill binding was reused with different inputs"
                    )
                return existing["id"]
            connection.execute(
                """
                INSERT INTO contract_skill_bindings (
                    id, contract_id, skill_definition_id, capability_id,
                    ordinal, bound_digest, content_character_count, state,
                    idempotency_key, bound_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'BOUND', ?, ?)
                """,
                (
                    binding_id,
                    contract_id,
                    skill_definition_id,
                    capability_id,
                    ordinal,
                    bound_digest,
                    content_character_count,
                    binding_key,
                    utc_now(),
                ),
            )
        return binding_id

    def record_contract_admission(
        self,
        *,
        contract_id: str,
        accepted: bool,
        reason_code: str | None,
    ) -> str:
        """Persist one admission; rejection deliberately creates no attempt row."""

        contract_id = require_identifier(contract_id, "contract_id")
        accepted = require_boolean(accepted, "accepted")
        if accepted:
            if reason_code is not None:
                raise ValueError("accepted admission must not include reason_code")
            decision = AdmissionDecision.ACCEPTED.value
        else:
            reason_code = require_identifier(reason_code, "reason_code")
            decision = AdmissionDecision.REJECTED.value

        with self.transaction(immediate=True) as connection:
            contract = connection.execute(
                "SELECT * FROM activation_contracts WHERE id = ?",
                (contract_id,),
            ).fetchone()
            if contract is None:
                raise KeyError(contract_id)
            admission_id = stable_identifier(
                "admission", contract_id, contract["contract_digest"]
            )
            idempotency_key = f"admission:{contract_id}"
            existing = connection.execute(
                "SELECT * FROM contract_admissions WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["decision"] != decision
                    or existing["reason_code"] != reason_code
                    or existing["contract_digest"] != contract["contract_digest"]
                ):
                    raise IdempotencyConflictError(
                        "contract admission was replayed with a different decision"
                    )
                return existing["id"]
            admitted_at = utc_now()
            connection.execute(
                """
                INSERT INTO contract_admissions (
                    id, contract_id, decision, reason_code, deterministic,
                    contract_digest, idempotency_key, admitted_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    admission_id,
                    contract_id,
                    decision,
                    reason_code,
                    contract["contract_digest"],
                    idempotency_key,
                    admitted_at,
                ),
            )
            if not accepted:
                connection.execute(
                    """
                    INSERT INTO token_ledger_entries (
                        id, contract_id, admission_id, attempt_id,
                        worker_fingerprint_id, entry_kind, input_tokens,
                        output_tokens, model_calls, idempotency_key, occurred_at
                    ) VALUES (
                        ?, ?, ?, NULL, ?, 'ADMISSION_REJECTED',
                        0, 0, 0, ?, ?
                    )
                    """,
                    (
                        stable_identifier(
                            "token-entry", admission_id, "ADMISSION_REJECTED"
                        ),
                        contract_id,
                        admission_id,
                        contract["worker_fingerprint_id"],
                        f"token-entry:{admission_id}:ADMISSION_REJECTED",
                        admitted_at,
                    ),
                )
        return admission_id

    def record_contract_attempt(
        self,
        *,
        contract_id: str,
        backend: str,
        model: str,
        input_digest: str,
        attempt_kind: ContractAttemptKind | str = ContractAttemptKind.PRIMARY,
        idempotency_key: str | None = None,
    ) -> str:
        contract_id = require_identifier(contract_id, "contract_id")
        backend = require_nonempty(backend, "backend")
        model = require_nonempty(model, "model")
        input_digest = require_sha256(input_digest, "input_digest")
        kind = _enum_text(attempt_kind, ContractAttemptKind, "attempt_kind")
        attempt_number = 1 if kind == ContractAttemptKind.PRIMARY.value else 2
        output_only = int(kind == ContractAttemptKind.FORMAT_REPAIR.value)
        attempt_id = stable_identifier("attempt-v4", contract_id, kind)
        attempt_key = require_nonempty(
            idempotency_key or f"attempt-v4:{contract_id}:{kind}",
            "idempotency_key",
        )

        with self.transaction(immediate=True) as connection:
            contract = connection.execute(
                """
                SELECT activation_contracts.*, contract_admissions.id AS admission_id,
                       contract_admissions.decision AS admission_decision
                FROM activation_contracts
                LEFT JOIN contract_admissions
                  ON contract_admissions.contract_id = activation_contracts.id
                WHERE activation_contracts.id = ?
                """,
                (contract_id,),
            ).fetchone()
            if contract is None:
                raise KeyError(contract_id)
            if contract["admission_decision"] != AdmissionDecision.ACCEPTED.value:
                raise IntentStateError(
                    "backend attempt requires an accepted contract admission"
                )
            existing = connection.execute(
                """
                SELECT * FROM contract_attempts
                WHERE id = ? OR idempotency_key = ?
                """,
                (attempt_id, attempt_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["contract_id"] != contract_id
                    or existing["attempt_kind"] != kind
                    or existing["backend"] != backend
                    or existing["model"] != model
                    or existing["input_digest"] != input_digest
                ):
                    raise IdempotencyConflictError(
                        "attempt ID/idempotency key was reused with different inputs"
                    )
                return existing["id"]
            connection.execute(
                """
                INSERT INTO contract_attempts (
                    id, contract_id, admission_id, worker_fingerprint_id,
                    attempt_number, attempt_kind, output_only, backend, model,
                    input_digest, state, idempotency_key, started_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?
                )
                """,
                (
                    attempt_id,
                    contract_id,
                    contract["admission_id"],
                    contract["worker_fingerprint_id"],
                    attempt_number,
                    kind,
                    output_only,
                    backend,
                    model,
                    input_digest,
                    attempt_key,
                    utc_now(),
                ),
            )
        return attempt_id

    def record_activation_result(
        self,
        *,
        attempt_id: str,
        result_kind: str,
        output_digest: str,
        evidence_digest: str,
        payload: Mapping[str, Any],
        disposition: ResultDisposition | str | None = None,
        accepted: bool | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        attempt_id = require_identifier(attempt_id, "attempt_id")
        result_kind = require_identifier(result_kind, "result_kind")
        output_digest = require_sha256(output_digest, "output_digest")
        evidence_digest = require_sha256(evidence_digest, "evidence_digest")
        payload_json = compact_json(payload)
        if disposition is None:
            if accepted is None:
                raise ValueError("accepted or disposition is required")
            accepted = require_boolean(accepted, "accepted")
            disposition_value = (
                ResultDisposition.ACCEPTED.value
                if accepted
                else ResultDisposition.REJECTED.value
            )
        else:
            disposition_value = _enum_text(
                disposition, ResultDisposition, "disposition"
            )
            if accepted is not None:
                accepted = require_boolean(accepted, "accepted")
                if accepted != (
                    disposition_value == ResultDisposition.ACCEPTED.value
                ):
                    raise ValueError("accepted conflicts with disposition")
        result_id = stable_identifier("activation-result", attempt_id)
        result_key = require_nonempty(
            idempotency_key or f"activation-result:{attempt_id}",
            "idempotency_key",
        )
        with self.transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM contract_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            existing = connection.execute(
                "SELECT * FROM activation_results WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["disposition"] != disposition_value
                    or existing["result_kind"] != result_kind
                    or existing["output_digest"] != output_digest
                    or existing["evidence_digest"] != evidence_digest
                    or existing["payload_json"] != payload_json
                ):
                    raise IdempotencyConflictError(
                        "attempt result was replayed with different evidence"
                    )
                return existing["id"]
            connection.execute(
                """
                INSERT INTO activation_results (
                    id, contract_id, attempt_id, disposition, result_kind,
                    output_digest, evidence_digest, payload_json,
                    idempotency_key, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id,
                    attempt["contract_id"],
                    attempt_id,
                    disposition_value,
                    result_kind,
                    output_digest,
                    evidence_digest,
                    payload_json,
                    result_key,
                    utc_now(),
                ),
            )
        return result_id

    def record_format_invalid_result_and_violation(
        self,
        *,
        activation_id: str,
        attempt_id: str,
        result_kind: str,
        output_digest: str,
        evidence_digest: str,
        payload: Mapping[str, Any],
        violation_evidence_digest: str,
        violation_details: Mapping[str, Any],
        violation_idempotency_key: str,
        result_idempotency_key: str | None = None,
        quarantine_messages: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[str, str, str, tuple[str, ...], tuple[str, ...]]:
        """Atomically persist one malformed backend result and its violation.

        The result is deliberately inserted first so the database repair gate
        can prove that a FORMAT_REPAIR attempt follows durable FORMAT_INVALID
        evidence.  A second format violation in the same worker/goal/run scope
        opens the existing circuit in this same transaction.
        """

        activation_id = require_identifier(activation_id, "activation_id")
        attempt_id = require_identifier(attempt_id, "attempt_id")
        result_kind = require_identifier(result_kind, "result_kind")
        output_digest = require_sha256(output_digest, "output_digest")
        evidence_digest = require_sha256(evidence_digest, "evidence_digest")
        violation_evidence_digest = require_sha256(
            violation_evidence_digest, "violation_evidence_digest"
        )
        payload_json = compact_json(payload)
        details_json = compact_json(violation_details)
        result_id = stable_identifier("activation-result", attempt_id)
        result_key = require_nonempty(
            result_idempotency_key or f"activation-result:{attempt_id}:format-invalid",
            "result_idempotency_key",
        )
        violation_key = require_nonempty(
            violation_idempotency_key, "violation_idempotency_key"
        )
        violation_id = stable_identifier("violation", violation_key)
        normalized_messages = _normalize_outgoing_messages(
            quarantine_messages,
            transaction_key=result_key,
        )

        with self.transaction(immediate=True) as connection:
            scope = connection.execute(
                """
                SELECT contract_attempts.contract_id,
                       activation_contracts.goal_id,
                       activation_contracts.run_id,
                       activation_contracts.worker_fingerprint_id,
                       activation_contracts.subject_oid,
                       activation_contracts.state AS contract_state,
                       repository_registrations.target_id AS repository_target_id,
                       activations.target_id AS activation_target_id,
                       activations.goal_id AS activation_goal_id,
                       activations.run_id AS activation_run_id,
                       activations.subject_oid AS activation_subject_oid,
                       activations.state AS activation_state,
                       activations.result_json AS activation_result_json
                FROM contract_attempts
                JOIN activation_contracts
                  ON activation_contracts.id = contract_attempts.contract_id
                JOIN repository_registrations
                  ON repository_registrations.id = activation_contracts.repository_id
                JOIN activations ON activations.id = ?
                WHERE contract_attempts.id = ?
                """,
                (activation_id, attempt_id),
            ).fetchone()
            if scope is None:
                raise KeyError(attempt_id)
            if (
                scope["activation_target_id"] != scope["repository_target_id"]
                or scope["activation_goal_id"] != scope["goal_id"]
                or scope["activation_run_id"] != scope["run_id"]
                or scope["activation_subject_oid"] != scope["subject_oid"]
            ):
                raise IntentStateError(
                    "format-invalid result activation scope is inconsistent"
                )

            existing_result = connection.execute(
                "SELECT * FROM activation_results WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing_result is not None and (
                existing_result["disposition"] != ResultDisposition.FORMAT_INVALID.value
                or existing_result["result_kind"] != result_kind
                or existing_result["output_digest"] != output_digest
                or existing_result["evidence_digest"] != evidence_digest
                or existing_result["payload_json"] != payload_json
                or existing_result["idempotency_key"] != result_key
            ):
                raise IdempotencyConflictError(
                    "format-invalid result was replayed with different evidence"
                )

            existing_violation = connection.execute(
                """
                SELECT * FROM contract_violations
                WHERE id = ? OR idempotency_key = ?
                """,
                (violation_id, violation_key),
            ).fetchone()
            if existing_violation is not None and (
                existing_violation["contract_id"] != scope["contract_id"]
                or existing_violation["attempt_id"] != attempt_id
                or existing_violation["violation_code"]
                != ContractViolationCode.FORMAT.value
                or existing_violation["evidence_digest"]
                != violation_evidence_digest
                or existing_violation["details_json"] != details_json
            ):
                raise IdempotencyConflictError(
                    "format violation was replayed with different evidence"
                )
            if existing_violation is not None and existing_result is None:
                raise IntentStateError(
                    "format violation exists without its FORMAT_INVALID result"
                )
            if existing_result is not None and existing_violation is not None:
                disposition = existing_violation["disposition"]
                if disposition == ViolationDisposition.CIRCUIT_OPEN.value:
                    if (
                        scope["contract_state"] != ContractState.QUARANTINED.value
                        or scope["activation_state"] != "QUARANTINED"
                        or scope["activation_result_json"] != payload_json
                    ):
                        raise IntentStateError(
                            "format circuit replay is missing terminal state"
                        )
                    message_ids, outbox_ids = _persist_or_verify_outgoing_messages(
                        connection,
                        normalized_messages,
                        occurred_at=utc_now(),
                        replay=True,
                    )
                else:
                    if (
                        scope["contract_state"] != ContractState.RUNNING.value
                        or scope["activation_state"] != "RUNNING"
                        or scope["activation_result_json"] is not None
                        or normalized_messages
                    ):
                        raise IntentStateError(
                            "format repair replay changed its active activation"
                        )
                    message_ids, outbox_ids = (), ()
                return (
                    existing_result["id"],
                    existing_violation["id"],
                    disposition,
                    message_ids,
                    outbox_ids,
                )

            if (
                scope["contract_state"] != ContractState.RUNNING.value
                or scope["activation_state"] != "RUNNING"
                or scope["activation_result_json"] is not None
            ):
                raise IntentStateError(
                    "format-invalid result requires one active running activation"
                )

            prior_format_count = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM contract_violations
                WHERE worker_fingerprint_id = ?
                  AND goal_id = ?
                  AND run_id = ?
                  AND violation_code = 'FORMAT'
                """,
                (
                    scope["worker_fingerprint_id"],
                    scope["goal_id"],
                    scope["run_id"],
                ),
            ).fetchone()["count"]
            disposition = (
                ViolationDisposition.FORMAT_REPAIR.value
                if prior_format_count == 0
                else ViolationDisposition.CIRCUIT_OPEN.value
            )
            occurred_at = utc_now()
            if existing_result is None:
                connection.execute(
                    """
                    INSERT INTO activation_results (
                        id, contract_id, attempt_id, disposition, result_kind,
                        output_digest, evidence_digest, payload_json,
                        idempotency_key, recorded_at
                    ) VALUES (?, ?, ?, 'FORMAT_INVALID', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result_id,
                        scope["contract_id"],
                        attempt_id,
                        result_kind,
                        output_digest,
                        evidence_digest,
                        payload_json,
                        result_key,
                        occurred_at,
                    ),
                )
            connection.execute(
                """
                INSERT INTO contract_violations (
                    id, contract_id, attempt_id, goal_id, run_id,
                    worker_fingerprint_id, violation_code, disposition,
                    evidence_digest, details_json, idempotency_key, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'FORMAT', ?, ?, ?, ?, ?)
                """,
                (
                    violation_id,
                    scope["contract_id"],
                    attempt_id,
                    scope["goal_id"],
                    scope["run_id"],
                    scope["worker_fingerprint_id"],
                    disposition,
                    violation_evidence_digest,
                    details_json,
                    violation_key,
                    occurred_at,
                ),
            )
            if disposition == ViolationDisposition.CIRCUIT_OPEN.value:
                message_ids, outbox_ids = _persist_or_verify_outgoing_messages(
                    connection,
                    normalized_messages,
                    occurred_at=occurred_at,
                    replay=False,
                )
                activation_cas = connection.execute(
                    """
                    UPDATE activations
                    SET result_json = ?, state = 'QUARANTINED', updated_at = ?
                    WHERE id = ? AND state = 'RUNNING' AND result_json IS NULL
                    """,
                    (payload_json, occurred_at, activation_id),
                )
                if activation_cas.rowcount != 1:
                    raise IntentStateError(
                        "format circuit did not quarantine exactly one activation"
                    )
            else:
                if normalized_messages:
                    raise ValueError(
                        "first format repair cannot enqueue quarantine messages"
                    )
                message_ids, outbox_ids = (), ()
        return result_id, violation_id, disposition, message_ids, outbox_ids

    def record_contract_violation(
        self,
        *,
        contract_id: str,
        violation_code: ContractViolationCode | str,
        evidence_digest: str,
        details: Mapping[str, Any],
        attempt_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        contract_id = require_identifier(contract_id, "contract_id")
        if attempt_id is not None:
            attempt_id = require_identifier(attempt_id, "attempt_id")
        code = _enum_text(
            violation_code, ContractViolationCode, "violation_code"
        )
        evidence_digest = require_sha256(evidence_digest, "evidence_digest")
        details_json = compact_json(details)
        violation_key = require_nonempty(
            idempotency_key
            or f"violation:{contract_id}:{code}:{attempt_id or 'admission'}",
            "idempotency_key",
        )
        violation_id = stable_identifier("violation", violation_key)

        with self.transaction(immediate=True) as connection:
            contract = connection.execute(
                "SELECT * FROM activation_contracts WHERE id = ?",
                (contract_id,),
            ).fetchone()
            if contract is None:
                raise KeyError(contract_id)
            existing = connection.execute(
                """
                SELECT * FROM contract_violations
                WHERE id = ? OR idempotency_key = ?
                """,
                (violation_id, violation_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["contract_id"] != contract_id
                    or existing["attempt_id"] != attempt_id
                    or existing["violation_code"] != code
                    or existing["evidence_digest"] != evidence_digest
                    or existing["details_json"] != details_json
                ):
                    raise IdempotencyConflictError(
                        "violation idempotency key was reused with different evidence"
                    )
                return existing["id"]
            if code == ContractViolationCode.FORMAT.value:
                prior_format = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM contract_violations
                    WHERE worker_fingerprint_id = ?
                      AND goal_id = ?
                      AND run_id = ?
                      AND violation_code = 'FORMAT'
                    """,
                    (
                        contract["worker_fingerprint_id"],
                        contract["goal_id"],
                        contract["run_id"],
                    ),
                ).fetchone()["count"]
                disposition = (
                    ViolationDisposition.FORMAT_REPAIR.value
                    if prior_format == 0
                    else ViolationDisposition.CIRCUIT_OPEN.value
                )
            elif code in {
                ContractViolationCode.AUTHORITY.value,
                ContractViolationCode.OID.value,
                ContractViolationCode.WRITE_ROOT.value,
                ContractViolationCode.NESTED_SPAWN.value,
            }:
                disposition = ViolationDisposition.QUARANTINED.value
            else:
                disposition = ViolationDisposition.REJECTED.value
            connection.execute(
                """
                INSERT INTO contract_violations (
                    id, contract_id, attempt_id, goal_id, run_id,
                    worker_fingerprint_id, violation_code, disposition,
                    evidence_digest, details_json, idempotency_key, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    violation_id,
                    contract_id,
                    attempt_id,
                    contract["goal_id"],
                    contract["run_id"],
                    contract["worker_fingerprint_id"],
                    code,
                    disposition,
                    evidence_digest,
                    details_json,
                    violation_key,
                    utc_now(),
                ),
            )
        return violation_id

    def get_open_circuit(
        self,
        *,
        worker_fingerprint_id: str,
        goal_id: str,
        run_id: str,
    ) -> Mapping[str, Any] | None:
        worker_fingerprint_id = require_identifier(
            worker_fingerprint_id, "worker_fingerprint_id"
        )
        goal_id = require_identifier(goal_id, "goal_id")
        run_id = require_identifier(run_id, "run_id")
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM worker_circuit_breakers
                WHERE worker_fingerprint_id = ?
                  AND goal_id = ?
                  AND run_id = ?
                  AND state = 'OPEN'
                """,
                (worker_fingerprint_id, goal_id, run_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_token_ledger_entry(
        self,
        *,
        contract_id: str,
        entry_kind: TokenLedgerEntryKind | str,
        input_tokens: int,
        output_tokens: int,
        model_calls: int,
        attempt_id: str | None = None,
        idempotency_key: str,
    ) -> str:
        contract_id = require_identifier(contract_id, "contract_id")
        if attempt_id is not None:
            attempt_id = require_identifier(attempt_id, "attempt_id")
        kind = _enum_text(entry_kind, TokenLedgerEntryKind, "entry_kind")
        input_tokens = require_nonnegative(input_tokens, "input_tokens")
        output_tokens = require_nonnegative(output_tokens, "output_tokens")
        if (
            not isinstance(model_calls, int)
            or isinstance(model_calls, bool)
            or model_calls not in (0, 1)
        ):
            raise ValueError("model_calls must be 0 or 1")
        idempotency_key = require_nonempty(idempotency_key, "idempotency_key")
        entry_id = stable_identifier("token-entry", idempotency_key)
        with self.transaction(immediate=True) as connection:
            scope = connection.execute(
                """
                SELECT activation_contracts.worker_fingerprint_id,
                       contract_admissions.id AS admission_id
                FROM activation_contracts
                JOIN contract_admissions
                  ON contract_admissions.contract_id = activation_contracts.id
                WHERE activation_contracts.id = ?
                """,
                (contract_id,),
            ).fetchone()
            if scope is None:
                raise KeyError(contract_id)
            existing = connection.execute(
                "SELECT * FROM token_ledger_entries WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["contract_id"] != contract_id
                    or existing["attempt_id"] != attempt_id
                    or existing["entry_kind"] != kind
                    or existing["input_tokens"] != input_tokens
                    or existing["output_tokens"] != output_tokens
                    or existing["model_calls"] != model_calls
                ):
                    raise IdempotencyConflictError(
                        "token ledger idempotency key was reused"
                    )
                return existing["id"]
            connection.execute(
                """
                INSERT INTO token_ledger_entries (
                    id, contract_id, admission_id, attempt_id,
                    worker_fingerprint_id, entry_kind, input_tokens,
                    output_tokens, model_calls, idempotency_key, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    contract_id,
                    scope["admission_id"],
                    attempt_id,
                    scope["worker_fingerprint_id"],
                    kind,
                    input_tokens,
                    output_tokens,
                    model_calls,
                    idempotency_key,
                    utc_now(),
                ),
            )
        return entry_id

    def commit_activation_result_transaction(
        self,
        *,
        activation_id: str,
        contract_id: str,
        attempt_id: str,
        result_kind: str,
        output_digest: str,
        evidence_digest: str,
        payload: Mapping[str, Any],
        input_tokens: int,
        output_tokens: int,
        model_calls: int,
        mcp_receipts: Sequence[Mapping[str, str]],
        serena_receipts: Sequence[Mapping[str, str]],
        workflow_instance_id: str,
        workflow_transition_id: str,
        from_state: str,
        to_state: str,
        transition_receipt_id: str,
        result_idempotency_key: str,
        token_idempotency_key: str,
        transition_idempotency_key: str,
        outgoing_messages: Sequence[Mapping[str, Any]],
    ) -> DurableActivationCommit:
        """Commit one schema-valid activation outcome as a single v4 fact.

        Trusted MCP receipts must already exist.  Result evidence, Serena
        consumption, token accounting, workflow receipt/CAS, and contract
        finalization either commit together or all roll back.
        """

        identifiers = {
            "activation_id": activation_id,
            "contract_id": contract_id,
            "attempt_id": attempt_id,
            "result_kind": result_kind,
            "workflow_instance_id": workflow_instance_id,
            "workflow_transition_id": workflow_transition_id,
            "from_state": from_state,
            "to_state": to_state,
            "transition_receipt_id": transition_receipt_id,
        }
        normalized = {
            key: require_identifier(value, key)
            for key, value in identifiers.items()
        }
        output_digest = require_sha256(output_digest, "output_digest")
        evidence_digest = require_sha256(evidence_digest, "evidence_digest")
        payload_json = compact_json(payload)
        input_tokens = require_nonnegative(input_tokens, "input_tokens")
        output_tokens = require_nonnegative(output_tokens, "output_tokens")
        if (
            not isinstance(model_calls, int)
            or isinstance(model_calls, bool)
            or model_calls not in (0, 1)
        ):
            raise ValueError("model_calls must be 0 or 1")
        result_key = require_nonempty(
            result_idempotency_key, "result_idempotency_key"
        )
        token_key = require_nonempty(
            token_idempotency_key, "token_idempotency_key"
        )
        transition_key = require_nonempty(
            transition_idempotency_key, "transition_idempotency_key"
        )
        normalized_messages = _normalize_outgoing_messages(
            outgoing_messages,
            transaction_key=result_key,
        )
        if not isinstance(mcp_receipts, Sequence) or isinstance(
            mcp_receipts, (str, bytes)
        ):
            raise ValueError("mcp_receipts must be a sequence")
        if not isinstance(serena_receipts, Sequence) or isinstance(
            serena_receipts, (str, bytes)
        ):
            raise ValueError("serena_receipts must be a sequence")
        normalized_serena: list[tuple[str, str, str, str]] = []
        for item in serena_receipts:
            if not isinstance(item, Mapping):
                raise ValueError("each Serena receipt must be an object")
            memory_binding_id = require_identifier(
                item.get("memory_binding_id"), "memory_binding_id"
            )
            receipt_digest = require_sha256(
                item.get("receipt_digest"), "receipt_digest"
            )
            idempotency_key = require_nonempty(
                item.get("idempotency_key"), "serena idempotency_key"
            )
            receipt_id = stable_identifier(
                "serena-consumption", idempotency_key
            )
            normalized_serena.append(
                (receipt_id, memory_binding_id, receipt_digest, idempotency_key)
            )
        if len({item[1] for item in normalized_serena}) != len(
            normalized_serena
        ):
            raise ValueError("Serena receipt bindings must be unique")

        result_id = stable_identifier("activation-result", normalized["attempt_id"])
        token_entry_id = stable_identifier("token-entry", token_key)
        occurred_at = utc_now()
        with self.transaction(immediate=True) as connection:
            scope = connection.execute(
                """
                SELECT activation_contracts.state AS contract_state,
                       activation_contracts.goal_id AS contract_goal_id,
                       activation_contracts.run_id AS contract_run_id,
                       activation_contracts.subject_oid AS contract_subject_oid,
                       activation_contracts.workflow_instance_id,
                       activation_contracts.workflow_transition_id,
                       activation_contracts.worker_fingerprint_id,
                       contract_attempts.contract_id AS attempt_contract_id,
                       contract_attempts.state AS attempt_state,
                       contract_admissions.id AS admission_id,
                       workflow_instances.current_state_key,
                       workflow_instances.status AS workflow_status,
                       workflow_transitions.from_state_key,
                       workflow_transitions.to_state_key,
                       workflow_transitions.failure_route,
                       activations.target_id AS activation_target_id,
                       activations.goal_id AS activation_goal_id,
                       activations.run_id AS activation_run_id,
                       activations.subject_oid AS activation_subject_oid,
                       activations.state AS activation_state,
                       activations.result_json AS activation_result_json,
                       repository_registrations.target_id AS repository_target_id
                FROM activation_contracts
                JOIN contract_attempts
                  ON contract_attempts.id = ?
                JOIN contract_admissions
                  ON contract_admissions.contract_id = activation_contracts.id
                JOIN workflow_instances
                  ON workflow_instances.id =
                     activation_contracts.workflow_instance_id
                JOIN workflow_transitions
                  ON workflow_transitions.id =
                     activation_contracts.workflow_transition_id
                JOIN repository_registrations
                  ON repository_registrations.id =
                     activation_contracts.repository_id
                JOIN activations ON activations.id = ?
                WHERE activation_contracts.id = ?
                """,
                (
                    normalized["attempt_id"],
                    normalized["activation_id"],
                    normalized["contract_id"],
                ),
            ).fetchone()
            if scope is None:
                raise KeyError(normalized["contract_id"])
            if (
                scope["attempt_contract_id"] != normalized["contract_id"]
                or scope["workflow_instance_id"]
                != normalized["workflow_instance_id"]
                or scope["workflow_transition_id"]
                != normalized["workflow_transition_id"]
                or scope["from_state_key"] != normalized["from_state"]
                or normalized["to_state"]
                not in {scope["to_state_key"], scope["failure_route"]}
                or scope["activation_goal_id"] != scope["contract_goal_id"]
                or scope["activation_run_id"] != scope["contract_run_id"]
                or scope["activation_target_id"] != scope["repository_target_id"]
                or scope["activation_subject_oid"] != scope["contract_subject_oid"]
            ):
                raise IntentStateError(
                    "result route differs from the immutable activation contract"
                )

            self._validate_trusted_mcp_receipt_references(
                connection,
                contract_id=normalized["contract_id"],
                attempt_id=normalized["attempt_id"],
                receipts=mcp_receipts,
            )

            existing_result = connection.execute(
                "SELECT * FROM activation_results WHERE attempt_id = ?",
                (normalized["attempt_id"],),
            ).fetchone()
            existing_token = connection.execute(
                "SELECT * FROM token_ledger_entries WHERE idempotency_key = ?",
                (token_key,),
            ).fetchone()
            existing_transition = connection.execute(
                """
                SELECT * FROM workflow_transition_receipts
                WHERE id = ? OR idempotency_key = ?
                """,
                (normalized["transition_receipt_id"], transition_key),
            ).fetchone()
            if any(
                item is not None
                for item in (existing_result, existing_token, existing_transition)
            ):
                if not all(
                    item is not None
                    for item in (existing_result, existing_token, existing_transition)
                ):
                    raise IntentStateError(
                        "partial legacy result commit requires reconciliation"
                    )
                if (
                    existing_result["id"] != result_id
                    or existing_result["contract_id"] != normalized["contract_id"]
                    or existing_result["disposition"]
                    != ResultDisposition.ACCEPTED.value
                    or existing_result["result_kind"] != normalized["result_kind"]
                    or existing_result["output_digest"] != output_digest
                    or existing_result["evidence_digest"] != evidence_digest
                    or existing_result["payload_json"] != payload_json
                    or existing_result["idempotency_key"] != result_key
                    or existing_token["id"] != token_entry_id
                    or existing_token["contract_id"] != normalized["contract_id"]
                    or existing_token["attempt_id"] != normalized["attempt_id"]
                    or existing_token["entry_kind"]
                    != TokenLedgerEntryKind.CONSUMED.value
                    or existing_token["input_tokens"] != input_tokens
                    or existing_token["output_tokens"] != output_tokens
                    or existing_token["model_calls"] != model_calls
                    or existing_transition["workflow_instance_id"]
                    != normalized["workflow_instance_id"]
                    or existing_transition["workflow_transition_id"]
                    != normalized["workflow_transition_id"]
                    or existing_transition["from_state_key"]
                    != normalized["from_state"]
                    or existing_transition["to_state_key"]
                    != normalized["to_state"]
                    or existing_transition["evidence_digest"] != evidence_digest
                    or existing_transition["idempotency_key"] != transition_key
                    or scope["contract_state"] != ContractState.COMPLETED.value
                    or scope["current_state_key"] != normalized["to_state"]
                    or scope["activation_state"] != "RESULT_PERSISTED"
                    or scope["activation_result_json"] != payload_json
                ):
                    raise IdempotencyConflictError(
                        "activation result transaction was replayed differently"
                    )
                for receipt_id, memory_binding_id, receipt_digest, key in normalized_serena:
                    existing_serena = connection.execute(
                        """
                        SELECT * FROM serena_consumption_receipts
                        WHERE id = ? AND idempotency_key = ?
                        """,
                        (receipt_id, key),
                    ).fetchone()
                    if existing_serena is None or (
                        existing_serena["contract_id"] != normalized["contract_id"]
                        or existing_serena["memory_binding_id"] != memory_binding_id
                        or existing_serena["receipt_digest"] != receipt_digest
                    ):
                        raise IntentStateError(
                            "activation result replay is missing Serena evidence"
                        )
                message_ids, outbox_ids = _persist_or_verify_outgoing_messages(
                    connection,
                    normalized_messages,
                    occurred_at=occurred_at,
                    replay=True,
                )
                return DurableActivationCommit(
                    activation_result_id=existing_result["id"],
                    message_ids=message_ids,
                    outbox_ids=outbox_ids,
                )

            if (
                scope["contract_state"] != ContractState.RUNNING.value
                or scope["attempt_state"]
                not in {
                    ContractAttemptState.CREATED.value,
                    ContractAttemptState.RUNNING.value,
                }
                or scope["workflow_status"] != "ACTIVE"
                or scope["activation_state"] != "RUNNING"
                or scope["activation_result_json"] is not None
            ):
                raise IntentStateError(
                    "activation result transaction requires an active running scope"
                )

            for receipt_id, memory_binding_id, receipt_digest, key in normalized_serena:
                binding = connection.execute(
                    """
                    SELECT 1
                    FROM contract_serena_memory_bindings
                    WHERE id = ? AND contract_id = ?
                    """,
                    (memory_binding_id, normalized["contract_id"]),
                ).fetchone()
                if binding is None:
                    raise IntentStateError(
                        "Serena consumption receipt has no contract binding"
                    )
                existing = connection.execute(
                    """
                    SELECT * FROM serena_consumption_receipts
                    WHERE id = ? OR idempotency_key = ?
                    """,
                    (receipt_id, key),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["contract_id"] != normalized["contract_id"]
                        or existing["memory_binding_id"] != memory_binding_id
                        or existing["receipt_digest"] != receipt_digest
                    ):
                        raise IdempotencyConflictError(
                            "Serena consumption key was reused"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO serena_consumption_receipts (
                        id, contract_id, memory_binding_id,
                        worker_fingerprint_id, receipt_digest,
                        idempotency_key, consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        normalized["contract_id"],
                        memory_binding_id,
                        scope["worker_fingerprint_id"],
                        receipt_digest,
                        key,
                        occurred_at,
                    ),
                )

            connection.execute(
                """
                INSERT INTO activation_results (
                    id, contract_id, attempt_id, disposition, result_kind,
                    output_digest, evidence_digest, payload_json,
                    idempotency_key, recorded_at
                ) VALUES (?, ?, ?, 'ACCEPTED', ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id,
                    normalized["contract_id"],
                    normalized["attempt_id"],
                    normalized["result_kind"],
                    output_digest,
                    evidence_digest,
                    payload_json,
                    result_key,
                    occurred_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO token_ledger_entries (
                    id, contract_id, admission_id, attempt_id,
                    worker_fingerprint_id, entry_kind, input_tokens,
                    output_tokens, model_calls, idempotency_key, occurred_at
                ) VALUES (?, ?, ?, ?, ?, 'CONSUMED', ?, ?, ?, ?, ?)
                """,
                (
                    token_entry_id,
                    normalized["contract_id"],
                    scope["admission_id"],
                    normalized["attempt_id"],
                    scope["worker_fingerprint_id"],
                    input_tokens,
                    output_tokens,
                    model_calls,
                    token_key,
                    occurred_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO workflow_transition_receipts (
                    id, workflow_instance_id, workflow_transition_id,
                    from_state_key, to_state_key, evidence_digest,
                    idempotency_key, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["transition_receipt_id"],
                    normalized["workflow_instance_id"],
                    normalized["workflow_transition_id"],
                    normalized["from_state"],
                    normalized["to_state"],
                    evidence_digest,
                    transition_key,
                    occurred_at,
                ),
            )
            message_ids, outbox_ids = _persist_or_verify_outgoing_messages(
                connection,
                normalized_messages,
                occurred_at=occurred_at,
                replay=False,
            )
            workflow_cas = connection.execute(
                """
                UPDATE workflow_instances
                SET current_state_key = ?, updated_at = ?
                WHERE id = ? AND current_state_key = ? AND status = 'ACTIVE'
                """,
                (
                    normalized["to_state"],
                    occurred_at,
                    normalized["workflow_instance_id"],
                    normalized["from_state"],
                ),
            )
            if workflow_cas.rowcount != 1:
                raise IntentStateError(
                    "workflow state CAS did not update exactly one active row"
                )
            contract_cas = connection.execute(
                """
                UPDATE activation_contracts
                SET state = 'COMPLETED', updated_at = ?, completed_at = ?
                WHERE id = ? AND state = 'RESULT_RECORDED'
                """,
                (
                    occurred_at,
                    occurred_at,
                    normalized["contract_id"],
                ),
            )
            if contract_cas.rowcount != 1:
                raise IntentStateError(
                    "contract finalization CAS did not update exactly one row"
                )
            activation_cas = connection.execute(
                """
                UPDATE activations
                SET result_json = ?, state = 'RESULT_PERSISTED', updated_at = ?
                WHERE id = ? AND state = 'RUNNING' AND result_json IS NULL
                """,
                (payload_json, occurred_at, normalized["activation_id"]),
            )
            if activation_cas.rowcount != 1:
                raise IntentStateError(
                    "activation result CAS did not update exactly one running row"
                )
        return DurableActivationCommit(
            activation_result_id=result_id,
            message_ids=message_ids,
            outbox_ids=outbox_ids,
        )

    def commit_invalid_activation_result_transaction(
        self,
        *,
        activation_id: str,
        contract_id: str,
        attempt_id: str,
        disposition: ResultDisposition | str,
        result_kind: str,
        output_digest: str,
        evidence_digest: str,
        payload: Mapping[str, Any],
        violation_code: ContractViolationCode | str,
        violation_evidence_digest: str,
        violation_details: Mapping[str, Any],
        violation_idempotency_key: str,
        workflow_instance_id: str,
        workflow_transition_id: str,
        from_state: str,
        failure_state: str | None,
        transition_receipt_id: str | None,
        result_idempotency_key: str,
        transition_idempotency_key: str | None,
        outgoing_messages: Sequence[Mapping[str, Any]],
    ) -> DurableActivationCommit:
        """Atomically persist rejected/quarantined evidence and its PL route."""

        activation_id = require_identifier(activation_id, "activation_id")
        contract_id = require_identifier(contract_id, "contract_id")
        attempt_id = require_identifier(attempt_id, "attempt_id")
        result_kind = require_identifier(result_kind, "result_kind")
        workflow_instance_id = require_identifier(
            workflow_instance_id, "workflow_instance_id"
        )
        workflow_transition_id = require_identifier(
            workflow_transition_id, "workflow_transition_id"
        )
        from_state = require_identifier(from_state, "from_state")
        disposition_value = _enum_text(
            disposition, ResultDisposition, "disposition"
        )
        if disposition_value not in {
            ResultDisposition.REJECTED.value,
            ResultDisposition.QUARANTINED.value,
        }:
            raise ValueError("invalid result disposition must be REJECTED or QUARANTINED")
        is_rework = disposition_value == ResultDisposition.REJECTED.value
        if is_rework:
            failure_state = require_identifier(failure_state, "failure_state")
            transition_receipt_id = require_identifier(
                transition_receipt_id, "transition_receipt_id"
            )
            transition_idempotency_key = require_nonempty(
                transition_idempotency_key, "transition_idempotency_key"
            )
        elif any(
            value is not None
            for value in (
                failure_state,
                transition_receipt_id,
                transition_idempotency_key,
            )
        ):
            raise ValueError("quarantine does not write a workflow transition receipt")
        output_digest = require_sha256(output_digest, "output_digest")
        evidence_digest = require_sha256(evidence_digest, "evidence_digest")
        violation_evidence_digest = require_sha256(
            violation_evidence_digest, "violation_evidence_digest"
        )
        violation_code_value = _enum_text(
            violation_code, ContractViolationCode, "violation_code"
        )
        result_key = require_nonempty(
            result_idempotency_key, "result_idempotency_key"
        )
        violation_key = require_nonempty(
            violation_idempotency_key, "violation_idempotency_key"
        )
        payload_json = compact_json(payload)
        details_json = compact_json(violation_details)
        result_id = stable_identifier("activation-result", attempt_id)
        violation_id = stable_identifier("violation", violation_key)
        expected_violation_disposition = (
            ViolationDisposition.QUARANTINED.value
            if disposition_value == ResultDisposition.QUARANTINED.value
            else ViolationDisposition.REJECTED.value
        )
        normalized_messages = _normalize_outgoing_messages(
            outgoing_messages,
            transaction_key=result_key,
        )
        occurred_at = utc_now()

        with self.transaction(immediate=True) as connection:
            scope = connection.execute(
                """
                SELECT activation_contracts.state AS contract_state,
                       activation_contracts.goal_id AS contract_goal_id,
                       activation_contracts.run_id AS contract_run_id,
                       activation_contracts.subject_oid AS contract_subject_oid,
                       activation_contracts.workflow_instance_id,
                       activation_contracts.workflow_transition_id,
                       activation_contracts.worker_fingerprint_id,
                       contract_attempts.contract_id AS attempt_contract_id,
                       contract_attempts.state AS attempt_state,
                       workflow_instances.current_state_key,
                       workflow_instances.status AS workflow_status,
                       workflow_transitions.from_state_key,
                       workflow_transitions.failure_route,
                       repository_registrations.target_id AS repository_target_id,
                       activations.target_id AS activation_target_id,
                       activations.goal_id AS activation_goal_id,
                       activations.run_id AS activation_run_id,
                       activations.subject_oid AS activation_subject_oid,
                       activations.state AS activation_state,
                       activations.result_json AS activation_result_json
                FROM activation_contracts
                JOIN contract_attempts ON contract_attempts.id = ?
                JOIN workflow_instances
                  ON workflow_instances.id = activation_contracts.workflow_instance_id
                JOIN workflow_transitions
                  ON workflow_transitions.id = activation_contracts.workflow_transition_id
                JOIN repository_registrations
                  ON repository_registrations.id = activation_contracts.repository_id
                JOIN activations ON activations.id = ?
                WHERE activation_contracts.id = ?
                """,
                (attempt_id, activation_id, contract_id),
            ).fetchone()
            if scope is None:
                raise KeyError(contract_id)
            if (
                scope["attempt_contract_id"] != contract_id
                or scope["workflow_instance_id"] != workflow_instance_id
                or scope["workflow_transition_id"] != workflow_transition_id
                or scope["from_state_key"] != from_state
                or (is_rework and scope["failure_route"] != failure_state)
                or scope["activation_goal_id"] != scope["contract_goal_id"]
                or scope["activation_run_id"] != scope["contract_run_id"]
                or scope["activation_target_id"] != scope["repository_target_id"]
                or scope["activation_subject_oid"] != scope["contract_subject_oid"]
            ):
                raise IntentStateError(
                    "invalid result route differs from the immutable activation scope"
                )

            existing_result = connection.execute(
                "SELECT * FROM activation_results WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            existing_violation = connection.execute(
                """
                SELECT * FROM contract_violations
                WHERE id = ? OR idempotency_key = ?
                """,
                (violation_id, violation_key),
            ).fetchone()
            existing_transition = None
            if is_rework:
                existing_transition = connection.execute(
                    """
                    SELECT * FROM workflow_transition_receipts
                    WHERE id = ? OR idempotency_key = ?
                    """,
                    (transition_receipt_id, transition_idempotency_key),
                ).fetchone()
            primary = (existing_result, existing_violation)
            replay = any(item is not None for item in primary) or (
                is_rework and existing_transition is not None
            )
            if replay:
                if not all(item is not None for item in primary) or (
                    is_rework and existing_transition is None
                ):
                    raise IntentStateError(
                        "partial invalid result commit requires reconciliation"
                    )
                expected_contract_state = (
                    ContractState.COMPLETED.value
                    if is_rework
                    else ContractState.QUARANTINED.value
                )
                expected_activation_state = (
                    "RESULT_PERSISTED" if is_rework else "QUARANTINED"
                )
                if (
                    existing_result["id"] != result_id
                    or existing_result["contract_id"] != contract_id
                    or existing_result["disposition"] != disposition_value
                    or existing_result["result_kind"] != result_kind
                    or existing_result["output_digest"] != output_digest
                    or existing_result["evidence_digest"] != evidence_digest
                    or existing_result["payload_json"] != payload_json
                    or existing_result["idempotency_key"] != result_key
                    or existing_violation["contract_id"] != contract_id
                    or existing_violation["attempt_id"] != attempt_id
                    or existing_violation["violation_code"] != violation_code_value
                    or existing_violation["disposition"]
                    != expected_violation_disposition
                    or existing_violation["evidence_digest"]
                    != violation_evidence_digest
                    or existing_violation["details_json"] != details_json
                    or scope["contract_state"] != expected_contract_state
                    or scope["activation_state"] != expected_activation_state
                    or scope["activation_result_json"] != payload_json
                    or (
                        is_rework
                        and (
                            existing_transition["workflow_instance_id"]
                            != workflow_instance_id
                            or existing_transition["workflow_transition_id"]
                            != workflow_transition_id
                            or existing_transition["from_state_key"] != from_state
                            or existing_transition["to_state_key"] != failure_state
                            or existing_transition["evidence_digest"] != evidence_digest
                            or existing_transition["idempotency_key"]
                            != transition_idempotency_key
                            or scope["current_state_key"] != failure_state
                        )
                    )
                ):
                    raise IdempotencyConflictError(
                        "invalid activation result was replayed differently"
                    )
                message_ids, outbox_ids = _persist_or_verify_outgoing_messages(
                    connection,
                    normalized_messages,
                    occurred_at=occurred_at,
                    replay=True,
                )
                return DurableActivationCommit(
                    activation_result_id=result_id,
                    message_ids=message_ids,
                    outbox_ids=outbox_ids,
                )

            if (
                scope["contract_state"] != ContractState.RUNNING.value
                or scope["attempt_state"]
                not in {
                    ContractAttemptState.CREATED.value,
                    ContractAttemptState.RUNNING.value,
                }
                or scope["workflow_status"] != "ACTIVE"
                or scope["current_state_key"] != from_state
                or scope["activation_state"] != "RUNNING"
                or scope["activation_result_json"] is not None
            ):
                raise IntentStateError(
                    "invalid result transaction requires an active running scope"
                )

            connection.execute(
                """
                INSERT INTO activation_results (
                    id, contract_id, attempt_id, disposition, result_kind,
                    output_digest, evidence_digest, payload_json,
                    idempotency_key, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id, contract_id, attempt_id, disposition_value,
                    result_kind, output_digest, evidence_digest, payload_json,
                    result_key, occurred_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO contract_violations (
                    id, contract_id, attempt_id, goal_id, run_id,
                    worker_fingerprint_id, violation_code, disposition,
                    evidence_digest, details_json, idempotency_key, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    violation_id, contract_id, attempt_id,
                    scope["contract_goal_id"], scope["contract_run_id"],
                    scope["worker_fingerprint_id"], violation_code_value,
                    expected_violation_disposition, violation_evidence_digest,
                    details_json, violation_key, occurred_at,
                ),
            )
            if is_rework:
                connection.execute(
                    """
                    INSERT INTO workflow_transition_receipts (
                        id, workflow_instance_id, workflow_transition_id,
                        from_state_key, to_state_key, evidence_digest,
                        idempotency_key, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transition_receipt_id, workflow_instance_id,
                        workflow_transition_id, from_state, failure_state,
                        evidence_digest, transition_idempotency_key, occurred_at,
                    ),
                )
            message_ids, outbox_ids = _persist_or_verify_outgoing_messages(
                connection,
                normalized_messages,
                occurred_at=occurred_at,
                replay=False,
            )
            if is_rework:
                workflow_cas = connection.execute(
                    """
                    UPDATE workflow_instances
                    SET current_state_key = ?, updated_at = ?
                    WHERE id = ? AND current_state_key = ? AND status = 'ACTIVE'
                    """,
                    (failure_state, occurred_at, workflow_instance_id, from_state),
                )
                if workflow_cas.rowcount != 1:
                    raise IntentStateError(
                        "invalid result workflow CAS did not update exactly one row"
                    )
                recorded_cas = connection.execute(
                    """
                    UPDATE activation_contracts
                    SET state = 'RESULT_RECORDED', updated_at = ?
                    WHERE id = ? AND state = 'RUNNING'
                    """,
                    (occurred_at, contract_id),
                )
                if recorded_cas.rowcount != 1:
                    raise IntentStateError(
                        "rejected result did not record exactly one contract result"
                    )
                contract_cas = connection.execute(
                    """
                    UPDATE activation_contracts
                    SET state = 'COMPLETED', updated_at = ?, completed_at = ?
                    WHERE id = ? AND state = 'RESULT_RECORDED'
                    """,
                    (occurred_at, occurred_at, contract_id),
                )
                if contract_cas.rowcount != 1:
                    raise IntentStateError(
                        "rejected result did not finalize exactly one contract"
                    )
                activation_state = "RESULT_PERSISTED"
            else:
                quarantine_cas = connection.execute(
                    """
                    UPDATE activation_contracts
                    SET updated_at = ?, completed_at = ?
                    WHERE id = ? AND state = 'QUARANTINED'
                    """,
                    (occurred_at, occurred_at, contract_id),
                )
                if quarantine_cas.rowcount != 1:
                    raise IntentStateError(
                        "quarantine did not terminalize exactly one contract"
                    )
                activation_state = "QUARANTINED"
            activation_cas = connection.execute(
                """
                UPDATE activations
                SET result_json = ?, state = ?, updated_at = ?
                WHERE id = ? AND state = 'RUNNING' AND result_json IS NULL
                """,
                (payload_json, activation_state, occurred_at, activation_id),
            )
            if activation_cas.rowcount != 1:
                raise IntentStateError(
                    "invalid result activation CAS did not update exactly one row"
                )
        return DurableActivationCommit(
            activation_result_id=result_id,
            message_ids=message_ids,
            outbox_ids=outbox_ids,
        )

    def register_mcp_definition(
        self,
        *,
        server_name: str,
        tool_name: str,
        version: str,
        sha256: str,
    ) -> str:
        server_name = require_identifier(server_name, "server_name")
        tool_name = require_identifier(tool_name, "tool_name")
        version = require_nonempty(version, "version")
        digest = require_sha256(sha256, "sha256")
        definition_id = stable_identifier(
            "mcp-definition", server_name, tool_name, version
        )
        key = f"mcp-definition:{server_name}:{tool_name}:{version}"
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM mcp_definitions
                WHERE server_name = ? AND tool_name = ? AND version = ?
                """,
                (server_name, tool_name, version),
            ).fetchone()
            if row is not None:
                if row["sha256"] != digest:
                    raise IdempotencyConflictError(
                        "MCP definition version has different bytes"
                    )
                return row["id"]
            connection.execute(
                """
                INSERT INTO mcp_definitions (
                    id, server_name, tool_name, version, sha256, state,
                    idempotency_key, registered_at
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (
                    definition_id,
                    server_name,
                    tool_name,
                    version,
                    digest,
                    key,
                    utc_now(),
                ),
            )
        return definition_id

    def bind_contract_mcp(
        self,
        *,
        contract_id: str,
        mcp_definition_id: str,
        required_availability: bool,
        invocation_required: bool,
        trigger_rule: str,
    ) -> str:
        contract_id = require_identifier(contract_id, "contract_id")
        mcp_definition_id = require_identifier(
            mcp_definition_id, "mcp_definition_id"
        )
        required_availability = require_boolean(
            required_availability, "required_availability"
        )
        invocation_required = require_boolean(
            invocation_required, "invocation_required"
        )
        trigger_rule = require_nonempty(trigger_rule, "trigger_rule")
        binding_id = stable_identifier(
            "mcp-binding", contract_id, mcp_definition_id
        )
        key = f"mcp-binding:{contract_id}:{mcp_definition_id}"
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM contract_mcp_bindings
                WHERE contract_id = ? AND mcp_definition_id = ?
                """,
                (contract_id, mcp_definition_id),
            ).fetchone()
            if row is not None:
                signature = (
                    row["required_availability"],
                    row["invocation_required"],
                    row["trigger_rule"],
                )
                expected = (
                    int(required_availability),
                    int(invocation_required),
                    trigger_rule,
                )
                if signature != expected:
                    raise IdempotencyConflictError(
                        "contract MCP binding was replayed differently"
                    )
                return row["id"]
            connection.execute(
                """
                INSERT INTO contract_mcp_bindings (
                    id, contract_id, mcp_definition_id,
                    required_availability, invocation_required, trigger_rule,
                    idempotency_key, bound_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_id,
                    contract_id,
                    mcp_definition_id,
                    int(required_availability),
                    int(invocation_required),
                    trigger_rule,
                    key,
                    utc_now(),
                ),
            )
        return binding_id

    def record_mcp_health_observation(
        self,
        *,
        mcp_definition_id: str,
        status: McpHealthStatus | str,
        evidence_digest: str,
        idempotency_key: str,
        contract_id: str | None = None,
    ) -> str:
        mcp_definition_id = require_identifier(
            mcp_definition_id, "mcp_definition_id"
        )
        if contract_id is not None:
            contract_id = require_identifier(contract_id, "contract_id")
        status_value = _enum_text(status, McpHealthStatus, "status")
        evidence_digest = require_sha256(evidence_digest, "evidence_digest")
        idempotency_key = require_nonempty(idempotency_key, "idempotency_key")
        observation_id = stable_identifier("mcp-health", idempotency_key)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM mcp_health_observations
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                if (
                    row["mcp_definition_id"] != mcp_definition_id
                    or row["contract_id"] != contract_id
                    or row["status"] != status_value
                    or row["evidence_digest"] != evidence_digest
                ):
                    raise IdempotencyConflictError(
                        "MCP health observation key was reused"
                    )
                return row["id"]
            connection.execute(
                """
                INSERT INTO mcp_health_observations (
                    id, mcp_definition_id, contract_id, status,
                    evidence_digest, idempotency_key, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    mcp_definition_id,
                    contract_id,
                    status_value,
                    evidence_digest,
                    idempotency_key,
                    utc_now(),
                ),
            )
        return observation_id

    def _record_trusted_mcp_invocation_receipt(
        self,
        *,
        _authority: object | None = None,
        contract_id: str,
        attempt_id: str,
        server_name: str,
        tool_name: str,
        input_digest: str,
        output_digest: str,
        idempotency_key: str,
    ) -> str:
        if _authority is not self.__mcp_receipt_authority:
            raise IntentStateError(
                "trusted MCP receipt writer authority is missing or invalid"
            )
        contract_id = require_identifier(contract_id, "contract_id")
        attempt_id = require_identifier(attempt_id, "attempt_id")
        server_name = require_identifier(server_name, "server_name")
        tool_name = require_identifier(tool_name, "tool_name")
        input_digest = require_sha256(input_digest, "input_digest")
        output_digest = require_sha256(output_digest, "output_digest")
        idempotency_key = require_nonempty(idempotency_key, "idempotency_key")
        receipt_id = stable_identifier("mcp-receipt", idempotency_key)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT mcp_usage_receipts.*,
                       mcp_definitions.server_name AS server_name,
                       mcp_definitions.tool_name AS bound_tool_name
                FROM mcp_usage_receipts
                JOIN contract_mcp_bindings
                  ON contract_mcp_bindings.id =
                     mcp_usage_receipts.mcp_binding_id
                JOIN mcp_definitions
                  ON mcp_definitions.id =
                     contract_mcp_bindings.mcp_definition_id
                WHERE mcp_usage_receipts.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                signature = (
                    row["contract_id"],
                    row["attempt_id"],
                    row["server_name"],
                    row["tool_name"],
                    row["input_digest"],
                    row["output_digest"],
                )
                expected = (
                    contract_id,
                    attempt_id,
                    server_name,
                    tool_name,
                    input_digest,
                    output_digest,
                )
                if signature != expected:
                    raise IdempotencyConflictError(
                        "MCP receipt key was reused with different evidence"
                    )
                return row["id"]
            bindings = connection.execute(
                """
                SELECT contract_mcp_bindings.id AS mcp_binding_id
                FROM contract_attempts
                JOIN activation_contracts
                  ON activation_contracts.id = contract_attempts.contract_id
                JOIN contract_mcp_bindings
                  ON contract_mcp_bindings.contract_id =
                     activation_contracts.id
                JOIN mcp_definitions
                  ON mcp_definitions.id =
                     contract_mcp_bindings.mcp_definition_id
                WHERE contract_attempts.id = ?
                  AND contract_attempts.contract_id = ?
                  AND contract_attempts.state IN ('CREATED', 'RUNNING')
                  AND activation_contracts.state = 'RUNNING'
                  AND mcp_definitions.server_name = ?
                  AND mcp_definitions.tool_name = ?
                  AND mcp_definitions.state = 'ACTIVE'
                """,
                (attempt_id, contract_id, server_name, tool_name),
            ).fetchall()
            if len(bindings) != 1:
                raise IntentStateError(
                    "trusted MCP receipt requires one active contract, attempt, "
                    "server, and tool binding"
                )
            mcp_binding_id = bindings[0]["mcp_binding_id"]
            connection.execute(
                """
                INSERT INTO mcp_usage_receipts (
                    id, contract_id, attempt_id, mcp_binding_id, tool_name,
                    input_digest, output_digest, idempotency_key, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    contract_id,
                    attempt_id,
                    mcp_binding_id,
                    tool_name,
                    input_digest,
                    output_digest,
                    idempotency_key,
                    utc_now(),
                ),
            )
        return receipt_id

    def record_mcp_usage_receipt(
        self,
        *,
        contract_id: str,
        attempt_id: str,
        mcp_binding_id: str,
        tool_name: str,
        input_digest: str,
        output_digest: str,
        idempotency_key: str,
    ) -> str:
        """Removed public writer; only ``McpInvocationBroker`` may mint receipts."""

        raise IntentStateError(
            "record_mcp_usage_receipt is disabled; use the runtime MCP broker"
        )

    @staticmethod
    def _validate_trusted_mcp_receipt_references(
        connection: sqlite3.Connection,
        *,
        contract_id: str,
        attempt_id: str,
        receipts: Sequence[Mapping[str, str]],
    ) -> None:
        referenced_pairs: set[tuple[str, str]] = set()
        referenced_ids: set[str] = set()
        for receipt in receipts:
            receipt_id = require_identifier(receipt.get("receipt_id"), "receipt_id")
            server_name = require_identifier(receipt.get("server_id"), "server_id")
            tool_name = require_identifier(receipt.get("tool_id"), "tool_id")
            output_digest = require_sha256(
                receipt.get("evidence_sha256"), "evidence_sha256"
            )
            pair = (server_name, tool_name)
            if receipt_id in referenced_ids or pair in referenced_pairs:
                raise IntentStateError("MCP receipt references must be unique")
            row = connection.execute(
                """
                SELECT mcp_usage_receipts.contract_id,
                       mcp_usage_receipts.attempt_id,
                       mcp_usage_receipts.output_digest,
                       mcp_definitions.server_name,
                       mcp_definitions.tool_name
                FROM mcp_usage_receipts
                JOIN contract_mcp_bindings
                  ON contract_mcp_bindings.id =
                     mcp_usage_receipts.mcp_binding_id
                JOIN mcp_definitions
                  ON mcp_definitions.id =
                     contract_mcp_bindings.mcp_definition_id
                WHERE mcp_usage_receipts.id = ?
                """,
                (receipt_id,),
            ).fetchone()
            if row is None or (
                row["contract_id"] != contract_id
                or row["attempt_id"] != attempt_id
                or row["server_name"] != server_name
                or row["tool_name"] != tool_name
                or row["output_digest"] != output_digest
            ):
                raise IntentStateError(
                    "MCP result references fabricated or mismatched trusted evidence"
                )
            referenced_ids.add(receipt_id)
            referenced_pairs.add(pair)

        required_pairs = {
            (row["server_name"], row["tool_name"])
            for row in connection.execute(
                """
                SELECT mcp_definitions.server_name,
                       mcp_definitions.tool_name
                FROM contract_mcp_bindings
                JOIN mcp_definitions
                  ON mcp_definitions.id =
                     contract_mcp_bindings.mcp_definition_id
                WHERE contract_mcp_bindings.contract_id = ?
                  AND contract_mcp_bindings.invocation_required = 1
                """,
                (contract_id,),
            ).fetchall()
        }
        if not required_pairs <= referenced_pairs:
            raise IntentStateError("required trusted MCP invocation receipt is missing")

    def validate_trusted_mcp_receipt_references(
        self,
        *,
        contract_id: str,
        attempt_id: str,
        receipts: Sequence[Mapping[str, str]],
    ) -> None:
        contract_id = require_identifier(contract_id, "contract_id")
        attempt_id = require_identifier(attempt_id, "attempt_id")
        if not isinstance(receipts, Sequence) or isinstance(receipts, (str, bytes)):
            raise ValueError("receipts must be a sequence of objects")
        with self.transaction() as connection:
            self._validate_trusted_mcp_receipt_references(
                connection,
                contract_id=contract_id,
                attempt_id=attempt_id,
                receipts=receipts,
            )

    def record_serena_onboarding_snapshot(
        self,
        *,
        repo_id: str,
        source_oid: str,
        policy_digest: str,
        memory_bindings: list[dict[str, str]],
    ) -> str:
        repo_id = require_identifier(repo_id, "repo_id")
        source_oid = str(require_oid(source_oid, "source_oid"))
        policy_digest = require_sha256(policy_digest, "policy_digest")
        if not isinstance(memory_bindings, list) or not memory_bindings:
            raise ValueError("memory_bindings must be a non-empty list")
        normalized: list[SerenaMemoryBinding] = []
        for raw in memory_bindings:
            if not isinstance(raw, Mapping):
                raise ValueError("each memory binding must be an object")
            normalized.append(
                SerenaMemoryBinding(
                    memory_name=raw.get("memory_name") or raw.get("name") or "",
                    memory_ref=raw.get("memory_ref") or raw.get("reference") or "",
                    memory_sha256=raw.get("memory_sha256")
                    or raw.get("sha256")
                    or "",
                )
            )
        normalized.sort(key=lambda item: item.memory_name)
        if len({item.memory_name for item in normalized}) != len(normalized):
            raise ValueError("memory_bindings must not repeat memory_name")
        manifest = [
            {
                "memory_name": item.memory_name,
                "memory_ref": item.memory_ref,
                "memory_sha256": item.memory_sha256,
            }
            for item in normalized
        ]
        manifest_digest = hashlib.sha256(
            compact_json(manifest).encode("utf-8")
        ).hexdigest()
        snapshot_id = stable_identifier(
            "serena-snapshot",
            repo_id,
            source_oid,
            policy_digest,
            manifest_digest,
        )
        key = (
            f"serena-snapshot:{repo_id}:{source_oid}:"
            f"{policy_digest}:{manifest_digest}"
        )
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM serena_onboarding_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is not None:
                return row["id"]
            connection.execute(
                """
                INSERT INTO serena_onboarding_snapshots (
                    id, repository_id, source_oid, policy_digest,
                    memory_manifest_digest, state, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, 'ACCEPTED', ?, ?)
                """,
                (
                    snapshot_id,
                    repo_id,
                    source_oid,
                    policy_digest,
                    manifest_digest,
                    key,
                    utc_now(),
                ),
            )
            for ordinal, binding in enumerate(normalized):
                connection.execute(
                    """
                    INSERT INTO serena_snapshot_memory_bindings (
                        snapshot_id, ordinal, memory_name,
                        memory_ref, memory_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        ordinal,
                        binding.memory_name,
                        binding.memory_ref,
                        binding.memory_sha256,
                    ),
                )
        return snapshot_id

    def bind_contract_serena_memory(
        self,
        *,
        contract_id: str,
        snapshot_id: str,
        memory_name: str,
        ordinal: int,
    ) -> str:
        contract_id = require_identifier(contract_id, "contract_id")
        snapshot_id = require_identifier(snapshot_id, "snapshot_id")
        memory_name = require_identifier(memory_name, "memory_name")
        ordinal = require_nonnegative(ordinal, "ordinal")
        binding_id = stable_identifier(
            "serena-memory", contract_id, snapshot_id, memory_name
        )
        key = f"serena-memory:{contract_id}:{snapshot_id}:{memory_name}"
        with self.transaction(immediate=True) as connection:
            memory = connection.execute(
                """
                SELECT * FROM serena_snapshot_memory_bindings
                WHERE snapshot_id = ? AND memory_name = ?
                """,
                (snapshot_id, memory_name),
            ).fetchone()
            if memory is None:
                raise KeyError(f"{snapshot_id}:{memory_name}")
            row = connection.execute(
                """
                SELECT * FROM contract_serena_memory_bindings
                WHERE contract_id = ? AND memory_name = ?
                """,
                (contract_id, memory_name),
            ).fetchone()
            if row is not None:
                if row["snapshot_id"] != snapshot_id or row["ordinal"] != ordinal:
                    raise IdempotencyConflictError(
                        "contract Serena memory was rebound differently"
                    )
                return row["id"]
            connection.execute(
                """
                INSERT INTO contract_serena_memory_bindings (
                    id, contract_id, snapshot_id, memory_name, memory_ref,
                    memory_sha256, ordinal, idempotency_key, bound_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_id,
                    contract_id,
                    snapshot_id,
                    memory_name,
                    memory["memory_ref"],
                    memory["memory_sha256"],
                    ordinal,
                    key,
                    utc_now(),
                ),
            )
        return binding_id

    def record_serena_consumption_receipt(
        self,
        *,
        contract_id: str,
        memory_binding_id: str,
        receipt_digest: str,
        idempotency_key: str,
    ) -> str:
        contract_id = require_identifier(contract_id, "contract_id")
        memory_binding_id = require_identifier(
            memory_binding_id, "memory_binding_id"
        )
        receipt_digest = require_sha256(receipt_digest, "receipt_digest")
        idempotency_key = require_nonempty(idempotency_key, "idempotency_key")
        receipt_id = stable_identifier("serena-consumption", idempotency_key)
        with self.transaction(immediate=True) as connection:
            contract = connection.execute(
                """
                SELECT worker_fingerprint_id
                FROM activation_contracts WHERE id = ?
                """,
                (contract_id,),
            ).fetchone()
            if contract is None:
                raise KeyError(contract_id)
            row = connection.execute(
                """
                SELECT * FROM serena_consumption_receipts
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                if (
                    row["contract_id"] != contract_id
                    or row["memory_binding_id"] != memory_binding_id
                    or row["receipt_digest"] != receipt_digest
                ):
                    raise IdempotencyConflictError(
                        "Serena consumption key was reused"
                    )
                return row["id"]
            connection.execute(
                """
                INSERT INTO serena_consumption_receipts (
                    id, contract_id, memory_binding_id, worker_fingerprint_id,
                    receipt_digest, idempotency_key, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    contract_id,
                    memory_binding_id,
                    contract["worker_fingerprint_id"],
                    receipt_digest,
                    idempotency_key,
                    utc_now(),
                ),
            )
        return receipt_id

    def _ensure_metadata(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ax_schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    schema_version INTEGER NOT NULL CHECK(schema_version >= 0),
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ax_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    destructive INTEGER NOT NULL CHECK(destructive IN (0, 1)),
                    applied_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO ax_schema_meta(singleton, schema_version, updated_at)
                VALUES (1, 0, ?)
                ON CONFLICT(singleton) DO NOTHING
                """,
                (utc_now(),),
            )
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _apply_migration(self, migration: SchemaMigration) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT schema_version FROM ax_schema_meta WHERE singleton = 1"
                ).fetchone()
                if current is None:
                    raise SchemaCompatibilityError("schema metadata disappeared")
                version = current["schema_version"]
                if version >= migration.version:
                    connection.rollback()
                    return
                if version != migration.version - 1:
                    raise SchemaCompatibilityError(
                        f"cannot apply migration {migration.version} after version {version}"
                    )
                for ordinal, statement in enumerate(migration.statements):
                    connection.execute(statement)
                    self._migration_statement_applied(
                        migration.version,
                        ordinal,
                        connection,
                    )
                applied_at = utc_now()
                connection.execute(
                    """
                    INSERT INTO ax_schema_migrations(
                        version, name, destructive, applied_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        int(migration.destructive),
                        applied_at,
                    ),
                )
                connection.execute(
                    """
                    UPDATE ax_schema_meta
                    SET schema_version = ?, updated_at = ?
                    WHERE singleton = 1 AND schema_version = ?
                    """,
                    (migration.version, applied_at, version),
                )
                connection.commit()
            except Exception as exc:
                connection.rollback()
                if isinstance(exc, (SchemaCompatibilityError, SchemaMigrationError)):
                    raise
                raise SchemaMigrationError(
                    f"migration {migration.version} ({migration.name}) failed: {exc}"
                ) from exc

    def _migration_statement_applied(
        self,
        version: int,
        ordinal: int,
        connection: sqlite3.Connection,
    ) -> None:
        """Failure-injection seam; production implementations leave it empty."""

    def _require_backup(self, migration: SchemaMigration) -> Path:
        if self.backup_hook is None:
            raise SchemaMigrationError(
                f"destructive migration {migration.version} requires backup_hook"
            )
        backup = Path(
            self.backup_hook(self.db_path, self.schema_version())
        ).expanduser().resolve()
        if backup == self.db_path or not backup.is_file() or backup.stat().st_size == 0:
            raise SchemaMigrationError(
                f"backup hook did not produce a usable external backup: {backup}"
            )
        return backup

    @staticmethod
    def _validate_legacy_schema(connection: sqlite3.Connection) -> None:
        for table, required in LEGACY_REQUIRED_COLUMNS.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual = {row["name"] for row in rows}
            missing = sorted(required - actual)
            if missing:
                raise SchemaCompatibilityError(
                    f"legacy table {table} is missing columns: {', '.join(missing)}"
                )

    @staticmethod
    def _assert_same_intent(
        row: sqlite3.Row,
        *,
        operation: str,
        expected_state: str,
        expected_oid: str | None,
        payload_json: str,
    ) -> None:
        if (
            row["operation"] != operation
            or row["expected_state"] != expected_state
            or row["expected_oid"] != expected_oid
            or row["payload_json"] != payload_json
        ):
            raise IdempotencyConflictError(
                f"idempotency key {row['idempotency_key']!r} "
                "was reused for a different intent"
            )

    @staticmethod
    def _intent_from_row(row: sqlite3.Row) -> IntentRecord:
        return IntentRecord(
            intent_id=row["id"],
            operation=row["operation"],
            idempotency_key=row["idempotency_key"],
            expected_state=row["expected_state"],
            expected_oid=row["expected_oid"],
            payload=json.loads(row["payload_json"]),
            status=IntentStatus(row["status"]),
            created_at=row["created_at"],
            resulting_state=row["resulting_state"],
            resulting_oid=row["resulting_oid"],
            evidence=json.loads(row["evidence_json"]),
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _audit_signature(row: sqlite3.Row) -> tuple[Any, ...]:
        return (
            row["id"],
            row["event_type"],
            row["actor"],
            row["subject_type"],
            row["subject_id"],
            row["goal_id"],
            row["run_id"],
            row["activation_id"],
            row["subject_oid"],
            row["payload_json"],
            row["idempotency_key"],
            row["occurred_at"],
        )


def _mcp_receipt_writer_for_broker(
    state_store: AxStateStore,
    broker_authority: object,
) -> _McpReceiptWriter:
    """Private friend factory used only by ``McpInvocationBroker``."""

    if not isinstance(state_store, AxStateStore):
        raise TypeError("MCP receipt writer requires an AxStateStore")
    if broker_authority is not _MCP_RECEIPT_BROKER_AUTHORITY:
        raise IntentStateError("MCP receipt writer can be issued only to the broker")
    return _McpReceiptWriter(
        state_store,
        state_store._AxStateStore__mcp_receipt_authority,
    )
