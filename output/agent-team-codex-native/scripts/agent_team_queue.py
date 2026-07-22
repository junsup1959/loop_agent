from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from .agent_team_state import AxStateStore
except ImportError:  # Direct script/module execution from the repository root.
    from agent_team_state import AxStateStore


WakeHook = Callable[["Message"], None]
PROJECT_KNOWLEDGE_STATES = frozenset(
    {"new", "refresh_required", "ready", "deferred"}
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def from_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def compact_json(value: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class Message:
    seq: int
    id: str
    thread_id: str
    work_item_id: str
    parent_message_id: str | None
    from_role: str
    to_role: str
    type: str
    priority: int
    payload: dict[str, Any]
    status: str
    available_at: str
    claimed_by: str | None
    lease_until: str | None
    attempts: int
    max_attempts: int
    dedupe_key: str | None
    last_error: str | None
    created_at: str
    processed_at: str | None


@dataclass(frozen=True)
class OutboxEvent:
    seq: int
    id: str
    message_id: str
    event_type: str
    payload: dict[str, Any]
    status: str
    attempts: int
    available_at: str
    created_at: str
    published_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class ThreadSnapshot:
    id: str
    thread_id: str
    work_item_id: str
    target_role: str
    covered_through_seq: int
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class ProjectKnowledgeState:
    repo_id: str
    project_path: str
    baseline_oid: str | None
    inspected_oid: str | None
    source_fingerprint: str | None
    state: str
    memory_manifest: dict[str, Any]
    owner_seat_id: str | None
    evidence_artifact_ref: str | None
    memory_manifest_sha256: str | None
    last_request_message_id: str | None
    acknowledged_at: str | None
    updated_at: str


class QueueStateError(RuntimeError):
    """Raised when a message transition violates the queue state contract."""


class ShellEchoHook:
    """Human-visible message tap that is never fed back into agent context."""

    def __init__(
        self,
        script_path: str | Path,
        *,
        shell_executable: str = "sh",
        log_path: str | Path | None = None,
    ) -> None:
        self.script_path = Path(script_path).expanduser().resolve()
        self.shell_executable = shell_executable
        self.log_path = Path(log_path).expanduser().resolve() if log_path else None

    def __call__(self, message: Message) -> None:
        environment = os.environ.copy()
        if self.log_path is not None:
            environment["AGENT_TEAM_MESSAGE_LOG"] = str(self.log_path)
        subprocess.run(
            [self.shell_executable, str(self.script_path)],
            input=compact_json(asdict(message)),
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            check=True,
        )


class SQLiteMessageQueue:
    """Durable local message queue for role-to-role agent communication."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        wake_hook: WakeHook | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.wake_hook = wake_hook
        self.busy_timeout_ms = busy_timeout_ms
        self.state_store = AxStateStore(
            self.db_path,
            busy_timeout_ms=self.busy_timeout_ms,
        )
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        return self.state_store.connect()

    def initialize(self) -> None:
        self.state_store.initialize()

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> Message:
        return Message(
            seq=row["seq"],
            id=row["id"],
            thread_id=row["thread_id"],
            work_item_id=row["work_item_id"],
            parent_message_id=row["parent_message_id"],
            from_role=row["from_role"],
            to_role=row["to_role"],
            type=row["type"],
            priority=row["priority"],
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            available_at=row["available_at"],
            claimed_by=row["claimed_by"],
            lease_until=row["lease_until"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            dedupe_key=row["dedupe_key"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            processed_at=row["processed_at"],
        )

    @staticmethod
    def _outbox_from_row(row: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(
            seq=row["seq"],
            id=row["id"],
            message_id=row["message_id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            attempts=row["attempts"],
            available_at=row["available_at"],
            created_at=row["created_at"],
            published_at=row["published_at"],
            last_error=row["last_error"],
        )

    @staticmethod
    def _project_knowledge_state_from_row(row: sqlite3.Row) -> ProjectKnowledgeState:
        return ProjectKnowledgeState(
            repo_id=row["repo_id"],
            project_path=row["project_path"],
            baseline_oid=row["baseline_oid"],
            inspected_oid=row["inspected_oid"],
            source_fingerprint=row["source_fingerprint"],
            state=row["state"],
            memory_manifest=json.loads(row["memory_manifest_json"]),
            owner_seat_id=row["owner_seat_id"],
            evidence_artifact_ref=row["evidence_artifact_ref"],
            memory_manifest_sha256=row["memory_manifest_sha256"],
            last_request_message_id=row["last_request_message_id"],
            acknowledged_at=row["acknowledged_at"],
            updated_at=row["updated_at"],
        )

    def enqueue(
        self,
        *,
        thread_id: str,
        work_item_id: str,
        from_role: str,
        to_role: str,
        message_type: str,
        payload: Mapping[str, Any],
        parent_message_id: str | None = None,
        priority: int = 0,
        available_at: datetime | None = None,
        max_attempts: int = 5,
        dedupe_key: str | None = None,
        message_id: str | None = None,
    ) -> Message:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        message_id = message_id or f"msg-{uuid.uuid4()}"
        event_id = f"evt-{uuid.uuid4()}"
        created_at = utc_now()
        available_at = available_at or created_at
        outbox_payload = {
            "message_id": message_id,
            "thread_id": thread_id,
            "work_item_id": work_item_id,
            "to_role": to_role,
            "message_type": message_type,
        }
        inserted = False

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO messages (
                        id, thread_id, work_item_id, parent_message_id,
                        from_role, to_role, type, priority, payload_json,
                        status, available_at, max_attempts, dedupe_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        thread_id,
                        work_item_id,
                        parent_message_id,
                        from_role,
                        to_role,
                        message_type,
                        priority,
                        compact_json(dict(payload)),
                        to_timestamp(available_at),
                        max_attempts,
                        dedupe_key,
                        to_timestamp(created_at),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO outbox (
                        id, message_id, event_type, payload_json,
                        status, available_at, created_at
                    ) VALUES (?, ?, 'MESSAGE_ENQUEUED', ?, 'PENDING', ?, ?)
                    """,
                    (
                        event_id,
                        message_id,
                        compact_json(outbox_payload),
                        to_timestamp(created_at),
                        to_timestamp(created_at),
                    ),
                )
                inserted = True
            except sqlite3.IntegrityError:
                if not dedupe_key:
                    connection.rollback()
                    raise
                existing = connection.execute(
                    "SELECT * FROM messages WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if existing is None:
                    connection.rollback()
                    raise
                connection.commit()
                return self._message_from_row(existing)
            else:
                connection.commit()

        message = self.get(message_id)
        if inserted and self.wake_hook is not None:
            try:
                self.wake_hook(message)
            except Exception as exc:  # The durable outbox remains the recovery source.
                print(f"wake hook failed for {message_id}: {exc}", file=sys.stderr)
        return message

    def get(self, message_id: str) -> Message:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return self._message_from_row(row)

    def get_project_knowledge_state(
        self, repo_id: str
    ) -> ProjectKnowledgeState | None:
        if not isinstance(repo_id, str) or not repo_id:
            raise ValueError("repo_id must be a non-empty string")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_knowledge_state WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
        return self._project_knowledge_state_from_row(row) if row else None

    def upsert_project_knowledge_state(
        self,
        *,
        repo_id: str,
        project_path: str,
        baseline_oid: str | None,
        inspected_oid: str | None,
        source_fingerprint: str | None,
        state: str,
        memory_manifest: Mapping[str, Any] | None = None,
        owner_seat_id: str | None = None,
        evidence_artifact_ref: str | None = None,
        memory_manifest_sha256: str | None = None,
        last_request_message_id: str | None = None,
        acknowledged_at: str | None = None,
    ) -> ProjectKnowledgeState:
        if not isinstance(repo_id, str) or not repo_id:
            raise ValueError("repo_id must be a non-empty string")
        if not isinstance(project_path, str) or not project_path:
            raise ValueError("project_path must be a non-empty string")
        if state not in PROJECT_KNOWLEDGE_STATES:
            raise ValueError(f"Unsupported project knowledge state: {state!r}")
        if memory_manifest is not None and not isinstance(memory_manifest, Mapping):
            raise ValueError("memory_manifest must be an object when supplied")

        now = to_timestamp(utc_now())
        manifest_json = compact_json(dict(memory_manifest or {}))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO project_knowledge_state (
                    repo_id, project_path, baseline_oid, inspected_oid,
                    source_fingerprint, state, memory_manifest_json,
                    owner_seat_id, evidence_artifact_ref, memory_manifest_sha256,
                    last_request_message_id, acknowledged_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id) DO UPDATE SET
                    project_path = excluded.project_path,
                    baseline_oid = excluded.baseline_oid,
                    inspected_oid = excluded.inspected_oid,
                    source_fingerprint = excluded.source_fingerprint,
                    state = excluded.state,
                    memory_manifest_json = excluded.memory_manifest_json,
                    owner_seat_id = excluded.owner_seat_id,
                    evidence_artifact_ref = excluded.evidence_artifact_ref,
                    memory_manifest_sha256 = excluded.memory_manifest_sha256,
                    last_request_message_id = excluded.last_request_message_id,
                    acknowledged_at = excluded.acknowledged_at,
                    updated_at = excluded.updated_at
                """,
                (
                    repo_id,
                    project_path,
                    baseline_oid,
                    inspected_oid,
                    source_fingerprint,
                    state,
                    manifest_json,
                    owner_seat_id,
                    evidence_artifact_ref,
                    memory_manifest_sha256,
                    last_request_message_id,
                    acknowledged_at,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM project_knowledge_state WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise QueueStateError("Project knowledge state upsert did not return a row")
        return self._project_knowledge_state_from_row(row)

    def list_messages(
        self,
        *,
        thread_id: str | None = None,
        to_role: str | None = None,
        status: str | None = None,
        after_seq: int = 0,
        limit: int | None = 50,
    ) -> list[Message]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        clauses = ["seq > ?"]
        parameters: list[Any] = [after_seq]
        if thread_id is not None:
            clauses.append("thread_id = ?")
            parameters.append(thread_id)
        if to_role is not None:
            clauses.append("to_role IN (?, '*')")
            parameters.append(to_role)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            parameters.append(limit)
        where = " AND ".join(clauses)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM messages
                WHERE {where}
                ORDER BY seq
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def reap_expired(self) -> tuple[int, int]:
        now = to_timestamp(utc_now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            dead = connection.execute(
                """
                UPDATE messages
                SET status = 'DEAD_LETTER',
                    claimed_by = NULL,
                    lease_until = NULL,
                    processed_at = ?,
                    last_error = COALESCE(last_error, 'lease expired after max attempts')
                WHERE status IN ('CLAIMED', 'RUNNING')
                  AND lease_until <= ?
                  AND attempts >= max_attempts
                """,
                (now, now),
            ).rowcount
            pending = connection.execute(
                """
                UPDATE messages
                SET status = 'PENDING',
                    claimed_by = NULL,
                    lease_until = NULL,
                    available_at = ?,
                    last_error = COALESCE(last_error, 'consumer lease expired')
                WHERE status IN ('CLAIMED', 'RUNNING')
                  AND lease_until <= ?
                  AND attempts < max_attempts
                """,
                (now, now),
            ).rowcount
            connection.commit()
        return pending, dead

    def claim(
        self,
        *,
        to_role: str,
        consumer_id: str,
        limit: int = 1,
        lease_seconds: int = 60,
    ) -> list[Message]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")

        self.reap_expired()
        now = utc_now()
        lease_until = now + timedelta(seconds=lease_seconds)

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT *
                FROM messages
                WHERE status IN ('PENDING', 'RETRY')
                  AND available_at <= ?
                  AND attempts < max_attempts
                  AND to_role IN (?, '*')
                ORDER BY priority DESC, seq
                LIMIT ?
                """,
                (to_timestamp(now), to_role, limit),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"""
                    UPDATE messages
                    SET status = 'CLAIMED',
                        claimed_by = ?,
                        lease_until = ?,
                        attempts = attempts + 1
                    WHERE id IN ({placeholders})
                    """,
                    (consumer_id, to_timestamp(lease_until), *ids),
                )
                rows = connection.execute(
                    f"SELECT * FROM messages WHERE id IN ({placeholders}) ORDER BY priority DESC, seq",
                    ids,
                ).fetchall()
            connection.commit()
        return [self._message_from_row(row) for row in rows]

    def mark_running(self, message_id: str, *, consumer_id: str) -> Message:
        return self._transition_owned(
            message_id,
            consumer_id=consumer_id,
            expected=("CLAIMED",),
            target="RUNNING",
        )

    def acknowledge(self, message_id: str, *, consumer_id: str) -> Message:
        return self._transition_owned(
            message_id,
            consumer_id=consumer_id,
            expected=("CLAIMED", "RUNNING"),
            target="ACKED",
            terminal=True,
        )

    def retry(
        self,
        message_id: str,
        *,
        consumer_id: str,
        error: str,
        delay_seconds: int = 0,
    ) -> Message:
        current = self.get(message_id)
        if current.claimed_by != consumer_id:
            raise QueueStateError(
                f"{message_id} is claimed by {current.claimed_by!r}, not {consumer_id!r}"
            )
        if current.status not in {"CLAIMED", "RUNNING"}:
            raise QueueStateError(f"{message_id} cannot retry from {current.status}")

        target = "DEAD_LETTER" if current.attempts >= current.max_attempts else "RETRY"
        available_at = utc_now() + timedelta(seconds=max(delay_seconds, 0))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE messages
                SET status = ?,
                    available_at = ?,
                    claimed_by = NULL,
                    lease_until = NULL,
                    last_error = ?,
                    processed_at = CASE WHEN ? = 'DEAD_LETTER' THEN ? ELSE NULL END
                WHERE id = ?
                  AND claimed_by = ?
                  AND status IN ('CLAIMED', 'RUNNING')
                """,
                (
                    target,
                    to_timestamp(available_at),
                    error,
                    target,
                    to_timestamp(utc_now()),
                    message_id,
                    consumer_id,
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise QueueStateError(f"concurrent transition rejected for {message_id}")
            connection.commit()
        return self.get(message_id)

    def _transition_owned(
        self,
        message_id: str,
        *,
        consumer_id: str,
        expected: tuple[str, ...],
        target: str,
        terminal: bool = False,
    ) -> Message:
        placeholders = ",".join("?" for _ in expected)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                f"""
                UPDATE messages
                SET status = ?,
                    processed_at = ?,
                    lease_until = CASE WHEN ? THEN NULL ELSE lease_until END
                WHERE id = ?
                  AND claimed_by = ?
                  AND status IN ({placeholders})
                """,
                (
                    target,
                    to_timestamp(utc_now()) if terminal else None,
                    1 if terminal else 0,
                    message_id,
                    consumer_id,
                    *expected,
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                current = self.get(message_id)
                raise QueueStateError(
                    f"{message_id} transition {current.status}->{target} rejected "
                    f"for consumer {consumer_id!r}"
                )
            connection.commit()
        return self.get(message_id)

    def pending_outbox(self, *, limit: int = 100) -> list[OutboxEvent]:
        now = to_timestamp(utc_now())
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM outbox
                WHERE status IN ('PENDING', 'RETRY')
                  AND available_at <= ?
                ORDER BY seq
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [self._outbox_from_row(row) for row in rows]

    def mark_outbox_published(self, event_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE outbox
                SET status = 'PUBLISHED',
                    published_at = ?,
                    attempts = attempts + 1,
                    last_error = NULL
                WHERE id = ?
                  AND status IN ('PENDING', 'RETRY')
                """,
                (to_timestamp(utc_now()), event_id),
            )

    def mark_outbox_failed(
        self,
        event_id: str,
        *,
        error: str,
        delay_seconds: int = 1,
        max_attempts: int = 10,
    ) -> None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT attempts FROM outbox WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            attempts = row["attempts"] + 1
            status = "DEAD_LETTER" if attempts >= max_attempts else "RETRY"
            connection.execute(
                """
                UPDATE outbox
                SET status = ?,
                    attempts = ?,
                    available_at = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (
                    status,
                    attempts,
                    to_timestamp(utc_now() + timedelta(seconds=max(delay_seconds, 0))),
                    error,
                    event_id,
                ),
            )

    def save_snapshot(
        self,
        *,
        thread_id: str,
        work_item_id: str,
        target_role: str,
        covered_through_seq: int,
        payload: Mapping[str, Any],
    ) -> ThreadSnapshot:
        snapshot_id = f"snapshot-{uuid.uuid4()}"
        created_at = to_timestamp(utc_now())
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO thread_snapshots (
                    id, thread_id, work_item_id, target_role,
                    covered_through_seq, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    thread_id,
                    work_item_id,
                    target_role,
                    covered_through_seq,
                    compact_json(dict(payload)),
                    created_at,
                ),
            )
        return ThreadSnapshot(
            id=snapshot_id,
            thread_id=thread_id,
            work_item_id=work_item_id,
            target_role=target_role,
            covered_through_seq=covered_through_seq,
            payload=dict(payload),
            created_at=created_at,
        )

    def latest_snapshot(
        self,
        *,
        thread_id: str,
        target_role: str,
        work_item_id: str | None = None,
    ) -> ThreadSnapshot | None:
        query = """
            SELECT *
            FROM thread_snapshots
            WHERE thread_id = ?
              AND target_role = ?
        """
        parameters: list[str] = [thread_id, target_role]
        if work_item_id is not None:
            query += " AND work_item_id = ?"
            parameters.append(work_item_id)
        query += " ORDER BY covered_through_seq DESC LIMIT 1"
        with closing(self._connect()) as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            return None
        return ThreadSnapshot(
            id=row["id"],
            thread_id=row["thread_id"],
            work_item_id=row["work_item_id"],
            target_role=row["target_role"],
            covered_through_seq=row["covered_through_seq"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
        )

    def context_delta(
        self,
        *,
        thread_id: str,
        target_role: str,
        work_item_id: str | None = None,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list[Message]:
        query = """
            SELECT *
            FROM messages
            WHERE thread_id = ?
              AND seq > ?
              AND (to_role IN (?, '*') OR from_role = ?)
        """
        parameters: list[Any] = [thread_id, after_seq, target_role, target_role]
        if work_item_id is not None:
            query += " AND work_item_id = ?"
            parameters.append(work_item_id)
        query += " ORDER BY seq LIMIT ?"
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._message_from_row(row) for row in rows]

    def context_bundle(
        self,
        *,
        thread_id: str,
        target_role: str,
        work_item_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        snapshot = self.latest_snapshot(
            thread_id=thread_id,
            target_role=target_role,
            work_item_id=work_item_id,
        )
        covered = snapshot.covered_through_seq if snapshot else 0
        delta = self.context_delta(
            thread_id=thread_id,
            target_role=target_role,
            work_item_id=work_item_id,
            after_seq=covered,
            limit=limit,
        )
        return {
            "snapshot": asdict(snapshot) if snapshot else None,
            "delta_messages": [asdict(message) for message in delta],
            "truncated": len(delta) >= limit,
        }


def parse_json_argument(value: str) -> dict[str, Any]:
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("payload must be a JSON object")
    return parsed


def json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local SQLite agent message queue")
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument(
        "--echo-script",
        help="Optional post-commit sh script that echoes the message for humans",
    )
    parser.add_argument(
        "--shell-executable",
        default=os.environ.get("AGENT_TEAM_SH", "sh"),
    )
    parser.add_argument(
        "--message-log",
        help="Optional human-only log path written by the echo script",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("--thread", required=True)
    enqueue.add_argument("--work-item", required=True)
    enqueue.add_argument("--from-role", required=True)
    enqueue.add_argument("--to-role", required=True)
    enqueue.add_argument("--type", required=True)
    enqueue.add_argument("--payload", default="{}")
    enqueue.add_argument("--parent-message-id")
    enqueue.add_argument("--priority", type=int, default=0)
    enqueue.add_argument("--dedupe-key")

    claim = subparsers.add_parser("claim")
    claim.add_argument("--role", required=True)
    claim.add_argument("--consumer", required=True)
    claim.add_argument("--limit", type=int, default=1)
    claim.add_argument("--lease-seconds", type=int, default=60)

    running = subparsers.add_parser("running")
    running.add_argument("--message-id", required=True)
    running.add_argument("--consumer", required=True)

    ack = subparsers.add_parser("ack")
    ack.add_argument("--message-id", required=True)
    ack.add_argument("--consumer", required=True)

    retry = subparsers.add_parser("retry")
    retry.add_argument("--message-id", required=True)
    retry.add_argument("--consumer", required=True)
    retry.add_argument("--error", required=True)
    retry.add_argument("--delay-seconds", type=int, default=0)

    context = subparsers.add_parser("context")
    context.add_argument("--thread", required=True)
    context.add_argument("--role", required=True)
    context.add_argument("--limit", type=int, default=200)

    outbox = subparsers.add_parser("outbox")
    outbox.add_argument("--limit", type=int, default=100)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    echo_script = args.echo_script or os.environ.get("AGENT_TEAM_MESSAGE_ECHO_SCRIPT")
    wake_hook = (
        ShellEchoHook(
            echo_script,
            shell_executable=args.shell_executable,
            log_path=args.message_log,
        )
        if echo_script
        else None
    )
    queue = SQLiteMessageQueue(args.db, wake_hook=wake_hook)

    if args.command == "init":
        json_print({"db": str(queue.db_path), "status": "initialized"})
    elif args.command == "enqueue":
        message = queue.enqueue(
            thread_id=args.thread,
            work_item_id=args.work_item,
            from_role=args.from_role,
            to_role=args.to_role,
            message_type=args.type,
            payload=parse_json_argument(args.payload),
            parent_message_id=args.parent_message_id,
            priority=args.priority,
            dedupe_key=args.dedupe_key,
        )
        json_print(asdict(message))
    elif args.command == "claim":
        messages = queue.claim(
            to_role=args.role,
            consumer_id=args.consumer,
            limit=args.limit,
            lease_seconds=args.lease_seconds,
        )
        json_print([asdict(message) for message in messages])
    elif args.command == "running":
        json_print(
            asdict(queue.mark_running(args.message_id, consumer_id=args.consumer))
        )
    elif args.command == "ack":
        json_print(asdict(queue.acknowledge(args.message_id, consumer_id=args.consumer)))
    elif args.command == "retry":
        json_print(
            asdict(
                queue.retry(
                    args.message_id,
                    consumer_id=args.consumer,
                    error=args.error,
                    delay_seconds=args.delay_seconds,
                )
            )
        )
    elif args.command == "context":
        json_print(
            queue.context_bundle(
                thread_id=args.thread,
                target_role=args.role,
                limit=args.limit,
            )
        )
    elif args.command == "outbox":
        json_print([asdict(event) for event in queue.pending_outbox(limit=args.limit)])
    else:
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
